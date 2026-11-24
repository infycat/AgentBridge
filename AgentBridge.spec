# -*- mode: python ; coding: utf-8 -*-
from importlib.util import find_spec
from os import name as platform_name
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

root = Path(SPECPATH).resolve()
if find_spec('PySide6') is None or find_spec('PySide6.QtWebEngineWidgets') is None:
    raise SystemExit('Qt WebEngine is missing. Install requirements.txt before packaging AgentBridge.')
icon = (root / 'assets' / 'agentbridge.ico').as_posix()
datas = [
    ((root / 'web').as_posix(), 'web'),
    (icon, 'assets'),
    ((root / 'assets' / 'agentbridge.png').as_posix(), 'assets'),
]
binaries = []
hiddenimports = ['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.lifespan.on']

packages = []
websocket_package = next((name for name in ('websockets', 'wsproto') if find_spec(name)), None)
if websocket_package:
    packages.append(websocket_package)
else:
    print('Warning: neither websockets nor wsproto is installed.')
packages += ['mcp.server', 'mcp.shared', 'mcp.types', 'uvicorn', 'starlette', 'anyio']
for package in packages:
    package_data, package_binaries, package_imports = collect_all(package)
    datas.extend(package_data)
    binaries.extend(package_binaries)
    hiddenimports.extend(package_imports)


a = Analysis(
    [(root / 'agentbridge_app.py').as_posix()],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The desktop uses QWidget and WebEngineWidgets. Skip the Python QML
    # integration and its optional plugins; native Qt DLL dependencies remain.
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets'],
    noarchive=False,
    optimize=0,
)
# Use Windows' ICU and native keyboard input. Omit the unrelated virtual
# keyboard plugin and any ICU DLL found on the build machine's PATH.
excluded_binaries = {'icuuc.dll', 'qt6virtualkeyboard.dll', 'qtvirtualkeyboardplugin.dll'}
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in excluded_binaries]
if platform_name == 'nt' and not any(Path(entry[0]).name.lower() == 'qtwebengineprocess.exe' for entry in a.binaries + a.datas):
    raise SystemExit('QtWebEngineProcess.exe was not collected. AgentBridge requires the embedded chat renderer.')
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AgentBridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[icon],
)
# Keep Chromium's sandboxed executable and DLLs in a stable application directory.
release = COLLECT(exe, a.binaries, a.datas, name='AgentBridge')
