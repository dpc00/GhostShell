@echo off
REM Closes Sublime Text, deletes the stale Packages\User\ai_terminal.sublime-color-scheme
REM copy, then restarts Sublime Text. Must NOT be run from inside an ai_terminal tab
REM (that PTY belongs to the ST process this script is about to kill) -- run it from a
REM normal cmd/PowerShell window instead.
REM
REM Canonical copy lives at Packages\GhostShell\ai_terminal.sublime-color-scheme
REM (junction-linked from C:\Users\donal\projects\GhostShell). ai_terminal.py no longer
REM writes to Packages\User -- this file is a leftover duplicate. Deleting it while ST
REM is running left ST's color-scheme resource cache in an inconsistent state
REM (2026-08-07 incident) -- hence the full close/delete/reopen cycle here.

setlocal enabledelayedexpansion

set "TARGET=%APPDATA%\Sublime Text\Packages\User\ai_terminal.sublime-color-scheme"

REM Locate sublime_text.exe so we can restart it later.
set "SUBL_EXE="
for /f "delims=" %%P in ('where sublime_text.exe 2^>NUL') do set "SUBL_EXE=%%P"
if not defined SUBL_EXE (
    if exist "C:\Program Files\Sublime Text\sublime_text.exe" set "SUBL_EXE=C:\Program Files\Sublime Text\sublime_text.exe"
)
if not defined SUBL_EXE (
    echo Could not locate sublime_text.exe. Aborting -- nothing was changed.
    exit /b 1
)
echo Found Sublime Text at: %SUBL_EXE%

REM Graceful close (not /F) so ST saves hot_exit session state before exiting.
tasklist /FI "IMAGENAME eq sublime_text.exe" 2>NUL | find /I "sublime_text.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    echo Closing Sublime Text...
    taskkill /IM sublime_text.exe >NUL 2>&1
) else (
    echo Sublime Text was not running.
)

REM Wait for it to fully exit (up to ~60s).
set /a WAITED=0
:waitloop
tasklist /FI "IMAGENAME eq sublime_text.exe" 2>NUL | find /I "sublime_text.exe" >NUL
if %ERRORLEVEL% EQU 0 (
    if !WAITED! GEQ 60 (
        echo Sublime Text did not close after 60s. Aborting -- file was not touched.
        exit /b 1
    )
    timeout /t 1 /nobreak >NUL
    set /a WAITED+=1
    goto waitloop
)
echo Sublime Text is closed.

REM Delete the stale file.
if exist "%TARGET%" (
    del "%TARGET%"
    if exist "%TARGET%" (
        echo FAILED to delete "%TARGET%".
    ) else (
        echo Deleted "%TARGET%".
    )
) else (
    echo Nothing to delete -- "%TARGET%" does not exist.
)

REM Restart Sublime Text (hot_exit restores the previous session/tabs).
echo Restarting Sublime Text...
start "" "%SUBL_EXE%"

endlocal
