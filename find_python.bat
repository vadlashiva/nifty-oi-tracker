@echo off
rem Sets PY to a working Python 3 command, or leaves it empty.
set "PY="
python --version >nul 2>&1 && (set "PY=python" & goto :eof)
py -3 --version >nul 2>&1 && (set "PY=py -3" & goto :eof)
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if defined PY (set "PY="%PY%"" & goto :eof)
for /d %%D in ("%ProgramFiles%\Python3*" "C:\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if defined PY set "PY="%PY%""
goto :eof
