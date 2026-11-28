# Finds the tracks of a MusicBrainz album or song (from mbsearch.py) on YouTube
# Music, so they can be downloaded without pasting links.
#
#   python findtracks.py <tracks.json> [--reupload]
#
# tracks.json is the output of "mbsearch.py tracks" (an album: {"release", "tracks"})
# or a list of songs: {"release": null, "tracks": [{"recording_id", "title",
# "artist", "length"}]}.
#  - For an album it first looks for YouTube Music's official album playlist and
#    takes each track from it whose title, version, artist and length (within
#    10 s) match the same position on the MusicBrainz track list. Playlist
#    tracks aren't checked for being downloadable; the download reports those.
#  - Every other track is searched for on its own with findcopy.py's rules: the
#    same artist, the same version (a remix or live take doesn't match the
#    original) and within 10 seconds of the real length.
# Prints one line per track, in track list order:
#   found <recording id> <video id> <artist> - <title>
#   unsure <recording id> <video id> <artist> - <title>   MusicBrainz has no length
#                                    for it, so only title and artist matched: ask
#   none <recording id>              searched fine, nothing matched
#   reupload <recording id> <video id> <artist> - <title>   every official upload is
#                                    age-restricted (needs a sign-in); this is someone
#                                    else's upload of it, within 2 s of the real length
#   agegated <recording id>          age-restricted and no exact re-upload: permanent
#   error <recording id> <reason>    blocked, offline, yt-dlp broken: try again later
#  - --reupload: the app's tracks.json names album-playlist tracks that turned out
#    age-restricted when downloading ("skip": that video id); only re-uploads are
#    searched for them.
# and before them, when an album playlist was used:
#   playlist <playlist id> <matched>/<total>
import json
import sys
import time
import urllib.parse
from pathlib import Path

import findcopy   # also puts the bundled yt-dlp on the path
import yt_dlp

PAUSE = 1.5       # seconds between YouTube searches, like findcopy.py
ALBUM_RESULTS = 3  # album search results checked for the official playlist


VARIOUS = "various artists"


def artist_names(track: dict, release: dict | None = None) -> list[str]:
    return track.get("artist_names") or (release or {}).get("artist_names") or [track["artist"]]


def matches(track: dict, entry: dict, release: dict) -> bool:
    """A playlist entry is this track: same title, same version, the same artist
    (so a tribute album with the same songs doesn't count) and about the same length.
    Tracks without a known length are never taken from a playlist."""
    title = entry.get("title") or ""
    if not findcopy.same_title(track["title"], title):
        return False
    if findcopy.versions(title) != findcopy.versions(track["title"]):
        return False
    channel = entry.get("channel") or entry.get("uploader") or ""
    wanted = artist_names(track, release) + [n for n in release.get("artist_names") or []
                                             if n.lower() != VARIOUS]
    if not any(findcopy.same_artist(n, findcopy.clean_artist(channel)) for n in wanted):
        return False
    length, dur = track.get("length"), entry.get("duration")
    return bool(length and dur) and abs(dur - length) <= 10


def album_playlist(ydl, release: dict, tracks: list) -> tuple[str | None, dict]:
    """The official YouTube Music album playlist that best lines up with the track
    list, and {track index: entry} for the positions that match."""
    q = urllib.parse.quote(f"{findcopy.clean_artist(release['artist'])} {release['title']}")
    res = ydl.extract_info(f"https://music.youtube.com/search?q={q}#albums", download=False)
    best_id, best = None, {}
    for result in list((res or {}).get("entries") or [])[:ALBUM_RESULTS]:
        if not result or not result.get("url"):
            continue
        time.sleep(PAUSE)
        album = ydl.extract_info(result["url"], download=False)   # album page -> its playlist
        entries = list((album or {}).get("entries") or [])
        hits = {i: e for i, (t, e) in enumerate(zip(tracks, entries)) if e and matches(t, e, release)}
        if len(hits) > len(best):
            best_id, best = album.get("id"), hits
        if len(best) == len(tracks):
            break
    # A playlist where most positions don't line up is a different album or edition.
    if len(best) * 2 < len(tracks):
        return None, {}
    return best_id, best


def main():
    reupload_only = "--reupload" in sys.argv[2:]
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    release, tracks = data.get("release"), data["tracks"]
    flat = yt_dlp.YoutubeDL({**findcopy.OPTS, "extract_flat": True})
    pages = yt_dlp.YoutubeDL({**findcopy.OPTS, "extract_flat": "in_playlist"})
    full = yt_dlp.YoutubeDL({**findcopy.OPTS, "simulate": True, "format": "bestaudio"})

    from_playlist = {}
    if release and len(tracks) > 1 and not reupload_only:
        try:
            playlist_id, from_playlist = album_playlist(pages, release, tracks)
            if playlist_id:
                print(f"playlist {playlist_id} {len(from_playlist)}/{len(tracks)}", flush=True)
        except Exception as e:
            # Not fatal: every track is searched for on its own instead.
            print(f"note: couldn't search for the album playlist ({' '.join(str(e).split())[:150]})", flush=True)

    searched = 0
    for i, track in enumerate(tracks):
        key = track.get("recording_id") or str(i + 1)
        entry = from_playlist.get(i)
        if entry:
            artist = findcopy.clean_artist(entry.get("channel") or entry.get("uploader") or track["artist"])
            print(f"found {key} {entry['id']} {artist} - {entry.get('title')}", flush=True)
            continue
        if searched:
            time.sleep(PAUSE)
        searched += 1
        song = {"id": track.get("skip"), "title": track["title"], "artist": track["artist"], "duration": track.get("length")}
        try:
            if reupload_only:
                raise findcopy.AgeGated("the album's upload is age-restricted")
            info = findcopy.find(flat, full, song)
        except findcopy.AgeGated:
            try:
                info = findcopy.find_reupload(flat, full, song, artist_names(track, release))
            except Exception as e:
                reason = " ".join(str(e).split())[:200] or type(e).__name__
                print(f"error {key} {reason}", flush=True)
                continue
            if info:
                artist = info.get("artist") or findcopy.clean_artist(info.get("channel"))
                print(f"reupload {key} {info['id']} {artist} - {info.get('title')}", flush=True)
            else:
                print(f"agegated {key}", flush=True)
            continue
        except Exception as e:
            reason = " ".join(str(e).split())[:200] or type(e).__name__
            print(f"error {key} {reason}", flush=True)
            continue
        if info:
            artist = info.get("artist") or findcopy.clean_artist(info.get("channel"))
            # Without a length only the title and artist were checked, which can
            # pick "Home Sweet Home" for "Home": let the user confirm it.
            word = "found" if track.get("length") else "unsure"
            print(f"{word} {key} {info['id']} {artist} - {info.get('title')}", flush=True)
        else:
            print(f"none {key}", flush=True)


if __name__ == "__main__":
    main()
