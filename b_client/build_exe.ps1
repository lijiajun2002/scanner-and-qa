$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$log = Join-Path $PSScriptRoot 'build_log.txt'
try { Start-Transcript -Path $log -Force | Out-Null } catch { }

function Fail($msg) {
    Write-Host "[x] $msg"
    Write-Host "    log: $log"
    try { Stop-Transcript | Out-Null } catch { }
    Read-Host "Press Enter to exit"
    exit 1
}

function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $paths = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe')
    )
    foreach ($p in $paths) { if (Test-Path $p) { return $p } }
    return $null
}

$uv = Find-Uv
if (-not $uv) {
    Write-Host "[i] uv not found, installing uv (it brings its own Python)..."
    try {
        $installer = Invoke-RestMethod -Uri 'https://astral.sh/uv/install.ps1' -UseBasicParsing
        Invoke-Expression $installer
    } catch {
        Write-Host "[!] auto install failed: $($_.Exception.Message)"
    }
    $uv = Find-Uv
}
if (-not $uv) {
    Fail "uv is not installed. Install it, then re-run:`n    powershell -c `"irm https://astral.sh/uv/install.ps1 | iex`"`n    (close and reopen the terminal afterwards)"
}
Write-Host "[i] builder: $uv"

$venv = Join-Path $PSScriptRoot '.build-venv'
$vpy  = Join-Path $venv 'Scripts\python.exe'

Write-Host "`n=== [1/4] create clean venv ==="
if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
& $uv venv $venv
if (-not (Test-Path $vpy)) { Fail "venv create failed, $vpy not found" }

Write-Host "`n=== [2/4] install deps + pyinstaller ==="
& $uv pip install --python $vpy -r requirements.txt pyinstaller
if ($LASTEXITCODE -ne 0) { Fail "dependency install failed" }

Write-Host "`n=== [3/4] build exe ==="
& $vpy -m PyInstaller --noconfirm --clean capture_agent.spec
$rc = $LASTEXITCODE

Write-Host "`n=== [4/4] copy default config ==="
$dist = Join-Path $PSScriptRoot 'dist'
$cfg = Join-Path $dist 'config.json'
if (-not (Test-Path $cfg)) { Copy-Item (Join-Path $PSScriptRoot 'config.example.json') $cfg }

$exePath = Join-Path $dist 'ScannerQA-Agent.exe'
if ($rc -eq 0 -and (Test-Path $exePath)) {
    Write-Host "[ok] built: $exePath" -ForegroundColor Green
} else {
    Write-Host "[x] PyInstaller failed with code $rc, see $log" -ForegroundColor Red
}

Write-Host "full log: $log"
try { Stop-Transcript | Out-Null } catch { }
Read-Host "Press Enter to exit"
