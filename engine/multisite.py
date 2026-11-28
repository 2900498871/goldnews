# -*- coding: utf-8 -*-
"""多平台中心：在应用当前页面内看热榜和正文，不再另开网页。

用户要求：
  * 不再在侧栏挂五个子模块；
  * 点击“打开 B站 / 打开 贴吧…”不弹新窗口，而是在当前页面加载内容；
  * 登录态直接复用设置页的平台登录，不在这里重新登录。

实现方式：
  * 热榜使用各平台公开榜单接口（B站 / 微博 / 知乎 / 抖音）和贴吧话题榜；
  * 正文用设置页已经登录的 Cookie，在后台 WebView2 里渲染后抽取文字；
  * 未登录时只展示公开热榜，点正文会提示“去设置页登录”，不会自动弹登录窗。
"""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import quote, urlsplit

from .platforms import _render_page, _webview_ready, is_logged_in, mark_expired
from .util import http_get

SITES = {
    "bilibili": {
        "key": "bilibili", "name": "B站", "short": "B",
        "login": "bilibili", "login_name": "B站",
        "hosts": ("bilibili.com", "b23.tv"),
        "desc": "B站热榜；登录后可在本页读取搜索结果/视频相关正文。",
    },
    "tieba": {
        "key": "tieba", "name": "贴吧", "short": "贴",
        "login": "baidu", "login_name": "百度（贴吧）",
        "hosts": ("tieba.baidu.com",),
        "desc": "贴吧热议话题；共用设置页的百度登录。",
    },
    "weibo": {
        "key": "weibo", "name": "微博", "short": "微",
        "login": "weibo", "login_name": "微博",
        "hosts": ("weibo.com", "weibo.cn"),
        "desc": "微博热搜；登录后可在本页读取热搜正文。",
    },
    "zhihu": {
        "key": "zhihu", "name": "知乎", "short": "知",
        "login": "zhihu", "login_name": "知乎",
        "hosts": ("zhihu.com",),
        "desc": "知乎热榜；登录后可在本页读取问答正文。",
    },
    "douyin": {
        "key": "douyin", "name": "抖音", "short": "抖",
        "login": "douyin", "login_name": "抖音",
        "hosts": ("douyin.com", "iesdouyin.com"),
        "desc": "抖音热点榜；登录后可在本页读取搜索/视频相关正文。",
    },
}
ORDER = ("bilibili", "tieba", "weibo", "zhihu", "douyin")

_FEED_LOCK = threading.RLock()
_FEED_CACHE = {}
_FEED_TTL = 90
_LAST_ERROR = ""


def sites() -> dict:
    return {
        "ok": True,
        "supported": _webview_ready(),
        "sites": [dict(SITES[k]) for k in ORDER],
        "error": _LAST_ERROR,
    }


def _login_state(data_dir: str, key: str) -> dict:
    conf = SITES[key]
    platform = conf["login"]
    name = conf["login_name"]
    return {
        "platform": platform,
        "name": name,
        "logged_in": bool(is_logged_in(data_dir, platform)),
    }


def _tieba_items() -> tuple[list, str]:
    status, text, _ = http_get("https://tieba.baidu.com/hottopic/browse/topicList",
                               timeout=12, headers={"Referer": "https://tieba.baidu.com/"})
    if status != 200 or not (text or "").strip():
        return [], "HTTP %s / 空响应" % status
    try:
        data = json.loads(text)
        topics = (((data.get("data") or {}).get("bang_topic") or {}).get("topic_list") or [])
    except Exception as e:
        return [], "解析失败：%s" % str(e)[:80]
    items = []
    for i, t in enumerate(topics[:30], 1):
        title = str(t.get("topic_name") or "").strip()
        if not title:
            continue
        tid = t.get("topic_id")
        items.append({
            "rank": i, "title": title,
            "summary": str(t.get("topic_desc") or "").strip(),
            "hot": t.get("discuss_num") or t.get("topic_hot") or "",
            "url": ("https://tieba.baidu.com/hottopic/browse/hottopic?topic_id=%s" % tid) if tid else
                   ("https://tieba.baidu.com/f/search/res?ie=utf-8&qw=" +
                    quote(title)),
        })
    return items, "ok" if items else "ok（0 条，页面结构可能已变化）"


def _hot_text(it: dict) -> str:
    hot = it.get("hot_text") or it.get("hot") or ""
    if isinstance(hot, (int, float)):
        if hot >= 10000:
            return "%.1f 万" % (hot / 10000.0)
        return str(hot)
    return str(hot)


def feed(data_dir: str, key: str) -> dict:
    """返回某平台热榜；纯公开榜单，不触发登录窗口。"""
    key = str(key or "").strip().lower()
    if key not in SITES:
        return {"ok": False, "message": "未知平台"}
    conf = SITES[key]
    now = time.time()
    with _FEED_LOCK:
        hit = _FEED_CACHE.get(key)
        if hit and now - hit[0] < _FEED_TTL:
            items, status = hit[1], hit[2]
        else:
            items, status = [], ""
    if not items:
        if key == "tieba":
            items, status = _tieba_items()
        else:
            from .daily import HOT_SOURCES, _fetch
            src = next((s for s in HOT_SOURCES if s["id"] == key), None)
            if not src:
                return {"ok": False, "message": "未配置该平台热榜"}
            items, status = _fetch(src["url"], src["parser"], src.get("headers"), timeout=12.0)
        items = items[:30]
        with _FEED_LOCK:
            _FEED_CACHE[key] = (now, items, status)
    return {
        "ok": True,
        "site": key,
        "name": conf["name"],
        "desc": conf["desc"],
        "items": [{
            "rank": it.get("rank") or (i + 1),
            "title": str(it.get("title") or ""),
            "summary": str(it.get("summary") or ""),
            "hot": _hot_text(it),
            "url": str(it.get("url") or ""),
        } for i, it in enumerate(items)],
        "status": status,
        "login": _login_state(data_dir, key),
        "message": "",
    }


def _url_allowed(key: str, url: str) -> bool:
    try:
        host = (urlsplit(url).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in SITES[key]["hosts"])


def read(data_dir: str, key: str, url: str, title: str = "") -> dict:
    """用设置页已有登录态读取正文；不打开任何新窗口，不自动重新登录。"""
    key = str(key or "").strip().lower()
    url = str(url or "").strip()
    if key not in SITES:
        return {"ok": False, "message": "未知平台"}
    if not url or not _url_allowed(key, url):
        return {"ok": False, "message": "链接不属于该平台，已拒绝"}
    conf = SITES[key]
    login = _login_state(data_dir, key)
    if not login["logged_in"]:
        return {"ok": False, "need_login": True, "platform": login["platform"],
                "login_name": login["name"],
                "message": "设置页里%s尚未登录；热榜可看，正文需要先到设置页登录（这里不会重新登录）"
                           % login["name"]}
    platform = conf["login"]
    if key == "tieba":
        res = _render_page(url, ["#j_p_postlist", ".post_content", ".p_postlist", ".l_post", "main", "article"])
        final_host = ""
        try:
            final_host = (urlsplit(res.get("final_url") or "").netloc or "").lower()
        except Exception:
            pass
        if final_host.startswith("passport") or final_host.startswith("wappass"):
            mark_expired(data_dir, "baidu")
            return {"ok": False, "need_login": True, "platform": "baidu", "login_name": conf["login_name"],
                    "message": "百度登录已失效，请到设置页重新登录"}
        if res.get("status") == "ok" and (res.get("chars") or 0) >= 40:
            return {"ok": True, "title": title, "content": res.get("content") or "",
                    "chars": res.get("chars") or 0, "login_name": conf["login_name"]}
        return {"ok": False, "message": res.get("reason") or "贴吧页面里没识别出正文"}
    from .platforms import fetch_rendered
    res = fetch_rendered(url, platform, data_dir)
    if res.get("status") == "need_login":
        return {"ok": False, "need_login": True, "platform": platform,
                "login_name": conf["login_name"],
                "message": res.get("reason") or "平台登录已失效，请到设置页重新登录"}
    if res.get("status") == "ok":
        content = res.get("content") or ""
        chars = res.get("chars") or 0
        if key == "douyin" and chars < 200:
            # 抖音搜索页常只返回导航/版权信息，不把这点文字冒充正文
            content = (title + "\n\n" if title else "") + \
                "抖音搜索页这次只返回了公开导航信息，完整视频与文案请在抖音客户端查看。"
            chars = len(content)
        return {"ok": True, "title": title, "content": content,
                "chars": chars, "login_name": conf["login_name"]}
    return {"ok": False, "message": res.get("reason") or "页面里没识别出正文"}
