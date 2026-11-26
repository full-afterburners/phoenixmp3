# Writes what the user picked in a search (mbsearch.py + findtracks.py) into the
# downloaded files before beets sees them. Runs after cleantitles.py.
#  - Every song gets its MusicBrainz recording ID, title and artist, so beets
#    matches that exact recording (beets tries a file's own MusicBrainz ID first)
#    instead of guessing from YouTube's title and a fingerprint.
#  - Album downloads also get the album, album artist, track and disc numbers,
#    year and release IDs. The album is the plain album name (no "(Deluxe
#    Edition)"), the same name fillalbums.py uses, so fillalbums keeps it.
# Files are found by the YouTube ID yt-dlp puts in the file name ("Title [id].mp3").
# Files not in the search are left alone.
#
#   python presettags.py <search-run.json> <download folder>
#
# search-run.json: {"release": <"release" from mbsearch.py tracks, or null>,
#                   "tracks": [{"video_id", "recording_id", "title", "artist",
#                               "disc", "n"}]}   (disc/n only for albums)
import json
import re
import sys
from collections import Counter
from pathlib import Path

from mediafile import MediaFile

ID_IN_NAME = re.compile(r"\[([A-Za-z0-9_-]{11})\]\.[^.]+$")


def disc_size(release: dict, disc: int | None, downloaded: Counter) -> int:
    """Tracks on that disc of the whole album, not just the ones being downloaded."""
    sizes = release.get("disc_tracks") or []
    if disc and 0 < disc <= len(sizes):
        return sizes[disc - 1]
    return downloaded[disc]


def preset(path: Path, track: dict, release: dict | None, disc_sizes: Counter) -> None:
    f = MediaFile(str(path), id3v23=True)   # MP3s stay ID3v2.3 for old iPods
    f.title = track["title"]
    f.artist = track["artist"]
    f.mb_trackid = track["recording_id"]
    if release:
        f.album = release["title"]
        f.albumartist = release["artist"]
        f.mb_albumid = release["id"]
        f.mb_releasegroupid = release.get("group_id")
        if release.get("year"):
            f.year = release["year"]
        f.track = track.get("n")
        f.tracktotal = disc_size(release, track.get("disc"), disc_sizes)
        f.disc = track.get("disc")
        f.disctotal = release.get("discs")
    f.save()


def main():
    run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    folder = Path(sys.argv[2])
    release = run.get("release")
    by_video = {t["video_id"]: t for t in run["tracks"] if t.get("video_id")}
    disc_sizes = Counter(t.get("disc") for t in run["tracks"])
    done = set()
    for path in sorted(folder.iterdir()):
        m = ID_IN_NAME.search(path.name)
        track = by_video.get(m.group(1)) if m else None
        if not track:
            continue
        try:
            preset(path, track, release, disc_sizes)
        except Exception as e:
            print(f"  preset: couldn't write tags ({e}) - {path.name}", flush=True)
            continue
        done.add(m.group(1))
        print(f"  preset: {track['artist']} - {track['title']}", flush=True)
    for vid, track in by_video.items():
        if vid not in done:
            # Usually the download itself failed, which yt-dlp already reported.
            print(f"  preset: no download for {track['artist']} - {track['title']}", flush=True)


if __name__ == "__main__":
    main()
