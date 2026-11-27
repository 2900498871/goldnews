# -*- coding: utf-8 -*-
"""单实例：同一端口重复启动时激活已有窗口并退出新进程。"""
from __future__ import annotations

import ctypes
import os

_MUTEX = None
_MUTEX_NAME = ""


def _activate_window(title_hint: str = "黄金积存金助手") -> bool:
    """按标题找到已运行的无边框窗口并还原到前台（失败不影响退出新进程）。"""
    try:
        from ctypes import wintypes
        u = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def cb(hwnd, _):
            n = u.GetWindowTextLengthW(hwnd)
            if n > 0:
                buf = ctypes.create_unicode_buffer(n + 1)
                u.GetWindowTextW(hwnd, buf, n + 1)
                if title_hint in buf.value and u.IsWindowVisible(hwnd):
                    found.append(hwnd)
            return True

        u.EnumWindows(cb, 0)
        if not found:
            return False
        hwnd = found[0]
        u.ShowWindow(hwnd, 9)           # SW_RESTORE
        u.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


def ensure_single(port: int) -> bool:
    """返回 True=本进程可以继续；False=已有实例在跑（已尝试激活其窗口）。"""
    global _MUTEX, _MUTEX_NAME
    if os.name != "nt":
        return True
    name = "Local\\GoldDesk-%s" % port
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        handle = k32.CreateMutexW(None, True, name)
        last = k32.GetLastError()
        _MUTEX = handle
        _MUTEX_NAME = name
        if not handle:
            return True
        if last == 183:                  # ERROR_ALREADY_EXISTS
            _activate_window()
            return False
        return True
    except Exception:
        return True
