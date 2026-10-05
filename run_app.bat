@echo off
cd /d "%~dp0"
call find_python.bat
if not defined PY (
  echo Python 3 was not found. Run build_exe.bat first - it can install Python for you.
  pause
  exit /b 1
)
%PY% -m pip install --quiet pyside6 requests
%PY% nifty_app.py
