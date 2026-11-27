# -*- coding: utf-8 -*-
"""DeepSeek 结构化研判（FR-03 / FR-15 / PRD §6）。

固定 JSON Schema、禁止清单、证据引用、低置信度输出 unclear、
空 content 兜底重试、按实例独立计数与预算降级。
"""
from __future__ import annotations

import json
import re
import time

from .goldhist import gold_history
from .util import DISCLAIMER, http_post_json, iso_bj, json_loads_loose, redact

PROMPT_VERSION = "v4"
DIRECTIONS = {"bullish", "bearish", "neutral", "unclear"}
CONFIDENCES = {"high", "medium", "low"}
HORIZONS = {"intraday", "1-3d", "1-2w"}
SHORT_HORIZONS = {"intraday", "1-3d", "1-2w"}
LONG_HORIZONS = {"6-12m", "1-2y", "3y+"}
FACTORS = {"usd_real_rates", "inflation_fed", "geopolitics", "central_bank_etf", "fx_supply", "macro"}
# v4 起放开「条件化投资建议」用词（用户明确要求给短期/长期投资建议）；
# 仍然禁止收益承诺、确定性断言、目标价与准确率类表述，命中即整体降级。
BANNED = ["必涨", "必跌", "必定", "一定涨", "一定跌", "稳赚", "保本", "胜率", "保证收益",
          "目标价", "无风险", "只赚不赔", "稳赚不赔"]
# 具体仓位百分比属于仓位指令，不允许出现（如 "30%仓位"、"仓位 20%"）
_POSITION_RE = re.compile(r"(\d+(?:\.\d+)?%\s*(?:的)?\s*(?:仓位|仓\b))|(仓位(?:比例)?[^，。；;\n]{0,10}\d+(?:\.\d+)?%)")
_SAFE_SHORT_ADVICE = "数据或置信度不足，暂不做单笔追入或集中操作；可维持原有积存节奏，等价格与消息面补齐后再研判。"
_SAFE_LONG_ADVICE = "长期按既定积存计划分批、定期进行，不以短期波动改变节奏；先留足应急资金，不加杠杆。"

SYSTEM_PROMPT = """你是贵金属研究助理，面向一位持有银行积存金的个人投资者做信息归纳、走势预判与条件化投资建议。
你的输出必须是且仅是一个 json 对象（json object），不要输出任何解释文字或 Markdown 围栏。

【口径定义，必须严格遵守】
1. 银行积存金买入价/卖出价（元/克）才是用户真实成交口径；上金所 Au99.99（元/克）是国内金价参考口径（交易所现货，非银行成交价），input.prices 中 symbol=Au99.99 的那一项就是它。
2. XAU/USD（美元/金衡盎司）是国际驱动口径；由它乘汇率得到的"折算参考价"仅作参考，非成交价。
3. 国内克价上涨不等于国际金价上涨，必须先拆 USD/CNY 汇率因子。禁止用国际金价直接解释国内克价。
4. 所有价格、涨跌幅、新闻事件、机构名、发布日期只能来自 input 中给出的字段（含 input.trend）。input 里没有的一律不得出现。
5. input 中 news_available=false 时，不得补充任何背景事件，必须在 data_gaps 写明"本次新闻数据缺失"。

【判断框架】
- 美元指数/美债实际利率走强通常压制无息黄金；注意名义利率上行不等于实际利率上行。
- 降息预期升温通常利多，但需提示"buy the rumor, sell the news"的兑现日反向波动。
- 地缘避险多为阶段性利多；流动性冲击下黄金也可能被抛售换现金。
- 央行购金/ETF 持续净流入是中期支撑，单周流量不等于趋势。
- 技术面只做趋势与位置描述，不给买卖点位。
- 单日大跌不等于趋势反转；盘中插针与假期清淡时段易失真。

【走势预判（v4 必填，放在输出最前面）】
- outlook.short：短期走向，horizon 取 intraday / 1-3d / 1-2w 之一；view 用 1–2 句话说清方向、依据（引用 input.trend 的区间涨跌与 input.news 的事件）和关键条件。
- outlook.long：长期走向，horizon 取 6-12m / 1-2y / 3y+ 之一；view 从实际利率周期、美元信用、央行购金、国内溢价与汇率等中期因子归纳。
- direction / horizon / confidence 描述的是短期走向（与 outlook.short 一致）。
- 方向与措辞必须一致：view 里若写了"偏弱/承压/偏强/有望上行/震荡为主"等方向性判断，direction 就必须填对应的 bearish / bullish / neutral；只有真的无法判断时才填 unclear，且 view 里不得出现方向性措辞。confidence=medium 时允许给方向，并如实写出反向条件。

【投资建议（v4 必填）】
- advice.short_term：短期投资建议，面向 1 天到 2 周；advice.long_term：长期投资建议，面向 1 年以上。
- 建议必须条件化、可执行但不构成指令：用「若…则…」「可以…/暂缓…」的句式，围绕是否继续定投、分批节奏、是否暂缓单笔买入、持有周期、避免追高、应急资金与不杠杆等展开。
- 允许出现「建议分批买入」「建议暂缓买入」「建议继续持有」「建议部分止盈」这类条件化表述。
- 禁止：具体买卖点位、目标价、具体仓位百分比、收益承诺、准确率、"必涨/必跌"等确定性断言。
- confidence=low 或 news_available=false 时：short/long 的 direction 均为 unclear，short_term 只能给"暂缓集中操作、维持原节奏"的保守建议，long_term 只能给与方向无关的积存计划建议。

【置信度规则】
- high：多因子同向 + 有一手来源事件 + 数据完整。
- medium：因子混合或仅有二手来源。
- low：数据缺失、源间分歧、因子互相矛盾或休市；此时所有 direction 必须为 "unclear"，只给关注框架，禁止硬猜方向。

【禁止行为（违反即视为无效输出）】
1. 编造任何价格、涨跌幅、新闻、机构或日期。
2. 输出确定性买卖指令、买卖点位、具体仓位比例、目标价、收益承诺或预测准确率。
3. 把折算参考价当成银行成交价，把媒体推测当成已发生事实。
4. 忽略 counter_case（必填，写出最有说服力的反向情形）。
5. 输出非 json 内容，或改写 disclaimer 字段。

【输出 json 样例（字段名固定，不可增减）】
{
  "as_of_bj": "2026-01-01 08:00:00+08:00",
  "horizon": "1-3d",
  "direction": "unclear",
  "confidence": "low",
  "confidence_reason": "输入数据缺失",
  "outlook": {
    "short": {"direction": "unclear", "horizon": "1-3d", "view": "短期走向及依据"},
    "long": {"direction": "unclear", "horizon": "1-2y", "view": "长期走向及依据"}
  },
  "advice": {"short_term": "短期投资建议（条件化）", "long_term": "长期投资建议（条件化）"},
  "key_drivers": [{"factor":"usd_real_rates","stance":"mixed","note":"一句话","evidence_ref":"输入中的 news url 或 price symbol"}],
  "counter_case": "判断可能错在哪",
  "watch_points": ["条件化的观察点"],
  "summary_cn": "120 字以内中文归纳，含数据口径提示",
  "data_gaps": ["本次缺失的数据项"],
  "disclaimer": "固定免责声明，由系统覆盖，不要自行改写"
}

disclaimer 字段请原样填入下面这一行（系统也会强制覆盖，确保不被改写）：
""" + DISCLAIMER


def build_payload(ds: dict, model: str, messages: list, max_tokens: int) -> dict:
    """构造 chat/completions 请求体。

    DeepSeek 的 deepseek-flash / deepseek-v4-pro 默认先写 reasoning_content，
    思考 token 与正文共用 max_tokens：长提示词下思考会吃满额度，content 变成空串
    （表现为「空 content（第 N 次）」）。研判只要最终 JSON，这里默认显式关闭思考。
    """
    payload = {"model": model, "messages": messages,
               "max_tokens": max(8, int(max_tokens)), "stream": False}
    if ds.get("json_output"):
        payload["response_format"] = {"type": "json_object"}
    if not ds.get("thinking_enabled", False):
        payload["thinking"] = {"type": "disabled"}
    return payload


def _outlook(o: dict, direction: str, horizons: set) -> dict:
    """清洗一组走向预判：方向/尺度不在白名单内就按保守值处理。"""
    o = o if isinstance(o, dict) else {}
    return {"direction": o.get("direction") if o.get("direction") in DIRECTIONS else direction,
            "horizon": o.get("horizon") if o.get("horizon") in horizons else sorted(horizons)[1],
            "view": str(o.get("view") or "").strip()[:240]}


def _advice_text(v, fallback: str) -> str:
    s = str(v or "").strip()
    return s[:300] if s else fallback


def _trend_summary() -> dict:
    """把真实区间涨跌喂给模型：国内 Au99.99 与国际 XAU 的近 1 月 / 近半年 / 近 1 年。"""
    out = {}
    for key, sym in (("domestic", "domestic"), ("xau", "xau")):
        rows = {}
        for per in ("1m", "6m", "1y"):
            try:
                r = gold_history(sym, per)
            except Exception:
                r = {}
            st = r.get("stats") if r.get("ok") else None
            if st:
                rows[per] = {k: st.get(k) for k in ("from", "to", "first", "last", "chg_pct", "high", "low")}
        out[key] = {"unit": "CNY/g" if key == "domestic" else "USD/oz", "ranges": rows}
    return out


def _fallback_signal(snapshot: dict, news: dict, reason: str) -> dict:
    """AI 不可用时的保守输出：不猜方向，只列数据缺口。"""
    gaps = []
    if not snapshot.get("prices"):
        gaps.append("价格数据不可用")
    if not news.get("news_available"):
        gaps.append("本次新闻数据缺失")
    if snapshot.get("quality_notes"):
        gaps.extend(snapshot["quality_notes"][:3])
    return {
        "as_of_bj": iso_bj(), "horizon": "1-3d", "direction": "unclear", "confidence": "low",
        "confidence_reason": "研判服务不可用或数据不足，按规则不输出方向结论：" + reason[:120],
        "outlook": {"short": {"direction": "unclear", "horizon": "1-3d",
                              "view": "研判服务不可用，短期走向暂不判断。"},
                    "long": {"direction": "unclear", "horizon": "1-2y",
                             "view": "研判服务不可用，长期走向暂不判断。"}},
        "advice": {"short_term": _SAFE_SHORT_ADVICE, "long_term": _SAFE_LONG_ADVICE},
        "key_drivers": [],
        "counter_case": "数据不足时任何方向判断都不可靠，需等待价格与消息面补齐后重新研判。",
        "watch_points": ["等待下一次采集完成后重新获取研判", "关注数据口径与源报价时间是否恢复"],
        "summary_cn": "本次 AI 研判暂不可用，仅展示已采集到的数据；请注意上述数据缺口与口径说明。",
        "data_gaps": gaps or ["研判服务不可用"], "disclaimer": DISCLAIMER,
        "_fallback": True,
    }


def _validate_signal(d: dict, snapshot: dict, news: dict) -> tuple[dict, list[str]]:
    warnings = []
    out = dict(d)
    out["as_of_bj"] = iso_bj()
    out["disclaimer"] = DISCLAIMER  # 强制覆盖，禁止模型改写
    out["horizon"] = d.get("horizon") if d.get("horizon") in HORIZONS else "1-3d"
    out["direction"] = d.get("direction") if d.get("direction") in DIRECTIONS else "unclear"
    out["confidence"] = d.get("confidence") if d.get("confidence") in CONFIDENCES else "low"
    forced = False
    if out["confidence"] == "low" or not news.get("news_available") or not snapshot.get("prices"):
        out["direction"] = "unclear"
        forced = True
        warnings.append("低置信度/数据缺失 → 强制 direction=unclear")
    # v4：短期 / 长期走向预判
    ol = d.get("outlook") if isinstance(d.get("outlook"), dict) else {}
    short = _outlook(ol.get("short"), out["direction"], SHORT_HORIZONS)
    long_ = _outlook(ol.get("long"), "unclear", LONG_HORIZONS)
    if forced:
        short["direction"] = long_["direction"] = "unclear"
    if short["direction"] != out["direction"]:
        # 顶层 direction 描述的就是短期走向，两者必须一致，以保守的一方为准
        rank = {"unclear": 0, "neutral": 1, "bullish": 2, "bearish": 2}
        if rank[short["direction"]] < rank[out["direction"]]:
            out["direction"] = short["direction"]
        else:
            short["direction"] = out["direction"]
    if not short["view"]:
        short["view"] = "本次未给出短期走向说明。"
    if not long_["view"]:
        long_["view"] = "本次未给出长期走向说明。"
    out["outlook"] = {"short": short, "long": long_}
    # v4：短期 / 长期投资建议
    adv = d.get("advice") if isinstance(d.get("advice"), dict) else {}
    out["advice"] = {"short_term": _advice_text(adv.get("short_term"), _SAFE_SHORT_ADVICE),
                     "long_term": _advice_text(adv.get("long_term"), _SAFE_LONG_ADVICE)}
    if forced:
        out["advice"]["short_term"] = _SAFE_SHORT_ADVICE
    for k, v in list(out["advice"].items()):
        if _POSITION_RE.search(v):
            out["advice"][k] = _SAFE_SHORT_ADVICE if k == "short_term" else _SAFE_LONG_ADVICE
            warnings.append("投资建议含具体仓位百分比 → 已替换为保守表述")
    drivers = []
    for x in (d.get("key_drivers") or [])[:6]:
        if isinstance(x, dict):
            drivers.append({"factor": x.get("factor") if x.get("factor") in FACTORS else "macro",
                            "stance": x.get("stance") if x.get("stance") in ("supportive", "pressuring", "mixed") else "mixed",
                            "note": str(x.get("note", ""))[:160],
                            "evidence_ref": str(x.get("evidence_ref", ""))[:240]})
    out["key_drivers"] = drivers
    out["counter_case"] = str(d.get("counter_case") or "未给出反向情形，已由系统补充：数据或框架变化可能使判断失效。")[:300]
    out["watch_points"] = [str(w)[:160] for w in (d.get("watch_points") or [])[:5]]
    out["summary_cn"] = str(d.get("summary_cn") or "本次研判未生成归纳。")[:200]
    out["data_gaps"] = [str(g)[:160] for g in (d.get("data_gaps") or [])[:6]]
    if not news.get("news_available"):
        out["data_gaps"].append("本次新闻数据缺失")
    if snapshot.get("quality_notes"):
        out["data_gaps"].extend(snapshot["quality_notes"][:2])
    blob = json.dumps(out, ensure_ascii=False)
    for w in BANNED:
        if w in blob:
            out["direction"] = "unclear"
            out["confidence"] = "low"
            out["outlook"]["short"]["direction"] = out["outlook"]["long"]["direction"] = "unclear"
            out["summary_cn"] = "输出命中确定性指令禁词，已降级为不明确；仅保留数据展示。"
            out["advice"] = {"short_term": _SAFE_SHORT_ADVICE, "long_term": _SAFE_LONG_ADVICE}
            out["key_drivers"] = []
            warnings.append("命中禁词 %s → 降级" % w)
            break
    return out, warnings


class Analyzer:
    def __init__(self, cfg_store, db, logger):
        self.cfg_store = cfg_store
        self.db = db
        self.log = logger

    def _budget_ok(self, daily: bool = False) -> tuple[bool, str]:
        cfg = self.cfg_store.get()["deepseek"]
        u = self.db.usage_today()
        max_calls = int(cfg.get("max_calls_per_day", 40))
        max_tokens = int(cfg.get("daily_token_budget", 200000))
        reserve_calls = 0 if daily else int(cfg.get("reserved_calls_for_daily", 0) or 0)
        reserve_tokens = 0 if daily else int(cfg.get("reserved_tokens_for_daily", 0) or 0)
        call_limit = max(1, max_calls - reserve_calls)
        token_limit = max(1, max_tokens - reserve_tokens)
        if u["calls"] >= call_limit:
            return False, ("本端今日调用次数已达上限 %d（其中为每日文档预留 %d 次）"
                           % (call_limit, reserve_calls) if reserve_calls else
                           "本端今日调用次数已达上限 %d" % max_calls)
        used = (u["prompt_tokens"] or 0) + (u["completion_tokens"] or 0)
        if used >= token_limit:
            return False, ("本端今日 token 已达预算 %d（其中为每日文档预留 %d）"
                           % (token_limit, reserve_tokens) if reserve_tokens else
                           "本端今日 token 已达预算 %d" % max_tokens)
        return True, ""

    def test_connection(self) -> dict:
        cfg = self.cfg_store.get()["deepseek"]
        key = self.cfg_store.secret("deepseek.api_key")
        if not key:
            return {"ok": False, "message": "尚未录入 API Key（请先在设置页保存）"}
        base = cfg["base_url"].rstrip("/")
        payload = build_payload(cfg, cfg["model_flash"],
                                [{"role": "user", "content": "ping，请只回复 ok"}], 16)
        payload.pop("response_format", None)   # 探活不需要 json 模式，也不该被 json 约束
        try:
            t0 = time.time()
            status, body = http_post_json(base + "/chat/completions", payload,
                                          {"Authorization": "Bearer " + key}, cfg["timeout_seconds"])
            latency = int((time.time() - t0) * 1000)
            if status != 200:
                return {"ok": False, "http_status": status,
                        "message": redact(body[:300], [key]), "latency_ms": latency}
            d = json.loads(body)
            model = d.get("model", cfg["model_flash"])
            choices = d.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            self.db.add_usage(d.get("usage", {}).get("prompt_tokens", 0),
                              d.get("usage", {}).get("completion_tokens", 0))
            if not (msg.get("content") or "").strip():
                return {"ok": False, "message": "模型返回空 content（finish=%s，思考 %d 字）" %
                        (choices[0].get("finish_reason") if choices else "—",
                         len(msg.get("reasoning_content") or "")), "latency_ms": latency}
            return {"ok": True, "message": "连接成功（产生一次极小额调用）",
                    "model": model, "latency_ms": latency}
        except Exception as e:
            return {"ok": False, "message": redact(str(e)[:300], [key])}

    def summarize(self, system: str, user: str, max_tokens: int = 1600,
                  force: bool = False, daily: bool = False) -> dict:
        """通用文本归纳（每日新闻总结 / 周期汇总）。

        返回 {ok, text, reason, usage}。计入本端调用次数与 token 预算；
        输出为纯文本 / Markdown，不启用 json 模式。
        """
        cfg = self.cfg_store.get()
        ds = cfg["deepseek"]
        key = self.cfg_store.secret("deepseek.api_key")
        if not key:
            return {"ok": False, "reason": "未配置 DeepSeek API Key"}
        ok, why = self._budget_ok(daily=daily)
        if not ok and not force:
            return {"ok": False, "reason": why}
        payload = build_payload(ds, ds["model_flash"], [
            {"role": "system", "content": system}, {"role": "user", "content": user}], max_tokens)
        payload.pop("response_format", None)
        headers = {"Authorization": "Bearer " + key}
        last_err, cur_max = "", max(600, int(max_tokens))
        attempts = int(ds.get("retry_times", 2)) + 1
        for i in range(attempts):
            payload["max_tokens"] = cur_max
            try:
                t0 = time.time()
                status, body = http_post_json(ds["base_url"].rstrip("/") + "/chat/completions",
                                              payload, headers, ds["timeout_seconds"])
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
                text = (msg.get("content") or "").strip()
                if not text:
                    last_err = "空 content（finish=%s，思考 %d 字）" % (
                        (choices[0].get("finish_reason") or "") if choices else "",
                        len(msg.get("reasoning_content") or ""))
                    if cur_max < 4096 and i < attempts - 1:
                        cur_max = min(4096, cur_max * 2)
                        continue
                    break
                text = re.sub(r"^```[\w-]*\s*|\s*```$", "", text, flags=re.M).strip()
                return {"ok": True, "text": text, "latency_ms": int((time.time() - t0) * 1000),
                        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                                  "completion_tokens": usage.get("completion_tokens", 0)}}
            except Exception as e:
                last_err = redact("%s: %s" % (type(e).__name__, str(e)[:200]), [key])
                if i < attempts - 1:
                    time.sleep(1.0 * (2 ** i))
        return {"ok": False, "reason": redact("DeepSeek 调用失败：" + (last_err or "未知原因"), [key])}

    def analyze(self, snapshot: dict, news: dict, trigger: str = "scheduled",
                force: bool = False, use_pro: bool = False) -> dict:
        cfg = self.cfg_store.get()
        ds = cfg["deepseek"]
        key = self.cfg_store.secret("deepseek.api_key")
        if not key:
            sig = _fallback_signal(snapshot, news, "未配置 API Key")
            sig.update({"prompt_version": PROMPT_VERSION, "model": ds["model_flash"],
                        "snapshot_hash": snapshot.get("snapshot_hash"), "trigger_source": trigger})
            self.db.insert_signal(sig)
            return {"available": False, "reason": "未配置 DeepSeek API Key", "signal": sig}
        ok, why = self._budget_ok()
        if not ok and not force:
            sig = _fallback_signal(snapshot, news, why)
            sig.update({"prompt_version": PROMPT_VERSION, "model": ds["model_flash"],
                        "snapshot_hash": snapshot.get("snapshot_hash"), "trigger_source": trigger})
            self.db.insert_signal(sig)
            return {"available": False, "reason": why, "signal": sig}
        cache_s = int(cfg.get("manual_fetch", {}).get("snapshot_cache_seconds", 300))
        if not force and cache_s > 0:
            last = self.db.last_signal_hash()
            if last == snapshot.get("snapshot_hash") and not news.get("force_refresh"):
                prev = self.db.latest_signal()
                if prev:
                    try:
                        cached = json.loads(prev["signal_json"])
                    except Exception:
                        cached = None
                    # 旧版（v3 及之前）研判没有走向预判与投资建议，不复用，重新生成
                    if cached and cached.get("outlook") and cached.get("advice"):
                        return {"available": True, "cached": True, "reason": "相同快照，复用 5 分钟内研判结果",
                                "signal": cached}
        try:
            trend = _trend_summary()
        except Exception as e:
            trend = {"error": str(e)[:120]}
        user_payload = {
            "as_of_bj": snapshot.get("as_of_bj"), "market_status": snapshot.get("market_status"),
            "prices": [{k: p.get(k) for k in ("symbol", "name", "price", "unit", "change_pct",
                                              "change_base", "source", "source_ts", "quality_flag", "role")}
                       for p in snapshot.get("prices", []) if not p.get("alternate")],
            "fx": snapshot.get("fx"), "folded_cny_g": snapshot.get("folded_cny_g"),
            "quality_notes": snapshot.get("quality_notes", []),
            "trend": trend,
            "news_available": bool(news.get("news_available")),
            "news": [{"title": n.get("title"), "summary": n.get("summary"), "source": n.get("source"),
                      "published_at_local": n.get("published_at_local"),
                      "published_at_bj": n.get("published_at_bj"), "topic": n.get("topic"),
                      "url": n.get("url")} for n in news.get("news", [])[:8]],
            "recent_signals": [{"direction": s.get("direction"), "confidence": s.get("confidence"),
                                "as_of_bj": s.get("as_of_bj")} for s in self.db.recent_signals(3)],
        }
        model_name = ds["model_pro"] if use_pro else ds["model_flash"]
        payload = build_payload(ds, model_name, [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "基于以下 input 生成 json 研判；先给短期与长期走向预判，再给短期与长期投资建议；"
                                        "只依据 input 中给出的事实，不要补充任何未提供的数据。\ninput = " +
             json.dumps(user_payload, ensure_ascii=False)}], ds["max_tokens"])
        base = ds["base_url"].rstrip("/")
        headers = {"Authorization": "Bearer " + key}
        last_err, raw = "", ""
        attempts = int(ds.get("retry_times", 2)) + 1
        cur_max = max(600, int(ds.get("max_tokens", 1200)))
        for i in range(attempts):
            payload["max_tokens"] = cur_max
            try:
                t0 = time.time()
                status, body = http_post_json(base + "/chat/completions", payload, headers,
                                              ds["timeout_seconds"])
                latency = int((time.time() - t0) * 1000)
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
                    # 兼容旧配置/其他中转：模型仍在思考而没有正文时，加倍额度再试
                    last_err = "空 content（finish=%s，思考 %d 字）" % (finish or "—", len(think))
                    if cur_max < 4096 and i < attempts - 1:
                        cur_max = min(4096, cur_max * 2)
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
                    if finish == "length" and cur_max < 4096 and i < attempts - 1:
                        cur_max = min(4096, cur_max * 2)
                        last_err += "，已把 max_tokens 提高到 %d 重试" % cur_max
                        continue
                    if i < attempts - 1:
                        time.sleep(1.0)
                        continue
                    break
                signal, warns = _validate_signal(parsed, snapshot, news)
                signal.update({"prompt_version": PROMPT_VERSION, "model": payload["model"],
                               "prompt_tokens": usage.get("prompt_tokens", 0),
                               "completion_tokens": usage.get("completion_tokens", 0),
                               "latency_ms": latency, "snapshot_hash": snapshot.get("snapshot_hash"),
                               "trigger_source": trigger})
                for w in warns:
                    self.log.warning("AI 输出校验：%s", w)
                self.db.insert_signal(signal)
                self.db.event("info", "AI 研判完成 direction=%s confidence=%s" %
                              (signal["direction"], signal["confidence"]))
                return {"available": True, "signal": signal, "warnings": warns,
                        "usage": {"prompt_tokens": usage.get("prompt_tokens", 0),
                                  "completion_tokens": usage.get("completion_tokens", 0),
                                  "latency_ms": latency}}
            except Exception as e:
                last_err = redact("%s: %s" % (type(e).__name__, str(e)[:200]), [key])
                if i < attempts - 1:
                    time.sleep(1.0 * (2 ** i))
                    continue
        reason = "DeepSeek 调用失败：" + (last_err or "未知原因")
        self.log.warning(redact(reason, [key]))
        self.db.event("warn", redact(reason, [key]))
        sig = _fallback_signal(snapshot, news, reason)
        sig.update({"prompt_version": PROMPT_VERSION, "model": payload["model"],
                    "snapshot_hash": snapshot.get("snapshot_hash"), "trigger_source": trigger})
        self.db.insert_signal(sig)
        return {"available": False, "reason": redact(reason, [key]), "signal": sig}
