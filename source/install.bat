@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
echo ============================================
echo  Image-PDF Korean Translator - install
echo ============================================
set "CHK=import sys; sys.exit(0 if (3,10)<=sys.version_info[:2]<=(3,14) else 1)"

rem --- remove .venv made with an unsupported Python (e.g. 3.15) ---
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "%CHK%" >nul 2>nul || (
    echo Removing old .venv made with unsupported Python ...
    rmdir /s /q .venv
  )
)

rem --- find a supported Python 3.10 - 3.14 ---
set "PY="
call :find
if not defined PY (
  echo.
  echo Supported Python 3.10 - 3.14 not found. Your default Python is not supported yet.
  echo Trying to install Python 3.13 with the Python Install Manager ...
  py install 3.13
  call :find
)
if not defined PY (
  echo.
  echo [ERROR] Could not find or install Python 3.13 automatically.
  echo   1^) Open https://www.python.org/downloads/windows/
  echo   2^) Download "Windows installer (64-bit)" of Python 3.13.x and install it
  echo   3^) Run install.bat again
  pause
  exit /b 1
)
echo [1/3] Using Python:
%PY% --version

echo [2/3] Creating virtual environment .venv ...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] venv creation failed.
  pause
  exit /b 1
)

echo [3/3] Installing packages - first time takes a few minutes ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [ERROR] Package install failed. Capture this window and ask for help.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -c "import PySide6, rapidocr, pymupdf, onnxruntime; print('check OK')"
if errorlevel 1 (
  echo [ERROR] Import check failed. Capture this window and ask for help.
  pause
  exit /b 1
)
echo.
echo Install complete. Double-click run.bat to start.
pause
exit /b 0

:find
for %%V in (3.13 3.14 3.12 3.11 3.10) do (
  if not defined PY (
    py -%%V -c "%CHK%" >nul 2>nul && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "%CHK%" >nul 2>nul && set "PY=python"
)
exit /b 0
