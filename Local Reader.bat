@echo off
setlocal
cd /d "%~dp0"
set "APP=%~dp0"
set "RUNTIME=%LOCALAPPDATA%\LocalReaderApp"
if "%LOCALAPPDATA%"=="" set "RUNTIME=%APP%.runtime"

set "LOCAL_READER_RUNTIME_DIR=%RUNTIME%"
set "LOCAL_READER_CLOUD_ENABLED=0"
set "LOCAL_READER_VIENEU_PORTS=8766"
set "LOCAL_READER_VIENEU_ENABLED=1"
set "LOCAL_READER_BACKGROUND_WORKERS=1"
set "LOCAL_READER_TTS_MODEL_VERSION=vieneu-tts-v3-turbo-48khz"
set "LOCAL_READER_VIENEU_MODEL_VERSION=vieneu-tts-v3-turbo-48khz"
set "LOCAL_READER_VIENEU_MODEL_FORMAT=v3-turbo"
set "LOCAL_READER_VIENEU_MODE=v3turbo"
set "LOCAL_READER_VIENEU_BACKBONE_DEVICE=auto"
set "LOCAL_READER_VIENEU_BACKEND=auto"
set "LOCAL_READER_VIENEU_BACKBONE_REPO=pnnbao-ump/VieNeu-TTS-v3-Turbo"
set "LOCAL_READER_VIENEU_GGUF_FILENAME="

set "PY=%RUNTIME%\.vieneu_test\Scripts\python.exe"
if exist "%PY%" goto run_file
set "PY=%APP%python\python.exe"
if exist "%PY%" goto run_file
set "PY=%APP%.vieneu_test\Scripts\python.exe"
if exist "%PY%" goto run_file
set "PY=%APP%..\.vieneu_test\Scripts\python.exe"
if exist "%PY%" goto run_file
set "PY=%USERPROFILE%\anaconda3\envs\localreader\python.exe"
if exist "%PY%" goto run_file
set "PY=%USERPROFILE%\anaconda3\python.exe"
if exist "%PY%" goto run_file

py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  start "" /min py -3.11 -B "%APP%open_reader.py"
  exit /b
)

py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
  start "" /min py -3 -B "%APP%open_reader.py"
  exit /b
)

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
  start "" /min python -B "%APP%open_reader.py"
  exit /b
)

echo Khong thay Python. Hay chay "Setup Local Reader.bat" truoc.
pause
exit /b 1

:run_file
start "" /min "%PY%" -B "%APP%open_reader.py"
