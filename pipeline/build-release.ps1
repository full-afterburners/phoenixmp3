<#
    Builds the zip for the GitHub Releases page into ..\dist.

        .\build-release.ps1 -Version 1.0

    The zip has everything needed to run (Python, ffmpeg, deno, fpcalc, beets,
    yt-dlp) and none of your own files. It stops if it finds any.
#>
param(
    [string]$Version = "1.0"
)
$ErrorActionPreference = "Stop"

$Pipeline = $PSScriptRoot
$Root     = Split-Path -Parent $Pipeline
$Py       = Join-Path $Pipeline "python\python.exe"

$AppName = (Select-String -Path (Join-Path $Pipeline "gui.pyw") -Pattern '^APP_NAME\s*=\s*"(.+)"').Matches[0].Groups[1].Value
$Safe    = ($AppName -replace '[^\w\- ]', '').Trim()
$ZipName = ($Safe -replace '\s+', '-') + "-v$Version.zip"
Write-Host "Building $AppName v$Version" -ForegroundColor Cyan

foreach ($need in "python\pythonw.exe", "bin\ffmpeg.exe", "bin\ffprobe.exe", "bin\deno.exe",
                  "bin\fpcalc.exe", "libs\beets", "yt-dlp-master\yt_dlp\__main__.py") {
    if (-not (Test-Path (Join-Path $Pipeline $need))) { throw "Missing pipeline\$need" }
}

$Dist  = Join-Path $Root "dist"
$Stage = Join-Path $Dist $Safe
if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
New-Item -ItemType Directory -Force (Join-Path $Stage "pipeline") | Out-Null

Copy-Item (Join-Path $Root "START HERE.bat") $Stage
Copy-Item (Join-Path $Root "README.md") $Stage
Copy-Item (Join-Path $Root "README.md") (Join-Path $Stage "READ ME.txt")

# Only these files from pipeline\, so nothing personal gets copied.
$Code = "gui.pyw", "squareart.py", "cleantitles.py", "fillalbums.py", "prunelib.py",
        "writeplaylists.py", "make_notices.py", "get-music.ps1", "config.yaml",
        "icon.ico", "README.md"
foreach ($f in $Code) { Copy-Item (Join-Path $Pipeline $f) (Join-Path $Stage "pipeline") }

foreach ($dir in "python", "bin", "libs", "yt-dlp-master") {
    robocopy (Join-Path $Pipeline $dir) (Join-Path $Stage "pipeline\$dir") /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $dir failed (robocopy exit $LASTEXITCODE)" }
}

& $Py (Join-Path $Pipeline "make_notices.py") (Join-Path $Stage "THIRD-PARTY-NOTICES.txt")

# Check nothing personal made it in.
$personal = "library.db", "downloaded.txt", "unavailable.txt", "settings.json", "run-config.yaml",
            "playlists.txt", "playlists-run.json", "shortcut.txt", "last-run.log", "import.log"
$found = Get-ChildItem -Recurse -File $Stage | Where-Object { $personal -contains $_.Name -or $_.Extension -eq ".lnk" }
if ($found) { throw "Personal files in the build: $($found.FullName -join ', ')" }
$who  = [regex]::Escape($env:USERNAME)
$leak = Get-ChildItem -File $Stage, (Join-Path $Stage "pipeline") |
        Select-String -Pattern "C:\\Users\\$who|C:/Users/$who" -List
if ($leak) { throw "Your user path is in: $($leak.Path -join ', ')" }

$Zip = Join-Path $Dist $ZipName
if (Test-Path $Zip) { Remove-Item $Zip }
tar.exe -a -c -f $Zip -C $Dist $Safe
if ($LASTEXITCODE -ne 0) { throw "Zipping failed (tar exit $LASTEXITCODE)" }

Write-Host ("`nDone: {0} ({1:N0} MB)" -f $Zip, ((Get-Item $Zip).Length / 1MB)) -ForegroundColor Green
