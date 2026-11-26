# Runs after the beets import. For each song beets matched on MusicBrainz:
#  - if YouTube didn't give it an album, use the earliest official studio album
#    by the same artist (no compilations, live albums or soundtracks), or the
#    single/EP if it isn't on an album
#  - if YouTube filed it under its single but it's also on a studio album by
#    the same artist that came out within a year of the single, use the album.
#    Remixes and other versions keep their single, and later greatest-hits
#    albums don't count.
#  - set the year to the real release year, since YouTube's is the upload date
# Each song is only looked up once.
#
#   python fillalbums.py [run-config.yaml]
import re
import sys
import unicodedata

from beets import config
from beets.library import Library
# beets' client handles MusicBrainz's rate limit and retries.
from beetsplug._utils.musicbrainz import MusicBrainzAPI

ALLOW_SINGLES = True
DONE_FLAG = "mbfill_checked"
SINGLE_FLAG = "single_checked"
VERSION = re.compile(r"[\(\[][^\)\]]*\b(?:remix|mix|edit|version|live|acoustic|instrumental|demo)\b", re.I)


def year_of(date):
    y = (date or "")[:4]
    return int(y) if y.isdigit() else 0


def norm(title):
    """Album title for loose comparison, ignoring bracketed bits and punctuation."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "").lower()
    return re.sub(r"[^0-9a-z]+", "", t)


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


def album_year(rec, album):
    want = norm(album)
    if not want:
        return 0
    years = [year_of(rg.get("first_release_date") or rel_date)
             for _, rg, rel_date, _ in release_groups(rec)
             if norm(rg.get("title")) == want]
    years = [y for y in years if y]
    return min(years) if years else 0


def main():
    config.read()
    if len(sys.argv) > 1:
        config.set_file(sys.argv[1])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    todo = [i for i in lib.items() if i.mb_trackid and not (i.get(DONE_FLAG) and i.get(SINGLE_FLAG))]
    api = MusicBrainzAPI() if todo else None
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
            year = album_year(rec, item.album) or year_of(rec.get("first_release_date"))
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
        try:
            item.move()  # a new album means a new folder with the folder layout
        except Exception as e:
            print(f"  album: couldn't move file ({e}) - {name}", flush=True)
        item.store()
        print(f"  album: {name}: {', '.join(notes) or 'nothing to change'}", flush=True)


if __name__ == "__main__":
    main()
