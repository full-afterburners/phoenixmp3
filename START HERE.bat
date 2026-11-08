@echo off
rem Starts the app with its own bundled Python (no install needed).
rem After the first start, the app puts a shortcut with its icon in this
rem folder - use that from then on.
if not exist "%~dp0pipeline\python\pythonw.exe" (
    echo.
    echo  This copy is missing its bundled parts ^(Python, ffmpeg, ...^).
    echo.
    echo  You probably downloaded the "Source code" zip. Instead, go to the
    echo  project's GitHub page, open "Releases" on the right, and download
    echo  the big .zip listed under "Assets".
    echo.
    pause
    exit /b 1
)
start "" "%~dp0pipeline\python\pythonw.exe" "%~dp0pipeline\gui.pyw"
