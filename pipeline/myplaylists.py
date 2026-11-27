# My Playlists for the new app window (app.pyw): the YouTube playlists saved in
# playlists.txt (in the pipeline folder; installdir.py moves an older one there), the same file the classic window and
# its "Add or remove My Playlists" button use. One link per line; a short "# Name"
# comment right above a link is its name. Adding appends "# Name" + the link, removing
# takes out the link and its name line, and every other line (your own comments, the
# instructions at the top) stays as it was. check() asks yt-dlp which songs are new.
import json
import os
import threading
from pathlib import Path

import core
import installdir

TITLES = installdir.PIPELINE / "cache" / "playlist-titles.json"   # YouTube's names, for unnamed playlists
MAX_NAME = 40
THUMBS = 4
_lock = threading.Lock()


def _label(line: str) -> str | None:
    """The name in a "# Name" line, or None if it's another kind of comment."""
    text = line.strip().lstrip("#").strip()
    if not line.strip().startswith("#") or not text or len(text) >= MAX_NAME or "http" in text or "playlist" in text.lower():
        return None
    return text


def _lines() -> list[str]:
    try:
        return installdir.playlists_file().read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []


def _titles() -> dict:
    """YouTube's own title for each playlist, remembered from its last check."""
    try:
        return json.loads(TITLES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _remember_title(url: str, title: str) -> None:
    with _lock:
        titles = _titles()
        if not title or titles.get(url) == title:
            return
        titles[url] = title
        try:
            TITLES.parent.mkdir(exist_ok=True)
            tmp = TITLES.with_suffix(".tmp")
            tmp.write_text(json.dumps(titles, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, TITLES)
        except OSError:
            pass   # only a nicer name; the next check tries again


def read() -> list[dict]:
    """[{"name", "url", "named"}] in file order. A playlist without its own "# Name" line shows
    YouTube's title (once it has been checked), else "Playlist N"."""
    out, label, titles = [], None, _titles()
    for line in _lines():
        s = line.strip()
        if not s or s.startswith("#"):
            # A name counts only right above its link (what remove() takes out with it), so the
            # instructions' example "# Gym" doesn't name the first real playlist
            label = _label(s) if s else None
        else:
            url = s.strip('"')
            out.append({"name": label or titles.get(url) or f"Playlist {len(out) + 1}", "url": url, "named": bool(label)})
            label = None
    return out


def _write(lines: list[str]) -> None:
    f = installdir.playlists_file()
    tmp = f.with_suffix(".txt.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, f)   # all or nothing


def _check_url(url) -> str:
    url = url.strip() if isinstance(url, str) else ""
    # urlsplit quietly drops line breaks inside a link, so check for them first: one could
    # otherwise write extra lines into playlists.txt
    if not url.isprintable() or any(c.isspace() or c in "\"'<>" for c in url) or url.startswith("#"):
        raise ValueError("that isn't a YouTube or YouTube Music playlist link")
    if not core.is_youtube_link(url) or "list=" not in url:
        raise ValueError("that isn't a YouTube or YouTube Music playlist link")
    return url


def add(url, name) -> list[dict]:
    url = _check_url(url)
    name = name.strip() if isinstance(name, str) else ""
    if name and (len(name) >= MAX_NAME or not name.isprintable() or "http" in name or "playlist" in name.lower()):
        # the same rules as reading names back, so the name isn't lost on the next read
        raise ValueError(f"names need to be under {MAX_NAME} characters, without “http” or “playlist”")
    with _lock:
        lines = _lines() or core.PLAYLISTS_TEMPLATE.splitlines()
        if any(line.strip().strip('"') == url for line in lines):
            raise ValueError("that playlist is already in My Playlists")
        while lines and not lines[-1].strip():
            lines.pop()
        _write(lines + [""] + ([f"# {name}"] if name else []) + [url])
    return read()


def remove(url) -> list[dict]:
    if not isinstance(url, str):
        raise ValueError("bad playlist link")
    with _lock:
        lines = _lines()
        at = next((i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith("#")
                   and line.strip().strip('"') == url.strip()), None)
        if at is None:
            raise ValueError("that playlist isn't in My Playlists")
        start = at - 1 if at and _label(lines[at - 1]) else at   # its name line goes too
        _write(lines[:start] + lines[at + 1:])
    return read()


def _thumb(entry: dict) -> str | None:
    thumbs = [t for t in entry.get("thumbnails") or [] if t.get("url")]
    if not thumbs:
        return None
    return min(thumbs, key=lambda t: abs((t.get("width") or 320) - 320))["url"]


def check(url) -> dict:
    """What Update would get from one saved playlist: new songs, and songs YouTube won't serve."""
    if url not in {p["url"] for p in read()}:
        raise ValueError("that playlist isn't in My Playlists")
    data = core.lookup(url)   # yt-dlp, no download; raises with yt-dlp's reason
    _remember_title(url, (data.get("title") or "").strip()[:MAX_NAME * 2])
    entries = [e for e in (data.get("entries") or [data]) if e and e.get("id")]
    have, dead, rep = core.archived_ids(), core.unavailable_ids(), core.replacements()
    fresh = [e for e in entries if e["id"] not in have | dead]
    gone = [e for e in entries if e["id"] in dead]
    return {
        "url": url, "title": data.get("title") or "", "total": len(entries),
        "new": [{"id": e["id"], "title": e.get("title") or e["id"]} for e in fresh],
        "swapped": sum(1 for e in gone if rep.get(e["id"]) in have),
        "unavailable": sum(1 for e in gone if rep.get(e["id"]) not in have),
        "thumbs": [t for t in (_thumb(e) for e in entries[:THUMBS * 3]) if t][:THUMBS],
    }
