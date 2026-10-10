@echo off
rem dola-pool local one-click start (Windows). Double-click this file.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-local.ps1"
pause
