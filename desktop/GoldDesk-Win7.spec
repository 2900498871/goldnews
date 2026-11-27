# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格（Win7 兼容版）：与 GoldDesk.spec 相同，仅去掉 PyInstaller 6 才有的 optimize 参数。

在 v2/ 目录执行（必须用 Python 3.8 + PyInstaller 5.13.1 的虚拟环境）：
  C:\SoftWare\Python38\venv-golddesk\Scripts\python.exe -X utf8 -m PyInstaller --noconfirm --clean ^
      --distpath dist\win7 --workpath build\win7 desktop\GoldDesk-Win7.spec
产物：dist\win7\GoldDesk.exe（Python 3.8.10 运行时，可在 Windows 7 SP1 上运行）。
"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_all

HERE = SPECPATH                       # v2/desktop
V2 = os.path.dirname(HERE)            # v2
hidden = collect_submodules("engine") + ["server", "launcher",
    "webview.platforms.winforms", "webview.platforms.edgechromium",
    "clr_loader", "clr_loader.netfx"]
binaries, datas = [], []
for pkg in ("webview", "pythonnet", "clr_loader"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hidden += h
datas += [(os.path.join(HERE, "ui"), "ui"),
          (os.path.join(HERE, "GoldDesk.ico"), ".")]

a = Analysis(
    [os.path.join(HERE, "frozen_main.py")],
    pathex=[V2],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GoldDesk",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(HERE, "GoldDesk.ico"),
    version=None,
)
