# Writes an .m3u8 file for each saved playlist into <output folder>\Playlists.
#
#   python writeplaylists.py <run-config.yaml> <playlists.json>
#
# playlists.json comes from the app: [{"title": ..., "ids": [video ids]}].
# Songs are matched to files by the YouTube link yt-dlp stores in their tags.
# Paths are relative so the playlists still work after copying to a player.
# relink() is used by fillalbums.py: when a song file is moved or renamed, every
# .m3u8 line pointing at it is updated.
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


def relink(moves: dict, out_root: Path) -> int:
    """After songs were moved or renamed ({old path: new path}), point the lines of every .m3u8 in
    <out_root>/Playlists at the new paths, so a playlist this app wrote (or one pasted once, which
    nothing rewrites) never loses a song. Returns how many lines changed."""
    pl_dir = out_root / "Playlists"
    if not moves or not pl_dir.is_dir():
        return 0
    moved = {os.path.normcase(os.path.abspath(o)): Path(n) for o, n in moves.items()}
    changed = 0
    for m3u in pl_dir.glob("*.m3u8"):
        lines = m3u.read_text(encoding="utf-8-sig").splitlines()
        out = []
        for ln in lines:
            if ln and not ln.startswith("#"):
                new = moved.get(os.path.normcase(os.path.abspath(pl_dir / ln)))
                if new:
                    ln = Path(os.path.relpath(new, pl_dir)).as_posix()
                    changed += 1
            out.append(ln)
        if out != lines:
            tmp = m3u.with_suffix(".m3u8.tmp")
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            os.replace(tmp, m3u)
    return changed


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
