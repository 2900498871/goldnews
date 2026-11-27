# -*- coding: utf-8 -*-
"""金价采集：可插拔适配器 + 多源交叉校验 + 陈旧/缓存/分歧降级（FR-01 / PRD §3.2）。

严格区分五种口径。银行积存金与 Au99.99 默认未接入，只有用户自备接口才启用，
绝不用折算价或国际金价冒充银行成交价。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from .util import (OZ_TO_GRAM, bj_str, http_get, iso_bj, now_bj, now_utc,
                   safe_float, short_hash, to_bj)

SYMBOL_META = {
    "BANK_ACCUMULATION": {"name": "银行积存金买入价", "unit": "CNY/g", "role": "主口径（真实成交）"},
    "Au99.99": {"name": "国内实时金价（上金所 Au99.99）", "unit": "CNY/g",
                "role": "国内金价参考（非银行成交价）"},
    "XAU/USD": {"name": "国际现货黄金", "unit": "USD/oz", "role": "驱动口径"},
    "USD/CNY": {"name": "美元兑人民币", "unit": "CNY/USD", "role": "折算用汇率"},
    "FOLDED_CNY_G": {"name": "折算参考价", "unit": "CNY/g", "role": "参考，非成交价"},
}


def _parse_ts(value) -> str:
    """把源自带的时间统一转成北京时间 ISO；无法解析则原样返回并注明。"""
    if not value:
        return ""
    s = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}Z", s):
            return bj_str(to_bj(datetime.fromisoformat(s.replace("Z", "+00:00"))))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+\-]\d{2}:?\d{2})?", s):
            return bj_str(to_bj(datetime.fromisoformat(s.replace("Z", "+00:00"))))
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                return bj_str(to_bj(dt))
            except ValueError:
                continue
    except Exception:
        pass
    return s


def _parse_sina_time(date_s: str, time_s: str) -> str:
    """新浪 hf_ 行情自带日期+时间字段，按源提供值原样带时区说明返回。"""
    if date_s and time_s:
        return "%s %s（源提供，按北京时间展示）" % (date_s, time_s)
    return date_s or ""


class BaseAdapter:
    def fetch(self, src: dict) -> dict:
        raise NotImplementedError


class GoldApiAdapter(BaseAdapter):
    def fetch(self, src: dict) -> dict:
        status, text, _ = http_get(src["url"], timeout=src.get("timeout", 10))
        if status != 200:
            raise RuntimeError("HTTP %s" % status)
        d = json.loads(text)
        price = safe_float(d.get("price"))
        if price is None:
            raise RuntimeError("响应缺少 price 字段")
        return {"price": price, "source_ts": _parse_ts(d.get("updatedAt")),
                "unit": "USD/oz", "symbol": "XAU/USD"}


class SinaHfAdapter(BaseAdapter):
    def fetch(self, src: dict) -> dict:
        status, text, _ = http_get(src["url"], timeout=src.get("timeout", 10),
                                   headers={"Referer": "https://finance.sina.com.cn"},
                                   encoding="gbk")
        if status != 200:
            raise RuntimeError("HTTP %s" % status)
        m = re.search(r'="([^"]*)"', text)
        if not m:
            raise RuntimeError("响应格式无法解析")
        f = m.group(1).split(",")
        price = safe_float(f[0]) if f else None
        if price is None or len(f) < 13:
            raise RuntimeError("字段不足或价格为空")
        return {"price": price, "source_ts": _parse_sina_time(f[12], f[6]),
                "unit": "USD/oz", "symbol": "XAU/USD",
                "source_ts_note": "新浪返回的是源自带日期+时间字符串，未做时区二次换算"}


class SinaSgeAdapter(BaseAdapter):
    """新浪上金所行情（gds_AU9999 = 沪金99 现货）。

    字段：最新价,?,买价,卖价,最高,最低,时间,昨收,开盘,成交量,?,?,日期,名称
    报价单位 元/克；时间按源提供值（北京时间）原样带出，供陈旧检测使用。
    """

    def fetch(self, src: dict) -> dict:
        status, text, _ = http_get(src["url"], timeout=src.get("timeout", 10),
                                   headers={"Referer": "https://finance.sina.com.cn"},
                                   encoding="gbk")
        if status != 200:
            raise RuntimeError("HTTP %s" % status)
        m = re.search(r'="([^"]*)"', text)
        f = m.group(1).split(",") if m else []
        price = safe_float(f[0]) if f else None
        if price is None or price <= 0 or len(f) < 13:
            raise RuntimeError("字段不足或价格为空")
        return {"price": price, "source_ts": "%s %s" % (f[12], f[6]),
                "unit": src.get("unit", "CNY/g"), "symbol": src.get("symbol", "Au99.99"),
                "bid": safe_float(f[2]), "ask": safe_float(f[3]),
                "day_high": safe_float(f[4]), "day_low": safe_float(f[5]),
                "prev_close": safe_float(f[7])}


class FxErAdapter(BaseAdapter):
    def fetch(self, src: dict) -> dict:
        status, text, _ = http_get(src["url"], timeout=src.get("timeout", 10))
        if status != 200:
            raise RuntimeError("HTTP %s" % status)
        d = json.loads(text)
        rate = safe_float((d.get("rates") or {}).get("CNY"))
        if rate is None:
            raise RuntimeError("响应缺少 rates.CNY")
        return {"price": rate, "source_ts": _parse_ts(d.get("time_last_update_utc") or d.get("time_last_update_unix")),
                "unit": "CNY/USD", "symbol": "USD/CNY"}


class GenericJsonAdapter(BaseAdapter):
    """用户自备接口：url + field（支持 a.b.c 路径）+ 可选 ts_field。"""
    def fetch(self, src: dict) -> dict:
        if not src.get("url"):
            raise RuntimeError("未配置 URL")
        status, text, _ = http_get(src["url"], timeout=src.get("timeout", 10),
                                   headers=src.get("headers") or {})
        if status != 200:
            raise RuntimeError("HTTP %s" % status)
        d = json.loads(text)
        cur = d
        for part in str(src.get("field", "price")).split("."):
            cur = cur[part] if isinstance(cur, dict) else None
            if cur is None:
                raise RuntimeError("字段路径 %s 未命中" % src.get("field"))
        price = safe_float(cur)
        if price is None:
            raise RuntimeError("字段值不是数字")
        ts = ""
        for k in (src.get("ts_field"), "updatedAt", "timestamp", "time", "quote_time"):
            if k and isinstance(d, dict) and d.get(k):
                ts = _parse_ts(d.get(k))
                break
        return {"price": price, "source_ts": ts or "源未提供报价时间",
                "unit": src.get("unit", "CNY/g"), "symbol": src.get("symbol", "CUSTOM"),
                "quality_flag": "ok" if ts else "unknown_ts"}


class MockAdapter(BaseAdapter):
    """离线自检用，绝不用于真实推送。"""
    def fetch(self, src: dict) -> dict:
        return {"price": 3573.6 if src.get("symbol") == "XAU/USD" else 7.1,
                "source_ts": iso_bj(), "unit": "USD/oz" if src.get("symbol") == "XAU/USD" else "CNY/USD",
                "symbol": src.get("symbol", "XAU/USD"), "quality_flag": "mock"}


ADAPTERS = {"gold_api": GoldApiAdapter, "sina_hf": SinaHfAdapter, "sina_sge": SinaSgeAdapter,
            "fx_er": FxErAdapter, "generic_json": GenericJsonAdapter, "mock": MockAdapter}


def market_status(dt=None) -> str:
    d = dt or now_bj()
    wd = d.weekday()
    hm = d.hour * 60 + d.minute
    if wd >= 5:
        return "closed"
    if 9 * 60 <= hm <= 15 * 60 + 30 or 20 * 60 <= hm <= 23 * 60 + 59 or hm <= 2 * 60 + 30:
        return "open"
    return "pre_open"


class PriceCollector:
    def __init__(self, cfg_store, db, logger):
        self.cfg_store = cfg_store
        self.db = db
        self.log = logger

    def _one(self, src: dict) -> tuple[str, dict | None, str]:
        sid = src.get("id", "unknown")
        try:
            cls = ADAPTERS.get(src.get("kind"))
            if not cls:
                raise RuntimeError("未知适配器类型 %s" % src.get("kind"))
            r = cls().fetch(src)
            r.update({"id": sid, "source": src.get("name") or sid,
                      "name": SYMBOL_META.get(r.get("symbol", src.get("symbol")), {}).get("name", src.get("name")),
                      "role": SYMBOL_META.get(r.get("symbol", src.get("symbol")), {}).get("role", "自定义口径"),
                      "quality_flag": r.get("quality_flag", "ok"),
                      "fetched_at": iso_bj(), "change_pct": None, "change_base": "prev_snapshot"})
            # 源自带的日内高低/买卖价只用于展示，不参与口径选择
            r["extra"] = {k: r.pop(k) for k in ("bid", "ask", "day_high", "day_low", "prev_close") if k in r}
            return sid, r, "ok"
        except Exception as e:
            return sid, None, str(e)[:180]

    def collect(self, trigger: str = "scheduled", force: bool = False) -> dict:
        cfg = self.cfg_store.get()
        sources = [s for s in cfg.get("price_sources", []) if s.get("enabled")]
        results, status = [], {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(sources)))) as ex:
            futs = {ex.submit(self._one, s): s for s in sources}
            for f in as_completed(futs):
                sid, r, st = f.result()
                status[sid] = st
                if r:
                    results.append(r)
        notes = []
        # 同一 symbol 多源：>容差则并列并标记源间分歧，不静默挑选
        grouped: dict[str, list] = {}
        for r in results:
            grouped.setdefault(r["symbol"], []).append(r)
        tol = float(cfg.get("divergence_tolerance_pct", 1.0))
        chosen = []
        for sym, group in grouped.items():
            group.sort(key=lambda x: next((s.get("priority", 9) for s in sources if s.get("id") == x["id"]), 9))
            good = [g for g in group if g["price"] and g["price"] > 0]
            if len(good) >= 2:
                lo, hi = min(g["price"] for g in good), max(g["price"] for g in good)
                if lo > 0 and (hi - lo) / lo * 100 > tol:
                    notes.append("源间分歧：%s 两源报价 %s / %s，差异 %.2f%%（并列展示，未取单值）"
                                 % (sym, good[0]["price"], good[1]["price"], (hi - lo) / lo * 100))
                    good[0]["quality_flag"] = "divergent"
                    good[0]["divergent_values"] = [
                        {"source": g["source"], "price": g["price"], "source_ts": g["source_ts"]} for g in good]
            if good:
                chosen.append(good[0])
            if len(good) > 1:
                for extra in good[1:]:
                    chosen.append(dict(extra, alternate=True))
        # 陈旧检测：行情用分钟级阈值；USD/CNY 是日更汇率，用独立宽阈值
        stale_min = int(cfg.get("stale_after_minutes", 45))
        fx_stale_min = int(cfg.get("stale_after_minutes_fx", 2880))
        for r in chosen:
            limit = fx_stale_min if r["symbol"] == "USD/CNY" else stale_min
            ts = str(r.get("source_ts") or "")
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", ts)
            if m:
                try:
                    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=now_bj().tzinfo)
                    if (now_bj() - dt).total_seconds() > limit * 60:
                        r["quality_flag"] = "stale"
                        notes.append("陈旧数据：%s 源报价时间 %s 已超过 %d 分钟，不参与 AI 判断"
                                     % (r["symbol"], m.group(1), limit))
                except ValueError:
                    pass
        # 缺失回退最近成功缓存值，明确标注。
        # 不依赖配置项里是否写了 symbol 字段：只要本轮有源失败（或全空），
        # 且本端库里存在该口径的历史成功值，就回退并显式标注（FR-09）。
        have = {r["symbol"] for r in chosen}
        attempted = {s.get("symbol") for s in sources if s.get("symbol")}
        has_failure = (not results) or any(v != "ok" for v in status.values())
        for sym in sorted(attempted | {"XAU/USD", "USD/CNY", "Au99.99", "BANK_ACCUMULATION"}):
            if sym in have or not has_failure:
                continue
            last = self.db.last_price(sym)
            if not last:
                continue
            chosen.append({"id": "cache", "symbol": sym, "name": last.get("name"),
                           "price": last.get("price"), "unit": last.get("unit"),
                           "source": (last.get("source") or "缓存") + "（缓存值）",
                           "source_ts": last.get("source_ts"), "fetched_at": iso_bj(),
                           "quality_flag": "cached", "change_pct": None,
                           "change_base": "prev_snapshot", "role": SYMBOL_META.get(sym, {}).get("role", "")})
            notes.append("缓存值：%s 本次数据源失败，展示最近成功值，可能滞后" % sym)
            status[sym] = "数据源失败，已回退缓存"
        if not chosen:
            notes.append("本次价格数据不可用（所有数据源均失败且无缓存）")
        # 涨跌幅：与本地上一快照比（同口径、同来源的主口径行；并列源另存不参与）
        by_sym = {}
        for r in chosen:
            if r.get("alternate"):
                continue
            prev = self.db.last_price(r["symbol"])
            if prev and prev.get("price") and r.get("price"):
                r["change_pct"] = round((r["price"] - prev["price"]) / prev["price"] * 100, 3)
                base_src = re.sub(r"（缓存值）$", "", str(prev.get("source") or ""))
                if base_src and base_src != str(r.get("source") or ""):
                    # 主口径源切换过（如某源失败），必须如实标注这是跨源比较
                    r["change_base"] = "prev_snapshot（上一条基准来自 %s，与本次来源不同，属跨源比较）" % base_src
                else:
                    r["change_base"] = "prev_snapshot（本端上一成功快照，同口径同来源）"
            by_sym[r["symbol"]] = r
        # 折算参考价（单独标注，非成交价）
        folded = None
        xau, fx = by_sym.get("XAU/USD"), by_sym.get("USD/CNY")
        if xau and fx and xau.get("quality_flag") != "stale" and fx.get("quality_flag") != "stale":
            folded = round(xau["price"] / OZ_TO_GRAM * fx["price"], 2)
            chosen.append({
                "id": "folded", "symbol": "FOLDED_CNY_G", "name": "折算参考价（非银行成交价）",
                "price": folded, "unit": "CNY/g", "source": "由 XAU/USD ÷ 31.1034768 × USD/CNY 计算",
                "source_ts": "同 XAU/USD 与 USD/CNY 源报价时间", "fetched_at": iso_bj(),
                "quality_flag": "derived", "change_pct": None, "change_base": "不适用（派生值）",
                "role": "参考，非成交价",
            })
        # 落库（不存派生值以外的一切都存）
        for r in chosen:
            self.db.insert_price(dict(r, trigger_source=trigger))
        snap = {
            "as_of_bj": iso_bj(), "market_status": market_status(),
            "prices": chosen, "fx": {"usd_cny": fx["price"] if fx else None,
                                     "source": fx["source"] if fx else None,
                                     "source_ts": fx["source_ts"] if fx else None},
            "folded_cny_g": folded, "quality_notes": notes, "source_status": status,
            "trigger_source": trigger, "snapshot_hash": short_hash(
                [(r["symbol"], r["price"], r["source"]) for r in chosen if not r.get("alternate")]),
        }
        return snap
