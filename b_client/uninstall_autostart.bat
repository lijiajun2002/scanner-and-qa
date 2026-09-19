@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'ScannerQA-Agent.lnk') -ErrorAction SilentlyContinue"
echo Autostart removed.
pause
