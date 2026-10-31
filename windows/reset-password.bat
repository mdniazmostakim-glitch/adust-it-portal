@echo off
REM Double-click to set a new password for an account (stop the portal first)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-portal.ps1" -ResetPassword
pause
