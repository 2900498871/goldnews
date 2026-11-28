# -*- coding: utf-8 -*-
"""多平台中心：用本程序自己的窗口打开平台官方网页。

主进程不直接操作网页，也不抓取、不注入脚本；点击子模块时由主进程拉起一个
同程序的轻量窗口进程（`日常小助手.exe --site <key>`），该进程用本程序内置的
WebView2 / Edge 应用窗口加载平台官网。每个平台使用独立的持久化浏览器目录，
关闭窗口后 Cookie 仍保留；平台的风控、扫码、短信验证码和登录都由官方页面完成。

这样既满足“使用官方页面、在本程序打开”，也避免把官方页面嵌进 iframe 被平台
拒绝，或通过隐藏窗口自动填表触发风控。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

SITES = {
    "bilibili": {
        "key": "bilibili", "name": "B站", "short": "B",
        "url": "https://www.bilibili.com/", "color": "#FB7299",
        "desc": "首页推荐、热门视频、分区和搜索。登录、播放、评论都在官方页面完成。",
    },
    "tieba": {
        "key": "tieba", "name": "贴吧", "short": "贴",
        "url": "https://tieba.baidu.com/", "color": "#3385FF",
        "desc": "贴吧首页、吧内帖子、搜索和回复。使用百度账号在官方页面登录。",
    },
    "weibo": {
        "key": "weibo", "name": "微博", "short": "微",
        "url": "https://weibo.com/", "color": "#E6162D",
        "desc": "首页信息流、热搜和搜索。登录、发博、评论都在官方页面完成。",
    },
    "zhihu": {
        "key": "zhihu", "name": "知乎", "short": "知",
        "url": "https://www.zhihu.com/", "color": "#1772F6",
        "desc": "首页推荐、问题与回答、搜索。登录和互动都在官方页面完成。",
    },
    "douyin": {
        "key": "douyin", "name": "抖音", "short": "抖",
        "url": "https://www.douyin.com/", "color": "#161823",
        "desc": "首页推荐、视频和搜索。登录、点赞、评论都在官方页面完成。",
    },
}

ORDER = ("bilibili", "tieba", "weibo", "zhihu", "douyin")

_LOCK = threading.RLock()
_PROCS = {}
_LAST_ERROR = ""
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def sites() -> dict:
    return {
        "ok": True,
        "supported": os.name == "nt",
        "sites": [dict(SITES[k], official=True) for k in ORDER],
        "error": _LAST_ERROR,
    }


def _spawn_args(key: str) -> list:
    """源码模式和打包模式都拉起同一个入口，由入口的 --site 分支开窗口。"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--site", key]
    return [sys.executable, "-X", "utf8", os.path.join(_ROOT, "desktop", "run.py"), "--site", key]


def open_site(data_dir: str, key: str) -> dict:
    """打开平台官方页面窗口。同一平台已有窗口进程时直接复用。"""
    global _LAST_ERROR
    key = str(key or "").strip().lower()
    if key not in SITES:
        return {"ok": False, "message": "未知平台"}
    site = SITES[key]
    with _LOCK:
        old = _PROCS.get(key)
        if old is not None and old.poll() is None:
            return {"ok": True, "existing": True, "url": site["url"],
                    "message": "%s官方页面已经打开，请在任务栏切换窗口" % site["name"]}
        _PROCS.pop(key, None)
    try:
        p = subprocess.Popen(
            _spawn_args(key), cwd=_ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=_CREATE_NO_WINDOW)
        with _LOCK:
            _PROCS[key] = p
        _LAST_ERROR = ""
        # 子进程启动后立刻退出通常表示入口/窗口创建失败；稍等一下给出明确结果
        time.sleep(0.6)
        if p.poll() is not None:
            with _LOCK:
                _PROCS.pop(key, None)
            return {"ok": False, "message": "打开%s窗口失败，请查看程序日志" % site["name"]}
        return {"ok": True, "url": site["url"],
                "message": "正在打开%s官方页面；登录和浏览都在这个程序窗口内完成" % site["name"]}
    except Exception as e:
        _LAST_ERROR = "%s: %s" % (type(e).__name__, str(e)[:120])
        return {"ok": False, "message": "打开%s官方页面失败：%s" % (site["name"], _LAST_ERROR)}


def show_official_window(key: str, data_dir: str, icon_path: str = "") -> None:
    """子进程入口：创建平台官方页面窗口并阻塞到窗口关闭。"""
    key = str(key or "").strip().lower()
    if key not in SITES:
        return
    site = SITES[key]
    # 每个平台独立配置目录，避免多个 WebView2 进程抢同一个用户数据目录；
    # 同一平台再次打开时复用该目录，Cookie / 登录态会保留。
    storage = os.path.join(data_dir, "multisite", key)
    os.makedirs(storage, exist_ok=True)
    from launcher import (open_app_window, open_webview_window, webview_available)
    profile_dir = os.path.join(storage, "browser-profile")
    if webview_available():
        try:
            open_webview_window(
                site["url"], "日常小助手 · 多平台中心 · " + site["name"],
                1200, 820, icon_path=icon_path or None, data_dir=storage,
                frameless=False, site_mode=True)
            return
        except Exception as e:
            print("WebView2 官方页面启动失败，回退 Edge --app：%s: %s"
                  % (type(e).__name__, e), flush=True)
    # Edge --app 在独立进程里跑；子进程必须等它关闭再退出，否则主程序会把
    # “子进程已退出”误判成窗口打开失败。
    proc = open_app_window(
        site["url"], 1200, 820, frameless=False, icon_path=icon_path or None,
        profile_dir=profile_dir, return_proc=True)
    if hasattr(proc, "wait"):
        proc.wait()


def close_all() -> None:
    """主程序退出时结束仍开着的平台窗口子进程。"""
    with _LOCK:
        items = list(_PROCS.values())
        _PROCS.clear()
    for p in items:
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
