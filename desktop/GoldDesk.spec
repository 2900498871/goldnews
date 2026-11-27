# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包规格：单文件、无控制台、UI 内置、数据留 exe 同目录。

在 v2/ 目录执行：python -m PyInstaller --noconfirm --clean desktop/GoldDesk.spec
窗口宿主是 pywebview + 系统 WebView2（pythonnet / WinForms），这些包带数据文件与动态库，
必须 collect_all 才能在冻结环境里起无边框窗口。
"""
import os
from PyInstaller.utils.hooks import collect_submodules, collect_all

HERE = SPECPATH                       # v2/desktop
V2 = os.path.dirname(HERE)            # v2
hidden = collect_submodules("engine") + ["server", "launcher",
    "webview.platforms.winforms", "webview.platforms.edgechromium",
    "clr_loader", "clr_loader.netfx"]
binaries, datas = [], []
for pkg in ("webview", "pythonnet", "clr_loader", "docx"):
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
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="日常小助手",
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
