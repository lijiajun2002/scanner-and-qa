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
        Write-Host "[x] 未找到 uv，无法创建环境。先运行 build_exe.ps1。" -ForegroundColor Red
        try { Stop-Transcript | Out-Null } catch { }
        exit 1
    }
    Write-Host "[i] 创建虚拟环境（首次或 -Force）"
    if (Test-Path $venv) { Remove-Item -Recurse -Force $venv }
    & $uv venv $venv
    & $uv pip install --python $vpy -r requirements.txt pyinstaller
} else {
    Write-Host "[i] 复用已有 .build-venv（要重建请用 -Force）"
    $uv = Find-Uv
    if ($uv) { & $uv pip install -q --python $vpy -r requirements.txt pyinstaller }
}

Write-Host "=== 增量打包（不加 --clean，复用 build 缓存）==="
& $vpy -m PyInstaller --noconfirm capture_agent.spec
$rc = $LASTEXITCODE

$dist = Join-Path $PSScriptRoot 'dist'
$cfg = Join-Path $dist 'config.json'
if (-not (Test-Path $cfg)) { Copy-Item (Join-Path $PSScriptRoot 'config.example.json') $cfg }

$exe = Join-Path $dist 'ScannerQA-Agent.exe'
if ($rc -eq 0 -and (Test-Path $exe)) {
    Write-Host "[ok] 已更新: $exe" -ForegroundColor Green
} else {
    Write-Host "[x] 打包失败（code $rc），见 $log" -ForegroundColor Red
}
try { Stop-Transcript | Out-Null } catch { }
