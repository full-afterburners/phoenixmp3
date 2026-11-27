
#  phoenixmp3

Takes youtube playlists, albums or singles and downloades the audio. Tags them with metadata from musicbraniz so they are ready to use on any device.


## Getting it

Go to the Releases section on the right side of this page, open the newest one, and
download the big zip file under Assets (around 150 MB). Don't use the green "Code"
button or the "Source code" zips. Those don't have the stuff the app needs to run.

Before you unzip it, right-click the zip, go to Properties and tick "Unblock" at the
bottom. Otherwise Windows complains about every file. Then right-click it again,
choose Extract All, and put the folder wherever you want to keep it.

Open the folder and double-click phoenixmp3. That folder only shows phoenixmp3 and
music_library, where your songs go. The app itself lives in a hidden pipeline folder.

## Using it

Go to Get Music and search for an album, song or artist, or paste a YouTube link.
Pick the album (and the edition, if there's more than one), tick the songs you want and
hit Download. Click an artist's name to see their profile with all their albums. You can
watch it work in Activity. Your songs go into the music_library folder: Music Folder at
the bottom of the sidebar opens it, and Show in Folder on an album opens it at that album.

If there are playlists you keep adding songs to, add them in My Playlists. After that,
Update checks those playlists for new songs that have been added and downloads them.

Preferences has the format (AAC or MP3), where songs go and the rest of the settings.
phoenixmp3 needs Microsoft Edge WebView2, which comes with Windows 11 and with Edge on
Windows 10. If it's missing, phoenixmp3 says so and offers Microsoft's download page.

YouTube changes stuff every now and then, which breaks downloading.


Only download stuff you're allowed to, for your own use. This is built on a bunch of
free open source tools (yt-dlp, beets, FFmpeg, Python and others), there's a list in
THIRD-PARTY-NOTICES.txt in the download.
