# -*- coding: utf-8 -*-
"""Windows 托盘图标与气泡通知（纯 ctypes，Win7/10/11 通用）。"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import wintypes

WM_TRAY = 0x0400 + 20
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x1, 0x2, 0x4, 0x10
WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0203, 0x0205
NIN_BALLOONUSERCLICK = 0x0405
TPM_RETURNCMD, TPM_NONOTIFY = 0x0100, 0x0080


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_byte * 8)]


class _NID(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON), ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", _GUID), ("hBalloonIcon", wintypes.HICON)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long,
                             wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WNDCLASS(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class Tray:
    def __init__(self, on_show, on_exit, icon_path: str = "", tip: str = "日常小助手"):
        self.on_show = on_show
        self.on_exit = on_exit
        self.icon_path = icon_path
        self.tip = tip
        self.hwnd = None
        self._nid = None
        self._thread = None
        self._ready = threading.Event()

    def start(self):
        if os.name != "nt" or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="golddesk-tray", daemon=True)
        self._thread.start()
        self._ready.wait(3)

    def _run(self):
        u = ctypes.windll.user32
        sh = ctypes.windll.shell32
        k = ctypes.windll.kernel32
        lresult = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        u.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.DefWindowProcW.restype = lresult
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, wintypes.HMENU,
                                      wintypes.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = wintypes.HWND
        sh.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(_NID)]
        sh.Shell_NotifyIconW.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.CreatePopupMenu.restype = wintypes.HMENU
        u.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
        u.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
        u.TrackPopupMenu.restype = wintypes.UINT
        u.DestroyMenu.argtypes = [wintypes.HMENU]
        hinst = k.GetModuleHandleW(None)
        cls_name = "GoldDeskTrayWnd"
        self._taskbar_msg = u.RegisterWindowMessageW("TaskbarCreated")
        wc = _WNDCLASS()
        wc.lpfnWndProc = WNDPROC(self._proc)
        wc.hInstance = hinst
        wc.lpszClassName = cls_name
        u.RegisterClassW(ctypes.byref(wc))
        self.hwnd = u.CreateWindowExW(0, cls_name, "", 0, 0, 0, 0, 0, None, None, hinst, None)
        if not self.hwnd:
            return
        hicon = None
        if self.icon_path and os.path.isfile(self.icon_path):
            hicon = u.LoadImageW(None, self.icon_path, 1, 0, 0, 0x10 | 0x20)  # IMAGE_ICON, FROMFILE|SHARED
        if not hicon:
            hicon = u.LoadIconW(None, wintypes.LPCWSTR(32512))               # IDI_APPLICATION
        nid = _NID()
        nid.cbSize = ctypes.sizeof(_NID)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = hicon
        nid.szTip = self.tip[:127]
        self._nid = nid
        self._hicon = hicon
        u.Shell_NotifyIconW = sh.Shell_NotifyIconW
        u.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._ready.set()
        msg = wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            u.TranslateMessage(ctypes.byref(msg))
            u.DispatchMessageW(ctypes.byref(msg))
        u.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        self.hwnd = None

    def _proc(self, hwnd, msg, wparam, lparam):
        u = ctypes.windll.user32
        if getattr(self, "_taskbar_msg", 0) and msg == self._taskbar_msg:
            # Explorer 重启后托盘区会清空，重新注册图标
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid))
            except Exception:
                pass
            return 0
        if msg == WM_TRAY:
            if lparam in (WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
                self._safe(self.on_show)
            elif lparam == WM_RBUTTONUP:
                menu = u.CreatePopupMenu()
                u.AppendMenuW(menu, 0x0000, 1, "显示主窗口")
                u.AppendMenuW(menu, 0x0000, 2, "退出程序")
                u.SetForegroundWindow(hwnd)
                cmd = u.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY, 0, 0, 0, hwnd, None)
                u.DestroyMenu(menu)
                if cmd == 1:
                    self._safe(self.on_show)
                elif cmd == 2:
                    self._safe(self.on_exit)
                    u.DestroyWindow(hwnd)
                    u.PostQuitMessage(0)
            return 0
        if msg == 0x0010:   # WM_CLOSE
            u.DestroyWindow(hwnd)
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _safe(self, fn):
        try:
            fn()
        except Exception:
            pass

    def notify(self, title: str, text: str):
        if not self.hwnd or not self._nid:
            return False
        try:
            nid = self._nid
            nid.uFlags = NIF_INFO
            nid.szInfoTitle = (title or "日常小助手")[:63]
            nid.szInfo = (text or "")[:255]
            nid.dwInfoFlags = 0x1   # NIIF_INFO
            ctypes.windll.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))
            return True
        except Exception:
            return False


_TRAY = None


def install(on_show, on_exit, icon_path: str = "") -> Tray:
    global _TRAY
    if _TRAY is None:
        _TRAY = Tray(on_show, on_exit, icon_path)
        _TRAY.start()
    return _TRAY


def notify(title: str, text: str) -> bool:
    return bool(_TRAY and _TRAY.notify(title, text))
