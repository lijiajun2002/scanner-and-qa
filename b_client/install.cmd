@echo off
rem ===== SQA Target one-click installer (double-click to run) =====
rem Downloads SQA-Agent-Server.exe + setup_server.ps1 from GitHub Releases, then installs.
rem Private repo: set env var SQA_GH_TOKEN (a PAT with read access) before running.
setlocal
set "REPO=lijiajun2002/scanner-and-qa"
set "DEST=%TEMP%\sqa-install"

echo ============================================
echo   SQA Target one-click installer
echo   repo: %REPO%
echo ============================================
echo.

set /p TOKEN=Enter the token (must match the Controller; Enter = admin): 
if "%TOKEN%"=="" set "TOKEN=admin"
set /p PORT=Enter the port (Enter = 8600): 
if "%PORT%"=="" set "PORT=8600"

echo.
echo [1/2] Downloading from GitHub...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$repo='%REPO%'; $tok=$env:SQA_GH_TOKEN; $dest='%DEST%';" ^
  "New-Item -ItemType Directory -Force -Path $dest | Out-Null;" ^
  "$h=@{'User-Agent'='sqa-installer';'Accept'='application/vnd.github+json'};" ^
  "if($tok){$h['Authorization']='Bearer '+$tok};" ^
  "$rel=Invoke-RestMethod -Headers $h ('https://api.github.com/repos/'+$repo+'/releases/latest');" ^
  "foreach($a in $rel.assets){ if($a.name -eq 'SQA-Agent-Server.exe' -or $a.name -eq 'setup_server.ps1'){ Invoke-WebRequest -Headers $h -Uri $a.browser_download_url -OutFile (Join-Path $dest $a.name) } };" ^
  "if(-not (Test-Path (Join-Path $dest 'SQA-Agent-Server.exe'))){ throw 'SQA-Agent-Server.exe not found (private repo? set SQA_GH_TOKEN; or the Release has no exe asset yet)' };" ^
  "if(-not (Test-Path (Join-Path $dest 'setup_server.ps1'))){ throw 'setup_server.ps1 not found' };" ^
  "Write-Host ('Downloaded to ' + $dest)"
if errorlevel 1 (
  echo.
  echo [x] Download failed. Check network / repo / SQA_GH_TOKEN.
  pause
  exit /b 1
)

echo.
echo [2/2] Installing and starting...
powershell -NoProfile -ExecutionPolicy Bypass -File "%DEST%\setup_server.ps1" -Token "%TOKEN%" -Port "%PORT%"
endlocal
