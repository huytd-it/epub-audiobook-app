<#
.SYNOPSIS
  Gỡ gost-egress do install-windows.ps1 cài: task khởi động, tiến trình, luật firewall, thư mục.
.EXAMPLE
  # PowerShell "Run as Administrator":
  .\uninstall-windows.ps1
#>
$ErrorActionPreference = "Stop"
$InstallDir = Join-Path $env:ProgramData "gost-egress"
$gostExe = Join-Path $InstallDir "gost.exe"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  throw "Cần chạy PowerShell bằng quyền Administrator."
}

Stop-ScheduledTask -TaskName "gost-egress" -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName "gost-egress" -Confirm:$false -ErrorAction SilentlyContinue
Get-Process -Name gost -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $gostExe } | Stop-Process -Force
Get-NetFirewallRule -DisplayName "gost-egress (LAN + Tailscale)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
  Where-Object { $_.Program -eq $gostExe } | Get-NetFirewallRule | Remove-NetFirewallRule
Remove-Item -Recurse -Force $InstallDir -ErrorAction SilentlyContinue
Write-Host "==> Đã gỡ gost-egress."
