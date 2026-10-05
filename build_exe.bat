@echo off
cd /d "%~dp0"
echo === Nifty OI Tracker - building NiftyOITracker.exe ===
echo.
call find_python.bat
if not defined PY goto :nopython
echo Using Python: %PY%
%PY% --version
echo.
echo [1/2] Installing PySide6, requests and PyInstaller ...
%PY% -m pip install --upgrade pyside6 requests pyinstaller || goto :err
echo.
echo [2/2] Building the app (1-3 minutes) ...
%PY% -m PyInstaller --noconfirm --onefile --windowed --name NiftyOITracker --icon app.ico --add-data "app.ico;." nifty_app.py || goto :err
copy /y dist\NiftyOITracker.exe . >nul
rmdir /s /q build >nul 2>&1
del /q NiftyOITracker.spec >nul 2>&1
echo.
echo Done. NiftyOITracker.exe is in this folder - double-click it to start.
echo Your data stays in the nifty_tracker_data folder next to it.
pause
exit /b 0

:nopython
echo Python 3 was not found on this computer.
echo.
echo Option 1: install it now with winget (Windows' own installer, about 30 MB).
echo Option 2: install it yourself from https://www.python.org/downloads/
echo           - tick "Add python.exe to PATH" on the first screen of the installer.
echo.
choice /c YN /m "Install Python 3.12 now with winget"
if errorlevel 2 goto :manual
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
if errorlevel 1 goto :manual
echo.
echo Python installed. Close this window and double-click build_exe.bat again.
pause
exit /b 0

:manual
echo.
echo After installing Python, close this window and double-click build_exe.bat again.
pause
exit /b 1

:err
echo.
echo Build failed - see the messages above.
pause
exit /b 1
