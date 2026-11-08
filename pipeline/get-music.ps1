<#
    get-music.ps1 - one-command yt-dlp -> beets pipeline

    Usage:
        .\get-music.ps1 "<youtube playlist / album / video URL>"
        .\get-music.ps1 "<album URL>" -Album      # group & match as a full album

    What it does:
        1. yt-dlp downloads every track in the URL as YouTube's own AAC stream
           (.m4a, ~128 kbps, no re-encode) into
           newdownloads\, embedding the YouTube thumbnail + basic metadata.
        2. beets acoustically fingerprints each file, matches MusicBrainz,
           writes clean tags, and MOVES the result into
           downloadswithmeta\ as "Artist - Title.m4a".

    Then just drag downloadswithmeta\ into iTunes and sync your Nano.

    Everything is self-contained under .\pipeline\ (portable ffmpeg, fpcalc,
    and a local beets install) so the whole app folder can be zipped and moved.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Url
)

$ErrorActionPreference = "Stop"

# --- Resolve all paths relative to this script's folder ---
# This script lives inside the pipeline folder; the output library sits one
# level up alongside it at the top of the app folder.
$Pipeline = $PSScriptRoot
$Music    = Split-Path -Parent $Pipeline
$Bin      = Join-Path $Pipeline "bin"
$Libs     = Join-Path $Pipeline "libs"
$Raw      = Join-Path $Pipeline "newdownloads"
$Final    = Join-Path $Music "downloadswithmeta"
$YtDlp    = Join-Path $Pipeline "yt-dlp-master\yt_dlp\__main__.py"
# Use the bundled Python if this copy has one (the release zip does); else the PC's.
$Bundled  = Join-Path $Pipeline "python\python.exe"
$Py       = if (Test-Path $Bundled) { $Bundled } else { "python" }
$env:PYTHONNOUSERSITE = "1"

New-Item -ItemType Directory -Force -Path $Raw, $Final | Out-Null

# Put our portable binaries (ffmpeg, fpcalc, deno) on PATH for this run.
# yt-dlp auto-detects the Deno JS runtime here, which YouTube now requires.
$env:PATH = "$Bin;$env:PATH"

# ---------------------------------------------------------------------------
# Stage 1: download the AAC audio (.m4a)
# ---------------------------------------------------------------------------
Write-Host "`n[1/2] Downloading with yt-dlp ..." -ForegroundColor Cyan
& $Py $YtDlp `
    --ffmpeg-location $Bin `
    --remote-components ejs:github `
    --format "bestaudio[ext=m4a]/bestaudio" `
    --extract-audio --audio-format m4a --audio-quality 160K `
    --embed-thumbnail --embed-metadata --convert-thumbnails jpg `
    --no-overwrites --ignore-errors `
    --download-archive (Join-Path $Pipeline "downloaded.txt") `
    --output "$Raw\%(title)s [%(id)s].%(ext)s" `
    $Url
if ($LASTEXITCODE -ne 0) { Write-Warning "yt-dlp exited with code $LASTEXITCODE (some tracks may have failed)." }

# Square-crop + shrink the 16:9 YouTube thumbnail for the iPod screen.
Write-Host "`nSquaring cover art ..." -ForegroundColor Cyan
$env:PYTHONPATH = $Libs                         # mutagen lives here
& $Py (Join-Path $Pipeline "squareart.py") $Raw

# Strip "Artist - " / "(Official Audio)" etc. so beets can match.
Write-Host "`nCleaning up titles ..." -ForegroundColor Cyan
& $Py (Join-Path $Pipeline "cleantitles.py") $Raw

# ---------------------------------------------------------------------------
# Stage 2: tag with beets and move into the master folder
# ---------------------------------------------------------------------------
Write-Host "`n[2/2] Tagging with beets ..." -ForegroundColor Cyan
$env:BEETSDIR   = $Pipeline                     # use OUR config.yaml + library
$env:PYTHONPATH = $Libs                         # local beets install
$env:FPCALC     = Join-Path $Bin "fpcalc.exe"   # fingerprint binary for chroma
$env:PIP_USER   = "0"

# -s = singleton: every track tagged individually (iPod indexes by tags).
# Forget songs deleted from the library folder (so re-downloads aren't skipped).
& $Py (Join-Path $Pipeline "prunelib.py")
& $Py -m beets import -q -s $Raw

# Matched songs with no album from YouTube: look up their studio album.
Write-Host "`nFilling in missing albums ..." -ForegroundColor Cyan
& $Py (Join-Path $Pipeline "fillalbums.py")

Write-Host "`nDone. Tagged files are in:" -ForegroundColor Green
Write-Host "  $Final"
Write-Host "Drag that folder into iTunes, then sync your iPod Nano." -ForegroundColor Green
