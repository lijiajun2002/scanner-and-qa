@echo off
setlocal
cd /d "%~dp0"

set "LOG=%~dp0build_log.txt"
set "VENV=.build-venv"
set "VPY=%VENV%\Scripts\python.exe"

echo === ScannerQA-Agent build === > "%LOG%"

rem ===== locate uv =====
set "UV="
where uv >nul 2>nul
if not errorlevel 1 set "UV=uv"
if not defined UV if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV if exist "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe" set "UV=%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
if defined UV goto :have_uv

echo [i] uv not found, installing uv now. It brings its own Python.
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex" >> "%LOG%" 2>&1
if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV=%USERPROFILE%\.local\bin\uv.exe"

:have_uv
echo.
if not defined UV echo [x] uv is not installed.
if not defined UV echo     run this, then reopen the terminal and retry:
if not defined UV echo       powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
if not defined UV echo.
if not defined UV echo     log: %LOG%
if not defined UV pause
if not defined UV exit /b 1

echo [i] builder: %UV%
echo. >> "%LOG%"

rem ===== [1/4] clean venv =====
echo === [1/4] create clean venv %VENV% ===
echo [1/4] create venv >> "%LOG%"
if exist "%VENV%" rmdir /s /q "%VENV%" >nul
"%UV%" venv "%VENV%" >> "%LOG%" 2>&1
if not exist "%VPY%" echo [x] venv create failed, see "%LOG%"
if not exist "%VPY%" pause
if not exist "%VPY%" exit /b 1

rem ===== [2/4] deps =====
echo === [2/4] install deps + pyinstaller ===
echo [2/4] install deps >> "%LOG%"
"%UV%" pip install --python "%VPY%" -r requirements.txt pyinstaller >> "%LOG%" 2>&1

rem ===== [3/4] build =====
echo === [3/4] build exe ===
echo [3/4] build exe >> "%LOG%"
"%VPY%" -m PyInstaller --noconfirm --clean capture_agent.spec >> "%LOG%" 2>&1
set "RC=%ERRORLEVEL%"

rem ===== [4/4] default config =====
echo === [4/4] copy default config ===
echo [4/4] copy config >> "%LOG%"
if not exist "dist\config.json" copy "config.example.json" "dist\config.json" >nul

echo.
if not "%RC%"=="0" echo [x] PyInstaller failed with code %RC%, see "%LOG%"
if "%RC%"=="0" if exist "dist\ScannerQA-Agent.exe" echo [ok] built: %~dp0dist\ScannerQA-Agent.exe
if "%RC%"=="0" if not exist "dist\ScannerQA-Agent.exe" echo [x] dist\ScannerQA-Agent.exe not found, see "%LOG%"
echo.
echo full log: %LOG%
pause
endlocal
