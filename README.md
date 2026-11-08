
#  phoenixmp3

Takes youtube playlists, albums or singles and downloades the audio. Tags them with metadata from musicbraniz so they are ready to use on any device.


## Getting it

Go to the Releases section on the right side of this page, open the newest one, and
download the big zip file under Assets (around 150 MB). Don't use the green "Code"
button or the "Source code" zips. Those don't have the stuff the app needs to run.

Before you unzip it, right-click the zip, go to Properties and tick "Unblock" at the
bottom. Otherwise Windows complains about every file. Then right-click it again,
choose Extract All, and put the folder wherever you want to keep it.

Open the folder and double-click START HERE.bat.

## Using it

Paste a link in the box at the top, hit Preview if you want to see what's in it, then
hit Download. Your songs go into the downloadswithmeta folder.

If there are playlists you keep adding songs to, click "List of checked playlists",
put one playlist link per line in the file that opens, and save it. After that,
"Check playlists" will check that persistant list for new songs that have been added to those playlists and downlaods them.

YouTube changes stuff every now and then, which breaks downloading.


Only download stuff you're allowed to, for your own use. This is built on a bunch of
free open source tools (yt-dlp, beets, FFmpeg, Python and others), there's a list in
THIRD-PARTY-NOTICES.txt in the download.
