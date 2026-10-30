@echo off
REM Double-click to create an IT admin account
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-portal.ps1" -CreateAdmin
pause
