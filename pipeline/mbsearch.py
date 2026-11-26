# Searches MusicBrainz so albums and songs can be downloaded without a YouTube
# link. MusicBrainz says what to get (the real track list, order and lengths);
# findtracks.py then finds each track on YouTube Music. Goes through beets'
# MusicBrainz client, which keeps to MusicBrainz's rate limit.
#
#   python mbsearch.py albums "<text>" ["<artist>"]  albums, EPs and singles matching the text
#   python mbsearch.py songs "<text>" ["<artist>"]   songs (recordings) matching the text
#   python mbsearch.py artists "<text>"        artists matching the text
#   python mbsearch.py discography <artist id> that artist's own albums and EPs
#   python mbsearch.py editions <album id>     the album's releases (standard, deluxe...)
#                                              and the one suggested as the default
#   python mbsearch.py tracks <release id>     one release's album details and track list
#
# With an artist, the text must match the title and the artist the artist field;
# without one the text is matched against everything (titles of covers too).
# Prints one JSON object. Search results come best match first, but a search can
# rank another artist's album with the same name first, so let the user pick.
# On failure it prints {"error": "..."} and exits 1.
import json
import re
import sys
from collections import Counter

from beets import config
# beets' client handles MusicBrainz's rate limit and retries.
from beetsplug._utils.musicbrainz import MusicBrainzAPI

LIMIT = 25
# Versions of a song (same idea as cleantitles/fillalbums/findcopy): ranked after the original.
VERSION = re.compile(r"(live|remix|mix|edit|acoustic|instrumental|demo|cover|karaoke|sped|slowed)", re.I)
# Prefer worldwide / US releases when choosing an album's default edition.
COUNTRY_RANK = {"XW": 0, "US": 1, "GB": 2}


def credit(artist_credit: list) -> str:
    """'Machine Gun Kelly feat. Halsey' from MusicBrainz's artist credit list."""
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in artist_credit or []).strip()


def year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


def seconds(ms) -> int | None:
    return round(ms / 1000) if ms else None


def query(api, entity: str, title_field: str, text: str, artist: str | None) -> list:
    """Search one entity. With an artist, match it as credited ("Machine Gun Kelly")
    or by the artist's own name ("mgk"), which beets' search() can't combine."""
    if not artist:
        return api.search(entity, {"": text}, limit=LIMIT)
    t, a = api.format_search_term("", text), api.format_search_term("", artist)
    q = f"{title_field}:({t}) AND (artist:({a}) OR artistname:({a}))"
    return api._get_resource(entity, query=q, limit=LIMIT)[entity.replace("-", "_") + "s"]


def search_albums(api, text: str, artist: str | None = None) -> dict:
    found = query(api, "release-group", "releasegroup", text, artist)
    return {"results": [{
        "id": rg["id"], "title": rg.get("title"), "artist": credit(rg.get("artist_credit")),
        "type": rg.get("primary_type"), "secondary": rg.get("secondary_types") or [],
        "year": year(rg.get("first_release_date")), "score": rg.get("score"),
    } for rg in found]}


def search_songs(api, text: str, artist: str | None = None) -> dict:
    found = query(api, "recording", "recording", text, artist)
    out = []
    for rec in found:
        releases = rec.get("releases") or []
        studio = [r for r in releases if r.get("status") == "Official"
                  and (r.get("release_group") or {}).get("primary_type") == "Album"
                  and not (r.get("release_group") or {}).get("secondary_types")]
        first = min(studio or releases, key=lambda r: r.get("date") or "9999", default={})
        out.append({
            "id": rec["id"], "title": rec.get("title"), "artist": credit(rec.get("artist_credit")),
            "length": seconds(rec.get("length")), "disambiguation": rec.get("disambiguation") or "",
            "album": (first.get("release_group") or {}).get("title") or first.get("title"),
            "year": year(rec.get("first_release_date") or first.get("date")),
            "score": rec.get("score"), "_studio": bool(studio),
            "_version": bool(VERSION.search(f"{rec.get('title')} {rec.get('disambiguation') or ''}")),
        })
    # The same song turns up once per recording: the studio original first, then
    # ones only on compilations or with no length, which match YouTube worse.
    # MusicBrainz scores 80+ are all good text matches, so those are ordered by the rest.
    out.sort(key=lambda r: ((r["score"] or 0) < 80, r["_version"], not r["_studio"],
                            r["length"] is None, r["year"] or 9999))
    for r in out:
        del r["_studio"], r["_version"]
    return {"results": out}


def search_artists(api, text: str) -> dict:
    found = api.search("artist", {"": text}, limit=LIMIT)
    return {"results": [{
        "id": a["id"], "name": a.get("name"), "disambiguation": a.get("disambiguation") or "",
        "country": a.get("country"), "type": a.get("type"), "score": a.get("score"),
    } for a in found]}


def discography(api, artist_id: str) -> dict:
    groups = api._browse("release-group", artist=artist_id, type="album|ep", limit=100)
    own = [g for g in groups if not g.get("secondary_types")]   # no live albums, compilations...
    own.sort(key=lambda g: g.get("first_release_date") or "9999")
    return {"results": [{
        "id": g["id"], "title": g.get("title"), "type": g.get("primary_type"),
        "year": year(g.get("first_release_date")),
    } for g in own]}


def editions(api, group_id: str) -> dict:
    releases = api._browse("release", inc="media", limit=100, **{"release-group": group_id})
    rows = [{
        "id": r["id"], "title": r.get("title"), "date": r.get("date"), "country": r.get("country"),
        "status": r.get("status"), "disambiguation": r.get("disambiguation") or "",
        "format": " + ".join(sorted({m.get("format") or "?" for m in r.get("media") or []})),
        "tracks": sum(m.get("track_count") or 0 for m in r.get("media") or []),
    } for r in releases]
    return {"default": default_edition(rows), "editions": rows}


def default_edition(rows: list[dict]) -> str | None:
    """The standard edition: official, the most common track count (deluxe and
    bonus-track editions are the odd ones out), digital first, then the earliest."""
    official = [r for r in rows if r["status"] == "Official"] or rows
    if not official:
        return None
    usual = Counter(r["tracks"] for r in official).most_common(1)[0][0]
    pick = min((r for r in official if r["tracks"] == usual), key=lambda r: (
        "Digital Media" not in r["format"], r["date"] or "9999", COUNTRY_RANK.get(r["country"], 9)))
    return pick["id"]


def tracks(api, release_id: str) -> dict:
    rel = api.get_release(release_id, includes=["recordings", "artist-credits", "release-groups"])
    group = rel.get("release_group") or {}
    album_artist = credit(rel.get("artist_credit"))
    media = rel.get("media") or []
    out = []
    for disc, medium in enumerate(media, 1):
        for t in medium.get("tracks") or []:
            rec = t.get("recording") or {}
            out.append({
                "disc": disc, "n": t.get("position"), "title": t.get("title") or rec.get("title"),
                "artist": credit(t.get("artist_credit") or rec.get("artist_credit")) or album_artist,
                "length": seconds(t.get("length") or rec.get("length")), "recording_id": rec.get("id"),
            })
    return {"release": {
        "id": rel["id"], "group_id": group.get("id"),
        # The release group title is the plain album name, without "(Deluxe Edition)" etc.,
        # the same name fillalbums.py uses.
        "title": group.get("title") or rel.get("title"), "edition": rel.get("title"),
        "artist": album_artist, "year": year(group.get("first_release_date") or rel.get("date")),
        "type": group.get("primary_type"), "discs": len(media),
        "disc_tracks": [len(m.get("tracks") or []) for m in media],
    }, "tracks": out}


COMMANDS = {"albums": search_albums, "songs": search_songs, "artists": search_artists,
            "discography": discography, "editions": editions, "tracks": tracks}


def main():
    with_artist = {"albums", "songs"}
    if not (len(sys.argv) == 3 or (len(sys.argv) == 4 and sys.argv[1] in with_artist))             or sys.argv[1] not in COMMANDS:
        print(json.dumps({"error": "usage: mbsearch.py " + "|".join(COMMANDS) + ' <text or id> ["<artist>"]'}))
        sys.exit(1)
    config.read()
    try:
        result = COMMANDS[sys.argv[1]](MusicBrainzAPI(), *sys.argv[2:])
    except Exception as e:
        print(json.dumps({"error": f"MusicBrainz lookup failed: {' '.join(str(e).split())[:300]}"}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
