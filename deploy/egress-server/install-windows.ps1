<#
.SYNOPSIS
  Cài gost (SOCKS5 có user/pass) làm điểm thoát mạng cho app trên máy 1 (server 2 chạy Windows).

.DESCRIPTION
  - Tải gost v3 (pin version + kiểm tra SHA-256) vào %ProgramData%\gost-egress.
  - Chạy lúc khởi động bằng Task Scheduler, tài khoản SYSTEM, tự khởi động lại khi chết.
  - Windows Firewall: chỉ cho dải LAN (tham số) + Tailscale 100.64.0.0/10 vào cổng proxy.

.EXAMPLE
  # PowerShell "Run as Administrator":
  .\install-windows.ps1 -LanCidr 192.168.1.0/24
  .\install-windows.ps1 -LanCidr 192.168.1.0/24 -User egress -Pass "mat-khau-dai-12-ky-tu" -Port 1080
  # Bật thêm kho tài khoản Kaggle/YouTube (gost file server, cùng user/pass) ở cổng 1081:
  .\install-windows.ps1 -LanCidr 172.16.0.0/16 -User egress -Pass "mat-khau-dai-12-ky-tu" -EnableAccounts
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$LanCidr,
  [string]$User = "egress",
  [string]$Pass = "",
  [ValidateRange(1, 65535)][int]$Port = 1080,
  [switch]$EnableAccounts,
  [string]$AccountsDir = "",
  [ValidateRange(1, 65535)][int]$AccountsPort = 1081
)
$ErrorActionPreference = "Stop"

$GostVersion = "3.3.3"
# SHA-256 do GitHub công bố cho asset của release v3.3.3 (go-gost/gost).
$GostSha256 = @{
  "amd64" = "b00441624a66ebed94a2ddcc01ab2832069f053e6ca0abf19d7104b84fad1f78"
  "arm64" = "9bdba3d429dbaa1f926b1a2e75a5e35d427129c5ae9dbebcaf1a929d8ab570d6"
}
$Tailscale = "100.64.0.0/10"
$InstallDir = Join-Path $env:ProgramData "gost-egress"
$TaskName = "gost-egress"
$RuleName = "gost-egress (LAN + Tailscale)"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Cần chạy PowerShell bằng quyền Administrator."
}
if ($LanCidr -notmatch '^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$') { throw "-LanCidr phải là CIDR IPv4, vd. 192.168.1.0/24" }
$generated = $false
if (-not $Pass) {
  $bytes = New-Object byte[] 18
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $Pass = -join ($bytes | ForEach-Object { $_.ToString("x2") })
  $generated = $true
}
# User/pass nằm trong URL socks5://user:pass@... nên chỉ nhận ký tự an toàn cho URL.
if ($User -notmatch '^[A-Za-z0-9._-]+$') { throw "-User chỉ gồm A-Z a-z 0-9 . _ -" }
if ($Pass -notmatch '^[A-Za-z0-9._-]{12,}$') { throw "-Pass tối thiểu 12 ký tự, chỉ gồm A-Z a-z 0-9 . _ -" }

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "arm64" } else { "amd64" }
$asset = "gost_${GostVersion}_windows_${arch}.zip"
$url = "https://github.com/go-gost/gost/releases/download/v$GostVersion/$asset"
# Tải vào thư mục cố định bên trong thư mục cài (không phải %TEMP% ngẫu nhiên): Microsoft
# Defender hay xếp gost (công cụ proxy/tunnel) vào "HackTool/PUA", và khi đó chỉ cần MỘT
# ngoại lệ cho $InstallDir là đủ cho cả file tải về lẫn gost.exe.
$tmp = Join-Path $InstallDir "_download"
Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$defenderHint = @"
Microsoft Defender đã chặn/cách ly file gost (thường bị xếp loại HackTool/PUA vì là công cụ proxy).
Xem tên mối đe doạ:   Get-MpThreatDetection | Sort-Object InitialDetectionTime | Select-Object -Last 1
Nếu đúng là gost và bạn chấp nhận, thêm ngoại lệ CHỈ cho thư mục cài rồi chạy lại script:
  Add-MpPreference -ExclusionPath "$InstallDir"
Checksum SHA-256 vẫn được kiểm tra nên file tải về phải đúng bản v$GostVersion chính thức.
"@
try {
  Write-Host "==> Tải $url"
  [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
  Invoke-WebRequest -Uri $url -OutFile (Join-Path $tmp $asset) -UseBasicParsing
  try {
    $hash = (Get-FileHash -Algorithm SHA256 (Join-Path $tmp $asset) -ErrorAction Stop).Hash.ToLowerInvariant()
  } catch {
    throw "Không đọc được file vừa tải: $($_.Exception.Message)`n$defenderHint"
  }
  if ($hash -ne $GostSha256[$arch]) { throw "Checksum KHÔNG khớp ($hash) — dừng, không cài." }
  Expand-Archive -Path (Join-Path $tmp $asset) -DestinationPath (Join-Path $tmp "x") -Force
  $exe = Get-ChildItem -Path (Join-Path $tmp "x") -Recurse -Filter "gost.exe" | Select-Object -First 1
  if (-not $exe) { throw "Không thấy gost.exe trong $asset (có thể Defender đã xoá khi giải nén).`n$defenderHint" }

  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Get-Process -Name gost -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallDir*" } | Stop-Process -Force
  New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
  Copy-Item $exe.FullName (Join-Path $InstallDir "gost.exe") -Force
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
$gostExe = Join-Path $InstallDir "gost.exe"
# Chỉ SYSTEM và Administrators đọc được thư mục (mật khẩu nằm trong định nghĩa task).
icacls $InstallDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null

# ------------------------------------------------------------------ kho tài khoản (tuỳ chọn)
$ports = @($Port)
$gostArgs = "-L `"socks5://${User}:${Pass}@:${Port}`""
if ($EnableAccounts) {
  if (-not $AccountsDir) { $AccountsDir = Join-Path $InstallDir "accounts" }
  if ($AccountsDir -match '[\s?&#"]') { throw "-AccountsDir không được chứa khoảng trắng hoặc ? & # `"" }
  foreach ($sub in "kaggle", "youtube") { New-Item -ItemType Directory -Force -Path (Join-Path $AccountsDir $sub) | Out-Null }
  if ($AccountsDir -notlike "$InstallDir*") {
    # Thư mục ngoài InstallDir: tự khoá quyền, vì trong đó là API key / refresh token.
    icacls $AccountsDir /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
  }
  $dir = $AccountsDir -replace '\\', '/'
  $gostArgs += " -L `"file://${User}:${Pass}@:${AccountsPort}?dir=$dir`""
  $ports += $AccountsPort
}

# ------------------------------------------------------------------ firewall
# Gỡ mọi luật cũ trỏ tới gost.exe (kể cả luật "Allow" Windows tự tạo khi hỏi lần đầu):
# một luật allow-any khác sẽ mở cổng cho cả Internet.
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
  Where-Object { $_.Program -eq $gostExe } | Get-NetFirewallRule | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP `
  -LocalPort $ports -RemoteAddress @($LanCidr, $Tailscale) -Program $gostExe -Profile Any | Out-Null
Write-Host "==> Windows Firewall: chỉ $LanCidr + $Tailscale vào được cổng $($ports -join ', ')"

# ------------------------------------------------------------------ chạy lúc khởi động
$action = New-ScheduledTaskAction -Execute $gostExe -Argument $gostArgs -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $taskPrincipal -Description "gost SOCKS5 egress cho epub-audiobook-app" | Out-Null
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 2
if (-not (Get-Process -Name gost -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $gostExe })) {
  throw "gost không chạy. Xem Task Scheduler → $TaskName → History."
}

$lanIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -notlike "100.*" } |
  Select-Object -First 1).IPAddress
$tsIp = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -like "100.*" } | Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "==> gost-egress đang chạy trên cổng $Port."
if ($generated) { Write-Host "    Mật khẩu sinh ngẫu nhiên (lưu lại): $Pass" }
Write-Host "    Thêm 2 endpoint trong app (Mạng & Server → Thêm endpoint):"
Write-Host ("      socks5h://{0}:<mật khẩu>@{1}:{2}" -f $User, ($(if ($lanIp) { $lanIp } else { "<IP-LAN>" })), $Port)
Write-Host ("      socks5h://{0}:<mật khẩu>@{1}:{2}" -f $User, ($(if ($tsIp) { $tsIp } else { "<IP-Tailscale-100.x>" })), $Port)
if ($EnableAccounts) {
  Write-Host ""
  Write-Host "==> Kho tài khoản: $AccountsDir (cổng $AccountsPort, cùng user/pass)."
  Write-Host "    Đặt file vào:  $AccountsDir\kaggle\<tên>.json   {""username"": ""..."", ""key"": ""...""}"
  Write-Host "                   $AccountsDir\youtube\<tên>.json  {""refresh_token"": ""..."", ""channel_id"": ""UC...""}"
  Write-Host "    Trong app (Mạng & Server → Kho tài khoản trên server 2), URL:"
  Write-Host ("      http://{0}:{1}" -f ($(if ($tsIp) { $tsIp } else { "<IP-Tailscale-100.x>" })), $AccountsPort)
  Write-Host ("      http://{0}:{1}" -f ($(if ($lanIp) { $lanIp } else { "<IP-LAN>" })), $AccountsPort)
}
