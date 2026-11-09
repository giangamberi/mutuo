# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Shared Knowledge desktop app.

Builds a onedir bundle (dist/SharedKnowledge/SharedKnowledge.exe) that
contains:
  - the GUI (desktop.py) and the FastAPI server (app.py) — the same exe is
    re-spawned with ``--server <profile>`` to run the server,
  - the read-only app files (static/, plugins/),
  - the native deps PyInstaller can't always find (libtorrent .pyd, pdfium.dll,
    pywebview's js/ + WebView2 loader dlls).

onedir (not onefile) on purpose: the server subprocess re-spawns the exe, and
onefile would re-extract the whole bundle to temp on every launch.

Mutable data (profiles/) is NOT bundled — at runtime it lives in
%APPDATA%/SharedKnowledge (see desktop.py _data_dir()).

Build:  pyinstaller SharedKnowledge.spec --noconfirm
"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# native / data-heavy packages: pull in their binaries + data files wholesale
webview_datas, webview_bins, webview_hidden = collect_all("webview")
pypdfium2_datas, pypdfium2_bins, pypdfium2_hidden = collect_all("pypdfium2")
pypdfium2raw_datas, pypdfium2raw_bins, pypdfium2raw_hidden = collect_all("pypdfium2_raw")
libtorrent_datas, libtorrent_bins, libtorrent_hidden = collect_all("libtorrent")

datas = (
    webview_datas + pypdfium2_datas + pypdfium2raw_datas + libtorrent_datas +
    # the app's own read-only files, served by the FastAPI app
    [("static", "static"), ("plugins", "plugins")]
)
binaries = webview_bins + pypdfium2_bins + pypdfium2raw_bins + libtorrent_bins

hiddenimports = (
    webview_hidden + pypdfium2_hidden + pypdfium2raw_hidden + libtorrent_hidden +
    # pywebview picks its backend at runtime (edgechromium on Windows)
    collect_submodules("webview.platforms") +
    # the server is imported by name (uvicorn.run("app:app")) and the app's
    # own modules
    ["app", "catalog", "torrent_engine", "plugin_loader", "profiles",
     "uvicorn", "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
     "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
     "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
     "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
     "fastapi", "libtorrent", "pypdfium2", "pypdfium2.raw"]
)

a = Analysis(
    ["desktop.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "scipy", "PIL.tests"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SharedKnowledge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI app: no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="SharedKnowledge",
)
