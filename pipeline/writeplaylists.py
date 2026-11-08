"""
writeplaylists.py - write one .m3u8 playlist file per saved YouTube playlist.

    python writeplaylists.py <run-config.yaml> <playlists.json>

playlists.json (written by the app) is [{"title": ..., "ids": [video ids in
order]}]. Every song the pipeline downloads keeps its YouTube link in its
tags (yt-dlp writes it, beets keeps it), so each video ID can be
mapped to the finished file in the beets library.

Files go in "<output folder>\\Playlists\\<playlist title>.m3u8", with paths
relative to that file (forward slashes, UTF-8), so the playlists keep working
when the whole output folder is copied to a player or SD card. Songs that
aren't downloaded (unavailable on YouTube, etc.) are simply left out.
Rewritten on every run, so they always match the playlist on YouTube.
Needs BEETSDIR set and libs\\ on PYTHONPATH (the app does both).
"""
import json
import os
import re
import sys
from pathlib import Path

from beets import config
from beets.library import Library

ID_RE = re.compile(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})")


def video_id(item, path):
    """The song's YouTube video ID. In .m4a files yt-dlp's link lands in the
    normal comment (beets' `comments`); in MP3s ffmpeg puts it in custom
    TXXX:purl / TXXX:comment frames instead, so read those from the file."""
    m = ID_RE.search(item.comments or "")
    if m:
        return m.group(1)
    try:
        import mutagen
        tags = mutagen.File(path).tags or {}
        for key in ("TXXX:purl", "TXXX:comment"):
            if key in tags:
                m = ID_RE.search(str(tags[key]))
                if m:
                    return m.group(1)
    except Exception:  # noqa: BLE001 - unreadable file: just leave it out
        pass
    return None


def safe_name(title):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return name or "Playlist"


def main():
    config.read()
    config.set_file(sys.argv[1])
    plists = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())

    by_id = {}
    for item in lib.items():
        path = Path(os.fsdecode(item.path))
        if path.exists():
            vid = video_id(item, path)
            if vid:
                by_id[vid] = (item, path)

    out_dir = Path(config["directory"].as_filename()) / "Playlists"
    out_dir.mkdir(parents=True, exist_ok=True)
    for pl in plists:
        lines = ["#EXTM3U"]
        found = 0
        for vid in pl["ids"]:
            if vid not in by_id:
                continue
            item, path = by_id[vid]
            rel = Path(os.path.relpath(path, out_dir)).as_posix()
            lines += [f"#EXTINF:{round(item.length or 0)},{item.artist} - {item.title}", rel]
            found += 1
        target = out_dir / f"{safe_name(pl['title'])}.m3u8"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        missing = len(pl["ids"]) - found
        print(f"playlist: {target.name} - {found} songs"
              + (f" ({missing} not downloaded)" if missing else ""), flush=True)


if __name__ == "__main__":
    main()
