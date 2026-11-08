"""
fillalbums.py - fill in album + correct year for songs beets matched on MusicBrainz.

Runs after the beets import, on every song beets matched (has a MusicBrainz
recording ID) that hasn't been processed yet. One MusicBrainz lookup per song
lists every release the recording is on, and from that:

ALBUM - only when YouTube didn't provide one (a YouTube album is never touched,
  since YouTube Music knows the exact release your file came from):
  picks the earliest official studio album (release-group type "Album" with no
  secondary type, so compilations, live albums, soundtracks are skipped). If the
  song isn't on any studio album, the single/EP it came out on (ALLOW_SINGLES).

YEAR - always from MusicBrainz, because YouTube's "year" is the video's upload
  date, not the release date. In order of preference:
    1. the year of the album the song is filed under (YouTube's or the one
       picked above), if that album is among the recording's releases
    2. otherwise the recording's first release year
  If MusicBrainz has no date at all, the existing year is kept.

Each song is looked up once; it's then marked so later runs skip it.
Needs BEETSDIR set and libs\\ on PYTHONPATH (the app/script do both).
"""
import re
import sys

from beets import config
from beets.library import Library
# beets' own MusicBrainz client: stays under MB's 1 request/sec limit and
# retries automatically when MB answers "503 slow down".
from beetsplug._utils.musicbrainz import MusicBrainzAPI

# Also accept a single/EP as the album when there's no studio album.
ALLOW_SINGLES = True
DONE_FLAG = "mbfill_checked"


def year_of(date):
    y = (date or "")[:4]
    return int(y) if y.isdigit() else 0


def norm(title):
    """Album title for loose comparison: no (Deluxe...) / [Remaster], no punctuation."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "").lower()
    return re.sub(r"[^0-9a-z]+", "", t)


def release_groups(rec):
    """Unique release groups (with their status) the recording appears on."""
    seen = {}
    for rel in rec.get("releases") or []:
        rg = rel.get("release_group") or {}
        key = rg.get("id") or rel.get("id")
        if key and key not in seen:
            seen[key] = (rel.get("status"), rg, rel.get("date"))
    return list(seen.values())


def pick_album(rec):
    """(title, date) of the earliest official studio album (or single/EP), or None."""
    wanted = ["Album"] + (["Single", "EP"] if ALLOW_SINGLES else [])
    best = None
    for status, rg, rel_date in release_groups(rec):
        ptype = rg.get("primary_type")
        if status != "Official" or rg.get("secondary_types") or ptype not in wanted:
            continue
        date = rg.get("first_release_date") or rel_date or "9999"
        key = (wanted.index(ptype), date)
        if best is None or key < best[0]:
            # Release-group title = the plain album name, not "(Deluxe Edition)".
            best = (key, rg.get("title"), date)
    return (best[1], best[2]) if best else None


def album_year(rec, album):
    """Release year of the named album, if it's one of the recording's releases."""
    want = norm(album)
    if not want:
        return 0
    years = [year_of(rg.get("first_release_date") or rel_date)
             for _, rg, rel_date in release_groups(rec)
             if norm(rg.get("title")) == want]
    years = [y for y in years if y]
    return min(years) if years else 0


def main():
    config.read()
    if len(sys.argv) > 1:  # the app's settings overlay (output folder, layout)
        config.set_file(sys.argv[1])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    todo = [i for i in lib.items() if i.mb_trackid and not i.get(DONE_FLAG)]
    api = MusicBrainzAPI() if todo else None
    for item in todo:
        name = f"{item.artist} - {item.title}"
        try:
            rec = api.get_base_recording_with_releases(
                item.mb_trackid, includes=["release-groups"])
        except Exception as e:  # noqa: BLE001 - still failing after retries: try next run
            print(f"  album: lookup failed ({e}) - {name}", flush=True)
            continue

        notes = []
        if not item.album:  # YouTube gave no album: fill one in
            found = pick_album(rec)
            if found:
                item.album = found[0]
                if not item.albumartist:
                    item.albumartist = item.artist
                notes.append(f'album "{item.album}"')
            else:
                notes.append("no album found")

        year = album_year(rec, item.album) or year_of(rec.get("first_release_date"))
        if year and year != item.year:
            notes.append(f"year {item.year or '?'} -> {year}")
            item.year = year
        # Clear month/day left over from YouTube's upload date.
        item.month = item.day = 0

        item[DONE_FLAG] = 1
        item.try_write()
        try:  # with the Artist\Album folder layout, a new album means a new folder
            item.move()
        except Exception as e:  # noqa: BLE001 - tags are written; location is cosmetic
            print(f"  album: couldn't move file ({e}) - {name}", flush=True)
        item.store()
        print(f"  album: {name}: {', '.join(notes) or 'nothing to change'}", flush=True)


if __name__ == "__main__":
    main()
