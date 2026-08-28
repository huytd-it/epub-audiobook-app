#Requires -Version 5.1
<#
.SYNOPSIS
    Chay epub-audiobook-app o che do development.
.DESCRIPTION
    1. Khoi dong Python FastAPI backend (port 8000)
    2. Chay Tauri dev (Vite + desktop window)
    3. Dung tat ca khi nhan Ctrl+C
#>

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  EPUB Audiobook Studio - Dev Mode" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Kiem tra venv
if (-not (Test-Path $venvPython)) {
    Write-Host "Virtual environment not found. Run setup.ps1 first." -ForegroundColor Red
    exit 1
}

# Khoi dong processes
$processes = @()

try {
    # ── 1. Python Backend ─────────────────────────────────────────────────────
    Write-Host "`n[1/2] Starting Python backend on port 8000..." -ForegroundColor Yellow
    $backendProc = Start-Process -FilePath $venvPython -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000", "--reload" -WorkingDirectory $Root -PassThru -NoNewWindow
    $processes += $backendProc
    Write-Host "  Backend PID: $($backendProc.Id)" -ForegroundColor DarkGray

    # Cho backend khoi dong
    Write-Host "  Waiting for backend to be ready..." -ForegroundColor DarkGray
    $maxWait = 30
    $waited = 0
    while ($waited -lt $maxWait) {
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2 -ErrorAction Stop
            if ($response.StatusCode -eq 200) {
                Write-Host "  Backend is ready!" -ForegroundColor Green
                break
            }
        } catch {
            Start-Sleep -Seconds 1
            $waited++
        }
    }
    if ($waited -ge $maxWait) {
        Write-Host "  Backend did not respond after ${maxWait}s, continuing anyway..." -ForegroundColor DarkYellow
    }

    # ── 2. Tauri Dev ──────────────────────────────────────────────────────────
    Write-Host "`n[2/2] Starting Tauri dev (Vite + Desktop)..." -ForegroundColor Yellow
    Write-Host "  This will start Vite dev server and open the Tauri window." -ForegroundColor DarkGray
    Write-Host "  Press Ctrl+C to stop all services.`n" -ForegroundColor DarkGray

    Push-Location $Root
    try {
        npx tauri dev
    } finally {
        Pop-Location
    }

} finally {
    # ── Cleanup ───────────────────────────────────────────────────────────────
    Write-Host "`nStopping all services..." -ForegroundColor Yellow
    foreach ($proc in $processes) {
        if (-not $proc.HasExited) {
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
                Write-Host "  Stopped PID $($proc.Id)" -ForegroundColor DarkGray
            } catch { }
        }
    }
    Write-Host "Done." -ForegroundColor Green
}
