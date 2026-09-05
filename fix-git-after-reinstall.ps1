#Requires -Version 5.1
<#
  Fix cac loi git thuong gap sau khi cai lai may Windows
  (vi du: VS Code bao "Bulk stage/unstage failed", git bao "dubious ownership",
  "Permission denied" khi ghi .git/config hoac .git/index, ...)

  Nguyen nhan pho bien nhat: sau khi cai lai Windows, tai khoan user duoc tao lai
  voi SID moi, nhung cac file trong repo (o o dia con lai, vi du D:\) van thuoc
  owner la SID cu -> ghi vao .git bi tu choi quyen du icacls nhin co ve binh thuong.

  Cach dung: copy file nay vao thu muc goc cua project (noi co .git),
  roi chay:  powershell -ExecutionPolicy Bypass -File .\fix-git-after-reinstall.ps1
  hoac click phai -> Run with PowerShell.
  Script se tu xin quyen Administrator (UAC) vi buoc lay lai quyen so huu file
  can quyen admin.
#>

$ErrorActionPreference = 'Stop'

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "    OK   $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "    WARN $msg" -ForegroundColor Yellow }
function Err($msg)  { Write-Host "    LOI  $msg" -ForegroundColor Red }

# 0. Tu nang quyen Administrator neu chua co (can cho buoc lay lai quyen so huu file)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Script can quyen Administrator de sua loi quyen so huu file (.git bi khoa sau khi cai lai may)." -ForegroundColor Yellow
    Write-Host "Dang mo lai voi quyen Administrator (se hien popup UAC, bam Yes)..." -ForegroundColor Yellow
    try {
        Start-Process powershell -Verb RunAs -ArgumentList @(
            '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$($MyInvocation.MyCommand.Path)`""
        ) | Out-Null
    } catch {
        Err "Khong the tu nang quyen (co the ban bam Cancel o UAC). Hay chuot phai vao file .ps1 nay -> Run with PowerShell as Administrator."
    }
    exit
}

# 1. Kiem tra git co san
Step "Kiem tra git da cai chua"
$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    Err "Khong tim thay git.exe trong PATH. Cai lai Git for Windows roi chay lai script nay."
    Read-Host "Nhan Enter de dong"
    exit 1
}
Ok "git: $($gitCmd.Source) ($(git --version))"

# 2. Xac dinh repo root tu vi tri script dang chay
Step "Xac dinh repo git"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $scriptDir
$repoRoot = $null
try {
    $repoRoot = (git rev-parse --show-toplevel 2>$null)
} catch {}
Pop-Location

if (-not $repoRoot) {
    Err "Khong tim thay repo git tai $scriptDir. Hay dat script nay vao thu muc goc project (co folder .git) roi chay lai."
    Read-Host "Nhan Enter de dong"
    exit 1
}
$repoRoot = ($repoRoot -replace '/', '\').Trim()
Ok "Repo: $repoRoot"

Push-Location $repoRoot

# 3. Lay lai quyen so huu (owner SID cu sau khi cai lai may -> ghi .git bi Permission denied)
Step "Kiem tra owner cua thu muc repo"
$currentSid = ([Security.Principal.WindowsIdentity]::GetCurrent()).User.Value
$ownerSid = $null
try {
    $acl = Get-Acl -Path $repoRoot
    $ownerSid = $acl.Owner
    if ($ownerSid -notmatch '^S-1-5-21-') { $ownerSid = ([System.Security.Principal.NTAccount]$ownerSid).Translate([System.Security.Principal.SecurityIdentifier]).Value }
} catch {}

if ($ownerSid -and $ownerSid -ne $currentSid) {
    Warn "Owner hien tai ($ownerSid) khac voi user dang dung ($currentSid) - day thuong la nguyen nhan chinh."
    Step "Lay lai quyen so huu + cap quyen Full Control cho toan bo repo (co the mat vai giay)"
    & takeown /F "$repoRoot" /R /D Y *> $null
    & icacls "$repoRoot" /reset /T /C *> $null
    & icacls "$repoRoot" /grant "$($env:USERNAME):(OI)(CI)F" /T /C *> $null
    Ok "Da lay lai quyen so huu va cap Full Control cho $env:USERNAME"
} else {
    Ok "Owner da khop voi user hien tai, khong can doi quyen"
}

# 4. safe.directory - loi "detected dubious ownership" hay gap sau khi cai lai may / doi user Windows
Step "Kiem tra git safe.directory"
$safeDirs = @(git config --global --get-all safe.directory 2>$null)
if ($safeDirs -notcontains $repoRoot) {
    git config --global --add safe.directory $repoRoot
    Ok "Da them safe.directory: $repoRoot"
} else {
    Ok "safe.directory da duoc cau hinh"
}

# 5. core.longpaths - loi bulk stage/unstage that bai khi duong dan file qua dai (node_modules, ...)
Step "Bat core.longpaths cho git"
git config --global core.longpaths true
Ok "core.longpaths=true (global)"

Step "Bat Windows LongPathsEnabled"
try {
    $fsKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem'
    $current = (Get-ItemProperty -Path $fsKey -Name 'LongPathsEnabled' -ErrorAction SilentlyContinue).LongPathsEnabled
    if ($current -ne 1) {
        Set-ItemProperty -Path $fsKey -Name 'LongPathsEnabled' -Value 1
        Warn "Da bat LongPathsEnabled - can khoi dong lai may de ap dung day du"
    } else {
        Ok "LongPathsEnabled da bat san"
    }
} catch {
    Warn "Khong the doi registry LongPathsEnabled. Bo qua."
}

# 6. Xoa file lock cu con sot lai (khien moi lenh git bi treo/that bai)
Step "Kiem tra file .lock cu trong .git"
$lockFiles = Get-ChildItem -Path (Join-Path $repoRoot '.git') -Filter '*.lock' -Recurse -ErrorAction SilentlyContinue
if ($lockFiles) {
    foreach ($f in $lockFiles) {
        Remove-Item $f.FullName -Force
        Ok "Da xoa lock cu: $($f.FullName)"
    }
} else {
    Ok "Khong co file lock cu"
}

# 7. core.filemode - Windows khong theo doi quyen thuc thi file, tranh moi file bi bao "modified" sai
Step "Kiem tra core.filemode"
git config core.filemode false
Ok "core.filemode=false (repo nay)"

# 8. user.name / user.email - can co de commit, hay bi mat sau khi cai lai may
Step "Kiem tra user.name / user.email"
$name = git config --global user.name
$email = git config --global user.email
if (-not $name -or -not $email) {
    Warn "Chua cau hinh user.name/user.email global."
    Warn "Chay tay: git config --global user.name `"Ten cua ban`""
    Warn "         git config --global user.email `"email@vi-du.com`""
} else {
    Ok "user.name=$name / user.email=$email"
}

# 9. credential.helper - can de push/pull khong bi hoi mat khau lien tuc
Step "Kiem tra credential.helper"
$cred = git config --global credential.helper
if (-not $cred) {
    git config --global credential.helper manager
    Ok "Da dat credential.helper=manager"
} else {
    Ok "credential.helper=$cred"
}

# 10. Kiem tra lai: ghi thu vao config va bulk add thu (khong thay doi gi thuc su)
Step "Kiem tra ghi .git/config"
git config --local --get core.filemode | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "Doc/ghi .git/config binh thuong" } else { Err "Van khong ghi duoc .git/config" }

Step "Kiem tra bulk stage/unstage bang git add --dry-run -A"
git add --dry-run -A 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "git add -A chay khong loi" } else { Err "git add -A van bao loi" }

Pop-Location

Write-Host ""
Write-Host "Hoan tat. Neu VS Code / IDE van bao 'Bulk stage/unstage failed':" -ForegroundColor Green
Write-Host "  - Dong het cua so VS Code va mo lai project (reload window)."
Write-Host "  - Neu vua bat LongPathsEnabled, khoi dong lai may."
Write-Host "  - Neu van loi, copy nguyen van thong bao loi de kiem tra tiep."
Read-Host "Nhan Enter de dong"
