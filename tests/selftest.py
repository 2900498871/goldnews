# -*- coding: utf-8 -*-
"""v1 离线自检：不联网、不发邮件、不调用 DeepSeek，覆盖关键降级与合规约束。

运行： python -X utf8 tests/selftest.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from engine.ai import _validate_signal  # noqa: E402
from engine.config import ConfigError, ConfigStore, default_config  # noqa: E402
from engine.history import (collect_history_news, fallback_result,  # noqa: E402
                            resolve_range, validate_result)
from engine.db import DB  # noqa: E402
from engine.news import NewsCollector  # noqa: E402
from engine.pipeline import Pipeline  # noqa: E402
from engine.prices import ADAPTERS, BaseAdapter, PriceCollector  # noqa: E402
from engine.renderer import render_daily, render_flash, render_history  # noqa: E402
from engine.util import (DISCLAIMER, get_logger, in_quiet_hours, iso_bj,  # noqa: E402
                         json_loads_loose, redact)

LOG = get_logger("selftest")


class _FakeAdapter(BaseAdapter):
    def __init__(self, value, symbol="XAU/USD", unit="USD/oz", ts=None):
        self.value, self.symbol, self.unit, self.ts = value, symbol, unit, ts

    def fetch(self, src):
        return {"price": self.value, "symbol": self.symbol, "unit": self.unit,
                "source_ts": self.ts or iso_bj(), "quality_flag": "mock"}


class _FailAdapter(BaseAdapter):
    def fetch(self, src):
        raise RuntimeError("模拟数据源中断")


class TmpInstance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = ConfigStore(self.tmp.name)
        self.db = DB(os.path.join(self.tmp.name, "test.db"), self.cfg.instance_id)
        self.pipe = Pipeline(self.cfg, self.db, LOG)

    def tearDown(self):
        self.tmp.cleanup()


class TestConfigAndSecrets(TmpInstance):
    def test_defaults_and_validation(self):
        c = self.cfg.get()
        self.assertEqual(c["app_kind"], "desktop")
        self.assertEqual(len(c["push_schedule"]["slots"]), 3)
        self.assertTrue(c["instance_id"].startswith("desktop-"))
        with self.assertRaises(ConfigError):
            self.cfg.update({"push_schedule": {"slots": [{"id": "x", "time": "25:99",
                                                           "type": "flash", "enabled": True}]}})
        with self.assertRaises(ConfigError):
            self.cfg.update({"smtp": {"enabled": True, "host": "", "to": []}})

    def test_secret_encrypted_and_not_leaked(self):
        self.cfg.update({"deepseek": {"api_key": "sk-SUPER-SECRET-123456"},
                         "smtp": {"credential": "APP-PASSWORD-999"}})
        with open(self.cfg.path, "r", encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("sk-SUPER-SECRET-123456", raw)
        self.assertNotIn("APP-PASSWORD-999", raw)
        self.assertEqual(self.cfg.secret("deepseek.api_key"), "sk-SUPER-SECRET-123456")
        pub = json.dumps(self.cfg.public(), ensure_ascii=False)
        self.assertNotIn("sk-SUPER-SECRET-123456", pub)
        self.assertTrue(self.cfg.public()["deepseek"]["api_key_set"])
        # 掩码回传应保持原值
        self.cfg.update({"deepseek": {"api_key": "__KEEP_EXISTING__"}})
        self.assertEqual(self.cfg.secret("deepseek.api_key"), "sk-SUPER-SECRET-123456")
        self.assertIn("REDACTED", redact("key=sk-SUPER-SECRET-123456", ["sk-SUPER-SECRET-123456"]))

    def test_versioning_and_rollback(self):
        v0 = self.cfg.cfg["config_version"]
        r = self.cfg.update({"alert": {"intraday_pct": 3.5}})
        self.assertEqual(r["config_version"], v0 + 1)
        self.assertEqual(self.cfg.get()["alert"]["intraday_pct"], 3.5)
        self.cfg.rollback()
        self.assertEqual(self.cfg.get()["alert"]["intraday_pct"], 2.0)


class TestPriceDegradation(TmpInstance):
    def test_divergence_detected(self):
        ADAPTERS["_t_a"] = lambda: _FakeAdapter(3500.0)
        ADAPTERS["_t_b"] = lambda: _FakeAdapter(3600.0)
        self.cfg.update({"price_sources": [
            {"id": "a", "name": "源A", "kind": "_t_a", "url": "", "symbol": "XAU/USD", "enabled": True, "priority": 1},
            {"id": "b", "name": "源B", "kind": "_t_b", "url": "", "symbol": "XAU/USD", "enabled": True, "priority": 2},
        ], "divergence_tolerance_pct": 1.0})
        snap = PriceCollector(self.cfg, self.db, LOG).collect("manual")
        self.assertTrue(any("源间分歧" in n for n in snap["quality_notes"]))
        main = [p for p in snap["prices"] if p["symbol"] == "XAU/USD" and not p.get("alternate")][0]
        self.assertEqual(main["quality_flag"], "divergent")
        self.assertEqual(len(main["divergent_values"]), 2)

    def test_all_sources_fail_falls_back_to_cache(self):
        ADAPTERS["_t_ok"] = lambda: _FakeAdapter(3500.0)
        ADAPTERS["_t_fail"] = _FailAdapter
        self.cfg.update({"price_sources": [
            {"id": "a", "name": "源A", "kind": "_t_ok", "url": "", "symbol": "XAU/USD", "enabled": True, "priority": 1}]})
        PriceCollector(self.cfg, self.db, LOG).collect("manual")
        self.cfg.update({"price_sources": [
            {"id": "a", "name": "源A", "kind": "_t_fail", "url": "", "symbol": "XAU/USD", "enabled": True, "priority": 1}]})
        snap = PriceCollector(self.cfg, self.db, LOG).collect("manual")
        cached = [p for p in snap["prices"] if p["quality_flag"] == "cached"]
        self.assertTrue(cached, "应有缓存值回退")
        self.assertTrue(any("缓存值" in n for n in snap["quality_notes"]))

    def test_every_price_has_source_and_source_ts(self):
        ADAPTERS["_t_ok"] = lambda: _FakeAdapter(3500.0, ts="2026-01-01 09:30:00")
        self.cfg.update({"price_sources": [
            {"id": "a", "name": "源A", "kind": "_t_ok", "url": "", "symbol": "XAU/USD", "enabled": True, "priority": 1}]})
        snap = PriceCollector(self.cfg, self.db, LOG).collect("manual")
        for p in snap["prices"]:
            self.assertTrue(p.get("source"))
            self.assertTrue(p.get("source_ts"))
            self.assertTrue(p.get("fetched_at"))
            self.assertTrue(p.get("unit"))
        # 禁止用抓取时刻顶替源报价时间
        main = [p for p in snap["prices"] if p["symbol"] == "XAU/USD"][0]
        self.assertEqual(main["source_ts"], "2026-01-01 09:30:00")
        self.assertNotEqual(main["source_ts"], main["fetched_at"])
        self.assertEqual(main["quality_flag"], "stale")


class TestNewsAndAI(TmpInstance):
    def test_news_missing_is_explicit(self):
        self.cfg.update({"news": {"sources": []}})
        r = NewsCollector(self.cfg, self.db, LOG).collect("manual")
        self.assertFalse(r["news_available"])
        self.assertIn("未获取到新闻", r["note"])

    def test_ai_schema_low_confidence_and_banned_words(self):
        news = {"news_available": False, "news": []}
        snap = {"prices": [], "quality_notes": []}
        raw = {"direction": "bullish", "confidence": "low", "horizon": "intraday",
               "counter_case": "可能错在汇率", "key_drivers": [], "watch_points": [],
               "summary_cn": "测试", "data_gaps": [], "disclaimer": "被模型改写了"}
        out, warns = _validate_signal(raw, snap, news)
        self.assertEqual(out["direction"], "unclear")
        self.assertEqual(out["disclaimer"], DISCLAIMER)
        self.assertTrue(warns)
        self.assertIn("本次新闻数据缺失", out["data_gaps"])
        raw2 = dict(raw, confidence="high", summary_cn="建议买入 5 克，目标价 800", counter_case="无")
        out2, warns2 = _validate_signal(raw2, {"prices": [{"symbol": "XAU/USD"}], "quality_notes": []},
                                        {"news_available": True, "news": []})
        self.assertEqual(out2["direction"], "unclear")
        self.assertTrue(any("禁词" in w for w in warns2))

    def test_empty_content_retry_helper(self):
        self.assertRaises(ValueError, lambda: json_loads_loose("   "))
        self.assertEqual(json_loads_loose('```json\n{"a":1}\n```'), {"a": 1})
        self.assertEqual(json_loads_loose('前缀 {"a": 2} 后缀'), {"a": 2})


class TestRenderingAndSchedule(TmpInstance):
    def _snapshot(self):
        return {
            "as_of_bj": iso_bj(), "market_status": "open", "quality_notes": [],
            "source_status": {}, "snapshot_hash": "abc", "folded_cny_g": 768.25,
            "fx": {"usd_cny": 6.68, "source": "open.er-api.com", "source_ts": iso_bj()},
            "prices": [
                {"symbol": "XAU/USD", "name": "国际现货黄金", "price": 3578.1, "unit": "USD/oz",
                 "change_pct": 0.12, "change_base": "prev_snapshot", "source": "gold-api.com",
                 "source_ts": "2026-11-26 11:55:47", "quality_flag": "ok", "role": "驱动口径"},
                {"symbol": "FOLDED_CNY_G", "name": "折算参考价（非银行成交价）", "price": 768.25,
                 "unit": "CNY/g", "change_pct": None, "change_base": "不适用", "source": "计算派生",
                 "source_ts": "同 XAU/USD", "quality_flag": "derived", "role": "参考，非成交价"},
            ],
        }

    def test_flash_and_daily_have_disclaimer_and_sources(self):
        snap = self._snapshot()
        news = {"news_available": False, "news": [], "note": "本次未获取到新闻", "source_status": {}}
        ai = {"available": False, "reason": "未配置 API Key", "signal": {
            "direction": "unclear", "confidence": "low", "horizon": "1-3d",
            "confidence_reason": "数据不足", "key_drivers": [], "counter_case": "数据不足",
            "watch_points": [], "summary_cn": "仅展示数据", "data_gaps": ["本次新闻数据缺失"],
            "disclaimer": DISCLAIMER, "_fallback": True, "model": "—", "prompt_version": "v3"}}
        for r in (render_flash(snap, news, ai), render_daily(snap, news, ai, "信号回看")):
            self.assertIn(DISCLAIMER, r["text_body"])
            self.assertIn(DISCLAIMER, r["html"])
            self.assertIn("gold-api.com", r["text_body"])
            self.assertIn("2026-11-26 11:55:47", r["text_body"])
            self.assertIn("非银行成交价", r["text_body"])
            self.assertIn("本次未获取到新闻", r["text_body"])
            self.assertIn("AI 研判暂不可用", r["text_body"])

    def test_quiet_hours_cross_midnight(self):
        self.assertTrue(in_quiet_hours("23:30", "23:00", "07:00"))
        self.assertTrue(in_quiet_hours("06:59", "23:00", "07:00"))
        self.assertFalse(in_quiet_hours("12:30", "23:00", "07:00"))
        self.assertFalse(in_quiet_hours("08:00", "23:00", "07:00"))

    def test_independent_data_dirs(self):
        with tempfile.TemporaryDirectory() as t2:
            other = ConfigStore(t2)
            self.cfg.update({"push_schedule": {"slots": [{"id": "z", "time": "06:00",
                                                           "type": "flash", "enabled": True}]}})
            self.assertEqual(len(other.get()["push_schedule"]["slots"]), 3)
            self.assertNotEqual(self.cfg.instance_id, other.instance_id)
            self.assertNotEqual(self.cfg.path, other.path)


class TestHistoryAnalysis(TmpInstance):
    def test_range_presets_and_validation(self):
        r = resolve_range("6m")
        self.assertEqual(r["preset"], "6m")
        self.assertEqual(r["days"], 183)
        r1 = resolve_range("1y")
        self.assertEqual(r1["days"], 365)
        r2 = resolve_range("custom", "2026-01-01", "2026-06-30")
        self.assertEqual((r2["start"], r2["end"]), ("2026-01-01", "2026-06-30"))
        with self.assertRaises(ValueError):
            resolve_range("custom", "2026-06-30", "2026-01-01")
        with self.assertRaises(ValueError):
            resolve_range("custom", "bad", "2026-01-01")
        with self.assertRaises(ValueError):
            resolve_range("7y")

    def _collection(self, total=3, months=1, archive=False):
        news = [{"title": "黄金与美联储利率路径", "summary": "", "url": "https://e/%d" % i,
                 "source": "测试源", "date": "2026-11-1%d" % (i + 1), "topic": "inflation_fed",
                 "importance": 6, "published_at_bj": "2026-11-1%d 09:00" % (i + 1),
                 "origin": "测试"} for i in range(min(total, 9))]
        return {"total": total, "news": news, "topic_stats": [{"topic": "inflation_fed",
                "label": "通胀 / 美联储", "count": total}],
                "monthly": [{"month": "2026-%02d" % (i + 1), "count": 1} for i in range(months)],
                "sources": {"local": "ok (%d 条)" % total}, "ok_sources": ["local"],
                "archive_sources_ok": archive, "coverage_note": "测试样本", "available": True}

    def test_low_sample_forces_unclear_and_banned_words(self):
        rng = resolve_range("6m")
        col = self._collection(total=3, months=1)
        raw = {"direction": "bullish", "confidence": "high", "forward_horizon": "1-3m",
               "summary_cn": "历史新闻偏多", "counter_case": "可能反转",
               "key_themes": [], "timeline": [], "watch_points": [], "confidence_reason": "样本",
               "data_gaps": [], "disclaimer": "被模型改写"}
        out, warns = validate_result(raw, rng, "1-3m", col)
        self.assertEqual(out["direction"], "unclear")
        self.assertEqual(out["confidence"], "low")
        self.assertEqual(out["disclaimer"], DISCLAIMER)
        self.assertTrue(any("unclear" in w for w in warns))
        col_big = self._collection(total=40, months=6, archive=True)
        raw2 = dict(raw, summary_cn="建议买入 10 克，目标价 900，必涨")
        out2, warns2 = validate_result(raw2, rng, "1-3m", col_big)
        self.assertEqual(out2["direction"], "unclear")
        self.assertTrue(any("禁词" in w for w in warns2))

    def test_fallback_and_report_have_disclaimer(self):
        rng = resolve_range("1y")
        col = self._collection(total=0)
        res = fallback_result(rng, "1-3m", col, "未配置 DeepSeek API Key")
        rep = render_history(rng, col, res, {"available": False,
                                             "reason": "未配置 DeepSeek API Key"})
        self.assertIn(DISCLAIMER, rep["html"])
        self.assertIn(DISCLAIMER, rep["text_body"])
        self.assertIn("不是价格预测", rep["text_body"])
        self.assertIn("未配置 DeepSeek", rep["text_body"])
        self.assertEqual(rep["report_type"], "history_analysis")

    def test_offline_collector_is_honest_and_time_bounded(self):
        # 关掉所有外部源，只留本端库；不应联网、不应抛错
        self.cfg.update({"history_analysis": {"sources": [
            {"id": "local", "name": "本端已落库新闻", "kind": "local", "enabled": True}]}})
        self.db.insert_news({
            "dedup_hash": "h1", "title": "Gold rises as Fed signals rate cut",
            "summary": "", "url": "https://example.com/1", "source": "测试源",
            "published_at_local": "Fri, 20 Nov 2026 09:00:00 +0000",
            "published_at_bj": "2026-11-20 17:00", "language": "en",
            "topic": "inflation_fed", "importance": 7, "fetched_at": iso_bj()})
        rng = resolve_range("custom", "2026-11-01", "2026-11-26")
        t0 = __import__("time").time()
        col = collect_history_news(self.cfg.get(), self.db, LOG, rng,
                                   self.cfg.secret, 20)
        self.assertLess(__import__("time").time() - t0, 3.0)
        self.assertEqual(col["total"], 1)
        self.assertFalse(col["archive_sources_ok"])
        self.assertIn("外部历史新闻档案源本次均不可用", col["coverage_note"])

    def test_config_migration_adds_history_section(self):
        with tempfile.TemporaryDirectory() as t2:
            p = os.path.join(t2, "config.json")
            old = default_config()
            old.pop("history_analysis")
            old["instance_id"] = "desktop-migrated"
            with open(p, "w", encoding="utf-8") as f:
                json.dump(old, f, ensure_ascii=False)
            c = ConfigStore(t2)
            self.assertIn("history_analysis", c.get())
            self.assertEqual(c.get()["history_analysis"]["default_preset"], "6m")
            self.assertEqual(c.instance_id, "desktop-migrated")
            self.assertEqual(c.get()["app_kind"], "desktop")


class TestV2Fixes(TmpInstance):
    """v2 修复回归：多源并列行、时间窗、时点补发、长区间抽样。"""

    def _price(self, symbol="XAU/USD", price=3574.0, source="gold-api.com",
               alternate=False, fetched=None):
        return {"symbol": symbol, "name": symbol, "price": price, "unit": "USD/oz",
                "source": source, "source_ts": iso_bj(),
                "fetched_at": fetched or iso_bj(), "quality_flag": "ok",
                "change_base": "prev_snapshot", "role": "驱动口径",
                "alternate": alternate}

    def test_alternate_rows_are_not_primary(self):
        self.db.insert_price(self._price(price=3574.0, source="gold-api.com"))
        self.db.insert_price(self._price(price=3573.8, source="新浪财经", alternate=True))
        # 并列源只留痕，不参与最新价 / 涨跌基准 / 走势图
        fresh = self.db.latest_prices(10)
        self.assertEqual(len(fresh), 1)
        self.assertEqual(fresh[0]["source"], "gold-api.com")
        self.assertEqual(self.db.last_price("XAU/USD")["source"], "gold-api.com")
        self.assertEqual([h["price"] for h in self.db.price_history("XAU/USD", 10)], [3574.0])
        # 老库迁移：raw_json 里的 alternate 会被回填成 is_alternate=1
        self.assertEqual(len(self.db.latest_prices(10, include_alternate=True)), 2)

    def test_last_price_window_respects_timezone(self):
        from datetime import timedelta
        from engine.util import now_bj
        old = (now_bj() - timedelta(hours=50)).isoformat(timespec="seconds")
        self.db.insert_price(self._price(price=3000.0, fetched=old))
        self.assertIsNone(self.db.last_price("XAU/USD"))
        self.db.insert_price(self._price(price=3500.0))
        self.assertEqual(self.db.last_price("XAU/USD")["price"], 3500.0)

    def test_recent_news_uses_publish_time(self):
        from engine.util import now_bj
        self.db.insert_news({"dedup_hash": "old-1", "title": "三个月前的旧闻",
                             "summary": "", "url": "https://example.com/old",
                             "source": "测试源", "published_at_local": "2026-05-01 02:00 UTC",
                             "published_at_bj": "2026-05-01 10:00", "language": "en",
                             "topic": "gold", "importance": 9, "fetched_at": iso_bj()})
        self.assertEqual(self.db.recent_news(24, 8), [])
        self.db.insert_news({"dedup_hash": "new-1", "title": "刚刚发布的新闻",
                             "summary": "", "url": "https://example.com/new",
                             "source": "测试源", "published_at_local": "现在",
                             "published_at_bj": now_bj().strftime("%Y-%m-%d %H:%M"),
                             "language": "zh", "topic": "gold", "importance": 5,
                             "fetched_at": iso_bj()})
        rows = self.db.recent_news(24, 8)
        self.assertEqual([r["title"] for r in rows], ["刚刚发布的新闻"])

    def test_history_interval_zero_allowed(self):
        r = self.cfg.update({"history_analysis": {"min_interval_seconds": 0}})
        self.assertEqual(self.cfg.get()["history_analysis"]["min_interval_seconds"], 0)
        self.assertEqual(r["config_version"], 2)

    def test_history_ai_sample_spreads_months(self):
        from engine.history import _sample_for_ai
        news = [{"date": "2026-%02d-%02d 10:00" % (m, d), "title": "t"}
                for m in (5, 6, 7, 8, 9) for d in range(1, 29)]
        pick = _sample_for_ai(news, 10)
        self.assertEqual(len(pick), 10)
        self.assertEqual(len({x["date"][:7] for x in pick}), 5)

    def test_slot_catchup_window(self):
        from datetime import datetime, timedelta
        from unittest import mock
        from engine import scheduler as sch_mod
        from engine.util import now_bj

        class _Cfg:
            path = os.path.join(ROOT, "no_such_config_for_test.json")

            def __init__(self, c):
                self.cfg, self.instance_id = c, "test"

            def get(self):
                return self.cfg

        class _DB:
            def __init__(self):
                self.fired = {}

            def slot_fired(self, k):
                return k in self.fired

            def mark_slot_fired(self, k, reason):
                self.fired[k] = reason

            def mails_today(self):
                return 0

            def event(self, level, msg):
                pass

        class _Pipe:
            def __init__(self):
                self.calls = []

            def run(self, kind, trigger, send_mail):
                self.calls.append(kind)
                return {"ok": True}

        cfg = self.cfg.get()
        cfg["push_schedule"].update({"slots": [
            {"id": "a", "time": "08:00", "type": "daily_summary", "channels": ["mail"], "enabled": True},
            {"id": "b", "time": "07:40", "type": "flash", "channels": ["mail"], "enabled": True},
        ], "catchup_minutes": 10})
        fake_now = datetime(2026, 11, 26, 8, 5, 30, tzinfo=now_bj().tzinfo)
        with mock.patch.object(sch_mod, "now_bj", lambda: fake_now):
            s = sch_mod.Scheduler(_Cfg(cfg), _DB(), _Pipe(), LOG)
            s.last_collect = __import__("time").time()
            s._tick()
            s._tick()
        self.assertEqual(s.pipeline.calls, ["daily_summary"])
        self.assertEqual(s.db.fired["2026-11-26|a|08:00"], "firing")


class TestServerHandler(unittest.TestCase):
    def test_no_local_import_shadows_module_global(self):
        """回归：do_POST 里曾有局部 `import threading`，导致 /api/window quit
        抛 UnboundLocalError，退出按钮失效。函数内不得重复导入同名全局模块。"""
        import ast
        path = os.path.join(ROOT, "server.py")
        with open(path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=path)
        globals_ = set()
        for n in tree.body:
            if isinstance(n, ast.Import):
                globals_.update(a.name.split(".")[0] for a in n.names)
        for n in ast.walk(tree):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for sub in ast.walk(n):
                if isinstance(sub, ast.Import):
                    for a in sub.names:
                        self.assertNotIn(
                            a.name.split(".")[0], globals_,
                            "%s 内重复导入了全局模块 %s" % (n.name, a.name))
        self.assertIn("threading", globals_)

    def test_shutdown_and_window_handlers_reference_timer(self):
        """退出链路必须能从本机 API 触发服务停止，且 window_action 支持 dpr。"""
        import inspect
        sys.path.insert(0, ROOT)
        import server  # noqa: E402
        src = inspect.getsource(server)
        self.assertIn("threading.Timer(0.8, self.server.shutdown)", src)
        self.assertIn('float(body.get("dpr") or 0)', src)
        import launcher  # noqa: E402
        self.assertIn("dpr", inspect.signature(launcher.window_action).parameters)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    print("GoldDesk v1 离线自检（不联网 / 不发信 / 不调用 DeepSeek）")
    unittest.main(verbosity=2)
