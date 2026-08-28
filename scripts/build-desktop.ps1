#Requires -Version 5.1
<#
.SYNOPSIS
    Build desktop executable voi Tauri.
.DESCRIPTION
    1. Build frontend (Vite -> app/spa_dist)
    2. Build Tauri desktop app (Windows .exe)
    3. Output: src-tauri/target/release/bundle/
.PARAMETER Release
    Build release (default). Dung -Debug de build debug.
.PARAMETER Clean
    Xoa truoc khi build.
#>
param(
    [switch]$Debug,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
$tauriConf = Join-Path $Root "src-tauri\tauri.conf.json"

function Write-TauriConfig($config) {
    # Tauri's JSON parser rejects the UTF-8 BOM emitted by Set-Content in Windows PowerShell 5.1.
    $json = $config | ConvertTo-Json -Depth 10
    [System.IO.File]::WriteAllText($tauriConf, $json, [System.Text.UTF8Encoding]::new($false))
}

function Read-TauriConfig {
    [System.IO.File]::ReadAllText($tauriConf, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
}

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  EPUB Audiobook Studio - Build Desktop" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Kiem tra prerequisites
if (-not (Test-Path (Join-Path $Root "node_modules"))) {
    Write-Host "node_modules not found. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

# ── 0. Kiem tra bundle.active ────────────────────────────────────────────────
$conf = Read-TauriConfig
if (-not $conf.bundle.active) {
    Write-Host "`n[WARN] bundle.active is false in tauri.conf.json" -ForegroundColor Yellow
    Write-Host "  Enabling for this build..." -ForegroundColor DarkYellow
    $conf.bundle.active = $true
    Write-TauriConfig $conf
    $revertBundle = $true
}

try {
    # ── 1. Clean (optional) ───────────────────────────────────────────────────
    if ($Clean) {
        Write-Host "`n[CLEAN] Removing previous builds..." -ForegroundColor Yellow
        $releaseDir = Join-Path $Root "src-tauri\target\release"
        $bundleDir = Join-Path $releaseDir "bundle"
        if (Test-Path $bundleDir) { Remove-Item $bundleDir -Recurse -Force }
        Write-Host "  Cleaned." -ForegroundColor Green
    }

    # ── 2. Build Frontend ─────────────────────────────────────────────────────
    Write-Host "`n[1/2] Building frontend (Vite)..." -ForegroundColor Yellow
    Push-Location $Root
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
        Write-Host "  Frontend built to app/spa_dist/" -ForegroundColor Green
    } finally {
        Pop-Location
    }

    # ── 3. Build Tauri ────────────────────────────────────────────────────────
    Write-Host "`n[2/2] Building Tauri desktop app..." -ForegroundColor Yellow
    $buildMode = if ($Debug) { "debug" } else { "release" }
    Write-Host "  Mode: $buildMode" -ForegroundColor DarkGray

    Push-Location $Root
    try {
        if ($Debug) {
            npx tauri build --debug
        } else {
            npx tauri build
        }
        if ($LASTEXITCODE -ne 0) { throw "Tauri build failed" }
    } finally {
        Pop-Location
    }

    # ── 4. Output ─────────────────────────────────────────────────────────────
    $bundleDir = Join-Path $Root "src-tauri\target\$buildMode\bundle"
    $builtExe = Join-Path $Root "src-tauri\target\$buildMode\epub-audiobook-studio.exe"
    $desktopExe = Join-Path $Root "XuongSachNoi.exe"
    if (Test-Path $builtExe) {
        Copy-Item -LiteralPath $builtExe -Destination $desktopExe -Force
        Write-Host "  Desktop executable: $desktopExe" -ForegroundColor Green
    }
    Write-Host "`n========================================" -ForegroundColor Cyan
    Write-Host "  Build complete!" -ForegroundColor Green
    Write-Host "  Output: $bundleDir" -ForegroundColor White

    # Hien thi cac file output
    if (Test-Path $bundleDir) {
        $msis = Get-ChildItem -Path $bundleDir -Recurse -Filter "*.msi" -ErrorAction SilentlyContinue
        $exes = Get-ChildItem -Path $bundleDir -Recurse -Filter "*.exe" -ErrorAction SilentlyContinue
        if ($msis) {
            Write-Host "`n  MSI installers:" -ForegroundColor Yellow
            $msis | ForEach-Object { Write-Host "    $($_.FullName) ($([math]::Round($_.Length / 1MB, 1)) MB)" -ForegroundColor White }
        }
        if ($exes) {
            Write-Host "`n  Executables:" -ForegroundColor Yellow
            $exes | ForEach-Object { Write-Host "    $($_.FullName) ($([math]::Round($_.Length / 1MB, 1)) MB)" -ForegroundColor White }
        }
    }
    Write-Host "========================================" -ForegroundColor Cyan

} finally {
    # ── Revert bundle.active ──────────────────────────────────────────────────
    if ($revertBundle) {
        $conf = Read-TauriConfig
        $conf.bundle.active = $false
        Write-TauriConfig $conf
        Write-Host "`nReverted bundle.active to false." -ForegroundColor DarkGray
    }
}
