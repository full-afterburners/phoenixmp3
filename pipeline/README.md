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
6. `fixartists.py` tidies the artist of songs that kept YouTube's tags. YouTube
   Music credits songwriters as artists too, so only the first name is kept, and
   fan uploads titled "Artist - Title" get split into artist and title.
7. `fillalbums.py` looks up each matched song on MusicBrainz. If YouTube didn't give
   an album it uses the earliest official studio album (or the single/EP), and it
   always sets the real release year, since YouTube's year is the upload date.
8. `fillgenres.py` gives each song one broad genre from Last.fm, from the short
   list in `genres.txt` (the mapping is in the `lastgenre` part of `config.yaml`).
   `source` picks song, album or artist tags first; `family: sound` regroups
   sub-genres by name and `moves` re-parents chosen genres (see the comments in
   `config.yaml` and `fillgenres.py`). Each song's Last.fm tags are kept in the
   library (`lastfm_*` fields), so `fillgenres.py <run-config> --refile
   [--since <unix time>]` can re-file genres with new settings without asking
   Last.fm again. Genre settings saved in the new window go to `my-genres.txt`
   and `my-genres.yaml` (personal, so updates don't overwrite them), which win
   over `genres.txt` and `config.yaml`. `--preview` counts exactly which songs
   new settings would move (kept tags only, changes nothing), and `--rules <folder>`
   reads those two files from another folder. YouTube's category ("Music") is blanked when downloading. Songs Last.fm doesn't
   know get no genre and are tried again next run.
9. With playlist files turned on, `writeplaylists.py` writes an .m3u8 for each
   saved playlist into `Playlists\` in the output folder.

The app shows a short summary. Everything each step prints goes to `last-run.log`.

### Downloading by search (backend only, not in the app yet)

Albums and songs can be found by name instead of a YouTube link:

1. `mbsearch.py albums|songs "<text>" ["<artist>"]` searches MusicBrainz
   (`artists` + `discography` browse an artist; `editions` lists an album's
   releases with a suggested standard edition; `tracks <release id>` gives the
   track list). Results are JSON; the user picks one, since another artist's
   album with the same name can rank first.
2. `findtracks.py <tracks.json>` finds each track on YouTube Music: from the
   official album playlist where its positions line up, otherwise one by one with
   `findcopy.py`'s rules (same artist, same version, length within 10 s). It
   prints `found` / `none` / `error` per track.
3. The found video IDs are downloaded like pasted links. After `cleantitles.py`,
   `presettags.py <search-run.json> newdownloads` writes the picked recording's
   MusicBrainz ID (and for albums the album, track numbers, year and release
   IDs) into the files, so beets matches that exact recording and `fillalbums.py`
   keeps the album.

Downloading too much at once gets you blocked by YouTube for a while (HTTP 403).
The "pause between songs" setting spaces downloads out to avoid that, and if
YouTube refuses 3 songs in a row the app stops downloading and tags what it got.
Stop (or closing the window) ends a run, and anything that finished downloading
gets tagged on the next run.

## The new window (work in progress)

`app.pyw` is the start of a new Apple Music-style window. `gui.pyw` stays the
app until the new one can do everything. Run it with `.\python\pythonw.exe app.pyw`.

- It's a web page, `ui/index.html`, shown in a native window by pywebview
  (in `libs/`), which uses Microsoft Edge WebView2. WebView2 is built into
  Windows 10/11.
- `app.pyw` serves `ui/` on 127.0.0.1:47613. The port stays fixed because the
  page's saved preferences belong to its address.
- The page asks Python for data through `window.pywebview.api`.
  `appdata.py` reads `library.db` read-only (sqlite `mode=ro`) and supplies:
  - the library and album covers,
  - the genre tree, with which songs fall under each genre,
  - Last.fm's popular albums per genre, fetched when a genre is opened.
- Cover thumbnails, Last.fm answers and the WebView2 profile are cached in `cache/`.
- Genre changes are saved for real (`genrerules.py`). Saving shows the exact
  number of songs that would move, and "Also re-file my library" runs
  `fillgenres.py --refile` in the background, with its progress in the status
  capsule.
- Get Music searches MusicBrainz live (`musicinfo.py`, using `mbsearch.py`).
  Last.fm is asked first, because its search is ordered by listeners and
  MusicBrainz free text can miss the famous song entirely. Clicking an artist
  opens their profile: MusicBrainz facts and releases, a Wikimedia Commons photo,
  and Last.fm bio, tags and top songs. Albums open with their editions and track
  list. Profiles and album pages are cached for a week in `cache/`.
- Downloads and preferences don't work yet. The Classic Window button opens
  `gui.pyw` for those, and `phoenixmp3.exe` opens this window (`--classic` for
  the old one).

The same page also runs outside the app as a design mockup. Without
`?desktop` it loads `data.json`/`genres.json` snapshots and `demo.json`
(sample search results).

`node ui/tests/desktop_test.mjs` opens the real window with WebView2's
DevTools port and checks it end to end.

## Files

Top folder:

- `phoenixmp3.exe` starts the app with the bundled Python. It's built from
  `pipeline\launcher.cs` by `build-release.ps1`, so it isn't in git.
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
- `replaced.txt` lists other uploads found for those songs. Labels sometimes
  pull an upload, and YouTube Music quietly plays a different one while the
  playlist still points at the old one. Update My Playlists searches YouTube Music
  (`findcopy.py`) and only takes a result by the same artist, the same version
  and within 10 seconds of the original length. A `-` means nothing matched.
  Delete a line to search for that song again.
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
