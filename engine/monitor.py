# -*- coding: utf-8 -*-
"""一键自检：逐项报告各外部依赖连通性（FR-09 / §13.1）。"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .prices import ADAPTERS
from .util import http_get, iso_bj

# 自检会在打开「行情」页时自动跑一次；DeepSeek 连通性测试是一次真实计费调用，
# 这里做 10 分钟短缓存，避免反复刷新页面把额度刷掉（设置页的「测试连接」不受影响）
_AI_CACHE = {"at": 0.0, "key": "", "result": None}


def _check_price(src: dict) -> dict:
    try:
        cls = ADAPTERS.get(src.get("kind"))
        if not cls:
            return {"id": src.get("id"), "name": src.get("name"), "ok": False,
                    "detail": "未知适配器 " + str(src.get("kind"))}
        r = cls().fetch(src)
        return {"id": src.get("id"), "name": src.get("name"), "ok": True,
                "detail": "%s %s（源报价时间 %s）" % (r.get("price"), r.get("unit"), r.get("source_ts") or "源未提供")}
    except Exception as e:
        return {"id": src.get("id"), "name": src.get("name"), "ok": False,
                "detail": "%s: %s" % (type(e).__name__, str(e)[:160])}


def _check_news(src: dict) -> dict:
    try:
        status, text, _ = http_get(src["url"], timeout=8)
        ok = status == 200 and bool(text.strip())
        return {"id": src.get("id"), "name": src.get("name"), "ok": ok,
                "detail": "HTTP %s · %d 字节" % (status, len(text))}
    except Exception as e:
        return {"id": src.get("id"), "name": src.get("name"), "ok": False,
                "detail": "%s: %s" % (type(e).__name__, str(e)[:140])}


def selfcheck(cfg_store, db, pipeline) -> dict:
    cfg = cfg_store.get()
    items = []
    ps = [s for s in cfg.get("price_sources", []) if s.get("enabled")]
    ns = [s for s in cfg.get("news", {}).get("sources", []) if s.get("enabled")]
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [("金价源", ex.submit(_check_price, s)) for s in ps] + \
               [("新闻源", ex.submit(_check_news, s)) for s in ns]
        for kind, f in futs:
            r = f.result()
            r["category"] = kind
            items.append(r)
    # DeepSeek
    key = cfg_store.secret("deepseek.api_key")
    if key:
        now = time.time()
        if (_AI_CACHE["result"] is not None and _AI_CACHE["key"] == key[-8:]
                and now - _AI_CACHE["at"] < 600):
            r = _AI_CACHE["result"]
        else:
            r = pipeline.ai.test_connection()
            _AI_CACHE.update(at=now, key=key[-8:], result=r)
    else:
        r = {"ok": False, "message": "未录入 API Key（设置页可配置）"}
    items.append({"category": "DeepSeek", "id": "deepseek", "name": "DeepSeek API",
                  "ok": bool(r.get("ok")), "detail": r.get("message", "")})
    # SMTP
    smtp = cfg.get("smtp", {})
    if smtp.get("enabled") and smtp.get("host"):
        r = pipeline.mailer.test_connection()
    else:
        r = {"ok": False, "message": "邮件通道未启用或未填服务器（设置页可配置）"}
    items.append({"category": "邮件", "id": "smtp", "name": "SMTP",
                  "ok": bool(r.get("ok")), "detail": r.get("message", "")})
    # 本地数据库
    try:
        db.event("info", "执行一键自检")
        items.append({"category": "本地", "id": "sqlite", "name": "本端 SQLite",
                      "ok": True, "detail": "可读写 · %s" % db.path})
    except Exception as e:
        items.append({"category": "本地", "id": "sqlite", "name": "本端 SQLite",
                      "ok": False, "detail": str(e)[:160]})
    ok_count = sum(1 for x in items if x["ok"])
    return {"ok": ok_count == len(items), "checked_at": iso_bj(),
            "instance_id": cfg_store.instance_id, "app_kind": cfg.get("app_kind"),
            "passed": ok_count, "total": len(items), "items": items,
            "note": "未配置或不启用的项目会显示为失败，不代表程序异常；金价/新闻源可多源冗余。"}
