<#
    Command line version of the app. Always uses the default settings
    (AAC, one folder, into music_library).

        .\get-music.ps1 "<youtube link>"
#>
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Url
)

$ErrorActionPreference = "Stop"

$Pipeline = $PSScriptRoot
$Top      = Split-Path -Parent $Pipeline
$Bin      = Join-Path $Pipeline "bin"
$Libs     = Join-Path $Pipeline "libs"
$Raw      = Join-Path $Pipeline "newdownloads"
$Final    = Join-Path $Top "music_library"
# An older install keeps its songs in downloadswithmeta: move it once, like the app does
# (beets reads music_library from config.yaml, so the songs must be there)
$Old      = Join-Path $Top "downloadswithmeta"
if (-not (Test-Path $Final) -and (Test-Path $Old)) {
    try { Rename-Item -LiteralPath $Old -NewName "music_library" }
    catch { throw "Couldn't rename downloadswithmeta to music_library. Close anything playing those songs and try again." }
}
$YtDlp    = Join-Path $Pipeline "yt-dlp-master\yt_dlp\__main__.py"
$Bundled  = Join-Path $Pipeline "python\python.exe"
$Py       = if (Test-Path $Bundled) { $Bundled } else { "python" }

New-Item -ItemType Directory -Force -Path $Raw, $Final | Out-Null

$env:PATH             = "$Bin;$env:PATH"   # ffmpeg, deno, fpcalc
$env:PYTHONPATH       = $Libs
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8       = "1"
$env:BEETSDIR         = $Pipeline
$env:FPCALC           = Join-Path $Bin "fpcalc.exe"
$env:PIP_USER         = "0"

Write-Host "`nDownloading" -ForegroundColor Cyan
& $Py $YtDlp `
    --ffmpeg-location $Bin `
    --remote-components ejs:github `
    --format "bestaudio[ext=m4a]/bestaudio" `
    --extract-audio --audio-format m4a --audio-quality 160K `
    --embed-thumbnail --embed-metadata --convert-thumbnails jpg `
    --parse-metadata ":(?P<meta_genre>)" `
    --no-overwrites --ignore-errors `
    --download-archive (Join-Path $Pipeline "downloaded.txt") `
    --output "$Raw\%(title)s [%(id)s].%(ext)s" `
    -- $Url
if ($LASTEXITCODE -ne 0) { Write-Warning "yt-dlp exited with code $LASTEXITCODE, some tracks may have failed." }

Write-Host "`nFixing cover art and titles" -ForegroundColor Cyan
& $Py (Join-Path $Pipeline "squareart.py") $Raw
& $Py (Join-Path $Pipeline "cleantitles.py") $Raw

Write-Host "`nTagging" -ForegroundColor Cyan
& $Py (Join-Path $Pipeline "prunelib.py")
& $Py -m beets import -q -s $Raw

Write-Host "`nArtists, albums, years and genres" -ForegroundColor Cyan
& $Py (Join-Path $Pipeline "fixartists.py")
& $Py (Join-Path $Pipeline "fillalbums.py")
& $Py (Join-Path $Pipeline "fillgenres.py")

Write-Host "`nDone. Songs are in $Final" -ForegroundColor Green
