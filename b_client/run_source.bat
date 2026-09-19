@echo off
cd /d "%~dp0"
if exist ".build-venv\Scripts\pythonw.exe" (
    start "" ".build-venv\Scripts\pythonw.exe" capture_agent.py
) else (
    start "" pythonw capture_agent.py
)
