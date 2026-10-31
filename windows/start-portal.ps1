# ADUST IT Section Portal - run on a Windows PC or Windows server.
#
# Usage (from PowerShell, or double-click start-portal.bat):
#   .\windows\start-portal.ps1               start the portal
#   .\windows\start-portal.ps1 -CreateAdmin  create an IT admin account first
#   .\windows\start-portal.ps1 -ResetPassword  set a new password for an account (also unlocks it)
#
# Edit the settings below once. Everything else is automatic.

param([switch]$CreateAdmin, [switch]$ResetPassword)

# ----------------------------------------------------------------- settings
$DataDir    = "C:\ADUST-IT-Data"                  # live database + files (local disk, NOT Google Drive)
$BackupCopy = "G:\My Drive\ADUST-IT-Backups"      # second backup copy (Google Drive / USB / other disk); "" = none
$Networks   = "172.20.0.0/16,172.16.0.0/24"       # campus networks allowed to open the portal (comma-separated)
$Port       = 5000
$SecretKey  = ""                                  # "" = a random key is created once and kept in $DataDir\secret.key
# Email (optional): fill these to send email notifications and password resets
$SmtpHost   = ""                                  # e.g. smtp.gmail.com
$SmtpUser   = ""                                  # e.g. adust.it.portal@gmail.com
$SmtpPass   = ""                                  # Gmail App password (16 characters)
# --------------------------------------------------------------------------

$ErrorActionPreference = "Stop"
$App = Split-Path -Parent $PSScriptRoot           # the folder that contains app.py
Set-Location $App
if (-not (Test-Path "$App\app.py")) { Write-Host "app.py not found in $App" -ForegroundColor Red; exit 1 }

# 1. Python environment (created once; packages refreshed when a new version needs them)
if (-not (Test-Path "$App\venv\Scripts\python.exe")) {
    Write-Host "Creating Python environment (first run only)..." -ForegroundColor Cyan
    py -m venv venv
    & "$App\venv\Scripts\python.exe" -m pip install --upgrade pip
    & "$App\venv\Scripts\pip.exe" install -r requirements.txt
}
if (-not (Test-Path "$App\venv\Scripts\waitress-serve.exe")) {
    & "$App\venv\Scripts\pip.exe" install -q -r requirements.txt
}

# 2. Data folders; move existing test data from the app folder on first run
New-Item -ItemType Directory -Force -Path $DataDir, "$DataDir\uploads", "$DataDir\backups" | Out-Null
if (-not (Test-Path "$DataDir\portal.db") -and (Test-Path "$App\portal.db")) {
    Write-Host "Copying existing data to $DataDir ..." -ForegroundColor Cyan
    Copy-Item "$App\portal.db" "$DataDir\portal.db"
    if (Test-Path "$App\uploads") { Copy-Item "$App\uploads\*" "$DataDir\uploads\" -Recurse -Force }
}

# 3. Settings for the portal
if (-not $SecretKey) {
    $keyFile = "$DataDir\secret.key"
    if (-not (Test-Path $keyFile)) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        ($bytes | ForEach-Object { $_.ToString("x2") }) -join "" | Set-Content -NoNewline $keyFile
    }
    $SecretKey = (Get-Content $keyFile -Raw).Trim()
}
$env:PORTAL_SECRET_KEY       = $SecretKey
$env:PORTAL_ALLOWED_NETWORKS = $Networks
$env:PORTAL_DATABASE         = "$DataDir\portal.db"
$env:PORTAL_UPLOADS          = "$DataDir\uploads"
$env:PORTAL_BACKUPS          = "$DataDir\backups"
$env:PORTAL_BACKUP_COPY      = $BackupCopy
if ($SmtpHost) {
    $env:SMTP_HOST = $SmtpHost; $env:SMTP_PORT = "587"; $env:SMTP_USER = $SmtpUser
    $env:SMTP_PASSWORD = $SmtpPass; $env:SMTP_FROM = "ADUST IT Section <$SmtpUser>"
}

$Flask = "$App\venv\Scripts\flask.exe"
if ($CreateAdmin) {
    & $Flask --app app create-admin
    exit
}
if ($ResetPassword) {
    & $Flask --app app reset-password
    exit
}

$ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -like "172.*" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "ADUST IT Portal is starting..." -ForegroundColor Green
Write-Host "  On this PC:        http://127.0.0.1:$Port"
if ($ip) { Write-Host "  From campus PCs:   http://${ip}:$Port" }
Write-Host "  Data folder:       $DataDir"
Write-Host "  Keep this window open. Press Ctrl+C to stop."
Write-Host ""
# waitress is a production web server (flask run is only for development)
& "$App\venv\Scripts\waitress-serve.exe" --listen="0.0.0.0:$Port" --threads=8 app:app
