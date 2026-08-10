@echo off
setlocal
cd /d "%~dp0"
set "APP=%~dp0"
set "RUNTIME=%LOCALAPPDATA%\LocalReaderApp"
if "%LOCALAPPDATA%"=="" set "RUNTIME=%APP%.runtime"
set "LOCAL_READER_RUNTIME_DIR=%RUNTIME%"

set "PY="
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.11"
if defined PY goto run_setup

py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if defined PY goto run_setup

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto run_setup

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto run_setup

if exist "%USERPROFILE%\anaconda3\python.exe" set "PY=%USERPROFILE%\anaconda3\python.exe"
if defined PY goto run_setup

echo Khong thay Python de setup Local Reader.
pause
exit /b 1

:run_setup
%PY% "%APP%setup_local_reader.py"
echo.
pause
