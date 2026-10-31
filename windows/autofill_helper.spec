# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the staging TileArc Auto-Fill helper.

Build from the repository root:

    pyinstaller --noconfirm --clean windows/autofill_helper.spec

The hosted FastAPI service is not collected. Employees get the loopback helper.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

root = Path(SPECPATH).resolve().parent
block_cipher = None

playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")
greenlet_datas, greenlet_binaries, greenlet_hidden = collect_all("greenlet")
pyee_datas, pyee_binaries, pyee_hidden = collect_all("pyee")

import playwright

driver = Path(playwright.__file__).resolve().parent / "driver"

datas = []
datas += playwright_datas
datas += greenlet_datas
datas += pyee_datas
datas += collect_data_files("autofill")
datas += [
    (str(root / "LICENSE"), "."),
    (str(root / "NOTICE"), "."),
]
if driver.is_dir():
    datas.append((str(driver), "playwright/driver"))

hidden = [
    name
    for name in collect_submodules("autofill")
    if not name.startswith("autofill.service")
]
hidden += playwright_hidden
hidden += greenlet_hidden
hidden += pyee_hidden
hidden += ["playwright.sync_api", "greenlet", "pyee"]

a = Analysis(
    [str(root / "src" / "autofill" / "win_launcher.py")],
    pathex=[str(root / "src")],
    binaries=playwright_binaries + greenlet_binaries + pyee_binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(root / "windows" / "pyi_rth_playwright.py")],
    excludes=["autofill.service", "fastapi", "uvicorn", "multipart"],
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
    name="TileArcAutoFillHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="TileArcAutoFillHelper",
)
