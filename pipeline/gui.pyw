#!/usr/bin/env pythonw
# phoenixmp3 - GUI for downloading YouTube links and tagging them with beets.
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
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
PLAYLISTS = MUSIC / "playlists.txt"
SETTINGS_FILE = PIPELINE / "settings.json"
RUN_CONFIG = PIPELINE / "run-config.yaml"    # beets config overlay, rebuilt each run

PLAYLISTS_TEMPLATE = """\
# phoenixmp3 - saved playlists
# Put one YouTube / YouTube Music playlist link per line, then save this file.
# "Check playlists" in the app downloads only the songs you don't have yet.
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

SHORTCUT = MUSIC / f"{APP_NAME}.lnk"
SHORTCUT_STAMP = PIPELINE / "shortcut.txt"

# yt-dlp errors that won't go away by retrying.
_ERR_RE = re.compile(r"ERROR: \[youtube\] ([0-9A-Za-z_-]{11}): (.+)")
_PERMANENT = ("video unavailable", "not available", "private video", "removed",
              "copyright", "terminated", "confirm your age", "inappropriate",
              "members-only", "join this channel")


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
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


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
        if m and m.group(1) not in known and any(p in m.group(2).lower() for p in _PERMANENT):
            found[m.group(1)] = m.group(2).strip()
    if found:
        with UNAVAILABLE.open("a", encoding="utf-8") as f:
            for vid, why in found.items():
                f.write(f"{vid}  # {why}\n")
    return len(found)


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
    return env


def ensure_shortcut():
    """Create the .lnk in the top folder. Shortcuts hold absolute paths so one
    can't be shipped in the zip; this also fixes it if the folder moves."""
    exe = Path(sys.executable)
    if os.name != "nt" or exe.name.lower() != "pythonw.exe":
        return
    want = f"{SHORTCUT}|{exe}|{PIPELINE / 'gui.pyw'}"
    try:
        if SHORTCUT.exists() and SHORTCUT_STAMP.read_text(encoding="utf-8") == want:
            return
    except OSError:
        pass
    env = dict(os.environ, LNK=str(SHORTCUT), TGT=str(exe),
               ARGS=f'"{PIPELINE / "gui.pyw"}"', WD=str(MUSIC),
               ICO=f"{ICON},0", DESC=APP_NAME)
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:LNK);"
          "$s.TargetPath=$env:TGT;$s.Arguments=$env:ARGS;$s.WorkingDirectory=$env:WD;"
          "$s.IconLocation=$env:ICO;$s.Description=$env:DESC;$s.Save()")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           env=env, creationflags=NO_WINDOW, timeout=30, capture_output=True)
        if r.returncode == 0 and SHORTCUT.exists():
            SHORTCUT_STAMP.write_text(want, encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        pass


def plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else "s")


def strip_id(name):
    return re.sub(r"\s*\[[0-9A-Za-z_-]{11}\]$", "", name)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.settings = load_settings()
        root.title(APP_NAME)
        try:
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
        root.geometry("680x560")
        root.minsize(560, 460)
        pad = dict(padx=12, pady=6)

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Paste link here (video link, album link, playlist link):").pack(anchor="w")
        row = ttk.Frame(top)
        row.pack(fill="x", pady=(2, 0))
        self.url = ttk.Entry(row)
        self.url.pack(side="left", fill="x", expand=True)
        self.url.focus()
        self.preview_btn = ttk.Button(row, text="Preview", command=self.on_preview)
        self.preview_btn.pack(side="left", padx=(6, 0))

        ttk.Label(root, text="What this link points to:").pack(anchor="w", padx=12)
        self.info = tk.Text(root, height=10, wrap="none", state="disabled")
        self.info.pack(fill="both", expand=True, padx=12)

        buttons = ttk.Frame(root)
        buttons.pack(**pad)
        self.dl_btn = ttk.Button(buttons, text="Download", command=self.on_download)
        self.dl_btn.pack(side="left")
        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=12)
        self.check_btn = ttk.Button(buttons, text="Check playlists", command=self.on_check_playlists)
        self.check_btn.pack(side="left")
        ttk.Button(buttons, text="List of checked playlists", command=self.on_edit_list).pack(
            side="left", padx=(6, 0))
        ttk.Separator(buttons, orient="vertical").pack(side="left", fill="y", padx=12)
        self.settings_btn = ttk.Button(buttons, text="Settings", command=self.open_settings)
        self.settings_btn.pack(side="left")
        self.summary = ttk.Label(root, text=settings_summary(self.settings), foreground="#666")
        self.summary.pack(anchor="w", padx=12)

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

        section("Extras")
        ttk.Checkbutton(body, text="Write playlist files (.m3u8) for saved playlists "
                        "when you click Check playlists", variable=m3u).pack(anchor="w")
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
                "playlist_files": m3u.get(), "replaygain": rg.get(),
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
        for b in (self.preview_btn, self.dl_btn, self.check_btn, self.settings_btn):
            b.config(state=state)
        if status:
            self.status.config(text=status)

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
             "--remote-components", "ejs:github", url],
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
            have, dead = archived_ids(), unavailable_ids()
            new = sum(1 for e in entries if e.get("id") not in have | dead)
            lines = [f"PLAYLIST: {data.get('title', '(untitled)')}",
                     f"{plural(len(entries), 'track')}, {new} new", ""]
            for i, e in enumerate(entries, 1):
                vid = e.get("id")
                note = "  (have it)" if vid in have else "  (unavailable)" if vid in dead else ""
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
        self.set_busy(True, "Downloading...")
        self.start_runlog(f"Download: {url}  settings: {json.dumps(s)}")
        self.log_line(f"Starting: {url}")
        threading.Thread(target=self._download_worker, args=([url], s), daemon=True).start()

    # Saved playlists

    def on_edit_list(self):
        if not PLAYLISTS.exists():
            PLAYLISTS.write_text(PLAYLISTS_TEMPLATE, encoding="utf-8")
        os.startfile(PLAYLISTS)

    def on_check_playlists(self):
        if self.busy:
            return
        urls = saved_playlists()
        if not urls:
            if messagebox.askyesno("No saved playlists",
                                   "playlists.txt has no links in it yet.\n\nOpen it now to add some?"):
                self.on_edit_list()
            return
        s = dict(self.settings)
        RAW.mkdir(exist_ok=True)
        out_dir(s).mkdir(parents=True, exist_ok=True)
        self.set_busy(True, f"Checking {plural(len(urls), 'playlist')}...")
        self.start_runlog(f"Check playlists: {plural(len(urls), 'playlist')}  settings: {json.dumps(s)}")
        self.write_info("Checking saved playlists...")
        self.log_line(f"Checking {plural(len(urls), 'saved playlist')}")
        threading.Thread(target=self._check_worker, args=(urls, s), daemon=True).start()

    def _write_playlists(self, s, plists):
        data = PIPELINE / "playlists-run.json"
        data.write_text(json.dumps(plists), encoding="utf-8")
        self.q.put(("log", "Writing playlist files"))
        self._stream([PY, str(PIPELINE / "writeplaylists.py"), str(write_run_config(s)), str(data)],
                     show=lambda ln: f"  {ln.strip()}" if ln.strip() else None)

    def _check_worker(self, urls, s):
        try:
            have, dead = archived_ids(), unavailable_ids()
            new_ids, lines, to_get, plists = set(), [], [], []
            for url in urls:
                try:
                    data = self._lookup(url)
                except Exception as e:
                    lines.append(f"Couldn't read: {url}\n     {e}")
                    self.q.put(("log", f"Couldn't read {url}: {e}"))
                    self.q.put(("info", "\n".join(lines)))
                    continue
                entries = [e for e in (data.get("entries") or [data]) if e]
                plists.append({"title": data.get("title") or "Playlist",
                               "ids": [e.get("id") for e in entries if e.get("id")]})
                fresh = [e for e in entries if e.get("id") not in have | dead]
                n_dead = sum(1 for e in entries if e.get("id") in dead)
                skipped = f"  ({n_dead} unavailable)" if n_dead else ""
                lines.append(f"{data.get('title') or url}: {len(fresh)} new of {len(entries)}{skipped}")
                lines += [f"     + {e.get('title', '(unknown)')}" for e in fresh]
                if fresh:
                    to_get.append(url)
                    new_ids.update(e.get("id") for e in fresh)
                self.q.put(("info", "\n".join(lines)))

            if not new_ids:
                if s["playlist_files"] and plists:
                    self._write_playlists(s, plists)
                self.q.put(("uptodate", None))
                return
            self.q.put(("log", f"{plural(len(new_ids), 'new song')} to download"))
            self._download_worker(to_get, s, label=plural(len(new_ids), "new song"),
                                  plists=plists if s["playlist_files"] else None)
        except Exception as e:
            self.q.put(("fatal", str(e)))

    def _stream(self, cmd, keep=None, show=None):
        """Run cmd. All output goes to last-run.log (and keep, if given).
        show(line) returns the text to put in the window, or None."""
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             env=child_env(), creationflags=NO_WINDOW, bufsize=1)
        for line in p.stdout:
            runlog(line)
            if keep is not None:
                keep.append(line)
            if show is not None:
                out = show(line.rstrip())
                if out:
                    self.q.put(("log", out))
        p.wait()
        return p.returncode

    def _yt_filter(self):
        """Reduce yt-dlp's output to one line per song plus the status bar."""
        st = {"n": 0, "total": 0, "songs": 0}
        item_re = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
        dest_re = re.compile(r"\[download\] Destination: (.+)")
        pct_re = re.compile(r"\[download\]\s+([\d.]+)%")

        def show(line):
            if m := item_re.search(line):
                st["n"], st["total"] = int(m.group(1)), int(m.group(2))
                return None
            if m := dest_re.search(line):
                st["songs"] += 1
                st["title"] = strip_id(Path(m.group(1)).stem)
                where = f"{st['n']}/{st['total']}  " if st["total"] else ""
                return f"  {where}{st['title']}"
            if m := pct_re.search(line):
                pos = f" ({st['n']} of {st['total']})" if st["total"] else ""
                self.q.put(("status", f"Downloading{pos}: {st.get('title', '')} {m.group(1)}%"))
                return None
            if line.startswith("ERROR:"):
                return "  Failed: " + re.sub(r"^ERROR: (\[[^\]]+\] )?", "", line)
            return None

        return show, st

    @staticmethod
    def _only_problems(line):
        low = line.lower()
        # Needs the colon so a song called "Human Error" doesn't count.
        if ("skipped (" in low or "lookup failed" in low or "traceback" in low
                or re.search(r"\b(error|exception)\b:", low)):
            return f"  Warning: {line.strip()}"
        return None

    def _download_worker(self, urls, s=None, label=None, plists=None):
        s = s or load_settings()
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

            self.q.put(("log", "Step 1 of 4: downloading"))
            ytlog = []
            yt_show, yt = self._yt_filter()
            self._stream([
                PY, str(YTDLP),
                "--ffmpeg-location", str(BIN),
                "--remote-components", "ejs:github",
                *fmt_args,
                "--embed-thumbnail", "--embed-metadata", "--convert-thumbnails", "jpg",
                "--no-overwrites", "--ignore-errors", "--newline",
                "--download-archive", str(ARCHIVE),
                "-o", str(RAW / "%(title)s [%(id)s].%(ext)s"),
                *urls,
            ], keep=ytlog, show=yt_show)
            n_dead = record_unavailable(ytlog)
            if n_dead:
                self.q.put(("log", f"  {n_dead} unavailable on YouTube, will be skipped next time"))

            self.q.put(("log", "Step 2 of 4: fixing cover art and titles"))
            self.q.put(("status", "Fixing cover art and titles..."))
            if s["square_art"]:
                size = "300" if s["small_art"] else "500"
                self._stream([PY, str(PIPELINE / "squareart.py"), str(RAW), size],
                             show=self._only_problems)
            self._stream([PY, str(PIPELINE / "cleantitles.py"), str(RAW)],
                         show=self._only_problems)

            n_files = sum(1 for p in RAW.iterdir() if p.suffix.lower() in (".m4a", ".mp3"))
            self.q.put(("log", f"Step 3 of 4: tagging {plural(n_files, 'song')}"))
            self.q.put(("status", f"Tagging {plural(n_files, 'song')}..."))
            # Forget songs deleted from the output folder, otherwise beets skips
            # them as "already in the library".
            self._stream([PY, str(PIPELINE / "prunelib.py"), run_cfg], show=self._only_problems)
            beets_log = []
            self._stream([PY, "-m", "beets", "-c", run_cfg, "import", "-q", "-s", str(RAW)],
                         keep=beets_log, show=self._only_problems)
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

            self.q.put(("log", "Step 4 of 4: albums and release years"))
            self.q.put(("status", "Looking up albums and years..."))
            album_log = []
            self._stream([PY, str(PIPELINE / "fillalbums.py"), run_cfg],
                         keep=album_log, show=self._only_problems)
            n_albums = sum(1 for ln in album_log if 'album "' in ln)

            if plists:
                self._write_playlists(s, plists)

            parts = [f"{yt['songs']} downloaded"]
            if n_dead:
                parts.append(f"{n_dead} unavailable")
            if dupes:
                parts.append(f"{len(dupes)} already in library")
            n_matched = n_files - n_asis - len(dupes)
            if n_matched > 0:
                parts.append(f"{n_matched} matched on MusicBrainz")
            if n_asis:
                parts.append(f"{n_asis} kept YouTube's info")
            if n_albums:
                parts.append(f"{plural(n_albums, 'album')} filled in")
            self.q.put(("log", "Summary: " + ", ".join(parts)))
            self.q.put(("done", label))
        except Exception as e:
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
                    self.log_line("All saved playlists are up to date.")
                    self.set_busy(False, "Nothing new to download.")
                elif kind == "done":
                    got = f" ({payload})" if payload else ""
                    out = out_dir(self.settings)
                    self.log_line(f"Finished{got}. Songs are in {out}\n"
                                  f"Full details are in pipeline\\{RUNLOG.name}")
                    self.set_busy(False, f"Finished{got}.")
                    messagebox.showinfo("Finished", f"Finished{got}.\n\nSongs are in:\n{out}")
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
    threading.Thread(target=ensure_shortcut, daemon=True).start()
    root.mainloop()
