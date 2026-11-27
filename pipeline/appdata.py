# Read-only data for the new app window (app.pyw): the library with cover art, the
# genre tree in both groupings with which songs fall under each genre, and Last.fm's
# popular albums per genre. library.db is opened read-only (sqlite mode=ro), never
# through beets' Library, so the window can't change the library while it's showing it.
# Cover thumbnails and Last.fm answers are cached in pipeline/cache/.
import base64
import hashlib
import json
import os
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PIPELINE = Path(__file__).resolve().parent
MUSIC = PIPELINE.parent
CACHE = PIPELINE / "cache"
COVERS = CACHE / "covers"
ALBUMS_CACHE = CACHE / "lastfm-albums.json"
FFMPEG = PIPELINE / "bin" / "ffmpeg.exe"
THUMB = 168             # px; grid tiles are 84 css px, so this is sharp at 2x
ALBUMS_PER_GENRE = 6
ALBUMS_MAX_AGE = 30 * 24 * 3600
TAG_FIELDS = ("lastfm_track", "lastfm_album", "lastfm_artist", "lastfm_track1", "lastfm_artist1")
CREATE_NO_WINDOW = 0x08000000
ALBUMS_LOCK = threading.Lock()   # the window calls in from several threads
LIBRARY_LOCK = threading.Lock()

# Genres the "Browse by Genre" tree always shows, on top of the ones on your list and
# in your library, so there's something to explore beyond what you already have.
POPULAR = set("""alternative rock|indie rock|punk rock|pop punk|emo|heavy metal|grunge|classic rock|hard rock|
post-punk|shoegaze|dream pop|indie pop|synth-pop|electropop|dance-pop|k-pop|house|deep house|techno|trance|
drum and bass|dubstep|ambient|trip hop|downtempo|disco|bebop|hard bop|cool jazz|swing|smooth jazz|jazz fusion|
soul jazz|vocal jazz|indie folk|americana|folk rock|bluegrass|alternative country|country pop|outlaw country|
gangsta rap|trap|southern hip hop|alternative hip hop|conscious hip hop|neo soul|funk|soul|reggaeton|salsa|
bossa nova|dancehall|ska|roots reggae|delta blues|chicago blues|blues rock|opera|baroque|modern classical|
metalcore|progressive rock|psychedelic rock|garage rock|new wave|post-rock|lo-fi|chillwave|electro house|
progressive house""".replace("\n", "").split("|"))


def load_settings() -> dict:
    try:
        return json.loads((PIPELINE / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def output_dir(settings: dict) -> Path:
    out = Path(settings.get("output_dir") or MUSIC / "downloadswithmeta")
    return out if out.is_absolute() else MUSIC / out


def open_db() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{(PIPELINE / 'library.db').as_posix()}?mode=ro", uri=True)


def has_library() -> bool:
    """beets makes library.db on the first download; a fresh install has none yet."""
    return (PIPELINE / "library.db").exists()


def as_text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")


def cover_for(path: Path) -> str | None:
    """A square JPEG thumbnail of the file's embedded art as a data URI, cached by path. (Not by
    modified time: re-filing genres rewrites every file's tags, which would redo every thumbnail.)"""
    if not path.exists():
        return None
    COVERS.mkdir(parents=True, exist_ok=True)
    cached = COVERS / (hashlib.sha1(str(path).encode("utf-8")).hexdigest() + ".jpg")
    if not cached.exists():
        scale = f"scale={THUMB}:{THUMB}:force_original_aspect_ratio=increase,crop={THUMB}:{THUMB}"
        try:
            jpg = subprocess.run([str(FFMPEG), "-loglevel", "error", "-i", str(path), "-an", "-frames:v", "1",
                                  "-vf", scale, "-q:v", "6", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                                 capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        # Write then rename, so a crash can't leave half a picture. An empty file means "no art",
        # so it isn't retried every start.
        tmp = cached.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(jpg)
        try:
            os.replace(tmp, cached)
        except OSError:   # Windows: someone else is writing or reading the same picture; theirs will do
            tmp.unlink(missing_ok=True)
            return "data:image/jpeg;base64," + base64.b64encode(jpg).decode() if jpg else None
    data = cached.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode() if data else None


def read_playlists() -> list[dict]:
    """My Playlists: [{"name", "url"}] from playlists.txt (myplaylists.py reads and edits it)."""
    import myplaylists

    return myplaylists.read()


def read_genre_list() -> list[str]:
    """The genre list in use (my-genres.txt when you've saved your own, else genres.txt)."""
    import fillgenres as fg

    lines = fg.list_file().read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.startswith("#")]


def library() -> dict:
    """Songs, albums (grouped by album artist + album, with cover art), playlists, genre list, settings.
    One load at a time: a page reload during a slow first load waits, then gets the cached covers."""
    with LIBRARY_LOCK:
        return _library()


def _library() -> dict:
    settings = load_settings()
    out_dir = output_dir(settings)
    rows = []
    if has_library():
        with open_db() as db:
            rows = db.execute("select id, title, artist, albumartist, album, year, genres, length, added, track, path, "
                              "mb_artistid, disc, disctotal, tracktotal, mb_releasegroupid from items order by added, id").fetchall()
    songs, paths, groups = [], [], defaultdict(list)
    discs = defaultdict(dict)        # album key -> {disc: its track count}, where MusicBrainz gave one
    disc_count = defaultdict(int)    # album key -> how many discs the album has
    for rid, title, artist, aa, album, year, genres, length, added, track, path, aid, disc, disctotal, tracktotal, rg in rows:
        key = ((aa or artist or "").strip(), (album or title or "").strip())
        if tracktotal:
            discs[key][disc or 1] = max(tracktotal, discs[key].get(disc or 1, 0))
            disc_count[key] = max(disc_count[key], disctotal or 1)
        p = Path(as_text(path))
        full = p if p.is_absolute() else out_dir / p
        groups[key].append(len(songs))
        songs.append({"id": rid, "t": title, "a": artist, "al": album or "", "y": year or None,
                      "g": as_text(genres).split("\x00")[0], "len": round(length or 0), "add": int(added or 0),
                      "n": track or 0, "ext": full.suffix.lstrip("."), "f": full.name,
                      "aid": as_text(aid),   # MusicBrainz artist id: opens the artist's profile
                      "rg": as_text(rg)})    # MusicBrainz release group: "Get the Rest" opens the album's page
        paths.append(full)
    keys = list(groups)
    with ThreadPoolExecutor(max_workers=4) as pool:
        covers = list(pool.map(lambda k: cover_for(paths[groups[k][0]]), keys))
    albums = []
    for (artist, name), cover in zip(keys, covers):
        idxs = groups[(artist, name)]
        years = [songs[i]["y"] for i in idxs if songs[i]["y"]]
        for i in idxs:
            songs[i]["alb"] = len(albums)
        albums.append({"id": len(albums), "name": name, "artist": artist, "songs": idxs,
                       "year": min(years) if years else None, "genre": songs[idxs[0]]["g"],
                       "added": max(songs[i]["add"] for i in idxs), "cover": cover,
                       # tracks on the whole album, None when unknown: MusicBrainz never said, or you
                       # have songs from only some of its discs (their counts don't cover the others)
                       "total": sum(discs[(artist, name)].values())
                       if discs[(artist, name)] and len(discs[(artist, name)]) >= disc_count[(artist, name)] else None,
                       "rg": next((songs[i]["rg"] for i in idxs if songs[i]["rg"]), "")})
    import genrerules

    return {"songs": songs, "albums": albums, "playlists": read_playlists(), "genres": read_genre_list(),
            "genre_rules": genrerules.current_rules(),
            "settings": {k: v for k, v in settings.items() if k != "output_dir"}, "folder": out_dir.name}


def song_tags() -> dict[int, set[str]]:
    """Per song id, every Last.fm tag kept by fillgenres plus the song's own genres."""
    if not has_library():
        return {}
    with open_db() as db:
        rows = db.execute("select id, genres from items").fetchall()
        attrs = db.execute("select entity_id, key, value from item_attributes where key in (%s)"
                           % ",".join("?" * len(TAG_FIELDS)), TAG_FIELDS).fetchall()
    kept = defaultdict(set)
    for rid, _, value in attrs:
        try:
            kept[rid].update(t.lower() for t in json.loads(as_text(value)))
        except (ValueError, TypeError, AttributeError):
            continue   # a hand-edited or damaged value: just don't use it
    out = {}
    for rid, genres in rows:
        own = {g.lower() for g in as_text(genres).split("\x00") if g}
        out[rid] = kept[rid] | own
    return out


def genres() -> dict:
    """The genre tree (beets' history grouping and our sounds-like grouping), trimmed to the
    genres worth showing, with the songs under each genre. Albums come from genre_albums()."""
    import yaml
    from beetsplug.lastgenre import flatten_tree

    import fillgenres as fg

    tree = PIPELINE / "libs" / "beetsplug" / "lastgenre" / "genres-tree.yaml"
    branches = []
    flatten_tree(yaml.safe_load(tree.read_text(encoding="utf-8")), [], branches)
    hist = fg.tree_parents(branches)
    sound = fg.sound_parents(hist)
    top = sorted(fg.allowed())
    tags = song_tags()
    present = {t for ts in tags.values() for t in ts}
    wanted = {g for g in set(top) | POPULAR | present if g in hist}
    for g in list(wanted):   # keep every genre's parents so the tree stays connected in both groupings
        for parents in (hist, sound):
            wanted.update(fg.ancestors(g, parents))
    # "lib" holds song ids, not positions: the library may have changed since library() was read.
    # The page turns them into positions in its own song list ("by": "id").
    nodes = {g: {"albums": None, "lib": sorted(i for i, ts in tags.items() if g in ts)} for g in wanted}
    return {"top": top, "hist": {g: hist[g] for g in wanted}, "sound": {g: sound[g] for g in wanted}, "nodes": nodes,
            "by": "id"}


def _albums_cache() -> dict:
    try:
        return json.loads(ALBUMS_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def genre_albums(tag: str) -> list[dict]:
    """Last.fm's top albums for a genre tag (title, artist, cover URL), cached for 30 days.
    Raises OSError/ValueError if Last.fm can't be reached; the page shows that."""
    cache = _albums_cache()
    hit = cache.get(tag)
    if hit and time.time() - hit["at"] < ALBUMS_MAX_AGE:
        return hit["albums"]
    from beets import plugins

    q = urllib.parse.urlencode({"method": "tag.gettopalbums", "tag": tag, "api_key": plugins.LASTFM_KEY,
                                "format": "json", "limit": ALBUMS_PER_GENRE})
    with urllib.request.urlopen("https://ws.audioscrobbler.com/2.0/?" + q, timeout=20) as r:
        body = json.load(r)
    if "error" in body or "albums" not in body:   # rate limit, service down...: an error, not "no albums"
        raise ValueError(body.get("message") or "Last.fm sent an answer without albums")
    found = body["albums"].get("album", [])
    if isinstance(found, dict):   # Last.fm sends a single album as an object, not a list
        found = [found]
    albums = [{"title": a.get("name"), "artist": (a.get("artist") or {}).get("name"),
               "cover": next((i["#text"] for i in a.get("image", []) if i.get("size") == "large" and i.get("#text")), None)}
              for a in found]
    with ALBUMS_LOCK:
        cache = _albums_cache()   # re-read: another genre may have been saved meanwhile
        cache[tag] = {"at": time.time(), "albums": albums}
        CACHE.mkdir(parents=True, exist_ok=True)
        ALBUMS_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return albums
