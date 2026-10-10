# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec：Target 端 HTTP 服务（无控制台单文件）
# 用法（b_client 目录）：pyinstaller --noconfirm --clean agent_server.spec

a = Analysis(
    ['agent_server.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=['capture_agent'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'numpy', 'pytest', 'matplotlib'],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SQA-Agent-Server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
