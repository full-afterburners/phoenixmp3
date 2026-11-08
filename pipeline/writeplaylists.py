# Writes an .m3u8 file for each saved playlist into <output folder>\Playlists.
#
#   python writeplaylists.py <run-config.yaml> <playlists.json>
#
# playlists.json comes from the app: [{"title": ..., "ids": [video ids]}].
# Songs are matched to files by the YouTube link yt-dlp stores in their tags.
# Paths are relative so the playlists still work after copying to a player.
import json
import os
import re
import sys
from pathlib import Path

from beets import config
from beets.library import Library

ID_RE = re.compile(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})")


def video_id(item, path):
    # .m4a files have the link in the normal comment tag. In MP3s ffmpeg puts it
    # in TXXX:purl / TXXX:comment, which beets doesn't read, so check the file.
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
    except Exception:
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
        print(f"playlist: {target.name}, {found} song" + ("" if found == 1 else "s")
              + (f" ({missing} not downloaded)" if missing else ""), flush=True)


if __name__ == "__main__":
    main()
