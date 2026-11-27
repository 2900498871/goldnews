# -*- coding: utf-8 -*-
"""平台登录与登录态正文读取。

登录窗口使用程序内置 WebView2，所有窗口共享同一个 WebView2 用户目录：
用户在窗口里扫码/账号登录后，Cookie 留在本机浏览器配置里；程序只额外记录
“哪个平台已登录、上次登录时间”，不保存账号密码，也不把 Cookie 明文写进状态文件。

热搜链接大多是搜索页，普通 HTTP 请求拿不到 JS 渲染后的内容。这里用一个最小化后
隐藏的 WebView2 窗口加载链接，等页面渲染完再抽取正文；登录态由同一个浏览器配置共享。
"""
from __future__ import annotations

import json
import os
import threading
import time

from .util import iso_bj

PLATFORMS = {
    "weibo": {
        "name": "微博",
        "login_url": "https://weibo.com/login.php",
        "logout_url": "https://passport.weibo.com/sso/signout",
        "auth_cookies": ("SUB", "SUBP"),
        "hosts": ("weibo.com", "weibo.cn"),
        "selectors": ["#pl_feedlist_index", ".card-wrap", ".Feed_wrap", "article", "main"],
        "login_hint": "登录完成后关闭本窗口即可保存登录状态",
        "logout_hint": "退出微博账号后关闭本窗口",
    },
    "baidu": {
        "name": "百度",
        "login_url": "https://passport.baidu.com/v2/?login",
        "logout_url": "https://passport.baidu.com/?logout&u=https%3A%2F%2Fwww.baidu.com%2F",
        "auth_cookies": ("BDUSS",),
        "hosts": ("baidu.com",),
        "selectors": ["#content_left", "#results", ".result", "article", "main"],
        "login_hint": "登录完成后关闭本窗口即可保存登录状态",
        "logout_hint": "退出百度账号后关闭本窗口",
    },
    "douyin": {
        "name": "抖音",
        "login_url": "https://www.douyin.com/",
        "logout_url": "https://www.douyin.com/",
        "auth_cookies": ("sessionid", "sid_guard", "sid_tt"),
        "hosts": ("douyin.com", "iesdouyin.com"),
        "selectors": ["#search-result-container", "[data-e2e='search-result']", "main", "article"],
        "login_hint": "登录完成后关闭本窗口即可保存登录状态",
        "logout_hint": "请在网页右上角头像菜单里退出登录，检测到退出后会自动关闭",
    },
    "bilibili": {
        "name": "B站",
        "login_url": "https://passport.bilibili.com/login",
        "logout_url": "https://passport.bilibili.com/login?act=logout",
        "auth_cookies": ("SESSDATA",),
        "hosts": ("bilibili.com", "b23.tv"),
        "selectors": [".search-content", ".video-list", "#i_cecream", "main", "article"],
        "login_hint": "登录完成后关闭本窗口即可保存登录状态",
        "logout_hint": "退出 B 站账号后关闭本窗口",
    },
    "zhihu": {
        "name": "知乎",
        "login_url": "https://www.zhihu.com/signin",
        "logout_url": "https://www.zhihu.com/",
        "auth_cookies": ("z_c0",),
        "hosts": ("zhihu.com",),
        "selectors": [".QuestionAnswer-content", ".RichText", ".Question-main", "article", "main"],
        "login_hint": "登录完成后关闭本窗口即可保存登录状态",
        "logout_hint": "请在网页右上角头像菜单里退出登录，检测到退出后会自动关闭",
    },
}

_STATUS_FILE = "platform_login.json"
_STATUS_VERSION = 1
_LOCK = threading.RLock()
_WINDOWS = {}          # platform -> {"window": win, "mode": login|logout}
_POLLS = {}
_RENDER_LOCK = threading.Lock()
_LAST_ERROR = ""

_JS_EXTRACT = r"""
(function(){
  var sels = __SELECTORS__;
  var junk = 'script,style,noscript,svg,iframe,canvas,nav,footer,header,aside,form,button';
  function clean(s){
    return String(s || '').replace(/\u00a0/g,' ').replace(/[ \t]+\n/g,'\n')
      .replace(/\n{3,}/g,'\n\n').replace(/[ \t]{2,}/g,' ').trim();
  }
  var nodes = [];
  for (var i=0; i<sels.length; i++) {
    try {
      var found = document.querySelectorAll(sels[i]);
      if (found && found.length) { nodes = Array.prototype.slice.call(found); break; }
    } catch(e) {}
  }
  var text = '';
  if (nodes.length) {
    var parts = [], seen = {};
    nodes.forEach(function(n){
      var t = clean(n.innerText || n.textContent || '');
      if (t && !seen[t]) { seen[t] = 1; parts.push(t); }
    });
    text = parts.join('\n\n');
  }
  if (!text || text.length < 30) {
    var root = document.querySelector('article, main, [role="main"]') || document.body;
    if (!root) return {title: document.title || '', url: location.href, text: ''};
    var clone = root.cloneNode(true);
    var bad = clone.querySelectorAll(junk);
    for (var j=0; j<bad.length; j++) bad[j].remove();
    text = clean(clone.innerText || clone.textContent || '');
  }
  return {title: (document.title || '').slice(0,240), url: location.href, text: text.slice(0,8000)};
})()
"""


def _data_file(data_dir: str) -> str:
    return os.path.join(data_dir, _STATUS_FILE)


def _load(data_dir: str) -> dict:
    path = _data_file(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("platforms"), dict):
            return d
    except Exception:
        pass
    return {"version": _STATUS_VERSION, "platforms": {}}


def _save(data_dir: str, d: dict) -> None:
    path = _data_file(data_dir)
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def _platform_row(data_dir: str, key: str) -> dict:
    conf = PLATFORMS[key]
    saved = (_load(data_dir).get("platforms") or {}).get(key) or {}
    active = _WINDOWS.get(key)
    return {
        "key": key,
        "name": conf["name"],
        "logged_in": bool(saved.get("logged_in")),
        "last_login": saved.get("last_login") or "",
        "last_logout": saved.get("last_logout") or "",
        "last_check": saved.get("last_check") or "",
        "active": bool(active),
        "mode": (active or {}).get("mode") or "",
    }


def _webview_ready() -> bool:
    if os.name != "nt":
        return False
    try:
        import webview  # noqa: F401
        from webview import guilib
        return guilib is not None
    except Exception:
        return False


def status(data_dir: str) -> dict:
    return {
        "ok": True,
        "supported": _webview_ready(),
        "platforms": [_platform_row(data_dir, k) for k in PLATFORMS],
        "error": _LAST_ERROR,
    }


def _cookie_names(cookies) -> list:
    names = []
    for c in cookies or []:
        try:
            for name in c.keys():
                if name and name not in names:
                    names.append(name)
        except Exception:
            pass
    return names


def _logged(cookies, key: str) -> bool:
    auth = set(PLATFORMS[key]["auth_cookies"])
    return bool(auth.intersection(_cookie_names(cookies)))


def _set_state(data_dir: str, key: str, logged: bool, names: list, mode: str) -> None:
    with _LOCK:
        d = _load(data_dir)
        platforms = d.setdefault("platforms", {})
        row = platforms.setdefault(key, {})
        was = bool(row.get("logged_in"))
        row["logged_in"] = bool(logged)
        row["cookie_names"] = names[:80]
        row["last_check"] = iso_bj()
        if logged and not was:
            row["last_login"] = iso_bj()
        if not logged and was:
            row["last_logout"] = iso_bj()
        _save(data_dir, d)


def _apply_cookies(data_dir: str, platform: str, window, mode: str) -> tuple[bool, bool]:
    """读一次窗口 Cookie 并写入本机状态；返回 (现在是否已登录, 之前是否已登录)。"""
    cookies = window.get_cookies()
    names = _cookie_names(cookies)
    logged = _logged(cookies, platform)
    was = bool((_load(data_dir).get("platforms") or {}).get(platform, {}).get("logged_in"))
    _set_state(data_dir, platform, logged, names, mode)
    return logged, was


def _poll(platform: str, data_dir: str, window, mode: str) -> None:
    """轮询登录窗口 Cookie；登录成功或退出成功时更新本机状态。"""
    try:
        while True:
            try:
                if window.events.closed.is_set():
                    break
            except Exception:
                pass
            try:
                logged, was = _apply_cookies(data_dir, platform, window, mode)
            except Exception:
                break
            if logged and mode == "login":
                try:
                    window.set_title("已检测到%s登录 · 关闭窗口即可" % PLATFORMS[platform]["name"])
                except Exception:
                    pass
            elif mode == "logout" and was:
                # 退出页已把登录 Cookie 清掉：自动关掉退出窗口，避免用户再手动关
                time.sleep(0.8)
                try:
                    window.destroy()
                except Exception:
                    pass
                break
            time.sleep(1.2)
    finally:
        with _LOCK:
            cur = _WINDOWS.get(platform)
            if cur and cur.get("window") is window:
                _WINDOWS.pop(platform, None)


def start_login(data_dir: str, platform: str) -> dict:
    global _LAST_ERROR
    if platform not in PLATFORMS:
        return {"ok": False, "message": "未知平台"}
    with _LOCK:
        if _WINDOWS.get(platform):
            return {"ok": True, "active": True, "message": "登录窗口已经打开，请完成登录后关闭窗口"}
    if not _webview_ready():
        return {"ok": False, "message": "当前版本无法创建内置登录窗口，请确认 WebView2 运行库已安装"}
    conf = PLATFORMS[platform]
    try:
        import webview
        window = webview.create_window(
            "登录%s · %s" % (conf["name"], conf["login_hint"] if "login_hint" in conf else conf["logout_hint"]),
            conf["login_url"], width=620, height=820, min_size=(420, 600), on_top=True, text_select=True)
        if window is None:
            raise RuntimeError("窗口创建被取消")
        with _LOCK:
            _WINDOWS[platform] = {"window": window, "mode": "login"}
        _POLLS[platform] = threading.Thread(target=_poll, args=(platform, data_dir, window, "login"),
                                           daemon=True, name="platform-login-" + platform)
        _POLLS[platform].start()
        _LAST_ERROR = ""
        return {"ok": True, "active": True, "message": "已打开%s登录窗口；完成登录后关闭窗口即可保存" % conf["name"]}
    except Exception as e:
        _LAST_ERROR = "%s: %s" % (type(e).__name__, str(e)[:120])
        return {"ok": False, "message": "打开登录窗口失败：%s" % _LAST_ERROR}


def start_logout(data_dir: str, platform: str) -> dict:
    global _LAST_ERROR
    if platform not in PLATFORMS:
        return {"ok": False, "message": "未知平台"}
    with _LOCK:
        if _WINDOWS.get(platform):
            return {"ok": True, "active": True, "message": "该平台的登录/退出窗口已经打开"}
    if not _webview_ready():
        return {"ok": False, "message": "当前版本无法创建内置退出窗口"}
    conf = PLATFORMS[platform]
    try:
        import webview
        window = webview.create_window(
            "退出%s账号 · %s" % (conf["name"], conf["logout_hint"]),
            conf["logout_url"], width=620, height=820, min_size=(420, 600), on_top=True, text_select=True)
        if window is None:
            raise RuntimeError("窗口创建被取消")
        with _LOCK:
            _WINDOWS[platform] = {"window": window, "mode": "logout"}
        _POLLS[platform] = threading.Thread(target=_poll, args=(platform, data_dir, window, "logout"),
                                           daemon=True, name="platform-logout-" + platform)
        _POLLS[platform].start()
        _LAST_ERROR = ""
        return {"ok": True, "active": True, "message": "已打开%s退出窗口；退出后会自动检测" % conf["name"]}
    except Exception as e:
        _LAST_ERROR = "%s: %s" % (type(e).__name__, str(e)[:120])
        return {"ok": False, "message": "打开退出窗口失败：%s" % _LAST_ERROR}


def clear_local(data_dir: str, platform: str) -> dict:
    if platform not in PLATFORMS:
        return {"ok": False, "message": "未知平台"}
    with _LOCK:
        d = _load(data_dir)
        row = (d.setdefault("platforms", {})).setdefault(platform, {})
        row["logged_in"] = False
        row["last_logout"] = iso_bj()
        row["cookie_names"] = []
        _save(data_dir, d)
    return {"ok": True, "message": "已清除本机登录记录；如需彻底退出，请再打开该平台退出账号"}


def platform_for_url(url: str) -> str:
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url or "").netloc or "").lower()
    except Exception:
        return ""
    for key, conf in PLATFORMS.items():
        if any(host == h or host.endswith("." + h) for h in conf["hosts"]):
            return key
    return ""


def is_logged_in(data_dir: str, platform: str) -> bool:
    return bool(((_load(data_dir).get("platforms") or {}).get(platform) or {}).get("logged_in"))


def mark_expired(data_dir: str, platform: str) -> None:
    if platform not in PLATFORMS:
        return
    with _LOCK:
        d = _load(data_dir)
        row = (d.setdefault("platforms", {})).setdefault(platform, {})
        if row.get("logged_in"):
            row["logged_in"] = False
            row["last_logout"] = iso_bj()
            row["last_check"] = iso_bj()
            _save(data_dir, d)


def _render_page(url: str, selectors: list) -> dict:
    """用最小化后隐藏的 WebView2 加载页面并抽取文字。每次用完销毁，避免残留窗口。"""
    if not _webview_ready():
        return {"status": "fail", "content": "", "reason": "当前版本没有 WebView2"}
    with _RENDER_LOCK:
        import webview
        window = None
        try:
            window = webview.create_window("日常小助手 · 读取正文", "about:blank",
                                           width=1200, height=800, minimized=True,
                                           min_size=(800, 600), text_select=True)
            if window is None:
                return {"status": "fail", "content": "", "reason": "渲染窗口创建失败"}
            if not window.events.shown.wait(12):
                return {"status": "fail", "content": "", "reason": "渲染窗口没有就绪"}
            try:
                window.hide()
            except Exception:
                pass
            window.events.loaded.clear()
            window.load_url(url)
            if not window.events.loaded.wait(22):
                return {"status": "fail", "content": "", "reason": "页面加载超时"}
            # 等前端脚本把搜索结果/回答渲染出来
            res = None
            for _ in range(8):
                time.sleep(0.8)
                try:
                    script = _JS_EXTRACT.replace("__SELECTORS__", json.dumps(selectors, ensure_ascii=False))
                    res = window.evaluate_js(script)
                except Exception:
                    res = None
                txt = (res or {}).get("text") if isinstance(res, dict) else ""
                if txt and len(txt) >= 80:
                    break
            final_url = ""
            try:
                final_url = window.get_current_url() or ""
            except Exception:
                pass
            txt = (res or {}).get("text") if isinstance(res, dict) else ""
            return {"status": "ok" if txt else "empty", "content": txt or "",
                    "chars": len(txt or ""), "title": (res or {}).get("title") if isinstance(res, dict) else "",
                    "final_url": final_url}
        except Exception as e:
            return {"status": "fail", "content": "", "reason": "%s: %s" % (type(e).__name__, str(e)[:120])}
        finally:
            if window is not None:
                try:
                    window.destroy()
                except Exception:
                    pass


def fetch_rendered(url: str, platform: str, data_dir: str) -> dict:
    """带登录态读取一篇热搜相关内容；返回 daily.fetch_article 同结构。"""
    conf = PLATFORMS.get(platform)
    if not conf:
        return {"status": "skip", "content": "", "chars": 0, "reason": "未知平台"}
    if not is_logged_in(data_dir, platform):
        return {"status": "need_login", "content": "", "chars": 0, "platform": platform,
                "reason": "需要先登录%s" % conf["name"]}
    res = _render_page(url, conf["selectors"])
    final_url = (res.get("final_url") or "").strip()
    # 只看域名和路径判断是否被踢到登录页，避免搜索词/文章标题里带 login 被误判
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(final_url)
        host = (parts.netloc or "").lower()
        path = (parts.path or "").lower()
        kicked = (host.startswith("passport.") or host.startswith("passport")
                  or path.startswith("/login") or path.startswith("/signin")
                  or path.startswith("/sign-up"))
    except Exception:
        kicked = False
    if kicked:
        mark_expired(data_dir, platform)
        return {"status": "need_login", "content": "", "chars": 0, "platform": platform,
                "reason": "%s登录已失效，请重新登录" % conf["name"]}
    if res.get("status") == "ok" and (res.get("chars") or 0) >= 60:
        return {"status": "ok", "content": res.get("content") or "",
                "chars": res.get("chars") or 0, "reason": ""}
    return {"status": "fail", "content": "", "chars": 0, "platform": platform,
            "reason": res.get("reason") or "页面里没识别出正文，可能需要在平台里打开"}


def close_all() -> None:
    with _LOCK:
        items = list(_WINDOWS.values())
        _WINDOWS.clear()
    for it in items:
        try:
            it["window"].destroy()
        except Exception:
            pass
