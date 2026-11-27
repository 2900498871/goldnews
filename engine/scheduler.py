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
        self._catchup_thread = None
        self.current_job = ""           # 当前正在写的文档任务（任务中心展示）
        self._daily_busy = threading.Event()   # 日报/汇总/补生成互斥，避免并发写同一批文档
        self._pause_event = threading.Event()  # 合并导入等维护任务期间暂停采集
        self.pause_reason = ""

    def pause(self, reason: str = ""):
        """暂停调度与采集；用于备份合并等需要独占数据库的维护任务。"""
        self.pause_reason = str(reason or "")
        self._pause_event.set()

    def resume(self):
        self._pause_event.clear()
        self.pause_reason = ""

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def is_busy(self) -> bool:
        """是否已有日报/汇总/补生成在写文档。"""
        return bool(self._daily_busy.is_set() or
                    (self.daily_thread and self.daily_thread.is_alive()) or
                    (self._catchup_thread and self._catchup_thread.is_alive()))


    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="golddesk-scheduler", daemon=True)
        self.thread.start()
        self.log.info("调度器已启动（%s · %s）", self.cfg.get().get("app_kind"), self.cfg.instance_id)
        self.db.event("info", "调度器启动")
        self._catchup_thread = threading.Thread(target=self._catchup_past, name="golddesk-catchup",
                                                daemon=True)
        self._catchup_thread.start()

    def stop(self):
        self.stop_event.set()
        self.db.event("info", "调度器停止")

    def snapshot(self) -> dict:
        """任务中心：线程状态、即将执行、最近执行与失败详情。"""
        cfg = self.cfg.get()
        now = now_bj()
        plan = cfg.get("daily_all") or {}
        jobs = []
        if plan.get("enabled"):
            per = "/".join(DIGEST_PERIOD_CN.get(p, p) for p in digest_periods(plan)) or "周/月"
            jobs.append({"label": "每日内容", "kind": "daily_all",
                         "time": plan.get("time") or "20:30",
                         "desc": "生成每日新闻、每日资讯、每日军事新闻三份文档，并合成发送合并邮件"})
            jobs.append({"label": "每日综合汇总", "kind": "digest_all",
                         "time": plan.get("digest_time") or "21:30",
                         "desc": "按%s周期生成综合汇总文档并发送邮件" % per})
        else:
            for kind in ("news", "brief", "mil"):
                sec = cfg.get({"news": "daily", "brief": "brief", "mil": "mil"}.get(kind, kind)) or {}
                if sec.get("enabled"):
                    jobs.append({"label": {"news": "每日新闻", "brief": "每日资讯",
                                            "mil": "每日军事新闻"}[kind],
                                 "kind": kind, "time": sec.get("time") or "—",
                                 "desc": "生成该板块当日文档，并按周期生成汇总邮件"})
        # 即时数据只取一次，后面失败、最近执行、上次状态共用
        try:
            recent = self.db.recent_slots(30)
        except Exception:
            recent = []
        try:
            task_runs = self.db.recent_tasks(30)
        except Exception:
            task_runs = []
        try:
            failed_tasks_raw = self.db.failed_tasks(100)
        except Exception:
            failed_tasks_raw = [r for r in task_runs
                                if str(r.get("status") or "") in ("error", "failed")]
        try:
            failed_slots_raw = self.db.failed_slots(100)
        except Exception:
            failed_slots_raw = [s for s in recent if str(s.get("status") or "") == "failed"]

        def last_slot(prefix):
            for s in recent:
                if str(s.get("fire_key") or "").startswith(prefix):
                    return s
            return None

        next_runs = []
        for job in jobs:
            hhmm = job["time"]
            if not hhmm_valid(hhmm):
                continue
            t = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            prefix = {"daily_all": "daily|all|", "digest_all": "digest|all|"}.get(job["kind"], "")
            last = last_slot(prefix) if prefix else None
            next_runs.append({
                "label": job["label"], "kind": job["kind"], "time": hhmm,
                "desc": job["desc"], "source": "daily",
                "at": t.strftime("%Y-%m-%d %H:%M"),
                "date": t.strftime("%Y-%m-%d"),
                "tomorrow": t.date() > now.date(),
                "seconds": max(0, int((t - now).total_seconds())),
                "last_status": (last or {}).get("status", ""),
                "last_at": (last or {}).get("fired_at", ""),
            })
        # 自定义推送时点也属于“任务”，只展示最近一次即将触发的那条
        sch = cfg.get("push_schedule") or {}
        for slot in sch.get("slots") or []:
            if not slot.get("enabled") or not cfg.get("push_master_switch", True):
                continue
            hhmm = str(slot.get("time") or "")
            if not hhmm_valid(hhmm) or "mail" not in (slot.get("channels") or []):
                continue
            sid = str(slot.get("id") or hhmm)
            last = next((s for s in recent if "|%s|" % sid in str(s.get("fire_key") or "")), None)
            t = now.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
            if t <= now:
                t += timedelta(days=1)
            kind = "daily_summary" if slot.get("type") in ("daily_summary", "both") else "flash"
            next_runs.append({
                "label": "自定义推送 %s" % hhmm,
                "kind": "push:%s" % sid,
                "time": hhmm, "source": "slot", "at": t.strftime("%Y-%m-%d %H:%M"),
                "date": t.strftime("%Y-%m-%d"), "tomorrow": t.date() > now.date(),
                "seconds": max(0, int((t - now).total_seconds())),
                "desc": "按配置发送%s并检查到价提醒" % ("日报汇总" if kind == "daily_summary" else "价格快报"),
                "last_status": (last or {}).get("status", ""),
                "last_at": (last or {}).get("fired_at", ""),
            })
        next_runs.sort(key=lambda x: x["at"])
        upcoming = next_runs[:8]

        # 失败记录：任务运行错误 + 时点重试仍失败，保留原始原因
        def task_label(name):
            n = str(name or "")
            mapping = {"daily:news": "每日新闻文档", "daily:brief": "每日资讯文档",
                       "daily:mil": "每日军事新闻文档", "daily:all": "每日内容",
                       "digest:news": "每日新闻汇总", "digest:brief": "每日资讯汇总",
                       "digest:mil": "每日军事新闻汇总", "digest:all": "每日综合汇总",
                       "pipeline:prices": "价格采集",
                       "pipeline:news": "资讯采集",
                       "pipeline:flash": "价格快报",
                       "pipeline:daily_summary": "每日综合汇总",
                       "pipeline:full": "全量任务"}
            return mapping.get(n) or mapping.get(n.replace("_", ":")) or n or "未命名任务"

        def slot_label(key):
            k = str(key or "")
            if k.startswith("daily|all|"):
                return "每日内容（三份文档）"
            if k.startswith("digest|all|"):
                return "每日综合汇总（时点）"
            if k.startswith("catchup|"):
                return "跨天补生成"
            if k.startswith("daily|"):
                return "每日文档（时点）"
            if k.startswith("digest|"):
                return "周期汇总（时点）"
            return k or "定时任务"

        failures = []
        for r in failed_tasks_raw:
            status = str(r.get("status") or "")
            if status in ("error", "failed") or (r.get("error") and status != "ok"):
                failures.append({
                    "source": "task", "name": task_label(r.get("task_name")),
                    "at": r.get("finished_at") or r.get("started_at") or "",
                    "status": status or "error", "trigger": r.get("trigger_source") or "",
                    "error": r.get("error") or "任务返回错误，详情见运行日志",
                    "latency_ms": r.get("latency_ms") or 0, "key": "",
                })
        for s in failed_slots_raw:
            failures.append({
                "source": "slot", "name": slot_label(s.get("fire_key")),
                "at": s.get("fired_at") or "", "status": "failed",
                "trigger": "scheduled", "error": "自动重试后仍失败，详情见运行日志",
                "latency_ms": 0, "key": s.get("fire_key") or "",
            })
        failures.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
        # 最近任务补上中文名称，界面不用再解析内部 task_name
        for r in task_runs:
            r["name_cn"] = task_label(r.get("task_name"))

        return {"ok": True, "running": bool(self.thread and self.thread.is_alive()),
                "daily_running": bool(self.daily_thread and self.daily_thread.is_alive()),
                "catchup_running": bool(self._catchup_thread and self._catchup_thread.is_alive()),
                "busy": bool(self._daily_busy.is_set()),
                "paused": self.is_paused(), "pause_reason": self.pause_reason,
                "current_job": self.current_job or "",
                "next": next_runs, "upcoming": upcoming, "next_push": self._next_push(cfg),
                "recent_slots": recent, "recent_tasks": task_runs,
                "failures": failures, "fails": dict(self._daily_fails),
                "now": now.strftime("%Y-%m-%d %H:%M:%S")}

    def _catchup_past(self):
        """启动后补生成最近 N 天缺失的日报：只用本机已存条目，不发旧邮件。"""
        if not self.daily:
            return
        acquired = False
        try:
            import time
            time.sleep(3)
            cfg = self.cfg.get()
            plan = cfg.get("daily_all") or {}
            if not plan.get("enabled"):
                return
            days = int(plan.get("catchup_days", 2) or 0)
            if days <= 0:
                return
            if self._daily_busy.is_set():
                return
            self._daily_busy.set()
            acquired = True
            today = now_bj().date()
            for offset in range(1, days + 1):
                day = (today - timedelta(days=offset)).isoformat()
                key = "catchup|all|%s" % day
                if self.db.slot_fired(key):
                    continue
                if self.daily_thread and self.daily_thread.is_alive():
                    return
                r = self.daily.catchup_day(day)
                if r.get("ok"):
                    self.db.mark_slot_fired(key, "done")
                    self.db.event("info", "跨天补生成 %s：%s" % (day, r.get("message") or "完成"))
                else:
                    self.db.mark_slot_fired(key, "no_data")
                    self.log.info("跨天补生成 %s 跳过：%s", day, r.get("message") or "无本机条目")
        except Exception as e:
            self.log.warning("跨天补生成失败：%s", str(e)[:160])
        finally:
            if acquired:
                self._daily_busy.clear()

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
        if self.is_paused():
            return
        self._reload_config()
        cfg = self.cfg.get()
        self.next_push = self._next_push(cfg)
        # 1) 分时段价格轮询（只采集，不发信）
        interval = max(1, self._poll_minutes(cfg)) * 60
        if time.time() - self.last_collect >= interval:
            if self.is_paused():
                return
            self.last_collect = time.time()
            try:
                r = self.pipeline.run(kind="prices", trigger="scheduled", send_mail=False)
                self._check_alert(cfg, r)
            except Exception as e:
                self.log.warning("定时采集失败：%s", str(e)[:160])
        if self.is_paused():
            return
        # 1.5) 每日新闻 / 每日资讯文档 + 周期汇总（有独立开关，不看下面的推送总开关）
        now = now_bj()
        today = now.strftime("%Y-%m-%d")
        self._check_daily_jobs(cfg, now, today)
        if self.is_paused():
            return
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
        if self.is_paused():
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
        if self._daily_busy.is_set() or (self.daily_thread and self.daily_thread.is_alive()):
            self.log.info("每日文档任务仍在运行，本轮跳过：%s", [j for j, _ in pending])
            return
        self._daily_busy.set()

        def work():
            try:
                for job, key in pending:
                    if self.is_paused():
                        self.log.info("合并/维护期间暂停每日任务：%s", key)
                        break
                    self.db.mark_slot_fired(key, "running")
                    if job[0] == "day_all":
                        self.current_job = "每日内容（三份文档）"
                    elif job[0] == "digest_all":
                        self.current_job = "每日综合汇总（%s）" % DIGEST_PERIOD_CN.get(job[1], job[1])
                    elif job[0] == "day":
                        self.current_job = "%s文档" % {"news": "每日新闻", "brief": "每日资讯",
                                                        "mil": "每日军事新闻"}.get(job[1], job[1])
                    else:
                        self.current_job = "%s%s汇总" % (
                            {"news": "每日新闻", "brief": "每日资讯", "mil": "每日军事新闻"}.get(job[1], job[1]),
                            DIGEST_PERIOD_CN.get(job[2], job[2]))
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
            finally:
                self.current_job = ""
                self._daily_busy.clear()

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
