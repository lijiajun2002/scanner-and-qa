@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist "ScannerQA-Agent.exe" ScannerQA-Agent.exe
if not exist "ScannerQA-Agent.exe" python capture_agent.py
echo.
pause
