# -*- coding: utf-8 -*-
"""本地 HTTP API 服务（Windows 桌面端单实例）。

只监听本机回环地址；如确需在局域网内访问，可在启动参数里显式指定 --host。
界面里的所有配置读写都落到本实例的 config.json / secrets.bin / SQLite。
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.config import ConfigError, ConfigStore  # noqa: E402
from engine.db import DB  # noqa: E402
from engine.history import PeriodAnalyzer, collect_history_news, resolve_range  # noqa: E402
from engine.monitor import selfcheck  # noqa: E402
from engine.pipeline import Pipeline  # noqa: E402
from engine.renderer import render_history, mail_prefix  # noqa: E402
from engine.scheduler import Scheduler  # noqa: E402
from engine.util import DISCLAIMER, get_logger, iso_bj  # noqa: E402


class Instance:
    def __init__(self, root: str, port: int, host: str = "127.0.0.1",
                 dir_name: str = "desktop", data_dir: str | None = None,
                 ui_dir: str | None = None):
        # 仅保留 Windows 桌面端：app_kind 固定为 desktop
        # dir_name 是构建目录；data_dir / ui_dir 允许显式覆盖（测试实例、冻结 exe 用）
        self.app_kind = "desktop"
        self.dir_name = dir_name
        self.root = os.path.abspath(root)
        self.port = port
        self.host = host
        self.data_dir = os.path.abspath(data_dir or os.path.join(self.root, self.dir_name, "data"))
        self.ui_dir = os.path.abspath(ui_dir or os.path.join(self.root, self.dir_name, "ui"))
        os.makedirs(self.data_dir, exist_ok=True)
        self.log = get_logger("golddesk-" + self.app_kind,
                              os.path.join(self.data_dir, "app.log"))
        self.cfg = ConfigStore(self.data_dir)
        self.db = DB(os.path.join(self.data_dir, "golddesk.db"), self.cfg.instance_id)
        self.pipeline = Pipeline(self.cfg, self.db, self.log)
        self.history = PeriodAnalyzer(self.cfg, self.db, self.log)
        self.history_lock = threading.Lock()
        self._last_history_ts = 0.0
        from engine.daily import DailyService
        # 「每日资讯」的素材就是「资讯与 AI 研判」页的内容：生成前先跑一次该页的新闻采集
        self.daily = DailyService(
            self.cfg, self.db, self.pipeline.ai, self.pipeline.mailer, self.log,
            collect_brief=lambda trigger: self.pipeline.run(kind="news", trigger=trigger, send_mail=False))
        self.scheduler = Scheduler(self.cfg, self.db, self.pipeline, self.log, daily=self.daily)
        self.scheduler.start()
        self.log.info("实例启动 | kind=%s | id=%s | port=%s", self.app_kind, self.cfg.instance_id, port)

    def state(self):
        s = self.pipeline.state()
        s["next_push"] = self.scheduler.next_push or self.scheduler._next_push(self.cfg.get())
        s["scheduler_running"] = bool(self.scheduler.thread and self.scheduler.thread.is_alive())
        s["disclaimer"] = DISCLAIMER
        s["port"] = self.port
        # 国内金价卡要展示买/卖价与日内高低：这些只在源原始报文里，取出后挂到 extra
        for p in s.get("prices") or []:
            if p.get("symbol") == "Au99.99" and p.get("raw_json"):
                try:
                    p["extra"] = json.loads(p["raw_json"]).get("extra") or {}
                except Exception:
                    p["extra"] = {}
        return s


def _json(handler, data, code=200):
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(inst: Instance):
    class Handler(BaseHTTPRequestHandler):
        server_version = "GoldDesk/1.0"

        def log_message(self, fmt, *args):
            # 不把带 query 的 URL 打进日志，避免任何敏感串落盘
            pass

        def _static(self, rel: str):
            path = os.path.normpath(os.path.join(inst.ui_dir, rel))
            if not path.startswith(inst.ui_dir) or not os.path.isfile(path):
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype += "; charset=utf-8"
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if not n:
                    return {}
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except Exception:
                return {}

        def do_GET(self):
            u = urllib.parse.urlsplit(self.path)
            p, q = u.path, urllib.parse.parse_qs(u.query)
            try:
                if p in ("/", "/index.html"):
                    return self._static("index.html")
                if p.startswith("/ui/"):
                    return self._static(p[4:])
                if p == "/api/state":
                    return _json(self, inst.state())
                if p == "/api/gold_history":
                    from engine.goldhist import gold_history
                    return _json(self, gold_history((q.get("symbol") or ["domestic"])[0],
                                                    (q.get("period") or ["6m"])[0]))
                if p == "/api/config":
                    d = inst.cfg.public()
                    from engine.autostart import status as _as_status
                    d["autostart_status"] = _as_status()
                    return _json(self, d)
                if p == "/api/daily":
                    return _json(self, self._daily_get((q.get("kind") or ["news"])[0], q))
                if p == "/api/daily/article":
                    # 列表点标题：读本机正文；没存过就按需现抓一次
                    return _json(self, inst.daily.article_detail(
                        (q.get("key") or [""])[0], (q.get("url") or [""])[0],
                        (q.get("title") or [""])[0]))
                if p == "/api/selfcheck":
                    return _json(self, selfcheck(inst.cfg, inst.db, inst.pipeline))
                if p == "/api/report":
                    rid = int((q.get("id") or [0])[0])
                    r = inst.db.report(rid)
                    return _json(self, r or {"error": "报告不存在"})
                if p == "/api/history/runs":
                    return _json(self, {"ok": True, "runs": inst.db.recent_history_runs(12)})
                if p == "/api/health":
                    return _json(self, {"ok": True, "app_kind": inst.app_kind,
                                        "instance_id": inst.cfg.instance_id, "time": iso_bj()})
                self.send_error(404)
            except Exception as e:
                _json(self, {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}, 500)

        def do_POST(self):
            u = urllib.parse.urlsplit(self.path)
            p = u.path
            body = self._body()
            try:
                # 仅允许本机调用：给无控制台的 exe 提供干净退出入口
                if p in ("/api/shutdown", "/api/window"):
                    if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                        return _json(self, {"ok": False, "message": "仅允许本机操作"}, 403)
                    origin = self.headers.get("Origin") or ""
                    if origin and not (origin.startswith("http://127.0.0.1:") or
                                       origin.startswith("http://localhost:")):
                        return _json(self, {"ok": False, "message": "来源不被允许"}, 403)
                if p == "/api/window":
                    if os.name != "nt":
                        return _json(self, {"ok": False, "message": "仅 Windows 桌面端支持窗口控制"})
                    try:
                        from launcher import window_action
                        act = str(body.get("action") or "info")
                        r = window_action(act, float(body.get("dx") or 0),
                                          float(body.get("dy") or 0),
                                          float(body.get("cap") or 0),
                                          float(body.get("dpr") or 0))
                        if act in ("close", "quit") and r.get("ok"):
                            threading.Timer(0.8, self.server.shutdown).start()
                            inst.log.info("收到退出指令，窗口与后台服务同步停止")
                    except Exception as e:
                        r = {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}
                    return _json(self, r)
                if p == "/api/shutdown":
                    srv = self.server
                    threading.Timer(0.4, srv.shutdown).start()
                    inst.log.info("收到本机退出请求，调度器与 HTTP 服务即将停止")
                    return _json(self, {"ok": True, "message": "本端服务正在退出"})
                if p == "/api/config":
                    r = inst.cfg.update(body.get("config") or {})
                    inst.db.config_log(r["config_version"], body.get("summary") or "配置保存")
                    inst.db.audit("config_save", "v%s 本端配置保存" % r["config_version"])
                    r["message"] = "本端配置已保存，调度器将即时重载"
                    return _json(self, r)
                if p == "/api/config/rollback":
                    r = inst.cfg.rollback()
                    inst.db.audit("config_rollback", "回滚到上一版本")
                    return _json(self, r)
                if p == "/api/test/smtp":
                    return _json(self, inst.pipeline.mailer.test_connection(body.get("override")))
                if p == "/api/test/deepseek":
                    return _json(self, inst.pipeline.ai.test_connection())
                if p == "/api/test/mail":
                    return _json(self, inst.pipeline.mailer.send_test_mail(body.get("override")))
                if p == "/api/selfcheck":
                    return _json(self, selfcheck(inst.cfg, inst.db, inst.pipeline))
                if p == "/api/fetch":
                    action = body.get("action") or "fetch_and_analyze"
                    mapping = {
                        "refresh_prices": dict(kind="prices", send_mail=False),
                        "fetch_news": dict(kind="news", send_mail=False),
                        "re_analyze": dict(kind="analyze", send_mail=False),
                        "fetch_and_analyze": dict(kind="full", send_mail=bool(body.get("send_mail"))),
                        "flash": dict(kind="flash", send_mail=bool(body.get("send_mail"))),
                        "daily": dict(kind="daily_summary", send_mail=bool(body.get("send_mail"))),
                    }
                    if action not in mapping:
                        return _json(self, {"ok": False, "message": "未知动作"}, 400)
                    kw = dict(mapping[action])
                    kw.update(trigger="manual", force=bool(body.get("force")))
                    return _json(self, inst.pipeline.run(**kw))
                if p == "/api/push-now":
                    kind = body.get("kind") if body.get("kind") in ("flash", "daily_summary") else "flash"
                    return _json(self, inst.pipeline.run(kind=kind, trigger="manual",
                                                         send_mail=bool(body.get("send_mail")), force=True))
                if p == "/api/schedule":
                    return _json(self, self._schedule_op(body))
                if p == "/api/autostart":
                    from engine.autostart import set_enabled as _as_set
                    r = _as_set(bool(body.get("enabled")))
                    if r.get("ok"):
                        inst.db.audit("autostart", "开机自启动 → %s" % ("开启" if r.get("enabled") else "关闭"))
                    return _json(self, r)
                if p == "/api/daily/run":
                    kind = body.get("kind") if body.get("kind") in ("news", "brief", "mil") else "news"
                    sm = body.get("send_mail")
                    return _json(self, inst.daily.run_day(kind, trigger="manual", force=bool(body.get("force")),
                                                          send_mail=None if sm is None else bool(sm)))
                if p == "/api/daily/run_all":
                    sm = body.get("send_mail")
                    return _json(self, inst.daily.run_all_day(
                        trigger="manual", force=bool(body.get("force")),
                        send_mail=None if sm is None else bool(sm)))
                if p == "/api/daily/digest_all":
                    sm = body.get("send_mail")
                    return _json(self, inst.daily.run_all_digest(
                        trigger="manual", force=bool(body.get("force")),
                        send_mail=None if sm is None else bool(sm),
                        period=body.get("period")))
                if p == "/api/daily/digest":
                    kind = body.get("kind") if body.get("kind") in ("news", "brief", "mil") else "news"
                    sm = body.get("send_mail")
                    return _json(self, inst.daily.run_digest(kind, trigger="manual", force=bool(body.get("force")),
                                                             send_mail=None if sm is None else bool(sm),
                                                             period=body.get("period")))
                if p == "/api/daily/open":
                    kind = body.get("kind") if body.get("kind") in ("news", "brief", "mil") else "news"
                    folder = inst.daily.doc_dir(kind)
                    if os.name != "nt":
                        return _json(self, {"ok": False, "message": "仅 Windows 支持打开文件夹"})
                    os.startfile(folder)  # noqa: S606 - 打开的是本端自己的文档目录
                    return _json(self, {"ok": True, "path": folder})
                if p == "/api/daily/article/open":
                    kind = body.get("kind") if body.get("kind") in ("news", "brief", "mil") else "news"
                    day = str(body.get("day") or iso_bj()[:10])
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                        return _json(self, {"ok": False, "message": "日期格式不对"})
                    path = inst.daily.article_path(kind, day)
                    if not os.path.isfile(path):
                        return _json(self, {"ok": False, "message": "当天还没有正文文件，先生成一次即可"})
                    if os.name != "nt":
                        return _json(self, {"ok": False, "message": "仅 Windows 支持直接打开文件"})
                    os.startfile(path)  # noqa: S606 - 打开的是本端自己抓下来的正文文件
                    return _json(self, {"ok": True, "path": path})
                if p == "/api/master":
                    cfg = inst.cfg.get()
                    cfg["push_master_switch"] = bool(body.get("enabled"))
                    r = inst.cfg.update({"push_master_switch": cfg["push_master_switch"]})
                    inst.db.audit("master_switch", "本端定时推送总开关 → %s" % cfg["push_master_switch"])
                    return _json(self, r)
                if p == "/api/holdings":
                    try:
                        g = float(body.get("grams") or 0)
                        c = float(body.get("cost_per_gram") or 0)
                    except ValueError:
                        return _json(self, {"ok": False, "message": "克数与成本必须为数字"}, 400)
                    if g < 0 or c < 0:
                        return _json(self, {"ok": False, "message": "不能为负数"}, 400)
                    inst.db.save_holdings(g, c, str(body.get("note") or "")[:120])
                    inst.db.audit("holdings", "手工录入持仓 %s 克 / 成本 %s 元每克" % (g, c))
                    return _json(self, {"ok": True, "message": "持仓已保存到本端（手工录入，未连接银行）"})
                if p == "/api/trade":
                    # 仅本端流程演示与审计留痕；未接银行官方接口，绝不发起真实交易
                    if inst.cfg.get().get("trade", {}).get("kill_switch", True):
                        return _json(self, {"ok": False,
                                            "message": "交易熔断已开启；且当前未接入银行官方开放接口，不会发起任何真实交易"})
                    inst.db.audit("trade_simulated", "本端二次确认流程演示（无真实下单）")
                    return _json(self, {"ok": True, "simulated": True,
                                        "message": "已完成本端二次确认与审计留痕；未连接银行接口，无真实交易"})
                if p == "/api/backtest":
                    return _json(self, self._backtest(body))
                if p == "/api/history/analyze":
                    return _json(self, self._history_analyze(body))
                self.send_error(404)
            except ConfigError as e:
                _json(self, {"ok": False, "message": str(e)}, 400)
            except ValueError as e:
                _json(self, {"ok": False, "message": str(e)}, 400)
            except Exception as e:
                inst.log.error("API %s 失败: %s\n%s", p, e, traceback.format_exc()[:600])
                _json(self, {"ok": False, "message": "%s: %s" % (type(e).__name__, e)}, 500)

        def _daily_get(self, kind: str, q: dict) -> dict:
            """/api/daily?kind=news|brief|mil[&path=...]：状态；带 path 时读取一份文档正文。"""
            if kind not in ("news", "brief", "mil"):
                kind = "news"
            if q.get("path"):
                return inst.daily.read_doc(kind, (q.get("path") or [""])[0])
            return inst.daily.state(kind)

        def _schedule_op(self, body: dict) -> dict:
            cfg = inst.cfg.get()
            sch = cfg.setdefault("push_schedule", {})
            slots = sch.setdefault("slots", [])
            op = body.get("op")
            if op == "add":
                import uuid
                t = body.get("time") or "07:15"
                if not re.fullmatch(r"\d{2}:\d{2}", t):
                    return {"ok": False, "message": "时间格式必须为 HH:mm"}
                slots.append({"id": "s" + uuid.uuid4().hex[:8], "time": t,
                              "type": body.get("type") or "flash", "enabled": True,
                              "channels": body.get("channels") or ["mail"],
                              "note": body.get("note") or ""})
            elif op == "del":
                slots[:] = [s for s in slots if s.get("id") != body.get("id")]
            elif op == "update":
                for s in slots:
                    if s.get("id") == body.get("id"):
                        for k in ("time", "type", "enabled", "channels", "note"):
                            if k in body:
                                s[k] = body[k]
            elif op == "toggle":
                for s in slots:
                    if s.get("id") == body.get("id"):
                        s["enabled"] = not s.get("enabled", True)
            elif op == "clear":
                slots[:] = []
            elif op == "settings":
                for k in ("quiet_hours", "max_event_mails_per_day", "skip_on_market_closed", "daily_summary_slot"):
                    if k in body:
                        sch[k] = body[k]
            else:
                return {"ok": False, "message": "未知操作时点"}
            r = inst.cfg.update({"push_schedule": sch})
            inst.db.audit("schedule", "时点表 %s，本端 v%s" % (op, r["config_version"]))
            r["message"] = "本端时点表已更新并热重载"
            return r

        def _history_analyze(self, body: dict) -> dict:
            """FR-16：按时段采集新闻 → AI 情景研判 → 生成报告 → 可选发信。"""
            cfg = inst.cfg.get()
            hcfg = cfg.get("history_analysis", {})
            rng = resolve_range(body.get("preset") or hcfg.get("default_preset", "6m"),
                                body.get("start"), body.get("end"))
            gap = float(hcfg.get("min_interval_seconds", 20))
            with inst.history_lock:
                if not body.get("force") and time.time() - inst._last_history_ts < gap:
                    return {"ok": False, "throttled": True,
                            "message": "时段研判执行过于频繁，请在 %.0f 秒后重试" %
                                       (gap - (time.time() - inst._last_history_ts))}
                inst._last_history_ts = time.time()
                max_items = max(10, min(200, int(body.get("max_items") or hcfg.get("max_items", 60))))
                horizon = str(body.get("horizon") or hcfg.get("forward_horizon", "1-3m"))
                col = collect_history_news(cfg, inst.db, inst.log, rng,
                                           inst.cfg.secret, max_items)
                ana = inst.history.analyze(rng, col, horizon, force=bool(body.get("force")))
                report = render_history(rng, col, ana.get("result") or {}, ana, prefix=mail_prefix(cfg))
                report["signal_id"] = None
                report["id"] = inst.db.insert_report(report)
                delivery = None
                if body.get("send_mail"):
                    delivery = inst.pipeline.mailer.send_report(report)
                run_id = inst.db.insert_history_run({
                    "start_date": rng["start"], "end_date": rng["end"], "preset": rng["preset"],
                    "news_count": col["total"], "topic_stats": col["topic_stats"],
                    "source_status": col["sources"], "result": ana.get("result") or {},
                    "report_id": report["id"],
                    "delivered": bool(delivery and delivery.get("ok")),
                })
                inst.db.audit("history_analysis",
                              "时段研判 %s~%s 样本 %d 条，报告 #%s，邮件 %s" %
                              (rng["start"], rng["end"], col["total"], report["id"],
                               "已发送" if delivery and delivery.get("ok") else "未发送"))
                return {"ok": True, "range": rng, "collection": col,
                        "analysis": {"available": ana.get("available"),
                                     "reason": ana.get("reason"),
                                     "result": ana.get("result"),
                                     "warnings": ana.get("warnings", []),
                                     "usage": ana.get("usage")},
                        "report": {"id": report["id"], "subject": report["subject"]},
                        "delivery": delivery, "run_id": run_id,
                        "finished_at": iso_bj(), "disclaimer": DISCLAIMER}

        def _backtest(self, body: dict) -> dict:
            """极简情景测算：只做历史快照区间的条件化描述，不给买卖点位/收益承诺。"""
            hist = inst.db.price_history("XAU/USD", 200)
            if len(hist) < 3:
                return {"ok": False, "message": "历史快照不足（需至少 3 次采集），请先多运行几轮"}
            vals = [h["price"] for h in hist if h["price"]]
            lo, hi, first, last = min(vals), max(vals), vals[0], vals[-1]
            return {"ok": True, "simulated": True, "samples": len(vals),
                    "range_low": lo, "range_high": hi,
                    "period_change_pct": round((last - first) / first * 100, 3) if first else None,
                    "note": "基于本端已落库的 XAU/USD 快照做的区间描述，非策略回测、非收益测算；"
                            "样本量小、未计点差与手续费、未做样本外检验，存在严重过拟合风险，不预示未来收益。",
                    "disclaimer": DISCLAIMER}

    return Handler


HTTPD = None   # 当前 HTTP 服务实例，供窗口关闭后干净退出使用


def stop_http():
    """让 serve() 的 serve_forever 返回（在其它线程调用安全）。"""
    if HTTPD is not None:
        threading.Timer(0.3, HTTPD.shutdown).start()


def serve(inst: Instance):
    global HTTPD
    httpd = ThreadingHTTPServer((inst.host, inst.port), make_handler(inst))
    httpd.daemon_threads = True
    HTTPD = httpd
    print("GoldDesk 桌面端已启动: http://%s:%d  (instance=%s)"
          % (inst.host, inst.port, inst.cfg.instance_id))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        HTTPD = None
        inst.scheduler.stop()
        httpd.server_close()
