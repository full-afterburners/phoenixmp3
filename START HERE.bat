@echo off
rem Starts phoenixmp3 with the bundled Python. The app makes a shortcut in this
rem folder on its first start, which can be used after that.
if not exist "%~dp0pipeline\python\pythonw.exe" (
    echo.
    echo  This copy is missing the bundled programs ^(Python, ffmpeg and so on^).
    echo.
    echo  You probably downloaded the "Source code" zip. Go to the project's
    echo  GitHub page, open Releases on the right, and download the big .zip
    echo  under Assets instead.
    echo.
    pause
    exit /b 1
)
start "" "%~dp0pipeline\python\pythonw.exe" "%~dp0pipeline\gui.pyw"
