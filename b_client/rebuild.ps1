param([switch]$Force)
$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$log = Join-Path $PSScriptRoot 'build_log.txt'
try { Start-Transcript -Path $log -Force | Out-Null } catch { }

function Find-Uv {
    $c = Get-Command uv -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\uv.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\uv\uv.exe'))) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

$venv = Join-Path $PSScriptRoot '.build-venv'
$vpy  = Join-Path $venv 'Scripts\python.exe'

if ($Force -or -not (Test-Path $vpy)) {
    $uv = Find-Uv
    if (-not $uv) {
        Write-Host "[x] uv not found. Run build_exe.ps1 first to create the environment." -ForegroundColor Red
        try { Stop-Transcript | Out-Null } catch { }
        exit 1
    }
    Write-Host "[i] creating virtualenv (first run or -Force)"
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $uv venv $venv
    & $uv pip install --python $vpy -r requirements.txt pyinstaller
} else {
    Write-Host "[i] reusing existing .build-venv (use -Force to recreate)"
    $uv = Find-Uv
    if ($uv) { & $uv pip install -q --python $vpy -r requirements.txt pyinstaller }
}

Write-Host "=== incremental build (no --clean, reuse build cache) ==="
& $vpy -m PyInstaller --noconfirm capture_agent.spec
$rc = $LASTEXITCODE

$dist = Join-Path $PSScriptRoot 'dist'
$cfg = Join-Path $dist 'config.json'
if (-not (Test-Path $cfg)) { Copy-Item (Join-Path $PSScriptRoot 'config.example.json') $cfg }

$exe = Join-Path $dist 'ScannerQA-Agent.exe'
if ($rc -eq 0 -and (Test-Path $exe)) {
    Write-Host "[ok] updated: $exe" -ForegroundColor Green
} else {
    Write-Host "[x] build failed (code $rc), see $log" -ForegroundColor Red
}
try { Stop-Transcript | Out-Null } catch { }
