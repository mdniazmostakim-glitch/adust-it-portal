@echo off
REM Run once as Administrator: allow other campus PCs to reach port 5000
powershell -NoProfile -Command "New-NetFirewallRule -DisplayName \"ADUST IT Portal\" -Direction Inbound -Protocol TCP -LocalPort 5000 -Action Allow"
pause
