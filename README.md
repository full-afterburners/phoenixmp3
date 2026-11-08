# phoenixmp3

I made this to get my YouTube Music playlists onto my old iPod nano. You paste in a
YouTube link (a song, an album or a whole playlist) and it downloads the audio and
sorts out the artist, album, year and cover art so it looks right on your player.
Works for iPods, MP3 players, car stereos, phones, whatever.

It only runs on Windows 10 or 11. You don't need to install anything.

## Getting it

Go to the Releases section on the right side of this page, open the newest one, and
download the big zip file under Assets (around 150 MB). Don't use the green "Code"
button or the "Source code" zips. Those don't have the stuff the app needs to run.

Before you unzip it, right-click the zip, go to Properties and tick "Unblock" at the
bottom. Otherwise Windows complains about every file. Then right-click it again,
choose Extract All, and put the folder wherever you want to keep it.

Open the folder and double-click START HERE.bat. If Windows shows a blue "Windows
protected your PC" box, click "More info" and then "Run anyway". It just does that
for anything that isn't from a big company. The app will open and make a shortcut in
the same folder, so after the first time you can just use that (or copy it to your
desktop).

## Using it

Paste a link in the box at the top, hit Preview if you want to see what's in it, then
hit Download. Your songs go into the downloadswithmeta folder.

If there are playlists you keep adding songs to, click "List of checked playlists",
put one playlist link per line in the file that opens, and save it. After that,
"Check playlists" only grabs the songs you don't already have. The playlists need to
be public or unlisted on YouTube, private ones won't work.

The Settings button lets you pick the format and how the files are laid out. AAC is
the default and is best for iPods and phones. If you're putting music on a car stereo,
a USB stick or a cheap MP3 player, switch it to MP3 and pick the Artist/Album folders
option, since those usually browse by folder. If you're not sure, MP3 plays on
everything. Settings only apply to new downloads.

## If it stops working

YouTube changes stuff every now and then, which breaks downloading. When that happens
grab the newest release from this page. To keep your music when you update, move the
downloadswithmeta folder, playlists.txt and pipeline\downloaded.txt from the old
folder into the new one.

Some songs just won't download because they're blocked in your country or got taken
down. The app skips those on its own.

If something else goes wrong, send me the file pipeline\last-run.log and I'll take a
look.

Only download stuff you're allowed to, for your own use. This is built on a bunch of
free open source tools (yt-dlp, beets, FFmpeg, Python and others), there's a list in
THIRD-PARTY-NOTICES.txt in the download.
