@echo off
rem 在交互桌面会话里用 venv 的 python 跑 agent_server。
rem 注意：这台机器的 Device Guard 会拦截新建的无签名 exe 和 pythonw.exe，故用 python.exe。
cd /d "%~dp0"
set "SQA_AGENT_CONFIG=%~dp0dist\config.json"
start "sqa-server" /min "%~dp0.build-venv\Scripts\python.exe" "%~dp0agent_server.py"
