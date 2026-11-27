# Search and artist profiles for the new app window's Get Music page (app.pyw). Searching
# goes to MusicBrainz through mbsearch.py's functions (beets' client, which keeps to
# MusicBrainz's rate limit). An artist profile puts together who they are (MusicBrainz),
# a photo (Wikidata/Wikimedia Commons, when there is one), a short bio, tags, listeners
# and their most played songs (Last.fm), and their albums, EPs and singles (MusicBrainz).
# An album page is the album's editions grouped by track count, with the track list of
# the standard one. Read-only: nothing here changes the library or downloads anything.
# Profiles and album pages are cached for a week in pipeline/cache/, because a big
# discography takes several MusicBrainz requests at one per second.
import hashlib
import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

import mbsearch

PIPELINE = Path(__file__).resolve().parent
CACHE = PIPELINE / "cache"
MAX_AGE = 7 * 24 * 3600
PROFILE_PAGES = 3          # 300 release groups is plenty for a profile and keeps big artists to a few seconds
TOP_SONGS = 10
TIMEOUT = 20
USER_AGENT = "phoenixmp3/1.0 (desktop music tagger)"
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
QID = re.compile(r"/(Q\d+)$")
TAG = re.compile(r"<[^>]+>")
READ_MORE = re.compile(r"\s*Read more on Last\.fm\.?\s*$", re.I)
SEARCHES = {"albums": mbsearch.search_albums, "songs": mbsearch.search_songs, "artists": mbsearch.search_artists}
_api = None
_api_lock = threading.Lock()
_cache_lock = threading.Lock()


def api():
    """One MusicBrainz client for the whole window, so its rate limit covers every request."""
    global _api
    with _api_lock:
        if _api is None:
            from beets import config
            from beetsplug._utils.musicbrainz import MusicBrainzAPI

            config.read()
            _api = MusicBrainzAPI()
        return _api


def check_id(value) -> str:
    if not isinstance(value, str) or not MBID.match(value):
        raise ValueError("that isn't a MusicBrainz id")
    return value


def _cache_file(kind: str, key: str) -> Path:
    return CACHE / kind / f"{key}.json"


def cached(kind: str, key: str, build):
    """build()'s result, kept for a week in cache/<kind>/<key>.json."""
    path = _cache_file(kind, key)
    try:
        hit = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - hit["at"] < MAX_AGE:
            return hit["data"]
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    data = build()
    with _cache_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"at": time.time(), "data": data}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    return data


def get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def lastfm(method: str, **params) -> dict:
    """One Last.fm API call with beets' key. Raises ValueError when Last.fm answers with an error."""
    from beets import plugins

    q = urllib.parse.urlencode({"method": method, "api_key": plugins.LASTFM_KEY, "format": "json", **params})
    body = get_json("https://ws.audioscrobbler.com/2.0/?" + q)
    if "error" in body:
        raise ValueError(body.get("message") or f"Last.fm error {body['error']}")
    return body


# ---------- search ----------

def search(kind: str, text: str, artist: str | None = None) -> list[dict]:
    if kind not in SEARCHES:
        raise ValueError(f"unknown search: {kind!r}")
    if not isinstance(text, str) or not text.strip() or len(text) > 200:
        raise ValueError("type something to search for")
    if artist is not None and (not isinstance(artist, str) or len(artist) > 200):
        raise ValueError("bad artist name")
    text, artist = text.strip(), (artist or "").strip() or None
    if kind == "artists":
        return rank_artists(text, lastfm_search(kind, text), mbsearch.search_artists(api(), text)["results"])[:mbsearch.LIMIT]
    popular = lastfm_search(kind, text, artist)
    if artist:
        rows = SEARCHES[kind](api(), text, artist)["results"]
    else:
        # MusicBrainz's free-text search can miss the famous one entirely (25 obscure songs called
        # "Fade Into You", no Mazzy Star), so first ask for the title by Last.fm's most played artists.
        top = list(dict.fromkeys(f["artist"] for f in popular if f["artist"]))[:POPULAR_ARTISTS]
        rows = SEARCHES[kind](api(), text, top, limit=WIDE)["results"] if top else []
        # MusicBrainz matches any word of the title, so "tickets to my downfall" by mgk also finds
        # "times of my life": keep what Last.fm named, or titles with every word typed
        named, words = {_key(f["artist"], f["title"]) for f in popular}, set(_key(text).split())
        rows = [r for r in rows if _key(r["artist"], r["title"]) in named or words <= set(_key(r["title"]).split())]
        if len(rows) < ENOUGH:   # little-known music: MusicBrainz's own matches fill up the list
            seen = {r["id"] for r in rows}
            rows += [r for r in SEARCHES[kind](api(), text)["results"] if r["id"] not in seen]
    rows = popular_first(kind, popular, rows)
    if kind == "songs":
        rows = one_per_song(rows)
    return rows[:mbsearch.LIMIT]


POPULAR_ARTISTS = 8
WIDE = 50       # one popular song can have dozens of recordings (live, compilations): room for the others too
ENOUGH = 10
TYPE_RANK = {"Album": 0, "EP": 1, "Single": 2}


def one_per_song(rows: list[dict]) -> list[dict]:
    """A famous song has dozens of recordings (bootlegs, compilations, live). Keep the first of each
    artist + title; mbsearch already puts the studio original first."""
    seen, out = set(), []
    for r in rows:
        k = _key(r["artist"], r["title"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _key(*parts) -> str:
    return "\0".join(re.sub(r"[^0-9a-z]+", " ", (p or "").lower()).strip() for p in parts)


# Last.fm search methods, and where their results and names are
LASTFM_SEARCH = {"songs": ("track.search", "track", "trackmatches", "track"),
                 "albums": ("album.search", "album", "albummatches", "album"),
                 "artists": ("artist.search", "artist", "artistmatches", "artist")}


def lastfm_search(kind: str, text: str, artist: str | None = None) -> list[dict]:
    """Last.fm's search, most listened first: [{"artist", "title" ("" for artists), "mbid", "listeners"}].
    mbid is the MusicBrainz id when Last.fm knows it (artist id for artists, recording id for songs,
    release id for albums); listeners is 0 where Last.fm doesn't say (albums).
    Empty when Last.fm can't be reached: then MusicBrainz's own order is used."""
    method, param, box, item = LASTFM_SEARCH[kind]
    extra = {"artist": artist} if artist and kind == "songs" else {}
    try:
        found = (lastfm(method, **{param: text}, limit=30, **extra).get("results") or {}).get(box, {}).get(item) or []
    except (OSError, ValueError):
        return []
    if isinstance(found, dict):
        found = [found]
    def listeners(f):
        try:
            return int(f.get("listeners") or 0)
        except (TypeError, ValueError):
            return 0
    if kind == "artists":
        return [{"artist": f.get("name") or "", "title": "", "mbid": f.get("mbid") or "", "listeners": listeners(f)} for f in found]
    return [{"artist": f.get("artist") or "", "title": f.get("name") or "", "mbid": f.get("mbid") or "", "listeners": listeners(f)}
            for f in found]


def popular_first(kind: str, popular: list[dict], rows: list[dict]) -> list[dict]:
    """MusicBrainz scores every good text match about the same, so a well-known song can come after
    an obscure one with the same title. Results Last.fm knows go first, in its order (most listened),
    matched by MusicBrainz id where Last.fm gives one (songs), else by artist + title; the rest keep
    MusicBrainz's order. Songs Last.fm counted get their "listeners" for the page to show."""
    found = popular
    rank, by_id = {}, {}
    for i, f in enumerate(popular):
        rank.setdefault(_key(f["artist"], f["title"]), i)
        if f["mbid"]:
            by_id.setdefault(f["mbid"], i)
    for r in rows:
        i = by_id.get(r.get("id"))
        if i is None:
            i = next((rank[k] for k in (_key(n, r.get("title")) for n in (r.get("artist_names") or []) + [r.get("artist")]) if k in rank), None)
        if i is not None and popular[i]["listeners"]:
            r["listeners"] = popular[i]["listeners"]

    def place(pair):
        i, r = pair
        title = r.get("title") if kind != "artists" else ""
        names = (r.get("artist_names") or []) + [r.get("artist") or r.get("name")]
        hits = [rank[k] for k in (_key(n, title) for n in names) if k in rank]
        if r.get("id") in by_id:
            hits.append(by_id[r["id"]])
        # MusicBrainz's weak matches (score under 80) go last unless Last.fm named them
        # an album before a single of the same name; live albums and compilations after both
        kind_rank = (bool(r.get("secondary")), TYPE_RANK.get(r.get("type"), 3)) if kind == "albums" else (False, 0)
        return (not hits and (r.get("score") or 0) < 80, min(hits) if hits else len(found), kind_rank, i)

    return [r for _, r in sorted(enumerate(rows), key=place)]


def _library_artist_ids() -> set[str]:
    """MusicBrainz ids of the artists in your library (read-only), so they rank first."""
    import appdata
    if not appdata.has_library():
        return set()
    try:
        with appdata.open_db() as db:
            return {appdata.as_text(a) for (a,) in db.execute("select distinct mb_artistid from items") if a}
    except Exception:   # noqa: BLE001 - only a ranking hint; a busy or odd database mustn't break search
        return set()


def _bare(name: str) -> str:
    """A name for "is this exactly what was typed": case, punctuation and a leading "the" don't count."""
    k = _key(name)
    return k[4:] if k.startswith("the ") else k


def rank_artists(text: str, found: list[dict], rows: list[dict]) -> list[dict]:
    """Artists in order: yours first, then exact name matches, then the rest; within each, most
    listened first (Last.fm), then MusicBrainz's own order. Last.fm's numbers are matched by
    MusicBrainz id where it gives one, so the famous Nirvana doesn't lend its listeners to every
    band called Nirvana; a name Last.fm only knows without an id counts by name. An unknown band
    whose exact name was typed still comes before famous partial matches."""
    by_id = {f["mbid"]: f["listeners"] for f in found if f["mbid"]}
    claimed = {_key(r.get("name")) for r in rows if r.get("id") in by_id}   # names whose real owner is known
    by_name = {}
    for f in found:
        if not f["mbid"]:
            by_name.setdefault(_key(f["artist"]), f["listeners"])
    mine, typed = _library_artist_ids(), _bare(text)
    for r in rows:
        k = _key(r.get("name"))
        r["listeners"] = by_id.get(r.get("id")) or (0 if k in claimed else by_name.get(k, 0))

    words = set(typed.split())

    def place(pair):
        i, r = pair
        name = _bare(r.get("name"))
        # yours first only when the name has what was typed: MusicBrainz also matches nicknames
        # ("queen" finds Ella Fitzgerald, "Queen of Jazz"), which shouldn't jump ahead of Queen
        yours = r.get("id") in mine and words <= set(name.split())
        weak = (r.get("score") or 0) < 80 and not r["listeners"]
        return (not yours, name != typed, weak, -r["listeners"], i)

    return [r for _, r in sorted(enumerate(rows), key=place)]


# ---------- artist profile ----------

def _photo(url_relations: list) -> str | None:
    """A photo of the artist from Wikimedia Commons, via the Wikidata page MusicBrainz links to."""
    wikidata = next((r["url"]["resource"] for r in url_relations or []
                     if r.get("type") == "wikidata" and (r.get("url") or {}).get("resource")), None)
    m = QID.search(wikidata or "")
    if not m:
        return None
    q = urllib.parse.urlencode({"action": "wbgetclaims", "entity": m.group(1), "property": "P18", "format": "json"})
    claims = get_json("https://www.wikidata.org/w/api.php?" + q).get("claims", {}).get("P18") or []
    name = next((c["mainsnak"]["datavalue"]["value"] for c in claims
                 if (c.get("mainsnak") or {}).get("datavalue")), None)
    if not name:
        return None
    return "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(name.replace(" ", "_")) + "?width=480"


def _bio(text: str) -> str:
    text = READ_MORE.sub("", TAG.sub("", text or "")).strip()
    return text.replace("&quot;", '"').replace("&amp;", "&").replace("&#39;", "'")


def _lastfm_artist(mbid: str, name: str) -> dict:
    """Bio, tags, listeners and top songs. Looked up by MusicBrainz id, or by name when Last.fm doesn't know the id."""
    def call(method: str, **extra) -> dict:
        try:
            return lastfm(method, mbid=mbid, **extra)
        except ValueError:
            return lastfm(method, artist=name, autocorrect=1, **extra)

    info = call("artist.getinfo").get("artist") or {}
    tops = (call("artist.gettoptracks", limit=TOP_SONGS).get("toptracks") or {}).get("track") or []
    if isinstance(tops, dict):   # Last.fm sends a single item as an object
        tops = [tops]
    tags = (info.get("tags") or {}).get("tag") or []
    if isinstance(tags, dict):
        tags = [tags]
    return {
        "bio": _bio((info.get("bio") or {}).get("summary")),
        "listeners": int((info.get("stats") or {}).get("listeners") or 0),
        "tags": [t.get("name") for t in tags if t.get("name")][:6],
        "top": [{"title": t.get("name"), "plays": int(t.get("playcount") or 0)} for t in tops if t.get("name")],
    }


def _releases(artist_mbid: str) -> dict:
    """The artist's own albums, EPs and singles, newest first (no live albums, compilations or remixes)."""
    groups = mbsearch.browse_all(api(), "release-group", max_pages=PROFILE_PAGES, artist=artist_mbid, type="album|ep|single")
    out = defaultdict(list)
    for g in groups:
        if g.get("secondary_types") or g.get("primary_type") not in ("Album", "EP", "Single"):
            continue
        out[g["primary_type"].lower() + "s"].append({"id": g["id"], "title": g.get("title"),
                                                    "year": mbsearch.year(g.get("first_release_date")),
                                                    "date": g.get("first_release_date") or ""})
    for rows in out.values():
        rows.sort(key=lambda r: r["date"], reverse=True)
    return {k: out.get(k, []) for k in ("albums", "eps", "singles")}


def _build_artist(mbid: str) -> dict:
    a = api()._lookup("artist", mbid, includes=["url-rels"])
    profile = {
        "id": mbid, "name": a.get("name"), "type": a.get("type") or "", "disambiguation": a.get("disambiguation") or "",
        "area": (a.get("area") or {}).get("name") or "", "from": (a.get("begin_area") or {}).get("name") or "",
        "years": mbsearch.years_active(a.get("life_span")), "photo": None, "bio": "", "listeners": 0, "tags": [],
        "top": [], "problems": [],
    }
    profile.update(_releases(mbid))
    # The extras are nice to have: a profile still shows when Wikidata or Last.fm can't be reached.
    try:
        profile["photo"] = _photo(a.get("url_relations"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        profile["problems"].append(f"no photo ({type(e).__name__})")
    try:
        profile.update(_lastfm_artist(mbid, profile["name"]))
    except (OSError, ValueError) as e:
        profile["problems"].append(f"no Last.fm info ({' '.join(str(e).split())[:120]})")
    return profile


def artist(mbid: str) -> dict:
    mbid = check_id(mbid)
    return cached("artists", mbid, lambda: _build_artist(mbid))


# ---------- album page ----------

def edition_groups(rows: list[dict], default: str | None) -> list[dict]:
    """Editions grouped by track count (standard, expanded, deluxe...), each with one release to show:
    the suggested default for its group, else the earliest digital one, else the earliest."""
    by_count = defaultdict(list)
    for r in rows:
        if r["tracks"]:
            by_count[r["tracks"]].append(r)
    out = []
    for n, group in sorted(by_count.items()):
        pick = next((r for r in group if r["id"] == default), None) or min(
            group, key=lambda r: ("Digital Media" not in r["format"], r["status"] != "Official", r["date"] or "9999"))
        out.append({"tracks": n, "count": len(group), "default": pick["id"] == default, "release": pick["id"],
                    "formats": sorted({f for r in group for f in r["format"].split(" + ")})})
    return out


def _build_album(group_id: str) -> dict:
    ed = mbsearch.editions(api(), group_id)
    groups = edition_groups(ed["editions"], ed["default"])
    if not groups:
        raise ValueError("MusicBrainz has no track list for this album")
    first = next((g for g in groups if g["default"]), groups[0])
    return {"editions": groups, **mbsearch.tracks(api(), first["release"])}


def album(group_id: str) -> dict:
    group_id = check_id(group_id)
    return cached("albums", group_id, lambda: _build_album(group_id))


def _find_library_album(name: str, artist: str) -> dict:
    rows = mbsearch.search_albums(api(), name, artist)["results"]
    same = [r for r in rows if _key(r["title"]) == _key(name)]
    # the studio album over a live album or compilation with the same name
    hit = min(same, key=lambda r: (bool(r["secondary"]), TYPE_RANK.get(r["type"], 9)), default=None)
    if not hit:
        return {}
    editions = album(hit["id"])["editions"]
    standard = next((g for g in editions if g["default"]), editions[0])
    return {"rg": hit["id"], "tracks": standard["tracks"], "most": max(g["tracks"] for g in editions)}


def library_album(name, artist) -> dict:
    """An album in your library, which most songs know only by name: its MusicBrainz release group
    and track count (standard edition; "most" is the biggest edition), or {} if MusicBrainz has no
    album by that name and artist. Cached a week, like album pages."""
    if not all(isinstance(v, str) and v.strip() and len(v) <= 300 for v in (name, artist)):
        raise ValueError("bad album name")
    key = hashlib.sha1(_key(artist, name).encode()).hexdigest()
    return cached("libalbums", key, lambda: _find_library_album(name, artist))


def release(release_id: str) -> dict:
    """One edition's track list (switching editions on an album page)."""
    release_id = check_id(release_id)
    return cached("releases", release_id, lambda: mbsearch.tracks(api(), release_id))
