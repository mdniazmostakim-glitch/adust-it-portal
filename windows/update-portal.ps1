# ADUST IT Section Portal - install a newer version from a downloaded ZIP.
#
# 1. Stop the portal (Ctrl+C in its window).
# 2. Download the new adust-it-portal*.zip (it can stay in Downloads).
# 3. Double-click update-portal.bat.
#
# Your data (portal.db, uploads, backups, venv) is never touched.

$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $PSScriptRoot
$Downloads = Join-Path $env:USERPROFILE "Downloads"
$Temp = Join-Path $env:TEMP "adust-portal-update"

$zip = Get-ChildItem $Downloads -Filter "adust-it-portal*.zip" -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $zip) {
    Write-Host "No adust-it-portal*.zip found in $Downloads. Download the new version first." -ForegroundColor Red
    exit 1
}
Write-Host "Using: $($zip.Name)  ($($zip.LastWriteTime))" -ForegroundColor Cyan

if (Test-Path $Temp) { Remove-Item $Temp -Recurse -Force }
Expand-Archive $zip.FullName -DestinationPath $Temp -Force
$src = Get-ChildItem $Temp -Recurse -Filter app.py | Select-Object -First 1
if (-not $src) { Write-Host "This ZIP does not contain the portal (app.py missing)." -ForegroundColor Red; exit 1 }
$src = $src.DirectoryName

$items = "app.py", "i18n.py", "schema.sql", "requirements.txt", "README.md", "templates", "static", "windows", "deploy", "tests"
foreach ($item in $items) {
    $from = Join-Path $src $item
    if (Test-Path $from) { Copy-Item $from -Destination $App -Recurse -Force; Write-Host "  updated $item" }
}
# New Python packages, if the new version needs any
if (Test-Path "$App\venv\Scripts\pip.exe") { & "$App\venv\Scripts\pip.exe" install -q -r "$App\requirements.txt" }
Remove-Item $Temp -Recurse -Force

Write-Host ""
Write-Host "Update finished. Start the portal again with start-portal.bat, then press Ctrl+F5 in the browser." -ForegroundColor Green
