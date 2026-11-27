# -*- coding: utf-8 -*-
"""到价 / 涨跌幅 / MA20 条件提醒。"""
from __future__ import annotations

TYPE_CN = {
    "price_above": "价格高于", "price_below": "价格低于",
    "pct_up": "涨幅达到", "pct_down": "跌幅达到",
    "ma20_above": "价格站上 MA20", "ma20_below": "价格跌破 MA20",
}


def _price_of(prices: list[dict], symbol: str):
    best = None
    for p in prices or []:
        if p.get("symbol") != symbol:
            continue
        try:
            price = float(p.get("price"))
        except (TypeError, ValueError):
            continue
        if p.get("quality_flag") in ("mock", "divergent"):
            continue
        if best is None or (p.get("quality_flag") == "ok" and best.get("quality_flag") != "ok"):
            best = {"price": price, **p}
    if best is None:
        return None, None
    return best["price"], best


def evaluate(cfg: dict, db, prices: list[dict]) -> list[dict]:
    """返回命中的规则列表；同一轮多条命中只汇总一次提醒。"""
    al = cfg.get("alerts") or {}
    if not al.get("enabled", True):
        return []
    hits = []
    for rule in al.get("rules") or []:
        if not rule.get("enabled", True):
            continue
        symbol = rule.get("symbol") or "Au99.99"
        price, _p = _price_of(prices, symbol)
        if price is None:
            continue
        typ = rule.get("type")
        try:
            val = float(rule.get("value"))
        except (TypeError, ValueError):
            continue
        hit = False
        base = ""
        if typ == "price_above":
            hit = price >= val
        elif typ == "price_below":
            hit = price <= val
        elif typ == "pct_up":
            ch = (_p.get("change_pct") if _p else None)
            hit = ch is not None and float(ch) >= abs(val)
            base = "涨跌幅 %s%%" % ch if ch is not None else ""
        elif typ == "pct_down":
            ch = (_p.get("change_pct") if _p else None)
            hit = ch is not None and float(ch) <= -abs(val)
            base = "涨跌幅 %s%%" % ch if ch is not None else ""
        elif typ in ("ma20_above", "ma20_below"):
            hist = db.price_history(symbol, 20) if db else []
            if len(hist) >= 20:
                ma = sum(float(x["price"]) for x in hist[-20:]) / 20.0
                hit = price >= ma if typ == "ma20_above" else price <= ma
                base = "MA20 %.2f" % ma
        if hit:
            hits.append({"rule": rule, "symbol": symbol, "price": price,
                         "text": "%s %s（现价 %.2f%s）" % (
                             TYPE_CN.get(typ, typ), val, price,
                             "，" + base if base else "")})
    return hits
