# -*- coding: utf-8 -*-
"""桌面窗口启动辅助：Edge/Chrome --app 模式 + Win32 真·无标题栏。

不引入 Electron/Tauri 等重依赖，也不需要联网安装任何包。
注意：Chromium 的 --app 窗口把标题栏画在客户区里，改 WS_CAPTION 完全无效
（实测样式清了、原生标题栏仍在）。这里改用 SetWindowRgn 把窗口顶部的
标题栏区域裁掉并把窗口上移，视觉与命中区一起消失，保留左右/下方调整边框。
自绘控制只提供「最小化 / 最大化」，关闭统一走顶栏「退出」按钮。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
import webbrowser
from ctypes import wintypes

CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
TITLE_HINT = "日常小助手"

_APP_HWND = None
_WIN32 = None
_PENDING_CAP = None      # 页面测得的标题栏高度（CSS px = Win32 逻辑 px）
_APPLIED_CAP = None      # 已裁剪的高度
_PENDING_DPR = 1.0      # 页面 devicePixelRatio：HRGN 用物理像素，需按它放大
_APPLIED_DPR = 1.0
_COMPACT = False        # 自绘「最大化」两态：紧凑窗口 / 铺满工作区


class _Win32:
    """只在 Windows 上初始化的窗口 API 封装。"""
    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000
    WS_SYSMENU = 0x00080000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    WM_SETICON = 0x0080
    WM_CLOSE = 0x0010
    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x0010
    SW_MINIMIZE = 6
    SW_MAXIMIZE = 3
    SW_RESTORE = 9
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    WS_POPUP = 0x80000000
    WS_EX_NOACTIVATE = 0x08000000
    WS_EX_TOOLWINDOW = 0x00000080
    SS_WHITERECT = 0x0006
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040

    def __init__(self):
        self.u32 = ctypes.WinDLL("user32", use_last_error=True)
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.g32 = ctypes.WinDLL("gdi32", use_last_error=True)
        # 拖动时每步同步一次桌面合成，避免移动请求堆积导致画面撕裂/闪烁
        self.dwm = None
        try:
            self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            self.dwm.DwmFlush.argtypes = []
            self.dwm.DwmFlush.restype = ctypes.c_long
        except Exception:
            self.dwm = None
        u = self.u32
        # 与 Chromium 保持一致：本进程按物理像素处理窗口坐标，避免 125%/150%
        # 缩放下 GetWindowRect / SetWindowPos / HRGN 混用逻辑与物理坐标。
        self.dpi_aware = False
        try:
            u.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            self.dpi_aware = bool(u.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
        except Exception:
            pass
        if not self.dpi_aware:
            try:
                shcore = ctypes.WinDLL("shcore", use_last_error=True)
                shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                self.dpi_aware = shcore.SetProcessDpiAwareness(2) == 0
            except Exception:
                pass
        if not self.dpi_aware:
            try:
                self.dpi_aware = bool(u.SetProcessDPIAware())
            except Exception:
                pass
        self.WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows.argtypes = [self.WNDENUMPROC, wintypes.LPARAM]
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.IsZoomed.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        u.SetWindowRgn.restype = ctypes.c_int
        u.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetClientRect.restype = wintypes.BOOL
        u.IsWindow.argtypes = [wintypes.HWND]
        u.IsWindow.restype = wintypes.BOOL
        u.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, wintypes.HWND,
                                      wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = wintypes.HWND
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.UINT]
        try:
            u.GetDpiForWindow.argtypes = [wintypes.HWND]
            u.GetDpiForWindow.restype = wintypes.UINT
        except Exception:
            pass
        self.g32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int]
        self.g32.CreateRectRgn.restype = ctypes.c_void_p
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetForegroundWindow.argtypes = []
        u.GetForegroundWindow.restype = wintypes.HWND
        u.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                 ctypes.c_int, ctypes.c_int, wintypes.UINT]
        u.LoadImageW.restype = wintypes.HICON
        long_t = ctypes.c_longlong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_long
        self.get_long = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
        self.get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_long.restype = long_t
        self.set_long = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
        self.set_long.argtypes = [wintypes.HWND, ctypes.c_int, long_t]
        self.set_long.restype = long_t
        self.k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.k32.CloseHandle.argtypes = [wintypes.HANDLE]
        u.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                            ctypes.c_void_p, wintypes.UINT]
        u.SystemParametersInfoW.restype = wintypes.BOOL

    def windows(self):
        """找标题含产品名、可见、且属于 Edge/Chrome 的窗口。"""
        found = []

        def cb(hwnd, _):
            try:
                if not self.u32.IsWindowVisible(hwnd):
                    return True
                n = self.u32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(n + 1)
                self.u32.GetWindowTextW(hwnd, buf, n + 1)
                if TITLE_HINT not in buf.value:
                    return True
                cls = ctypes.create_unicode_buffer(64)
                self.u32.GetClassNameW(hwnd, cls, 64)
                if "Chrome_WidgetWin" not in cls.value:
                    return True     # 避免误伤同名文件夹窗口
                found.append(hwnd)
            except Exception:
                pass
            return True

        self.u32.EnumWindows(self.WNDENUMPROC(cb), 0)
        try:
            self.u32.GetForegroundWindow.restype = wintypes.HWND
            fg = self.u32.GetForegroundWindow()
            if fg and fg in found:
                return [fg] + [h for h in found if h != fg]
        except Exception:
            pass
        return found

    def rect(self, hwnd):
        r = wintypes.RECT()
        self.u32.GetWindowRect(hwnd, ctypes.byref(r))
        return r

    def workarea(self):
        """主显示器工作区（不含任务栏），物理像素。"""
        r = wintypes.RECT()
        try:
            if not self.u32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
                raise OSError("SPI_GETWORKAREA failed")
        except Exception:
            self.u32.GetSystemMetrics.restype = ctypes.c_int
            r.left, r.top = 0, 0
            r.right = self.u32.GetSystemMetrics(0)
            r.bottom = self.u32.GetSystemMetrics(1)
        return r

    def _cap_px(self, cap: int, dpr: float) -> int:
        """页面测得的是 CSS px，HRGN/窗口坐标是物理 px，需要乘缩放比。"""
        scale = max(1.0, min(3.0, float(dpr or 1.0)))
        return int(round(max(18, min(64, int(cap or 30))) * scale))

    def _clip(self, hwnd, cap_px: int, scale: float):
        """裁掉窗口顶部 cap_px 物理像素。

        区域右/下边界按 dpr 再放大并留出余量：多出的区域在窗口之外，
        不会产生任何绘制，但能保证窗口右侧与底部绝不会被误裁成透明
        （那会露出后面窗口的原生标题栏和 X 按钮）。
        """
        r = self.rect(hwnd)
        w = int(round((r.right - r.left) * scale)) + 256
        h = int(round((r.bottom - r.top) * scale)) + 256
        rgn = self.g32.CreateRectRgn(0, cap_px, w, h)
        self.u32.SetWindowRgn(hwnd, rgn, True)

    def geometry(self, hwnd, compact: bool, cap: int, dpr: float):
        """两种窗口形态：

        * 最大化（铺满工作区）：把 Chromium 自绘标题栏裁掉并上移出屏幕，视觉无边框；
          此时窗口不能上下拖动（裁掉的那一条必须留在屏幕外）。
        * 普通窗口：同样裁掉原生标题栏，但不向上移——窗口顶部那条变成透明，
          内容从 y+cap 开始，视觉干净且上下左右都能拖。
        之前普通窗口保留原生标题栏，拖动还原后会出现「原生栏 + 自绘栏」双层，观感很差。
        """
        global _APPLIED_CAP, _APPLIED_DPR
        scale = max(1.0, min(3.0, float(dpr or 1.0)))
        cap_px = self._cap_px(cap, scale)
        wa = self.workarea()
        aw, ah = wa.right - wa.left, wa.bottom - wa.top
        if compact:
            # 页面按 1440×900 设计，普通窗口也用这个尺寸，避免顶栏按钮被挤出可视区；
            # 再留 80px 边距，让「还原」后真的像一个可拖动的窗口，而不是贴着屏幕边
            w = min(max(int(1440 * scale), 900), max(900, aw - 80))
            h = min(max(int(900 * scale), 560), max(560, ah - 80))
            x = wa.left + max(0, (aw - w) // 2)
            y = wa.top + max(0, (ah - h) // 2)
            # 普通窗口也裁掉 Chromium 原生标题栏，但不向上移：窗口内容从 y+cap 开始，
            # 顶部那一条被裁成透明，视觉上就是一个干净的无边框窗口，且上下左右都能拖。
            _APPLIED_CAP, _APPLIED_DPR = max(18, min(64, int(cap or 30))), scale
            self.u32.SetWindowPos(hwnd, 0, x, y, w, h, self.SWP_NOZORDER)
            self.refresh_region(hwnd, _APPLIED_CAP, scale)
        else:
            _APPLIED_CAP, _APPLIED_DPR = max(18, min(64, int(cap or 30))), scale
            self.u32.SetWindowPos(hwnd, 0, wa.left, wa.top - cap_px,
                                  aw, ah + cap_px, self.SWP_NOZORDER)
            self.refresh_region(hwnd, _APPLIED_CAP, scale)

    def hide_caption(self, hwnd, cap: int, dpr: float = 1.0):
        """进入无边框最大化形态（普通窗口形态由 toggle_maximize 切换）。"""
        cap = max(18, min(64, int(cap or 30)))
        self.geometry(hwnd, _COMPACT, cap, dpr)
        return cap

    def refresh_region(self, hwnd, cap: int, dpr: float = 1.0):
        """窗口尺寸变化后按当前形态重建裁剪区域（两种形态都裁掉原生标题栏）。"""
        if not _APPLIED_CAP:
            self.clear_region(hwnd)
            return
        scale = max(1.0, min(3.0, float(dpr or 1.0)))
        self._clip(hwnd, self._cap_px(cap, scale), scale)
    def clear_region(self, hwnd):
        self.u32.SetWindowRgn(hwnd, None, False)

    def set_icon(self, hwnd, ico_path):
        if not ico_path or not os.path.isfile(ico_path):
            return False
        h = self.u32.LoadImageW(0, ico_path, self.IMAGE_ICON, 0, 0, self.LR_LOADFROMFILE)
        if not h:
            return False
        self.u32.SendMessageW(hwnd, self.WM_SETICON, 0, h)   # ICON_SMALL
        self.u32.SendMessageW(hwnd, self.WM_SETICON, 1, h)   # ICON_BIG
        return True

    def move(self, hwnd, dx, dy):
        r = wintypes.RECT()
        self.u32.GetWindowRect(hwnd, ctypes.byref(r))
        scale = 1.0
        try:
            self.u32.GetDpiForSystem.restype = wintypes.UINT
            scale = self.u32.GetDpiForSystem() / 96.0
        except Exception:
            pass
        new_top = r.top + int(dy * scale)
        if not _COMPACT:
            # 只有「铺满工作区」形态才需要把裁掉的标题栏条留在屏幕外；普通窗口自由移动
            new_top = min(new_top, -self._cap_px(_APPLIED_CAP or 30, _APPLIED_DPR))
        self.u32.SetWindowPos(hwnd, 0, r.left + int(dx * scale), new_top,
                              0, 0, self.SWP_NOSIZE | self.SWP_NOZORDER)
        if self.dwm:
            try:
                self.dwm.DwmFlush()
            except Exception:
                pass

    def minimize(self, hwnd):
        self.u32.ShowWindow(hwnd, self.SW_MINIMIZE)

    def toggle_maximize(self, hwnd):
        global _COMPACT
        _COMPACT = not _COMPACT
        self.geometry(hwnd, _COMPACT, _APPLIED_CAP or _PENDING_CAP or 30, _APPLIED_DPR)
        return not _COMPACT

    def close(self, hwnd):
        self.u32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)


def _api():
    global _WIN32
    if os.name != "nt":
        return None
    if _WIN32 is None:
        try:
            _WIN32 = _Win32()
        except Exception:
            return None
    return _WIN32


# ---------------- WebView2 无边框宿主（pywebview） ----------------
# Edge --app 的标题栏只能靠裁剪 + 跨进程 SetWindowPos 来模拟拖动，合成器总比鼠标慢一拍，
# 拖动时窗口会抖/闪。换成系统自带的 WebView2 后窗口天生无边框，拖动由宿主自己完成。
_WV = None             # pywebview.Window
_WV_HOST = "edge"      # webview | edge
_WV_RESTORE = None     # 最大化前的窗口矩形 (l, t, r, b)
_WV_ZOOM = False       # 自管的最大化状态
_WV_ALLOW_CLOSE = False
_TRAY_HINTED = False


def webview_available() -> bool:
    if os.name != "nt":
        return False
    try:
        import webview  # noqa: F401
        return True
    except Exception:
        return False


def _wv_hwnd() -> int | None:
    if _WV is None:
        return None
    try:
        h = _WV.native.Handle          # .NET IntPtr
        return int(h.ToInt64()) if hasattr(h, "ToInt64") else int(h)
    except Exception:
        return None


def _wv_maximized() -> bool:
    if _WV_ZOOM:
        return True
    hwnd = _wv_hwnd()
    if not hwnd:
        return False
    api = _api()
    if not api:
        return False
    try:
        return bool(api.u32.IsZoomed(hwnd))
    except Exception:
        return False


def _wv_fill(on: bool):
    """最大化 = 铺满主屏工作区（任务栏保留）；还原 = 回到之前的窗口位置和尺寸。"""
    global _WV_RESTORE, _WV_ZOOM
    hwnd = _wv_hwnd()
    api = _api()
    if not hwnd or not api:
        return
    if on:
        r = api.rect(hwnd)
        _WV_RESTORE = (r.left, r.top, r.right, r.bottom)
        wa = api.workarea()
        api.u32.SetWindowPos(hwnd, 0, wa.left, wa.top, wa.right - wa.left, wa.bottom - wa.top,
                             0x0004 | 0x0010)   # SWP_NOZORDER | SWP_NOACTIVATE
        _WV_ZOOM = True
    else:
        l, t, rr, b = _WV_RESTORE or (240, 70, 1680, 970)
        api.u32.SetWindowPos(hwnd, 0, l, t, rr - l, b - t, 0x0004 | 0x0010)
        _WV_RESTORE = None
        _WV_ZOOM = False


def _tray_hint_once():
    global _TRAY_HINTED
    if _TRAY_HINTED:
        return
    _TRAY_HINTED = True
    try:
        import tray
        tray.notify("日常小助手", "窗口已收进托盘，双击托盘图标可恢复；退出请用托盘菜单。")
    except Exception:
        pass


def _wv_allow_close():
    global _WV_ALLOW_CLOSE
    _WV_ALLOW_CLOSE = True


def _wv_closing():
    """Alt+F4 / 系统关闭：隐藏到托盘而不是退出。托盘「退出程序」会先置允许关闭。"""
    if _WV_ALLOW_CLOSE:
        return True
    _wv_action("hide")
    return False


def _wv_action(action: str) -> dict:
    """WebView2 宿主下的窗口动作（调用会自动切到 UI 线程执行）。"""
    if _WV is None:
        return {"ok": False, "message": "窗口尚未创建"}
    zoomed = _wv_maximized()
    _wv_heal()
    if action == "info":
        hwnd = _wv_hwnd()
        rect = None
        api = _api()
        if hwnd and api:
            r = api.rect(hwnd)
            rect = [r.left, r.top, r.right, r.bottom]
        return {"ok": True, "host": "webview", "frameless": True, "caption_hidden": True,
                "hwnd": hwnd or 0, "rect": rect, "maximized": zoomed, "compact": not zoomed}
    if action == "hide":
        hwnd = _wv_hwnd()
        if not hwnd:
            return {"ok": False, "message": "窗口尚未创建"}
        ctypes.windll.user32.ShowWindow(hwnd, 0)
        _tray_hint_once()
        return {"ok": True}
    if action == "show":
        hwnd = _wv_hwnd()
        if not hwnd:
            return {"ok": False, "message": "窗口尚未创建"}
        ctypes.windll.user32.ShowWindow(hwnd, 9)
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        _wv_heal()
        return {"ok": True}
    if action == "minimize":
        _WV.minimize()
        return {"ok": True}
    if action == "maximize":
        _wv_fill(not zoomed)
        return {"ok": True, "maximized": not zoomed}
    if action == "close":
        return _wv_action("hide")
    if action in ("quit", "exit"):
        _wv_allow_close()
        _WV.destroy()
        return {"ok": True}
    # caption / refresh / move 在 WebView2 宿主下由系统原生处理，无需宿主介入
    return {"ok": True, "host": "webview"}


class _WvDrag:
    """顶栏拖动。页面只上报「开始 / 移动中 / 结束」，窗口位置按 Win32 读到的物理光标位置计算。

    pywebview 自带拖动用网页事件的 screenX 推算位置，而窗口正在鼠标下方移动，
    事件时序一抖（exe 首次启动、机器忙时）终点就会偏几像素；这里不依赖网页坐标。
    """

    def __init__(self):
        self._origin = None      # (光标x, 光标y, 窗口left, 窗口top)

    def _ready(self):
        return self._origin is not None and not _WV_ZOOM and _wv_hwnd() and _api()

    def drag_start(self) -> bool:
        _wv_heal()
        hwnd, api = _wv_hwnd(), _api()
        if not hwnd or not api or _WV_ZOOM:
            self._origin = None
            return False
        pt = wintypes.POINT()
        api.u32.GetCursorPos(ctypes.byref(pt))
        r = api.rect(hwnd)
        self._origin = (pt.x, pt.y, r.left, r.top)
        return True

    def drag_move(self):
        if not self._ready():
            return
        hwnd, api = _wv_hwnd(), _api()
        pt = wintypes.POINT()
        if not api.u32.GetCursorPos(ctypes.byref(pt)):
            return
        cx, cy, left, top = self._origin
        api.u32.SetWindowPos(hwnd, 0, left + (pt.x - cx), top + (pt.y - cy), 0, 0,
                             0x0001 | 0x0004 | 0x0010)   # NOSIZE | NOZORDER | NOACTIVATE

    def drag_end(self):
        self._origin = None

    def restored(self) -> bool:
        """页面从隐藏变为可见时调用（最小化后还原）。"""
        return _wv_heal()


_WV_DRAG = _WvDrag()
_WV_HEAL_LOCK = threading.Lock()


def _wv_heal() -> bool:
    """最小化后还原（任务栏 / 跨进程 SW_RESTORE）时，WinForms 偶发不重新布局子窗口：
    窗体已回到屏幕，WebView2 的子窗口却仍停在最小化位置（≈32928,32728），
    表现为页面画不出来、点哪都没反应。检测到就用 ±1px 触发一次重排。"""
    hwnd, api = _wv_hwnd(), _api()
    if not hwnd or not api:
        return False
    try:
        if api.u32.IsIconic(hwnd):
            return False
        r = api.rect(hwnd)
        if r.left > 20000:
            return False
        flags = 0x0002 | 0x0004 | 0x0010         # NOMOVE | NOZORDER | NOACTIVATE
        if r.left < -20000 or r.top < -20000:
            # 窗体自己停在最小化位置却不是最小化状态（用户会看到程序「不见了」）：拉回工作区中央
            wa = api.workarea()
            w = min(1440, wa.right - wa.left - 80)
            h = min(900, wa.bottom - wa.top - 80)
            api.u32.SetWindowPos(hwnd, 0, wa.left + max(0, (wa.right - wa.left - w) // 2),
                                 wa.top + max(0, (wa.bottom - wa.top - h) // 2), w, h, flags)
            global _WV_ZOOM
            _WV_ZOOM = False
            return True
        stuck = []

        @api.WNDENUMPROC
        def _cb(child, _l):
            c = api.rect(child)
            if c.left > 20000 or abs(c.left - r.left) > 2000 or abs(c.top - r.top) > 2000:
                stuck.append(1)
            return False            # 只看第一个子窗口

        api.u32.EnumChildWindows(hwnd, _cb, 0)
        if not stuck:
            return False
        with _WV_HEAL_LOCK:
            w, h = r.right - r.left, r.bottom - r.top
            api.u32.SetWindowPos(hwnd, 0, 0, 0, w + 1, h, flags)
            time.sleep(0.05)
            api.u32.SetWindowPos(hwnd, 0, 0, 0, w, h, flags)
        return True
    except Exception:
        return False


def open_webview_window(url: str, title: str, width: int = 1440, height: int = 900,
                        icon_path: str | None = None, data_dir: str | None = None,
                        frameless: bool = True, site_mode: bool = False):
    """在主线程创建 WebView2 窗口并阻塞运行，窗口关闭后返回。

    frameless=False 用于多平台中心的官方页面窗口：保留系统标题栏和关闭按钮；
    site_mode=True 表示这是独立窗口进程，不挂主窗口的“关闭到托盘”事件。
    """
    global _WV, _WV_HOST, _WV_ALLOW_CLOSE, _TRAY_HINTED
    _WV_ALLOW_CLOSE = False
    _TRAY_HINTED = False
    import webview
    storage = os.path.join(data_dir, "webview2") if data_dir else None
    if storage:
        os.makedirs(storage, exist_ok=True)
    # pywebview 先按「含系统边框」的尺寸建窗再去掉边框，内容区会比要求的小一圈；
    # 这里把标准边框尺寸补回去，保证内容区正好是设计尺寸
    fw, fh = 16, 39
    api = _api()
    if api:
        try:
            r = wintypes.RECT(0, 0, 0, 0)
            api.u32.AdjustWindowRectEx(ctypes.byref(r), 0x00CF0000, False, 0)
            fw, fh = max(0, r.right - r.left), max(0, r.bottom - r.top)
        except Exception:
            pass
    kw = {}
    if not site_mode:
        try:
            import inspect
            if "js_api" in inspect.signature(webview.create_window).parameters:
                kw["js_api"] = _WV_DRAG
        except Exception:
            pass
    _WV = webview.create_window(
        title, url, width=(width + fw if frameless else width),
        height=(height + fh if frameless else height),
        frameless=frameless, easy_drag=(not frameless),
        shadow=True, text_select=True, zoomable=False, background_color="#FFFFFF", **kw)
    _WV_HOST = "webview"
    if not site_mode:
        try:
            _WV.events.closing += _wv_closing
        except Exception:
            pass
    print("已打开 WebView2 窗口（%s）：%s" % ("官方页面" if site_mode else "主窗口", url), flush=True)
    webview.start(debug=False, private_mode=False, storage_path=storage, icon=icon_path)
    _WV = None
    _WV_HOST = "edge"
    _WV_RESTORE = None
    _WV_ZOOM = False


def open_desktop_window(url: str, title: str, width: int = 1440, height: int = 900,
                        icon_path: str | None = None, data_dir: str | None = None,
                        profile_dir: str | None = None, frameless: bool = True,
                        site_mode: bool = False):
    """优先用 WebView2 宿主；不可用时回退到 Edge --app。必须在主线程调用。"""
    if webview_available():
        try:
            open_webview_window(url, title, width, height, icon_path, data_dir,
                                frameless=frameless, site_mode=site_mode)
            return "WebView2 窗口"
        except Exception as e:
            print("WebView2 宿主启动失败，回退 Edge --app：%s: %s" % (type(e).__name__, e), flush=True)
            global _WV, _WV_HOST
            _WV, _WV_HOST = None, "edge"
    return open_app_window(url, width, height, frameless=frameless,
                           icon_path=icon_path, profile_dir=profile_dir)


def find_browser() -> str | None:
    for p in CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def focus_app_window() -> bool:
    """托盘双击/菜单「显示主窗口」：把无边框窗口还原到前台。"""
    if _WV_HOST == "webview":
        try:
            return bool(_wv_action("show").get("ok"))
        except Exception:
            return False
    api = _api()
    if not api:
        return False
    found = api.windows()
    if not found:
        return False
    try:
        api.u32.ShowWindow(found[0], 9)          # SW_RESTORE
        api.u32.SetForegroundWindow(found[0])
        return True
    except Exception:
        return False


def install_tray(icon_path: str = "") -> bool:
    """装上托盘图标；退出菜单复用窗口关闭流程。"""
    if os.name != "nt":
        return False
    try:
        import tray
        tray.install(focus_app_window, lambda: window_action("quit"), icon_path)
        return True
    except Exception:
        return False


def notify(title: str, text: str) -> bool:
    """托盘气泡通知；托盘没装或失败时静默返回 False。"""
    try:
        import tray
        return tray.notify(title, text)
    except Exception:
        return False


def window_action(action: str, dx: float = 0, dy: float = 0, cap: float = 0,
                  dpr: float = 0) -> dict:
    """供本地 API 调用：裁剪标题栏 / 拖动 / 最小化 / 最大化 / 退出 / 查询。"""
    global _APP_HWND, _PENDING_CAP, _PENDING_DPR
    api = _api()
    if not api:
        return {"ok": False, "message": "仅 Windows 桌面端支持窗口控制"}
    if _WV_HOST == "webview":
        try:
            return _wv_action(action)
        except Exception as e:
            return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
    if cap and cap > 0:
        _PENDING_CAP = int(cap)
    if dpr and dpr > 0:
        _PENDING_DPR = float(dpr)
    found = api.windows()
    if not found:
        # 页面已测好标题栏高度但窗口还没创建出来：先记下，由 _apply_later 应用
        if action == "caption":
            return {"ok": False, "pending": True, "message": "窗口尚未创建，稍后应用"}
        return {"ok": False, "message": "未检测到本程序的应用窗口"}
    _APP_HWND = found[0]
    hwnd = _APP_HWND
    try:
        if action == "info":
            r = api.rect(hwnd)
            dpi = 96
            try:
                dpi = int(api.u32.GetDpiForWindow(hwnd)) or 96
            except Exception:
                pass
            return {"ok": True, "host": "edge", "frameless": True, "caption_hidden": _APPLIED_CAP is not None,
                    "hwnd": int(hwnd),
                    "rect": [r.left, r.top, r.right, r.bottom], "dpi": dpi,
                    "dpi_aware": api.dpi_aware, "cap": _APPLIED_CAP,
                    "dpr": _APPLIED_DPR, "compact": bool(_COMPACT)}
        if action == "caption":
            used = api.hide_caption(hwnd, _PENDING_CAP or 30, _PENDING_DPR)
            return {"ok": True, "cap": used, "dpr": _PENDING_DPR}
        if action == "refresh":
            if _APPLIED_CAP:
                api.refresh_region(hwnd, _APPLIED_CAP, _APPLIED_DPR)
            return {"ok": True}
        if action == "move":
            if not _COMPACT:
                # 无边框最大化窗口被拖动时先还原为普通窗口，和系统窗口行为一致
                api.toggle_maximize(hwnd)
                return {"ok": True, "restored": True}
            api.move(hwnd, dx, dy)
            return {"ok": True}
        if action == "minimize":
            api.minimize(hwnd)
            return {"ok": True}
        if action == "hide":
            api.u32.ShowWindow(hwnd, 0)      # SW_HIDE：收进托盘，任务栏不再占位
            return {"ok": True}
        if action == "maximize":
            return {"ok": True, "maximized": api.toggle_maximize(hwnd)}
        if action in ("close", "quit"):
            api.close(hwnd)
            return {"ok": True}
        return {"ok": False, "message": "未知窗口动作"}
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}


def _edge_pids(profile_dir: str) -> list[int]:
    """找出使用本程序专属 profile 的 msedge 进程。

    Chromium 的进程单例：同一 user-data-dir 已有进程时，再执行 --app 只会
    在已有窗口里开一个普通标签页，无边框窗口永远出不来（表现为拖不动、
    没有最小化/最大化按钮）。重新拉起窗口前必须先清掉这些残留进程。
    """
    if os.name != "nt" or not profile_dir:
        return []
    p = profile_dir.replace("'", "''")
    # Get-WmiObject：Win7 自带的 PowerShell 2.0 没有 Get-CimInstance（要 WMF 3.0+），WMI 查询两代系统都有
    ps = ("Get-WmiObject Win32_Process -Filter \"Name='msedge.exe'\" | "
          "Where-Object { $_.CommandLine -like '*%s*' } | "
          "ForEach-Object { $_.ProcessId }" % p)
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, text=True, timeout=8)
        return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
    except Exception:
        return []


def _kill_profile_edges(profile_dir: str) -> int:
    pids = _edge_pids(profile_dir)
    killed = 0
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, timeout=5)
            killed += 1
        except Exception:
            pass
    return killed


def _focus_app_window() -> bool:
    api = _api()
    if not api:
        return False
    found = api.windows()
    if not found:
        return False
    hwnd = found[0]
    try:
        api.u32.ShowWindow(hwnd, api.SW_RESTORE)
    except Exception:
        pass
    try:
        api.u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        api.u32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return True


def _apply_later(icon_path, relaunch_args=None, profile_dir=None):
    """窗口是异步创建的，轮询等待后裁掉标题栏并设置图标。

    若迟迟找不到应用窗口（--app 被已有 Edge 进程合并成普通标签页），
    清掉该 profile 的残留进程后重新拉起一次。
    """
    api = _api()
    if not api:
        return
    rounds = 2 if relaunch_args else 1
    for r in range(rounds):
        for _ in range(40):
            time.sleep(0.3)
            found = api.windows()
            if not found:
                continue
            global _APP_HWND
            _APP_HWND = found[0]
            try:
                used = api.hide_caption(_APP_HWND, _PENDING_CAP or 30, _PENDING_DPR)
                api.set_icon(_APP_HWND, icon_path)
                print("标题栏已裁剪（%d CSS px × %.2f），应用窗口 hwnd=%s" %
                      (used, _PENDING_DPR, _APP_HWND), flush=True)
            except Exception as e:
                print("标题栏裁剪失败: %s: %s" % (type(e).__name__, e), flush=True)
            return
        if r + 1 < rounds:
            killed = _kill_profile_edges(profile_dir)
            print("未检测到应用窗口，清理 %d 个残留 Edge 进程后重试" % killed, flush=True)
            time.sleep(1.0)
            try:
                subprocess.Popen(relaunch_args, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:
                print("重新拉起窗口失败: %s: %s" % (type(e).__name__, e), flush=True)
                return


def open_app_window(url: str, width: int = 1440, height: int = 900,
                    frameless: bool = True, icon_path: str | None = None,
                    extra_args: list | None = None, profile_dir: str | None = None,
                    return_proc: bool = False):
    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return "系统默认浏览器（无边框不可用）"
    # 已有同 profile 的 Edge 进程时，--app 会被合并成普通标签页；直接清掉重新拉起。
    # 不再「聚焦已有窗口」：窗口可能连的是旧端口/旧实例，聚焦会继续用错误的页面。
    if frameless and os.name == "nt" and profile_dir:
        killed = _kill_profile_edges(profile_dir)
        if killed:
            print("已清理 %d 个占用专属 profile 的残留 Edge 进程" % killed, flush=True)
            time.sleep(1.0)
    args = [exe, "--app=" + url, "--window-size=%d,%d" % (width, height),
            "--disable-features=TranslateUI,CalculateNativeWinOcclusion",
            # 关闭 DirectComposition：外进程 SetWindowPos 拖动带裁剪区域的窗口时，
            # DComp 合成面容易整窗重绘出现白闪，走 GDI 合成路径更稳定
            "--disable-direct-composition",
            "--no-first-run", "--no-default-browser-check"]
    if profile_dir:
        # 独立用户配置：不复用日常浏览器进程，窗口尺寸/标题栏处理稳定可控
        os.makedirs(profile_dir, exist_ok=True)
        args += ["--user-data-dir=" + profile_dir, "--profile-directory=Default"]
    if extra_args:
        args += list(extra_args)
    try:
        p = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if return_proc:
            return p
        if frameless and os.name == "nt":
            threading.Thread(target=_apply_later, args=(icon_path, args, profile_dir),
                             daemon=True).start()
            return os.path.basename(exe) + " 无边框应用窗口"
        return os.path.basename(exe) + " --app 窗口"
    except Exception:
        webbrowser.open(url)
        return "系统默认浏览器（回退）"
