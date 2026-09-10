# Starts ha-entity-manager: makes sure config.json and PyYAML exist, gets your
# HOME_ASSISTANT_TOKEN (session env var, then a local .token file, then prompts),
# opens the dashboard in your browser, and runs the server.
#
# Usage: .\start.ps1   (or double-click start.bat)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found on PATH. Install it first: winget install --id Python.Python.3.12 -e" -ForegroundColor Red
    exit 1
}

$configPath = Join-Path $PSScriptRoot "config.json"
if (-not (Test-Path $configPath)) {
    Copy-Item (Join-Path $PSScriptRoot "config.example.json") $configPath
    Write-Host "Created config.json from config.example.json -- edit it with your Home Assistant host and yaml_root, then re-run this script." -ForegroundColor Yellow
    exit 0
}

$tokenFile = Join-Path $PSScriptRoot ".token"
if (-not $env:HOME_ASSISTANT_TOKEN) {
    if (Test-Path $tokenFile) {
        $env:HOME_ASSISTANT_TOKEN = (Get-Content $tokenFile -Raw).Trim()
    } else {
        $secure = Read-Host "Enter your Home Assistant long-lived access token" -AsSecureString
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
        $env:HOME_ASSISTANT_TOKEN = $plain
        $save = Read-Host "Save this token locally so you don't have to re-enter it? (y/N)"
        if ($save -eq "y") {
            Set-Content -Path $tokenFile -Value $plain -Encoding utf8 -NoNewline
            Write-Host "Saved to .token (already excluded from git via .gitignore)."
        }
    }
}

python -c "import yaml" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing PyYAML..."
    python -m pip install --quiet pyyaml
}

$serverPort = (Get-Content $configPath | ConvertFrom-Json).server_port
if (-not $serverPort) { $serverPort = 8765 }

Write-Host "Starting ha-entity-manager on http://localhost:$serverPort ..."
Start-Process "http://localhost:$serverPort"
python server.py
