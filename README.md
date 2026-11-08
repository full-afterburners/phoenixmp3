#  phoenixmp3

Turn YouTube and YouTube Music links into properly tagged music files for your
iPod, MP3 player, car stereo or phone. It handles single songs, whole albums and
playlists, and fills in artist, album, year and square cover art automatically.

**Windows 10/11 only. Nothing to install.**

---

## Download

1. On this page, find **Releases** on the right-hand side and click the latest one.
2. Under **Assets**, download the big **`.zip`** file (about 200 MB).
   >  Not "Code → Download ZIP" or "Source code". Those are missing the parts the app needs.

## Set it up (once)

1. **Unblock the zip:** right-click the downloaded `.zip` → **Properties** → tick
   **Unblock** (bottom right) → **OK**. This stops Windows from nagging about every file.
2. **Extract it:** right-click → **Extract All…** and pick a folder to keep it in,
   e.g. your Music folder or Desktop. Don't run it from inside the zip.
3. Open the extracted folder and double-click **`START HERE.bat`**.
   - If a blue **"Windows protected your PC"** box appears: click **More info** →
     **Run anyway**. It's because the app isn't from a big company, not because
     anything is wrong.
4. The app opens, and a **shortcut with the app's icon** appears in the same folder.
   Use that shortcut from now on (you can copy it to your Desktop).

## Use it

**One link:** paste a YouTube or YouTube Music link (a song, an album, or a playlist),
click **Preview** to see what's in it, then ** Download**.

**Playlists you keep adding to:** click **List of checked playlists**, paste one
playlist link per line, save the file. From then on, ** Check playlists** downloads
only the songs you don't have yet. Playlists must be **public or unlisted** on YouTube.

Your songs end up in the **`downloadswithmeta`** folder (you can change this in
Settings). Copy them to your device the way you normally would.

## Settings for your device ( Settings)

| Your device | Format | Folder layout | Also turn on |
|---|---|---|---|
| iPod | AAC (.m4a) | One folder | |
| Phone, or PC music player | AAC (.m4a) | either | Playlist files |
| Car stereo / USB stick | **MP3** | **Artist\Album folders** | Smaller cover art |
| Cheap MP3 player | **MP3** | **Artist\Album folders** | Smaller cover art |
| Rockbox player | AAC or MP3 | Artist\Album folders | Playlist files, Volume levelling |

If you're not sure: **MP3** plays on literally everything. Settings are remembered
and apply to new downloads.

## If something goes wrong

- **Downloads suddenly stop working:** YouTube changes things every so often. Get the
  newest version from the Releases page (see "Updating" below).
- **"Unavailable" songs:** some songs are blocked in your country or removed by the
  label. The app skips them automatically.
- **Something else:** the full details of the last run are in `pipeline\last-run.log`.
  Send that file to whoever gave you the app.

## Updating

Download and extract the new zip as above, then move these from your old folder into
the new one: the **`downloadswithmeta`** folder (your music), **`playlists.txt`**, and
**`pipeline\downloaded.txt`** (so it remembers what you already have).

---

Please only download music you have the right to, for your own personal use.
This app bundles free open-source software (yt-dlp, beets, FFmpeg, Python and more).
See `THIRD-PARTY-NOTICES.txt` in the download.
