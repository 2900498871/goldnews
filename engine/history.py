# -*- coding: utf-8 -*-
"""时段新闻回顾 + 未来涨跌情景研判（FR-16）。

按用户选定的时间区间（默认近半年 / 近一年，也支持自定义日期）采集新闻，
做去重、主题分类、月度统计，再交给 DeepSeek 输出结构化的「未来方向倾向」，
最后渲染成 HTML + 纯文本报告，可选择是否投递到邮箱。

合规边界（与 PRD §6 / §15 一致）：
  * 不承诺收益、保本或预测准确率；不给买卖点位、仓位比例、目标价或确定性指令。
  * 所有事实只来自 input 中给出的新闻与统计，禁止模型补新闻、补价格、补日期。
  * 样本不足 / 外部档案源不可用 / 低置信度 → 强制 direction=unclear。
  * 固定免责声明原样附加，禁止模型改写。

只依赖 Python 标准库；外部源全部可插拔，任一失败不影响其它源。
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from .util import (DISCLAIMER, http_get, http_post_json, iso_bj,
                   json_loads_loose, now_bj, redact, short_hash)
from .news import (BLOCK, GOLD_CORE, MACRO_CORE, _canon_url, _clean,
                   _hits_any, _importance, _jaccard, _parse_gdelt,
                   _parse_rss, _parse_time, _tokens, _topic,
                   parse_news_payload)
from .ai import build_payload

# ---------------------------------------------------------------- 区间解析
PRESET_DAYS = {"3m": 90, "6m": 183, "1y": 365}
PRESET_LABEL = {"3m": "近 3 个月", "6m": "近半年", "1y": "近一年", "custom": "自定义日期"}
HORIZONS = {"1-4w": "未来 2–4 周", "1-3m": "未来 1–3 个月", "3-6m": "未来 3–6 个月"}
TOPIC_LABEL = {"gold": "黄金本身", "inflation_fed": "通胀 / 美联储",
               "usd_real_rates": "美元 / 实际利率", "geopolitics": "地缘 / 避险",
               "central_bank_etf": "央行 / ETF 资金", "fx_supply": "汇率 / 供需",
               "macro": "其它宏观"}
STANCE_LABEL = {"supportive": "偏利多", "pressuring": "偏利空",
                "mixed": "混合", "unclear": "不明确"}
DIRECTIONS = {"bullish", "bearish", "neutral", "unclear"}
CONFIDENCES = {"high", "medium", "low"}
PROMPT_VERSION = "hist-v1"
BANNED = ["必涨", "必跌", "建议买入", "建议卖出", "目标价", "加仓", "减仓", "稳赚",
          "保本", "胜率", "保证收益", "一定涨", "一定跌", "预测准确率", "包赚"]


def resolve_range(preset: str | None = None, start: str | None = None,
                  end: str | None = None) -> dict:
    """返回北京时间的闭区间 [start, end]，并做合法性校验。"""
    today = now_bj().date()
    p = (preset or "6m").strip().lower()
    if p == "custom":
        try:
            s = date.fromisoformat(str(start or "").strip())
            e = date.fromisoformat(str(end or "").strip())
        except (ValueError, TypeError):
            raise ValueError("自定义日期格式必须为 YYYY-MM-DD，且起止日期都要填写")
    elif p in PRESET_DAYS:
        e = today
        s = today - timedelta(days=PRESET_DAYS[p] - 1)
    else:
        raise ValueError("不支持的时间段预设：%s" % p)
    if s > e:
        raise ValueError("开始日期不能晚于结束日期")
    if e > today:
        raise ValueError("结束日期不能晚于今天（北京时间 %s）" % today.isoformat())
    if (e - s).days > 730:
        raise ValueError("分析区间最长为 2 年，请缩短日期范围")
    return {"start": s.isoformat(), "end": e.isoformat(), "preset": p,
            "label": PRESET_LABEL.get(p, p), "days": (e - s).days + 1}


def _chunks(start: str, end: str, max_days: int = 120):
    """把长区间切成若干段，避免单次查询跨度过大。"""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    cur = s
    while cur <= e:
        nxt = min(cur + timedelta(days=max_days - 1), e)
        yield cur, nxt
        cur = nxt + timedelta(days=1)


# ---------------------------------------------------------------- 采集器
def _parse_alphavantage(text: str, src: dict) -> list[dict]:
    d = json.loads(text)
    if not isinstance(d, dict) or "feed" not in d:
        msg = str(d.get("Information") or d.get("Note") or d.get("Error Message") or "返回中没有 feed")
        low = msg.lower()
        if "premium" in low:
            raise RuntimeError("Alpha Vantage: 返回 Premium 提示（接口需 Premium 或 Key 无效），请核对 Key 或换源")
        if "rate limit" in low or "25 requests" in low or "per day" in low:
            raise RuntimeError("Alpha Vantage: 今日免费额度已用完（25 次/天）")
        if "invalid" in low and "apikey" in low:
            raise RuntimeError("Alpha Vantage: Key 无效或未生效")
        raise RuntimeError("Alpha Vantage: %s" % msg[:160])
    out = []
    for a in (d.get("feed") or []):
        title = _clean(a.get("title"))
        if not title:
            continue
        raw = str(a.get("time_published") or "")
        try:
            dt = datetime.strptime(raw, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
            date_s = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        except ValueError:
            date_s = raw
        out.append({"title": title[:240], "summary": _clean(a.get("summary"))[:300],
                    "url": _canon_url(a.get("url")), "source": a.get("source") or "Alpha Vantage",
                    "published_raw": date_s, "language": "en"})
    return out


def _fetch(url: str, parser, src: dict, timeout: float, label: str) -> dict:
    try:
        status, text, _ = http_get(url, timeout=timeout, headers=src.get("headers"))
        if status != 200 or not text.strip():
            return {"id": label, "ok": False, "items": [],
                    "detail": "HTTP %s / 空响应" % status}
        items = parser(text, src)
        return {"id": label, "ok": True, "items": items, "detail": "ok (%d 条)" % len(items)}
    except Exception as e:
        return {"id": label, "ok": False, "items": [],
                "detail": "%s: %s" % (type(e).__name__, str(e)[:150])}


def _build_tasks(cfg: dict, rng: dict, secret) -> list[tuple]:
    """返回 [(source_id, label, callable)]；所有外部源失败都不阻断其它源。"""
    hcfg = cfg.get("history_analysis", {})
    timeout = float(hcfg.get("per_source_timeout_seconds", 6))
    sources = [s for s in hcfg.get("sources", []) if s.get("enabled")]
    tasks = []
    for s in sources:
        sid, kind = s.get("id", "unknown"), s.get("kind")
        name = s.get("name") or sid
        if kind == "gdelt":
            base = s.get("url") or "http://api.gdeltproject.org/api/v2/doc/doc"
            for i, (a, b) in enumerate(_chunks(rng["start"], rng["end"])):
                q = {"query": s.get("query") or "gold", "mode": "artlist",
                     "maxrecords": str(int(s.get("maxrecords", 100))), "format": "json",
                     "startdatetime": a.strftime("%Y%m%d") + "000000",
                     "enddatetime": b.strftime("%Y%m%d") + "235959"}
                url = base + ("&" if "?" in base else "?") + urlencode(q)
                tasks.append((sid, "%s #%d" % (name, i + 1),
                              lambda u=url, sn=s, i=sid: _fetch(u, _parse_gdelt, sn, timeout, i)))
        elif kind == "googlenews":
            base = s.get("url") or "https://news.google.com/rss/search"
            for i, (a, b) in enumerate(_chunks(rng["start"], rng["end"])):
                q = "gold after:%s before:%s" % (a.isoformat(), b.isoformat())
                url = base + ("&" if "?" in base else "?") + urlencode(
                    {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})
                tasks.append((sid, "%s #%d" % (name, i + 1),
                              lambda u=url, sn=s, i=sid: _fetch(u, _parse_rss, sn, timeout, i)))
        elif kind == "alphavantage":
            key = secret("history_analysis.alpha_vantage_api_key") or \
                (os.environ.get("ALPHAVANTAGE_API_KEY") or "")
            if not key:
                tasks.append((sid, name, lambda: {"id": sid, "ok": False, "items": [],
                                                  "detail": "未录入 Key（设置页可粘贴，免费申请 20 秒）"}))
                continue
            base = s.get("url") or "https://www.alphavantage.co/query"
            for i, (a, b) in enumerate(_chunks(rng["start"], rng["end"], 120)):
                q = {"function": "NEWS_SENTIMENT",
                     "tickers": s.get("tickers") or "GLD",
                     "topics": s.get("topics") or "",
                     "apikey": key, "time_from": a.strftime("%Y%m%dT0000"),
                     "time_to": b.strftime("%Y%m%dT2359"),
                     "limit": str(int(s.get("limit", 50) or 50)), "sort": "LATEST"}
                q = {k: v for k, v in q.items() if v}
                url = base + ("&" if "?" in base else "?") + urlencode(q)
                tasks.append((sid, "%s #%d" % (name, i + 1),
                              lambda u=url, sn=s, i=sid: _fetch(u, _parse_alphavantage, sn, timeout, i)))
        elif kind == "rss":
            for ns in cfg.get("news", {}).get("sources", []):
                if not ns.get("enabled") or not ns.get("url"):
                    continue
                if "gdeltproject" in ns.get("url", ""):
                    continue
                pages = max(1, min(12, int(ns.get("pages", 1) or 1)))
                for p in range(1, pages + 1):
                    u = ns["url"]
                    if pages > 1 and "page=" in u:
                        u = u.replace("page=1", "page=%d" % p)
                    label = "%s / %s%s" % (name, ns.get("name") or ns.get("id"),
                                           "" if pages == 1 else " #%d" % p)
                    tasks.append((sid, label,
                                  lambda u=u, sn=ns, i=sid: _fetch(u, parse_news_payload, sn,
                                                                   timeout, i)))
    return tasks


def _local_items(db, rng: dict, limit: int) -> list[dict]:
    rows = db.news_between(rng["start"], rng["end"], limit)
    out = []
    for r in rows:
        d = (r.get("published_at_bj") or "")[:10] or (r.get("fetched_at") or "")[:10]
        if not (rng["start"] <= d <= rng["end"]):
            continue
        out.append({
            "title": r.get("title") or "", "summary": r.get("summary") or "",
            "url": r.get("url") or "", "source": r.get("source") or "本端",
            "published_at_local": r.get("published_at_local") or "",
            "published_at_bj": r.get("published_at_bj") or "",
            "date": d, "topic": r.get("topic") or "macro",
            "importance": r.get("importance") or 5,
            "language": r.get("language") or "zh", "origin": "本端已落库",
        })
    return out


def _sample_for_ai(news: list[dict], n: int) -> list[dict]:
    """按月分层抽样：长区间不会因为只截取最新 N 条而退化成「最近几周」。

    新闻已按日期倒序排列；逐月轮流各取一条，直到凑满 n 条。
    """
    n = max(1, int(n or 1))
    if len(news) <= n:
        return list(news)
    by_month: dict[str, list[dict]] = {}
    for it in news:
        by_month.setdefault((it.get("date") or "")[:7], []).append(it)
    months = sorted(by_month)
    picked, idx = [], 0
    while len(picked) < n:
        added = False
        for m in months:
            bucket = by_month[m]
            if idx < len(bucket):
                picked.append(bucket[idx])
                added = True
                if len(picked) >= n:
                    break
        if not added:
            break
        idx += 1
    return picked[:n]


def collect_history_news(cfg: dict, db, log, rng: dict, secret, max_items: int = 60) -> dict:
    """采集指定区间内的新闻并做去重/分类/统计。"""
    hcfg = cfg.get("history_analysis", {})
    timeout = float(hcfg.get("per_source_timeout_seconds", 6))
    deadline = float(hcfg.get("total_deadline_seconds", 12))
    max_items = max(10, min(200, int(max_items or 60)))
    status, raw = {}, []

    # 1) 本端已落库（无需网络，永远可用）
    local = _local_items(db, rng, max_items * 3)
    status["local"] = "ok (%d 条)" % len(local)
    raw.extend(local)

    # 2) 外部可插拔源：守护线程 + 硬时限，绝不因为某个源 DNS/连接卡住而拖住请求
    #    同一源先跑第 1 段，失败就不再发起该源剩余分段，减少无效网络请求
    tasks = _build_tasks(cfg, rng, secret)
    if tasks:
        by_source = {}
        for sid, label, fn in tasks:
            by_source.setdefault(sid, []).append((label, fn))
        q = queue.Queue()
        launched = {}      # label -> (sid, label)

        def worker(sid, label, fn):
            try:
                r = fn()
            except Exception as e:
                r = {"id": sid, "ok": False, "items": [],
                     "detail": "%s: %s" % (type(e).__name__, str(e)[:150])}
            q.put((sid, label, r))

        def launch(sid, label, fn):
            launched[label] = True
            threading.Thread(target=worker, args=(sid, label, fn), daemon=True).start()

        def drain(seconds):
            end = time.time() + seconds
            out = []
            while time.time() < end and len(out) < max(1, len(launched) - len(drained)):
                try:
                    out.append(q.get(timeout=0.2))
                except queue.Empty:
                    pass
            return out

        drained = set()
        results = []
        first_deadline = min(deadline * 0.6, max(4.0, timeout + 2.0))
        for sid, chunk_list in by_source.items():
            label, fn = chunk_list[0]
            launch(sid, label, fn)
        first = drain(first_deadline)
        for sid, label, r in first:
            results.append((sid, label, r))
            drained.add(label)
        for sid, chunk_list in by_source.items():
            first_label = chunk_list[0][0]
            first_res = next((r for s, l, r in first if l == first_label), None)
            if first_res is None or not first_res.get("ok"):
                for label, _ in chunk_list[1:]:
                    results.append((sid, label, {"id": sid, "ok": False, "items": [],
                                                 "detail": "首个分段失败/超时，跳过剩余分段"}))
                    drained.add(label)
                continue
            for label, fn in chunk_list[1:]:
                launch(sid, label, fn)
        rest = drain(max(0.5, deadline - first_deadline))
        for sid, label, r in rest:
            results.append((sid, label, r))
            drained.add(label)
        for label in launched:
            if label not in drained:
                sid = next((s for s, chunks in by_source.items()
                            for l, _ in chunks if l == label), "unknown")
                results.append((sid, label, {"id": sid, "ok": False, "items": [],
                                             "detail": "超过总时限，未返回"}))
        groups = {}
        for sid, label, r in results:
            if r.get("ok"):
                raw.extend(r.get("items") or [])
            # 同一子源的分页/分段合并成一条状态，避免界面出现 #1/#2/#3/#4 重复行
            base = re.sub(r" #\d+$", "", label)
            g = groups.setdefault((sid, base), {"ok": True, "count": 0, "err": []})
            if r.get("ok"):
                g["count"] += len(r.get("items") or [])
            else:
                g["ok"] = False
                d = str(r.get("detail") or "")
                if d and d not in g["err"]:
                    g["err"].append(d)
        for (sid, base), g in groups.items():
            if not isinstance(status.get(sid), list):
                status[sid] = []
            if g["ok"]:
                status[sid].append("%s：ok (%d 条)" % (base, g["count"]))
            else:
                status[sid].append("%s：%s" % (base, "；".join(g["err"][:2]) or "失败"))

    for k, v in list(status.items()):
        if isinstance(v, list):
            status[k] = " ｜ ".join(v[:6])

    # 3) 归一化、区间过滤、相关性过滤、去重
    kept, seen_urls, seen_titles, seen_hash = [], set(), [], set()
    for it in raw:
        title = _clean(it.get("title"))
        if not title:
            continue
        summary = _clean(it.get("summary"))[:300]
        text = (title + " " + summary).lower()
        if any(b in text for b in BLOCK):
            continue
        if not (_hits_any(text, GOLD_CORE) or _hits_any(text, MACRO_CORE)):
            continue
        if it.get("date"):
            d = it["date"]
            local_s = it.get("published_at_local") or ""
            bj_s = it.get("published_at_bj") or ""
        else:
            local_s, bj_s = _parse_time(it.get("published_raw"))
            d = bj_s[:10] if bj_s else ""
        if not d or not (rng["start"] <= d <= rng["end"]):
            continue
        u = _canon_url(it.get("url") or "")
        toks = _tokens(title)
        # 与新闻采集器的 dedup_hash 口径保持一致：标题先截断到 240 字再哈希，
        # 否则超长标题会在库里生成两条重复记录
        h = short_hash((u + "|" + title[:240].lower()))
        if (u and u in seen_urls) or h in seen_hash or any(_jaccard(toks, t) > 0.72 for t in seen_titles):
            continue
        if u:
            seen_urls.add(u)
        seen_hash.add(h)
        seen_titles.append(toks)
        kept.append({
            "title": title[:240], "summary": summary, "url": u,
            "source": it.get("source") or it.get("origin") or "—",
            "published_at_local": local_s, "published_at_bj": bj_s, "date": d,
            "topic": _topic(text), "importance": _importance(title, summary, "macro", 24.0),
            "language": it.get("language") or "en",
            "origin": it.get("origin") or it.get("source") or "外部源",
            "dedup_hash": h, "fetched_at": iso_bj(),
        })
    kept.sort(key=lambda x: (x["date"], x["importance"]), reverse=True)
    kept = kept[:max_items]
    # 落到本端库，逐步积累历史样本；重复标题会被 UNIQUE 约束跳过
    for it in kept:
        try:
            db.insert_news(it)
        except Exception:
            pass

    # 4) 统计
    topics, months = {}, {}
    for it in kept:
        topics[it["topic"]] = topics.get(it["topic"], 0) + 1
        m = it["date"][:7]
        months[m] = months.get(m, 0) + 1
    topic_stats = [{"topic": k, "label": TOPIC_LABEL.get(k, k), "count": v}
                   for k, v in sorted(topics.items(), key=lambda x: -x[1])]
    monthly = [{"month": m, "count": months[m]} for m in sorted(months)]
    def _ok(v) -> bool:
        s = str(v or "")
        # 单个源是 "ok (N 条)"；同一 id 下的多个子源会拼成
        # "现有 RSS 源 / CNBC Markets：ok (30 条) ｜ … / 新浪财经：ok (0 条)"
        return s.startswith("ok") or "：ok (" in s

    ok_sources = [k for k, v in status.items() if _ok(v)]
    archive_sources = [k for k in ("gdelt", "googlenews", "alphavantage")
                       if _ok(status.get(k, ""))]
    notes = []
    if not archive_sources:
        notes.append("外部历史新闻档案源本次均不可用，样本主要来自本端已落库新闻与近期 RSS")
    if not local:
        notes.append("本端在该区间没有已落库新闻（本地历史从程序首次采集开始积累）")
    if len(monthly) < 2:
        notes.append("样本跨越月份不足 2 个，时间序列代表性有限")
    notes.append("历史新闻覆盖度不等于市场全貌；媒体语气不等于价格结果")
    return {
        "range": rng, "news": kept, "total": len(kept),
        "topic_stats": topic_stats, "monthly": monthly,
        "sources": status, "ok_sources": ok_sources,
        "archive_sources_ok": bool(archive_sources),
        "coverage_note": "；".join(notes),
        "available": bool(kept), "fetched_at": iso_bj(),
        "max_items": max_items,
    }


# ---------------------------------------------------------------- AI 研判
SYSTEM_PROMPT = """你是贵金属研究助理，面向一位持有银行积存金的个人投资者做「历史新闻回顾 + 未来情景归纳」。
你的输出必须是且仅是一个 json 对象（json object），不要输出解释文字或 Markdown 围栏。

【任务边界，必须严格遵守】
1. 只依据 input 中给出的历史新闻（标题、日期、来源、主题、摘要）与统计分布做归纳。
   input 里没有的新闻、事件、机构、日期、价格、涨跌幅一律不得出现。
2. 输出的是「未来一段时间方向倾向的条件化情景」，不是价格预测；不承诺收益、保本或预测准确率，
   不给买卖点位、仓位比例、目标价，不输出确定性买卖指令。
3. input.sample_count 偏小、跨越月份不足、或 archive_sources_ok=false 时，
   必须把 direction 设为 "unclear"、confidence 设为 "low"，并在 data_gaps 写明样本/数据源缺口。
4. 历史新闻覆盖度不等于市场全貌；媒体语气不等于价格结果。必须写出 counter_case。
5. 银行积存金买卖价才是真实成交口径；XAU/USD 是国际驱动口径；折算参考价非成交价。
   不要把国际金价直接当成国内克价，不要把媒体推测当成已发生事实。

【判断框架】
- 美元指数 / 美债实际利率走强通常压制无息黄金；名义利率上行不等于实际利率上行。
- 降息预期升温通常利多，但需提示预期兑现日的反向波动。
- 地缘避险多为阶段性利多；流动性冲击下黄金也可能被抛售换现金。
- 央行购金 / ETF 持续净流入是中期支撑；单周流量不等于趋势。
- 只做趋势与条件化情景描述（若 X 延续/反转，则关注 Y），不做数值预测。

【置信度规则】
- high：样本充足（>=30 条且跨多个月）、多主题互相印证、外部历史档案源可用。
- medium：样本中等或主题集中但方向一致。
- low：样本不足、档案源不可用、主题互相矛盾或只有近期 RSS；此时 direction 必须为 "unclear"。

【禁止行为（违反即视为无效输出）】
1. 编造任何 input 之外的新闻、价格、日期、机构。
2. 输出确定性买卖指令、买卖点位、仓位比例、目标价、收益承诺、预测准确率或胜率。
3. 输出非 json 内容，或改写 disclaimer 字段。

【输出 json 样例（字段名固定，不可增减）】
{
  "as_of_bj": "2026-01-01 08:00:00+08:00",
  "period": {"start":"2026-05-26","end":"2026-11-26"},
  "forward_horizon": "1-3m",
  "direction": "unclear",
  "confidence": "low",
  "confidence_reason": "样本/源缺口说明",
  "summary_cn": "150 字以内中文归纳，必须写明这是基于历史新闻样本的情景归纳、不是价格预测",
  "key_themes": [{"theme":"inflation_fed","stance":"supportive|pressuring|mixed","note":"一句话","evidence_ref":"input 中的新闻标题或日期"}],
  "timeline": [{"month":"2026-08","tone":"supportive|pressuring|mixed|unclear","note":"一句话"}],
  "counter_case": "判断可能错在哪",
  "watch_points": ["条件化观察点"],
  "data_gaps": ["本次缺失的数据项"],
  "disclaimer": "固定免责声明，由系统覆盖，不要自行改写"
}

disclaimer 字段请原样填入下面这一行（系统会强制覆盖，确保不被改写）：
""" + DISCLAIMER


def fallback_result(rng: dict, horizon: str, collection: dict, reason: str) -> dict:
    gaps = ["AI 情景研判服务不可用：" + reason[:120]]
    if not collection.get("archive_sources_ok"):
        gaps.append("外部历史新闻档案源不可用")
    if collection.get("total", 0) < 8:
        gaps.append("区间新闻样本不足 8 条")
    gaps.append("历史新闻覆盖度不等于市场全貌")
    return {
        "as_of_bj": iso_bj(), "period": {"start": rng["start"], "end": rng["end"]},
        "forward_horizon": horizon if horizon in HORIZONS else "1-3m",
        "direction": "unclear", "confidence": "low",
        "confidence_reason": "AI 研判不可用或数据不足，按规则不输出方向结论：" + reason[:120],
        "summary_cn": "本次未生成 AI 情景研判，仅展示区间新闻采集结果；这不是价格预测，也不构成投资建议。",
        "key_themes": [], "timeline": [],
        "counter_case": "在缺少可追溯样本或 AI 服务不可用时，任何方向判断都不可靠。",
        "watch_points": ["在设置页配置 DeepSeek API Key 后重新运行",
                         "接入可用的历史新闻档案源以扩充样本"],
        "data_gaps": gaps[:6], "disclaimer": DISCLAIMER,
        "_fallback": True, "prompt_version": PROMPT_VERSION, "model": "—",
    }


def validate_result(d: dict, rng: dict, horizon: str, collection: dict,
                    min_news: int = 8) -> tuple[dict, list[str]]:
    warns = []
    out = dict(d)
    out["as_of_bj"] = iso_bj()
    out["period"] = {"start": rng["start"], "end": rng["end"]}
    out["forward_horizon"] = horizon if horizon in HORIZONS else "1-3m"
    out["disclaimer"] = DISCLAIMER
    out["direction"] = d.get("direction") if d.get("direction") in DIRECTIONS else "unclear"
    out["confidence"] = d.get("confidence") if d.get("confidence") in CONFIDENCES else "low"
    themes = []
    for x in (d.get("key_themes") or [])[:6]:
        if isinstance(x, dict):
            themes.append({"theme": str(x.get("theme", "macro"))[:40],
                           "stance": x.get("stance") if x.get("stance") in STANCE_LABEL else "mixed",
                           "note": str(x.get("note", ""))[:200],
                           "evidence_ref": str(x.get("evidence_ref", ""))[:240]})
    out["key_themes"] = themes
    timeline = []
    for x in (d.get("timeline") or [])[:12]:
        if isinstance(x, dict):
            timeline.append({"month": str(x.get("month", ""))[:7],
                             "tone": x.get("tone") if x.get("tone") in ("supportive", "pressuring", "mixed", "unclear") else "unclear",
                             "note": str(x.get("note", ""))[:160]})
    out["timeline"] = timeline
    out["counter_case"] = str(d.get("counter_case") or "未给出反向情形，已由系统补充：样本与框架变化可能使判断失效。")[:300]
    out["watch_points"] = [str(w)[:160] for w in (d.get("watch_points") or [])[:6]]
    out["confidence_reason"] = str(d.get("confidence_reason") or "—")[:240]
    out["summary_cn"] = str(d.get("summary_cn") or "本次未生成归纳。")[:240]
    out["data_gaps"] = [str(g)[:160] for g in (d.get("data_gaps") or [])[:6]]
    if not collection.get("archive_sources_ok"):
        out["data_gaps"].append("外部历史新闻档案源本次不可用，样本覆盖度有限")
    if len(collection.get("monthly") or []) < 2:
        out["data_gaps"].append("样本跨越月份不足 2 个，时间序列代表性有限")
    if out["confidence"] == "low" or collection.get("total", 0) < min_news or \
            not collection.get("ok_sources") or len(collection.get("monthly") or []) < 2:
        out["direction"] = "unclear"
        out["confidence"] = "low"
        warns.append("样本不足 / 低置信度 / 源不可用 → 强制 direction=unclear")
    blob = json.dumps(out, ensure_ascii=False)
    for w in BANNED:
        if w in blob:
            out["direction"] = "unclear"
            out["confidence"] = "low"
            out["summary_cn"] = "输出命中确定性指令禁词，已降级为不明确；仅保留数据展示。"
            out["key_themes"] = []
            warns.append("命中禁词 %s → 降级" % w)
            break
    return out, warns


class PeriodAnalyzer:
    """时段新闻 → DeepSeek 结构化情景研判。与常规研判共用同一预算与密钥。"""

    def __init__(self, cfg_store, db, logger):
        self.cfg = cfg_store
        self.db = db
        self.log = logger

    def _budget_ok(self) -> tuple[bool, str]:
        ds = self.cfg.get()["deepseek"]
        u = self.db.usage_today()
        if u["calls"] >= ds["max_calls_per_day"]:
            return False, "本端今日调用次数已达上限 %d" % ds["max_calls_per_day"]
        used = (u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0)
        if used >= ds["daily_token_budget"]:
            return False, "本端今日 token 已达预算 %d" % ds["daily_token_budget"]
        return True, ""

    def analyze(self, rng: dict, collection: dict, horizon: str = "1-3m",
                force: bool = False) -> dict:
        cfg = self.cfg.get()
        hcfg = cfg.get("history_analysis", {})
        min_news = int(hcfg.get("min_news_for_direction", 8))
        horizon = horizon if horizon in HORIZONS else "1-3m"
        ds = cfg["deepseek"]
        key = self.cfg.secret("deepseek.api_key")
        if not key:
            r = fallback_result(rng, horizon, collection, "未配置 DeepSeek API Key")
            return {"available": False, "reason": "未配置 DeepSeek API Key", "result": r}
        ok, why = self._budget_ok()
        if not ok and not force:
            r = fallback_result(rng, horizon, collection, why)
            return {"available": False, "reason": why, "result": r}
        ai_news = [{"date": n["date"], "title": n["title"], "source": n["source"],
                    "topic": n["topic"], "url": n["url"], "summary": n["summary"][:120]}
                   for n in _sample_for_ai(collection["news"],
                                           int(hcfg.get("max_ai_items", 40)))]
        user_payload = {
            "period": {"start": rng["start"], "end": rng["end"], "days": rng["days"],
                       "preset": rng["preset"]},
            "forward_horizon": horizon,
            "forward_horizon_label": HORIZONS[horizon],
            "sample_count": collection["total"],
            "archive_sources_ok": collection["archive_sources_ok"],
            "source_status": collection["sources"],
            "topic_stats": collection["topic_stats"],
            "monthly_counts": collection["monthly"],
            "coverage_note": collection["coverage_note"],
            "news": ai_news,
            "latest_price_context": None,
        }
        xau = self.db.last_price("XAU/USD", 72)
        if xau:
            user_payload["latest_price_context"] = {
                "symbol": "XAU/USD", "price": xau.get("price"), "unit": xau.get("unit"),
                "source": xau.get("source"), "source_ts": xau.get("source_ts"),
                "fetched_at": xau.get("fetched_at"),
                "note": "仅作当前位置参考，不是区间历史价格，不得据此推算区间涨跌"}
        payload = build_payload(ds, ds["model_pro"], [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "基于以下 input 生成 json 情景研判；"
                                        "只依据 input 中给出的事实，不要补充任何未提供的数据。\ninput = " +
             json.dumps(user_payload, ensure_ascii=False)}],
            max(1600, int(ds.get("max_tokens", 1200))))
        base = ds["base_url"].rstrip("/")
        headers = {"Authorization": "Bearer " + key}
        last_err, attempts = "", int(ds.get("retry_times", 2)) + 1
        cur_max = max(1600, int(ds.get("max_tokens", 1200)))
        for i in range(attempts):
            payload["max_tokens"] = cur_max
            try:
                status, body = http_post_json(base + "/chat/completions", payload, headers,
                                              ds["timeout_seconds"])
                if status != 200:
                    last_err = "HTTP %s %s" % (status, redact(body[:200], [key]))
                    if status in (429, 500, 502, 503, 504) and i < attempts - 1:
                        time.sleep(1.5 * (2 ** i))
                        continue
                    break
                d = json.loads(body)
                usage = d.get("usage") or {}
                self.db.add_usage(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
                choices = d.get("choices") or []
                msg = (choices[0].get("message") or {}) if choices else {}
                finish = (choices[0].get("finish_reason") or "") if choices else ""
                raw = msg.get("content") or ""
                think = msg.get("reasoning_content") or ""
                if not raw.strip():
                    last_err = "空 content（finish=%s，思考 %d 字）" % (finish or "—", len(think))
                    if cur_max < 6144 and i < attempts - 1:
                        cur_max = min(6144, cur_max * 2)
                        last_err += "，已把 max_tokens 提高到 %d 重试" % cur_max
                        continue
                    if i < attempts - 1:
                        time.sleep(1.0)
                        continue
                    break
                try:
                    parsed = json_loads_loose(raw)
                except Exception as je:
                    last_err = "返回无法解析为 json（finish=%s，content %d 字）：%s" % (
                        finish or "—", len(raw), str(je)[:80])
                    if finish == "length" and cur_max < 6144 and i < attempts - 1:
                        cur_max = min(6144, cur_max * 2)
                        last_err += "，已把 max_tokens 提高到 %d 重试" % cur_max
                        continue
                    if i < attempts - 1:
                        time.sleep(1.0)
                        continue
                    break
                result, warns = validate_result(parsed, rng, horizon, collection, min_news)
                result.update({"prompt_version": PROMPT_VERSION, "model": payload["model"],
                               "prompt_tokens": usage.get("prompt_tokens", 0),
                               "completion_tokens": usage.get("completion_tokens", 0)})
                for w in warns:
                    self.log.warning("时段研判输出校验：%s", w)
                self.db.event("info", "时段研判完成 %s~%s 样本=%d direction=%s" %
                              (rng["start"], rng["end"], collection["total"], result["direction"]))
                return {"available": True, "result": result, "warnings": warns,
                        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                                  "completion_tokens": usage.get("completion_tokens", 0)}}
            except Exception as e:
                last_err = redact("%s: %s" % (type(e).__name__, str(e)[:200]), [key])
                if i < attempts - 1:
                    time.sleep(1.0 * (2 ** i))
                    continue
        reason = "DeepSeek 调用失败：" + (last_err or "未知原因")
        self.log.warning(redact(reason, [key]))
        self.db.event("warn", redact(reason, [key])[:200])
        r = fallback_result(rng, horizon, collection, redact(reason, [key]))
        return {"available": False, "reason": redact(reason, [key]), "result": r}
