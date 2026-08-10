@echo off
cd /d "%~dp0"
set "APP=%~dp0"
set "RUNTIME=%LOCALAPPDATA%\LocalReaderApp"
if "%LOCALAPPDATA%"=="" set "RUNTIME=%APP%.runtime"

set "PY=%RUNTIME%\.vieneu_test\Scripts\python.exe"
if exist "%PY%" goto run_file
set "PY=%APP%python\python.exe"
if exist "%PY%" goto run_file
set "PY=%APP%.vieneu_test\Scripts\python.exe"
if exist "%PY%" goto run_file
set "PY=%USERPROFILE%\anaconda3\python.exe"
if exist "%PY%" goto run_file

py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3.11 -B "%APP%stop_reader.py"
  exit /b
)

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  py -3 -B "%APP%stop_reader.py"
  exit /b
)

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
  python -B "%APP%stop_reader.py"
  exit /b
)

echo Khong thay Python de stop Local Reader.
pause
exit /b 1

:run_file
"%PY%" -B "%APP%stop_reader.py"
