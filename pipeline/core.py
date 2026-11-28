# What the app's pieces share: where things are, settings and the beets run-config,
# yt-dlp's download archive and the songs YouTube won't serve, the environment every
# step runs in, and small helpers (reading a link with yt-dlp, the run log). The app
# window (app.pyw), downloadrun.py, playlistrun.py, myplaylists.py, appsettings.py and
# genrerules.py use it. It used to be the top of gui.pyw, the old tkinter window.
import collections
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import installdir

APP_NAME = "phoenixmp3"

PIPELINE = Path(__file__).resolve().parent
MUSIC = PIPELINE.parent
BIN = PIPELINE / "bin"
LIBS = PIPELINE / "libs"
RAW = PIPELINE / "newdownloads"
FINAL = installdir.FINAL                     # music_library (installdir.py: the old name, moving it)
YTDLP = PIPELINE / "yt-dlp-master" / "yt_dlp" / "__main__.py"
RUNLOG = PIPELINE / "last-run.log"
ARCHIVE = PIPELINE / "downloaded.txt"        # yt-dlp's list of already downloaded IDs
UNAVAILABLE = PIPELINE / "unavailable.txt"   # IDs YouTube won't serve
REPLACED = PIPELINE / "replaced.txt"         # other uploads found for those
PLAYLISTS = installdir.PLAYLISTS             # where it's made; installdir.playlists_file() is where it is
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
    p = Path(s.get("output_dir") or installdir.default_dir())
    return p if p.is_absolute() else MUSIC / p


def store_dir(path):
    p = Path(path)
    if p in (FINAL, installdir.default_dir()):
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
    if p and p.is_absolute() and (p in (FINAL, installdir.OLD_FINAL) or (p.name in (FINAL.name, installdir.OLD_FINAL.name) and not p.parent.exists())):
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
    plugins = "musicbrainz chroma ftintitle versionguard" + (" replaygain" if s["replaygain"] else "")
    lines = [
        "# Generated from settings.json on every run.",
        f"directory: {json.dumps(out_dir(s).as_posix())}",
        "paths:",
        f"  singleton: {json.dumps(LAYOUTS[s['layout']])}",
        f"plugins: {plugins}",
        f"pluginpath: [{json.dumps((PIPELINE / 'beetsplugins').as_posix())}]",
    ]
    if s["replaygain"]:
        lines += ["replaygain:", "  backend: ffmpeg", "  auto: yes"]
    RUN_CONFIG.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return RUN_CONFIG





# Saved playlists, download archive, unavailable songs




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


def start_runlog(what):
    """Start last-run.log afresh for a new run."""
    import datetime
    try:
        RUNLOG.write_text(f"{datetime.datetime.now():%Y-%m-%d %H:%M}  {what}\n", encoding="utf-8")
    except OSError:
        pass


def lookup(url):
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


def only_problems(line):
    """A step's output line worth showing (warnings, errors), reworded; None for the rest."""
    low = line.lower()
    # Needs the colon so a song called "Human Error" doesn't count. No \b in
    # front, so Python's "ValueError: ..." (the line that says what went
    # wrong after a traceback) is shown too.
    if ("skipped (" in low or "lookup failed" in low or "traceback" in low
            or "couldn't" in low or re.search(r"(error|exception)\b:", low)):
        return f"  Warning: {line.strip()}"
    return None
