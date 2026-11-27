#!/usr/bin/env pythonw
# phoenixmp3 - GUI for downloading YouTube links and tagging them with beets.
import collections
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import urllib.parse
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

APP_NAME = "phoenixmp3"

PIPELINE = Path(__file__).resolve().parent
MUSIC = PIPELINE.parent
BIN = PIPELINE / "bin"
LIBS = PIPELINE / "libs"
RAW = PIPELINE / "newdownloads"
FINAL = MUSIC / "downloadswithmeta"
YTDLP = PIPELINE / "yt-dlp-master" / "yt_dlp" / "__main__.py"
ICON = PIPELINE / "icon.ico"
RUNLOG = PIPELINE / "last-run.log"
ARCHIVE = PIPELINE / "downloaded.txt"        # yt-dlp's list of already downloaded IDs
UNAVAILABLE = PIPELINE / "unavailable.txt"   # IDs YouTube won't serve
REPLACED = PIPELINE / "replaced.txt"         # other uploads found for those
PLAYLISTS = MUSIC / "playlists.txt"
SETTINGS_FILE = PIPELINE / "settings.json"
RUN_CONFIG = PIPELINE / "run-config.yaml"    # beets config overlay, rebuilt each run

PLAYLISTS_TEMPLATE = """\
# phoenixmp3 - My Playlists
# Put one YouTube / YouTube Music playlist link per line, then save this file.
# "Update My Playlists" in the app downloads only the songs you don't have yet.
# Lines starting with # are ignored, so you can label your playlists:
#
# Gym
# https://music.youtube.com/playlist?list=PLxxxxxxxx
"""

DEFAULTS = {
    "format": "aac",          # aac or mp3
    "layout": "flat",         # flat or folders
    "output_dir": "",         # "" means FINAL
    "square_art": True,
    "small_art": False,
    "playlist_files": False,
    "replaygain": False,
    "pause": 10,              # seconds between songs, so YouTube doesn't block us
}
LAYOUTS = {
    "flat": "$artist - $title",
    "folders": "$artist/%if{$album,$album,Singles}/$title",
}

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Run helper steps with python.exe rather than pythonw.exe. A pythonw child has
# no console, so every console tool it starts (ffmpeg, deno, fpcalc) would flash
# up its own window.
_console_py = Path(sys.executable).with_name("python.exe")
PY = str(_console_py) if _console_py.exists() else sys.executable

# yt-dlp errors that won't go away by retrying. Kept specific on purpose: an
# outdated yt-dlp says "Requested format is not available", and a bare "not
# available" used to mark whole playlists as dead after one broken run.
_ERR_RE = re.compile(r"ERROR: \[youtube\] ([0-9A-Za-z_-]{11}): (.+)")
_PERMANENT = ("video unavailable", "video is not available", "not made this video available",
              "private video", "has been removed", "copyright", "terminated",
              "confirm your age", "inappropriate", "members-only", "join this channel")
_NOT_PERMANENT = ("requested format", "on this app", "not a bot")


# Settings. The output folder is saved as "" (default) or relative to the app
# folder, so moving or renaming the folder doesn't break it.

def out_dir(s):
    p = Path(s.get("output_dir") or FINAL)
    return p if p.is_absolute() else MUSIC / p


def store_dir(path):
    p = Path(path)
    if p == FINAL:
        return ""
    try:
        return str(p.relative_to(MUSIC))
    except ValueError:
        return str(p)


def load_settings():
    s = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        s.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    # Older versions saved a full path. Reset it if it's the default folder or
    # points into a folder that has since been renamed.
    p = Path(s["output_dir"]) if s["output_dir"] else None
    if p and p.is_absolute() and (p == FINAL or (p.name == FINAL.name and not p.parent.exists())):
        s["output_dir"] = ""
    elif p and p.is_absolute():
        s["output_dir"] = store_dir(p)
    return s


def save_settings(s):
    # Write then rename: reading a half-written file would fall back to the defaults,
    # and the next save would keep those (the new window reads settings from other threads).
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    os.replace(tmp, SETTINGS_FILE)


def write_run_config(s):
    plugins = "musicbrainz chroma ftintitle" + (" replaygain" if s["replaygain"] else "")
    lines = [
        "# Generated from settings.json on every run.",
        f"directory: {json.dumps(out_dir(s).as_posix())}",
        "paths:",
        f"  singleton: {json.dumps(LAYOUTS[s['layout']])}",
        f"plugins: {plugins}",
    ]
    if s["replaygain"]:
        lines += ["replaygain:", "  backend: ffmpeg", "  auto: yes"]
    RUN_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return RUN_CONFIG


def settings_summary(s):
    fmt = "AAC (.m4a)" if s["format"] == "aac" else "MP3"
    lay = "one folder" if s["layout"] == "flat" else "Artist\\Album folders"
    return f"Saving as {fmt}, {lay}, to {out_dir(s)}"


# Saved playlists, download archive, unavailable songs

def saved_playlists():
    try:
        lines = PLAYLISTS.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []
    return [ln.strip().strip('"') for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]


def archived_ids():
    try:
        lines = ARCHIVE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {ln.split()[-1] for ln in lines if ln.strip()}


def unavailable_ids():
    try:
        lines = UNAVAILABLE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {ln.split()[0] for ln in lines if ln.strip() and not ln.startswith("#")}


def record_unavailable(log_lines):
    """Add songs that failed for a permanent reason to unavailable.txt, so they
    don't show up as new on every playlist check."""
    known = unavailable_ids() | archived_ids()
    found = {}
    for ln in log_lines:
        m = _ERR_RE.search(ln)
        if not m or m.group(1) in known:
            continue
        why = m.group(2).lower()
        if any(p in why for p in _PERMANENT) and not any(p in why for p in _NOT_PERMANENT):
            found[m.group(1)] = m.group(2).strip()
    if found:
        with UNAVAILABLE.open("a", encoding="utf-8") as f:
            for vid, why in found.items():
                f.write(f"{vid}  # {why}\n")
    return len(found)


def is_youtube_link(url):
    try:
        u = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and (
        host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"))


def replacements():
    """{old id: new id} from replaced.txt. The new id is "" when no other
    upload was found, so the song isn't searched for again."""
    out = {}
    try:
        lines = REPLACED.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for ln in lines:
        parts = ln.split("#")[0].split()
        if len(parts) >= 2:
            out[parts[0]] = "" if parts[1] == "-" else parts[1]
    return out


def dead_note(entries, dead):
    """"  (3 from another copy, 2 unavailable)" for a playlist summary."""
    have, rep = archived_ids(), replacements()
    ids = [e.get("id") for e in entries if e.get("id") in dead]
    swapped = sum(1 for i in ids if rep.get(i) in have)
    gone = len(ids) - swapped
    parts = ([f"{swapped} from another copy"] if swapped else []) + \
            ([f"{gone} unavailable"] if gone else [])
    return f"  ({', '.join(parts)})" if parts else ""


# Helpers

def runlog(line):
    try:
        with RUNLOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line if line.endswith("\n") else line + "\n")
    except OSError:
        pass


def child_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(LIBS)
    env["BEETSDIR"] = str(PIPELINE)
    env["FPCALC"] = str(BIN / "fpcalc.exe")
    env["PATH"] = str(BIN) + os.pathsep + env.get("PATH", "")
    env["PIP_USER"] = "0"
    env["PYTHONNOUSERSITE"] = "1"   # ignore packages installed elsewhere on the PC
    # Song names can have characters the Windows console encoding can't print
    # (yt-dlp swaps "/" in titles for a lookalike Unicode slash), which used to
    # crash the helpers halfway through a folder.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def strip_id(name):
    return re.sub(r"\s*\[[0-9A-Za-z_-]{11}\]$", "", name)


def kill_tree(pid):
    # taskkill /T also ends the ffmpeg/deno processes yt-dlp started.
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   creationflags=NO_WINDOW, capture_output=True)


def clean_raw():
    """Delete leftovers from newdownloads: cover art from failed downloads and
    half-finished files. A song file whose ID isn't in downloaded.txt didn't
    finish (yt-dlp only records it once it's done), so it goes too and gets
    downloaded again next time."""
    if not RAW.exists():
        return
    done = archived_ids()
    for p in RAW.iterdir():
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        m = re.search(r"\[([0-9A-Za-z_-]{11})\]", p.name)
        unfinished = ext in (".m4a", ".mp3") and m and m.group(1) not in done
        if ext in (".jpg", ".webp", ".png", ".part", ".ytdl") or ".temp." in p.name or unfinished:
            try:
                p.unlink()
            except OSError:
                pass


def raw_songs():
    if not RAW.exists():
        return []
    return [p for p in RAW.iterdir() if p.suffix.lower() in (".m4a", ".mp3")]


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.proc = None        # the step currently running
        self.stopping = False   # Stop was clicked
        self.failed = []        # steps that crashed this run: "name (reason)"
        self.cut_step = False   # end the current step early (YouTube blocking)
        self.titles = {}        # video id -> title, for error messages
        self.songs = {}         # video id -> title, channel, length
        self.settings = load_settings()
        root.title(APP_NAME)
        try:
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
        root.geometry("760x640")
        root.minsize(700, 540)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        pad = dict(padx=12, pady=6)
        bold = ("Segoe UI", 9, "bold")

        # One-off links
        link_box = ttk.LabelFrame(root, labelwidget=ttk.Label(root, text="Download a link", font=bold),
                                  padding=8)
        link_box.pack(fill="x", **pad)
        ttk.Label(link_box, text="Paste a song, album or playlist link:").pack(anchor="w")
        row = ttk.Frame(link_box)
        row.pack(fill="x", pady=(2, 0))
        self.url = ttk.Entry(row)
        self.url.pack(side="left", fill="x", expand=True)
        self.url.focus()
        self.preview_btn = ttk.Button(row, text="Preview", command=self.on_preview)
        self.preview_btn.pack(side="left", padx=(6, 0))
        self.dl_btn = ttk.Button(row, text="Download", command=self.on_download)
        self.dl_btn.pack(side="left", padx=(6, 0))

        # Saved playlists
        pl_box = ttk.LabelFrame(root, labelwidget=ttk.Label(root, text="My Playlists", font=bold),
                                padding=8)
        pl_box.pack(fill="x", padx=12, pady=(0, 6))
        ttk.Label(pl_box, text="Save the playlists you keep adding to, then grab just the new "
                  "songs by clicking Update My Playlists").pack(anchor="w")
        prow = ttk.Frame(pl_box)
        prow.pack(fill="x", pady=(4, 0))
        self.check_btn = ttk.Button(prow, text="Update My Playlists", command=self.on_check_playlists)
        self.check_btn.pack(side="left")
        ttk.Button(prow, text="Add or remove My Playlists", command=self.on_edit_list).pack(
            side="left", padx=(6, 0))
        self.plpreview_btn = ttk.Button(prow, text="Preview My Playlists",
                                        command=self.on_preview_playlists)
        self.plpreview_btn.pack(side="left", padx=(6, 0))
        self.pl_count = ttk.Label(prow, foreground="#666")
        self.pl_count.pack(side="right")
        self.show_playlist_count()
        # Refresh the count after the list is edited in Notepad.
        root.bind("<FocusIn>", lambda e: self.show_playlist_count() if e.widget is root else None)

        ttk.Label(root, text="Preview:").pack(anchor="w", padx=12)
        self.info = tk.Text(root, height=8, wrap="none", state="disabled")
        self.info.pack(fill="both", expand=True, padx=12)

        bottom = ttk.Frame(root)
        bottom.pack(fill="x", **pad)
        self.summary = ttk.Label(bottom, text=settings_summary(self.settings), foreground="#666")
        self.summary.pack(side="left")
        self.stop_btn = ttk.Button(bottom, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.settings_btn = ttk.Button(bottom, text="Settings", command=self.open_settings)
        self.settings_btn.pack(side="right", padx=(0, 6))

        ttk.Label(root, text="Progress:").pack(anchor="w", padx=12)
        self.log = tk.Text(root, height=8, wrap="word", state="disabled", bg="#111", fg="#ddd")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.status = ttk.Label(root, text="Ready.", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

        self.root.after(100, self.drain_queue)

    # Settings window

    def open_settings(self):
        if self.busy:
            return
        s = self.settings
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        hint = dict(foreground="#666")

        fmt = tk.StringVar(value=s["format"])
        layout = tk.StringVar(value=s["layout"])
        out = tk.StringVar(value=str(out_dir(s)))
        square = tk.BooleanVar(value=s["square_art"])
        small = tk.BooleanVar(value=s["small_art"])
        m3u = tk.BooleanVar(value=s["playlist_files"])
        rg = tk.BooleanVar(value=s["replaygain"])

        def section(text):
            ttk.Label(body, text=text, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 2))

        section("Audio format")
        ttk.Radiobutton(body, text="AAC (.m4a): best quality, keeps YouTube's audio as it is. "
                        "For iPods, phones and most players.", variable=fmt, value="aac").pack(anchor="w")
        ttk.Radiobutton(body, text="MP3 (about 190 kbps): plays on everything, including car "
                        "stereos and cheap MP3 players.", variable=fmt, value="mp3").pack(anchor="w")

        section("Folder layout")
        ttk.Radiobutton(body, text='All songs in one folder ("Artist - Title")',
                        variable=layout, value="flat").pack(anchor="w")
        ttk.Radiobutton(body, text="Artist\\Album folders (for players and car stereos that "
                        "browse by folder)", variable=layout, value="folders").pack(anchor="w")

        section("Save songs to")
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=out, width=58, state="readonly").pack(side="left")

        def browse():
            d = filedialog.askdirectory(parent=win, initialdir=out.get(),
                                        title="Choose where to save songs")
            if d:
                out.set(str(Path(d)))
        ttk.Button(row, text="Browse...", command=browse).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="Default", command=lambda: out.set(str(FINAL))).pack(
            side="left", padx=(6, 0))
        ttk.Label(body, text="Changing this doesn't move songs you've already downloaded.",
                  **hint).pack(anchor="w")

        section("Cover art")
        small_chk = ttk.Checkbutton(body, text="Smaller cover art (300 px) for players that "
                                    "struggle with big images", variable=small)

        def sync_small(*_):
            small_chk.config(state="normal" if square.get() else "disabled")
        ttk.Checkbutton(body, text="Square-crop cover art (YouTube thumbnails are widescreen)",
                        variable=square, command=sync_small).pack(anchor="w")
        small_chk.pack(anchor="w")
        sync_small()

        section("Pause between songs")
        pause = tk.IntVar(value=int(s["pause"]))
        prow = ttk.Frame(body)
        prow.pack(fill="x")
        pause_label = ttk.Label(prow, width=12)

        def show_pause(*_):
            pause.set(round(float(pause.get())))
            v = pause.get()
            pause_label.config(text="no wait" if v == 0 else f"about {round(v * 1.5)} sec")
        ttk.Scale(prow, from_=0, to=30, orient="horizontal", length=300, variable=pause,
                  command=show_pause).pack(side="left")
        pause_label.pack(side="left", padx=(8, 0))
        show_pause()
        ttk.Label(body, text="How long to wait after each song, so YouTube doesn't block you for "
                  "downloading too much at once.\nBig playlists take longer, but no wait can get "
                  "you blocked after 50 or so songs.", **hint).pack(anchor="w")

        section("Extras")
        ttk.Checkbutton(body, text="Write playlist files (.m3u8) for My Playlists "
                        "when you click Update My Playlists", variable=m3u).pack(anchor="w")
        ttk.Checkbutton(body, text="Volume levelling (ReplayGain tags)", variable=rg).pack(anchor="w")
        ttk.Label(body, text="Evens out loudness between songs. Only the tags change, not the "
                  "audio. Also writes iTunes Sound Check\ndata, which iPods only use if your sync "
                  "tool copies it over. Makes tagging a bit slower.", **hint).pack(anchor="w")

        ttk.Label(body, text="Settings apply to new downloads only.", **hint).pack(
            anchor="w", pady=(12, 0))

        def save():
            self.settings = {
                "format": fmt.get(), "layout": layout.get(), "output_dir": store_dir(out.get()),
                "square_art": square.get(), "small_art": small.get(),
                "playlist_files": m3u.get(), "replaygain": rg.get(), "pause": pause.get(),
            }
            try:
                save_settings(self.settings)
            except OSError as e:
                messagebox.showerror("Couldn't save settings", str(e), parent=win)
                return
            self.summary.config(text=settings_summary(self.settings))
            win.destroy()

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=save).pack(side="right", padx=(0, 6))

    # UI helpers

    def set_busy(self, busy, status=None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for b in (self.preview_btn, self.dl_btn, self.check_btn, self.plpreview_btn,
                  self.settings_btn):
            b.config(state=state)
        self.stop_btn.config(state="normal" if busy else "disabled")
        if status:
            self.status.config(text=status)

    def on_stop(self):
        if not self.busy or self.stopping:
            return
        self.stopping = True
        self.stop_btn.config(state="disabled")
        self.status.config(text="Stopping...")
        if self.proc and self.proc.poll() is None:
            kill_tree(self.proc.pid)

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("Still working",
                                       "A download is still running. Stop it and close?"):
                return
            self.stopping = True
            if self.proc and self.proc.poll() is None:
                kill_tree(self.proc.pid)
            clean_raw()
        self.root.destroy()

    def write_info(self, text):
        self.info.config(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("end", text)
        self.info.config(state="disabled")

    @staticmethod
    def start_runlog(what):
        import datetime
        try:
            RUNLOG.write_text(f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {what}\n", encoding="utf-8")
        except OSError:
            pass

    def log_line(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def get_url(self):
        url = self.url.get().strip().strip('"')
        if not url:
            messagebox.showwarning("No link", "Paste a YouTube link first.")
            return None
        if not is_youtube_link(url):
            messagebox.showwarning("Not a YouTube link",
                                   "That doesn't look like a YouTube link. It should start "
                                   "with https://www.youtube.com/, https://music.youtube.com/ "
                                   "or https://youtu.be/")
            return None
        return url

    # Preview

    def on_preview(self):
        url = self.get_url()
        if not url or self.busy:
            return
        self.set_busy(True, "Looking up link...")
        self.write_info("Loading...")
        threading.Thread(target=self._preview_worker, args=(url,), daemon=True).start()

    @staticmethod
    def _lookup(url):
        """Get a link's info and track list from yt-dlp without downloading."""
        out = subprocess.run(
            [PY, str(YTDLP), "--flat-playlist", "--dump-single-json",
             "--remote-components", "ejs:github", "--", url],
            capture_output=True, text=True, env=child_env(),
            creationflags=NO_WINDOW, timeout=120,
        )
        # On failure yt-dlp prints "null" or nothing, with the reason on stderr.
        data = json.loads(out.stdout) if out.stdout.strip() else None
        if not isinstance(data, dict):
            err = [ln for ln in (out.stderr or "").splitlines() if "ERROR" in ln]
            msg = err[-1] if err else "yt-dlp couldn't read this link"
            raise RuntimeError(msg.replace("ERROR: ", "", 1))
        return data

    def _preview_worker(self, url):
        try:
            self.q.put(("preview", self._lookup(url)))
        except Exception as e:
            self.q.put(("preview_err", str(e)))

    def _render_preview(self, data):
        if data.get("_type") == "playlist" or "entries" in data:
            entries = [e for e in (data.get("entries") or []) if e]
            have, dead, rep = archived_ids(), unavailable_ids(), replacements()
            new = sum(1 for e in entries if e.get("id") not in have | dead)
            lines = [f"PLAYLIST: {data.get('title', '(untitled)')}",
                     f"{plural(len(entries), 'track')}, {new} new{dead_note(entries, dead)}", ""]
            for i, e in enumerate(entries, 1):
                vid = e.get("id")
                note = ("  (have it)" if vid in have
                        else "  (have another copy)" if rep.get(vid) in have
                        else "  (unavailable)" if vid in dead else "")
                lines.append(f"{i:>3}. {e.get('title', '(unknown)')}{note}")
            self.write_info("\n".join(lines))
            if new:
                self.status.config(text=f"Playlist: {new} new of {plural(len(entries), 'track')}.")
            else:
                self.status.config(text=f"Playlist: you already have all {len(entries)} tracks.")
        else:
            title = data.get("title", "(unknown)")
            up = data.get("uploader", "")
            dur = data.get("duration")
            mins = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
            got = data.get("id") in archived_ids()
            note = "\n\nAlready downloaded, will be skipped." if got else ""
            self.write_info(f"SINGLE TRACK\n\n{title}\nby {up}\nlength {mins}{note}")
            self.status.config(text="Already downloaded." if got else "Single track.")

    # Download

    def on_download(self):
        url = self.get_url()
        if not url or self.busy:
            return
        s = dict(self.settings)
        RAW.mkdir(exist_ok=True)
        out_dir(s).mkdir(parents=True, exist_ok=True)
        self.stopping = False
        self.titles = {}
        self.songs = {}
        self.set_busy(True, "Downloading...")
        self.start_runlog(f"Download: {url}  settings: {json.dumps(s)}")
        self.log_line(f"Starting: {url}")
        threading.Thread(target=self._download_worker, args=([url], s), daemon=True).start()

    # Saved playlists

    def show_playlist_count(self):
        n = len(saved_playlists())
        self.pl_count.config(text=f"{plural(n, 'playlist')} saved" if n else "No playlists saved yet")

    def on_edit_list(self):
        if not PLAYLISTS.exists():
            PLAYLISTS.write_text(PLAYLISTS_TEMPLATE, encoding="utf-8")
        os.startfile(PLAYLISTS)

    def _playlists_or_ask(self):
        urls = saved_playlists()
        if not urls and messagebox.askyesno(
                "My Playlists is empty",
                "You haven't saved any playlists yet.\n\nOpen My Playlists now to add some?"):
            self.on_edit_list()
        return urls

    def on_check_playlists(self):
        if self.busy:
            return
        urls = self._playlists_or_ask()
        if not urls:
            return
        s = dict(self.settings)
        RAW.mkdir(exist_ok=True)
        out_dir(s).mkdir(parents=True, exist_ok=True)
        self.stopping = False
        self.titles = {}
        self.songs = {}
        self.set_busy(True, f"Checking {plural(len(urls), 'playlist')}...")
        self.start_runlog(f"Update My Playlists: {plural(len(urls), 'playlist')}  settings: {json.dumps(s)}")
        self.write_info("Checking My Playlists...")
        self.log_line(f"Updating My Playlists ({plural(len(urls), 'playlist')})")
        threading.Thread(target=self._check_worker, args=(urls, s), daemon=True).start()

    def on_preview_playlists(self):
        if self.busy:
            return
        urls = self._playlists_or_ask()
        if not urls:
            return
        self.stopping = False
        self.titles = {}
        self.songs = {}
        self.set_busy(True, f"Checking {plural(len(urls), 'playlist')}...")
        self.write_info("Checking My Playlists...")
        threading.Thread(target=self._check_worker, args=(urls, dict(self.settings), True),
                         daemon=True).start()

    def _swap_work(self, plists):
        """Songs in the saved playlists that YouTube won't serve: the ones not
        searched for yet, and the other uploads found but not downloaded yet."""
        ids = list(dict.fromkeys(i for pl in plists for i in pl["ids"]))
        dead, have, rep = unavailable_ids(), archived_ids(), replacements()
        look = [{"id": i, **self.songs[i]} for i in ids
                if i in dead and i not in rep and self.songs.get(i, {}).get("title")]
        get = [rep[i] for i in ids
               if rep.get(i) and rep[i] not in have and rep[i] not in dead]
        return look, get

    def _write_playlists(self, s, plists):
        rep = replacements()
        plists = [{**pl, "ids": [rep.get(i) or i for i in pl["ids"]]} for pl in plists]
        data = PIPELINE / "playlists-run.json"
        data.write_text(json.dumps(plists), encoding="utf-8")
        self.q.put(("log", "Writing playlist files"))
        self._stream([PY, str(PIPELINE / "writeplaylists.py"), str(write_run_config(s)), str(data)],
                     show=lambda ln: f"  {ln.strip()}" if ln.strip() else None)

    def _check_worker(self, urls, s, preview_only=False):
        try:
            have, dead = archived_ids(), unavailable_ids()
            new_ids, lines, to_get, plists = set(), [], [], []
            for url in urls:
                if self.stopping:
                    break
                try:
                    data = self._lookup(url)
                except Exception as e:
                    lines.append(f"Couldn't read: {url}\n     {e}")
                    self.q.put(("log", f"Couldn't read {url}: {e}"))
                    self.q.put(("info", "\n".join(lines)))
                    continue
                entries = [e for e in (data.get("entries") or [data]) if e]
                self.titles.update({e["id"]: e.get("title") for e in entries if e.get("id")})
                self.songs.update({e["id"]: {"title": e.get("title"),
                                             "artist": e.get("channel") or e.get("uploader"),
                                             "duration": e.get("duration")}
                                   for e in entries if e.get("id")})
                plists.append({"title": data.get("title") or "Playlist",
                               "ids": [e.get("id") for e in entries if e.get("id")]})
                fresh = [e for e in entries if e.get("id") not in have | dead]
                lines.append(f"{data.get('title') or url}: {len(fresh)} new of {len(entries)}"
                             f"{dead_note(entries, dead)}")
                lines += [f"     + {e.get('title', '(unknown)')}" for e in fresh]
                if fresh:
                    to_get.append(url)
                    new_ids.update(e.get("id") for e in fresh)
                self.q.put(("info", "\n".join(lines)))

            if self.stopping:
                self.q.put(("stopped", None))
                return
            look, get = self._swap_work(plists)
            if look or get:
                n = len(look) + len(get)
                lines.append(f"\n{n} unavailable on YouTube, will look for another copy")
                self.q.put(("info", "\n".join(lines)))
            if preview_only:
                self.q.put(("previewed", len(new_ids) + len(look) + len(get)))
                return
            if not new_ids:
                if raw_songs() or look or get:
                    if raw_songs():
                        # Songs left over from a run that was stopped or closed early.
                        self.q.put(("log", "Nothing new, but tagging songs left from last time"))
                    self._download_worker([], s, plists=plists)
                    return
                if s["playlist_files"] and plists:
                    self._write_playlists(s, plists)
                self.q.put(("uptodate", None))
                return
            self.q.put(("log", f"{plural(len(new_ids), 'new song')} to download"))
            self._download_worker(to_get, s, label=plural(len(new_ids), "new song"), plists=plists)
        except Exception as e:
            self.q.put(("fatal", str(e)))

    def _stream(self, cmd, keep=None, show=None):
        """Run cmd. All output goes to last-run.log (and keep, if given).
        show(line) returns the text to put in the window, or None."""
        if self.stopping:
            return -1
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             env=child_env(), creationflags=NO_WINDOW, bufsize=1)
        self.proc = p
        self.cut_step = False
        tail = collections.deque(maxlen=5)
        for line in p.stdout:
            runlog(line)
            if line.strip():
                tail.append(line.strip())
            if keep is not None:
                keep.append(line)
            if show is not None:
                out = show(line.rstrip())
                if out:
                    self.q.put(("log", out))
            if self.cut_step:  # the filter decided to end this step early
                kill_tree(p.pid)
                break
        p.wait()
        self.proc = None
        # yt-dlp exits with 1 whenever any song failed (--ignore-errors), which
        # is normal and reported per song, so only the other steps count here.
        if p.returncode and not self.stopping and not self.cut_step and cmd[1] != str(YTDLP):
            name = f"beets {cmd[4]}" if cmd[1] == "-m" else Path(cmd[1]).name
            # The error line, not just the last line: buffered output can land
            # after the traceback.
            errs = [ln for ln in tail if self._only_problems(ln) and "traceback" not in ln.lower()]
            why = (errs or list(tail) or [f"exit code {p.returncode}"])[-1]
            self.failed.append(f"{name} ({why})")
            runlog(f"!! {name} failed with exit code {p.returncode}\n")
            self.q.put(("log", f"  Problem: {name} crashed: {why}"))
        return p.returncode

    def _yt_filter(self):
        """Reduce yt-dlp's output to one line per song plus the status bar.
        Ends the download early if YouTube refuses several songs in a row."""
        st = {"n": 0, "total": 0, "songs": 0, "refused": 0, "blocked": False, "title": ""}
        item_re = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
        url_re = re.compile(r"Extracting URL: .*(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})")
        thumb_re = re.compile(r"Writing video thumbnail .* to: (.+)")
        dest_re = re.compile(r"\[download\] Destination: (.+)")
        pct_re = re.compile(r"\[download\]\s+([\d.]+)%")
        sleep_re = re.compile(r"Sleeping ([\d.]+) seconds")
        err_re = re.compile(r"^ERROR: (?:\[\w+\] ([0-9A-Za-z_-]{11}): )?(.*)")

        def show(line):
            if m := item_re.search(line):
                st["n"], st["total"] = int(m.group(1)), int(m.group(2))
                return None
            if m := url_re.search(line):
                st["title"] = self.titles.get(m.group(1)) or m.group(1)
                return None
            if m := thumb_re.search(line):
                st["title"] = strip_id(Path(m.group(1)).stem)
                return None
            if m := dest_re.search(line):
                st["songs"] += 1
                st["refused"] = 0
                st["title"] = strip_id(Path(m.group(1)).stem)
                where = f"{st['n']}/{st['total']}  " if st["total"] else ""
                return f"  {where}{st['title']}"
            if m := sleep_re.search(line):
                self.q.put(("status", f"Waiting {round(float(m.group(1)))} sec before the next song"))
                return None
            if m := pct_re.search(line):
                pos = f" ({st['n']} of {st['total']})" if st["total"] else ""
                self.q.put(("status", f"Downloading{pos}: {st['title']} {m.group(1)}%"))
                return None
            if m := err_re.search(line):
                title = self.titles.get(m.group(1)) or st["title"] or m.group(1) or "a song"
                reason = m.group(2)
                if "403" in reason:
                    st["refused"] += 1
                    if st["refused"] >= 3:
                        st["blocked"] = True
                        self.cut_step = True
                    return f"  Failed: {title} (YouTube refused the download)"
                if "unavailable" in reason.lower() or "not available" in reason.lower():
                    return f"  Failed: {title} (not available on YouTube)"
                return f"  Failed: {title} ({reason})"
            return None

        return show, st

    def _swap_step(self, plists, yt_download, yt, n_dead):
        """Look for other uploads of songs YouTube won't serve and download
        them. Returns (songs downloaded this way, songs still unavailable)."""
        look, get = self._swap_work(plists)
        names = {x["id"]: x["title"] for x in look}
        n_none = n_err = 0
        if look:
            self.q.put(("log", f"Looking for another copy of {plural(len(look), 'unavailable song')}"))
            data = PIPELINE / "findcopy-run.json"
            data.write_text(json.dumps(look), encoding="utf-8")
            done = [0]

            def show(line):
                nonlocal n_none, n_err
                parts = line.split(" ", 3)
                if (parts[0] not in ("found", "none", "error") or len(parts) < 2
                        or parts[1] not in names):
                    return self._only_problems(line)
                done[0] += 1
                self.q.put(("status", f"Looking for another copy {done[0]} of {len(look)}"))
                old = parts[1]
                if parts[0] == "error":
                    # Not written to replaced.txt, so it's searched again next run.
                    n_err += 1
                    why = line.split(" ", 2)[2] if len(parts) > 2 else "unknown error"
                    return f"  Couldn't search for {names[old]} ({why}), will try again next time"
                with REPLACED.open("a", encoding="utf-8") as f:
                    if parts[0] == "found" and len(parts) == 4:
                        f.write(f"{old} {parts[2]}  # {parts[3]}\n")
                        get.append(parts[2])
                        self.titles[parts[2]] = parts[3]
                        return f"  Found another copy: {parts[3]}"
                    f.write(f"{old} -  # no other copy found: {names[old]}\n")
                n_none += 1
                return f"  No other copy found: {names[old]}"

            self._stream([PY, str(PIPELINE / "findcopy.py"), str(data)], show=show)
            n_none += n_err  # still unavailable this run, just not remembered
        elif n_dead:
            self.q.put(("log", f"  {n_dead} unavailable on YouTube, will be skipped next time"))
            n_none = n_dead
        if not get or self.stopping:
            return 0, n_none
        self.q.put(("log", f"Downloading {len(get)} other cop{'y' if len(get) == 1 else 'ies'}"))
        st, still_dead = yt_download([f"https://music.youtube.com/watch?v={v}" for v in get])
        yt["songs"] += st["songs"]
        yt["blocked"] = yt["blocked"] or st["blocked"]
        return st["songs"], n_none + still_dead

    def _counter(self, label, total):
        """Status bar count for steps 2 to 4, plus any problems in the window."""
        n = [0]

        def show(line):
            stripped = line.strip()
            if stripped.startswith(("art:", "album:", "genre:", str(RAW))):
                n[0] += 1
                self.q.put(("status", f"{label} {min(n[0], total)} of {total}"))
            return self._only_problems(line)
        return show

    @staticmethod
    def _only_problems(line):
        low = line.lower()
        # Needs the colon so a song called "Human Error" doesn't count. No \b in
        # front, so Python's "ValueError: ..." (the line that says what went
        # wrong after a traceback) is shown too.
        if ("skipped (" in low or "lookup failed" in low or "traceback" in low
                or "couldn't" in low or re.search(r"(error|exception)\b:", low)):
            return f"  Warning: {line.strip()}"
        return None

    def _download_worker(self, urls, s=None, label=None, plists=None):
        s = s or load_settings()
        self.failed = []
        try:
            run_cfg = str(write_run_config(s))
            if s["format"] == "mp3":
                # Re-encoded from YouTube's ~128k audio, so use a high VBR setting.
                fmt_args = ["-f", "bestaudio", "-x", "--audio-format", "mp3",
                            "--audio-quality", "2"]
            else:
                # YouTube's AAC stream is kept as is. If there isn't one, the best
                # audio gets converted to AAC 160k.
                fmt_args = ["-f", "bestaudio[ext=m4a]/bestaudio", "-x",
                            "--audio-format", "m4a", "--audio-quality", "160K"]

            pause = int(s.get("pause") or 0)
            pause_args = (["--sleep-requests", "0.75", "--sleep-interval", str(pause),
                           "--max-sleep-interval", str(pause * 2)] if pause else [])

            def yt_download(targets):
                ytlog = []
                show, st = self._yt_filter()
                self._stream([
                    PY, str(YTDLP),
                    "--ffmpeg-location", str(BIN),
                    "--remote-components", "ejs:github",
                    *fmt_args, *pause_args,
                    "--embed-thumbnail", "--embed-metadata", "--convert-thumbnails", "jpg",
                    # YouTube's category ("Music") would end up as the genre.
                    "--parse-metadata", ":(?P<meta_genre>)",
                    "--no-overwrites", "--ignore-errors", "--newline",
                    "--download-archive", str(ARCHIVE),
                    "-o", str(RAW / "%(title)s [%(id)s].%(ext)s"),
                    # "--" so a link can never be read as an option (--exec etc.)
                    "--", *targets,
                ], keep=ytlog, show=show)
                return st, record_unavailable(ytlog)

            yt = {"songs": 0, "blocked": False}
            n_dead = n_swapped = 0
            if urls:
                self.q.put(("log", "Step 1 of 4: downloading"))
                yt, n_dead = yt_download(urls)
                if yt["blocked"]:
                    self.q.put(("log", "  YouTube is refusing downloads right now, so downloading "
                                       "stopped. The songs that did download will still be tagged."))

            if plists and not yt["blocked"] and not self.stopping:
                n_swapped, n_dead = self._swap_step(plists, yt_download, yt, n_dead)
            elif n_dead:
                self.q.put(("log", f"  {n_dead} unavailable on YouTube, will be skipped next time"))
            clean_raw()
            if self.stopping:
                self.q.put(("stopped", None))
                return

            n_files = len(raw_songs())
            self.q.put(("log", "Step 2 of 4: fixing cover art and titles"))
            if s["square_art"]:
                size = "300" if s["small_art"] else "500"
                self._stream([PY, str(PIPELINE / "squareart.py"), str(RAW), size],
                             show=self._counter("Fixing cover art", n_files))
            self._stream([PY, str(PIPELINE / "cleantitles.py"), str(RAW)],
                         show=self._only_problems)
            if self.stopping:
                self.q.put(("stopped", None))
                return

            self.q.put(("log", f"Step 3 of 4: tagging {plural(n_files, 'song')}"))
            self.q.put(("status", f"Tagging 0 of {n_files}"))
            # Forget songs deleted from the output folder, otherwise beets skips
            # them as "already in the library".
            self._stream([PY, str(PIPELINE / "prunelib.py"), run_cfg], show=self._only_problems)
            beets_log = []
            beets_ok = self._stream([PY, "-m", "beets", "-c", run_cfg, "import", "-q", "-s",
                                     str(RAW)],
                                    keep=beets_log, show=self._counter("Tagging", n_files)) == 0
            if self.stopping:
                self.q.put(("stopped", None))
                return
            n_asis = sum(1 for ln in beets_log if "Importing as-is" in ln)
            # Songs that really are already in the library get skipped by beets
            # and left in newdownloads. Report them and delete the extra copy.
            dupes, current = [], None
            for ln in beets_log:
                if ln.strip().startswith(str(RAW)):
                    current = Path(ln.strip())
                elif "already in the library" in ln and current:
                    dupes.append(current)
            for p in dupes:
                self.q.put(("log", f"  Skipped, already in your library: {strip_id(p.stem)}"))
                try:
                    p.unlink()
                except OSError:
                    pass

            self.q.put(("log", "Step 4 of 4: artists, albums, years and genres"))
            # Songs that kept YouTube's info list songwriters as artists too.
            self._stream([PY, str(PIPELINE / "fixartists.py"), run_cfg], show=self._only_problems)
            # If beets crashed, the songs it didn't get to weren't matched at all.
            n_matched = n_files - n_asis - len(dupes) if beets_ok else 0
            self.q.put(("status", f"Albums and years 0 of {max(n_matched, 0)}"))
            album_log = []
            self._stream([PY, str(PIPELINE / "fillalbums.py"), run_cfg],
                         keep=album_log, show=self._counter("Albums and years", max(n_matched, 0)))
            n_albums = sum(1 for ln in album_log if 'album "' in ln)
            n_new = max(n_files - len(dupes), 0)
            self.q.put(("status", f"Genres 0 of {n_new}"))
            self._stream([PY, str(PIPELINE / "fillgenres.py"), run_cfg],
                         show=self._counter("Genres", n_new))
            if self.stopping:
                self.q.put(("stopped", None))
                return

            if s["replaygain"]:
                # beets stores the volume values for songs it couldn't match but
                # doesn't always write them into the file. This writes anything
                # that's in the database but missing from the files.
                self._stream([PY, "-m", "beets", "-c", run_cfg, "write"], show=self._only_problems)

            if plists and s["playlist_files"]:
                self._write_playlists(s, plists)
            clean_raw()

            parts = [f"{yt['songs']} downloaded"]
            if yt["blocked"]:
                parts.append("stopped early because YouTube is blocking downloads")
            if n_swapped:
                parts.append(f"{n_swapped} of those from another copy")
            if n_dead:
                parts.append(f"{n_dead} unavailable")
            if dupes:
                parts.append(f"{len(dupes)} already in library")
            if n_matched > 0:
                parts.append(f"{n_matched} matched on MusicBrainz")
            if n_asis:
                parts.append(f"{n_asis} kept YouTube's info")
            if n_albums:
                parts.append(f"{plural(n_albums, 'album')} filled in")
            self.q.put(("log", "Summary: " + ", ".join(parts)))
            self.q.put(("done", (label, yt["blocked"], list(self.failed))))
        except Exception as e:
            if self.stopping:
                self.q.put(("stopped", None))
            else:
                self.q.put(("fatal", str(e)))

    def drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log_line(payload)
                elif kind == "status":
                    self.status.config(text=payload)
                elif kind == "preview":
                    self._render_preview(payload)
                    self.set_busy(False)
                elif kind == "preview_err":
                    self.write_info(f"Couldn't read that link:\n\n{payload}")
                    self.set_busy(False, "Preview failed.")
                elif kind == "info":
                    self.write_info(payload)
                elif kind == "uptodate":
                    self.log_line("My Playlists are up to date.")
                    self.set_busy(False, "Nothing new to download.")
                elif kind == "previewed":
                    self.set_busy(False, f"{plural(payload, 'new song')} in My Playlists. Click "
                                         "Update My Playlists to download them."
                                  if payload else "My Playlists are up to date.")
                elif kind == "done":
                    label, blocked, failed = payload
                    got = f" ({label})" if label else ""
                    head = "Finished with problems" if failed else "Finished"
                    out = out_dir(self.settings)
                    self.log_line(f"{head}{got}. Songs are in {out}\n"
                                  f"Full details are in pipeline\\{RUNLOG.name}")
                    self.set_busy(False, f"{head}{got}.")
                    note = ("\n\nYouTube started blocking downloads partway through. "
                            "Wait an hour or so, then run it again to get the rest. "
                            "A longer pause between songs in Settings helps avoid this."
                            if blocked else "")
                    if failed:
                        steps = "\n".join(f"- {f}" for f in failed)
                        messagebox.showwarning(
                            head, f"{head}{got}.\n\nThese steps crashed, so some songs may not "
                                  f"be fully tagged:\n{steps}\n\nThe details are in "
                                  f"pipeline\\{RUNLOG.name}. Songs are in:\n{out}{note}")
                    else:
                        messagebox.showinfo(head, f"{head}{got}.\n\nSongs are in:\n{out}{note}")
                elif kind == "stopped":
                    self.log_line("Stopped. Songs that already finished downloading will be "
                                  "tagged next time you run it.")
                    self.set_busy(False, "Stopped.")
                elif kind == "fatal":
                    self.set_busy(False, "Error.")
                    messagebox.showerror("Error", payload)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_queue)


if __name__ == "__main__":
    if os.name == "nt":
        # Own taskbar entry and icon instead of being grouped under Python.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("phoenixmp3.app")
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
