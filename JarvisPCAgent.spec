# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# Bundle pywebview completely (hidden imports + required dll/data) so
# the EdgeChromium backend works in frozen builds.
hiddenimports = []
webview_datas = []
webview_binaries = []
try:
    webview_datas, webview_binaries, hiddenimports = collect_all('webview')
except Exception:
    pass

a = Analysis(
    ['pc_agent_app.py'],
    pathex=[],
    binaries=[] + (webview_binaries or []),
    datas=[('assets\\jarvis.ico', 'assets'), ('assets\\pc_agent_ui.html', 'assets')] + (webview_datas or []),
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='JarvisPCAgent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['assets\\jarvis.ico'],
)
