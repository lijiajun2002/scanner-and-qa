<#
.SYNOPSIS
  Install and start SQA Agent Server on a Target (Windows) machine.

.DESCRIPTION
  Does the "needs privileges / would pop up" steps automatically:
    - copy the exe to a fixed install dir and Unblock-File (remove MOTW)
    - write config.json (token / port)
    - add a firewall inbound rule (avoids the "allow network access" popup)
    - add a Defender exclusion for the install dir
    - register an ONLOGON scheduled task (interactive session) and start it
    - print the machine's LAN IP

.PARAMETER Port        Listen port (default 8600)
.PARAMETER Token       Auth token (must match the Controller), default admin
.PARAMETER InstallDir  Install directory, default C:\ProgramData\SQA

Usage (run as Administrator; the script self-elevates if needed):
  powershell -ExecutionPolicy Bypass -File setup_server.ps1
  powershell -ExecutionPolicy Bypass -File setup_server.ps1 -Token mytoken -Port 8600
#>
param(
    [int]$Port = 8600,
    [string]$Token = "admin",
    [string]$InstallDir = "C:\ProgramData\SQA"
)

$ErrorActionPreference = "Stop"

# ---- self-elevate ----
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[i] Requesting administrator privileges..."
    $argv = "-ExecutionPolicy Bypass -File `"$PSCommandPath`" -Port $Port -Token `"$Token`" -InstallDir `"$InstallDir`""
    Start-Process powershell -Verb RunAs -ArgumentList $argv
    exit
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exeSrc = Join-Path $here "SQA-Agent-Server.exe"
if (-not (Test-Path $exeSrc)) { $exeSrc = Join-Path $here "dist\SQA-Agent-Server.exe" }
if (-not (Test-Path $exeSrc)) {
    Write-Host "[x] SQA-Agent-Server.exe not found (looked next to this script and in .\dist)."
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[1/6] Installing to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$exeDst = Join-Path $InstallDir "SQA-Agent-Server.exe"
Copy-Item $exeSrc $exeDst -Force
Unblock-File $exeDst
Get-ChildItem $InstallDir -File | Unblock-File -ErrorAction SilentlyContinue

Write-Host "[2/6] Writing config.json (port=$Port token=$Token)"
$cfg = @{
    port         = $Port
    token        = $Token
    monitor      = 1
    jpeg_quality = 85
    scale        = 1.0
    hotkeys      = @{
        prompt_screenshot = "ctrl+shift+alt+8"
        stop_typing       = "ctrl+shift+alt+0"
    }
} | ConvertTo-Json -Depth 4
Set-Content -Path (Join-Path $InstallDir "config.json") -Value $cfg -Encoding UTF8

Write-Host "[3/6] Firewall inbound rule (TCP $Port)"
$ruleName = "SQA Agent Server ($Port)"
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $Port -Profile Any | Out-Null

Write-Host "[4/6] Defender exclusion for install dir"
try { Add-MpPreference -ExclusionPath $InstallDir -ErrorAction Stop }
catch { Write-Host "    (skipped: $($_.Exception.Message))" }

Write-Host "[5/6] Register ONLOGON task and start"
$taskName = "SQAServer"
schtasks /End /TN $taskName 2>$null | Out-Null
schtasks /Delete /TN $taskName /F 2>$null | Out-Null
$user = "$env:USERDOMAIN\$env:USERNAME"
schtasks /Create /TN $taskName /TR "`"$exeDst`"" /SC ONLOGON /RU $user /IT /F | Out-Null
Get-Process SQA-Agent-Server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
schtasks /Run /TN $taskName | Out-Null
Start-Sleep -Seconds 4

Write-Host "[6/6] Health check"
try {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
    Write-Host "    OK: $($r | ConvertTo-Json -Compress)"
} catch {
    Write-Host "    [!] local self-check failed: $($_.Exception.Message)"
    Write-Host "        If Device Guard / WDAC blocks the unsigned exe, ask IT for an allow-list,"
    Write-Host "        or run from source: python agent_server.py"
}

$ips = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -ExpandProperty IPAddress)
Write-Host ""
Write-Host "Done. On the Controller set:" -ForegroundColor Green
Write-Host "  SQA_TARGET_URL=http://<one-of-the-IPs-below>:$Port"
Write-Host "  SQA_TARGET_TOKEN=$Token"
Write-Host ("  IPs: " + ($ips -join ", "))
Read-Host "Press Enter to exit"
