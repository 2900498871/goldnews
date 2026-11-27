# -*- coding: utf-8 -*-
"""国际新闻采集：多源 RSS/GDELT、去重去噪、主题分类、时间归一（FR-02）。

只存标题、摘要、链接、来源与时间，不存全文（版权约束）。
任一源失败不影响其它源；全部失败时返回 news_available=False，
由 AI 输入显式标注缺失，禁止模型自行补新闻。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import xml.etree.ElementTree as ET

from .util import bj_str, http_get, iso_bj, now_bj, short_hash, to_bj

TOPIC_RULES = [
    ("gold", ["gold", "bullion", "xau", "precious metal", "黄金", "金价", "贵金属"]),
    ("inflation_fed", ["fed", "fomc", "rate cut", "rate hike", "cpi", "inflation", "powell",
                       "nonfarm", "payroll", "就业", "通胀", "美联储", "加息", "降息", "议息", "非农"]),
    ("usd_real_rates", ["dollar", "treasury", "yield", "real rate", "dxy", "bond",
                        "美元", "美债", "收益率", "国债"]),
    ("geopolitics", ["war", "conflict", "tariff", "sanction", "geopolit", "haven", "risk-off",
                     "地缘", "冲突", "关税", "避险", "制裁"]),
    ("central_bank_etf", ["central bank", "etf", "gold purchase", "reserve", "inflow", "holding",
                           "央行", "购金", "资金流", "持仓"]),
    ("fx_supply", ["yuan", "cny", "rmb", "人民币", "汇率", "supply", "demand", "mine", "产量"]),
]
# 只有命中黄金本身，或命中下列高置信宏观事件词，才认为与金价相关。
# 用词边界匹配，避免 "warning" 命中 "war"、"Warsh" 命中 "war" 这类误判。
GOLD_CORE = ["gold", "bullion", "xau", "precious metal", "黄金", "金价", "贵金属"]
MACRO_CORE = ["fed", "fomc", "cpi", "inflation", "powell", "nonfarm", "payroll", "rate cut",
              "rate hike", "treasury yield", "dollar index", "dxy", "central bank", "etf",
              "geopolitical", "safe haven", "tariff", "sanction", "interest rate",
              "美联储", "通胀", "降息", "加息", "议息", "非农", "美债", "美元指数",
              "央行", "购金", "地缘", "关税", "避险", "制裁", "人民币", "汇率"]
BLOCK = ["sponsored", "advertorial", "newsletter signup", "subscribe now", "广告", "推广"]


def _hit(text: str, kw: str) -> bool:
    """中文用子串；英文用词边界，防止短词误命中。"""
    if re.search(r"[\u4e00-\u9fff]", kw):
        return kw in text
    return re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", text) is not None


def _hits_any(text: str, kws: list[str]) -> bool:
    return any(_hit(text, k) for k in kws)


def _clean(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _canon_url(u: str) -> str:
    if not u:
        return ""
    try:
        p = urlsplit(u)
        q = [(k, v) for k, v in parse_qsl(p.query) if not k.lower().startswith(("utm_", "fbclid", "gclid"))]
        return urlunsplit((p.scheme, p.netloc.lower(), p.path, urlencode(q), ""))
    except Exception:
        return u


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9\u4e00-\u9fff]{3,}", (s or "").lower()))


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _topic(text: str) -> str:
    low = text.lower()
    # 先归到具体宏观驱动，纯黄金稿才归 gold，便于归因
    for t, kws in TOPIC_RULES[1:]:
        if _hits_any(low, kws):
            return t
    if _hits_any(low, GOLD_CORE):
        return "gold"
    return "macro"


def _importance(title: str, summary: str, topic: str, hours_old: float) -> int:
    score = 4
    low = (title + " " + summary).lower()
    if _hits_any(low, ["fed", "fomc", "cpi", "rate decision", "powell", "美联储", "议息", "通胀", "nonfarm", "非农"]):
        score += 3
    if _hits_any(low, ["geopolitical", "sanction", "tariff", "地缘", "关税", "制裁"]):
        score += 2
    if _hits_any(low, GOLD_CORE):
        score += 2
    if hours_old <= 6:
        score += 1
    return min(10, score)


def _parse_time(s: str | None):
    """返回 (本地时间字符串, 北京时间字符串)。"""
    if not s:
        return "", ""
    raw = str(s).strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        bj = to_bj(dt)
        return dt.strftime("%Y-%m-%d %H:%M %Z") or dt.isoformat(timespec="minutes"), bj_str(bj, "%Y-%m-%d %H:%M")
    except Exception:
        pass
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.isoformat(timespec="minutes"), bj_str(to_bj(dt), "%Y-%m-%d %H:%M")
    except Exception:
        return raw, ""


def _parse_rss(text: str, src: dict) -> list[dict]:
    text = text.lstrip("\ufeff \r\n\t")
    root = ET.fromstring(text)
    items = []
    ns = {"a": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
    for node in list(root.iter()):
        tag = node.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue

        def txt(*names):
            for n in names:
                for el in node.iter():
                    if el.tag.split("}")[-1] == n and el.text:
                        return _clean(el.text)
            return ""

        title = txt("title")
        link = txt("link")
        if not link:
            for el in node.iter():
                if el.tag.split("}")[-1] == "link" and el.get("href"):
                    link = el.get("href")
                    break
        summary = _clean(txt("description", "summary", "encoded"))[:300]
        date_s = txt("pubDate", "published", "updated", "date")
        if title:
            items.append({"title": title[:240], "summary": summary, "url": _canon_url(link),
                          "source": src.get("name") or src.get("id"), "published_raw": date_s,
                          "language": src.get("lang", "en")})
    return items


def _parse_sina_roll(text: str, src: dict) -> list[dict]:
    """新浪财经滚动新闻 JSON（feed.mix.sina.com.cn）。

    新浪的 RSS（rss.sina.com.cn/roll/...）自 2018 年起已停止更新，解析为 0 条；
    滚动接口仍可用，但只保留最近约 8 天的列表，适合补近期尾部，不能当历史档案。
    """
    d = json.loads(text)
    arr = ((d.get("result") or {}).get("data")) or []
    out = []
    for a in arr:
        title = _clean(a.get("title"))
        if not title:
            continue
        raw = ""
        ct = str(a.get("ctime") or "")
        if ct.isdigit():
            try:
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(int(ct), tz=timezone.utc)
                raw = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
            except Exception:
                raw = ""
        out.append({"title": title[:240], "summary": _clean(a.get("intro"))[:300],
                    "url": _canon_url(a.get("url")),
                    "source": a.get("media_name") or src.get("name") or "新浪财经",
                    "published_raw": raw, "language": "zh"})
    return out


def parse_news_payload(text: str, src: dict) -> list[dict]:
    """按源地址选择解析器：GDELT JSON / 新浪滚动 JSON / 通用 RSS。"""
    u = src.get("url") or ""
    if "gdeltproject" in u:
        return _parse_gdelt(text, src)
    if "feed.mix.sina.com.cn" in u or (text.lstrip().startswith("{") and '"ctime"' in text):
        return _parse_sina_roll(text, src)
    return _parse_rss(text, src)


def _parse_gdelt(text: str, src: dict) -> list[dict]:
    d = json.loads(text)
    out = []
    for a in (d.get("articles") or []):
        title = _clean(a.get("title"))
        if not title:
            continue
        raw = a.get("seendate", "")
        try:
            from datetime import datetime, timezone
            dt = datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            date_s = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except Exception:
            date_s = raw
        out.append({"title": title[:240], "summary": "", "url": _canon_url(a.get("url")),
                    "source": a.get("domain") or src.get("name"), "published_raw": date_s,
                    "language": src.get("lang", "en")})
    return out


def _fetch_source(src: dict, window_hours: int) -> tuple[str, list[dict], str]:
    sid = src.get("id", "unknown")
    try:
        status, text, _ = http_get(src["url"], timeout=src.get("timeout", 12),
                                   headers=src.get("headers"))
        if status != 200 or not text.strip():
            return sid, [], "HTTP %s / 空响应" % status
        items = parse_news_payload(text, src)
        return sid, items, "ok"
    except Exception as e:
        return sid, [], "%s: %s" % (type(e).__name__, str(e)[:140])


class NewsCollector:
    def __init__(self, cfg_store, db, logger):
        self.cfg_store = cfg_store
        self.db = db
        self.log = logger

    def collect(self, trigger: str = "scheduled") -> dict:
        cfg = self.cfg_store.get()
        ncfg = cfg.get("news", {})
        window = int(ncfg.get("window_hours", 24))
        max_items = int(ncfg.get("max_items", 8))
        sources = [s for s in ncfg.get("sources", []) if s.get("enabled")]
        all_items, status = [], {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(sources)))) as ex:
            futs = [ex.submit(_fetch_source, s, window) for s in sources]
            for f in as_completed(futs):
                sid, items, st = f.result()
                status[sid] = st if st != "ok" else "ok (%d 条)" % len(items)
                all_items.extend(items)
        # 相关性过滤（关键词白名单 + 黑名单）
        cutoff = now_bj() - timedelta(hours=window)
        kept, seen_urls, seen_titles = [], set(), []
        for it in all_items:
            text = (it["title"] + " " + it["summary"]).lower()
            if any(b in text for b in BLOCK):
                continue
            # 相关性：必须是黄金稿，或命中高置信宏观事件词；用词边界避免 warning/war 类误命中
            if not (_hits_any(text, GOLD_CORE) or _hits_any(text, MACRO_CORE)):
                continue
            local_s, bj_s = _parse_time(it.get("published_raw"))
            it["published_at_local"], it["published_at_bj"] = local_s, bj_s
            if window <= 48 and bj_s:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(bj_s, "%Y-%m-%d %H:%M").replace(tzinfo=now_bj().tzinfo)
                    if dt < cutoff:
                        continue
                except ValueError:
                    pass
            hours_old = 24.0
            if bj_s:
                try:
                    from datetime import datetime
                    dt = datetime.strptime(bj_s, "%Y-%m-%d %H:%M").replace(tzinfo=now_bj().tzinfo)
                    hours_old = max(0.0, (now_bj() - dt).total_seconds() / 3600)
                except ValueError:
                    pass
            it["topic"] = _topic(text)
            it["importance"] = _importance(it["title"], it["summary"], it["topic"], hours_old)
            u = it.get("url") or ""
            toks = _tokens(it["title"])
            if (u and u in seen_urls) or any(_jaccard(toks, t) > 0.72 for t in seen_titles):
                continue
            if u:
                seen_urls.add(u)
            seen_titles.append(toks)
            it["dedup_hash"] = short_hash(_canon_url(u) + "|" + it["title"].lower())
            it["fetched_at"] = iso_bj()
            kept.append(it)
        kept.sort(key=lambda x: (x["importance"], x.get("published_at_bj", "")), reverse=True)
        kept = kept[:max_items]
        for it in kept:
            self.db.insert_news(it)
        ok_sources = [k for k, v in status.items() if v.startswith("ok")]
        return {
            "news_available": bool(kept) and bool(ok_sources),
            "news": kept, "source_status": status,
            "note": "" if kept else "本次未获取到新闻（全部新闻源失败或时间窗内无相关稿件）",
            "fetched_at": iso_bj(), "trigger_source": trigger,
        }
