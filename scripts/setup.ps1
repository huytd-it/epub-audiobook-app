#Requires -Version 5.1
<#
.SYNOPSIS
    Cai dat tat ca phu thuoc cho epub-audiobook-app.
.DESCRIPTION
    - Node.js (npm install)
    - Python venv + pip packages
    - Kiem tra Rust va Tauri CLI
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  EPUB Audiobook Studio - Setup" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# ── 1. Node.js / npm ─────────────────────────────────────────────────────────
Write-Host "`n[1/4] Installing npm packages..." -ForegroundColor Yellow
Push-Location $Root
try {
    npm install
    if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
} finally {
    Pop-Location
}

# ── 2. Python venv + pip ─────────────────────────────────────────────────────
Write-Host "`n[2/4] Setting up Python virtual environment..." -ForegroundColor Yellow
$venvDir = Join-Path $Root ".venv"
$pythonVersion = Get-Content (Join-Path $Root ".python-version") -ErrorAction SilentlyContinue
if (-not $pythonVersion) { $pythonVersion = "3.11" }
$pythonVersion = $pythonVersion.Trim()

# Tim Python executable
$pythonExe = $null
$candidates = @("python", "python3", "py")
foreach ($cmd in $candidates) {
    try {
        $ver = & $cmd --version 2>&1 | Select-String "Python $pythonVersion"
        if ($ver) {
            $pythonExe = $cmd
            break
        }
    } catch { }
}
if (-not $pythonExe) {
    Write-Host "  Python $pythonVersion not found, trying default python..." -ForegroundColor DarkYellow
    $pythonExe = "python"
}

# Tao venv neu chua co
if (-not (Test-Path (Join-Path $venvDir "Scripts\python.exe"))) {
    Write-Host "  Creating virtual environment (Python $pythonVersion)..." -ForegroundColor DarkGray
    & $pythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
}

$pipExe = Join-Path $venvDir "Scripts\pip.exe"

Write-Host "  Upgrading pip..." -ForegroundColor DarkGray
& $pipExe install --upgrade pip --quiet

Write-Host "  Installing project dependencies..." -ForegroundColor DarkGray
& $pipExe install -e "." --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

Write-Host "  Installing dev dependencies..." -ForegroundColor DarkGray
& $pipExe install -e ".[dev]" --quiet

# ── 3. Rust (kiem tra) ──────────────────────────────────────────────────────
Write-Host "`n[3/4] Checking Rust installation..." -ForegroundColor Yellow
try {
    $rustVer = & rustc --version 2>&1
    Write-Host "  Found: $rustVer" -ForegroundColor Green
} catch {
    Write-Host "  Rust not found!" -ForegroundColor Red
    Write-Host "  Install from: https://rustup.rs" -ForegroundColor White
    Write-Host "  Then run this script again." -ForegroundColor White
    exit 1
}

# ── 4. Tauri CLI (kiem tra) ─────────────────────────────────────────────────
Write-Host "`n[4/4] Checking Tauri CLI..." -ForegroundColor Yellow
try {
    $tauriVer = & npx tauri --version 2>&1
    Write-Host "  Found: $tauriVer" -ForegroundColor Green
} catch {
    Write-Host "  Installing @tauri-apps/cli..." -ForegroundColor DarkGray
    npm install -D @tauri-apps/cli
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Tauri CLI" }
}

# ── Done ─────────────────────────────────────────────────────────────────────
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Setup complete!" -ForegroundColor Green
Write-Host "  Run: .\scripts\dev.ps1" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Cyan
