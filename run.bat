@echo off
setlocal
cd /d "%~dp0"
title PDF Studio

python app.py
if errorlevel 1 (
  echo.
  echo PDF Studio could not start. See the message above.
  pause
)
