@echo off
rem Double-click this. Starts the reader and opens the page.
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py"
%PY% --version >nul 2>nul || (
  echo.
  echo Python 3 was not found on this PC.
  echo Install it from https://www.python.org/downloads/
  echo and TICK "Add python.exe to PATH" during setup, then run this again.
  echo.
  pause
  exit /b 1
)
%PY% server.py --open
echo.
echo Reader stopped.
pause