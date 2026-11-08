<#
    build-release.ps1 - build the ready-to-run zip for the GitHub Releases page.

    Usage (from the pipeline folder):
        .\build-release.ps1 -Version 1.0

    Produces  ..\dist\<app-name>-v<Version>.zip  containing everything a friend
    needs (bundled Python, ffmpeg, deno, fpcalc, beets, yt-dlp) and NONE of your
    personal files (music, playlists, download history, settings, logs).
    It refuses to finish if it finds personal paths in the staged files.
#>
param(
    [string]$Version = "1.0"
)
$ErrorActionPreference = "Stop"

$Pipeline = $PSScriptRoot
$Root     = Split-Path -Parent $Pipeline
$Py       = Join-Path $Pipeline "python\python.exe"

# --- App name (from gui.pyw) -------------------------------------------------
$AppName = (Select-String -Path (Join-Path $Pipeline "gui.pyw") -Pattern '^APP_NAME\s*=\s*"(.+)"').Matches[0].Groups[1].Value
$Safe    = ($AppName -replace '[^\w\- ]', '').Trim()
$ZipName = ($Safe -replace '\s+', '-') + "-v$Version.zip"
Write-Host "Building '$AppName' v$Version" -ForegroundColor Cyan

# --- Everything we bundle must be present ------------------------------------
foreach ($need in "python\pythonw.exe", "bin\ffmpeg.exe", "bin\ffprobe.exe", "bin\deno.exe",
                  "bin\fpcalc.exe", "libs\beets", "yt-dlp-master\yt_dlp\__main__.py") {
    if (-not (Test-Path (Join-Path $Pipeline $need))) { throw "Missing pipeline\$need - can't build." }
}

# --- Stage --------------------------------------------------------------------
$Dist  = Join-Path $Root "dist"
$Stage = Join-Path $Dist $Safe
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force (Join-Path $Stage "pipeline") | Out-Null

# Top level
Copy-Item (Join-Path $Root "START HERE.bat") $Stage
Copy-Item (Join-Path $Root "README.md") $Stage
Copy-Item (Join-Path $Root "README.md") (Join-Path $Stage "READ ME.txt")   # opens in Notepad

# Our own code + config (explicit list: nothing personal can sneak in)
$Code = "gui.pyw", "squareart.py", "cleantitles.py", "fillalbums.py", "prunelib.py",
        "writeplaylists.py", "make_notices.py", "get-music.ps1", "config.yaml",
        "icon.ico", "README.md"
foreach ($f in $Code) { Copy-Item (Join-Path $Pipeline $f) (Join-Path $Stage "pipeline") }

# Bundled dependencies (no Python caches)
foreach ($dir in "python", "bin", "libs", "yt-dlp-master") {
    robocopy (Join-Path $Pipeline $dir) (Join-Path $Stage "pipeline\$dir") /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $dir failed (robocopy exit $LASTEXITCODE)" }
}

# Licences / credits
& $Py (Join-Path $Pipeline "make_notices.py") (Join-Path $Stage "THIRD-PARTY-NOTICES.txt")

# --- Privacy check -------------------------------------------------------------
$personal = "library.db", "downloaded.txt", "unavailable.txt", "settings.json", "run-config.yaml",
            "playlists.txt", "playlists-run.json", "shortcut.txt", "last-run.log", "import.log"
$found = Get-ChildItem -Recurse -File $Stage | Where-Object { $personal -contains $_.Name -or $_.Extension -eq ".lnk" }
if ($found) { throw "Personal file(s) in the build: $($found.FullName -join ', ')" }
$who  = [regex]::Escape($env:USERNAME)
$ours = Get-ChildItem -File $Stage, (Join-Path $Stage "pipeline")
$leak = $ours | Select-String -Pattern "C:\\Users\\$who|C:/Users/$who" -List
if ($leak) { throw "Personal path found in: $($leak.Path -join ', ')" }
Write-Host "Privacy check passed." -ForegroundColor Green

# --- Zip (Windows' built-in tar makes standard zips, much faster than Compress-Archive)
$Zip = Join-Path $Dist $ZipName
if (Test-Path $Zip) { Remove-Item $Zip }
tar.exe -a -c -f $Zip -C $Dist $Safe
if ($LASTEXITCODE -ne 0) { throw "Zipping failed (tar exit $LASTEXITCODE)" }

$mb = (Get-Item $Zip).Length / 1MB
Write-Host ("`nDone: {0}  ({1:N0} MB)" -f $Zip, $mb) -ForegroundColor Green
Write-Host "Upload it on GitHub: your repo -> Releases -> Draft a new release -> attach this file."
