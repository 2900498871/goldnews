# -*- coding: utf-8 -*-
"""诊断包：日志尾部、脱敏配置、调度状态、数据库统计与最近事件。"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import zipfile

from .export import _unique
from .util import now_bj


def _tail(path: str, limit: int = 2 * 1024 * 1024) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > limit:
                f.seek(size - limit)
            return f.read().decode("utf-8", errors="replace")
    except OSError as e:
        return "读取失败：%s" % e


def diagnostics_zip(inst) -> dict:
    out = _unique(os.path.join(inst.data_dir, "exports", "日常小助手-诊断-%s.zip" %
                               now_bj().strftime("%Y%m%d-%H%M%S")))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    try:
        cfg = inst.cfg.public()
        sched = inst.scheduler.snapshot()
        state = inst.state()
        state.pop("history", None)
        info = {
            "created_at": now_bj().isoformat(),
            "os": platform.platform(),
            "python": sys.version,
            "executable": sys.executable,
            "data_dir": inst.data_dir,
            "frozen": bool(getattr(sys, "frozen", False)),
            "disk": dict(shutil.disk_usage(inst.data_dir)._asdict()),
        }
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("info.json", json.dumps(info, ensure_ascii=False, indent=2, default=str))
            z.writestr("config.public.json", json.dumps(cfg, ensure_ascii=False, indent=2, default=str))
            z.writestr("scheduler.json", json.dumps(sched, ensure_ascii=False, indent=2, default=str))
            z.writestr("state.json", json.dumps(state, ensure_ascii=False, indent=2, default=str))
            z.writestr("db_stats.json", json.dumps(inst.db.db_stats(), ensure_ascii=False,
                                                   indent=2, default=str))
            z.writestr("events.json", json.dumps(inst.db.recent_events(200), ensure_ascii=False,
                                                indent=2, default=str))
            z.writestr("docs.json", json.dumps(inst.db.daily_docs("news", limit=30), ensure_ascii=False,
                                              indent=2, default=str))
            z.writestr("app.log", _tail(os.path.join(inst.data_dir, "app.log")))
            z.writestr("stdout.log", _tail(os.path.join(inst.data_dir, "stdout.log")))
        return {"ok": True, "path": out, "size": os.path.getsize(out)}
    except Exception as e:
        try:
            os.remove(out)
        except OSError:
            pass
        return {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
