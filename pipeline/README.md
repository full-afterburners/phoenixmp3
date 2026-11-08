# 🎵 phoenixmp3

Paste a YouTube link → get tagged AAC (.m4a) songs ready for an iPod Nano.

This folder is a self-contained pipeline: **yt-dlp** downloads the audio, **beets**
tags it against the MusicBrainz database, and the finished songs land in
`downloadswithmeta\`. Everything needed (ffmpeg, beets, etc.) lives inside this
folder — nothing is installed system-wide, so the whole app folder can be
zipped up and moved to another PC.

---

## How to use it

### The easy way (app window)
1. Double-click the **`phoenixmp3`** shortcut in the app's top folder (the first time,
   use **`START HERE.bat`**, which creates that shortcut).
2. Paste a YouTube link into the box.
3. Click **Preview** to see what the link points to (single track, or a playlist
   with its track list).
4. Click **⬇ Download** and wait. A popup tells you when it's finished.
5. Copy the new songs from **`downloadswithmeta`** to your iPod with your sync tool
   (iOpenPod, or iTunes).

### Saved playlists (grab just the new songs)
1. Click **List of checked playlists** in the app. It opens `playlists.txt` (in the app's top folder).
2. Paste one playlist link per line and save. Lines starting with `#` are labels/notes.
   Playlists must be **public or unlisted** (private ones need a login).
3. Whenever you like, click **🔄 Check playlists**. The app shows how many new songs
   each playlist has, then downloads only those. If nothing's new it just says
   "All playlists up to date".

`playlists.txt` is an ordinary file, so it's kept between uses. The app never
overwrites it.

**Unavailable songs.** Some songs still show in a playlist even though YouTube won't
serve them (blocked in your country, removed by the label, etc.). The first time one
fails, it's written to `pipeline\unavailable.txt` and after that it's shown as
"unavailable" (✗ in Preview) instead of "new". To retry them all, delete that file.

### Settings (⚙ button)
Saved in `pipeline\settings.json`, so they stick between uses. They apply to new
downloads only.

| Setting | Options |
|---|---|
| Audio format | **AAC (.m4a)**: YouTube's audio kept as-is, best quality (iPods, phones, most players) · **MP3** (~190 kbps): plays on everything (car stereos, cheap MP3 players) |
| Folder layout | **One folder** (`Artist - Title`) · **Artist\Album folders**, for players that browse by folder |
| Save songs to | any folder (default `downloadswithmeta`). Changing it doesn't move existing songs |
| Cover art | square-crop on/off · smaller 300 px art for players that struggle with big images |
| Playlist files | on **Check playlists**, writes `Playlists\<name>.m3u8` per saved playlist inside the output folder, in YouTube's order. Paths are relative, so it works on any player/SD card |
| Volume levelling | ReplayGain tags + iTunes Sound Check data. Only tags, the audio isn't changed. Players that read ReplayGain (Rockbox, foobar2000, many phone apps) use it directly; an iPod only if your sync tool copies the Sound Check value to it. Tagging gets a bit slower |

The command-line script below ignores these settings and always uses the defaults.

### The command-line way (optional)
Open the `pipeline` folder in File Explorer, type `powershell` in the address bar, Enter, then:

```powershell
.\get-music.ps1 "https://www.youtube.com/playlist?list=XXXXXXXX"
```

Keep the quotes — playlist links contain characters that break the command otherwise.

Both the app and the script do exactly the same thing and share the same settings.

---

## What you get

- Every track is saved as its **own AAC file** (`.m4a`), named `Artist - Title.m4a`.
  This is YouTube's own ~128 kbps AAC stream kept **exactly as-is**: no re-encoding,
  so no quality is lost, and AAC plays natively on the iPod. (If a video has no AAC
  stream, its best audio is converted to AAC 160 kbps instead.)
- Older songs downloaded before the switch are MP3s with ID3v2.3 tags. Both kinds
  live side by side in the library and the iPod plays both.
- Cover art = the YouTube thumbnail, centre-cropped to a square and shrunk to
  500×500 (YouTube's are 16:9, the iPod shows art square), embedded in each file.
- Metadata is matched by **acoustic fingerprint**, so it works even when the YouTube
  title is messy. If a track isn't in the database, it's kept with YouTube's own tags
  (nothing is ever dropped).

---

## Folder layout

At the top level: the shortcut, your library, your saved playlists, and `pipeline\`
(everything else).

```
phoenixmp3\
├─ START HERE.bat              ← first start (creates the shortcut below)
├─ phoenixmp3.lnk              ← double-click this (made by the app)
├─ playlists.txt               ← your saved playlist links (created by "List of checked playlists")
├─ downloadswithmeta\          ← YOUR LIBRARY — finished, tagged songs go here
└─ pipeline\                   ← everything else (the engine)
   ├─ README.md                ← you are here
   ├─ get-music.ps1            ← command-line version (same thing)
   ├─ gui.pyw                  ← the app window
   ├─ squareart.py             ← squares + shrinks the cover art (runs automatically)
   ├─ cleantitles.py           ← strips "Artist - " / "(Official Audio)" from titles (automatic)
   ├─ fillalbums.py            ← MusicBrainz year + album when YouTube gave none (automatic)
   ├─ config.yaml              ← tagging settings (ID3v2.3, naming, etc.)
   ├─ library.db               ← beets' record of what's been imported
   ├─ downloaded.txt           ← IDs of every song already downloaded (skipped next time)
   ├─ unavailable.txt          ← songs YouTube refuses to serve (skipped; delete to retry)
   ├─ last-run.log             ← full detailed output of the last run (for troubleshooting)
   ├─ settings.json            ← your app settings (created when you first click Save)
   ├─ run-config.yaml          ← generated from settings.json each run (don't edit)
   ├─ writeplaylists.py        ← writes the .m3u8 playlist files (automatic)
   ├─ prunelib.py              ← forgets songs you deleted, so they can be re-downloaded (automatic)
   ├─ newdownloads\            ← scratch space; empties itself after each run
   ├─ yt-dlp-master\yt_dlp\    ← the YouTube downloader (source)
   ├─ bin\                     ← ffmpeg, ffprobe, deno, fpcalc
   └─ libs\                    ← beets + its dependencies
```

---

## How it works (two stages)

1. **Download** — `yt-dlp` grabs each track's AAC audio into an `.m4a` (no
   conversion), embedding the thumbnail + basic metadata, into `newdownloads\`.
   Then `squareart.py` crops each embedded thumbnail to a 500×500 square, and
   `cleantitles.py` tidies YouTube-style titles: `Caamp - Moonsmoke (Official Audio)`
   becomes `Moonsmoke`. It only removes a repeated artist name and video junk like
   "(Official Video)" or "(Lyrics)". Real version info like "(Live)" or "(Remix)" is kept.
2. **Tag & file** — `beets` fingerprints each song (`fpcalc`), matches MusicBrainz,
   writes clean tags, and **moves** the file into `downloadswithmeta\`.
3. **Album + year from MusicBrainz** — for every song beets matched,
   `fillalbums.py` asks MusicBrainz which releases the song is on, then:
   - **Album** (only if YouTube gave none, typical for official-channel uploads):
     the earliest official **studio album**, never a compilation, live album or
     soundtrack. If it's not on any album, the single/EP it came out on
     (`ALLOW_SINGLES` at the top of `fillalbums.py`). A YouTube-provided album is
     never changed, because YouTube Music knows the exact release you have.
   - **Year** (always): the real release year from MusicBrainz, since YouTube's
     "year" is just the video's upload date. It uses the year of the album the song
     is filed under, or else the song's first release year.

   Songs beets can't match keep YouTube's details for everything.

---

## Notes & troubleshooting

- **Re-running a playlist only grabs the new songs.** Every finished song's
  YouTube ID is written to `pipeline\downloaded.txt`, and anything listed there
  is skipped. Preview marks those songs with ✓. To download a song again,
  delete its line from that file (or delete the file to start fresh).

- **First run needs internet** beyond just the download: yt-dlp fetches a small
  YouTube "challenge solver" component once, then caches it. (You need internet to
  download anyway.)
- **Nothing downloaded / empty result?** Almost always YouTube changed something and
  `yt-dlp` needs updating — see below.
- **Preview takes a few seconds** — it's asking YouTube for the playlist contents.
  The window shows "Loading…" while it works. Normal.
- **A track imported "as-is"** in the log just means it wasn't found in the
  fingerprint database (common for live covers / obscure uploads). It still works —
  it just keeps YouTube's tags instead of MusicBrainz's.

### Updating yt-dlp (do this if downloads start failing)
YouTube changes often, so yt-dlp needs occasional updates:
1. Download the latest source zip from https://github.com/yt-dlp/yt-dlp
2. Replace the `pipeline\yt-dlp-master\yt_dlp\` folder with the new one.

---

## Requirements
- Windows 10/11, 64-bit. Nothing else: Python 3.11.9 is bundled in `pipeline\python\`
  (a plain unpack of python.org's official installer parts, nothing installed),
  along with ffmpeg, deno, fpcalc (`bin\`), beets and its libraries (`libs\`) and yt-dlp.
- The libraries in `libs\` are compiled for **Python 3.11** and won't load in other versions.

## Sharing / GitHub
- The **repository** holds only the code (see `.gitignore`). `bin\`, `libs\`, `python\`
  and `yt-dlp-master\` are too big for a repo (ffmpeg is over GitHub's 100 MB file
  limit), and personal files (music, playlists, history, settings, logs) never go in.
- The **Release zip** holds everything. Build it with:
  ```powershell
  .\build-release.ps1 -Version 1.1
  ```
  It writes `dist\<app-name>-v1.1.zip`, generates `THIRD-PARTY-NOTICES.txt`, and stops
  if it finds any personal file or your Windows user path in the build.
  Upload the zip on GitHub under **Releases → Draft a new release**.
- **Updating yt-dlp for everyone:** replace `yt-dlp-master\yt_dlp\` (see above), build a
  new version, and publish it as a new release.
- New users start with **`START HERE.bat`**. On its first run the app creates a shortcut
  with its icon in the top folder, and recreates it if the folder is ever moved.
