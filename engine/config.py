# -*- coding: utf-8 -*-
"""本端配置：结构、校验、版本留痕、回滚、密钥引用解析。

单端配置：config.json 只保存在本机 data 目录，无任何跨端同步逻辑（PRD §1.4 / FR-07）。
"""
from __future__ import annotations

import copy
import json
import os
import re
import uuid

from .util import hhmm_valid
from .secrets import SecretsStore

SENSITIVE_FIELDS = {
    "smtp.credential": "smtp_credential",
    "deepseek.api_key": "deepseek_api_key",
    # 时段研判的可选历史新闻源密钥（Alpha Vantage 免费 Key）
    "history_analysis.alpha_vantage_api_key": "history_alpha_vantage_api_key",
}
MASK_KEEP = "__KEEP_EXISTING__"
# 新浪 rss.sina.com.cn 的财经滚动 RSS 自 2018 年起不再更新（解析 0 条），
# 改用滚动新闻 JSON 接口；它只覆盖最近约 8 天，用作「近期尾部」补充。
SINA_ROLL_URL = "https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&num=50&page=1"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn/"}


def _build_default_config() -> dict:
    """默认配置字典。函数体在前、赋值在后，是因为字典里要调用 _digest_defaults。"""
    return {
        "timezone": "Asia/Shanghai",
        "usage": "single_user",
        "app_kind": "desktop",
        "instance_id": "",
        "config_version": 1,
        "last_modified": "",
        "bank": "待确认（银行积存金报价未接入）",
        "push_master_switch": True,
        "duplicate_push_hint_ack": False,

        "price_sources": [
            {"id": "xau_goldapi", "name": "gold-api.com", "kind": "gold_api",
             "url": "https://api.gold-api.com/price/XAU", "symbol": "XAU/USD",
             "enabled": True, "priority": 1},
            {"id": "xau_sina", "name": "新浪财经 hf_XAU", "kind": "sina_hf",
             "url": "https://hq.sinajs.cn/list=hf_XAU", "symbol": "XAU/USD",
             "enabled": True, "priority": 2},
            {"id": "fx_erapi", "name": "open.er-api.com", "kind": "fx_er",
             "url": "https://open.er-api.com/v6/latest/USD", "symbol": "USD/CNY",
             "enabled": True, "priority": 1},
            {"id": "sge_au9999", "name": "上金所 Au99.99（新浪行情）", "kind": "sina_sge",
             "url": "https://hq.sinajs.cn/list=gds_AU9999", "symbol": "Au99.99", "unit": "CNY/g",
             "enabled": True, "priority": 3},
            {"id": "bank_quote", "name": "银行积存金买卖价（需自备接口）", "kind": "generic_json",
             "url": "", "symbol": "BANK_ACCUMULATION", "field": "buy", "unit": "CNY/g",
             "enabled": False, "priority": 0},
        ],
        "price_cache_seconds": 120,
        "stale_after_minutes": 45,
        # 汇率源为日更，用独立的宽阈值，避免把正常日更数据误判为陈旧
        "stale_after_minutes_fx": 2880,
        "divergence_tolerance_pct": 1.0,
        "poll_minutes": {"active": 5, "midday": 20, "overnight": 10, "closed": 60},

        "smtp": {
            "enabled": False, "host": "", "port": 465, "security": "ssl_tls",
            "user": "", "credential": "", "from_name": "日常小助手",
            "to": [], "cc": [], "max_mails_per_day": 12,
            "timeout_seconds": 20, "retry_times": 3, "html_and_text": True,
        },
        "deepseek": {
            "base_url": "https://api.deepseek.com", "api_key": "",
            "model_flash": "deepseek-flash", "model_pro": "deepseek-v4-pro",
            "max_tokens": 1200, "timeout_seconds": 60, "retry_times": 2,
            "json_output": True, "max_calls_per_day": 40,
            "daily_token_budget": 200000, "thinking_enabled": False,
            # 给每日文档/周期汇总预留的额度：通用研判/快报先停，日报仍有额度可用
            "reserved_calls_for_daily": 6, "reserved_tokens_for_daily": 30000,
            "prompt_version": "v3",
        },

        "push_schedule": {
            "slots": [
                {"id": "p1", "time": "08:00", "type": "daily_summary", "enabled": True,
                 "channels": ["mail"], "note": "盘前每日总结"},
                {"id": "p2", "time": "12:30", "type": "flash", "enabled": True,
                 "channels": ["mail", "app"], "note": "午间快报"},
                {"id": "p3", "time": "20:00", "type": "flash", "enabled": True,
                 "channels": ["mail", "app"], "note": "欧美盘前快报"},
            ],
            "quiet_hours": {"start": "23:00", "end": "07:00", "allow_major_events": True},
            "max_event_mails_per_day": 4,
            "skip_on_market_closed": True,
            "daily_summary_slot": "p1",
        },
        "manual_fetch": {
            "min_interval_seconds": 60, "snapshot_cache_seconds": 300,
            "count_toward_daily_budget": True, "send_mail_by_default": False,
        },
        "alert": {"intraday_pct": 2.0, "news_importance": 8},
        "news": {
            "window_hours": 24, "max_items": 8, "languages": ["en", "zh"],
            "sources": [
                {"id": "cnbc", "name": "CNBC Markets", "lang": "en", "enabled": True,
                 "timeout": 10,
                 "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html"},
                {"id": "wsj", "name": "WSJ Markets", "lang": "en", "enabled": True,
                 "timeout": 10,
                 "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml"},
                {"id": "sina", "name": "新浪财经滚动", "lang": "zh", "enabled": True,
                 "timeout": 10, "pages": 4, "headers": dict(SINA_HEADERS),
                 "url": SINA_ROLL_URL},
                {"id": "gdelt", "name": "GDELT DOC（此网络可能超时）", "lang": "en", "enabled": False,
                 "url": "https://api.gdeltproject.org/api/v2/doc/doc?query=gold%20price&mode=artlist&maxrecords=20&format=json&timespan=1d"},
            ],
        },
        # FR-16 时段新闻回顾 + 未来涨跌情景研判
        # 外部历史档案源可插拔：任一失败不影响其它源，界面如实显示源状态
        "history_analysis": {
            "default_preset": "6m",
            "max_items": 60,
            "max_ai_items": 40,
            "min_news_for_direction": 8,
            "forward_horizon": "1-3m",
            "per_source_timeout_seconds": 10,
            "total_deadline_seconds": 22,
            "min_interval_seconds": 20,
            "send_mail_by_default": False,
            "alpha_vantage_api_key": "",
            "sources": [
                {"id": "local", "name": "本端已落库新闻", "kind": "local", "enabled": True},
                {"id": "rss", "name": "现有 RSS 源（覆盖区间尾部）", "kind": "rss", "enabled": True},
                {"id": "gdelt", "name": "GDELT DOC 历史档案", "kind": "gdelt", "enabled": True,
                 "url": "http://api.gdeltproject.org/api/v2/doc/doc", "query": "gold",
                 "maxrecords": 100},
                {"id": "googlenews", "name": "Google News 日期检索", "kind": "googlenews",
                 "enabled": True, "url": "https://news.google.com/rss/search"},
                {"id": "alphavantage", "name": "Alpha Vantage NEWS（免费 Key · 25 次/天）",
                 "kind": "alphavantage", "enabled": False,
                 "url": "https://www.alphavantage.co/query", "tickers": "GLD", "limit": 50},
            ],
        },
        "trade": {"confirm_in_same_instance": True, "daily_limit": 5000, "kill_switch": True},

        # 开机自启动：镜像值仅用于界面回显，真实状态以注册表 Run 项为准（engine/autostart.py）
        "autostart": False,
        # 文档保存目录（每日新闻 / 每日资讯 / 周期汇总的 Markdown）；留空 = 数据目录
        "doc_dir": "",
        # 每日新闻：央视新闻 + 互联网热搜 → AI 总结 → 每日文档 → 周期汇总邮件
        "daily": dict(_digest_defaults("20:30"),
                      cctv_enabled=True, cctv_max_items=20,
                      hot_enabled=True, hot_max_items=10,
                      hot_sources={"weibo": True, "baidu": True, "douyin": True,
                                   "bilibili": True, "zhihu": True}),
        # 每日资讯：本端已采集的财经要闻 → 每日文档 → 周期汇总邮件
        "brief": dict(_digest_defaults("21:10"), max_items=40),
        # 每日军事新闻：中国军网 / 人民网军事（国内）+ 央视国际军事（国际）→ 每日文档 → 周期汇总邮件
        "mil": dict(_digest_defaults("19:40"), max_items=60),
        # 每日综合：三个模块共用一套时间与汇总周期，到点依次生成三份文档，再合成一封邮件
        "daily_all": dict(_digest_defaults("20:30"), catchup_days=2),
        # 存储与清理：日志轮转固定 10MB×5；正文 HTML 与文章缓存按天数清理，Markdown 文档永久保留
        "storage": {"keep_body_days": 365, "keep_cache_days": 180,
                    "backup_keep": 10, "vacuum_min_mb": 50, "vacuum_days": 7},
        # 到价 / 均线提醒：rules 为规则列表，冷却时间避免同一条反复发信
        "alerts": {"enabled": True, "cooldown_hours": 12, "send_mail": True,
                   "max_age_minutes": 15, "rules": []},
        # 首次配置向导是否已完成（完成后不再自动弹出）
        "setup": {"done": False},
    }


def _digest_defaults(time: str) -> dict:
    """每日文档 + 周期汇总的公共默认项（每日新闻 / 每日资讯共用一套设置）。"""
    return {
        "enabled": True,
        "time": time,                    # 每日采集 / AI 总结时间
        "ai_enabled": True,
        "send_mail": False,              # 每日文档（含 AI 总结）是否发送到邮箱
        "digest_periods": ["1m"],       # 周期汇总，可多选：1d 每天 | 1w 每周 | 1m 每月 | 6m 每半年 | 1y 每年 | custom 自定义；空列表 = 不汇总
        "digest_custom_days": 15,        # custom 时的间隔天数
        "digest_time": "21:30",          # 周期汇总生成（并发信）时间
        "digest_send_mail": True,        # 周期汇总是否发送到邮箱
    }


DIGEST_PERIODS = ("1d", "1w", "1m", "6m", "1y", "custom")
DIGEST_PERIOD_CN = {"1d": "每天", "1w": "每周", "1m": "每月", "6m": "每半年", "1y": "每年", "custom": "自定义"}


def digest_periods(sec: dict) -> list:
    """取出某个类型（每日新闻 / 每日资讯）已勾选的汇总周期列表（去重、保持勾选顺序）。

    新配置是列表 digest_periods；旧配置是单值 digest_period，这里顺手兼容。
    显式保存成空列表表示「不做周期汇总」。
    """
    raw = sec.get("digest_periods")
    if not isinstance(raw, list):
        raw = [sec.get("digest_period")] if sec.get("digest_period") else None
    if raw is None:
        raw = ["1m"]
    out = []
    for p in raw:
        if p in DIGEST_PERIODS and p not in out:
            out.append(p)
    return out

_DEFAULT_CONFIG = _build_default_config()


def _validate_digest_section(name: str, sec: dict):
    """每日新闻 / 每日资讯：时间格式、周期（可多选）、条数、自定义天数。"""
    if not hhmm_valid(str(sec.get("time", ""))):
        raise ConfigError("%s的每日时间格式必须为 HH:mm" % name)
    if not hhmm_valid(str(sec.get("digest_time", ""))):
        raise ConfigError("%s的汇总时间格式必须为 HH:mm" % name)
    raw = sec.get("digest_periods")
    if raw is not None and not isinstance(raw, list):
        raise ConfigError("%s的汇总周期必须是列表" % name)
    if isinstance(raw, list):
        bad = [p for p in raw if p not in DIGEST_PERIODS]
        if bad:
            raise ConfigError("%s的汇总周期不合法：%r" % (name, bad))
    # 归一：旧的单值 digest_period 迁移为列表，之后配置里只保留列表；空列表 = 不做周期汇总
    sec["digest_periods"] = [] if raw == [] else digest_periods(sec)
    sec.pop("digest_period", None)
    n = sec.get("digest_custom_days")
    if not isinstance(n, int) or not (1 <= n <= 3650):
        raise ConfigError("%s的自定义汇总天数必须为 1–3650 的整数" % name)


def default_config() -> dict:
    """返回一份全新的默认配置（深拷贝，调用方可随意修改）。"""
    return copy.deepcopy(_DEFAULT_CONFIG)


class ConfigError(ValueError):
    pass


def _merge(base: dict, patch: dict):
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _validate(cfg: dict):
    """保存前校验；不通过直接抛错，绝不静默沿用旧值（FR-14 / FR-15）。"""
    sch = cfg.get("push_schedule", {})
    seen = set()
    for s in sch.get("slots", []):
        t = s.get("time", "")
        if not hhmm_valid(t):
            raise ConfigError("推送时点格式必须为 HH:mm（00:00–23:59）：%r" % t)
        if s.get("type") not in ("daily_summary", "flash", "both"):
            raise ConfigError("时点类型不合法：%r" % s.get("type"))
        if t in seen:
            raise ConfigError("同一时点存在多条配置：%s（请合并或改时间）" % t)
        seen.add(t)
    qh = sch.get("quiet_hours", {})
    if not hhmm_valid(qh.get("start", "")) or not hhmm_valid(qh.get("end", "")):
        raise ConfigError("静默时段格式必须为 HH:mm")
    smtp = cfg.get("smtp", {})
    if smtp.get("enabled"):
        if not smtp.get("host"):
            raise ConfigError("已启用邮件，但 SMTP 服务器为空")
        if not isinstance(smtp.get("port"), int) or not (1 <= smtp["port"] <= 65535):
            raise ConfigError("SMTP 端口必须为 1–65535 的整数")
        if smtp.get("security") not in ("ssl_tls", "starttls", "none"):
            raise ConfigError("加密方式不合法")
        to = smtp.get("to") or []
        if not to:
            raise ConfigError("已启用邮件，但收件人为空")
        mail_re = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
        for m in list(to) + list(smtp.get("cc") or []):
            if not mail_re.match(str(m)):
                raise ConfigError("邮箱格式不正确：%r" % m)
        if smtp.get("user") and not mail_re.match(str(smtp["user"])):
            raise ConfigError("发件邮箱格式不正确")
    ds = cfg.get("deepseek", {})
    url = str(ds.get("base_url", ""))
    if not re.match(r"^https?://[^\s/]+", url):
        raise ConfigError("DeepSeek Base URL 不合法：%r" % url)
    for key in ("max_tokens", "timeout_seconds", "retry_times", "max_calls_per_day", "daily_token_budget"):
        v = ds.get(key)
        if not isinstance(v, int) or v <= 0:
            raise ConfigError("DeepSeek 参数 %s 必须为正整数" % key)
    for key, lo, hi in (("reserved_calls_for_daily", 0, 40),
                        ("reserved_tokens_for_daily", 0, 200000)):
        v = ds.get(key, 0)
        if not isinstance(v, int) or not (lo <= v <= hi):
            raise ConfigError("DeepSeek 预留额度 %s 必须为 %d–%d 的整数" % (key, lo, hi))
    if ds.get("reserved_calls_for_daily", 0) >= ds.get("max_calls_per_day", 40):
        raise ConfigError("每日预留调用次数必须小于每日调用上限")
    for key in ("max_mails_per_day", "timeout_seconds", "retry_times"):
        v = smtp.get(key)
        if not isinstance(v, int) or v <= 0:
            raise ConfigError("SMTP 参数 %s 必须为正整数" % key)
    if not isinstance(sch.get("max_event_mails_per_day"), int) or sch["max_event_mails_per_day"] < 0:
        raise ConfigError("单日事件级推送上限必须为非负整数")
    mf = cfg.get("manual_fetch", {})
    if not isinstance(mf.get("min_interval_seconds"), int) or mf["min_interval_seconds"] < 0:
        raise ConfigError("手动获取最小间隔必须为非负整数")
    ha = cfg.get("history_analysis", {})
    if ha:
        for key in ("max_items", "max_ai_items", "min_news_for_direction",
                    "per_source_timeout_seconds", "total_deadline_seconds"):
            v = ha.get(key, 0)
            if not isinstance(v, (int, float)) or v <= 0:
                raise ConfigError("时段研判参数 %s 必须为正数" % key)
        # 重复运行最小间隔允许填 0（表示不节流），与界面 min=0 保持一致
        iv = ha.get("min_interval_seconds", 0)
        if not isinstance(iv, (int, float)) or iv < 0:
            raise ConfigError("时段研判参数 min_interval_seconds 必须为非负数（0 表示不节流）")
        if ha.get("forward_horizon") not in ("1-4w", "1-3m", "3-6m"):
            raise ConfigError("未来观察尺度不合法")
        if ha.get("default_preset") not in ("3m", "6m", "1y", "custom"):
            raise ConfigError("默认时间段预设不合法")
        if not isinstance(ha.get("sources", []), list):
            raise ConfigError("时段研判数据源配置必须是列表")
    for name, key in (("每日新闻", "daily"), ("每日资讯", "brief"), ("每日军事新闻", "mil")):
        sec = cfg.get(key) or {}
        _validate_digest_section(name, sec)
        for field, lo, hi in (("cctv_max_items", 1, 100), ("hot_max_items", 1, 50), ("max_items", 1, 200)):
            if field in sec and (not isinstance(sec[field], int) or not (lo <= hi and lo <= sec[field] <= hi)):
                raise ConfigError("%s的 %s 必须为 %d–%d 的整数" % (name, field, lo, hi))
        if not isinstance(sec.get("hot_sources", {}), dict):
            raise ConfigError("热搜平台开关配置必须是键值对")
    all_sec = cfg.get("daily_all")
    if all_sec is not None:
        _validate_digest_section("每日综合", all_sec)
        cd = all_sec.get("catchup_days", 0)
        if not isinstance(cd, int) or not (0 <= cd <= 7):
            raise ConfigError("跨天补生成天数必须为 0–7 的整数")
    st = cfg.get("storage") or {}
    st_defaults = {"keep_body_days": 365, "keep_cache_days": 180, "backup_keep": 10,
                   "vacuum_min_mb": 50, "vacuum_days": 7}
    for key, lo, hi in (("keep_body_days", 0, 3650), ("keep_cache_days", 0, 3650),
                        ("backup_keep", 1, 100), ("vacuum_min_mb", 0, 100000),
                        ("vacuum_days", 0, 365)):
        v = st.get(key, st_defaults[key])
        if not isinstance(v, int) or not (lo <= v <= hi):
            raise ConfigError("存储清理参数 %s 必须为 %d–%d 的整数" % (key, lo, hi))
    al = cfg.get("alerts") or {}
    if not isinstance(al.get("rules", []), list):
        raise ConfigError("到价提醒规则必须是列表")
    ch = al.get("cooldown_hours", 12)
    if not isinstance(ch, (int, float)) or ch < 0:
        raise ConfigError("提醒冷却小时必须为非负数")
    ma = al.get("max_age_minutes", 15)
    if not isinstance(ma, int) or not (1 <= ma <= 1440):
        raise ConfigError("提醒报价最大时效必须为 1–1440 分钟")
    for i, r in enumerate(al.get("rules", []) or [], 1):
        if not isinstance(r, dict):
            raise ConfigError("第 %d 条提醒规则格式不正确" % i)
        if r.get("type") not in ("price_above", "price_below", "pct_up", "pct_down",
                                 "ma20_above", "ma20_below"):
            raise ConfigError("第 %d 条提醒规则的类型不合法" % i)
        try:
            float(r.get("value"))
        except (TypeError, ValueError):
            raise ConfigError("第 %d 条提醒规则的数值必须为数字" % i)


class ConfigStore:
    """单端配置存储。data_dir 默认指向 desktop/data。"""

    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        os.makedirs(self.data_dir, exist_ok=True)
        self.path = os.path.join(self.data_dir, "config.json")
        self.backup_path = os.path.join(self.data_dir, "config.backup.json")
        self.app_kind = "desktop"
        if not os.path.exists(self.path):
            cfg = default_config()
            cfg["instance_id"] = "%s-%s" % (self.app_kind, uuid.uuid4().hex[:12])
            self._write(cfg)
        self.cfg = self._read()
        self._ensure_defaults()
        if not self.cfg.get("instance_id"):
            self.cfg["instance_id"] = "%s-%s" % (self.app_kind, uuid.uuid4().hex[:12])
            self._write(self.cfg)
        self.secrets = SecretsStore(self.data_dir, self.cfg["instance_id"])

    def _ensure_defaults(self):
        """升级兼容：老配置缺少的顶层字段按默认值补齐，不覆盖用户已保存的值。"""
        base = default_config()
        changed = False
        # 旧版 APK 实例的配置若被搬到本端，强制归一为桌面端
        if self.cfg.get("app_kind") != self.app_kind:
            self.cfg["app_kind"] = self.app_kind
            changed = True
        had_all = "daily_all" in self.cfg
        for k, v in base.items():
            if k not in self.cfg:
                self.cfg[k] = v
                changed = True
        # 旧配置的嵌套段补齐新增键（不覆盖用户已保存值）
        nested_defaults = {
            "deepseek": {"reserved_calls_for_daily": 6, "reserved_tokens_for_daily": 30000},
            "storage": {"backup_keep": 10, "vacuum_min_mb": 50, "vacuum_days": 7},
            "daily_all": {"catchup_days": 2},
            "alerts": {"max_age_minutes": 15},
        }
        for section, defaults in nested_defaults.items():
            sec = self.cfg.get(section)
            if not isinstance(sec, dict):
                continue
            for key, val in defaults.items():
                if key not in sec:
                    sec[key] = val
                    changed = True
        # 上金所 Au99.99：旧默认是「需自备接口」的空壳，现在改用新浪行情接口并默认启用
        for s in (self.cfg.get("price_sources") or []):
            if not isinstance(s, dict) or s.get("id") != "sge_au9999":
                continue
            if s.get("kind") != "sina_sge" and not str(s.get("url") or "").strip():
                s["kind"] = "sina_sge"
                s["url"] = "https://hq.sinajs.cn/list=gds_AU9999"
                s["name"] = "上金所 Au99.99（新浪行情）"
                s["unit"] = "CNY/g"
                s.pop("field", None)
                s["enabled"] = True
                changed = True
        # 周期汇总：旧的单值 digest_period 迁移为多选列表 digest_periods（每日新闻 / 每日资讯 / 每日军事新闻）
        for key in ("daily", "brief", "mil"):
            sec = self.cfg.get(key)
            if isinstance(sec, dict) and "digest_periods" not in sec:
                sec["digest_periods"] = digest_periods(sec)
                sec.pop("digest_period", None)
                changed = True
        # 旧界面把「每日新闻」设置误写进了 news 段（新闻源配置）：清掉不属于它的键，
        # 真正的每日新闻配置在 daily 段。这里只做清理，不改动 daily。
        news_sec = self.cfg.get("news")
        if isinstance(news_sec, dict):
            for k in ("enabled", "time", "ai_enabled", "send_mail", "digest_periods", "digest_period",
                      "digest_custom_days", "digest_time", "digest_send_mail", "cctv_enabled",
                      "cctv_max_items", "hot_enabled", "hot_max_items", "hot_sources"):
                if news_sec.pop(k, None) is not None:
                    changed = True
        # 新浪 RSS 早已停更：把旧默认地址迁移到滚动 JSON 接口（只改仍指向死源的配置）
        for s in (self.cfg.get("news", {}).get("sources") or []):
            if not isinstance(s, dict):
                continue
            if s.get("id") == "sina" and "rss.sina.com.cn" in str(s.get("url") or ""):
                s["url"] = SINA_ROLL_URL
                s["name"] = "新浪财经滚动"
                s["headers"] = dict(SINA_HEADERS)
                s.setdefault("pages", 4)
                s.setdefault("timeout", 10)
                changed = True
        # Alpha Vantage 新闻源：旧默认用 GOLD（巴里克矿业代码），改成 GLD（黄金 ETF）更贴近金价
        for s in (self.cfg.get("history_analysis", {}).get("sources") or []):
            if not isinstance(s, dict) or s.get("id") != "alphavantage":
                continue
            if str(s.get("tickers") or "").upper() == "GOLD":
                s["tickers"] = "GLD"
                changed = True
            if "limit" not in s:
                s["limit"] = 50
                changed = True
            if "需自备 Key" in str(s.get("name") or ""):
                s["name"] = "Alpha Vantage NEWS（免费 Key · 25 次/天）"
                changed = True
        # 旧的采集时限（单源 6s / 总 12s）会把 CNBC、4 页新浪滚动都卡掉，抬到新默认值
        hist = self.cfg.get("history_analysis") or {}
        if int(hist.get("per_source_timeout_seconds") or 0) < 10:
            hist["per_source_timeout_seconds"] = 10
            changed = True
        if int(hist.get("total_deadline_seconds") or 0) < 20:
            hist["total_deadline_seconds"] = 22
            changed = True
        # 首次引入「每日综合」：沿用每日新闻原有时间与汇总周期，并把三个模块各自的发信关掉，
        # 避免旧配置继续一天发三封（合并邮件由 daily_all 统一控制）。
        if not had_all:
            daily = self.cfg.get("daily") or {}
            olds = [self.cfg.get(k) or {} for k in ("daily", "brief", "mil")]
            all_sec = self.cfg.get("daily_all") or {}
            all_sec["time"] = daily.get("time") or all_sec.get("time") or "20:30"
            all_sec["ai_enabled"] = daily.get("ai_enabled", True)
            # 任一旧模块原来开过发信，就保留合并发信，避免升级后突然收不到邮件
            all_sec["send_mail"] = any(bool(s.get("send_mail")) for s in olds)
            merged = []
            for s in olds:
                for per in digest_periods(s):
                    if per not in merged:
                        merged.append(per)
            # 合并日报本身每天一封，再把「每天」汇总也开着会同一天发两封，默认去掉
            if all_sec["send_mail"]:
                merged = [p for p in merged if p != "1d"] or [p for p in digest_periods(daily) if p != "1d"] or ["1m"]
            all_sec["digest_periods"] = merged or digest_periods(daily)
            all_sec["digest_custom_days"] = daily.get("digest_custom_days") or 15
            all_sec["digest_time"] = daily.get("digest_time") or "21:30"
            all_sec["digest_send_mail"] = any(bool(s.get("digest_send_mail")) for s in olds)
            self.cfg["daily_all"] = all_sec
            for k in ("daily", "brief", "mil"):
                sec = self.cfg.get(k)
                if isinstance(sec, dict):
                    sec["send_mail"] = False
                    sec["digest_send_mail"] = False
            changed = True
        if changed:
            self._write(self.cfg)

    def _read(self) -> dict:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, cfg: dict):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    @property
    def instance_id(self) -> str:
        return self.cfg["instance_id"]

    def get(self) -> dict:
        return copy.deepcopy(self.cfg)

    def secret(self, dotted: str) -> str:
        name = SENSITIVE_FIELDS.get(dotted)
        return (self.secrets.get(name) or "") if name else ""

    def public(self) -> dict:
        """给界面用：敏感字段替换为掩码，并附 *_set 标记；绝不返回明文。"""
        from .util import mask
        cfg = self.get()
        cfg["smtp"]["credential"] = mask(self.secret("smtp.credential"))
        cfg["smtp"]["credential_set"] = self.secrets.has("smtp_credential")
        cfg["deepseek"]["api_key"] = mask(self.secret("deepseek.api_key"))
        cfg["deepseek"]["api_key_set"] = self.secrets.has("deepseek_api_key")
        ha = cfg.setdefault("history_analysis", {})
        ha["alpha_vantage_api_key"] = mask(self.secret("history_analysis.alpha_vantage_api_key"))
        ha["alpha_vantage_api_key_set"] = self.secrets.has("history_alpha_vantage_api_key")
        cfg["secure_backend"] = "Windows DPAPI" if self.secrets.windows else "机器绑定密钥（弱加密，见 README）"
        return cfg

    def update(self, patch: dict) -> dict:
        """保存配置。值为 MASK_KEEP / 纯掩码时保留原密钥，不清空、不覆盖。"""
        new = self.get()
        secrets_patch = {}
        for dotted in SENSITIVE_FIELDS:
            section, field = dotted.split(".")
            if section in (patch or {}) and field in patch[section]:
                val = patch[section][field]
                if isinstance(val, str) and (val == MASK_KEEP or set(val) <= set("*•") or val.strip() == ""):
                    patch[section].pop(field)
                elif isinstance(val, str) and val:
                    secrets_patch[dotted] = val
                    patch[section][field] = SecretsStore.ref(SENSITIVE_FIELDS[dotted])
        _merge(new, patch or {})
        # 录入 Alpha Vantage Key 即自动打开该历史源，省得再去找开关
        if "history_analysis.alpha_vantage_api_key" in secrets_patch:
            for s in (new.get("history_analysis", {}).get("sources") or []):
                if isinstance(s, dict) and s.get("id") == "alphavantage":
                    s["enabled"] = True
        new["app_kind"] = self.app_kind
        new["instance_id"] = self.instance_id
        _validate(new)
        # 敏感字段在 config.json 中只保留引用，不落明文
        for dotted, name in SENSITIVE_FIELDS.items():
            section, field = dotted.split(".")
            v = new.get(section, {}).get(field)
            if v and not SecretsStore.is_ref(v):
                secrets_patch[dotted] = v
            new.setdefault(section, {})[field] = SecretsStore.ref(name)
        backup = self.get()
        for dotted, name in SENSITIVE_FIELDS.items():
            section, field = dotted.split(".")
            backup.setdefault(section, {})[field] = SecretsStore.ref(name)
        with open(self.backup_path, "w", encoding="utf-8") as f:
            json.dump(backup, f, ensure_ascii=False, indent=2)
        new["config_version"] = int(new.get("config_version", 1)) + 1
        from .util import iso_bj
        new["last_modified"] = iso_bj()
        self._write(new)
        self.cfg = new
        for dotted, val in secrets_patch.items():
            self.secrets.set(SENSITIVE_FIELDS[dotted], val)
        return {"ok": True, "config_version": new["config_version"],
                "last_modified": new["last_modified"]}

    def rollback(self) -> dict:
        if not os.path.exists(self.backup_path):
            raise ConfigError("没有可恢复的上一版本")
        with open(self.backup_path, "r", encoding="utf-8") as f:
            old = json.load(f)
        old["config_version"] = int(self.cfg.get("config_version", 1)) + 1
        from .util import iso_bj
        old["last_modified"] = iso_bj()
        self._write(old)
        self.cfg = old
        return {"ok": True, "config_version": old["config_version"]}

