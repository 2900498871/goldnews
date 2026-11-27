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


def maybe_vacuum(inst) -> bool:
    """数据库超过阈值且距上次 VACUUM 超过 N 天时回收空闲页。"""
    st = inst.cfg.get().get("storage") or {}
    min_mb = int(st.get("vacuum_min_mb", 50) or 0)
    days = int(st.get("vacuum_days", 7) or 0)
    if min_mb <= 0 or days <= 0:
        return False
    try:
        size = os.path.getsize(inst.db.path)
    except OSError:
        return False
    if size < min_mb * 1024 * 1024:
        return False
    marker = os.path.join(inst.data_dir, ".last_vacuum")
    try:
        if os.path.isfile(marker) and time.time() - os.path.getmtime(marker) < days * 86400:
            return False
    except OSError:
        pass
    inst.db.vacuum()
    try:
        with open(marker, "w", encoding="utf-8") as f:
            f.write(now_bj().isoformat())
    except OSError:
        pass
    return True


def run_housekeeping(inst) -> dict:
    """启动时与每日任务后调用：清理文章缓存、过期正文 HTML、过量事件日志。"""
    cfg = inst.cfg.get()
    st = cfg.get("storage") or {}
    body_days = int(st.get("keep_body_days", 365) or 0)
    cache_days = int(st.get("keep_cache_days", 180) or 0)
    out = {"bodies": 0, "cache": 0, "events": 0, "slots": 0, "vacuum": False}
    try:
        out["vacuum"] = maybe_vacuum(inst)
    except Exception:
        pass
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
