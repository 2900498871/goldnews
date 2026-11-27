# -*- coding: utf-8 -*-
"""快报 / 每日总结渲染（FR-04 / FR-05）。HTML + 纯文本双版本，固定免责声明原样附加。"""
from __future__ import annotations

import html as _html

from .util import DISCLAIMER, bj_str, now_bj

CN_DIRECTION = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性", "unclear": "信号不明确"}
CN_CONF = {"high": "高", "medium": "中", "low": "低"}
CN_HORIZON = {"intraday": "日内", "1-3d": "1–3 日", "1-2w": "1–2 周"}


def _e(s) -> str:
    return _html.escape(str(s if s is not None else ""), quote=True)


def _color(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "#5B6472"
    return "#D33A3A" if f > 0 else ("#12946B" if f < 0 else "#5B6472")


def _pct(v) -> str:
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
        # 快照间涨跌多为千分之几，固定两位小数会显示成 +0.00%
        return ("+" if f > 0 else "") + (("%.3f%%" if abs(f) < 0.1 else "%.2f%%") % f)
    except (TypeError, ValueError):
        return "—"


def price_rows(snapshot: dict) -> list[dict]:
    out = []
    for p in snapshot.get("prices", []):
        if p.get("alternate"):
            continue
        out.append({
            "symbol": p.get("symbol"), "name": p.get("name"), "price": p.get("price"),
            "unit": p.get("unit"), "change_pct": p.get("change_pct"),
            "change_base": p.get("change_base"), "source": p.get("source"),
            "source_ts": p.get("source_ts"), "quality_flag": p.get("quality_flag"),
            "role": p.get("role"), "divergent_values": p.get("divergent_values"),
        })
    return out


def _quality_text(snapshot: dict, news: dict, ai_result: dict | None) -> list[str]:
    q = list(snapshot.get("quality_notes") or [])
    if not snapshot.get("prices"):
        q.append("本次价格数据不可用")
    if not news.get("news_available"):
        q.append("本次未获取到新闻")
    if ai_result is not None and not ai_result.get("available"):
        # 这里只存原文，最终由 _quality_html 统一转义；提前转义会导致 &amp;lt; 这类二次转义
        q.append("AI 研判暂不可用：" + str(ai_result.get("reason", ""))[:120])
    if snapshot.get("market_status") == "closed":
        q.append("当前为休市时段，展示最近一次有效报价")
    return q


def _html_price_table(snapshot: dict) -> str:
    """价格并列：一条一段、单列堆叠（名称 → 价格+涨跌 → 来源一行）。

    手机邮件客户端（QQ 邮箱、微信内置）会把多列表格压成窄列，出现竖排单字；
    这里完全不用并排单元格，宽屏是整齐的卡片列表，窄屏只是行变宽，不会崩。
    """
    rows = []
    for p in price_rows(snapshot):
        chg = p["change_pct"]
        chg_html = '<span style="color:%s;font-weight:700">%s</span>' % (_color(chg), _e(_pct(chg)))
        flag = ""
        if p["quality_flag"] in ("cached", "stale", "divergent", "derived", "unknown_ts"):
            label = {"cached": "缓存值，可能滞后", "stale": "陈旧数据", "divergent": "源间分歧",
                     "derived": "折算参考价 · 非成交价", "unknown_ts": "源未提供报价时间"}[p["quality_flag"]]
            flag = ('<span style="display:inline-block;background:#FFF3D6;color:#8A6100;'
                    'padding:1px 6px;border-radius:4px;font-size:11px;margin-top:4px">%s</span>' % label)
        extra = ""
        if p.get("divergent_values"):
            extra = ('<div style="font-size:11px;color:#8A6100;margin-top:4px;line-height:1.6;word-break:break-word">并列值：'
                     + " ｜ ".join("%s %s" % (_e(x["source"]), _e(x["price"]))
                                   for x in p["divergent_values"]) + "</div>")
        rows.append(
            '<div style="padding:11px 0;border-bottom:1px solid #EDF1F7">'
            # 第 1 行：口径名称 + 代码
            '<div style="font-size:13px;font-weight:600;color:#1A2434;line-height:1.6;word-break:break-word">%s'
            '<span style="font-weight:400;color:#8490A3;font-size:11px;margin-left:5px">%s</span></div>'
            # 第 2 行：价格 + 涨跌（同一行，价格不换行）
            '<div style="margin-top:3px;line-height:1.6"><span style="font-size:16px;font-weight:700;'
            'color:#1A2434;white-space:nowrap">%s</span>'
            '<span style="font-size:11px;color:#8490A3;margin-left:4px;white-space:nowrap">%s</span>'
            '<span style="font-size:11px;margin-left:10px;white-space:nowrap">涨跌 %s</span></div>'
            # 第 3 行：口径角色
            '<div style="font-size:11px;color:#8490A3;margin-top:2px;line-height:1.6;word-break:break-word">%s</div>'
            # 第 4 行：来源与时间（整行铺开，窄屏自然换行）
            '<div style="font-size:11px;color:#5B6472;margin-top:4px;line-height:1.7;word-break:break-word">'
            '来源 %s ｜ 源报价时间 %s ｜ 涨跌基准 %s</div>%s%s</div>'
            % (_e(p["name"]), _e(p["symbol"]),
               _e(p["price"]), _e(p["unit"]), chg_html,
               _e(p["role"]),
               _e(p["source"]), _e(p["source_ts"] or "源未提供"), _e(p["change_base"] or "—"),
               flag, extra))
    if not rows:
        return '<div style="padding:12px;color:#8A6100;background:#FFF8EA">本次价格数据不可用</div>'
    return ('<div style="font-size:11px;color:#8490A3;margin:-2px 0 4px">同一时点各口径并列；涨跌按各口径自身基准计算，'
            '折算价非成交价。</div>' + "".join(rows))


def _text_price_block(snapshot: dict) -> str:
    lines = []
    for p in price_rows(snapshot):
        lines.append("· %s（%s）%s %s  涨跌 %s（基准 %s）" %
                     (p["name"], p["symbol"], p["price"], p["unit"],
                      _pct(p["change_pct"]), p["change_base"] or "—"))
        lines.append("    口径角色：%s | 来源：%s | 源报价时间：%s | 质量：%s" %
                     (p["role"], p["source"], p["source_ts"] or "源未提供", p["quality_flag"]))
        if p.get("divergent_values"):
            lines.append("    源间分歧并列值：" + " / ".join("%s=%s" % (x["source"], x["price"])
                                                       for x in p["divergent_values"]))
    return "\n".join(lines) if lines else "本次价格数据不可用"


def _html_news(news: dict) -> str:
    items = news.get("news") or []
    if not items:
        return '<div style="padding:10px;color:#8A6100;background:#FFF8EA;font-size:12px">本次未获取到新闻（新闻源不可用或时间窗内无相关稿件）；AI 输入已标注 news_available=false。</div>'
    out = []
    for n in items[:5]:
        out.append('<li style="margin-bottom:9px"><a href="%s" style="color:#2456C4;text-decoration:none;font-weight:600">%s</a>'
                   '<div style="font-size:11px;color:#8490A3;margin-top:2px">%s ｜ 本地 %s / 北京时间 %s ｜ 主题 %s</div>'
                   '%s</li>' % (_e(n.get("url")), _e(n.get("title")), _e(n.get("source")),
                                _e(n.get("published_at_local") or "—"), _e(n.get("published_at_bj") or "—"),
                                _e(n.get("topic")),
                                ('<div style="font-size:12px;color:#5B6472">%s</div>' % _e(n.get("summary"))) if n.get("summary") else ""))
    return '<ul style="padding-left:18px;margin:6px 0;font-size:13px;color:#1A2434">' + "".join(out) + "</ul>"


def _text_news(news: dict) -> str:
    items = news.get("news") or []
    if not items:
        return "本次未获取到新闻（新闻源不可用或时间窗内无相关稿件）。"
    return "\n".join("· %s — %s ｜ 本地 %s / 北京时间 %s\n  %s" %
                     (n.get("title"), n.get("source"), n.get("published_at_local") or "—",
                      n.get("published_at_bj") or "—", n.get("url") or "") for n in items[:5])


CN_LONG_HORIZON = {"6-12m": "6–12 个月", "1-2y": "1–2 年", "3y+": "3 年以上"}


def _outlook_lines(sig: dict) -> list:
    """v4：短期 / 长期走向 + 短期 / 长期投资建议，放在研判最前面。旧版信号没有这些字段则不输出。"""
    ol, adv = sig.get("outlook") or {}, sig.get("advice") or {}
    out = []
    for key, lb, hm in (("short", "短期走向", CN_HORIZON), ("long", "长期走向", CN_LONG_HORIZON)):
        o = ol.get(key) or {}
        if o:
            out.append("%s：%s（%s）—— %s" % (lb, CN_DIRECTION.get(o.get("direction"), "信号不明确"),
                                           hm.get(o.get("horizon"), "—"), o.get("view") or "—"))
    for key, lb in (("short_term", "短期投资建议"), ("long_term", "长期投资建议")):
        if adv.get(key):
            out.append("%s：%s" % (lb, adv[key]))
    return out


def _html_signal(sig: dict) -> str:
    if not sig:
        return '<div style="padding:10px;color:#8A6100;background:#FFF8EA;font-size:12px">AI 研判暂不可用</div>'
    drivers = "".join("<li>%s ｜ %s：%s（依据 %s）</li>" %
                      (_e(d.get("factor")), _e(d.get("stance")), _e(d.get("note")), _e(d.get("evidence_ref")))
                      for d in sig.get("key_drivers", []))
    ol = _outlook_lines(sig)
    ol_html = "".join('<div style="font-size:13px;margin-bottom:6px"><b>%s</b> %s</div>' % (_e(l.split("：", 1)[0]), _e(l.split("：", 1)[1] if "：" in l else ""))
                      for l in ol) if ol else ""
    return ('<div style="background:#F5F8FD;border-radius:8px;padding:12px;font-size:13px">'
            '%s'
            '<div style="font-size:15px;font-weight:700;margin-bottom:6px">方向倾向 %s ｜ 置信度 %s ｜ 时间尺度 %s</div>'
            '<div style="margin-bottom:6px">%s</div>'
            '<div style="font-size:12px;color:#5B6472"><b>置信度依据：</b>%s</div>'
            '<div style="font-size:12px;margin-top:6px"><b>主要驱动：</b><ul style="margin:4px 0;padding-left:18px">%s</ul></div>'
            '<div style="font-size:12px;color:#B45309"><b>反向情形：</b>%s</div>'
            '<div style="font-size:12px;margin-top:5px"><b>关注：</b>%s</div>'
            '<div style="font-size:11px;color:#8490A3;margin-top:6px">走向预判与投资建议为 AI 基于公开信息的条件化参考，不含买卖点位与仓位比例。</div>'
            '</div>') % (ol_html,
                         _e(CN_DIRECTION.get(sig.get("direction"), "信号不明确")),
                         _e(CN_CONF.get(sig.get("confidence"), "低")),
                         _e(CN_HORIZON.get(sig.get("horizon"), "—")),
                         _e(sig.get("summary_cn")), _e(sig.get("confidence_reason")),
                         drivers or "<li>本次未列出可追溯驱动（数据不足）</li>",
                         _e(sig.get("counter_case")),
                         _e("；".join(sig.get("watch_points", [])) or "—"))


def _text_signal(sig: dict) -> str:
    if not sig:
        return "AI 研判暂不可用"
    ol = _outlook_lines(sig)
    return ("%s方向倾向 %s ｜ 置信度 %s ｜ 时间尺度 %s\n%s\n置信度依据：%s\n主要驱动：%s\n反向情形：%s\n关注：%s" %
            (("".join(l + "\n" for l in ol)),
             CN_DIRECTION.get(sig.get("direction"), "信号不明确"), CN_CONF.get(sig.get("confidence"), "低"),
             CN_HORIZON.get(sig.get("horizon"), "—"), sig.get("summary_cn", ""),
             sig.get("confidence_reason", ""),
             "；".join("%s:%s" % (d.get("factor"), d.get("note")) for d in sig.get("key_drivers", [])) or "数据不足",
             sig.get("counter_case", ""), "；".join(sig.get("watch_points", [])) or "—"))


def _wrap(title: str, body: str) -> str:
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="x-apple-disable-message-reformatting">'
            '<title>%s</title>'
            '<style>'
            '@media only screen and (max-width:520px){'
            '.gd-wrap{padding:10px !important}'
            '.gd-card{padding:14px !important;border-radius:10px !important}'
            '.gd-card h2{font-size:17px !important}'
            '}'
            '</style></head>'
            '<body style="margin:0;padding:0;background:#F2F5FA;font-family:-apple-system,BlinkMacSystemFont,'
            "'Segoe UI','Microsoft YaHei',sans-serif;color:#1A2434\">"
            '<div class="gd-wrap" style="padding:16px">'
            '<div class="gd-card" style="max-width:680px;width:100%%;box-sizing:border-box;margin:0 auto;'
            'background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 12px rgba(20,40,80,.06)">'
            '<h2 style="margin:0 0 4px;font-size:18px;line-height:1.5">%s</h2>'
            '<div style="font-size:11px;color:#8490A3;margin-bottom:14px">日常小助手 · 准实时（2–5 分钟级） · 北京时间 %s · 单用户自用</div>'
            '%s'
            '<div style="margin-top:18px;padding-top:12px;border-top:1px solid #EDF1F7;font-size:11px;color:#5B6472;line-height:1.7">%s</div>'
            '</div></div></body></html>') % (_e(title), _e(title), _e(bj_str(now_bj())), body, _e(DISCLAIMER))


def _quality_html(q: list[str]) -> str:
    if not q:
        return ""
    return ('<div style="margin-top:12px;padding:10px;background:#FFF8EA;border-radius:8px;font-size:12px;color:#8A6100">'
            '<b>数据质量提示：</b><br>' + "<br>".join(_e(x) for x in q) + "</div>")


def mail_prefix(cfg: dict, default: str = "黄金积存金") -> str:
    """邮件主题前缀：优先用设置页的发件人显示名，保持与发件人一致。"""
    s = (cfg or {}).get("smtp") or {}
    return str(s.get("from_name") or "").strip() or default


def render_flash(snapshot: dict, news: dict, ai_result: dict, prefix: str = "黄金积存金") -> dict:
    subject = "【%s】快报 · %s" % (prefix, bj_str(now_bj(), "%Y-%m-%d %H:%M"))
    sig = (ai_result or {}).get("signal") or {}
    q = _quality_text(snapshot, news, ai_result)
    body = (
        '<h3 style="font-size:14px;margin:16px 0 6px">价格（多口径并列）</h3>' + _html_price_table(snapshot) +
        '<h3 style="font-size:14px;margin:18px 0 4px">消息面</h3>' + _html_news(news) +
        '<h3 style="font-size:14px;margin:18px 0 6px">AI 研判</h3>' +
        ('<div style="font-size:11px;color:#8490A3;margin-bottom:6px">模型 %s ｜ Prompt %s ｜ 信号可追溯至输入价格与新闻；不构成投资建议</div>'
         % (_e(sig.get("model") or "—"), _e(sig.get("prompt_version") or "—"))) +
        _html_signal(sig) + _quality_html(q) +
        ('<div style="margin-top:10px;font-size:12px;color:#5B6472"><b>数据缺口：</b>%s</div>'
         % _e("；".join(sig.get("data_gaps", [])) or "无")))
    text = "\n".join([
        subject, "（准实时 2–5 分钟级 · 北京时间 %s）" % bj_str(now_bj()),
        "", "[价格卡片]", _text_price_block(snapshot),
        "", "[要闻]", _text_news(news),
        "", "[AI 研判]", _text_signal(sig),
        "", "[数据质量提示]", ("\n".join("· " + x for x in q) if q else "无"),
        "", "[数据缺口]", "；".join(sig.get("data_gaps", [])) or "无",
        "", DISCLAIMER,
    ])
    return {"subject": subject, "html": _wrap(subject, body), "text_body": text,
            "report_type": "flash", "report_date": bj_str(now_bj(), "%Y-%m-%d"),
            "generated_at": bj_str(now_bj())}


def render_history(rng: dict, collection: dict, result: dict, analysis: dict | None = None,
                   prefix: str = "黄金积存金") -> dict:
    """FR-16：时段新闻回顾 + 未来情景研判报告（HTML + 纯文本，固定免责声明）。"""
    from .history import HORIZONS, TOPIC_LABEL, STANCE_LABEL, PRESET_LABEL  # 避免顶层循环依赖
    cn_dir = {"bullish": "偏多倾向", "bearish": "偏空倾向", "neutral": "中性",
              "unclear": "信号不明确"}
    r = result or {}
    analysis = analysis or {}
    period = "%s ~ %s（%s，共 %d 天）" % (rng["start"], rng["end"],
                                         PRESET_LABEL.get(rng["preset"], rng["preset"]), rng["days"])
    horizon = HORIZONS.get(r.get("forward_horizon"), "未来 1–3 个月")
    subject = "【%s】时段新闻情景研判 · %s ~ %s" % (prefix, rng["start"], rng["end"])

    topics_html = "".join(
        "<li>%s：<b>%d</b> 条</li>" % (_e(t.get("label") or TOPIC_LABEL.get(t.get("topic"), t.get("topic"))),
                                     t.get("count", 0))
        for t in collection.get("topic_stats", [])) or "<li>本次无样本</li>"
    months_html = "".join(
        "<li>%s：<b>%d</b> 条</li>" % (_e(m["month"]), m["count"])
        for m in collection.get("monthly", [])) or "<li>本次无样本</li>"
    src_html = "".join("<li>%s：%s</li>" % (_e(k), _e(v))
                       for k, v in (collection.get("sources") or {}).items()) or "<li>无数据源</li>"
    themes_html = "".join(
        "<li><b>%s</b>（%s）：%s ｜ 依据 %s</li>" %
        (_e(t.get("theme")), _e(STANCE_LABEL.get(t.get("stance"), "混合")),
         _e(t.get("note")), _e(t.get("evidence_ref")))
        for t in r.get("key_themes", [])) or "<li>本次未列出可追溯主题（数据不足）</li>"
    timeline_html = "".join(
        "<li>%s（%s）：%s</li>" %
        (_e(t.get("month")), _e(STANCE_LABEL.get(t.get("tone"), "不明确")), _e(t.get("note")))
        for t in r.get("timeline", [])) or "<li>本次未生成月度节奏（样本不足）</li>"
    news_html = _html_news({"news": collection.get("news", [])[:8]})
    gaps = r.get("data_gaps", []) or []
    ai_state = ("AI 情景研判完成" if analysis.get("available") else
                "AI 情景研判暂不可用：" + _e(analysis.get("reason", "未配置/调用失败")))

    body = (
        '<div style="background:#F0F5FF;border-radius:8px;padding:10px 12px;font-size:12px;color:#2456C4;'
        'margin-bottom:12px"><b>这是基于历史新闻样本的条件化情景归纳，不是价格预测</b>；'
        '不承诺收益或准确率，不提供买卖点位、仓位比例或确定性买卖指令。</div>'
        '<h3 style="font-size:14px;margin:14px 0 6px">1. 分析区间与样本</h3>'
        '<div style="font-size:13px">%s ｜ 未来观察尺度：%s ｜ 有效新闻样本：<b>%d</b> 条</div>'
        '<div style="font-size:11.5px;color:#5B6472;margin-top:5px">%s</div>'
        '<h3 style="font-size:14px;margin:18px 0 6px">2. 数据源状态</h3><ul style="font-size:12px;padding-left:18px">%s</ul>'
        '<h3 style="font-size:14px;margin:18px 0 6px">3. 主题与月度分布</h3>'
        '<div style="font-size:12px"><b>主题：</b><ul style="padding-left:18px;margin:4px 0">%s</ul>'
        '<b>月度：</b><ul style="padding-left:18px;margin:4px 0">%s</ul></div>'
        '<h3 style="font-size:14px;margin:18px 0 6px">4. AI 未来涨跌情景研判</h3>'
        '<div style="font-size:11px;color:#8490A3;margin-bottom:6px">%s ｜ 模型 %s ｜ Prompt %s</div>'
        '<div style="background:#F5F8FD;border-radius:8px;padding:12px;font-size:13px">'
        '<div style="font-size:16px;font-weight:700;margin-bottom:6px">方向倾向：%s ｜ 置信度 %s</div>'
        '<div style="margin-bottom:6px">%s</div>'
        '<div style="font-size:12px;color:#5B6472"><b>置信度依据：</b>%s</div>'
        '<div style="font-size:12px;margin-top:6px"><b>关键主题：</b><ul style="margin:4px 0;padding-left:18px">%s</ul></div>'
        '<div style="font-size:12px"><b>月度节奏：</b><ul style="margin:4px 0;padding-left:18px">%s</ul></div>'
        '<div style="font-size:12px;color:#B45309"><b>反向情形：</b>%s</div>'
        '<div style="font-size:12px;margin-top:5px"><b>关注：</b>%s</div></div>'
    ) % (_e(period), _e(horizon), collection.get("total", 0),
         _e(collection.get("coverage_note", "")), src_html, topics_html, months_html,
         ai_state, _e(r.get("model") or "—"), _e(r.get("prompt_version") or "—"),
         _e(cn_dir.get(r.get("direction"), "信号不明确")),
         _e(CN_CONF.get(r.get("confidence"), "低")),
         _e(r.get("summary_cn")), _e(r.get("confidence_reason")),
         themes_html, timeline_html, _e(r.get("counter_case")),
         _e("；".join(r.get("watch_points", [])) or "—"))
    # 新闻 HTML 中可能含 URL 百分号编码，不能参与 %-format，故在格式化后拼接
    body += ('<h3 style="font-size:14px;margin:18px 0 6px">5. 区间新闻样本（节选）</h3>' +
             news_html +
             '<div style="margin-top:12px;font-size:12px;color:#5B6472"><b>数据缺口：</b>%s</div>'
             % _e("；".join(gaps) or "无"))

    text_lines = [
        subject,
        "（分析区间 %s ｜ 未来观察尺度 %s ｜ 北京时间 %s）" % (period, horizon, bj_str(now_bj())),
        "重要提示：这是基于历史新闻样本的条件化情景归纳，不是价格预测；不承诺收益或准确率，不提供买卖点位或仓位指令。",
        "", "1. 分析区间与样本",
        "有效新闻样本 %d 条；%s" % (collection.get("total", 0), collection.get("coverage_note", "")),
        "", "2. 数据源状态",
    ]
    text_lines += ["· %s：%s" % (k, v) for k, v in (collection.get("sources") or {}).items()]
    text_lines += ["", "3. 主题与月度分布",
                   "主题：" + ("；".join("%s %d 条" % (t.get("label", t.get("topic")), t.get("count", 0))
                                       for t in collection.get("topic_stats", [])) or "无样本"),
                   "月度：" + ("；".join("%s %d 条" % (m["month"], m["count"])
                                       for m in collection.get("monthly", [])) or "无样本"),
                   "", "4. AI 未来涨跌情景研判",
                   ai_state.replace("&amp;", "&"),
                   "方向倾向 %s ｜ 置信度 %s ｜ %s" %
                   (cn_dir.get(r.get("direction"), "信号不明确"),
                    CN_CONF.get(r.get("confidence"), "低"), horizon),
                   r.get("summary_cn", ""),
                   "置信度依据：" + str(r.get("confidence_reason", "")),
                   "关键主题：" + ("；".join("%s(%s):%s" % (t.get("theme"), t.get("stance"), t.get("note"))
                                          for t in r.get("key_themes", [])) or "数据不足"),
                   "月度节奏：" + ("；".join("%s(%s):%s" % (t.get("month"), t.get("tone"), t.get("note"))
                                          for t in r.get("timeline", [])) or "数据不足"),
                   "反向情形：" + str(r.get("counter_case", "")),
                   "关注：" + ("；".join(r.get("watch_points", [])) or "—"),
                   "", "5. 区间新闻样本（节选）", _text_news({"news": collection.get("news", [])[:8]}),
                   "", "[数据缺口]", "；".join(gaps) or "无",
                   "", DISCLAIMER]
    return {"subject": subject, "html": _wrap(subject, body),
            "text_body": "\n".join(text_lines),
            "report_type": "history_analysis",
            "report_date": bj_str(now_bj(), "%Y-%m-%d"),
            "generated_at": bj_str(now_bj())}


def render_daily(snapshot: dict, news: dict, ai_result: dict, signal_review: str = "",
                 prefix: str = "黄金积存金") -> dict:
    subject = "【%s】每日总结 · %s" % (prefix, bj_str(now_bj(), "%Y-%m-%d"))
    sig = (ai_result or {}).get("signal") or {}
    q = _quality_text(snapshot, news, ai_result)
    prices = price_rows(snapshot)
    xau = next((p for p in prices if p["symbol"] == "XAU/USD"), None)
    folded = next((p for p in prices if p["symbol"] == "FOLDED_CNY_G"), None)
    overview = []
    for p in prices:
        overview.append("<li>%s %s %s，涨跌 %s（基准 %s，来源 %s，源报价时间 %s）</li>" %
                        (_e(p["name"]), _e(p["price"]), _e(p["unit"]), _e(_pct(p["change_pct"])),
                         _e(p["change_base"] or "—"), _e(p["source"]), _e(p["source_ts"] or "—")))
    drivers = sig.get("key_drivers", [])
    attribution = ("".join("<li><b>%s</b>（%s）：%s ｜ 依据 %s</li>" %
                           (_e(d.get("factor")), _e(d.get("stance")), _e(d.get("note")), _e(d.get("evidence_ref")))
                           for d in drivers)) or "<li>本次驱动归因数据不足，不做推测</li>"
    if folded:
        attribution += ("<li><b>汇率与国内口径</b>：折算参考价 %s 元/克（USD/CNY %s），"
                        "仅作参考、非银行成交价；国内克价与国际金价短期可能因汇率分化。</li>" %
                        (_e(folded["price"]), _e(snapshot.get("fx", {}).get("usd_cny") or "—")))
    body = (
        '<h3 style="font-size:14px;margin:16px 0 6px">1. 市场概览</h3><ul style="font-size:13px;padding-left:18px">' +
        ("".join(overview) or "<li>本次价格数据不可用</li>") + "</ul>" +
        '<h3 style="font-size:14px;margin:18px 0 4px">2. 消息面复盘</h3>' + _html_news(news) +
        '<h3 style="font-size:14px;margin:18px 0 4px">3. 驱动归因</h3><ul style="font-size:13px;padding-left:18px">' + attribution + "</ul>" +
        '<h3 style="font-size:14px;margin:18px 0 4px">4. 技术面观察</h3>'
        '<div style="font-size:13px;color:#5B6472">仅做趋势与位置描述，不给买卖点位。%s</div>' %
        ("当前国际现货 %s 美元/盎司，较本端上一快照 %s；单日波动不等于趋势反转，盘中插针与假期清淡时段易失真。"
         % (_e(xau["price"]), _e(_pct(xau["change_pct"]))) if xau else "本次价格数据不足，不输出技术面描述。") +
        '<h3 style="font-size:14px;margin:18px 0 4px">5. 今日关注</h3><ul style="font-size:13px;padding-left:18px">' +
        ("".join("<li>%s</li>" % _e(w) for w in sig.get("watch_points", [])) or
         "<li>关注下一轮采集的价格口径与源报价时间是否恢复</li>") + "</ul>" +
        '<h3 style="font-size:14px;margin:18px 0 4px">6. 信号回看</h3>'
        '<div style="font-size:13px;color:#5B6472">%s（仅作校准，不包装成胜率或准确率）</div>' % _e(signal_review or "暂无可回看的历史信号") +
        '<h3 style="font-size:14px;margin:18px 0 4px">7. 风险与配置框架</h3>'
        '<div style="font-size:13px;color:#B45309"><b>反向情形：</b>%s</div>'
        '<div style="font-size:13px;color:#5B6472;margin-top:4px">条件化情景：若上述观察条件发生变化，需重新评估驱动因子；'
        '本总结不提供买卖点位、仓位比例或收益承诺。</div>' % _e(sig.get("counter_case", "数据不足，无法给出反向情形")) +
        _quality_html(q))
    text = "\n".join([
        subject, "（准实时 2–5 分钟级 · 北京时间 %s）" % bj_str(now_bj()), "",
        "1. 市场概览", _text_price_block(snapshot), "",
        "2. 消息面复盘", _text_news(news), "",
        "3. 驱动归因", "；".join("%s:%s" % (d.get("factor"), d.get("note")) for d in drivers) or "数据不足，不做推测", "",
        "4. 技术面观察", "仅做趋势与位置描述，不给买卖点位；单日波动不等于趋势反转。", "",
        "5. 今日关注", "；".join(sig.get("watch_points", [])) or "关注下一轮数据是否恢复", "",
        "6. 信号回看", signal_review or "暂无可回看的历史信号", "（仅作校准，不包装成胜率）", "",
        "7. 风险与配置框架", "反向情形：" + str(sig.get("counter_case", "数据不足")),
        "条件化情景，不提供买卖点位、仓位比例或收益承诺。", "",
        "[数据质量提示]", ("\n".join("· " + x for x in q) if q else "无"), "", DISCLAIMER,
    ])
    return {"subject": subject, "html": _wrap(subject, body), "text_body": text,
            "report_type": "daily_summary", "report_date": bj_str(now_bj(), "%Y-%m-%d"),
            "generated_at": bj_str(now_bj())}
