# Finds another upload of songs YouTube won't serve anymore.
#
#   python findcopy.py <songs.json>
#
# songs.json comes from the app: [{"id", "title", "artist", "duration"}].
# Labels sometimes pull an upload and YouTube Music quietly plays a different
# one, but the playlist still points at the old one. This searches YouTube
# Music for the same song and only accepts a result that is by the same artist,
# is the same version (a remix doesn't match the original) and is within 10
# seconds of the original length. A song YouTube age-restricts ("age": true from
# the app, or every match needs a sign-in) may instead get someone else's upload
# of the same recording: same title, version and artist, within 2 seconds of the
# original length, and downloadable without signing in (find_reupload).
# Prints one line per song:
#   found <old id> <new id> <artist> - <title>
#   none <old id>
#   error <old id> <reason>
# "none" means the search worked and nothing matched, so the app remembers it.
# "error" (blocked, offline, yt-dlp broken) isn't remembered and is retried.
import json
import re
import sys
import time
import unicodedata
import urllib.parse
from difflib import SequenceMatcher
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "yt-dlp-master"))
import yt_dlp

VERSION = re.compile(r"\b(remix|mix|edit|live|acoustic|instrumental|extended|version|"
                     r"demo|cover|sped|slowed)\b[^()\[\]]*", re.I)
class _Silent:
    """yt-dlp prints a candidate's error even when the exception is caught here. Every failure
    is handled (and reported on its own line), so those printouts would only confuse the window."""
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


OPTS = {"quiet": True, "no_warnings": True, "remote_components": ["ejs:github"], "logger": _Silent()}
AGE_WORDS = ("confirm your age", "inappropriate for some users")   # YouTube's age check
REUPLOAD_RESULTS = 6   # video search results checked for a re-upload
REUPLOAD_SECONDS = 2   # a re-upload must be this close to the real length


class AgeGated(Exception):
    """Every matching upload needs a YouTube sign-in to confirm the viewer's age."""


def age_gated(err) -> bool:
    return any(w in str(err).lower() for w in AGE_WORDS)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(official|audio|video|lyrics?|remaster(ed)?|topic|vevo|music)\b", " ", s)
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", s).split())


def clean_artist(name):
    return re.sub(r"\s*-\s*Topic$|\s+Official$|VEVO$", "", (name or "").strip())


def versions(title):
    # "Radio Edit" is left out on purpose: the album version usually is the
    # radio edit, so it shouldn't stop a match.
    return {" ".join(m.group(0).lower().split()) for m in VERSION.finditer(title or "")
            if not m.group(0).lower().startswith("edit")}


def same_artist(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a in b or b in a or SequenceMatcher(None, a, b).ratio() > 0.8)


def same_title(a, b):
    a, b = norm(a), norm(b)
    return bool(a and b) and (a in b or SequenceMatcher(None, a, b).ratio() >= 0.75)


def find(flat, full, song):
    title, artist, length = song["title"], clean_artist(song["artist"]), song.get("duration")
    q = urllib.parse.quote(f"{artist} {title}")
    res = flat.extract_info(f"https://music.youtube.com/search?q={q}#songs", download=False)
    failed = gated = None
    for e in list((res or {}).get("entries") or [])[:3]:
        if not e or e.get("id") == song["id"]:
            continue
        if not same_title(title, e.get("title")) or versions(e.get("title")) != versions(title):
            continue
        # Search results don't include the artist or length, and this also
        # checks that the copy can actually be downloaded.
        try:
            info = full.extract_info(f"https://www.youtube.com/watch?v={e['id']}", download=False)
        except Exception as err:
            if age_gated(err):
                gated = err   # permanent without a sign-in: not a reason to retry later
            else:
                failed = err  # this candidate may be dead too, try the next one
            continue
        names = [info.get("artist"), info.get("creator"), info.get("channel"),
                 info.get("uploader"), *(info.get("artists") or [])]
        if not any(same_artist(artist, n) for n in names if n):
            continue
        if length and info.get("duration") and abs(info["duration"] - length) > 10:
            continue
        return info
    if failed:
        # A title match that couldn't be checked isn't a real "no copy".
        raise failed
    if gated:
        raise AgeGated(str(gated))
    return None


def find_reupload(flat, full, song, artists=None):
    """Someone else's upload of the same recording, for a song whose official uploads
    YouTube won't play without a sign-in (age-restricted). Searches YouTube Music's
    videos and only accepts one with the same title and version, the artist in its
    title or channel, a length within REUPLOAD_SECONDS of the real one (which keeps
    out sped-up, slowed, live and cut versions), and that downloads without signing in.
    Needs the real length: without it nothing is accepted."""
    length = song.get("duration")
    if not length:
        return None
    title = song["title"]
    names = [clean_artist(a) for a in (artists or [song["artist"]]) if a]
    q = urllib.parse.quote(f"{names[0] if names else ''} {title}")
    res = flat.extract_info(f"https://music.youtube.com/search?q={q}#videos", download=False)
    for e in list((res or {}).get("entries") or [])[:REUPLOAD_RESULTS]:
        if not e or e.get("id") == song.get("id"):
            continue
        if not same_title(title, e.get("title")) or versions(e.get("title")) != versions(title):
            continue
        if e.get("duration") and abs(e["duration"] - length) > REUPLOAD_SECONDS:
            continue
        try:
            info = full.extract_info(f"https://www.youtube.com/watch?v={e['id']}", download=False)
        except Exception:
            continue   # gated or dead as well: try the next one
        credited = [info.get("artist"), info.get("creator"), info.get("channel"), info.get("uploader"),
                    *(info.get("artists") or [])]
        by_artist = any(same_artist(a, n) for a in names for n in credited if n) or \
            any(norm(a) and norm(a) in norm(info.get("title")) for a in names)
        if not by_artist or not info.get("duration") or abs(info["duration"] - length) > REUPLOAD_SECONDS:
            continue
        return info
    return None


def main():
    songs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    flat = yt_dlp.YoutubeDL({**OPTS, "extract_flat": True})
    full = yt_dlp.YoutubeDL({**OPTS, "simulate": True, "format": "bestaudio"})
    for i, song in enumerate(songs):
        if i:
            time.sleep(1.5)
        try:
            try:
                info = find(flat, full, song)
            except AgeGated:
                info, song = None, {**song, "age": True}
            if not info and song.get("age"):   # age-restricted: someone else's upload, if one matches exactly
                info = find_reupload(flat, full, song)
        except Exception as e:
            reason = " ".join(str(e).split())[:200] or type(e).__name__
            print(f"error {song['id']} {reason}", flush=True)
            continue
        if info:
            artist = info.get("artist") or clean_artist(info.get("channel"))
            print(f"found {song['id']} {info['id']} {artist} - {info.get('title')}", flush=True)
        else:
            print(f"none {song['id']}", flush=True)


if __name__ == "__main__":
    main()
