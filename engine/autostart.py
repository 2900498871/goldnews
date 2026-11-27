# -*- coding: utf-8 -*-
"""开机自启动：写 / 删 当前用户注册表 Run 项（HKCU，不需要管理员权限）。

真实状态以注册表为准；config.json 里的 autostart 只是界面回显用的镜像。
"""
from __future__ import annotations

import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
NAME = "GoldDesk"


def _root() -> str:
    # engine/autostart.py → v2 根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def current_command() -> str:
    """本程序「开机时应执行」的命令行。打包版直接指向 exe；源码模式用 pythonw + run.py。"""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        return '"%s"' % exe if os.path.isfile(exe) else ""
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    script = os.path.join(_root(), "desktop", "run.py")
    if os.path.isfile(pyw) and os.path.isfile(script):
        return '"%s" "%s"' % (pyw, script)
    return ""


def status() -> dict:
    out = {"supported": os.name == "nt", "enabled": False, "command": "",
           "frozen": bool(getattr(sys, "frozen", False)), "stale": False, "name": NAME}
    if not out["supported"]:
        out["message"] = "仅 Windows 支持开机自启动"
        return out
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            val, _typ = winreg.QueryValueEx(k, NAME)
        out["command"] = str(val or "")
    except FileNotFoundError:
        out["command"] = ""
    except OSError as e:
        out["message"] = "读取注册表失败：%s" % e
        return out
    out["enabled"] = bool(out["command"])
    # 程序被移动 / 重装后，Run 项里的旧路径会失效：如实提示，重新打开开关即更新
    want = current_command()
    if out["enabled"] and want and out["command"] != want:
        out["stale"] = True
        out["message"] = "自启动项指向的路径与当前程序不一致（程序可能被移动过），请关闭后再打开开关以更新"
    return out


def set_enabled(on: bool) -> dict:
    if os.name != "nt":
        return {"ok": False, "message": "仅 Windows 支持开机自启动"}
    import winreg
    try:
        if on:
            cmd = current_command()
            if not cmd:
                return {"ok": False, "message": "无法定位可启动程序，请使用打包版 GoldDesk.exe"}
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.SetValueEx(k, NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, NAME)
            except FileNotFoundError:
                pass
    except OSError as e:
        return {"ok": False, "message": "写入注册表失败：%s" % e}
    out = status()
    out["ok"] = True
    out["enabled"] = bool(on)
    return out
