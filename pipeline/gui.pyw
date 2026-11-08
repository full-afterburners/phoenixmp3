#!/usr/bin/env pythonw
"""
phoenixmp3 - tiny GUI front-end for the yt-dlp -> beets music pipeline.

Paste a YouTube video / album / playlist link, hit Preview to see what it
points to, then Download. Tagged AAC .m4a files (cover art) land in
downloadswithmeta\\ ready for the iPod Nano.

Launched with no console window via pythonw (see the double-click shortcut).
Shares config.yaml with get-music.ps1, so tagging behaves identically.
"""
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

# --- Paths (this file lives in Music\pipeline) ------------------------------
# Everything the pipeline needs lives under pipeline\; only the finished
# library (downloadswithmeta) sits alongside it at the top level.
PIPELINE = Path(__file__).resolve().parent
MUSIC    = PIPELINE.parent
BIN      = PIPELINE / "bin"
LIBS     = PIPELINE / "libs"
RAW      = PIPELINE / "newdownloads"
FINAL    = MUSIC / "downloadswithmeta"
YTDLP    = PIPELINE / "yt-dlp-master" / "yt_dlp" / "__main__.py"
ICON     = PIPELINE / "icon.ico"
# Full, unfiltered output of the last run (the window only shows a summary).
RUNLOG   = PIPELINE / "last-run.log"
# yt-dlp's record of every video already downloaded ("youtube <id>" per line).
# Songs listed here are skipped, so re-running a playlist only grabs new ones.
# Delete a line to allow that song to be downloaded again.
ARCHIVE  = PIPELINE / "downloaded.txt"
# Your saved playlists, one link per line; "Check playlists" grabs new songs.
# The app only ever reads this file (it writes the template only if it's missing).
PLAYLISTS = MUSIC / "playlists.txt"
PLAYLISTS_TEMPLATE = """\
# phoenixmp3 - saved playlists
# Put one YouTube / YouTube Music playlist link per line, then save this file.
# "Check playlists" in the app downloads only the songs you don't have yet.
# Lines starting with # are ignored, so you can label your playlists:
#
# Gym
# https://music.youtube.com/playlist?list=PLxxxxxxxx
"""


# --- Settings (persisted between uses) --------------------------------------
SETTINGS_FILE = PIPELINE / "settings.json"
# Per-run beets config overlay generated from the settings (passed with -c).
RUN_CONFIG = PIPELINE / "run-config.yaml"
DEFAULTS = {
    "format": "aac",            # "aac" (.m4a, no re-encode) | "mp3" (plays everywhere)
    "layout": "flat",           # "flat" (Artist - Title) | "folders" (Artist\Album\Title)
    "output_dir": "",           # "" = default (downloadswithmeta); see out_dir()
    "square_art": True,         # crop 16:9 thumbnails to a square
    "small_art": False,         # 300 px art instead of 500 px (picky players)
    "playlist_files": False,    # write .m3u8 per saved playlist on Check playlists
    "replaygain": False,        # ReplayGain volume-levelling tags
}
LAYOUTS = {
    "flat": "$artist - $title",
    "folders": "$artist/%if{$album,$album,Singles}/$title",
}


def out_dir(s):
    """The output folder as a full path. Stored as "" (default) or relative to
    the app folder when it's inside it, so renaming/moving the app folder (or a
    friend unzipping it anywhere) never points songs at a stale location."""
    p = Path(s.get("output_dir") or FINAL)
    return p if p.is_absolute() else MUSIC / p


def store_dir(path):
    """Inverse of out_dir(): what to save in settings.json for a chosen folder."""
    p = Path(path)
    if p == FINAL:
        return ""
    try:
        return str(p.relative_to(MUSIC))
    except ValueError:
        return str(p)  # outside the app folder (e.g. another drive): keep as-is


def load_settings():
    s = dict(DEFAULTS)
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        s.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    # Older settings stored a full path. If it's this app's default folder, or a
    # "downloadswithmeta" in a folder that no longer exists (app folder was
    # renamed/moved), fall back to the default.
    p = Path(s["output_dir"]) if s["output_dir"] else None
    if p and p.is_absolute() and (p == FINAL or (p.name == FINAL.name and not p.parent.exists())):
        s["output_dir"] = ""
    elif p and p.is_absolute():
        s["output_dir"] = store_dir(p)
    return s


def save_settings(s):
    SETTINGS_FILE.write_text(json.dumps(s, indent=2), encoding="utf-8")


def write_run_config(s):
    """beets config overlay for this run: output folder, layout, plugins."""
    plugins = "musicbrainz chroma ftintitle" + (" replaygain" if s["replaygain"] else "")
    lines = [
        "# Generated from settings.json for each run - edit settings in the app.",
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
    return f"Saving as {fmt} · {lay} · {out_dir(s)}"


def saved_playlists():
    """Links from playlists.txt (blank lines and # comments skipped)."""
    try:
        lines = PLAYLISTS.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []
    return [ln.strip().strip('"') for ln in lines
            if ln.strip() and not ln.strip().startswith("#")]


def archived_ids():
    """Set of video IDs already in the download archive."""
    try:
        lines = ARCHIVE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {ln.split()[-1] for ln in lines if ln.strip()}


# Songs YouTube refuses to serve (region-blocked, removed, private...). They still
# show in playlists, so without this they'd look "new" on every check forever.
# Format: "<id>  # <reason>". Delete the file to retry them all.
UNAVAILABLE = PIPELINE / "unavailable.txt"
# yt-dlp error lines look like: "ERROR: [youtube] <11-char id>: <reason>"
_ERR_RE = re.compile(r"ERROR: \[youtube\] ([0-9A-Za-z_-]{11}): (.+)")
# Only reasons that won't fix themselves; network hiccups / bot checks are retried.
_PERMANENT = ("video unavailable", "not available", "private video", "removed",
              "copyright", "terminated", "confirm your age", "inappropriate",
              "members-only", "join this channel")


def unavailable_ids():
    """Set of video IDs recorded as permanently unavailable."""
    try:
        lines = UNAVAILABLE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return set()
    return {ln.split()[0] for ln in lines if ln.strip() and not ln.startswith("#")}


def record_unavailable(log_lines):
    """Scan yt-dlp output for permanent per-video failures; append new ones."""
    known = unavailable_ids() | archived_ids()
    found = {}
    for ln in log_lines:
        m = _ERR_RE.search(ln)
        if m and m.group(1) not in known and any(
                p in m.group(2).lower() for p in _PERMANENT):
            found[m.group(1)] = m.group(2).strip()
    if found:
        with UNAVAILABLE.open("a", encoding="utf-8") as f:
            for vid, why in found.items():
                f.write(f"{vid}  # {why}\n")
    return len(found)

# Hide child-process console windows (pythonw has no console of its own).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

# Run helper steps with console python.exe, NOT the pythonw.exe running this
# window. A pythonw child has no console, so every console tool IT starts
# (ffmpeg, ffprobe, deno, fpcalc) would pop up its own flashing window.
# python.exe + CREATE_NO_WINDOW gets one hidden console that they all share.
_console_py = Path(sys.executable).with_name("python.exe")
PY = str(_console_py) if _console_py.exists() else sys.executable


def runlog(line):
    """Append one raw output line to last-run.log (troubleshooting copy)."""
    try:
        with RUNLOG.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line if line.endswith("\n") else line + "\n")
    except OSError:
        pass


def child_env():
    env = os.environ.copy()
    env["PYTHONPATH"] = str(LIBS)
    env["BEETSDIR"]   = str(PIPELINE)
    env["FPCALC"]     = str(BIN / "fpcalc.exe")
    env["PATH"]       = str(BIN) + os.pathsep + env.get("PATH", "")
    env["PIP_USER"]   = "0"
    # Ignore any Python packages installed elsewhere on this PC; everything we
    # need is in libs\ (keeps friends' PCs from interfering).
    env["PYTHONNOUSERSITE"] = "1"
    return env


# --- Double-click shortcut ---------------------------------------------------
# A Windows shortcut stores absolute paths, so one can't be shipped in the zip.
# Instead the app (re)creates "<APP_NAME>.lnk" in the top folder on startup,
# whenever it's missing or the folder has moved. START HERE.bat is only needed
# the very first time.
SHORTCUT = MUSIC / f"{APP_NAME}.lnk"
SHORTCUT_STAMP = PIPELINE / "shortcut.txt"


def ensure_shortcut():
    exe = Path(sys.executable)
    if os.name != "nt" or exe.name.lower() != "pythonw.exe":
        return  # only when launched normally (not from a console/test)
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
                           env=env, creationflags=NO_WINDOW, timeout=30,
                           capture_output=True)
        if r.returncode == 0 and SHORTCUT.exists():
            SHORTCUT_STAMP.write_text(want, encoding="utf-8")
    except (OSError, subprocess.SubprocessError):
        pass  # cosmetic; START HERE.bat still works


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.settings = load_settings()
        root.title(APP_NAME)
        try:  # window + taskbar icon (default= also applies to popups)
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
        root.geometry("680x560")
        root.minsize(560, 460)

        pad = dict(padx=12, pady=6)

        # URL row
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

        # Info / preview area
        ttk.Label(root, text="What this link points to:").pack(anchor="w", padx=12)
        self.info = tk.Text(root, height=10, wrap="none", state="disabled")
        self.info.pack(fill="both", expand=True, padx=12)

        # Download button + options
        dl_row = ttk.Frame(root)
        dl_row.pack(**pad)
        self.dl_btn = ttk.Button(dl_row, text="⬇  Download", command=self.on_download)
        self.dl_btn.pack(side="left")
        # Saved playlists (playlists.txt in the app's top folder)
        ttk.Separator(dl_row, orient="vertical").pack(side="left", fill="y", padx=12)
        self.check_btn = ttk.Button(dl_row, text="🔄  Check playlists",
                                    command=self.on_check_playlists)
        self.check_btn.pack(side="left")
        ttk.Button(dl_row, text="List of checked playlists", command=self.on_edit_list).pack(
            side="left", padx=(6, 0))
        ttk.Separator(dl_row, orient="vertical").pack(side="left", fill="y", padx=12)
        self.settings_btn = ttk.Button(dl_row, text="⚙  Settings",
                                       command=self.open_settings)
        self.settings_btn.pack(side="left")
        # One-line reminder of the current settings.
        self.summary = ttk.Label(root, text=settings_summary(self.settings),
                                 foreground="#666")
        self.summary.pack(anchor="w", padx=12)

        # Log area
        ttk.Label(root, text="Progress:").pack(anchor="w", padx=12)
        self.log = tk.Text(root, height=8, wrap="word", state="disabled",
                           bg="#111", fg="#ddd")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.status = ttk.Label(root, text="Ready.", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

        self.root.after(100, self.drain_queue)

    # --- Settings window ----------------------------------------------------
    def open_settings(self):
        if self.busy:
            return
        s = self.settings
        win = tk.Toplevel(self.root)
        win.title("Settings")
        win.transient(self.root)
        win.resizable(False, False)
        win.grab_set()  # modal
        body = ttk.Frame(win, padding=14)
        body.pack(fill="both", expand=True)
        hint = dict(foreground="#666")

        fmt = tk.StringVar(value=s["format"])
        layout = tk.StringVar(value=s["layout"])
        out = tk.StringVar(value=str(out_dir(s)))  # shown as a full path
        square = tk.BooleanVar(value=s["square_art"])
        small = tk.BooleanVar(value=s["small_art"])
        m3u = tk.BooleanVar(value=s["playlist_files"])
        rg = tk.BooleanVar(value=s["replaygain"])

        def section(text):
            ttk.Label(body, text=text, font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 2))

        section("Audio format")
        ttk.Radiobutton(body, text="AAC (.m4a): best quality, YouTube's audio kept as-is. "
                        "iPods, phones, most players.", variable=fmt, value="aac").pack(anchor="w")
        ttk.Radiobutton(body, text="MP3 (~190 kbps): plays on everything: car stereos, "
                        "cheap MP3 players.", variable=fmt, value="mp3").pack(anchor="w")

        section("Folder layout")
        ttk.Radiobutton(body, text='All songs in one folder ("Artist - Title")',
                        variable=layout, value="flat").pack(anchor="w")
        ttk.Radiobutton(body, text="Artist \\ Album folders (for players and car stereos "
                        "that browse by folder)", variable=layout, value="folders").pack(anchor="w")

        section("Save songs to")
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Entry(row, textvariable=out, width=58, state="readonly").pack(side="left")

        def browse():
            d = filedialog.askdirectory(parent=win, initialdir=out.get(),
                                        title="Choose where to save songs")
            if d:
                out.set(str(Path(d)))
        ttk.Button(row, text="Browse…", command=browse).pack(side="left", padx=(6, 0))
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
        ttk.Label(body, text="Evens out loudness between songs (audio is not changed, only tags). "
                  "Also writes iTunes Sound Check data;\niPods use it only if your sync tool "
                  "copies it to the iPod. Makes tagging a little slower.", **hint).pack(anchor="w")

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

    # --- UI helpers ---------------------------------------------------------
    def set_busy(self, busy, status=None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.preview_btn.config(state=state)
        self.dl_btn.config(state=state)
        self.check_btn.config(state=state)
        self.settings_btn.config(state=state)  # no changing settings mid-run
        if status:
            self.status.config(text=status)

    def write_info(self, text):
        self.info.config(state="normal")
        self.info.delete("1.0", "end")
        self.info.insert("end", text)
        self.info.config(state="disabled")

    @staticmethod
    def start_runlog(what):
        """Start a fresh last-run.log for this run."""
        import datetime
        try:
            RUNLOG.write_text(f"=== {datetime.datetime.now():%Y-%m-%d %H:%M} · {what} ===\n",
                              encoding="utf-8")
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

    # --- Preview ------------------------------------------------------------
    def on_preview(self):
        url = self.get_url()
        if not url or self.busy:
            return
        self.set_busy(True, "Looking up link…")
        self.write_info("Loading…")
        threading.Thread(target=self._preview_worker, args=(url,), daemon=True).start()

    @staticmethod
    def _lookup(url):
        """Ask yt-dlp what a link points to (track list only, no download)."""
        out = subprocess.run(
            [PY, str(YTDLP), "--flat-playlist",
             "--dump-single-json", "--remote-components", "ejs:github", url],
            capture_output=True, text=True, env=child_env(),
            creationflags=NO_WINDOW, timeout=120,
        )
        # On failure yt-dlp prints "null" (or nothing) and the reason on stderr.
        data = json.loads(out.stdout) if out.stdout.strip() else None
        if not isinstance(data, dict):
            err = [ln for ln in (out.stderr or "").splitlines() if "ERROR" in ln]
            msg = err[-1] if err else "yt-dlp couldn't read this link"
            raise RuntimeError(msg.replace("ERROR: ", "", 1))
        return data

    def _preview_worker(self, url):
        try:
            self.q.put(("preview", self._lookup(url)))
        except Exception as e:  # noqa: BLE001 - surface anything to the user
            self.q.put(("preview_err", str(e)))

    def _render_preview(self, data):
        if data.get("_type") == "playlist" or "entries" in data:
            entries = [e for e in (data.get("entries") or []) if e]
            have, dead = archived_ids(), unavailable_ids()
            new = sum(1 for e in entries if e.get("id") not in have | dead)
            lines = [f"PLAYLIST: {data.get('title', '(untitled)')}",
                     f"{len(entries)} track(s), {new} new  "
                     "(✓ = already downloaded, ✗ = unavailable on YouTube):", ""]
            for i, e in enumerate(entries, 1):
                vid = e.get("id")
                mark = "✓" if vid in have else "✗" if vid in dead else " "
                lines.append(f"{mark} {i:>3}. {e.get('title', '(unknown)')}")
            self.write_info("\n".join(lines))
            if new:
                self.status.config(text=f"Playlist: {new} new of {len(entries)} track(s). Ready to download.")
            else:
                self.status.config(text=f"Playlist: all {len(entries)} track(s) already downloaded.")
        else:
            title = data.get("title", "(unknown)")
            up = data.get("uploader", "")
            dur = data.get("duration")
            mins = f"{int(dur)//60}:{int(dur)%60:02d}" if dur else "?"
            got = data.get("id") in archived_ids()
            note = "\n\n✓ Already downloaded (will be skipped)." if got else ""
            self.write_info(f"SINGLE TRACK\n\n{title}\nby {up}\nlength {mins}{note}")
            self.status.config(text="Single track already downloaded." if got
                               else "Single track. Ready to download.")

    # --- Download -----------------------------------------------------------
    def on_download(self):
        url = self.get_url()
        if not url or self.busy:
            return
        s = dict(self.settings)  # snapshot: settings can't change mid-run
        RAW.mkdir(exist_ok=True)
        out_dir(s).mkdir(parents=True, exist_ok=True)
        self.set_busy(True, "Downloading…")
        self.start_runlog(f"Download: {url}  settings: {json.dumps(s)}")
        self.log_line(f"=== Starting: {url} ===")
        threading.Thread(target=self._download_worker, args=([url], s),
                         daemon=True).start()

    # --- Saved playlists ----------------------------------------------------
    def on_edit_list(self):
        if not PLAYLISTS.exists():
            PLAYLISTS.write_text(PLAYLISTS_TEMPLATE, encoding="utf-8")
        os.startfile(PLAYLISTS)  # opens in Notepad (or your .txt editor)

    def on_check_playlists(self):
        if self.busy:
            return
        urls = saved_playlists()
        if not urls:
            if messagebox.askyesno(
                    "No saved playlists",
                    "playlists.txt has no links in it yet.\n\nOpen it now to add some?"):
                self.on_edit_list()
            return
        s = dict(self.settings)  # snapshot: settings can't change mid-run
        RAW.mkdir(exist_ok=True)
        out_dir(s).mkdir(parents=True, exist_ok=True)
        self.set_busy(True, f"Checking {len(urls)} playlist(s)…")
        self.start_runlog(f"Check playlists: {len(urls)} saved  settings: {json.dumps(s)}")
        self.write_info("Checking saved playlists…")
        self.log_line(f"=== Checking {len(urls)} saved playlist(s) ===")
        threading.Thread(target=self._check_worker, args=(urls, s),
                         daemon=True).start()

    def _write_playlists(self, s, plists):
        """Write one .m3u8 per saved playlist (songs in playlist order)."""
        data = PIPELINE / "playlists-run.json"
        data.write_text(json.dumps(plists), encoding="utf-8")
        self.q.put(("log", "Writing playlist files…"))
        self._stream([PY, str(PIPELINE / "writeplaylists.py"), str(write_run_config(s)),
                      str(data)], show=lambda ln: f"  {ln.strip()}" if ln.strip() else None)

    def _check_worker(self, urls, s):
        """Look up every saved playlist, report new songs, download only those."""
        try:
            have, dead = archived_ids(), unavailable_ids()
            new_ids, lines, to_get = set(), [], []
            plists = []  # [{"title", "ids"}] for the .m3u8 files
            for url in urls:
                try:
                    data = self._lookup(url)
                except Exception as e:  # noqa: BLE001 - one bad link mustn't stop the rest
                    lines.append(f"⚠ Couldn't read: {url}\n     {e}")
                    self.q.put(("log", f"Couldn't read {url}: {e}"))
                    self.q.put(("info", "\n".join(lines)))
                    continue
                entries = [e for e in (data.get("entries") or [data]) if e]
                plists.append({"title": data.get("title") or "Playlist",
                               "ids": [e.get("id") for e in entries if e.get("id")]})
                fresh = [e for e in entries if e.get("id") not in have | dead]
                n_dead = sum(1 for e in entries if e.get("id") in dead)
                skipped = f"  ({n_dead} unavailable on YouTube)" if n_dead else ""
                lines.append(f"{data.get('title') or url}: "
                             f"{len(fresh)} new of {len(entries)}{skipped}")
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
            self.q.put(("log", f"{len(new_ids)} new song(s) to download."))
            self._download_worker(to_get, s, label=f"{len(new_ids)} new song(s)",
                                  plists=plists if s["playlist_files"] else None)
        except Exception as e:  # noqa: BLE001
            self.q.put(("fatal", str(e)))

    def _stream(self, cmd, keep=None, show=None):
        """Run cmd. Every raw line goes to last-run.log (and `keep`, if a list).
        `show(line)` decides what the window gets: a string to print, or None
        to hide it. Without `show`, nothing is printed to the window."""
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
        """Turn yt-dlp's chatter into one line per song + a live status bar."""
        st = {"n": 0, "total": 0, "songs": 0, "skipped": 0}
        item_re = re.compile(r"\[download\] Downloading item (\d+) of (\d+)")
        dest_re = re.compile(r"\[download\] Destination: (.+)")
        pct_re = re.compile(r"\[download\]\s+([\d.]+)%")

        def show(line):
            if m := item_re.search(line):
                st["n"], st["total"] = int(m.group(1)), int(m.group(2))
                return None
            if m := dest_re.search(line):
                name = Path(m.group(1)).stem
                name = re.sub(r"\s*\[[0-9A-Za-z_-]{11}\]$", "", name)  # drop [video id]
                st["songs"] += 1
                st["title"] = name
                where = f"{st['n']}/{st['total']}  " if st["total"] else ""
                return f"  ⬇ {where}{name}"
            if m := pct_re.search(line):
                pos = f" ({st['n']} of {st['total']})" if st["total"] else ""
                self.q.put(("status", f"Downloading{pos}: {st.get('title', '')} — {m.group(1)}%"))
                return None
            if "has already been recorded in the archive" in line:
                st["skipped"] += 1
                return None
            if line.startswith("ERROR:"):
                msg = re.sub(r"^ERROR: (\[[^\]]+\] )?", "", line)
                return f"  ✗ {msg}"
            return None

        return show, st

    @staticmethod
    def _only_problems(line):
        """For the art/title/album helpers: show nothing unless something failed."""
        low = line.lower()
        # Match real failures only, not a song called "Human Error".
        if ("skipped (" in low or "lookup failed" in low or "traceback" in low
                or re.search(r"\b(error|exception)\b:", low)):
            return f"  ⚠ {line.strip()}"
        return None

    def _download_worker(self, urls, s=None, label=None, plists=None):
        s = s or load_settings()
        try:
            run_cfg = str(write_run_config(s))  # output folder, layout, plugins
            if s["format"] == "mp3":
                # MP3 plays everywhere. It's a re-encode of YouTube's ~128k audio,
                # so use a generous VBR (LAME V2, ~190k) to lose as little as possible.
                fmt_args = ["-f", "bestaudio", "-x", "--audio-format", "mp3",
                            "--audio-quality", "2"]
            else:
                # Grab YouTube's AAC stream (format 140, ~128k) and keep it as-is in
                # .m4a: no re-encode, and AAC plays natively on the iPod. If a video
                # has no AAC stream, fall back to the best audio, converted to AAC 160k.
                fmt_args = ["-f", "bestaudio[ext=m4a]/bestaudio", "-x",
                            "--audio-format", "m4a", "--audio-quality", "160K"]

            self.q.put(("log", "Step 1/4 · Downloading…"))
            ytlog = []
            yt_show, yt = self._yt_filter()
            rc = self._stream([
                PY, str(YTDLP),
                "--ffmpeg-location", str(BIN),
                "--remote-components", "ejs:github",
                *fmt_args,
                "--embed-thumbnail", "--embed-metadata", "--convert-thumbnails", "jpg",
                "--no-overwrites", "--ignore-errors",
                "--newline",  # one progress update per line, for the status bar
                "--download-archive", str(ARCHIVE),  # skip songs already downloaded
                "-o", str(RAW / "%(title)s [%(id)s].%(ext)s"),
                *urls,
            ], keep=ytlog, show=yt_show)
            n_dead = record_unavailable(ytlog)
            if n_dead:
                self.q.put(("log", f"  {n_dead} song(s) unavailable on YouTube; "
                                   "they'll be skipped next time."))

            # Square-crop + shrink the 16:9 YouTube thumbnail for the iPod screen,
            # and strip "Artist - " / "(Official Audio)" etc. so beets can match.
            self.q.put(("log", "Step 2/4 · Fixing cover art and titles…"))
            self.q.put(("status", "Fixing cover art and titles…"))
            if s["square_art"]:
                size = "300" if s["small_art"] else "500"
                self._stream([PY, str(PIPELINE / "squareart.py"), str(RAW), size],
                             show=self._only_problems)
            self._stream([PY, str(PIPELINE / "cleantitles.py"), str(RAW)],
                         show=self._only_problems)

            # -s = singleton: every track tagged individually (iPod indexes by tags).
            n_files = sum(1 for p in RAW.iterdir() if p.suffix.lower() in (".m4a", ".mp3"))
            self.q.put(("log", f"Step 3/4 · Tagging {n_files} song(s) with MusicBrainz…"))
            self.q.put(("status", f"Tagging {n_files} song(s)"
                                  f"{' + volume levelling' if s['replaygain'] else ''}…"))
            # Forget songs you've deleted from the output folder, so beets doesn't
            # skip a re-download as "already in the library".
            self._stream([PY, str(PIPELINE / "prunelib.py"), run_cfg],
                         show=self._only_problems)
            beets_log = []
            self._stream([PY, "-m", "beets", "-c", run_cfg, "import", "-q", "-s", str(RAW)],
                         keep=beets_log, show=self._only_problems)
            n_asis = sum(1 for ln in beets_log if "Importing as-is" in ln)
            # Real duplicates (the song IS already in your folder): beets skips them
            # and leaves the new copy behind. Say so, and remove the leftover copy.
            dupes, current = [], None
            for ln in beets_log:
                if ln.strip().startswith(str(RAW)):
                    current = Path(ln.strip())
                elif "already in the library" in ln and current:
                    dupes.append(current)
            for p in dupes:
                name = re.sub(r"\s*\[[0-9A-Za-z_-]{11}\]$", "", p.stem)
                self.q.put(("log", f"  ⚠ Skipped, already in your library: {name}"))
                try:
                    p.unlink()
                except OSError:
                    pass

            # Album (if YouTube gave none) + real release year from MusicBrainz.
            self.q.put(("log", "Step 4/4 · Albums and release years…"))
            self.q.put(("status", "Looking up albums and years…"))
            album_log = []
            self._stream([PY, str(PIPELINE / "fillalbums.py"), run_cfg],
                         keep=album_log, show=self._only_problems)
            n_albums = sum(1 for ln in album_log if 'album "' in ln)

            if plists:
                self._write_playlists(s, plists)

            # One-glance summary.
            parts = [f"{yt['songs']} downloaded"]
            if n_dead:
                parts.append(f"{n_dead} unavailable")
            if dupes:
                parts.append(f"{len(dupes)} skipped (already in library)")
            n_matched = n_files - n_asis - len(dupes)
            if n_matched > 0:
                parts.append(f"{n_matched} matched on MusicBrainz")
            if n_asis:
                parts.append(f"{n_asis} kept YouTube's info")
            if n_albums:
                parts.append(f"{n_albums} album(s) filled in")
            self.q.put(("log", "✓ " + ", ".join(parts)))
            self.q.put(("done", label))
        except Exception as e:  # noqa: BLE001
            self.q.put(("fatal", str(e)))

    # --- Queue pump ---------------------------------------------------------
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
                    self.log_line("=== All saved playlists are up to date. ===")
                    self.set_busy(False, "All playlists up to date. Nothing new to download.")
                elif kind == "done":
                    got = f" ({payload})" if payload else ""
                    out = out_dir(self.settings)
                    self.log_line(f"Done{got}. Songs are in: {out}\n"
                                  f"(full details: {RUNLOG.name} in the pipeline folder)")
                    self.set_busy(False, f"Done{got}. Songs are in {Path(out).name}.")
                    messagebox.showinfo("Done",
                                        f"Finished{got}!\n\nTagged songs are in:\n{out}\n\n"
                                        "Copy the new songs to your player.")
                elif kind == "fatal":
                    self.set_busy(False, "Error.")
                    messagebox.showerror("Error", payload)
        except queue.Empty:
            pass
        self.root.after(100, self.drain_queue)


if __name__ == "__main__":
    if os.name == "nt":
        # Give the app its own taskbar identity; otherwise Windows groups the
        # window under pythonw.exe and shows Python's icon instead of ours.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "phoenixmp3.app")
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
    root = tk.Tk()
    App(root)
    threading.Thread(target=ensure_shortcut, daemon=True).start()
    root.mainloop()
