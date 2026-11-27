# -*- coding: utf-8 -*-
"""任务编排：定时与手动复用同一条链路（FR-11），全链路降级（FR-09）。"""
from __future__ import annotations

import threading
import time

from .ai import Analyzer
from .news import NewsCollector
from .notifier import Mailer
from .prices import PriceCollector
from .renderer import render_daily, render_flash
from .util import OZ_TO_GRAM, iso_bj, log_line, now_bj, redact


class Pipeline:
    """一条采集 → 研判 → 报告 → 投递流水线，由本端进程独占。"""

    def __init__(self, cfg_store, db, logger):
        self.cfg = cfg_store
        self.db = db
        self.log = logger
        self.prices = PriceCollector(cfg_store, db, logger)
        self.news = NewsCollector(cfg_store, db, logger)
        self.ai = Analyzer(cfg_store, db, logger)
        self.mailer = Mailer(cfg_store, db, logger)
        self.lock = threading.RLock()
        self.last_manual_ts = 0.0
        self.last_run = None
        self.last_quality_notes = []

    # ---------- 采集 ----------
    def collect(self, trigger="scheduled", need_prices=True, need_news=True, force=False):
        snapshot = self.prices.collect(trigger, force) if need_prices else self._cached_snapshot()
        news = self.news.collect(trigger) if need_news else self._cached_news()
        self.last_quality_notes = list(snapshot.get("quality_notes") or [])
        return snapshot, news

    def _cached_snapshot(self):
        rows = self.db.latest_prices(40)
        if not rows:
            return {"as_of_bj": iso_bj(), "market_status": "unknown", "prices": [], "fx": {},
                    "folded_cny_g": None, "quality_notes": ["无历史价格缓存"], "source_status": {},
                    "snapshot_hash": "empty"}
        seen, prices = set(), []
        for r in reversed(rows):
            if r["symbol"] in seen:
                continue
            seen.add(r["symbol"])
            prices.append({k: r.get(k) for k in ("symbol", "name", "price", "unit", "change_pct",
                                                 "change_base", "source", "source_ts", "quality_flag",
                                                 "role")})
        xau = next((p for p in prices if p["symbol"] == "XAU/USD"), None)
        fx = next((p for p in prices if p["symbol"] == "USD/CNY"), None)
        folded = None
        if xau and fx and xau.get("price") and fx.get("price"):
            folded = round(float(xau["price"]) / OZ_TO_GRAM * float(fx["price"]), 2)
        return {"as_of_bj": iso_bj(), "market_status": "cached", "prices": prices,
                "fx": {"usd_cny": fx.get("price") if fx else None,
                       "source": fx.get("source") if fx else None,
                       "source_ts": fx.get("source_ts") if fx else None},
                "folded_cny_g": folded,
                "quality_notes": ["使用本端最近一次成功快照（非本轮实时采集）"],
                "source_status": {}, "snapshot_hash": "cached-" + str(rows[-1]["id"])}

    def _cached_news(self):
        items = self.db.recent_news(24, 8)
        return {"news_available": bool(items), "news": items, "source_status": {},
                "note": "" if items else "本次未获取到新闻", "fetched_at": iso_bj()}

    # ---------- 主链路 ----------
    def run(self, kind="flash", trigger="scheduled", send_mail=None, force=False,
            manual_action=None) -> dict:
        """kind: flash | daily_summary | news | analyze | prices | full
        manual_action: fetch_news | re_analyze | fetch_and_analyze | refresh_prices
        """
        with self.lock:
            cfg = self.cfg.get()
            if trigger == "manual":
                gap = float(cfg.get("manual_fetch", {}).get("min_interval_seconds", 60))
                if not force and time.time() - self.last_manual_ts < gap:
                    return {"ok": False, "throttled": True,
                            "message": "手动获取过于频繁，请在 %.0f 秒后重试（最小间隔 %d 秒）" %
                                       (gap - (time.time() - self.last_manual_ts), gap)}
                self.last_manual_ts = time.time()
            started = now_bj()
            tid = self.db.start_task("pipeline:" + kind, trigger)
            src_status = {}
            report = None
            delivery = None
            try:
                need_prices = kind not in ("news", "analyze")
                need_news = kind not in ("prices", "analyze")
                do_ai = kind not in ("prices", "news")
                snapshot, news = self.collect(trigger, need_prices, need_news, force)
                src_status = {"price": snapshot.get("source_status", {}),
                              "news": news.get("source_status", {})}
                ai_result = None
                if do_ai:
                    ai_result = self.ai.analyze(snapshot, news, trigger, force,
                                                use_pro=(kind == "daily_summary"))
                    src_status["ai"] = "ok" if ai_result.get("available") else "unavailable: " + str(ai_result.get("reason", ""))[:100]
                else:
                    prev = self.db.latest_signal()
                    if prev:
                        import json
                        try:
                            ai_result = {"available": True, "signal": json.loads(prev["signal_json"])}
                        except Exception:
                            ai_result = None
                if kind in ("flash", "daily_summary", "full"):
                    rtype = "daily_summary" if kind == "daily_summary" else "flash"
                    if rtype == "daily_summary":
                        report = render_daily(snapshot, news, ai_result or {}, self._signal_review())
                    else:
                        report = render_flash(snapshot, news, ai_result or {})
                    report["signal_id"] = None
                    report["id"] = self.db.insert_report(report)
                if send_mail is None:
                    send_mail = (trigger == "scheduled" and kind in ("flash", "daily_summary"))
                if send_mail and report:
                    delivery = self.mailer.send_report(report)
                    src_status["mail"] = "ok" if delivery.get("ok") else delivery.get("message", "failed")
                self.db.end_task(tid, "ok", src_status, started=started)
                log_line(self.log, "pipeline:" + kind, "ok", trigger,
                         prices=sum(1 for p in snapshot.get("prices", []) if not p.get("alternate")),
                         news=len(news.get("news", [])),
                         ai=bool(ai_result and ai_result.get("available")), mail=bool(delivery and delivery.get("ok")))
                out = {"ok": True, "kind": kind, "trigger": trigger,
                       "snapshot": snapshot, "news": news, "ai": ai_result,
                       "report": ({"id": report["id"], "subject": report["subject"],
                                   "html": report["html"], "text_body": report["text_body"]} if report else None),
                       "delivery": delivery, "source_status": src_status,
                       "finished_at": iso_bj(), "budget": self.db.usage_today()}
                self.last_run = out
                return out
            except Exception as e:
                msg = redact("%s: %s" % (type(e).__name__, str(e)[:300]))
                self.db.end_task(tid, "error", src_status, error=msg, started=started)
                self.db.event("error", "pipeline 失败：" + msg[:200])
                self.log.exception("pipeline failed")
                return {"ok": False, "message": msg, "source_status": src_status}

    def _signal_review(self) -> str:
        rows = self.db.recent_signals(2)
        if len(rows) < 2:
            return "暂无可回看的历史信号（需累计两次以上研判）"
        prev = rows[1]
        cur = rows[0]
        xau = self.db.last_price("XAU/USD")
        return ("上一次信号（%s）：方向 %s / 置信度 %s；本次 %s / %s。%s。"
                "该对照仅用于人工校准阈值与 Prompt，不代表胜率或预测准确率。" %
                (prev.get("as_of_bj"), prev.get("direction"), prev.get("confidence"),
                 cur.get("direction"), cur.get("confidence"),
                 ("XAU/USD 最新快照 %s 美元/盎司" % xau["price"]) if xau else "价格数据缺失"))

    def state(self) -> dict:
        cfg = self.cfg.get()
        sig = self.db.latest_signal()
        import json
        signal = None
        if sig:
            try:
                signal = json.loads(sig["signal_json"])
                signal["id"] = sig["id"]
            except Exception:
                signal = None
        return {
            "app_kind": cfg.get("app_kind"), "instance_id": self.cfg.instance_id,
            "config_version": cfg.get("config_version"), "last_modified": cfg.get("last_modified"),
            "push_master_switch": cfg.get("push_master_switch", True),
            "server_time": iso_bj(),
            "prices": self.db.latest_prices(40),
            "price_history": self.db.price_history("XAU/USD", 80),
            "news": self.db.recent_news(24, 8),
            "signal": signal,
            "signals": self.db.recent_signals(10),
            "reports": self.db.recent_reports(10),
            "deliveries": self.db.recent_deliveries(10),
            "tasks": self.db.recent_tasks(10),
            "events": self.db.recent_events(20),
            "usage": self.db.usage_today(),
            "budget": {"max_calls_per_day": cfg["deepseek"]["max_calls_per_day"],
                       "daily_token_budget": cfg["deepseek"]["daily_token_budget"]},
            "holdings": self.db.holdings(),
            "audit": self.db.recent_audit(10),
            "quality_notes": self.last_quality_notes,
            "next_push": None,
        }
