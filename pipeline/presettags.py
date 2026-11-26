# Writes what the user picked in a search (mbsearch.py + findtracks.py) into the
# downloaded files before beets sees them. Runs after cleantitles.py.
#  - Every song gets its MusicBrainz recording ID, title and artist, so beets
#    matches that exact recording (beets tries a file's own MusicBrainz ID first)
#    instead of guessing from YouTube's title and a fingerprint.
#  - Album downloads also get the album, album artist, track and disc numbers,
#    year and release IDs. The album is the plain album name (no "(Deluxe
#    Edition)"), the same name fillalbums.py uses, so fillalbums keeps it.
# Files are found by the YouTube ID yt-dlp puts in the file name ("Title [id].mp3").
# Files not in the search are left alone. If a file can't be written (locked,
# read-only) it says so and exits with an error at the end; beets then tags that
# song the usual way.
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
AUDIO = {".mp3", ".m4a", ".opus", ".ogg", ".flac"}   # not cover art or half-finished downloads


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


def tracks_by_video(tracks: list) -> dict:
    """{video id: track}. Two tracks found as the same video can't both be tagged
    onto one file, so neither is (the song still gets tagged by beets as usual)."""
    by_video, dupes = {}, set()
    for t in tracks:
        vid = t.get("video_id")
        if not vid:
            continue
        if vid in by_video:
            dupes.add(vid)
        by_video[vid] = t
    for vid in dupes:
        print(f"  preset: skipped (the same video {vid} was found for two tracks) - {by_video.pop(vid)['title']}",
              flush=True)
    return by_video


def main():
    run = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    folder = Path(sys.argv[2])
    release = run.get("release")
    by_video = tracks_by_video(run["tracks"])
    disc_sizes = Counter(t.get("disc") for t in run["tracks"])
    done, failed = set(), 0
    for path in sorted(folder.iterdir()):
        m = ID_IN_NAME.search(path.name)
        track = by_video.get(m.group(1)) if m and path.suffix.lower() in AUDIO else None
        if not track:
            continue
        done.add(m.group(1))   # it was downloaded, whether or not the tags can be written
        try:
            preset(path, track, release, disc_sizes)
        except Exception as e:
            failed += 1
            print(f"  preset: couldn't write tags ({e}) - {path.name}", flush=True)
            continue
        print(f"  preset: {track['artist']} - {track['title']}", flush=True)
    for vid, track in by_video.items():
        if vid not in done:
            # Usually the download itself failed, which yt-dlp already reported.
            print(f"  preset: no download for {track['artist']} - {track['title']}", flush=True)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
