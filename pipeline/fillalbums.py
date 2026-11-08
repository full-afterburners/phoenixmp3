# Runs after the beets import. For each song beets matched on MusicBrainz:
#  - if YouTube didn't give it an album, use the earliest official studio album
#    it's on (no compilations, live albums or soundtracks), or the single/EP if
#    it isn't on an album
#  - set the year to the real release year, since YouTube's is the upload date
# Each song is only looked up once.
#
#   python fillalbums.py [run-config.yaml]
import re
import sys

from beets import config
from beets.library import Library
# beets' client handles MusicBrainz's rate limit and retries.
from beetsplug._utils.musicbrainz import MusicBrainzAPI

ALLOW_SINGLES = True
DONE_FLAG = "mbfill_checked"


def year_of(date):
    y = (date or "")[:4]
    return int(y) if y.isdigit() else 0


def norm(title):
    """Album title for loose comparison, ignoring bracketed bits and punctuation."""
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", title or "").lower()
    return re.sub(r"[^0-9a-z]+", "", t)


def release_groups(rec):
    seen = {}
    for rel in rec.get("releases") or []:
        rg = rel.get("release_group") or {}
        key = rg.get("id") or rel.get("id")
        if key and key not in seen:
            seen[key] = (rel.get("status"), rg, rel.get("date"))
    return list(seen.values())


def pick_album(rec):
    wanted = ["Album"] + (["Single", "EP"] if ALLOW_SINGLES else [])
    best = None
    for status, rg, rel_date in release_groups(rec):
        ptype = rg.get("primary_type")
        if status != "Official" or rg.get("secondary_types") or ptype not in wanted:
            continue
        date = rg.get("first_release_date") or rel_date or "9999"
        key = (wanted.index(ptype), date)
        if best is None or key < best[0]:
            # The release group title is the plain album name, without "(Deluxe Edition)" etc.
            best = (key, rg.get("title"), date)
    return (best[1], best[2]) if best else None


def album_year(rec, album):
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
    if len(sys.argv) > 1:
        config.set_file(sys.argv[1])
    lib = Library(config["library"].as_filename(), config["directory"].as_filename())
    todo = [i for i in lib.items() if i.mb_trackid and not i.get(DONE_FLAG)]
    api = MusicBrainzAPI() if todo else None
    for item in todo:
        name = f"{item.artist} - {item.title}"
        try:
            rec = api.get_base_recording_with_releases(item.mb_trackid, includes=["release-groups"])
        except Exception as e:
            print(f"  album: lookup failed ({e}) - {name}", flush=True)
            continue

        notes = []
        if not item.album:
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
        item.month = item.day = 0

        item[DONE_FLAG] = 1
        item.try_write()
        try:
            item.move()  # a new album means a new folder with the folder layout
        except Exception as e:
            print(f"  album: couldn't move file ({e}) - {name}", flush=True)
        item.store()
        print(f"  album: {name}: {', '.join(notes) or 'nothing to change'}", flush=True)


if __name__ == "__main__":
    main()
