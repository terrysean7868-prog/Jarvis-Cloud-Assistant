# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

_WEBVIEW_HIDDENIMPORTS = []
_WEBVIEW_DATAS = []
_WEBVIEW_BINARIES = []
try:
    _WEBVIEW_DATAS, _WEBVIEW_BINARIES, _WEBVIEW_HIDDENIMPORTS = collect_all('webview')
except Exception:
    _WEBVIEW_HIDDENIMPORTS = ['webview']


a = Analysis(
    ['apps\\desktop\\desktop_app.py'],
    # Add repo root so the packaged exe can import app.py and src/*.
    pathex=['.'],
    binaries=[] + (_WEBVIEW_BINARIES or []),
    datas=[
        ('assets\\jarvis.ico', 'assets'),
        # Include the React production build so the desktop backend can serve it locally.
        ('jarvis-frontend\\build', 'jarvis-frontend\\build'),
    ] + (_WEBVIEW_DATAS or []),
    hiddenimports=['app'] + _WEBVIEW_HIDDENIMPORTS,
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
    name='Jarvis',
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
