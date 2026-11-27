# -*- coding: utf-8 -*-
"""本端调度器：自定义推送时点、静默时段、限流、热重载、事件级告警（FR-12 / FR-06）。

每个实例一条独立线程；不读取、不写入、不请求另一个实例的任何东西。
"""
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta

from .prices import market_status
from .daily import SECTION, DIGEST_PERIOD_CN, digest_periods
from .util import bj_str, hhmm_valid, in_quiet_hours, iso_bj, log_line, now_bj


class Scheduler:
    def __init__(self, cfg_store, db, pipeline, logger, daily=None):
        self.cfg = cfg_store
        self.db = db
        self.pipeline = pipeline
        self.log = logger
        self.daily = daily            # engine.daily.DailyService（每日新闻 / 每日资讯）
        self.daily_thread = None
        self.stop_event = threading.Event()
        self.thread = None
        self.last_collect = 0.0
        self.last_cfg_mtime = 0.0
        self.last_alert_at = ""
        self.next_push = ""
        self._daily_fails = {}        # 时点 key → 已失败次数（只自动重试一次）
        self._alert_last = {}         # 提醒规则 id → 上次命中时间

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="golddesk-scheduler", daemon=True)
        self.thread.start()
        self.log.info("调度器已启动（%s · %s）", self.cfg.get().get("app_kind"), self.cfg.instance_id)
        self.db.event("info", "调度器启动")

    def stop(self):
        self.stop_event.set()
        self.db.event("info", "调度器停止")

    def _reload_config(self):
        try:
            m = os.path.getmtime(self.cfg.path)
            if m != self.last_cfg_mtime:
                self.last_cfg_mtime = m
                self.cfg.cfg = self.cfg._read()
                self.db.event("info", "检测到本端配置变更，调度器热重载 v%s" % self.cfg.cfg.get("config_version"))
                self.log.info("config hot-reloaded v%s", self.cfg.cfg.get("config_version"))
        except OSError:
            pass

    def _next_push(self, cfg) -> str:
        if not cfg.get("push_master_switch", True):
            return "已关闭（本端定时推送总开关为关）"
        now = now_bj()
        slots = [s for s in cfg.get("push_schedule", {}).get("slots", []) if s.get("enabled")]
        times = sorted(s["time"] for s in slots)
        cur = now.strftime("%H:%M")
        for t in times:
            if t > cur:
                return "%s %s" % (now.strftime("%Y-%m-%d"), t)
        if times:
            d = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            return "%s %s（次日）" % (d, times[0])
        return "无（时点列表为空）"

    def _in_quiet(self, cfg, hhmm: str) -> bool:
        q = cfg.get("push_schedule", {}).get("quiet_hours", {})
        return in_quiet_hours(hhmm, q.get("start", "23:00"), q.get("end", "07:00"))

    def _over_daily_limit(self, cfg) -> bool:
        limit = int(cfg.get("push_schedule", {}).get("max_event_mails_per_day", 4))
        return self.db.mails_today() >= limit

    def _poll_minutes(self, cfg) -> int:
        st = market_status()
        p = cfg.get("poll_minutes", {})
        now = now_bj()
        hm = now.hour * 60 + now.minute
        if st == "closed":
            return int(p.get("closed", 60))
        if 11 * 60 + 30 <= hm < 13 * 60 + 30:
            return int(p.get("midday", 20))
        if 20 * 60 <= hm or hm <= 2 * 60 + 30:
            return int(p.get("overnight", 10))
        return int(p.get("active", 5))

    def _loop(self):
        import time
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as e:
                self.log.exception("scheduler tick error")
                self.db.event("error", "调度器异常：" + str(e)[:200])
            self.stop_event.wait(15)

    def _tick(self):
        import time
        self._reload_config()
        cfg = self.cfg.get()
        self.next_push = self._next_push(cfg)
        # 1) 分时段价格轮询（只采集，不发信）
        interval = max(1, self._poll_minutes(cfg)) * 60
        if time.time() - self.last_collect >= interval:
            self.last_collect = time.time()
            try:
                r = self.pipeline.run(kind="prices", trigger="scheduled", send_mail=False)
                self._check_alert(cfg, r)
            except Exception as e:
                self.log.warning("定时采集失败：%s", str(e)[:160])
        # 1.5) 每日新闻 / 每日资讯文档 + 周期汇总（有独立开关，不看下面的推送总开关）
        now = now_bj()
        today = now.strftime("%Y-%m-%d")
        self._check_daily_jobs(cfg, now, today)
        # 2) 用户自定义时点
        if not cfg.get("push_master_switch", True):
            return
        sch = cfg.get("push_schedule", {})
        # 补发窗口：tick 每 15 秒一次，若程序在该分钟内才启动、或上一轮任务超时，
        # 只按「HH:MM 完全相等」匹配会整天漏发。这里允许时点过后 N 分钟内补发，
        # 由 slot_fire 表按「日期|时点|时间」去重，不会重复推送。
        catchup = max(1, min(60, int(sch.get("catchup_minutes", 10) or 10)))
        for slot in sch.get("slots", []):
            if not slot.get("enabled"):
                continue
            hhmm = str(slot.get("time") or "")
            if not hhmm_valid(hhmm):
                continue
            key = "%s|%s|%s" % (today, slot.get("id"), hhmm)
            if self.db.slot_fired(key):
                continue
            slot_at = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
            lag = (now - slot_at).total_seconds()
            if lag < 0 or lag > catchup * 60:
                continue
            if "mail" not in (slot.get("channels") or []):
                self.db.mark_slot_fired(key, "no_mail_channel")
                continue
            if self._in_quiet(cfg, hhmm) and not sch.get("quiet_hours", {}).get("allow_major_events"):
                self.db.mark_slot_fired(key, "quiet")
                self.db.event("info", "时点 %s 落在静默时段，已跳过" % hhmm)
                continue
            if sch.get("skip_on_market_closed") and market_status() == "closed" and slot.get("type") == "flash":
                self.db.mark_slot_fired(key, "market_closed")
                self.db.event("info", "休市跳过价格快报 %s" % hhmm)
                continue
            if self._over_daily_limit(cfg) and slot.get("type") != "daily_summary":
                self.db.mark_slot_fired(key, "daily_limit")
                continue
            self.db.mark_slot_fired(key, "firing")
            kind = "daily_summary" if slot.get("type") in ("daily_summary", "both") else "flash"
            if lag >= 60:
                self.db.event("info", "时点 %s 补发（延迟 %d 秒，启动或上一轮任务占用了触发时刻）"
                              % (hhmm, int(lag)))
            log_line(self.log, "scheduled_push:" + slot.get("id", "?"), "start", "scheduled",
                     time=hhmm, kind=kind, lag_s=int(lag))
            try:
                r = self.pipeline.run(kind=kind, trigger="scheduled", send_mail=True)
                self.db.event("info" if r.get("ok") else "error",
                              "时点 %s 推送 %s：%s" % (hhmm, kind, "完成" if r.get("ok") else r.get("message", "失败")))
            except Exception as e:
                self.db.event("error", "时点 %s 推送异常：%s" % (hhmm, str(e)[:160]))

    def _check_daily_jobs(self, cfg, now, today):
        """每日文档与周期汇总。

        判定方式是「当天到点之后任意时刻都补做」而不是「正好等于这一分钟」：
        程序到晚上才开机、或上一轮任务拖过了触发时刻，当天的文档与汇总都不会漏。
        slot_fire 按「日期」去重，同一天只跑一次。两个任务串在一条后台线程里，
        不占用调度线程（AI 归纳要几十秒，否则会拖慢价格轮询）。
        """
        if not self.daily:
            return
        pending = []
        plan = cfg.get("daily_all") or {}
        if plan.get("enabled"):
            if self._due_today("daily|all|%s" % today, plan.get("time"), now):
                pending.append((("day_all",), "daily|all|%s" % today))
            # 统一汇总：三个模块共用一套周期与时间；任意一个模块到期就整封汇总一起做
            due = {p for p, _ in self.daily.due_periods_all(plan, now.date())}
            for per in digest_periods(plan):
                key = "digest|all|%s|%s" % (per, today)
                if per in due and self._due_today(key, plan.get("digest_time"), now):
                    pending.append((("digest_all", per), key))
        else:
            # 兼容：老配置里 daily_all 被关掉时，退回三个模块各自调度（不再发合并邮件）
            for kind in ("news", "brief", "mil"):
                sec = cfg.get(SECTION.get(kind, kind)) or {}
                if not sec.get("enabled"):
                    continue
                key = "daily|%s|%s" % (kind, today)
                if self._due_today(key, sec.get("time"), now):
                    pending.append((("day", kind), key))
                for per in digest_periods(sec):
                    key = "digest|%s|%s|%s" % (kind, per, today)
                    if self.daily.due_range(kind, sec, now.date(), per) and \
                            self._due_today(key, sec.get("digest_time"), now):
                        pending.append((("digest", kind, per), key))
        if not pending:
            return
        if self.daily_thread and self.daily_thread.is_alive():
            self.log.info("每日文档任务仍在运行，本轮跳过：%s", [j for j, _ in pending])
            return

        def work():
            for job, key in pending:
                self.db.mark_slot_fired(key, "running")
                try:
                    if job[0] == "day_all":
                        r = self.daily.run_all_day(trigger="scheduled")
                        if not r.get("ok"):
                            raise RuntimeError("有日报生成失败")
                        self._notify("每日内容已生成", "%d/3 份文档%s" % (
                            sum(1 for x in (r.get("results") or {}).values() if x.get("ok")),
                            "，合并邮件已发送" if (r.get("delivery") or {}).get("ok") else ""))
                    elif job[0] == "digest_all":
                        r = self.daily.run_all_digest(trigger="scheduled", period=job[1])
                        if r.get("not_due"):
                            self.db.event("info", "每日综合的%s汇总未到期，本次不生成" % (
                                DIGEST_PERIOD_CN.get(job[1], job[1])))
                        elif r.get("ok"):
                            self._notify("每日综合汇总已生成", r.get("label") or job[1])
                    elif job[0] == "day":
                        r = self.daily.run_day(job[1], trigger="scheduled")
                        if not r.get("ok"):
                            raise RuntimeError(str(r.get("message") or "生成失败"))
                    else:
                        _, kind, per = job
                        r = self.daily.run_digest(kind, trigger="scheduled", period=per)
                        if r.get("not_due"):
                            self.db.event("info", "%s 的%s汇总未到期，本次不生成" % (
                                {"news": "每日新闻", "brief": "每日资讯", "mil": "每日军事新闻"}.get(kind, kind),
                                DIGEST_PERIOD_CN.get(per, per)))
                    self.db.mark_slot_fired(key, "done")
                    self._daily_fails.pop(key, None)
                except Exception as e:
                    # 第一次失败释放标记，下一轮自动重试；第二次仍失败则本日不再反复跑
                    if self._daily_fails.get(key, 0) <= 0:
                        self._daily_fails[key] = 1
                        self.db.clear_slot_fired(key)
                        self.db.event("warn", "每日文档任务失败（%s），稍后自动重试一次：%s" % (
                            "/".join(str(x) for x in job), str(e)[:160]))
                    else:
                        self.db.mark_slot_fired(key, "failed")
                        self.db.event("error", "每日文档任务重试仍失败（%s）：%s" % (
                            "/".join(str(x) for x in job), str(e)[:160]))

        self.daily_thread = threading.Thread(target=work, name="golddesk-daily", daemon=True)
        self.daily_thread.start()

    def _notify(self, title: str, text: str):
        try:
            from launcher import notify
            notify(title, text)
        except Exception:
            pass

    def _due_today(self, key: str, hhmm, now) -> bool:
        hhmm = str(hhmm or "")
        if not hhmm_valid(hhmm) or self.db.slot_fired(key):
            return False
        t = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
        return now >= t

    def _check_alert(self, cfg, run_result: dict):
        """日内异动 + 用户自定义到价/涨跌幅/MA20 规则：同一轮只发一封提醒。"""
        if not run_result or not run_result.get("ok"):
            return
        prices = run_result.get("snapshot", {}).get("prices", []) or []
        now = now_bj()
        hit_msgs = []
        threshold = float(cfg.get("alert", {}).get("intraday_pct", 2.0))
        intraday = None
        for p in prices:
            if p.get("symbol") in ("XAU/USD", "Au99.99") and p.get("change_pct") is not None:
                if abs(float(p["change_pct"])) >= threshold and p.get("quality_flag") == "ok":
                    intraday = p
                    break
        if intraday:
            hit_msgs.append("异动：%s 较本端上一快照 %s%%（盘中瞬时波动，非趋势确认）" % (
                intraday["symbol"], intraday["change_pct"]))
        al = cfg.get("alerts") or {}
        if al.get("enabled", True):
            from .alerts import evaluate
            cooldown = float(al.get("cooldown_hours", 12) or 0) * 3600
            for h in evaluate(cfg, self.db, prices):
                rid = str(h["rule"].get("id") or h["rule"].get("type"))
                last = self._alert_last.get(rid)
                if last and cooldown > 0 and (now - last).total_seconds() < cooldown:
                    continue
                self._alert_last[rid] = now
                hit_msgs.append("提醒：%s" % h["text"])
        if not hit_msgs:
            return
        # 异动沿用原来的 2 小时总冷却；纯自定义提醒按每条规则自己的冷却
        if intraday and self.last_alert_at:
            try:
                prev = datetime.fromisoformat(self.last_alert_at)
                if (now - prev).total_seconds() < 7200:
                    return
            except ValueError:
                pass
        if self._in_quiet(cfg, now.strftime("%H:%M")):
            return
        if self._over_daily_limit(cfg):
            return
        self.last_alert_at = iso_bj()
        for m in hit_msgs:
            self.db.event("warn", m)
        self._notify("金价提醒", "；".join(hit_msgs))
        if al.get("send_mail", True):
            try:
                self.pipeline.run(kind="flash", trigger="scheduled", send_mail=True)
            except Exception as e:
                self.log.warning("提醒快报失败：%s", str(e)[:160])
