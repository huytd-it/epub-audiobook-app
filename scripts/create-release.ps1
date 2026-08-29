#Requires -Version 5.1
<#
.SYNOPSIS
  Tạo tag và push để trigger GitHub Release tự động.
.DESCRIPTION
  1. Đồng bộ version vào package.json / pyproject.toml / src-tauri/tauri.conf.json
  2. Commit, tạo tag vX.Y.Z, push + push tag => kích hoạt .github/workflows/release.yml
  3. Release notes được GitHub tự động generate từ .github/release.yml
.EXAMPLE
  ./scripts/create-release.ps1 -Version 1.1.0
  ./scripts/create-release.ps1 -Version 1.1.0-rc1 -Prerelease
  ./scripts/create-release.ps1 -Version 1.0.1 -Message "fix: hotfix TTS OOM"
#>
param(
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$Message = "",
    [switch]$Prerelease,
    [switch]$SkipPush,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

# Chuẩn hoá version: cho phép "1.0.1" hoặc "v1.0.1"
$raw = $Version.Trim()
if ($raw.StartsWith("v")) { $raw = $raw.Substring(1) }
$tag = "v$raw"
$semver = $raw  # không có v

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Create Release $tag" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Kiểm tra gh CLI (optional)
$hasGh = $null -ne (Get-Command gh -ErrorAction SilentlyContinue)

# 1. Cập nhật version trong 3 file
Write-Host "`n[1/4] Updating version to $semver ..." -ForegroundColor Yellow

# package.json
$pkgPath = Join-Path $Root "package.json"
$pkg = Get-Content $pkgPath -Raw | ConvertFrom-Json
$pkg.version = $semver
($pkg | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $pkgPath -Encoding utf8
Write-Host "  ✓ package.json" -ForegroundColor Green

# pyproject.toml
$pyPath = Join-Path $Root "pyproject.toml"
$pyContent = Get-Content $pyPath -Raw
$pyContent = $pyContent -replace 'version = ".*?"', "version = `"$semver`""
Set-Content -LiteralPath $pyPath -Value $pyContent -Encoding utf8 -NoNewline
Write-Host "  ✓ pyproject.toml" -ForegroundColor Green

# src-tauri/tauri.conf.json
$tauriPath = Join-Path $Root "src-tauri/tauri.conf.json"
$tauriJson = Get-Content $tauriPath -Raw | ConvertFrom-Json
$tauriJson.version = $semver
# Ghi không BOM (Tauri parser kỵ BOM)
$json = $tauriJson | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText($tauriPath, $json, [System.Text.UTF8Encoding]::new($false))
Write-Host "  ✓ src-tauri/tauri.conf.json" -ForegroundColor Green

if ($DryRun) {
    Write-Host "`n[DryRun] Dừng trước commit/tag." -ForegroundColor Magenta
    git -C $Root diff --stat
    exit 0
}

# 2. Commit
Write-Host "`n[2/4] Committing ..." -ForegroundColor Yellow
git -C $Root add package.json pyproject.toml src-tauri/tauri.conf.json
$commitMsg = if ($Message) { "chore(release): $tag - $Message" } else { "chore(release): $tag" }
# Nếu không có gì để commit (version trùng), bỏ qua
$staged = git -C $Root diff --cached --name-only
if (-not $staged) {
    Write-Host "  (no changes to commit — version có thể đã đúng)" -ForegroundColor DarkYellow
} else {
    git -C $Root commit -m $commitMsg
    Write-Host "  ✓ committed: $commitMsg" -ForegroundColor Green
}

# 3. Tag
Write-Host "`n[3/4] Creating tag $tag ..." -ForegroundColor Yellow
$existing = git -C $Root tag --list $tag
if ($existing) {
    Write-Host "  Tag $tag đã tồn tại — xoá và tạo lại?" -ForegroundColor Yellow
    git -C $Root tag -d $tag
}
$tagMsg = if ($Message) { "$tag - $Message" } else { $tag }
git -C $Root tag -a $tag -m $tagMsg
Write-Host "  ✓ tag $tag" -ForegroundColor Green

if ($SkipPush) {
    Write-Host "`n[SkipPush] Không push. Chạy thủ công:" -ForegroundColor Magenta
    Write-Host "  git push origin main && git push origin $tag" -ForegroundColor White
    exit 0
}

# 4. Push
Write-Host "`n[4/4] Pushing ..." -ForegroundColor Yellow
$branch = (git -C $Root rev-parse --abbrev-ref HEAD).Trim()
git -C $Root push origin $branch
git -C $Root push origin $tag
Write-Host "  ✓ pushed $branch + $tag" -ForegroundColor Green

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Done! GitHub Actions sẽ tạo Release" -ForegroundColor Green
Write-Host "  https://github.com/$(git -C $Root remote get-url origin | ForEach-Object { $_ -replace '.*github.com[:/](.*)\.git.*','$1' })/releases" -ForegroundColor DarkGray
if ($hasGh) {
    Write-Host "`n  Xem log workflow:" -ForegroundColor DarkGray
    Write-Host "  gh run watch" -ForegroundColor White
    Write-Host "  gh release view $tag --web" -ForegroundColor White
}
Write-Host "========================================" -ForegroundColor Cyan

# Gợi ý tạo release thủ công bằng gh nếu workflow không chạy
Write-Host "`nTip: tạo release thủ công với auto-notes:" -ForegroundColor DarkGray
Write-Host "  gh release create $tag --generate-notes $(if($Prerelease){'--prerelease'})" -ForegroundColor White
