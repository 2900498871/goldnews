# -*- coding: utf-8 -*-
"""本端数据清理：日志已由 RotatingFileHandler 轮转，这里清理正文缓存与陈旧事件。"""
from __future__ import annotations

import os
import time
from datetime import timedelta

from .util import now_bj


def prune_documents(doc_root: str, keep_body_days: int) -> int:
    """删除超过保留期的「*.正文.html」；Markdown 文档不动。"""
    if keep_body_days <= 0 or not os.path.isdir(doc_root):
        return 0
    cutoff = time.time() - keep_body_days * 86400
    removed = 0
    for base, _dirs, files in os.walk(doc_root):
        for name in files:
            if not name.lower().endswith(".正文.html"):
                continue
            path = os.path.join(base, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    removed += 1
            except OSError:
                pass
    return removed


def run_housekeeping(inst) -> dict:
    """启动时与每日任务后调用：清理文章缓存、过期正文 HTML、过量事件日志。"""
    cfg = inst.cfg.get()
    st = cfg.get("storage") or {}
    body_days = int(st.get("keep_body_days", 365) or 0)
    cache_days = int(st.get("keep_cache_days", 180) or 0)
    out = {"bodies": 0, "cache": 0, "events": 0, "slots": 0}
    try:
        doc_root = inst.daily.doc_root() if getattr(inst, "daily", None) else \
            (cfg.get("doc_dir") or inst.data_dir)
        out["bodies"] = prune_documents(doc_root, body_days)
    except Exception:
        pass
    try:
        if cache_days > 0:
            cutoff = (now_bj() - timedelta(days=cache_days)).isoformat()
            out["cache"] = inst.db.prune_article_cache(cutoff)
        out["events"] = inst.db.prune_events(3000)
        out["slots"] = inst.db.clear_stuck_slots(30)
    except Exception:
        pass
    return out
