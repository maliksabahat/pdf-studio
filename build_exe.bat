@echo off
setlocal
cd /d "%~dp0"
title Build PDF Studio EXE

echo ============================================================
echo   Building PDF Studio as a standalone Windows application
echo ============================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python is not installed or not on PATH.
  echo Install Python from https://www.python.org/downloads/ and try again.
  pause
  exit /b 1
)

python -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
  echo Installing PyInstaller...
  python -m pip install --upgrade pyinstaller
  if errorlevel 1 goto fail
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo Running PyInstaller...
python -m PyInstaller --noconfirm --clean --console ^
  --name "PDF Studio" ^
  --icon "app.ico" ^
  --add-data "static;static" ^
  --collect-all pymupdf ^
  --collect-all pillow_heif ^
  --hidden-import waitress ^
  app.py
if errorlevel 1 goto fail

copy /y "READ ME.txt" "dist\PDF Studio\READ ME.txt" >nul

echo.
echo Creating ZIP package...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Compress-Archive -Path 'dist\PDF Studio' -DestinationPath 'dist\PDF-Studio-Anas-Computer.zip' -Force"
if errorlevel 1 goto fail

echo.
echo ============================================================
echo   Build complete
echo.
echo   EXE : dist\PDF Studio\PDF Studio.exe
echo   ZIP : dist\PDF-Studio-Anas-Computer.zip
echo ============================================================
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
