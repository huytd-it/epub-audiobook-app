#Requires -Version 5.1
<#
.SYNOPSIS
  Tao tag va push de trigger GitHub Release tu dong.
.DESCRIPTION
  1. Dong bo version vao package.json / pyproject.toml / src-tauri/tauri.conf.json
  2. Commit, tao tag vX.Y.Z, push + push tag => kich hoat .github/workflows/release.yml
  3. Release notes duoc GitHub tu dong generate tu .github/release.yml
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

# Chuan hoa version: cho phep "1.0.1" hoac "v1.0.1"
$raw = $Version.Trim()
if ($raw.StartsWith("v")) { $raw = $raw.Substring(1) }
$tag = "v$raw"
$semver = $raw  # khong co v

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Create Release $tag" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Kiem tra gh CLI (optional)
$hasGh = $null -ne (Get-Command gh -ErrorAction SilentlyContinue)

# 1. Cap nhat version trong 3 file
Write-Host "`n[1/4] Updating version to $semver ..." -ForegroundColor Yellow

# package.json -- regex replace to keep formatting
$pkgPath = Join-Path $Root "package.json"
$pkgContent = Get-Content $pkgPath -Raw
$pkgContentNew = $pkgContent -replace '"version"\s*:\s*".*?"', "`"version`": `"$semver`""
if ($pkgContentNew -ne $pkgContent) {
    Set-Content -LiteralPath $pkgPath -Value $pkgContentNew -Encoding utf8 -NoNewline
    Write-Host "  [OK] package.json" -ForegroundColor Green
} else {
    Write-Host "  [OK] package.json (already $semver)" -ForegroundColor DarkGray
}

# pyproject.toml
$pyPath = Join-Path $Root "pyproject.toml"
$pyContent = Get-Content $pyPath -Raw
$pyContentNew = $pyContent -replace 'version = ".*?"', "version = `"$semver`""
if ($pyContentNew -ne $pyContent) {
    Set-Content -LiteralPath $pyPath -Value $pyContentNew -Encoding utf8 -NoNewline
    Write-Host "  [OK] pyproject.toml" -ForegroundColor Green
} else {
    Write-Host "  [OK] pyproject.toml (already $semver)" -ForegroundColor DarkGray
}

# src-tauri/tauri.conf.json -- regex replace keep formatting, write without BOM
$tauriPath = Join-Path $Root "src-tauri/tauri.conf.json"
$tauriContent = Get-Content $tauriPath -Raw
$tauriContentNew = $tauriContent -replace '"version"\s*:\s*".*?"', "`"version`": `"$semver`""
if ($tauriContentNew -ne $tauriContent) {
    [System.IO.File]::WriteAllText($tauriPath, $tauriContentNew, [System.Text.UTF8Encoding]::new($false))
    Write-Host "  [OK] src-tauri/tauri.conf.json" -ForegroundColor Green
} else {
    Write-Host "  [OK] src-tauri/tauri.conf.json (already $semver)" -ForegroundColor DarkGray
}

if ($DryRun) {
    Write-Host "`n[DryRun] Dung truoc commit/tag." -ForegroundColor Magenta
    git -C $Root diff --stat
    exit 0
}

# 2. Commit
Write-Host "`n[2/4] Committing ..." -ForegroundColor Yellow
git -C $Root add package.json pyproject.toml src-tauri/tauri.conf.json
$commitMsg = if ($Message) { "chore(release): $tag - $Message" } else { "chore(release): $tag" }
# Neu khong co gi de commit (version trung), bo qua
$staged = git -C $Root diff --cached --name-only
if (-not $staged) {
    Write-Host "  (no changes to commit -- version co the da dung)" -ForegroundColor DarkYellow
} else {
    git -C $Root commit -m $commitMsg
    Write-Host "  [OK] committed: $commitMsg" -ForegroundColor Green
}

# 3. Tag
Write-Host "`n[3/4] Creating tag $tag ..." -ForegroundColor Yellow
$existing = git -C $Root tag --list $tag
if ($existing) {
    Write-Host "  Tag $tag da ton tai -- xoa va tao lai?" -ForegroundColor Yellow
    git -C $Root tag -d $tag
}
$tagMsg = if ($Message) { "$tag - $Message" } else { $tag }
git -C $Root tag -a $tag -m $tagMsg
Write-Host "  [OK] tag $tag" -ForegroundColor Green

if ($SkipPush) {
    Write-Host "`n[SkipPush] Khong push. Chay thu cong:" -ForegroundColor Magenta
    Write-Host "  git push origin master && git push origin $tag" -ForegroundColor White
    exit 0
}

# 4. Push
Write-Host "`n[4/4] Pushing ..." -ForegroundColor Yellow
$branch = (git -C $Root rev-parse --abbrev-ref HEAD).Trim()
git -C $Root push origin $branch
git -C $Root push origin $tag
Write-Host "  [OK] pushed $branch + $tag" -ForegroundColor Green

Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "  Done! GitHub Actions se tao Release" -ForegroundColor Green
$remoteUrl = git -C $Root remote get-url origin
$slug = $remoteUrl -replace '.*github.com[:/](.*)\.git.*','$1'
Write-Host "  https://github.com/$slug/releases" -ForegroundColor DarkGray
if ($hasGh) {
    Write-Host "`n  Xem log workflow:" -ForegroundColor DarkGray
    Write-Host "  gh run watch" -ForegroundColor White
    Write-Host "  gh release view $tag --web" -ForegroundColor White
}
Write-Host "========================================" -ForegroundColor Cyan

# Goi y tao release thu cong bang gh neu workflow khong chay
Write-Host "`nTip: tao release thu cong voi auto-notes:" -ForegroundColor DarkGray
$preFlag = if ($Prerelease) { "--prerelease" } else { "" }
Write-Host "  gh release create $tag --generate-notes $preFlag" -ForegroundColor White
