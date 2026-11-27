# -*- coding: utf-8 -*-
"""金价历史走势（总览「金价详细走势」用）。

国内序列：
  * 今日分时 —— 上金所官网 graph/quotations（Au99.99，元/克，分钟线，含夜盘）
  * 日 K     —— 新浪财经 沪金主力 AU0 日 K（元/克）；失败时退到东方财富 Au(T+D)
国际序列：
  * 日 K     —— 新浪 GlobalFuturesDailyKLine（XAU，美元/金衡盎司，2006 年至今）
所有数据带来源与抓取时间，接口失败时如实返回错误，不补数、不平滑。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import date, datetime, timedelta

from .util import http_get, iso_bj, now_bj, safe_float

PERIOD_DAYS = {"1d": 1, "1m": 31, "3m": 92, "6m": 183, "1y": 365, "5y": 365 * 5}
PERIOD_LABEL = {"1d": "今日分时", "1m": "近 1 月", "3m": "近 3 月", "6m": "近半年", "1y": "近 1 年", "5y": "近 5 年"}
SYMBOL_LABEL = {"domestic": "国内金价", "xau": "国际金价"}
UNIT = {"domestic": "CNY/g", "xau": "USD/oz"}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_LOCK = threading.Lock()
_CACHE: dict = {}          # key -> (expire_ts, payload)


def _cache(key: str, ttl: int, fn):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    data = fn()
    if data.get("ok"):
        with _LOCK:
            _CACHE[key] = (now + ttl, data)
    return data


def _stats(series: list[dict]) -> dict:
    if not series:
        return {}
    closes = [s["c"] for s in series]
    first, last = closes[0], closes[-1]
    highs = [s.get("h") or s["c"] for s in series]
    lows = [s.get("l") or s["c"] for s in series]
    return {"first": first, "last": last,
            "chg_pct": round((last - first) / first * 100, 3) if first else None,
            "high": max(highs), "low": min(lows),
            "avg": round(sum(closes) / len(closes), 3),
            "from": series[0]["t"], "to": series[-1]["t"], "points": len(series)}


def _sge_intraday() -> dict:
    """上金所官网分时：{"times":[...],"data":[...],"min":..,"max":..,"heyue":..}"""
    body = "instid=Au99.99"
    status, text, _ = http_get(
        "https://www.sge.com.cn/graph/quotations", timeout=10, data=body,
        headers={"User-Agent": UA, "Referer": "https://www.sge.com.cn/sjzx/yshqbg",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "X-Requested-With": "XMLHttpRequest"})
    if status != 200:
        raise RuntimeError("上金所接口 HTTP %s" % status)
    d = json.loads(text)
    times, data = d.get("times") or [], d.get("data") or []
    series = []
    for t, v in zip(times, data):
        p = safe_float(v)
        if p:
            series.append({"t": str(t), "c": p})
    if not series:
        raise RuntimeError("上金所分时无数据（可能休市）")
    return {"series": series, "source": "上海黄金交易所官网分时（Au99.99）",
            "note": "包含夜盘与日盘分钟线，时间为北京时间"}


def _sina_au0_daily(days: int) -> dict:
    """新浪财经 沪金主力（AU0）日 K：d/o/h/l/c/v/p/s，2008 年至今，稳定 <0.5s。"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_AU0=/"
           "InnerFuturesNewService.getDailyKLine?symbol=AU0")
    status, text, _ = http_get(url, timeout=12,
                               headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
    if status != 200:
        raise RuntimeError("新浪接口 HTTP %s" % status)
    m = re.search(r"=\((.*)\);?\s*$", text, re.S)
    rows = json.loads(m.group(1)) if m else []
    if isinstance(rows, dict):
        raise RuntimeError("新浪沪金日 K: %s" % rows.get("__ERRORMSG", "无数据"))
    series = []
    for r in rows or []:
        o, c = safe_float(r.get("o")), safe_float(r.get("c"))
        if not c:
            continue
        series.append({"t": str(r.get("d")), "o": o, "c": c,
                       "h": safe_float(r.get("h")), "l": safe_float(r.get("l")),
                       "v": safe_float(r.get("v"))})
    if not series:
        raise RuntimeError("新浪沪金日 K 无数据")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    series = [s for s in series if s["t"] >= cutoff] or series[-1:]
    return {"series": series, "source": "新浪财经 沪金主力 AU0 日 K",
            "note": "国内金价走势用沪金主力合约日 K 代表，与上金所 Au99.99 现货价相差数元；今日一行为最新价"}


def _em_daily(days: int) -> dict:
    """东方财富 沪金 Au(T+D) 日 K（备选源：连续请求时会被远端断连）。"""
    beg = (date.today() - timedelta(days=int(days * 1.6) + 15)).strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=118.AUTD"
           "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=0"
           "&beg=%s&end=20500101" % beg)
    # 东方财富对「不接受压缩」的请求直接断连，必须允许 gzip
    status, text, _ = http_get(url, timeout=10,
                               headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
                                        "Accept-Encoding": "gzip"})
    if status != 200:
        raise RuntimeError("东方财富接口 HTTP %s" % status)
    d = (json.loads(text).get("data") or {})
    kl = d.get("klines") or []
    series = []
    for line in kl:
        f = line.split(",")
        if len(f) < 6:
            continue
        o, c, h, l = safe_float(f[1]), safe_float(f[2]), safe_float(f[3]), safe_float(f[4])
        if c:
            series.append({"t": f[0], "o": o, "c": c, "h": h, "l": l, "v": safe_float(f[5])})
    if not series:
        raise RuntimeError("东方财富日 K 无数据")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    series = [s for s in series if s["t"] >= cutoff] or series[-1:]
    return {"series": series, "source": "东方财富 沪金 Au(T+D) 日 K（主力连续）",
            "note": "国内金价走势用沪金 Au(T+D) 日 K 代表，与上金所 Au99.99 现货相差数元"}


def _sina_xau_daily(days: int) -> dict:
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_XAU=/"
           "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=XAU")
    status, text, _ = http_get(url, timeout=12,
                               headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn"})
    if status != 200:
        raise RuntimeError("新浪接口 HTTP %s" % status)
    m = re.search(r"=\((.*)\);?\s*$", text, re.S)
    rows = json.loads(m.group(1)) if m else []
    series = []
    for r in rows:
        o, c = safe_float(r.get("open")), safe_float(r.get("close"))
        if not c:
            continue
        series.append({"t": str(r.get("date")), "o": o, "c": c,
                       "h": safe_float(r.get("high")), "l": safe_float(r.get("low")),
                       "v": safe_float(r.get("volume"))})
    if not series:
        raise RuntimeError("新浪国际金价日 K 无数据")
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    series = [s for s in series if s["t"] >= cutoff] or series[-1:]
    return {"series": series, "source": "新浪财经 国际现货黄金 XAU 日 K",
            "note": "单位 美元/金衡盎司"}


def gold_history(symbol: str, period: str) -> dict:
    """symbol: domestic | xau；period: 1d | 1m | 3m | 6m | 1y | 5y。"""
    symbol = symbol if symbol in SYMBOL_LABEL else "domestic"
    period = period if period in PERIOD_DAYS else "6m"
    days = PERIOD_DAYS[period]

    def load():
        try:
            if period == "1d":
                if symbol == "xau":
                    # 国际金价没有稳定的免费分时源，今日用最近 7 天日 K 代替并如实标注
                    r = _sina_xau_daily(7)
                    r["note"] += "；国际金价无稳定免费分时源，此处展示近 7 日日 K"
                else:
                    r = _sge_intraday()
            elif symbol == "domestic":
                try:
                    r = _sina_au0_daily(days)
                except Exception:
                    r = _em_daily(days)
            else:
                r = _sina_xau_daily(days)
            return {"ok": True, "symbol": symbol, "period": period, "unit": UNIT[symbol],
                    "title": SYMBOL_LABEL[symbol] + " · " + PERIOD_LABEL[period],
                    "source": r["source"], "note": r.get("note", ""),
                    "series": r["series"], "stats": _stats(r["series"]), "fetched_at": iso_bj()}
        except Exception as e:
            return {"ok": False, "symbol": symbol, "period": period,
                    "message": "%s: %s" % (type(e).__name__, str(e)[:160]), "fetched_at": iso_bj()}

    ttl = 60 if period == "1d" else 1800
    return _cache("gh:%s:%s" % (symbol, period), ttl, load)
