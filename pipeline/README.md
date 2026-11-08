# phoenixmp3 internals

This is the technical side. The README in the top folder covers normal use.

## How a download works

1. yt-dlp downloads the audio into `newdownloads\` with the YouTube thumbnail and
   basic tags embedded. For AAC it keeps YouTube's own ~128 kbps stream without
   re-encoding. For MP3 it converts at LAME V2.
2. `squareart.py` crops the thumbnail to a square (500 px, or 300 with the smaller
   art setting).
3. `cleantitles.py` tidies the tags, e.g. "Caamp - Moonsmoke (Official Audio)"
   becomes "Moonsmoke". It only removes repeated artist names and things like
   "(Official Video)", never version info like "(Live)" or "(Remix)".
4. `prunelib.py` removes library entries for songs that were deleted by hand, so
   beets doesn't skip them as duplicates.
5. beets fingerprints each song with fpcalc, matches it on MusicBrainz, writes the
   tags and moves it into the output folder. Songs it can't match keep YouTube's tags.
6. `fillalbums.py` looks up each matched song on MusicBrainz. If YouTube didn't give
   an album it uses the earliest official studio album (or the single/EP), and it
   always sets the real release year, since YouTube's year is the upload date.
7. With playlist files turned on, `writeplaylists.py` writes an .m3u8 for each
   saved playlist into `Playlists\` in the output folder.

The app shows a short summary. Everything each step prints goes to `last-run.log`.

## Files

Top folder:

- `START HERE.bat` starts the app with the bundled Python. The app then creates
  `phoenixmp3.lnk` next to it, and recreates it if the folder moves.
- `playlists.txt` is the list of saved playlist links.
- `downloadswithmeta\` is the default output folder.

In `pipeline\`:

- `gui.pyw` is the app.
- `get-music.ps1` is a command line version that always uses the default settings.
- `config.yaml` is the beets config. The app writes `run-config.yaml` from its
  settings on every run and passes it to beets on top of this.
- `settings.json` holds the app settings.
- `downloaded.txt` is yt-dlp's list of downloaded video IDs. Anything in it is
  skipped. Delete a line to download that song again.
- `unavailable.txt` lists songs YouTube won't serve, so they don't count as new.
  Delete it to retry them.
- `library.db` is beets' database.
- `python\`, `bin\` (ffmpeg, ffprobe, deno, fpcalc), `libs\` (beets and its
  libraries) and `yt-dlp-master\` are the bundled dependencies. The libraries are
  built for Python 3.11 and won't load in other versions.

## Updating yt-dlp

When downloads start failing it's usually because YouTube changed something.
Download the latest source from https://github.com/yt-dlp/yt-dlp and replace
`yt-dlp-master\yt_dlp\` with the new `yt_dlp` folder.

## Releases

The git repo only has the code. The dependencies are too big for it (ffmpeg is
over GitHub's 100 MB limit) and personal files are excluded in `.gitignore`.

To make a release zip, run this in the `pipeline` folder:

    .\build-release.ps1 -Version 1.1

It writes `dist\phoenixmp3-v1.1.zip` with everything bundled, and stops if any
personal file or your Windows user path ends up in it. Upload the zip as a new
release on GitHub.

If you set this up on a new PC, clone the repo and copy `python\`, `bin\`, `libs\`
and `yt-dlp-master\` from a release zip into `pipeline\`.
