# Runs after the beets import. For each song beets matched on MusicBrainz:
#  - if YouTube didn't give it an album, use the earliest official studio album
#    by the same artist (no compilations, live albums or soundtracks), or the
#    single/EP if it isn't on an album
#  - if YouTube filed it under its single but it's also on a studio album by
#    the same artist that came out within a year of the single, use the album.
#    Remixes and other versions keep their single, and later greatest-hits
#    albums don't count.
#  - set the year to the real release year, since YouTube's is the upload date:
#    the album's first release, looked up by name when beets matched a copy of the
#    recording that MusicBrainz only lists on a later compilation
#  - a song matched to a recording MusicBrainz only has on live albums or bootlegs
#    (and not called "live") gets the studio recording instead, and its album
#  - a song YouTube filed under a compilation or someone else's album (festival
#    and hits collections) moves to the artist's own studio album
#  - playlist files follow any song file that gets moved or renamed
# Each song is only looked up once (older songs get the studio check once too,
# without rewriting files that need no change).
#
#   python fillalbums.py [run-config.yaml]
import os
import re
import sys
import unicodedata
from pathlib import Path

from beets import config
from beets.library import Library
# beets' client handles MusicBrainz's rate limit and retries.
from beetsplug._utils.musicbrainz import MusicBrainzAPI

import writeplaylists   # relink(): playlist files follow moved songs

ALLOW_SINGLES = True
DONE_FLAG = "mbfill_checked"
SINGLE_FLAG = "single_checked"
STUDIO_FLAG = "studio_checked"
VERSION = re.compile(r"[\(\[][^\)\]]*\b(?:remix|mix|edit|version|live|acoustic|instrumental|demo)\b", re.I)
VERSION_NOTE = re.compile(r"\b(?:remix|mix|edit|version|live|acoustic|instrumental|demo|karaoke|cover)\b", re.I)
LIVE_WORD = re.compile(r"\blive\b", re.I)
STUDIO_SEARCH = 50   # MusicBrainz search results checked for the studio recording


def year_of(date):
    y = (date or "")[:4]
    return int(y) if y.isdigit() else 0


def norm(title):
    """Album title for loose comparison, ignoring bracketed bits and punctuation."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "").lower()
    return re.sub(r"[^0-9a-z]+", "", t)


def main_title(title):
    """An album title without its subtitle: "Brother Bear: An Original Disney Records
    Soundtrack" -> "Brother Bear"."""
    return re.split(r"\s*:\s+|\s+[-–—]\s+", title or "", maxsplit=1)[0]


def plain(name):
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def release_groups(rec):
    """(status, release group, release date, credited artists) per release group."""
    seen = {}
    for rel in rec.get("releases") or []:
        rg = rel.get("release_group") or {}
        key = rg.get("id") or rel.get("id")
        if key and key not in seen:
            # Both the name as credited ("Kylie") and the artist's name ("Kylie Minogue").
            names = [n for c in rel.get("artist_credit") or []
                     for n in (c.get("name"), (c.get("artist") or {}).get("name"))]
            seen[key] = (rel.get("status"), rg, rel.get("date"), [n for n in names if n])
    return list(seen.values())


def by_artist(names, artist):
    """True if the release is credited to the song's artist (not "Various Artists")."""
    return not names or plain(artist) in {plain(n) for n in names}


def studio_albums(rec, artist):
    for status, rg, rel_date, names in release_groups(rec):
        if (status == "Official" and rg.get("primary_type") == "Album"
                and not rg.get("secondary_types") and by_artist(names, artist)):
            yield rg.get("first_release_date") or rel_date or "9999", rg.get("title")


def pick_album(rec, artist):
    wanted = ["Album"] + (["Single", "EP"] if ALLOW_SINGLES else [])
    best = None
    for status, rg, rel_date, names in release_groups(rec):
        ptype = rg.get("primary_type")
        if (status != "Official" or rg.get("secondary_types") or ptype not in wanted
                or not by_artist(names, artist)):
            continue
        date = rg.get("first_release_date") or rel_date or "9999"
        key = (wanted.index(ptype), date)
        if best is None or key < best[0]:
            # The release group title is the plain album name, without "(Deluxe Edition)" etc.
            best = (key, rg.get("title"), date)
    return (best[1], best[2]) if best else None


def album_for_single(rec, item):
    """The studio album to use instead of the single YouTube filed the song under."""
    if VERSION.search(item.title or ""):
        return None
    cur = norm(item.album)
    groups = release_groups(rec)
    types = {rg.get("primary_type") for _, rg, _, _ in groups if norm(rg.get("title")) == cur}
    if not types & {"Single", "EP"} or "Album" in types:
        return None
    single_year = min((year_of(rg.get("first_release_date") or d) for _, rg, d, _ in groups
                       if norm(rg.get("title")) == cur), default=0)
    albums = [(date, title) for date, title in studio_albums(rec, item.artist)
              if not single_year or not year_of(date) or year_of(date) <= single_year + 1]
    return min(albums)[1] if albums else None


def is_live(group) -> bool:
    status, rg, _, _ = group
    return status == "Bootleg" or "Live" in (rg.get("secondary_types") or [])


def live_only(rec) -> bool:
    """Every release this recording is on is a live album or a bootleg."""
    groups = release_groups(rec)
    return bool(groups) and all(is_live(g) for g in groups)


def studio_recording(api, item):
    """The studio recording of a song beets matched to a live-only one: same title by the same
    artist, no version note ("live, ...", "remix"), within 10 s of the file's length, on an official
    studio album (else single/EP) by the artist. Earliest album first. None if there isn't one."""
    found = api.search("recording", {"recording": item.title, "artist": item.artist}, limit=STUDIO_SEARCH)
    best = None
    for r in found:
        if norm(r.get("title")) != norm(item.title) or VERSION_NOTE.search(r.get("disambiguation") or ""):
            continue
        if not r.get("length") or not item.length or abs(r["length"] / 1000 - item.length) > 10:
            continue
        for status, rg, rel_date, names in release_groups(r):
            ptype = rg.get("primary_type")
            if status != "Official" or rg.get("secondary_types") or ptype not in ("Album", "Single", "EP") \
                    or not by_artist(names, item.artist):
                continue
            key = (ptype != "Album", rg.get("first_release_date") or rel_date or "9999")
            if best is None or key < best[0]:
                best = (key, r["id"])
    return best[1] if best else None


def filed_on_compilation(rec, item) -> bool:
    """The song's album is one of its recording's releases, and a compilation or not the artist's own
    (YouTube Music often credits songs to festival or hits collections)."""
    want = norm(item.album)
    filed = [g for g in release_groups(rec) if want and norm(g[1].get("title")) == want]
    return bool(filed) and all("Compilation" in (g[1].get("secondary_types") or []) or not by_artist(g[3], item.artist)
                               for g in filed)


def set_album(item, album) -> None:
    """File the song under another album. The old release's ids no longer apply to it."""
    item.album = album
    item.mb_albumid = item.mb_releasegroupid = ""


def fix_studio(api, rec, item, notes: list):
    """A: a song matched to a live-only recording (and not called live) gets its studio recording,
    and that recording's album when the filed one isn't among its releases. B: a song YouTube filed
    under a compilation or someone else's album moves to the artist's own studio album with it.
    Returns the recording now in use; appends what changed to notes."""
    if live_only(rec) and not LIVE_WORD.search(item.title or "") and not LIVE_WORD.search(item.album or "") \
            and not on_filed_album(rec, item):   # filed under that live album: it really is the live take
        rid = studio_recording(api, item)
        if not rid:
            return rec
        rec = api.get_base_recording_with_releases(rid, includes=["release-groups", "artist-credits"])
        item.mb_trackid = rid
        notes.append("live recording -> studio recording")
        if not item.album or not album_year(rec, item.album):   # the filed album doesn't have this recording
            found = pick_album(rec, item.artist)
            if found and norm(found[0]) != norm(item.album):
                notes.append(f'album "{item.album}" -> "{found[0]}"')
                set_album(item, found[0])
        return rec
    if item.album and not VERSION.search(item.title or "") and filed_on_compilation(rec, item):
        studio = list(studio_albums(rec, item.artist))
        album = min(studio)[1] if studio else None
        if album and norm(album) != norm(item.album):
            notes.append(f'album "{item.album}" -> "{album}"')
            set_album(item, album)
    return rec


def on_filed_album(rec, item) -> bool:
    """The song's album is one of the recording's releases ("At the Blackhawk" matches "At the
    Black Hawk, Volume 5"): the match and the filing agree, so the match is trusted."""
    want = re.sub(r"[^0-9a-z]+", "", plain(item.album))
    if not want:
        return False
    for _, rg, _, _ in release_groups(rec):
        have = re.sub(r"[^0-9a-z]+", "", plain(rg.get("title")))
        if have and (have.startswith(want) or want.startswith(have)):
            return True
    return False


def album_year(rec, album):
    want = norm(album)
    if not want:
        return 0
    years = [year_of(rg.get("first_release_date") or rel_date)
             for _, rg, rel_date, _ in release_groups(rec)
             if norm(rg.get("title")) == want]
    years = [y for y in years if y]
    return min(years) if years else 0


def searched_album_year(api, artist, album):
    """The year the album first came out, found by searching MusicBrainz for the album
    itself. Needed when beets matched a copy of the recording that MusicBrainz only
    lists on a later compilation (MGMT's "Kids" on a 2019 hits collection), so the
    album the song is filed under isn't among that recording's releases."""
    found = api.search("release-group", {"releasegroup": album, "artist": artist}, limit=10)
    want = {norm(album), norm(main_title(album))} - {""}
    years = []
    for rg in found:
        names = [n for c in rg.get("artist_credit") or []
                 for n in (c.get("name"), (c.get("artist") or {}).get("name")) if n]
        titles = {norm(rg.get("title")), norm(main_title(rg.get("title")))}
        if want & titles and names and by_artist(names, artist):
            years.append(year_of(rg.get("first_release_date")))
    years = [y for y in years if y]
    return min(years) if years else 0


def main():
    config.read()
    if len(sys.argv) > 1:
        config.set_file(sys.argv[1])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    todo = [i for i in lib.items() if i.mb_trackid and not (i.get(DONE_FLAG) and i.get(SINGLE_FLAG) and i.get(STUDIO_FLAG))]
    api = MusicBrainzAPI() if todo else None
    moves = {}
    for item in todo:
        name = f"{item.artist} - {item.title}"
        try:
            rec = api.get_base_recording_with_releases(item.mb_trackid,
                                                       includes=["release-groups", "artist-credits"])
        except Exception as e:
            print(f"  album: lookup failed ({e}) - {name}", flush=True)
            continue

        notes = []
        first_time = not item.get(DONE_FLAG)
        older = item.get(DONE_FLAG) and item.get(SINGLE_FLAG)   # only the studio check is new for it
        if not item.get(STUDIO_FLAG):
            try:
                rec = fix_studio(api, rec, item, notes)
            except Exception as e:
                print(f"  album: lookup failed ({e}) - {name}", flush=True)
                continue
        if older and not notes:
            item[STUDIO_FLAG] = 1   # nothing to fix: the file stays as it is
            item.store()
            continue
        if first_time and not item.album:
            found = pick_album(rec, item.artist)
            if found:
                item.album = found[0]
                notes.append(f'album "{item.album}"')
            else:
                notes.append("no album found")
        elif item.album and not item.get(SINGLE_FLAG):
            album = album_for_single(rec, item)
            if album:
                notes.append(f'album "{item.album}" -> "{album}"')
                item.album = album

        if first_time or notes:
            year = album_year(rec, item.album)
            if not year and item.album:
                try:
                    year = searched_album_year(api, item.artist, item.album)
                except Exception as e:
                    print(f"  album: lookup failed ({e}) - {name}", flush=True)
                    continue   # not flagged: tried again next run
            year = year or year_of(rec.get("first_release_date"))
            if year and year != item.year:
                notes.append(f"year {item.year or '?'} -> {year}")
                item.year = year
            item.month = item.day = 0

        if item.album and not item.albumartist:
            item.albumartist = item.artist
        if not item.try_write():
            # Locked (open in a player, syncing) or read-only. Not stored and
            # not flagged, so the library still matches the file and it's
            # looked up again next run.
            print(f"  album: couldn't write tags, will try again next time - {name}", flush=True)
            continue
        item[DONE_FLAG] = 1
        item[SINGLE_FLAG] = 1
        item[STUDIO_FLAG] = 1
        old_path = os.fsdecode(item.path)
        try:
            item.move()  # a new album means a new folder with the folder layout
        except Exception as e:
            print(f"  album: couldn't move file ({e}) - {name}", flush=True)
        if os.fsdecode(item.path) != old_path:
            moves[old_path] = os.fsdecode(item.path)
        item.store()
        print(f"  album: {name}: {', '.join(notes) or 'nothing to change'}", flush=True)
    if moves:   # moved or renamed files: keep every playlist file pointing at them
        n = writeplaylists.relink(moves, Path(config["directory"].as_filename()))
        if n:
            print(f"  album: updated {n} playlist file line{'' if n == 1 else 's'} for moved songs", flush=True)


if __name__ == "__main__":
    main()
