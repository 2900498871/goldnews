# -*- coding: utf-8 -*-
"""多平台中心：在程序窗口内容区里嵌入平台官方页面。

不是新开浏览器窗口，也不是 iframe：
  * 启动时预先建一个隐藏 WebView2 窗口，和主窗口共用同一个用户数据目录；
  * 点击平台卡片时，把这个窗口挂到主窗口内容区（顶栏/侧栏保留），加载官方站点；
  * 页面里的登录、扫码、视频、帖子、搜索都由平台官方页面自己完成；
  * 登录态直接复用设置页已经保存的 Cookie，不在多平台中心重复登录。
"""
from __future__ import annotations

import os

SITES = {
    "bilibili": {
        "key": "bilibili", "name": "B站", "short": "B",
        "url": "https://www.bilibili.com/",
        "login": "bilibili", "login_name": "B站",
        "desc": "官方首页、热门、搜索和视频；登录态复用设置页的 B站 登录。",
    },
    "tieba": {
        "key": "tieba", "name": "贴吧", "short": "贴",
        "url": "https://tieba.baidu.com/",
        "login": "baidu", "login_name": "百度（贴吧）",
        "desc": "贴吧官方首页、进吧、帖子和搜索；共用设置页的百度登录。",
    },
    "weibo": {
        "key": "weibo", "name": "微博", "short": "微",
        "url": "https://weibo.com/",
        "login": "weibo", "login_name": "微博",
        "desc": "微博官方首页、热搜、搜索和发博；登录态复用设置页的微博登录。",
    },
    "zhihu": {
        "key": "zhihu", "name": "知乎", "short": "知",
        "url": "https://www.zhihu.com/",
        "login": "zhihu", "login_name": "知乎",
        "desc": "知乎官方首页、热榜、问题和回答；登录态复用设置页的知乎登录。",
    },
    "douyin": {
        "key": "douyin", "name": "抖音", "short": "抖",
        "url": "https://www.douyin.com/",
        "login": "douyin", "login_name": "抖音",
        "desc": "抖音官方首页、推荐、搜索和视频；登录态复用设置页的抖音登录。",
    },
}
ORDER = ("bilibili", "tieba", "weibo", "zhihu", "douyin")


def sites() -> dict:
    try:
        from launcher import webview_available
        supported = bool(webview_available())
    except Exception:
        supported = os.name == "nt"
    return {"ok": True, "supported": supported,
            "sites": [dict(SITES[k]) for k in ORDER], "error": ""}


def show(data_dir: str, key: str) -> dict:
    key = str(key or "").strip().lower()
    if key not in SITES:
        return {"ok": False, "message": "未知平台"}
    conf = SITES[key]
    try:
        from launcher import show_site_panel
        r = show_site_panel(conf["url"])
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
    r = dict(r or {})
    r["site"] = key
    r["name"] = conf["name"]
    return r


def hide() -> dict:
    try:
        from launcher import hide_site_panel
        return hide_site_panel()
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}


def fullscreen(on: bool) -> dict:
    """官方应用全屏 / 还原；只改变内嵌页矩形，不产生新的系统窗口。"""
    try:
        from launcher import set_site_fullscreen
        return set_site_fullscreen(bool(on))
    except Exception as e:
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
