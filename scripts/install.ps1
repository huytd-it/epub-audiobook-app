#Requires -Version 5.1
<#
.SYNOPSIS
    Cai dat du moi truong cho epub-audiobook-app.
.DESCRIPTION
    - Tao Python venv (neu chua co)
    - Cai pip + dev dependencies tu pyproject.toml
    - Cai npm packages
    - Kiem tra Rust va Tauri CLI
    - Hien thi danh sach model TTS de chon tai weights/package
    - Don dep build/ cu truoc khi cai de tranh loi WinError 183
.PARAMETER SkipNpm
    Bo qua buoc cai npm packages.
#>
param(
    [switch]$SkipNpm
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  EPUB Audiobook Studio - Install" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# ── 0. Don dep build/ cu (tranh loi WinError 183) ────────────────────────────
Write-Host "`n[0/5] Cleaning stale build artifacts..." -ForegroundColor Yellow
$buildDir = Join-Path $Root "build"
if (Test-Path $buildDir) {
    Remove-Item $buildDir -Recurse -Force
    Write-Host "  Removed stale build/ (root cause of WinError 183)." -ForegroundColor Green
} else {
    Write-Host "  No stale build/ dir found." -ForegroundColor DarkGray
}

# ── 1. Python venv + pip ─────────────────────────────────────────────────────
Write-Host "`n[1/5] Setting up Python virtual environment..." -ForegroundColor Yellow
$venvDir = Join-Path $Root ".venv"
$pythonVersion = Get-Content (Join-Path $Root ".python-version") -ErrorAction SilentlyContinue
if (-not $pythonVersion) { $pythonVersion = "3.11" }
$pythonVersion = $pythonVersion.Trim()

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

if (-not (Test-Path (Join-Path $venvDir "Scripts\python.exe"))) {
    Write-Host "  Creating virtual environment (Python $pythonVersion)..." -ForegroundColor DarkGray
    & $pythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Failed to create venv" }
} else {
    Write-Host "  venv already exists." -ForegroundColor DarkGray
}

$pythonExe = Join-Path $venvDir "Scripts\python.exe"
$pipExe = Join-Path $venvDir "Scripts\pip.exe"

Write-Host "  Upgrading pip..." -ForegroundColor DarkGray
& $pythonExe -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip" }

Write-Host "  Installing project dependencies..." -ForegroundColor DarkGray
& $pipExe install -e ".[dev]" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# ── 2. Node.js / npm ─────────────────────────────────────────────────────────
if ($SkipNpm) {
    Write-Host "`n[2/5] Skipping npm install (-SkipNpm)." -ForegroundColor DarkYellow
} else {
    Write-Host "`n[2/5] Installing npm packages..." -ForegroundColor Yellow
    Push-Location $Root
    try {
        npm install
        if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    } finally {
        Pop-Location
    }
}

# ── 3. Rust (kiem tra) ──────────────────────────────────────────────────────
Write-Host "`n[3/5] Checking Rust installation..." -ForegroundColor Yellow
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
Write-Host "`n[4/5] Checking Tauri CLI..." -ForegroundColor Yellow
try {
    $tauriVer = & npx tauri --version 2>&1
    Write-Host "  Found: $tauriVer" -ForegroundColor Green
} catch {
    Write-Host "  Installing @tauri-apps/cli..." -ForegroundColor DarkGray
    npm install -D @tauri-apps/cli
    if ($LASTEXITCODE -ne 0) { throw "Failed to install Tauri CLI" }
}

# ── 5. Chon model TTS de tai ─────────────────────────────────────────────────
Write-Host "`n[5/5] TTS models..." -ForegroundColor Yellow
Write-Host "  Cac model sau co the tai weights/package tu app. Go so de chon, bo trong de bo qua." -ForegroundColor DarkGray

# Bang model: index | id | ten | module kiem tra | extra | ghi chu
$models = @(
    @{ Index = 1; Id = "voxcpm2";    Name = "VoxCPM2 (clone giong noi)" ; Module = "voxcpm";  Extra = "tts";         Detail = "Weights duoc tai boi thu vien khi chay lan dau." },
    @{ Index = 2; Id = "omnivoice";  Name = "OmniVoice (clone giong noi)"; Module = "omnivoice"; Extra = "omnivoice"; Detail = "Weights duoc tai boi thu vien khi chay lan dau." },
    @{ Index = 3; Id = "vieneu-fast"; Name = "VieNeu fast (giong preset)" ; Module = "vieneu";  Extra = "vieneu-fast"; Detail = "Package VieNeu va voice presets." },
    @{ Index = 4; Id = "edge-tts";   Name = "Edge TTS (online)"          ; Module = "edge_tts"; Extra = "light-tts";   Detail = "Dich vu Edge TTS truc tuyen." },
    @{ Index = 5; Id = "gtts";       Name = "Google Translate TTS (online)"; Module = "gtts"; Extra = "light-tts"; Detail = "Dich vu Google Translate TTS truc tuyen." },
    @{ Index = 6; Id = "zerotts";    Name = "ZeroTTS (giong preset)"     ; Module = "zerotts"; Extra = "zerotts";     Detail = "Weights ONNX va voice pack cuc bo (chay scripts/download_zerotts.py)." },
    @{ Index = 7; Id = "f5-vivoice"; Name = "F5-TTS Vietnamese ViVoice"  ; Module = "f5_tts";  Extra = "f5-vivoice";  Detail = "Weights tu Hugging Face (hynt/F5-TTS-Vietnamese-ViVoice)." }
)

Write-Host ""
foreach ($m in $models) {
    $check = & $pythonExe -c "import importlib.util; print(1 if importlib.util.find_spec('$($m.Module)') else 0)" 2>$null
    $installed = if ("$check".Trim() -eq "1") { $true } else { $false }
    $status = if ($installed) { " [DA CAI]" } else { "" }
    Write-Host ("  {0}. {1}  {2}" -f $m.Index, $m.Name, $status) -ForegroundColor White
    Write-Host ("     {0}" -f $m.Detail) -ForegroundColor DarkGray
}

$selection = Read-Host "`nChon so model muon tai (vd: 1 3, bo trong de skip)"
$selections = ($selection.Trim() -split "[\s,;]+") | Where-Object { $_ -ne "" }

foreach ($sel in $selections) {
    $m = $models | Where-Object { "$($_.Index)" -eq $sel } | Select-Object -First 1
    if (-not $m) {
        Write-Host "  Khong nhan dien so '$sel', bo qua." -ForegroundColor DarkYellow
        continue
    }
    Write-Host "`n  -> Tai '$($m.Name)' (extra: $($m.Extra))..." -ForegroundColor Yellow
    Push-Location $Root
    try {
        & $pipExe install -e ".[$($m.Extra)]" --quiet
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  Tai '$($m.Name)' that bai. Xem loi ben tren." -ForegroundColor Red
        } else {
            Write-Host "  Tai '$($m.Name)' hoan tat." -ForegroundColor Green
        }
    } finally {
        Pop-Location
    }
    if ($m.Id -eq "zerotts") {
        Write-Host "  -> Chay download_zerotts.py..." -ForegroundColor Yellow
        & $pythonExe (Join-Path $Root "scripts\download_zerotts.py")
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  Tai weights ZeroTTS that bai." -ForegroundColor Red
        } else {
            Write-Host "  Tai weights ZeroTTS hoan tat." -ForegroundColor Green
        }
    }
}

# ── Done ─────────────────────────────────────────────────────────────────────
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Install complete!" -ForegroundColor Green
Write-Host "  Run: .\scripts\dev.ps1" -ForegroundColor White
Write-Host "========================================" -ForegroundColor Cyan
