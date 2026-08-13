@echo off
setlocal
cd /d "%~dp0"

rem  Python prints UTF-8 bytes, so the console must be UTF-8 too (65001).
rem  Without this the Korean text shows up as mojibake.
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo =====================================
echo  Timetable -^> Google Calendar (.ics)
echo =====================================
echo.

if not exist timetable.json (
  echo [!] timetable.json not found.
  echo     Run crawl.js in the portal console, save the output as timetable.json.
  echo.
)

rem  ^<nul : answer prompts with detected defaults
rem  remove ^<nul below if you want to be asked
py make_timetable.py <nul
if errorlevel 1 goto fail

echo.
echo Done. Import the .ics file into Google Calendar.
echo.
echo Press any key to close...
pause >nul
exit /b 0

:fail
echo.
echo *** Failed. See the message above. ***
echo.
echo Press any key to close...
pause >nul
exit /b 1
