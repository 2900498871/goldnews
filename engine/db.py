# -*- coding: utf-8 -*-
"""本端 SQLite 存储（PRD §11），数据库文件落在本机 data 目录。"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta

from .util import iso_bj, now_bj, short_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS instance_meta (
  key TEXT PRIMARY KEY, value TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS config_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, version INTEGER, changed_at TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS price_tick (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, name TEXT, price REAL, unit TEXT,
  change_pct REAL, change_base TEXT, source TEXT, source_ts TEXT, fetched_at TEXT,
  quality_flag TEXT, trigger_source TEXT, raw_json TEXT,
  role TEXT, is_alternate INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_price_time ON price_tick(fetched_at);
CREATE INDEX IF NOT EXISTS idx_price_symbol ON price_tick(symbol, fetched_at);
CREATE TABLE IF NOT EXISTS news_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_hash TEXT UNIQUE, title TEXT, summary TEXT,
  url TEXT, source TEXT, published_at_local TEXT, published_at_bj TEXT, language TEXT,
  topic TEXT, importance INTEGER, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_time ON news_item(fetched_at);
CREATE TABLE IF NOT EXISTS ai_signal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, as_of_bj TEXT, horizon TEXT, direction TEXT,
  confidence TEXT, signal_json TEXT, prompt_version TEXT, model TEXT,
  prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER,
  snapshot_hash TEXT, instance_id TEXT, trigger_source TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_time ON ai_signal(as_of_bj);
CREATE TABLE IF NOT EXISTS daily_report (
  id INTEGER PRIMARY KEY AUTOINCREMENT, report_date TEXT, report_type TEXT,
  subject TEXT, html TEXT, text_body TEXT, signal_id INTEGER, generated_at TEXT
);
CREATE TABLE IF NOT EXISTS delivery_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE, report_id INTEGER,
  channel TEXT, recipient TEXT, status TEXT, attempt_count INTEGER, error TEXT,
  sent_at TEXT, instance_id TEXT
);
CREATE TABLE IF NOT EXISTS usage_counter (
  date TEXT, instance_id TEXT, calls INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0, PRIMARY KEY(date, instance_id)
);
CREATE TABLE IF NOT EXISTS task_run (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_name TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, source_status_json TEXT, error TEXT, trigger_source TEXT, latency_ms INTEGER
);
CREATE TABLE IF NOT EXISTS slot_fire (
  fire_key TEXT PRIMARY KEY, fired_at TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
  id INTEGER PRIMARY KEY AUTOINCREMENT, grams REAL, cost_per_gram REAL, note TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, action TEXT, detail TEXT, instance_id TEXT
);
CREATE TABLE IF NOT EXISTS event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, level TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS history_analysis (
  id INTEGER PRIMARY KEY AUTOINCREMENT, start_date TEXT, end_date TEXT, preset TEXT,
  news_count INTEGER, topic_stats_json TEXT, source_status_json TEXT, result_json TEXT,
  report_id INTEGER, delivered INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS daily_item (
  id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, kind TEXT, src TEXT, src_name TEXT, rank INTEGER,
  title TEXT, summary TEXT, url TEXT, hot_value INTEGER, published_at_bj TEXT,
  dedup_hash TEXT UNIQUE, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_daily_day ON daily_item(day, kind, src, rank);
CREATE TABLE IF NOT EXISTS daily_doc (
  day TEXT, kind TEXT, path TEXT, ai_available INTEGER, ai_reason TEXT, summary_md TEXT,
  counts_json TEXT, generated_at TEXT, PRIMARY KEY(day, kind)
);
CREATE TABLE IF NOT EXISTS article_cache (
  key TEXT PRIMARY KEY, url TEXT, title TEXT, content TEXT, chars INTEGER, status TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS digest_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, period TEXT, start_date TEXT, end_date TEXT,
  label TEXT, days INTEGER, path TEXT, ai_available INTEGER, mailed INTEGER, status TEXT,
  created_at TEXT
);
"""


class DB:
    def __init__(self, path: str, instance_id: str):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.instance_id = instance_id
        with self.conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)
            c.execute("INSERT OR REPLACE INTO instance_meta VALUES(?,?,?)",
                      ("instance_id", instance_id, iso_bj()))
            c.commit()

    @staticmethod
    def _migrate(c):
        """老库升级：补齐新增列，不清空、不重建，保证历史快照可继续使用。"""
        cols = {r[1] for r in c.execute("PRAGMA table_info(price_tick)").fetchall()}
        if "role" not in cols:
            c.execute("ALTER TABLE price_tick ADD COLUMN role TEXT")
        if "is_alternate" not in cols:
            c.execute("ALTER TABLE price_tick ADD COLUMN is_alternate INTEGER DEFAULT 0")
        # 老库里同一口径多源并列的行没有标记，按 raw_json 里的 alternate 字段回填
        c.execute("UPDATE price_tick SET is_alternate=1 WHERE is_alternate=0 "
                  "AND raw_json LIKE '%\"alternate\": true%'")
        # 老库没有 role 列时，界面会显示「口径角色 —」，从 raw_json 里补齐
        for rid, raw in c.execute("SELECT id, raw_json FROM price_tick WHERE role IS NULL").fetchall():
            try:
                role = (json.loads(raw or "{}") or {}).get("role")
            except (ValueError, TypeError):
                role = None
            if role:
                c.execute("UPDATE price_tick SET role=? WHERE id=?", (role, rid))

    @contextmanager
    def conn(self):
        c = sqlite3.connect(self.path, timeout=15)
        c.row_factory = sqlite3.Row
        try:
            yield c
        finally:
            c.close()

    def rows(self, sql: str, args=()) -> list[dict]:
        with self.conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        with self.conn() as c:
            r = c.execute(sql, args).fetchone()
            return dict(r) if r else None

    def run(self, sql: str, args=()):
        with self.conn() as c:
            cur = c.execute(sql, args)
            c.commit()
            return cur.lastrowid

    # ---- 价格 ----
    def insert_price(self, p: dict):
        self.run(
            "INSERT INTO price_tick(symbol,name,price,unit,change_pct,change_base,source,source_ts,"
            "fetched_at,quality_flag,trigger_source,raw_json,role,is_alternate) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.get("symbol"), p.get("name"), p.get("price"), p.get("unit"),
             p.get("change_pct"), p.get("change_base", "prev_snapshot"), p.get("source"),
             p.get("source_ts"), p.get("fetched_at"), p.get("quality_flag", "ok"),
             p.get("trigger_source", "scheduled"), json.dumps(p, ensure_ascii=False),
             p.get("role"), 1 if p.get("alternate") else 0))

    def latest_prices(self, limit: int = 60, include_alternate: bool = False) -> list[dict]:
        """默认只返回主口径行；同一口径的并列源报价只用于留痕与分歧说明。"""
        where = "" if include_alternate else " WHERE is_alternate=0"
        return self.rows(
            "SELECT * FROM (SELECT * FROM price_tick%s ORDER BY id DESC LIMIT ?) ORDER BY id ASC"
            % where, (limit,))

    def last_price(self, symbol: str, within_hours: int = 48, include_alternate: bool = False):
        """上一个主口径报价。

        fetched_at 存的是带 +08:00 的 ISO 串，不能用 SQLite 的 datetime('now')
        （UTC + 空格分隔）直接比较，这里在 Python 侧生成同格式的截止时间。
        """
        cutoff = (now_bj() - timedelta(hours=max(1, int(within_hours)))).isoformat(timespec="seconds")
        where = "" if include_alternate else " AND is_alternate=0"
        return self.one(
            "SELECT * FROM price_tick WHERE symbol=? AND fetched_at >= ?" + where +
            " ORDER BY id DESC LIMIT 1", (symbol, cutoff))

    def price_history(self, symbol: str, limit: int = 120):
        return self.rows("SELECT symbol,price,fetched_at,source FROM price_tick "
                         "WHERE symbol=? AND is_alternate=0 ORDER BY id DESC LIMIT ?",
                         (symbol, limit))[::-1]

    # ---- 新闻 ----
    def insert_news(self, n: dict) -> bool:
        try:
            self.run("INSERT INTO news_item(dedup_hash,title,summary,url,source,published_at_local,"
                     "published_at_bj,language,topic,importance,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     (n["dedup_hash"], n.get("title"), n.get("summary"), n.get("url"),
                      n.get("source"), n.get("published_at_local"), n.get("published_at_bj"),
                      n.get("language"), n.get("topic"), n.get("importance", 0), n.get("fetched_at")))
            return True
        except sqlite3.IntegrityError:
            return False

    def recent_news(self, hours: int = 24, limit: int = 8) -> list[dict]:
        """按稿件发布时间取时间窗新闻（无发布时间才回退抓取时间）。

        不能只看 fetched_at：时段研判会把历史档案稿件写进同一张表，
        若按抓取时间过滤，几个月前的旧新闻会被当成「最近 24 小时要闻」
        展示，还会进入当日 AI 的输入。
        """
        cutoff = (now_bj() - timedelta(hours=max(1, int(hours)))).strftime("%Y-%m-%d %H:%M:%S")
        return self.rows(
            "SELECT * FROM news_item WHERE "
            "COALESCE(NULLIF(published_at_bj,''), NULLIF(substr(fetched_at,1,19),'')) >= ? "
            "ORDER BY importance DESC, id DESC LIMIT ?", (cutoff, limit))

    def news_between(self, start_date: str, end_date: str, limit: int = 300) -> list[dict]:
        """按稿件发布日（北京时间）取区间新闻；无发布时间时回退到抓取日。"""
        return self.rows(
            "SELECT * FROM news_item WHERE "
            "COALESCE(NULLIF(substr(published_at_bj,1,10),''), substr(fetched_at,1,10)) "
            "BETWEEN ? AND ? ORDER BY published_at_bj DESC, id DESC LIMIT ?",
            (start_date, end_date, limit))

    # ---- 信号 ----
    def insert_signal(self, s: dict):
        return self.run("INSERT INTO ai_signal(as_of_bj,horizon,direction,confidence,signal_json,"
                        "prompt_version,model,prompt_tokens,completion_tokens,latency_ms,snapshot_hash,"
                        "instance_id,trigger_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (s.get("as_of_bj"), s.get("horizon"), s.get("direction"), s.get("confidence"),
                         json.dumps(s, ensure_ascii=False), s.get("prompt_version"), s.get("model"),
                         s.get("prompt_tokens", 0), s.get("completion_tokens", 0), s.get("latency_ms", 0),
                         s.get("snapshot_hash"), self.instance_id, s.get("trigger_source", "scheduled")))

    def latest_signal(self):
        return self.one("SELECT * FROM ai_signal ORDER BY id DESC LIMIT 1")

    def recent_signals(self, limit: int = 10):
        return self.rows("SELECT * FROM ai_signal ORDER BY id DESC LIMIT ?", (limit,))

    def last_signal_hash(self, within_seconds: int = 300):
        r = self.one("SELECT snapshot_hash, as_of_bj FROM ai_signal ORDER BY id DESC LIMIT 1")
        return r["snapshot_hash"] if r else None

    # ---- 报告与投递 ----
    def insert_report(self, r: dict):
        return self.run("INSERT INTO daily_report(report_date,report_type,subject,html,text_body,"
                        "signal_id,generated_at) VALUES(?,?,?,?,?,?,?)",
                        (r.get("report_date"), r.get("report_type"), r.get("subject"),
                         r.get("html"), r.get("text_body"), r.get("signal_id"), r.get("generated_at")))

    def recent_reports(self, limit: int = 10):
        return self.rows("SELECT id,report_date,report_type,subject,generated_at,signal_id "
                         "FROM daily_report ORDER BY id DESC LIMIT ?", (limit,))

    def report(self, rid: int):
        return self.one("SELECT * FROM daily_report WHERE id=?", (rid,))

    def insert_delivery(self, d: dict):
        try:
            self.run("INSERT INTO delivery_log(message_id,report_id,channel,recipient,status,"
                     "attempt_count,error,sent_at,instance_id) VALUES(?,?,?,?,?,?,?,?,?)",
                     (d.get("message_id"), d.get("report_id"), d.get("channel"),
                      d.get("recipient"), d.get("status"), d.get("attempt_count", 0),
                      d.get("error"), d.get("sent_at"), self.instance_id))
        except sqlite3.IntegrityError:
            pass

    def recent_deliveries(self, limit: int = 20):
        return self.rows("SELECT * FROM delivery_log ORDER BY id DESC LIMIT ?", (limit,))

    def mails_today(self) -> int:
        d = now_bj().strftime("%Y-%m-%d")
        r = self.one("SELECT COUNT(*) n FROM delivery_log WHERE status='ok' AND sent_at LIKE ?", (d + "%",))
        return r["n"] if r else 0

    # ---- 用量 / 任务 ----
    def usage_today(self) -> dict:
        d = now_bj().strftime("%Y-%m-%d")
        r = self.one("SELECT * FROM usage_counter WHERE date=? AND instance_id=?", (d, self.instance_id))
        return r or {"date": d, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0}

    def add_usage(self, prompt_tokens: int, completion_tokens: int):
        d = now_bj().strftime("%Y-%m-%d")
        self.run("INSERT INTO usage_counter(date,instance_id,calls,prompt_tokens,completion_tokens) "
                 "VALUES(?,?,1,?,?) ON CONFLICT(date,instance_id) DO UPDATE SET "
                 "calls=calls+1, prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
                 "completion_tokens=completion_tokens+excluded.completion_tokens",
                 (d, self.instance_id, prompt_tokens or 0, completion_tokens or 0))

    def start_task(self, name, trigger):
        return self.run("INSERT INTO task_run(task_name,started_at,status,trigger_source) VALUES(?,?,?,?)",
                        (name, iso_bj(), "running", trigger))

    def end_task(self, tid, status, source_status=None, error=None, started=None):
        from datetime import datetime
        try:
            latency = int((datetime.now(started.tzinfo) - started).total_seconds() * 1000) if started else 0
        except Exception:
            latency = 0
        self.run("UPDATE task_run SET finished_at=?,status=?,source_status_json=?,error=?,latency_ms=? WHERE id=?",
                 (iso_bj(), status, json.dumps(source_status or {}, ensure_ascii=False),
                  (error or "")[:500], latency, tid))

    def recent_tasks(self, limit=20):
        return self.rows("SELECT * FROM task_run ORDER BY id DESC LIMIT ?", (limit,))

    def slot_fired(self, key: str) -> bool:
        return self.one("SELECT 1 FROM slot_fire WHERE fire_key=?", (key,)) is not None

    def mark_slot_fired(self, key: str, status="ok"):
        """同一时点只应有一条记录；状态可更新（running → done / failed）。"""
        self.run("INSERT INTO slot_fire(fire_key,fired_at,status) VALUES(?,?,?) "
                 "ON CONFLICT(fire_key) DO UPDATE SET fired_at=excluded.fired_at,status=excluded.status",
                 (key, iso_bj(), status))

    def clear_slot_fired(self, key: str):
        """任务失败时释放时点标记，让下一次调度可以重试。"""
        self.run("DELETE FROM slot_fire WHERE fire_key=?", (key,))

    def clear_stuck_slots(self, minutes: int = 30) -> int:
        """进程崩溃留下的 running/firing 标记：超过 N 分钟就释放，避免当天漏做。"""
        cutoff = (now_bj() - timedelta(minutes=max(1, minutes))).isoformat(timespec="seconds")
        with self.conn() as c:
            cur = c.execute("DELETE FROM slot_fire WHERE status IN ('running','firing') AND fired_at < ?",
                            (cutoff,))
            c.commit()
            return int(cur.rowcount or 0)

    # ---- 每日新闻 / 每日资讯 ----
    def replace_daily_items(self, day: str, src: str, items: list[dict]):
        """热搜榜每次采集都是一份完整快照：先清掉当天该平台的旧行再写入。"""
        with self.conn() as c:
            c.execute("DELETE FROM daily_item WHERE day=? AND kind='news' AND src=?", (day, src))
            for i, it in enumerate(items, 1):
                c.execute("INSERT OR IGNORE INTO daily_item(day,kind,src,src_name,rank,title,summary,url,"
                          "hot_value,published_at_bj,dedup_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (day, "news", src, it.get("src_name"), i, (it.get("title") or "")[:240],
                           (it.get("summary") or "")[:300], it.get("url"), it.get("hot_value"),
                           it.get("published_at_bj"), it.get("dedup_hash"), iso_bj()))
            c.commit()

    def add_daily_items(self, day: str, items: list[dict], kind: str = "news") -> int:
        """央视稿件按稿件 id 去重累积（同一篇稿子跨天出现只算首次采集那天）。"""
        n = 0
        with self.conn() as c:
            for it in items:
                try:
                    c.execute("INSERT INTO daily_item(day,kind,src,src_name,rank,title,summary,url,"
                              "hot_value,published_at_bj,dedup_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                              (day, kind, it.get("src"), it.get("src_name"), int(it.get("rank") or 0),
                               (it.get("title") or "")[:240], (it.get("summary") or "")[:300],
                               it.get("url"), None, it.get("published_at_bj"), it.get("dedup_hash"), iso_bj()))
                    n += 1
                except sqlite3.IntegrityError:
                    c.execute("UPDATE daily_item SET rank=? WHERE dedup_hash=?", (int(it.get("rank") or 0), it.get("dedup_hash")))
            c.commit()
        return n

    def drop_daily_items(self, day: str, kind: str = "news") -> int:
        """清掉某天某类的条目（重跑当日采集时用，避免新旧两次结果混在一份文档里）。"""
        with self.conn() as c:
            cur = c.execute("DELETE FROM daily_item WHERE day=? AND kind=?", (day, kind))
            c.commit()
            return cur.rowcount

    def daily_items(self, day: str, kind: str = "news", src: str | None = None, limit: int = 400) -> list[dict]:
        where, args = "day=? AND kind=?", [day, kind]
        if src:
            where += " AND src=?"
            args.append(src)
        return self.rows("SELECT * FROM daily_item WHERE %s ORDER BY src, rank, id LIMIT ?" % where,
                         tuple(args) + (int(limit),))

    # ---- 新闻正文本地缓存（key = 链接哈希，同一篇跨天只存一份） ----
    def article_get(self, key: str) -> dict | None:
        return self.one("SELECT * FROM article_cache WHERE key=?", (key,))

    def prune_article_cache(self, cutoff_iso: str) -> int:
        """清理早于 cutoff 的文章正文缓存，返回删除条数。"""
        with self.conn() as c:
            cur = c.execute("DELETE FROM article_cache WHERE fetched_at < ?", (cutoff_iso,))
            c.commit()
            return int(cur.rowcount or 0)

    def prune_events(self, keep: int = 3000) -> int:
        """事件日志只保留最近 N 条。"""
        row = self.one("SELECT COUNT(*) AS n FROM event_log")
        n = int((row or {}).get("n") or 0)
        if n <= keep:
            return 0
        self.run("DELETE FROM event_log WHERE id NOT IN "
                 "(SELECT id FROM event_log ORDER BY id DESC LIMIT ?)", (keep,))
        return n - keep

    def article_many(self, keys: list[str]) -> dict:
        keys = [k for k in (keys or []) if k]
        if not keys:
            return {}
        out = {}
        for chunk in range(0, len(keys), 200):
            part = keys[chunk:chunk + 200]
            rows = self.rows("SELECT * FROM article_cache WHERE key IN (%s)" % ",".join("?" * len(part)),
                             tuple(part))
            for r in rows:
                out[r["key"]] = r
        return out

    def article_save(self, d: dict):
        self.run("INSERT INTO article_cache(key,url,title,content,chars,status,fetched_at) "
                 "VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET url=excluded.url,"
                 "title=excluded.title,content=excluded.content,chars=excluded.chars,"
                 "status=excluded.status,fetched_at=excluded.fetched_at",
                 (d["key"], d.get("url") or "", (d.get("title") or "")[:240], d.get("content") or "",
                  int(d.get("chars") or 0), d.get("status") or "", iso_bj()))

    def upsert_daily_doc(self, d: dict):
        self.run("INSERT INTO daily_doc(day,kind,path,ai_available,ai_reason,summary_md,counts_json,generated_at) "
                 "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(day,kind) DO UPDATE SET path=excluded.path,"
                 "ai_available=excluded.ai_available,ai_reason=excluded.ai_reason,"
                 "summary_md=excluded.summary_md,counts_json=excluded.counts_json,"
                 "generated_at=excluded.generated_at",
                 (d.get("day"), d.get("kind"), d.get("path"), 1 if d.get("ai_available") else 0,
                  d.get("ai_reason") or "", d.get("summary_md") or "",
                  json.dumps(d.get("counts") or {}, ensure_ascii=False), iso_bj()))

    def daily_docs(self, kind: str, start: str | None = None, end: str | None = None,
                   limit: int = 400) -> list[dict]:
        where, args = "kind=?", [kind]
        if start:
            where += " AND day>=?"
            args.append(start)
        if end:
            where += " AND day<=?"
            args.append(end)
        return self.rows("SELECT * FROM daily_doc WHERE %s ORDER BY day DESC LIMIT ?" % where,
                         tuple(args) + (int(limit),))

    def insert_digest(self, d: dict) -> int:
        # 兼容 start/end 与 start_date/end_date 两种键名（起止日期不能落空，否则「已汇总到哪天」判断失效）
        return self.run("INSERT INTO digest_log(kind,period,start_date,end_date,label,days,path,ai_available,"
                        "mailed,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (d.get("kind"), d.get("period"), d.get("start_date") or d.get("start"),
                         d.get("end_date") or d.get("end"), d.get("label"),
                         d.get("days", 0), d.get("path"), 1 if d.get("ai_available") else 0,
                         1 if d.get("mailed") else 0, d.get("status") or "ok", iso_bj()))

    def recent_digests(self, kind: str | None = None, limit: int = 20) -> list[dict]:
        if kind:
            return self.rows("SELECT * FROM digest_log WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))
        return self.rows("SELECT * FROM digest_log ORDER BY id DESC LIMIT ?", (limit,))

    def news_of_day(self, day: str, limit: int = 60) -> list[dict]:
        """按发布日（缺发布时间时按抓取日）取某天的财经要闻。"""
        return self.rows(
            "SELECT * FROM news_item WHERE COALESCE(NULLIF(substr(published_at_bj,1,10),''), "
            "substr(fetched_at,1,10))=? ORDER BY importance DESC, id DESC LIMIT ?", (day, limit))

    # ---- 其它 ----
    def holdings(self):
        return self.one("SELECT * FROM holdings ORDER BY id DESC LIMIT 1")

    def save_holdings(self, grams: float, cost: float, note: str = ""):
        self.run("INSERT INTO holdings(grams,cost_per_gram,note,updated_at) VALUES(?,?,?,?)",
                 (grams, cost, note, iso_bj()))

    def audit(self, action: str, detail: str):
        self.run("INSERT INTO audit_log(at,action,detail,instance_id) VALUES(?,?,?,?)",
                 (iso_bj(), action, detail, self.instance_id))

    def recent_audit(self, limit=20):
        return self.rows("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))

    def event(self, level: str, message: str):
        self.run("INSERT INTO event_log(at,level,message) VALUES(?,?,?)", (iso_bj(), level, message[:500]))

    def recent_events(self, limit=30):
        return self.rows("SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,))

    def config_log(self, version: int, summary: str):
        self.run("INSERT INTO config_log(version,changed_at,summary) VALUES(?,?,?)",
                 (version, iso_bj(), short_hash(summary) + ":" + summary[:120]))

    # ---- 时段新闻研判留痕 ----
    def insert_history_run(self, r: dict):
        return self.run(
            "INSERT INTO history_analysis(start_date,end_date,preset,news_count,topic_stats_json,"
            "source_status_json,result_json,report_id,delivered,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (r.get("start_date"), r.get("end_date"), r.get("preset"), r.get("news_count", 0),
             json.dumps(r.get("topic_stats", []), ensure_ascii=False),
             json.dumps(r.get("source_status", {}), ensure_ascii=False),
             json.dumps(r.get("result", {}), ensure_ascii=False),
             r.get("report_id"), 1 if r.get("delivered") else 0, iso_bj()))

    def recent_history_runs(self, limit: int = 10) -> list[dict]:
        rows = self.rows(
            "SELECT id,start_date,end_date,preset,news_count,result_json,report_id,delivered,created_at "
            "FROM history_analysis ORDER BY id DESC LIMIT ?", (limit,))
        out = []
        for r in rows:
            try:
                res = json.loads(r.get("result_json") or "{}")
            except Exception:
                res = {}
            out.append({"id": r["id"], "start_date": r["start_date"], "end_date": r["end_date"],
                        "preset": r["preset"], "news_count": r["news_count"],
                        "direction": res.get("direction"), "confidence": res.get("confidence"),
                        "ai_available": not res.get("_fallback", False),
                        "report_id": r["report_id"], "delivered": bool(r["delivered"]),
                        "created_at": r["created_at"]})
        return out
