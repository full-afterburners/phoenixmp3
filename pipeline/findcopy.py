# Finds another upload of songs YouTube won't serve anymore.
#
#   python findcopy.py <songs.json>
#
# songs.json comes from the app: [{"id", "title", "artist", "duration"}].
# Labels sometimes pull an upload and YouTube Music quietly plays a different
# one, but the playlist still points at the old one. This searches YouTube
# Music for the same song and only accepts a result that is by the same artist,
# is the same version (a remix doesn't match the original) and is within 10
# seconds of the original length. Prints one line per song:
#   found <old id> <new id> <artist> - <title>
#   none <old id>
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
OPTS = {"quiet": True, "no_warnings": True, "remote_components": ["ejs:github"]}


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
    for e in list((res or {}).get("entries") or [])[:3]:
        if not e or e.get("id") == song["id"]:
            continue
        if not same_title(title, e.get("title")) or versions(e.get("title")) != versions(title):
            continue
        # Search results don't include the artist or length, and this also
        # checks that the copy can actually be downloaded.
        try:
            info = full.extract_info(f"https://www.youtube.com/watch?v={e['id']}", download=False)
        except Exception:
            continue
        names = [info.get("artist"), info.get("creator"), info.get("channel"),
                 info.get("uploader"), *(info.get("artists") or [])]
        if not any(same_artist(artist, n) for n in names if n):
            continue
        if length and info.get("duration") and abs(info["duration"] - length) > 10:
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
            info = find(flat, full, song)
        except Exception:
            info = None
        if info:
            artist = info.get("artist") or clean_artist(info.get("channel"))
            print(f"found {song['id']} {info['id']} {artist} - {info.get('title')}", flush=True)
        else:
            print(f"none {song['id']}", flush=True)


if __name__ == "__main__":
    main()
