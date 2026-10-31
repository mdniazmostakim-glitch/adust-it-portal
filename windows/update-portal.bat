@echo off
REM Double-click after downloading a new adust-it-portal ZIP (stop the portal first)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-portal.ps1"
pause
