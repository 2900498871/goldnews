# -*- coding: utf-8 -*-
"""每日新闻 / 每日资讯：采集 → AI 总结 → 每日 Markdown 文档 → 周期汇总 → 邮箱。

每日新闻（kind=news）：央视网要闻/国内/国际/社会/科技 + 微博/百度/抖音/B站/知乎热搜，
                     按栏目与平台排名列表展示，AI 归纳成日报。
每日资讯（kind=brief）：本端已采集的财经要闻（news_item 表）按日落成文档，AI 归纳当日要点。

文档一律落在本机（默认 数据目录/每日新闻、数据目录/每日资讯，可在设置里改），
每天一份 Markdown；周期汇总（每天 / 每月 / 每半年 / 每年 / 自定义天数）由 AI 对
区间内各日总结做二次归纳，写入「汇总」子目录，并按设置发送到邮箱。

只保存标题、摘要、链接、来源、时间，并把当天有正文的稿件全文抓下来存到本机
（SQLite 缓存 + 同目录的 YYYY-MM-DD.正文.html），界面上点标题即可看本机正文。
"""
from __future__ import annotations

import html as _html
import json
import os
import re
import threading
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

from .config import DIGEST_PERIOD_CN, digest_periods
from .util import DISCLAIMER_MD, bj_str, http_get, iso_bj, now_bj, short_hash

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

KINDS = {"news": {"name": "每日新闻", "folder": "每日新闻"},
         "brief": {"name": "每日资讯", "folder": "每日资讯"},
         "mil": {"name": "每日军事新闻", "folder": "每日军事新闻"}}
# 类型 → config.json 里的配置节名（每日新闻的配置节历史上叫 daily）
SECTION = {"news": "daily", "brief": "brief", "mil": "mil"}


def _sec(cfg: dict, kind: str) -> dict:
    return cfg.get(SECTION.get(kind, kind)) or {}


WEEK_CN = "一二三四五六日"

# 央视网栏目接口：news.cctv.com/2019/07/gaiban/cmsdatainterface/page/<栏目>_1.jsonp
# 注意路径是 cmsdatainterface（没有斜杠），旧写法 cms/datainterface 已 404
CCTV_URL = "https://news.cctv.com/2019/07/gaiban/cmsdatainterface/page/%s_1.jsonp"
CCTV_COLUMNS = [("news", "要闻"), ("china", "国内"), ("world", "国际"),
                ("society", "社会"), ("tech", "科技")]

HOT_SOURCES = [
    {"id": "weibo", "name": "微博热搜", "parser": "weibo",
     "url": "https://weibo.com/ajax/side/hotSearch",
     "headers": {"Referer": "https://weibo.com/", "Accept": "application/json"}},
    {"id": "baidu", "name": "百度热搜", "parser": "baidu",
     "url": "https://top.baidu.com/board?tab=realtime",
     "headers": {"Referer": "https://www.baidu.com/", "Accept-Language": "zh-CN,zh;q=0.9"}},
    {"id": "douyin", "name": "抖音热点榜", "parser": "douyin",
     "url": "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"},
    {"id": "bilibili", "name": "B站热搜", "parser": "bilibili",
     "url": "https://app.bilibili.com/x/v2/search/trending/ranking",
     "headers": {"Referer": "https://www.bilibili.com/"}},
    {"id": "zhihu", "name": "知乎热榜", "parser": "zhihu",
     "url": "https://api.zhihu.com/topstory/hot-list?limit=50",
     "headers": {"Referer": "https://www.zhihu.com/hot"}},
]

NEWS_DAY_SYSTEM = """你是新闻编辑助理，读者是一位关注黄金与宏观经济的中国个人投资者。
请根据给出的「央视新闻」与「互联网热搜」清单，写一份客观、简洁的中文 Markdown 日报。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息；材料里没有的不写。
2. 输出结构固定为三个二级标题：
## 今日要闻
- 4–6 条，每条一句话，覆盖国内、国际、社会、科技里最重要的内容；
## 财经与黄金相关线索
- 列出与宏观经济、美联储/利率/通胀、美元与汇率、地缘局势、央行购金、大宗商品相关的条目，并各用一句话说明为何值得关注；没有就写「今日清单中未见到直接相关内容」。只陈述事实，不预测价格，不给投资建议；
## 热搜观察
- 3–5 条，概括全网关注焦点，指出多个平台共同关注的话题。
3. 保持中立，只转述事实，不对政治人物、政策、国家或群体作褒贬评价。
4. 全文 350–600 字，直接输出正文，不要开场白和结尾客套。"""

BRIEF_DAY_SYSTEM = """你是财经资讯编辑助理，读者是一位关注黄金的个人投资者。
请根据给出的「当日财经要闻」清单（标题 / 摘要 / 来源），以及可能附带的「当日 AI 研判」结论，写一份客观、简洁的中文 Markdown 日报。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息。
2. 输出结构固定为以下二级标题：
## 今日要点
- 4–6 条，每条一句话；
## 与金价相关的线索
- 归纳与金价、美元、利率、通胀、地缘、央行购金、人民币汇率相关的条目及其含义；没有就如实说明。只陈述事实，不预测价格，不给投资建议；
## 当日研判回顾
- 仅当材料里有「当日 AI 研判」时输出：原样转述其中的走向与建议，不改写、不强化、不补充新的判断；没有就不写这一节。
3. 保持中立，只转述事实。全文 250–450 字，直接输出正文，不要开场白和结尾客套。"""

DIGEST_SYSTEM = """你是新闻编辑助理。下面是某一段时间内每一天的新闻摘要（按日期排列）。
请写一份客观、简洁的中文 Markdown 阶段综述。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息。
2. 输出结构固定为三个二级标题：
## 阶段综述
- 5–8 条，归纳这段时间的主要脉络与变化（哪些话题持续、哪些新增、哪些消退），尽量带出大致时间；
## 财经与黄金相关线索
- 归纳与宏观经济、美联储/利率/通胀、美元与汇率、地缘局势、央行购金相关的线索及其演变；没有就如实说明。只陈述事实，不预测价格，不给投资建议；
## 热点变化
- 3–5 条。
3. 保持中立，只转述事实，不对政治人物、政策、国家或群体作褒贬评价。
4. 全文 600–1000 字，直接输出正文，不要开场白和结尾客套。"""

DIGEST_MAP_SYSTEM = """你是新闻编辑助理。请把给出的这一段区间内的每日新闻摘要压缩成要点清单：
Markdown 无序列表，不超过 350 字，保留日期与关键事实、数字；只转述，不评价，不预测。直接输出正文。"""

CHUNK_CHARS = 9000      # 单次送入 AI 的素材上限（字符）
MAX_CHUNKS = 14         # 超长区间先分段归纳再合并，最多分这么多段

# ---- 每日军事新闻：国内军事（中国军网 / 人民网军事）+ 国际军事（央视国际按关键词过滤） ----
MIL_81_URL = "http://www.81.cn/yw_208727/index.html"
MIL_PEOPLE_URL = "http://military.people.com.cn/"
# 国际军事：从央视「要闻 / 国际」里按军事与地缘关键词挑，避免引入不稳定的第三方源
MIL_WORLD_KW = re.compile(
    r"军演|演习|导弹|航母|战机|战斗机|轰炸机|无人机|军舰|舰队|潜艇|核武|核试验|军费|国防|防务|"
    r"北约|五角大楼|美军|俄军|乌军|自卫队|空袭|停火|战事|军售|军工|驻军|阅兵|航展|军队|军事|"
    r"防空|雷达|武器出口|爱国者|胡塞武装|武装力量|国防预算|军事部署|战场|作战")
# 英文源：一般国际新闻（环球时报英文 / 法国24 / 联合国新闻等）按军事关键词筛
MIL_EN_KW = re.compile(
    r"\b(military|armed forces|defen[cs]e|army|navy|air force|troops|soldier|missile|"
    r"war|warfare|airstrike|air strike|drone|warship|aircraft carrier|submarine|nuclear|"
    r"artillery|ceasefire|truce|offensive|invasion|NATO|Pentagon|weapons|arms sale|"
    r"deployment|drill|combat|conflict|insurgent|militant|fighter jet|bomber|"
    r"air defen[cs]e|peacekeep\w*|hostage|border clash|military aid)\b", re.I)

MIL_DAY_SYSTEM = """你是军事新闻编辑助理，读者是一位只做黄金积存（银行积存金的定投与买卖）的个人投资者。
材料是当天采集的国内军事要闻与国际军事 / 地缘要闻清单（标题 + 摘要 + 来源），国际条目多为英文。请写一份客观、简短的中文 Markdown 日报。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息；材料里没有的不写。英文条目用中文转述，保留关键机构名（如 NATO、Pentagon 可写「北约」「五角大楼」）。
2. 输出结构固定为三个二级标题：
## 今日军事要闻
- 4–6 条，每条一句话；国内 2–3 条、国际 2–3 条，优先选官方发布、演习演训、装备进展、冲突与谈判；
## 地缘风险与黄金
- 3–4 条：哪些事件会通过「避险情绪 / 美元与美债 / 能源与航运 / 通胀预期」传导到金价，每条一句话说明传导路径；没有就写「今日清单中未见到能明显传导到金价的事件」；
## 对黄金积存买卖的参考
- 2–4 条：把上面的风险因素落到积存操作上，只用条件化表述（例如「若冲突进一步升级，短期避险买盘可能推升金价，可暂缓一次加仓」/「若只是例行演训，对积存节奏影响有限，按原计划定投」）。不给具体点位、不给仓位百分比、不下买卖指令；结论不确定时明确写「影响有限，按原计划执行」。
3. 保持中立：只转述事实，不评价政治制度，不对国家、军队、群体作褒贬，不渲染战争。
4. 全文 400–700 字，直接输出正文，不要开场白和结尾客套。"""

MIL_DIGEST_SYSTEM = """你是军事新闻编辑助理。下面是某一段时间内每一天的军事新闻摘要（按日期排列）。
请写一份客观、简短的中文 Markdown 阶段综述。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息。
2. 输出结构固定为三个二级标题：
## 阶段军事脉络
- 5–8 条，归纳这段时间国内外军事动态的主线（演习、装备、冲突与谈判等），尽量带出大致时间；
## 地缘风险演变
- 3–5 条，说明风险是升温、降温还是僵持，以及通过什么渠道影响市场；
## 对积存节奏的启示
- 2–4 条，只用条件化表述说明这段时间的军事风险对黄金积存定投节奏意味着什么，不给点位与仓位百分比，不下买卖指令。
3. 保持中立，只转述事实，不评价政治制度、不褒贬国家与军队。
4. 全文 600–1000 字，直接输出正文，不要开场白和结尾客套。"""

COMBINED_DAY_SYSTEM = """你是「黄金积存金助手」的编辑，读者是一位只做黄金积存（银行积存金定投与买卖）的个人投资者。
材料是今天已经分别生成的三份日报 AI 总结：每日新闻、每日资讯、每日军事新闻。请把它们整合成**一封**给读者看的邮件正文（Markdown）。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息；三份材料都没提到的不写。英文内容用中文转述。
2. 结构固定：
# 每日综合 · 日期（周几）
## 今日要点
- 3–5 条，跨三个板块挑出今天最值得知道的事，每条一句话；
## 国内与国际要闻
- 3–5 条，来自每日新闻；优先政策、经济数据、地缘与突发；
## 财经资讯与市场
- 3–5 条，来自每日资讯，说明对金价、美元、利率预期的含义；
## 军事动态
- 3–5 条，来自每日军事新闻；国内 1–2 条、国际 1–3 条；
## 对黄金积存的参考
- 2–4 条，只用条件化表述（「若……则……」），不给点位、不给仓位百分比、不下买卖指令；不确定时写「影响有限，按原计划执行」。
3. 三个板块不重复同一条内容；同一事件只出现在最相关的一节。
4. 全文 700–1100 字，直接输出正文，不要开场白和结尾客套。"""

COMBINED_DIGEST_SYSTEM = """你是「黄金积存金助手」的编辑。材料是同一时间区间内三个板块的周期汇总文档：每日新闻、每日资讯、每日军事新闻。
请整合成**一份**阶段综述邮件（Markdown）。规则：
1. 只依据材料归纳，不编造、不补充材料之外的信息。
2. 结构固定：
# 每日综合汇总 · 区间
## 阶段主线
- 4–6 条，跨板块归纳这段时间的主线，尽量带出大致时间；
## 新闻与政策
- 3–5 条；
## 财经与市场
- 3–5 条，说明对金价与利率预期的影响；
## 军事与地缘
- 3–4 条，说明风险升温/降温及传导渠道；
## 对积存节奏的启示
- 2–3 条，只用条件化表述，不给点位与仓位，不下买卖指令。
3. 三个板块不重复同一条内容。
4. 全文 800–1200 字，直接输出正文，不要开场白和结尾客套。"""


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------
def _clean(s) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    return re.sub(r"\s+", " ", s).strip()


def _jsonp(text: str):
    text = (text or "").strip().lstrip("\ufeff")
    m = re.match(r"^[\w$.]+\s*\((.*)\)\s*;?\s*$", text, re.S)
    return json.loads(m.group(1) if m else text)


def _p_cctv(text: str) -> list[dict]:
    lst = ((_jsonp(text).get("data") or {}).get("list") or [])
    out = []
    for a in lst:
        t = _clean(a.get("title"))
        if not t:
            continue
        out.append({"title": t[:240], "summary": _clean(a.get("brief"))[:300],
                    "url": a.get("url") or "", "time": str(a.get("focus_date") or ""),
                    "id": str(a.get("id") or short_hash(t))})
    return out


def _p_weibo(text: str) -> list[dict]:
    out = []
    for a in ((_jsonp(text).get("data") or {}).get("realtime") or []):
        w = _clean(a.get("word")).strip("#")
        if not w or a.get("is_ad"):
            continue
        out.append({"title": w, "hot": a.get("num"),
                    "url": "https://s.weibo.com/weibo?q=%23" + quote(w) + "%23"})
    return out


def _p_baidu(text: str) -> list[dict]:
    m = re.search(r"<!--s-data:(.*?)-->", text, re.S)
    if not m:
        return []
    raw = m.group(1)
    # 百度内嵌 JSON 里偶有 \x27 这类 JSON 不认的转义，先还原成字符再解析
    raw = re.sub(r"\\x([0-9a-fA-F]{2})", lambda x: chr(int(x.group(1), 16)), raw)
    raw = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", raw)
    d = json.loads(raw)
    # 新结构直接挂在 data 下；旧结构在 props.pageProps.data，两种都兼容
    data = d.get("data") or (((d.get("props") or {}).get("pageProps") or {}).get("data") or {})
    out = []
    for c in data.get("cards") or []:
        for a in c.get("content") or []:
            t = _clean(a.get("word"))
            if t:
                out.append({"title": t, "hot": a.get("hotScore"),
                            "url": a.get("url") or ("https://www.baidu.com/s?wd=" + quote(t))})
    return out


def _p_douyin(text: str) -> list[dict]:
    out = []
    for a in (_jsonp(text).get("word_list") or []):
        t = _clean(a.get("word"))
        if t:
            out.append({"title": t, "hot": a.get("hot_value"),
                        "url": "https://www.douyin.com/search/" + quote(t)})
    return out


def _p_bilibili(text: str) -> list[dict]:
    out = []
    for a in ((_jsonp(text).get("data") or {}).get("list") or []):
        t = _clean(a.get("show_name") or a.get("keyword"))
        if t:
            out.append({"title": t, "hot": a.get("heat_score"),
                        "url": "https://search.bilibili.com/all?keyword=" + quote(t)})
    return out


def _p_zhihu(text: str) -> list[dict]:
    out = []
    for a in (_jsonp(text).get("data") or []):
        tg = a.get("target") or {}
        t = _clean(tg.get("title"))
        if t:
            out.append({"title": t, "hot": None, "hot_text": _clean(a.get("detail_text")),
                        "url": "https://www.zhihu.com/question/" + str(tg.get("id") or ""),
                        "summary": _clean(tg.get("excerpt"))[:120]})
    return out


PARSERS = {"cctv": _p_cctv, "weibo": _p_weibo, "baidu": _p_baidu, "douyin": _p_douyin,
           "bilibili": _p_bilibili, "zhihu": _p_zhihu}


def _fetch(url: str, parser_name: str, headers: dict | None = None, timeout: float = 12.0):
    """返回 (条目列表, 状态文本)。任一源失败只影响自己。"""
    try:
        hd = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Encoding": "gzip"}
        hd.update(headers or {})
        st, text, _ = http_get(url, timeout=timeout, headers=hd)
        if st != 200 or not (text or "").strip():
            return [], "HTTP %s / 空响应" % st
        items = PARSERS[parser_name](text)
        return items, "ok" if items else "ok（0 条，页面结构可能已变化）"
    except Exception as e:
        return [], "%s: %s" % (type(e).__name__, str(e)[:100])


def collect_daily(cfg: dict, day: str) -> tuple[list[dict], dict, dict]:
    """并发采集央视各栏目与各热搜平台。

    返回 (条目, 源状态, 统计)。条目字段：src / src_name / rank / title / summary / url /
    hot_value / published_at_bj / dedup_hash。
    """
    sec = cfg.get("daily") or {}
    jobs = []
    if sec.get("cctv_enabled", True):
        limit = max(1, min(100, int(sec.get("cctv_max_items") or 20)))
        for cid, cname in CCTV_COLUMNS:
            jobs.append(("cctv_" + cid, cname, "cctv", CCTV_URL % cid, limit,
                         {"Referer": "https://news.cctv.com/"}))
    hot_on = sec.get("hot_sources") or {}
    if sec.get("hot_enabled", True):
        hlimit = max(1, min(50, int(sec.get("hot_max_items") or 10)))
        for s in HOT_SOURCES:
            if hot_on.get(s["id"], True):
                jobs.append((s["id"], s["name"], s["parser"], s["url"], hlimit, s.get("headers")))
    items, status = [], {}
    if not jobs:
        return [], {"note": "央视新闻与热搜采集均已关闭"}, {"cctv": 0, "hot": 0, "hot_sources": []}
    with ThreadPoolExecutor(max_workers=min(10, len(jobs))) as ex:
        futs = {ex.submit(_fetch, url, parser, hd): (sid, sname, limit)
                for sid, sname, parser, url, limit, hd in jobs}
        for f in as_completed(futs):
            sid, sname, limit = futs[f]
            try:
                got, st = f.result()
            except Exception as e:
                got, st = [], "异常: %s" % str(e)[:80]
            got = got[:limit]
            status[sid] = ("%s：%s" % (sname, st)) if not st.startswith("ok") else \
                "%s：ok（%d 条）" % (sname, len(got))
            if sid.startswith("cctv_"):
                status[sid] = "央视·" + status[sid]
            for i, it in enumerate(got, 1):
                pub = ""
                if sid.startswith("cctv_") and it.get("time"):
                    pub = it["time"][:16]
                dedup = ("cctv|" + it["id"]) if it.get("id") else \
                    short_hash("%s|%s|%s|%s" % (day, sid, i, it["title"]))
                items.append({"src": sid, "src_name": sname, "rank": i, "title": it["title"],
                              "summary": it.get("summary") or "", "url": it.get("url") or "",
                              "hot_value": _int(it.get("hot")), "hot_text": it.get("hot_text") or "",
                              "published_at_bj": pub, "dedup_hash": dedup})
    cctv = [x for x in items if x["src"].startswith("cctv_")]
    hot = [x for x in items if not x["src"].startswith("cctv_")]
    # 央视只保留当天稿件：接口里混有前一天晚间的稿子，按发布日过滤
    cctv = [x for x in cctv if x["published_at_bj"][:10] == day]
    # 过滤掉隔日稿件后序号会跳号，按栏目重新从 1 编号
    for key in {x["src"] for x in cctv}:
        for i, x in enumerate([y for y in cctv if y["src"] == key], 1):
            x["rank"] = i
    items = cctv + hot
    stats = {"cctv": len(cctv), "hot": len(hot),
             "hot_sources": sorted({x["src"] for x in hot}),
             "cctv_columns": sorted({x["src"][5:] for x in cctv})}
    return items, status, stats


def _int(v):
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# 军事新闻采集：中国军网 / 人民网军事（国内）+ 央视国际（国际，关键词筛）
# --------------------------------------------------------------------------
def _p_81(html_text: str) -> list[dict]:
    """中国军网要闻列表：/yw_208727/<id>.html；标题后面跟着发布时间，偶尔夹带「多媒体稿件…」。"""
    out, seen = [], set()
    for href, raw in re.findall(r'href="(http://www\.81\.cn/yw_\d+/\d+\.html)"[^>]*>(.*?)</a>',
                                html_text, re.S):
        s = _clean(re.sub(r"<[^>]+>", " ", raw))
        s = re.sub(r"多媒体|阅读全文.{0,10}|稿件", " ", s)
        m = re.search(r"(\d{2,4})-(\d{2})-(\d{2})(?:\s*(\d{1,2}:\d{2}))?", s)
        title = (s[:m.start()] if m else s).strip(" ，,|·、")
        if len(title) < 6 or title in seen:
            continue
        seen.add(title)
        pub = ""
        if m:
            y = m.group(1) if len(m.group(1)) == 4 else "20" + m.group(1)
            pub = "%s-%s-%s" % (y, m.group(2), m.group(3))
            if m.group(4):
                pub += " " + m.group(4)
        out.append({"title": title[:240], "url": href, "time": pub})
    return out


def _p_people(html_text: str) -> list[dict]:
    """人民网军事：/n1/YYYY/MMDD/c....html，日期可以从链接里取。"""
    out, seen = [], set()
    for href, raw in re.findall(
            r'href="(http://military\.people\.com\.cn/n1/\d{4}/\d{4}/[^"]+\.html)"[^>]*>(.*?)</a>',
            html_text, re.S):
        title = _clean(re.sub(r"<[^>]+>", " ", raw)).strip(" |·")
        if len(title) < 6 or title in seen:
            continue
        seen.add(title)
        m = re.search(r"/n1/(\d{4})/(\d{2})(\d{2})/", href)
        pub = "%s-%s-%s" % (m.group(1), m.group(2), m.group(3)) if m else ""
        out.append({"title": title[:240], "url": href, "time": pub})
    return out


def _p_rss(text: str) -> list[dict]:
    """通用 RSS / Atom 解析：title / link / description / pubDate。"""
    out = []
    for b in (re.findall(r"<item[\s>].*?</item>", text or "", re.S) or
              re.findall(r"<entry[\s>].*?</entry>", text or "", re.S)):
        t = re.search(r"<title[^>]*>(.*?)</title>", b, re.S)
        if not t:
            continue
        title = _clean(_html.unescape(re.sub(r"<![CDATA[|]]>", "", t.group(1))))
        if len(title) < 8:
            continue
        l = re.search(r"<link>(.*?)</link>", b, re.S)
        if not l:   # Atom：优先 rel="alternate"，排除正文里混进来的 stylesheet 之类
            l = re.search(r'<link[^>]*rel="alternate"[^>]*href="([^"]+)"', b) or \
                re.search(r'<link(?![^>]*stylesheet)[^>]*href="([^"]+)"', b)
        link = _clean(l.group(1)) if l else ""
        if link and not link.startswith("http"):
            link = ""
        d = re.search(r"<description[^>]*>(.*?)</description>", b, re.S) or \
            re.search(r"<summary[^>]*>(.*?)</summary>", b, re.S)
        summary = ""
        if d:
            summary = _clean(_html.unescape(re.sub(r"<![CDATA[|]]>", "", d.group(1))))
            summary = re.sub(r"The post .*?appeared first on .*$", "", summary, flags=re.S).strip()
        p = (re.search(r"<pubDate>(.*?)</pubDate>", b, re.S) or
             re.search(r"<published>(.*?)</published>", b, re.S) or
             re.search(r"<updated>(.*?)</updated>", b, re.S) or
             re.search(r"<dc:date>(.*?)</dc:date>", b, re.S))
        out.append({"title": _html.unescape(title)[:240], "summary": summary[:200],
                    "url": link, "time": _rss_time(_clean(p.group(1)) if p else "")})
    return out


def _rss_time(s: str) -> str:
    """RSS 时间统一成北京时间 YYYY-MM-DD HH:MM；解析不了返回空串。"""
    if not s:
        return ""
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


# 国际军事：军事垂直站（直接要闻，不做关键词筛）+ 综合国际源（按英文军事关键词筛）
# 路透 / 美联在本机网络不可达（超时 / 403），默认关闭，网络恢复时在设置里打开即可。
MIL_RSS_SOURCES = [
    {"id": "mil_defensenews", "name": "Defense News（美国·防务新闻）", "kind": "mil",
     "url": "https://www.defensenews.com/arc/outboundfeeds/rss/?outputType=xml", "limit": 10},
    {"id": "mil_defenseone", "name": "Defense One（美国·防务一号）", "kind": "mil",
     "url": "https://www.defenseone.com/rss/all/", "limit": 10},
    {"id": "mil_militarytimes", "name": "Military Times（美国·军事时报）", "kind": "mil",
     "url": "https://www.militarytimes.com/arc/outboundfeeds/rss/?outputType=xml", "limit": 8},
    {"id": "mil_airforcetimes", "name": "Air Force Times（美国·空军时报）", "kind": "mil",
     "url": "https://www.airforcetimes.com/arc/outboundfeeds/rss/?outputType=xml", "limit": 6},
    {"id": "mil_navalnews", "name": "Naval News（海军新闻）", "kind": "mil",
     "url": "https://www.navalnews.com/feed/", "limit": 6},
    {"id": "mil_taskpurpose", "name": "Task & Purpose（任务与目的）", "kind": "mil",
     "url": "https://taskandpurpose.com/feed/", "limit": 6},
    {"id": "mil_twz", "name": "The War Zone（战区）", "kind": "mil",
     "url": "https://www.twz.com/feed", "limit": 8},
    {"id": "mil_defensescoop", "name": "DefenseScoop（美国·国防采购）", "kind": "mil",
     "url": "https://defensescoop.com/feed/", "limit": 6},
    {"id": "mil_airandspace", "name": "Air & Space Forces（空天军杂志）", "kind": "mil",
     "url": "https://www.airandspaceforces.com/feed/", "limit": 6},
    {"id": "mil_armytimes", "name": "Army Times（美国·陆军时报）", "kind": "mil",
     "url": "https://www.armytimes.com/arc/outboundfeeds/rss/?outputType=xml", "limit": 6},
    {"id": "mil_navytimes", "name": "Navy Times（美国·海军时报）", "kind": "mil",
     "url": "https://www.navytimes.com/arc/outboundfeeds/rss/?outputType=xml", "limit": 6},
    {"id": "mil_marinecorpstimes", "name": "Marine Corps Times（美国·陆战队时报）", "kind": "mil",
     "url": "https://www.marinecorpstimes.com/arc/outboundfeeds/rss/?outputType=xml", "limit": 6},
    {"id": "mil_globaltimes", "name": "Global Times（环球时报·英文）", "kind": "world",
     "url": "https://www.globaltimes.cn/rss/outbrain.xml", "limit": 8},
    {"id": "mil_france24", "name": "France 24（法国国际）", "kind": "world",
     "url": "https://www.france24.com/en/rss", "limit": 4},
    {"id": "mil_un", "name": "UN News（联合国新闻）", "kind": "world",
     "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml", "limit": 4},
    {"id": "mil_reuters", "name": "Reuters World（路透·国际）", "kind": "world", "enabled": False,
     "url": "https://www.reuters.com/arc/outboundfeeds/rss/category/world/?outputType=xml", "limit": 8},
    {"id": "mil_ap", "name": "AP News（美联社）", "kind": "world", "enabled": False,
     "url": "https://apnews.com/index.rss", "limit": 8},
]
# 归属「国际军事」的源（含央视国际）
MIL_WORLD_SRCS = {"mil_cctv_intl"} | {s["id"] for s in MIL_RSS_SOURCES}


MIL_HTML_SOURCES = [
    {"id": "mil_81", "name": "中国军网 · 要闻", "url": MIL_81_URL, "parser": _p_81,
     "headers": {"Referer": "http://www.81.cn/"}, "limit": 20},
    {"id": "mil_people", "name": "人民网 · 军事", "url": MIL_PEOPLE_URL, "parser": _p_people,
     "headers": {"Referer": "http://military.people.com.cn/"}, "limit": 12},
    {"id": "mil_cctv", "name": "央视 · 军事频道", "url": "https://military.cctv.com/data/index.json",
     "parser": "_p_cctv_mil_index", "headers": {"Referer": "https://military.cctv.com/"}, "limit": 12},
]


def _p_cctv_mil_index(text: str) -> list[dict]:
    """央视军事频道首页的收稿列表（data/index.json）：title / description / dateTime / url。"""
    d = _jsonp(text) or {}
    out = []
    for a in (d.get("rollData") or []):
        t = _clean(a.get("title"))
        if not t:
            continue
        out.append({"title": t[:240], "summary": _clean(a.get("description"))[:300],
                    "url": a.get("url") or "", "time": str(a.get("dateTime") or ""),
                    "id": str(a.get("id") or short_hash(t))})
    return out


def _cctv_military(day: str, limit: int = 10) -> list[dict]:
    """从央视「要闻 / 国际」里挑与军事、地缘冲突相关的稿件（零新源，复用已通过验证的接口）。"""
    packs, out = [], []
    for cid in ("news", "world"):
        try:
            text = http_get(CCTV_URL % cid, timeout=10,
                            headers={"User-Agent": BROWSER_UA, "Referer": "https://news.cctv.com/",
                                     "Accept": "*/*"})[1]
            packs.append(PARSERS["cctv"](text))
        except Exception:
            continue
    seen = set()
    for pack in packs:
        for it in pack:
            t = (it.get("title") or "") + " " + (it.get("summary") or "")
            if not MIL_WORLD_KW.search(t) or it["title"] in seen:
                continue
            pub = (it.get("time") or "")[:16]
            if pub[:10] and pub[:10] != day:   # 只留当天稿，避免旧闻混进日报
                continue
            seen.add(it["title"])
            out.append({"title": it["title"], "summary": it.get("summary") or "",
                        "url": it.get("url") or "", "time": pub, "id": it.get("id")})
    return out[:limit]


def collect_military(cfg: dict, day: str) -> tuple[list[dict], dict, dict]:
    """采集军事要闻：国内（军网 / 人民网 / 央视军事）+ 国际（央视国际 + 军事垂直站 RSS）。"""
    sec = cfg.get("mil") or {}
    limit = max(1, min(240, int(sec.get("max_items") or 60)))
    per_src = max(4, limit // 3)
    rss_sources = [s for s in MIL_RSS_SOURCES if s.get("enabled", True)]

    def _one(s: dict):
        try:
            hd = {"User-Agent": BROWSER_UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
            hd.update(s.get("headers") or {})
            st, text, _ = http_get(s["url"], timeout=12, headers=hd)
            if st != 200 or not (text or "").strip():
                return [], "HTTP %s / 空响应" % st
            got = s["parser"](text) if callable(s["parser"]) else globals()[s["parser"]](text)
            return got, "ok" if got else "ok（0 条，页面结构可能已变化）"
        except Exception as e:
            return [], "%s: %s" % (type(e).__name__, str(e)[:100])

    def _one_rss(s: dict):
        try:
            st, text, _ = http_get(s["url"], timeout=10, headers={
                "User-Agent": BROWSER_UA, "Accept": "application/rss+xml,application/xml,text/xml,*/*",
                "Accept-Language": "en-US,en;q=0.9"})
            if st != 200 or not (text or "").strip():
                return [], "HTTP %s / 空响应" % st
            got = _p_rss(text)
            if s.get("kind") == "world":      # 综合国际源只认标题里的军事词，避免靠摘要误命中
                got = [x for x in got if MIL_EN_KW.search(x["title"])]
            return got, "ok" if got else "ok（0 条，页面结构可能已变化）"
        except Exception as e:
            return [], "%s: %s" % (type(e).__name__, str(e)[:100])

    dom, wld, status = [], [], {}
    try:
        yday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        yday = day

    def _fresh(got, strict=False):
        # 列表页 / RSS 里可能混着更早的稿子：有发布时间的只留近期稿件，避免旧闻混进日报。
        # HTML 列表页：今天 / 昨天；RSS（strict）：近 3 天——欧美站点按美东时间发稿，
        # 北京时间早上对应它们的「昨天」，只留 2 天会把大部分刚发的稿件筛掉。
        if strict:
            try:
                oldest = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=2)).strftime("%Y-%m-%d")
            except ValueError:
                oldest = yday
            return [x for x in got if not (x.get("time") or "")[:10] or (x.get("time") or "")[:10] >= oldest]
        fresh = [x for x in got if not (x.get("time") or "")[:10] or (x.get("time") or "")[:10] in (day, yday)]
        return fresh or got

    with ThreadPoolExecutor(max_workers=len(MIL_HTML_SOURCES) + len(rss_sources) + 1) as ex:
        futs = {ex.submit(_one, s): ("dom", s) for s in MIL_HTML_SOURCES}
        for s in rss_sources:                 # 军事垂直站归国际军事
            futs[ex.submit(_one_rss, s)] = ("wld", s)
        fut_world = ex.submit(_cctv_military, day, per_src)
        for f in as_completed(futs):
            where, s = futs[f]
            try:
                got, st = f.result()
            except Exception as e:
                got, st = [], "异常: %s" % str(e)[:80]
            got = got[:min(s.get("limit") or 12, per_src if where == "wld" else max(per_src, 15))]
            got = _fresh(got, strict=(where == "wld"))
            status[s["id"]] = "%s：%s" % (s["name"], "ok（%d 条）" % len(got) if st.startswith("ok") else st)
            bucket = dom if where == "dom" else wld
            for i, it in enumerate(got, 1):
                bucket.append({"src": s["id"], "src_name": s["name"], "rank": i,
                               "title": it["title"], "summary": it.get("summary") or "",
                               "url": it["url"], "published_at_bj": it.get("time") or "",
                               "dedup_hash": short_hash("%s|%s" % (day, it["url"] or it["title"])),
                               "hot_value": None, "hot_text": ""})
        try:
            world, st_world = fut_world.result(), None
            status["mil_cctv_intl"] = "央视 · 国际军事：ok（%d 条）" % len(world)
        except Exception as e:
            world = []
            status["mil_cctv_intl"] = "央视 · 国际军事：异常: %s" % str(e)[:80]
        for i, it in enumerate(_fresh(world), 1):
            wld.insert(i - 1, {"src": "mil_cctv_intl", "src_name": "央视 · 国际军事", "rank": i,
                               "title": it["title"], "summary": it.get("summary") or "", "url": it["url"],
                               "published_at_bj": it.get("time") or "",
                               "dedup_hash": "milcctv|" + (it.get("id") or short_hash(it["title"])),
                               "hot_value": None, "hot_text": ""})

    # 国内外都要占位：先给国内一半额度，国际把剩下的吃掉，国际不够再由国内补满
    items, seen_t, seen_title = [], set(), set()

    def _take(lst, cap):
        n = 0
        for x in lst:
            if n >= cap or len(items) >= limit:
                break
            key = (x["url"] or "") or x["title"]
            # 同一篇稿件常被 Military Times / Army Times / Marine Corps Times 等同步转载，
            # 链接不同但标题一样，按标题去一次重
            nt = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (x["title"] or "").lower())[:60]
            if key in seen_t or (nt and nt in seen_title):
                continue
            seen_t.add(key)
            if nt:
                seen_title.add(nt)
            items.append(x)
            n += 1

    _take(dom, max(1, limit // 2))
    _take(wld, limit)
    _take(dom, limit)
    stats = {"mil_domestic": len([x for x in items if x["src"] not in MIL_WORLD_SRCS]),
             "mil_world": len([x for x in items if x["src"] in MIL_WORLD_SRCS]),
             "sources": sorted({x["src"] for x in items})}
    return items, status, stats


# --------------------------------------------------------------------------
# Markdown 渲染
# --------------------------------------------------------------------------
def _link(title: str, url: str) -> str:
    t = str(title or "").replace("[", "【").replace("]", "】").strip()
    return "[%s](%s)" % (t, url) if url else t


def _weekday(day: str) -> str:
    try:
        return "周" + WEEK_CN[datetime.strptime(day, "%Y-%m-%d").weekday()]
    except ValueError:
        return ""


def _period_label(per: str, start: date, end: date) -> str:
    if per == "1m":
        last = monthrange(start.year, start.month)[1]
        if start.day > 1 or end.day < last:
            return "本月至今（%d月%d日–%d月%d日）" % (start.month, start.day, end.month, end.day)
        return "%d 年 %d 月月报" % (start.year, start.month)
    if per == "1w":
        if (end - start).days + 1 < 7:
            return "本周至今（%d月%d日–%d月%d日）" % (start.month, start.day, end.month, end.day)
        return "%d 年 %d 月 %d 日至 %d 月 %d 日周报" % (
            start.year, start.month, start.day, end.month, end.day)
    if per == "6m":
        if start.month not in (1, 7) or start.day != 1:
            return "%s 至 %s 阶段汇总" % (start.isoformat(), end.isoformat())
        return "%d 年%s半年报" % (start.year, "上" if start.month == 1 else "下")
    if per == "6m":
        return "%d 年%s半年报" % (start.year, "上" if start.month == 1 else "下")
    if per == "1y":
        return "%d 年年报" % start.year
    if per == "custom":
        return "%s 至 %s 阶段汇总" % (start.isoformat(), end.isoformat())
    return "%s 日报" % start.isoformat()


_PERIOD_FILE = {"1d": "日报", "1w": "周报", "1m": "月报", "6m": "半年报", "1y": "年报", "custom": "阶段汇总"}


def _demote(md: str) -> str:
    """AI 总结里的 ## 标题落在文档「AI 总结」一节之下，整体降一级，层级才对。"""
    return re.sub(r"^(#{1,5})\s+", r"#\1 ", md or "", flags=re.M)


def _reason_text(s: str) -> str:
    """把接口报错压成一句人话：去掉 JSON 细节，也不把密钥片段写进文档。"""
    raw = (s or "").strip()
    if not raw:
        return "未知原因"
    code = re.search(r"HTTP\s*(\d{3})", raw)
    head = re.sub(r"[{（(].*", "", raw).strip() or "DeepSeek 调用失败"
    head = re.sub(r"(?i)(your\s+)?api\s*key[^,，;；]*", "密钥", head)
    head = re.sub(r"HTTP\s*\d{3}", "", head).strip(" ：:，,、-")
    return (head + ("（HTTP %s）" % code.group(1) if code else ""))[:120]


def _drop_empty_review(md: str) -> str:
    """没有当日研判时，去掉模型仍然写出的「材料中未附…」那一节。"""
    return re.sub(r"\n*#{2,3}\s*当日研判回顾\s*\n.*?(?=\n#{2,3}\s|\Z)", "", md or "", flags=re.S).strip()


# ---------- 正文抓取：存到本机，列表点标题就能看全文 ----------
ARTICLE_MAX_CHARS = 8000      # 单篇正文最多留多少字
ARTICLE_RUN_LIMIT = 40        # 单次每日生成最多抓多少篇（已缓存的直接复用）
ARTICLE_WORKERS = 6
ARTICLE_TIMEOUT = 12.0
ART_NAME = "正文"

# 这些来源给的是搜索页或需要登录，抓不到正文：标 skip 以后不再重试
_NO_ARTICLE_HOSTS = ("zhihu.com", "weibo.com", "baidu.com", "douyin.com", "iesdouyin.com",
                     "bilibili.com", "sogou.com", "toutiao.com", "kuaishou.com", "xiaohongshu.com")

_TAIL_MARKS = ("责任编辑", "编辑：", "编辑:", "校对", "相关阅读", "热门推荐", "热门新闻", "相关新闻",
               "推荐阅读", "网友评论", "分享到", "扫码", "扫一扫", "客户端下载", "版权", "免责声明",
               "【纠错】", "更多精彩", "广告", "为你推荐", "延伸阅读")
_LEAD_JUNK = ("下载新浪财经", "下载客户端", "登录后", "海量资讯", "新浪财经APP", "随时随地",
              "关注公众号", "点击查看", "查看原图", "视频加载", "原标题：本文")


def article_key(url: str) -> str:
    """正文缓存的键：链接哈希，同一篇跨天只存一份。"""
    return short_hash((url or "").strip())


def _art_unsupported(url: str) -> bool:
    try:
        u = urlsplit(url)
    except Exception:
        return True
    if u.scheme not in ("http", "https"):
        return True
    host = (u.netloc or "").lower()
    return any(h in host for h in _NO_ARTICLE_HOSTS)


def _clean_para(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s)
    s = re.sub(r"\[!--.*?--\]", "", s)                       # 编辑器注释标记
    s = re.sub(r"[0-9a-fA-F]{32},0,1,\d+:\d+,newPlayer", "", s)  # 视频播放器占位
    s = _html.unescape(s).replace("\u3000", " ").replace("\xa0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", s).strip()


# 央视部分稿件不在页面里写 <p>，而是把这些段落塞进 <script> 的 contentdate 变量
_JS_BODY_RE = re.compile(r"(?is)var\s+contentdate\s*=\s*'((?:[^'\\]|\\.)*)'")


def _js_unescape(s: str) -> str:
    s = s.replace("\\'", "'").replace('\\"', '"').replace("\\/", "/")
    s = s.replace("\\r", "").replace("\\n", "\n").replace("\\t", " ")
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    return s.replace("\\\\", "\\")


def _paras(fragment: str) -> str:
    """合并片段里可读的 <p>，遇页尾/推荐位就停，避免把别栏内容混进正文。"""
    paras, total = [], 0
    for raw in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", fragment or ""):
        p = _clean_para(raw)
        if len(p) < 12:
            continue
        if any(p.startswith(j) for j in _LEAD_JUNK):
            continue
        if len(paras) >= 3 and any(p.startswith(m) for m in _TAIL_MARKS):
            break
        if paras and paras[-1] == p:
            continue
        paras.append(p)
        total += len(p)
        if total >= ARTICLE_MAX_CHARS:
            break
    return "\n\n".join(paras)[:ARTICLE_MAX_CHARS].strip()


def extract_article(html_text: str) -> str:
    """从稿件页抽正文。

    两种写法都覆盖：正文在普通 <p> 里（新浪、英文财经站、部分央视页），
    或正文被塞进 <script> 的 contentdate 变量再由前端注入（央视另一类稿件）。
    """
    html_text = html_text or ""
    m = _JS_BODY_RE.search(html_text)
    if m:
        text = _paras(_js_unescape(m.group(1)))
        if len(text) >= 60:
            return text
    body = re.sub(r"(?is)<(script|style|noscript|iframe|svg|head)\b[^>]*>.*?</\1>", "", html_text)
    body = re.sub(r"(?s)<!--.*?-->", "", body)
    return _paras(body)


def fetch_article(url: str, timeout: float = ARTICLE_TIMEOUT) -> dict:
    """抓一篇正文，返回 {status: ok|skip|fail, content, chars, reason}。"""
    if not url:
        return {"status": "skip", "content": "", "chars": 0, "reason": "这条没有原文链接"}
    if _art_unsupported(url):
        return {"status": "skip", "content": "", "chars": 0,
                "reason": "该来源给的是搜索页或需登录，抓不到正文"}
    try:
        st, text, _ = http_get(url, timeout=timeout, headers={"User-Agent": BROWSER_UA})
    except Exception as e:
        return {"status": "fail", "content": "", "chars": 0,
                "reason": "%s: %s" % (type(e).__name__, str(e)[:60])}
    if st != 200:
        return {"status": "fail", "content": "", "chars": 0, "reason": "HTTP %s" % st}
    content = extract_article(text)
    if len(content) < 60:
        # 图片图集页（photo.cctv.com）本来就没有正文，标 skip 不再重试
        if "photo." in (urlsplit(url).netloc or ""):
            return {"status": "skip", "content": "", "chars": 0, "reason": "这是图片图集，没有正文"}
        return {"status": "fail", "content": "", "chars": 0,
                "reason": "页面里没识别出正文（多为视频页或需要跳转）"}
    return {"status": "ok", "content": content, "chars": len(content), "reason": ""}


def _anchor_map(rows: list[dict], amap: dict) -> dict:
    """按列表顺序给有正文的条目编号，Markdown 与正文 HTML 共用这套锚点 #aN。"""
    out, n = {}, 0
    for r in rows:
        url = r.get("url") or ""
        if not url:
            continue
        c = amap.get(article_key(url)) or {}
        if c.get("status") == "ok" and (c.get("content") or ""):
            n += 1
            out[article_key(url)] = n
    return out


def render_news_doc(day: str, rows: list[dict], ai: dict, stats: dict, generated_at: str,
                    amap: dict | None = None, art_name: str = "") -> str:
    anchors = _anchor_map(rows, amap or {})
    cctv_cols = {s: n for s, n in CCTV_COLUMNS}
    hot_names = {s["id"]: s["name"] for s in HOT_SOURCES}
    cols = [c for c, _ in CCTV_COLUMNS if any(r["src"] == "cctv_" + c for r in rows)]
    hots = [h for h in hot_names if any(r["src"] == h for r in rows)]
    L = ["# 每日新闻 · %s（%s）" % (day, _weekday(day)), "",
         "> 采集时间：%s ｜ 央视新闻 %d 条 · 热搜 %d 条" % (
             bj_str(datetime.fromisoformat(generated_at), "%Y-%m-%d %H:%M:%S") if generated_at else "—",
             stats.get("cctv", 0), stats.get("hot", 0)),
         "> 来源：央视网（%s）；%s" % (" / ".join(cctv_cols[c] for c in cols) or "未采集",
                                     " / ".join(hot_names[h] for h in hots) or "未采集"),
         "> 程序自动采集整理；央视稿件正文已抓取存到本机，界面里点条目标题即可看全文。"
         "AI 总结由 DeepSeek 生成，仅归纳事实，不构成投资建议。"]
    if art_name and anchors:
        L.append("> 全文文件 `%s`（共 %d 篇，条目后的「正文 #aN」就是该文件里的第 N 篇）。"
                 % (art_name, len(anchors)))
    L += ["", "## AI 总结", ""]
    ai_text = _demote((ai.get("text") or "").strip())
    L.append(ai_text or "> 本次未生成 AI 总结：%s" % (ai.get("reason") or "未知原因"))
    L.append("")
    L += ["## 央视新闻", ""]
    for c, cname in CCTV_COLUMNS:
        part = [r for r in rows if r["src"] == "cctv_" + c]
        if not part:
            continue
        L += ["### " + cname, ""]
        for r in part:
            hhmm = (r.get("published_at_bj") or "")[11:16]
            line = "%d. %s%s" % (r["rank"], _link(r["title"], r["url"]),
                                 "（%s）" % hhmm if hhmm else "")
            if r.get("summary"):
                line += " — %s" % r["summary"]
            an = anchors.get(article_key(r.get("url") or ""))
            if art_name and an:
                line += " ｜ 正文 #a%d" % an
            L.append(line)
        L.append("")
    L += ["## 互联网热搜", ""]
    for h in HOT_SOURCES:
        part = [r for r in rows if r["src"] == h["id"]]
        if not part:
            continue
        L += ["### " + h["name"], ""]
        for r in part:
            line = "%d. %s" % (r["rank"], _link(r["title"], r["url"]))
            hv = r.get("hot_text") or (("%s 万" % round(r["hot_value"] / 10000, 1))
                                       if r.get("hot_value") and r["hot_value"] >= 10000
                                       else r.get("hot_value"))
            if hv:
                line += " · 热度 %s" % hv
            L.append(line)
        L.append("")
    if not any(r["src"].startswith("cctv_") for r in rows):
        L.insert(10, "> 今日未采集到央视新闻（已关闭、未到发布时段或接口异常）。")
    L += ["---", "", DISCLAIMER_MD, ""]
    return "\n".join(L)


MIL_SRC_ORDER = [("mil_81", "中国军网 · 要闻", "domestic"),
                 ("mil_people", "人民网 · 军事", "domestic"),
                 ("mil_cctv", "央视 · 军事频道", "mix"),
                 ("mil_cctv_intl", "央视 · 国际军事", "world")] + \
                [(s["id"], s["name"], "world") for s in MIL_RSS_SOURCES]


def render_mil_doc(day: str, rows: list[dict], ai: dict, stats: dict, generated_at: str,
                   amap: dict | None = None, art_name: str = "") -> str:
    anchors = _anchor_map(rows, amap or {})
    L = ["# 每日军事新闻 · %s（%s）" % (day, _weekday(day)), "",
         "> 采集时间：%s ｜ 国内军事 %d 条 · 国际军事 %d 条" % (
             bj_str(datetime.fromisoformat(generated_at), "%Y-%m-%d %H:%M:%S") if generated_at else "—",
             stats.get("mil_domestic", 0), stats.get("mil_world", 0)),
         "> 来源：国内 = 中国军网（要闻）、人民网军事、央视军事；国际 = 央视国际（按军事与地缘关键词筛选）、"
         "Defense News / Defense One / Military Times / Air Force Times / Naval News / Task & Purpose、"
         "Global Times 英文版、France 24、UN News（后三家按英文军事关键词筛）。"
         "程序自动采集整理，正文已抓取存到本机，界面里点条目标题即可看全文；"
         "AI 总结由 DeepSeek 生成，只做事实归纳与条件化提示，不构成投资建议。"]
    if art_name and anchors:
        L.append("> 全文文件 `%s`（共 %d 篇，条目后的「正文 #aN」就是该文件里的第 N 篇）。"
                 % (art_name, len(anchors)))
    L += ["", "## AI 总结", ""]
    L.append(_demote((ai.get("text") or "").strip()) or
             "> 本次未生成 AI 总结：%s" % (ai.get("reason") or "未知原因"))
    L.append("")
    for group, title in (("domestic", "国内军事"), ("mix", "央视军事（含国际）"), ("world", "国际军事")):
        part = [r for r in rows if dict((s[0], s[2]) for s in MIL_SRC_ORDER).get(r["src"]) == group]
        if not part:
            continue
        L += ["## " + title, ""]
        for sid, sname, g in MIL_SRC_ORDER:
            if g != group:
                continue
            sub = [r for r in part if r["src"] == sid]
            if not sub:
                continue
            L += ["### " + sname, ""]
            for r in sub:
                hhmm = (r.get("published_at_bj") or "")[11:16]
                date10 = (r.get("published_at_bj") or "")[:10]
                stamp = hhmm or (date10 if date10 and date10 != day else "")
                line = "%d. %s%s" % (r["rank"], _link(r["title"], r["url"]),
                                     "（%s）" % stamp if stamp else "")
                if r.get("summary"):
                    line += " — %s" % r["summary"][:120]
                an = anchors.get(article_key(r.get("url") or ""))
                if art_name and an:
                    line += " ｜ 正文 #a%d" % an
                L.append(line)
            L.append("")
    if not rows:
        L.append("> 今日未采集到军事稿件（源不可用或尚未到发布时段）。")
    L += ["---", "", DISCLAIMER_MD, ""]
    return "\n".join(L)


DIR_CN = {"bullish": "偏多", "bearish": "偏空", "neutral": "中性", "unclear": "信号不明确"}
CONF_CN = {"high": "高", "medium": "中", "low": "低"}


def _signal_lines(sig: dict | None) -> list[str]:
    """把一条 AI 研判压成几行可读素材（原样转述，不改写结论）。"""
    if not sig:
        return []
    out = ["- 方向：%s（置信度 %s）；归纳：%s" % (
        DIR_CN.get(sig.get("direction"), "信号不明确"),
        CONF_CN.get(sig.get("confidence"), "低"), sig.get("summary_cn") or "—")]
    ol = sig.get("outlook") or {}
    for key, name in (("short", "短期走向"), ("long", "长期走向")):
        o = ol.get(key) or {}
        if o.get("view") or o.get("direction"):
            out.append("- %s：%s（%s）%s" % (
                name, DIR_CN.get(o.get("direction"), "信号不明确"), o.get("horizon") or "",
                "：" + o["view"] if o.get("view") else ""))
    adv = sig.get("advice") or {}
    if adv.get("short_term"):
        out.append("- 短期投资建议：%s" % adv["short_term"])
    if adv.get("long_term"):
        out.append("- 长期投资建议：%s" % adv["long_term"])
    return out


def render_brief_doc(day: str, rows: list[dict], ai: dict, generated_at: str,
                     sig: dict | None = None, amap: dict | None = None, art_name: str = "") -> str:
    anchors = _anchor_map(rows, amap or {})
    L = ["# 每日资讯 · %s（%s）" % (day, _weekday(day)), "",
         "> 生成时间：%s ｜ 内容：「资讯与 AI 研判」页当日财经要闻 %d 条%s" % (
             bj_str(datetime.fromisoformat(generated_at), "%Y-%m-%d %H:%M:%S") if generated_at else "—",
             len(rows), " + 当日 AI 研判" if sig else ""),
         "> 要闻正文已抓取存到本机，界面里点条目标题即可看全文；"
         "AI 总结由 DeepSeek 生成，仅归纳事实，不构成投资建议。"]
    if art_name and anchors:
        L.append("> 全文文件 `%s`（共 %d 篇，条目后的「正文 #aN」就是该文件里的第 N 篇）。"
                 % (art_name, len(anchors)))
    L += ["", "## AI 总结", "",
          _demote((ai.get("text") or "").strip()) or
          "> 本次未生成 AI 总结：%s" % (ai.get("reason") or "未知原因"), ""]
    if sig:
        L += ["## 当日 AI 研判（原样留存）", ""]
        L += _signal_lines(sig)
        L.append("")
    L += ["## 当日要闻", ""]
    for i, r in enumerate(rows, 1):
        pub = (r.get("published_at_bj") or "")[11:16]
        line = "%d. %s — %s%s%s" % (
            i, _link(r.get("title"), r.get("url") or ""), r.get("source") or "—",
            " · %s" % pub if pub else "",
            " — %s" % r["summary"] if r.get("summary") else "")
        an = anchors.get(article_key(r.get("url") or ""))
        if art_name and an:
            line += " ｜ 正文 #a%d" % an
        L.append(line)
    if not rows:
        L.append("_当日暂无已采集的财经要闻（可到「资讯与 AI 研判」页点「立即获取资讯」）。_")
    L += ["", "---", "", DISCLAIMER_MD, ""]
    return "\n".join(L)


def _plain(md: str) -> str:
    """Markdown 压成一行纯文本（给列表里的一句话预览用）。"""
    s = re.sub(r"^#{1,6}\s*", "", md or "", flags=re.M)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = re.sub(r"[*`>]+", "", s)
    s = re.sub(r"^\s*[-\d.、)]\s*", "", s, flags=re.M)
    return re.sub(r"\s*\n+\s*", "；", s).strip()


def render_digest_doc(kind: str, per: str, start: date, end: date, days: list[dict],
                      ai: dict, fallback_note: str = "") -> str:
    name = KINDS[kind]["name"]
    label = _period_label(per, start, end)
    L = ["# %s · %s" % (name, label), "",
         "> 区间：%s 至 %s（共 %d 天有记录）｜ 生成时间：%s" % (
             start.isoformat(), end.isoformat(), len(days), bj_str(now_bj(), "%Y-%m-%d %H:%M:%S")),
         "> 由 AI 对区间内各日总结二次归纳；仅陈述事实，不预测价格，不构成投资建议。", "",
         "## AI 阶段综述", ""]
    text = _demote((ai.get("text") or "").strip())
    L.append(text or "> 本次未生成 AI 综述：%s" % (ai.get("reason") or "未知原因"))
    if fallback_note:
        L += ["", fallback_note]
    L += ["", "## 区间内每日记录", ""]
    for d in days:
        s = _plain(d.get("summary_md"))
        L.append("- **%s**%s%s" % (d["day"], "（无 AI 总结）" if not d.get("ai_available") else "",
                                  "：" + s[:120] + ("…" if len(s) > 120 else "") if s else ""))
    L += ["", "---", "", DISCLAIMER_MD, ""]
    return "\n".join(L)


def md_to_html(md: str) -> str:
    """邮件用的极简 Markdown → HTML（标题 / 列表 / 引用 / 粗体 / 链接 / 分隔线）。"""
    def inline(s: str) -> str:
        s = _html.escape(s)
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                   r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
        return s

    out, ul, ol, bq = [], False, False, False

    def close():
        nonlocal ul, ol, bq
        if ul:
            out.append("</ul>")
        if ol:
            out.append("</ol>")
        if bq:
            out.append("</blockquote>")
        ul = ol = bq = False

    for raw in (md or "").splitlines():
        line = raw.strip()
        if not line:
            close()
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            close()
            lv = min(4, len(m.group(1)) + 1)
            out.append("<h%d>%s</h%d>" % (lv, inline(m.group(2)), lv))
            continue
        if re.match(r"^(-{3,}|\*{3,})$", line):
            close()
            out.append("<hr>")
            continue
        if line.startswith("&gt;") or line.startswith(">"):
            close()
            bq = True
            out.append("<blockquote>%s</blockquote>" % inline(line.lstrip("&gt; ").lstrip("> ")))
            bq = False
            continue
        m = re.match(r"^[-*+]\s+(.*)$", line)
        if m:
            if not ul:
                close()
                out.append("<ul>")
                ul = True
            out.append("<li>%s</li>" % inline(m.group(1)))
            continue
        m = re.match(r"^\d+[.、)]\s*(.*)$", line)
        if m:
            if not ol:
                close()
                out.append("<ol>")
                ol = True
            out.append("<li>%s</li>" % inline(m.group(1)))
            continue
        close()
        out.append("<p>%s</p>" % inline(line))
    close()
    return "\n".join(out)


def _strip_title(md: str) -> str:
    """去掉 AI 自己加的 H1 标题，避免和程序拼的标题重复。"""
    lines = (md or "").strip().splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^#\s+", lines[0]):
        lines.pop(0)
    return "\n".join(lines).strip()


# --------------------------------------------------------------------------
# 服务
# --------------------------------------------------------------------------
class DailyService:
    def __init__(self, cfg_store, db, ai, mailer, logger, collect_brief=None):
        self.cfg = cfg_store
        self.db = db
        self.ai = ai
        self.mailer = mailer
        self.log = logger
        # brief 的素材来自本端新闻采集：run_day('brief') 前先跑一次 kind=news 的采集
        self.collect_brief = collect_brief
        self.lock = threading.Lock()
        self._progress_lock = threading.Lock()
        self._progress = {"running": False, "mode": "", "step": 0, "total": 0,
                          "text": "", "started_at": "", "finished_at": "", "error": ""}

    def progress_state(self) -> dict:
        with self._progress_lock:
            return dict(self._progress)

    def _set_progress(self, **kw):
        with self._progress_lock:
            self._progress.update(kw)

    def _mail_name(self) -> str:
        """邮件主题前缀取「发件人显示名」，与设置页保持一致。"""
        s = self.cfg.get().get("smtp") or {}
        return str(s.get("from_name") or "").strip() or "黄金积存金助手"

    # ---------- 路径 ----------
    def doc_root(self) -> str:
        d = str(self.cfg.get().get("doc_dir") or "").strip()
        if not d:
            return self.cfg.data_dir
        return os.path.abspath(os.path.expanduser(d))

    def doc_dir(self, kind: str) -> str:
        p = os.path.join(self.doc_root(), KINDS[kind]["folder"])
        os.makedirs(p, exist_ok=True)
        return p

    def day_path(self, kind: str, day: str) -> str:
        p = os.path.join(self.doc_dir(kind), day[:7])
        os.makedirs(p, exist_ok=True)
        return os.path.join(p, "%s.md" % day)

    def digest_path(self, kind: str, start: date, end: date, per: str = "") -> str:
        """汇总文档路径；文件名带周期类型，多选周期时各周期的文件不会互相覆盖。"""
        p = os.path.join(self.doc_dir(kind), "汇总")
        os.makedirs(p, exist_ok=True)
        tag = _PERIOD_FILE.get(per, "")
        return os.path.join(p, "%s_%s%s至%s汇总.md" % (
            KINDS[kind]["name"], tag + "_" if tag else "", start.isoformat(), end.isoformat()))

    @staticmethod
    def _write(path: str, text: str):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    # ---------- 正文本地保存 ----------
    def article_path(self, kind: str, day: str) -> str:
        """当天全部正文的本地文件：与日记同目录的 YYYY-MM-DD.正文.html。"""
        p = os.path.join(self.doc_dir(kind), day[:7])
        os.makedirs(p, exist_ok=True)
        return os.path.join(p, "%s.%s.html" % (day, ART_NAME))

    def _article_rows(self, kind: str, rows: list[dict]) -> list[dict]:
        """哪些条目值得抓正文：新闻只抓央视稿件；资讯抓财经要闻；热搜是搜索页，不抓。"""
        if kind == "news":
            return [r for r in rows if (r.get("src") or "").startswith("cctv_") and r.get("url")]
        return [r for r in rows if r.get("url")]

    def collect_contents(self, kind: str, rows: list[dict], limit: int | None = None) -> dict:
        """并发抓正文写进本机缓存；已抓过的直接复用，当天失败过的不反复重试。"""
        cand = self._article_rows(kind, rows)
        limit = int(limit or ARTICLE_RUN_LIMIT)
        keys = [article_key(r["url"]) for r in cand]
        cached = self.db.article_many(keys)
        todo, today_s = [], now_bj().strftime("%Y-%m-%d")
        for r, k in zip(cand, keys):
            hit = cached.get(k) or {}
            if hit.get("status") in ("ok", "skip"):
                continue
            if (hit.get("fetched_at") or "")[:10] == today_s:
                continue
            todo.append((k, r))
        todo = todo[:limit]
        stat = {"ok": 0, "skip": 0, "fail": 0, "fetched": 0}
        if todo:
            with ThreadPoolExecutor(max_workers=ARTICLE_WORKERS) as ex:
                futs = {ex.submit(fetch_article, r["url"]): (k, r) for k, r in todo}
                for f in as_completed(futs):
                    k, r = futs[f]
                    try:
                        res = f.result()
                    except Exception as e:  # 单篇失败不影响整天
                        res = {"status": "fail", "content": "", "chars": 0, "reason": str(e)[:60]}
                    self.db.article_save({"key": k, "url": r["url"], "title": r.get("title"),
                                          "content": res.get("content") or "",
                                          "chars": res.get("chars") or 0,
                                          "status": res.get("status") or "fail"})
                    stat[res.get("status") if res.get("status") in stat else "fail"] += 1
                    stat["fetched"] += 1
        amap = self.db.article_many(keys)
        stat["total"] = len(cand)
        stat["with_content"] = len([k for k in keys
                                    if (amap.get(k) or {}).get("status") == "ok"])
        stat["map"] = amap
        return stat

    def write_article_html(self, kind: str, day: str, rows: list[dict], amap: dict) -> dict:
        """把当天有正文的条目落成一份自包含 HTML（带 #aN 锚点），作为本机全文备份。"""
        anchors = _anchor_map(rows, amap)
        if not anchors:
            return {"path": "", "count": 0}
        esc = _html.escape
        parts = ['<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
                 "<title>%s · %s 正文</title>" % (esc(KINDS[kind]["name"]), day),
                 '<style>body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;'
                 "max-width:820px;margin:32px auto;padding:0 18px;line-height:1.8;color:#1F2430}"
                 "h1{font-size:22px}h2{font-size:17px;margin:26px 0 6px}"
                 ".meta{font-size:12px;color:#5B6472;margin-bottom:10px}"
                 ".idx{background:#F6F8FB;border-radius:10px;padding:10px 16px;font-size:13px}"
                 ".idx a{display:block;color:#1F5FD0;text-decoration:none;padding:2px 0}"
                 "article p{margin:0 0 12px;text-indent:2em}a{color:#1F5FD0}"
                 "</style></head><body>"]
        parts.append("<h1>%s · %s 正文（%d 篇）</h1>" % (esc(KINDS[kind]["name"]), day, len(anchors)))
        parts.append('<div class="meta">本机自动抓取留存，仅供个人查阅；原文版权归原媒体所有。</div>')
        parts.append('<div class="idx">')
        for r in rows:
            an = anchors.get(article_key(r.get("url") or ""))
            if an:
                parts.append('<a href="#a%d">%d. %s</a>' % (an, an, esc(r.get("title") or "")))
        parts.append("</div>")
        for r in rows:
            an = anchors.get(article_key(r.get("url") or ""))
            if not an:
                continue
            c = amap.get(article_key(r["url"])) or {}
            pub = (r.get("published_at_bj") or "").replace("T", " ")[:16]
            parts.append('<article id="a%d"><h2>%d. %s</h2><div class="meta">%s%s ｜ '
                         '<a href="%s" target="_blank" rel="noopener">打开原文</a></div>' % (
                             an, an, esc(r.get("title") or ""),
                             esc(r.get("src_name") or r.get("source") or "—"),
                             " · " + esc(pub) if pub else "", esc(r.get("url") or "")))
            for p in (c.get("content") or "").split("\n\n"):
                if p.strip():
                    parts.append("<p>%s</p>" % esc(p.strip()))
            parts.append("</article>")
        parts.append("</body></html>")
        path = self.article_path(kind, day)
        self._write(path, "\n".join(parts))
        return {"path": path, "count": len(anchors)}

    def article_detail(self, key: str, url: str = "", title: str = "") -> dict:
        """读一条本机正文；没存过或上次失败时按需现抓一次并落库。"""
        hit = self.db.article_get(key) if key else None
        if (not hit or hit.get("status") not in ("ok", "skip")) and url:
            try:
                res = fetch_article(url)
            except Exception as e:  # 现抓出错也不能把已存好的正文覆盖掉
                res = {"status": "fail", "content": "", "chars": 0, "reason": str(e)[:80]}
            self.db.article_save({"key": key, "url": url, "title": title,
                                  "content": res.get("content") or "",
                                  "chars": res.get("chars") or 0, "status": res.get("status")})
            hit = self.db.article_get(key)
        if not hit:
            return {"ok": False, "message": "这条还没有保存正文"}
        good = hit.get("status") == "ok" and bool(hit.get("content"))
        return {"ok": True, "key": key, "title": hit.get("title") or title,
                "url": hit.get("url") or url, "status": hit.get("status") or "fail",
                "chars": int(hit.get("chars") or 0), "content": hit.get("content") or "",
                "fetched_at": hit.get("fetched_at"),
                "message": "" if good else ("该来源给的是搜索页或需登录，抓不到正文，可点「打开原文」" if
                                            hit.get("status") == "skip" else
                                            "这篇没抓到正文（页面可能是视频或需要跳转），可点「打开原文」")}

    # ---------- 每日文档 ----------
    def run_day(self, kind: str, trigger: str = "manual", force: bool = False,
                send_mail: bool | None = None) -> dict:
        """采集（或取当日要闻）→ AI 总结 → 写当日 Markdown 文档 → 可选发信。"""
        if kind not in KINDS:
            return {"ok": False, "message": "未知类型"}
        with self.lock:
            cfg = self.cfg.get()
            sec = _sec(cfg, kind)
            now = now_bj()
            day = now.strftime("%Y-%m-%d")
            started = now
            tid = self.db.start_task("daily:" + kind, trigger)
            status: dict = {}
            sig = None
            try:
                if kind == "news":
                    items, status, stats = collect_daily(cfg, day)
                    # 央视按稿件 id 去重累积；热搜是整榜快照，每次覆盖当天旧榜
                    cctv = [x for x in items if x["src"].startswith("cctv_")]
                    self.db.add_daily_items(day, cctv)
                    for hid in [s["id"] for s in HOT_SOURCES]:
                        if any(x["src"] == hid for x in items):
                            self.db.replace_daily_items(day, hid, [x for x in items if x["src"] == hid])
                    rows = self.db.daily_items(day, "news")
                    stats = {"cctv": len([r for r in rows if r["src"].startswith("cctv_")]),
                             "hot": len([r for r in rows if not r["src"].startswith("cctv_")]),
                             "hot_sources": stats["hot_sources"], "cctv_columns": stats["cctv_columns"]}
                    if not stats["cctv_columns"]:
                        status["cctv"] = "央视网：当日暂无稿件或采集失败"
                elif kind == "mil":
                    items, status, stats = collect_military(cfg, day)
                    if items:                     # 重跑当天：先清旧条目，避免新旧两次采集混在一份文档里
                        self.db.drop_daily_items(day, "mil")
                    self.db.add_daily_items(day, items, "mil")
                    rows = self.db.daily_items(day, "mil")
                    stats = {"mil_domestic": len([r for r in rows if r["src"] not in MIL_WORLD_SRCS]),
                             "mil_world": len([r for r in rows if r["src"] in MIL_WORLD_SRCS]),
                             "sources": sorted({r["src"] for r in rows})}
                    if not stats["mil_domestic"] and not stats["mil_world"]:
                        status["mil"] = "军事源：当日暂无稿件或采集失败"
                else:
                    if self.collect_brief:
                        try:
                            r = self.collect_brief(trigger)
                            status["news"] = "财经要闻采集：" + ("完成" if r.get("ok") else r.get("message", "失败"))
                        except Exception as e:
                            status["news"] = "财经要闻采集异常：%s" % str(e)[:100]
                    # 「资讯与 AI 研判」页的当日内容：财经要闻列表 + 当日最近一次 AI 研判
                    rows = self.db.news_of_day(day, max(1, min(200, int(sec.get("max_items") or 40))))
                    sig_row = self.db.one("SELECT signal_json FROM ai_signal WHERE substr(as_of_bj,1,10)=? "
                                          "ORDER BY id DESC LIMIT 1", (day,))
                    try:
                        sig = json.loads(sig_row["signal_json"]) if sig_row else None
                    except (ValueError, TypeError):
                        sig = None
                    stats = {"brief": len(rows), "signal": bool(sig)}
                # 正文抓取：存本机（库里一份 + 当天一份 HTML），界面与文档都从本地看
                art = {"map": {}, "with_content": 0, "total": 0}
                art_file = {"path": "", "count": 0}
                try:
                    art = self.collect_contents(kind, rows, int(sec.get("content_max") or ARTICLE_RUN_LIMIT))
                    art_file = self.write_article_html(kind, day, rows, art.get("map") or {})
                    stats["content"] = art.get("with_content", 0)
                    status["article"] = "正文本地保存 %d/%d 篇%s" % (
                        art.get("with_content", 0), art.get("total", 0),
                        "" if not art_file.get("path") else "：" + os.path.basename(art_file["path"]))
                except Exception as e:
                    status["article"] = "正文抓取异常：%s" % str(e)[:100]
                ai = self._ai_day(kind, day, rows, sec, force, sig)
                # 本次 AI 没生成（失败 / 关闭）而当天早先已生成过：沿用旧总结，不让文档倒退
                prev = self.db.one("SELECT * FROM daily_doc WHERE day=? AND kind=?", (day, kind))
                reused = False
                if not ai.get("available") and prev and prev.get("ai_available") and prev.get("summary_md"):
                    ai = {"available": True, "reused": True,
                          "text": prev["summary_md"], "reason": ai.get("reason")}
                    reused = True
                generated_at = iso_bj()
                art_name = os.path.basename(art_file.get("path") or "")
                if kind == "news":
                    md = render_news_doc(day, rows, ai, stats, generated_at, art.get("map"), art_name)
                elif kind == "mil":
                    md = render_mil_doc(day, rows, ai, stats, generated_at, art.get("map"), art_name)
                else:
                    md = render_brief_doc(day, rows, ai, generated_at, sig, art.get("map"), art_name)
                path = self.day_path(kind, day)
                self._write(path, md)
                self.db.upsert_daily_doc({
                    "day": day, "kind": kind, "path": path, "ai_available": ai.get("available"),
                    "ai_reason": ai.get("reason") or "", "summary_md": ai.get("text") or "",
                    "counts": stats})
                delivery = None
                if send_mail is None:
                    send_mail = bool(sec.get("send_mail"))
                if send_mail:
                    delivery = self._send(
                        kind, "【%s】%s · %s（%s）" % (self._mail_name(), KINDS[kind]["name"], day, _weekday(day)),
                        md, "daily_" + kind)
                self.db.end_task(tid, "ok", status, started=started)
                self.db.event("info", "%s 已生成：%s（%d 条，AI %s%s）" % (
                    KINDS[kind]["name"], path, len(rows), "完成" if ai.get("available") else "未生成",
                    "，邮件已发送" if delivery and delivery.get("ok") else ""))
                return {"ok": True, "kind": kind, "day": day, "path": path, "markdown": md,
                        "counts": stats, "items": len(rows),
                        "article": {"with_content": art.get("with_content", 0),
                                    "total": art.get("total", 0),
                                    "fetched": art.get("fetched", 0),
                                    "file": art_file.get("path") or "",
                                    "pieces": art_file.get("count", 0)},
                        "ai": {"available": bool(ai.get("available")), "reused": reused,
                               "reason": ai.get("reason") or "", "summary_md": ai.get("text") or ""},
                        "delivery": delivery, "source_status": status, "finished_at": iso_bj()}
            except Exception as e:
                msg = "%s: %s" % (type(e).__name__, str(e)[:300])
                self.db.end_task(tid, "error", status, error=msg, started=started)
                self.db.event("error", "%s 生成失败：%s" % (KINDS[kind]["name"], msg[:200]))
                self.log.exception("daily run_day failed")
                return {"ok": False, "message": msg, "source_status": status}

    def run_all_day(self, trigger: str = "manual", force: bool = False,
                    send_mail: bool | None = None) -> dict:
        """三份日报依次生成，再合成一封「每日综合」邮件（设置页合并后的唯一入口）。"""
        plan = self.cfg.get().get("daily_all") or {}
        day = now_bj().strftime("%Y-%m-%d")
        started_at = iso_bj()
        self._set_progress(running=True, mode="day", step=0, total=4,
                           text="准备采集", started_at=started_at, finished_at="", error="")
        results = {}
        try:
            for idx, kind in enumerate(("news", "brief", "mil"), 1):
                name = KINDS[kind]["name"]
                self._set_progress(step=idx - 1, text="%s 采集中（%d/3）" % (name, idx))
                results[kind] = self.run_day(kind, trigger=trigger, force=force, send_mail=False)
                self._set_progress(step=idx, text="%s 已完成（%d/3）" % (name, idx))
            if send_mail is None:
                send_mail = bool(plan.get("send_mail"))
            self._set_progress(step=3, text="AI 合成每日综合" + ("并发送邮件" if send_mail else ""))
            # 无论是否发信，都生成一份「每日综合」本地文档；发信单独由 send_mail 控制
            combined = self.send_combined_day(day, force=force, send_mail=bool(send_mail))
            delivery = combined.get("delivery")
            ok = all(bool(r.get("ok")) for r in results.values())
            if delivery and delivery.get("ok"):
                tail = "，合并邮件已发送"
            elif delivery:
                tail = "，合并邮件发送失败：" + str(delivery.get("message", ""))[:100]
            elif combined.get("ok"):
                tail = "，合并文档已生成（未发信）"
            else:
                tail = ""
            self.db.event("info" if ok else "error",
                          "每日综合：三份文档 %s%s" % ("已生成" if ok else "生成失败", tail))
            self._set_progress(running=False, step=4, text="已完成", finished_at=iso_bj())
            return {"ok": ok, "day": day, "results": results, "combined": combined, "delivery": delivery}
        except Exception as e:
            self._set_progress(running=False, error="%s: %s" % (type(e).__name__, str(e)[:200]),
                               finished_at=iso_bj(), text="失败")
            raise

    def due_periods_all(self, plan: dict, today: date | None = None) -> list[tuple[str, tuple]]:
        """统一汇总周期：任意一个模块在该周期到期，就整封汇总一起做。"""
        today = today or now_bj().date()
        out = []
        for per in digest_periods(plan):
            rng = None
            for kind in KINDS:
                rng = self.due_range(kind, plan, today, per)
                if rng:
                    break
            if rng:
                out.append((per, rng))
        return out

    def send_combined_day(self, day: str | None = None, force: bool = False,
                          send_mail: bool = True) -> dict:
        """把当天三份日报的 AI 总结交给 AI 合成一份「每日综合」Markdown，按需发信。"""
        day = day or now_bj().strftime("%Y-%m-%d")
        pieces, missing = [], []
        for kind in ("news", "brief", "mil"):
            doc = self.db.one("SELECT * FROM daily_doc WHERE day=? AND kind=?", (day, kind))
            name = KINDS[kind]["name"]
            if not doc:
                missing.append(name)
                continue
            s = (doc.get("summary_md") or "").strip()
            if s:
                pieces.append("【%s】\n%s" % (name, s))
            else:
                counts = _loads(doc.get("counts_json"))
                brief_items = []
                if kind == "news":
                    brief_items = [r["title"] for r in self.db.daily_items(day, "news", None, 12)]
                elif kind == "mil":
                    brief_items = [r["title"] for r in self.db.daily_items(day, "mil", None, 12)]
                else:
                    brief_items = [r.get("title") or "" for r in self.db.news_of_day(day, 12)]
                pieces.append("【%s】\nAI 总结未生成；当日条目 %s：\n%s" % (
                    name, counts or "", "\n".join("- " + x for x in brief_items if x)))
        if not pieces:
            return {"ok": False, "message": "今天还没有任何每日文档，先生成一次再发信"}
        user = "日期：%s（%s）\n\n%s\n\n%s" % (day, _weekday(day), "\n\n".join(pieces),
                                              ("今日未生成：" + "、".join(missing)) if missing else "")
        ai = self._summarize_or_none(COMBINED_DAY_SYSTEM, user, 2000, force)
        title = "# 每日综合 · %s（%s）" % (day, _weekday(day))
        if ai.get("available"):
            body = _strip_title(ai.get("text") or "")
        else:
            body = ("## 今日要点\n- 合并综述未生成（%s）；以下为三份日报的 AI 总结摘录\n\n%s" %
                    (ai.get("reason") or "未知原因", "\n\n".join(pieces)))
        md = title + "\n\n" + body.strip() + "\n"
        path = os.path.join(self.doc_root(), "每日综合", day[:7], day + ".md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._write(path, md)
        subject = "【%s】每日综合 · %s（%s）" % (self._mail_name(), day, _weekday(day))
        delivery = self._send_combined(subject, md, "daily_all") if send_mail else None
        self.db.event("info", "每日综合%s：%s（AI %s）" % (
            "邮件" if send_mail else "文档", subject[:60],
            "完成" if ai.get("available") else "未生成"))
        return {"ok": True, "path": path, "markdown": md,
                "ai": {"available": bool(ai.get("available")), "reason": ai.get("reason") or ""},
                "delivery": delivery}

    def run_all_digest(self, trigger: str = "manual", force: bool = False,
                       period: str | None = None, send_mail: bool | None = None) -> dict:
        """统一周期汇总：三个模块按同一区间各出一份汇总文档，再合成一封汇总邮件。"""
        plan = self.cfg.get().get("daily_all") or {}
        per = period or (digest_periods(plan) or [None])[0]
        if not per:
            return {"ok": False, "not_due": True, "message": "未勾选任何汇总周期，请先到设置页选择"}
        today = now_bj().date()
        # 统一区间：取三个模块各自到期区间的并集，避免同一次汇总里各模块起止不一致
        ranges = []
        for kind in KINDS:
            r = self.due_range(kind, plan, today, per)
            if r is None and force:
                r = self.force_range(kind, plan, today, per)
            if r:
                ranges.append(r)
        if not ranges:
            return {"ok": False, "not_due": True, "period": per,
                    "message": "所选周期都已汇总过，尚未到下一个周期；点「立即汇总」可强制重新生成"}
        start = min(x[0] for x in ranges)
        end = max(x[1] for x in ranges)
        self._set_progress(running=True, mode="digest", step=0, total=4,
                           text="%s 汇总准备中" % _period_label(per, start, end),
                           started_at=iso_bj(), finished_at="", error="")
        results = {}
        try:
            for idx, kind in enumerate(KINDS, 1):
                name = KINDS[kind]["name"]
                self._set_progress(step=idx - 1, text="%s 汇总中（%d/3）" % (name, idx))
                results[kind] = self.run_digest(kind, trigger=trigger, force=force, send_mail=False,
                                                period=per, rng=(start, end))
                self._set_progress(step=idx, text="%s 汇总完成（%d/3）" % (name, idx))
            good = [r for r in results.values() if r.get("ok") and (r.get("markdown") or "").strip()]
            if not good:
                reasons = [str(r.get("message") or "") for r in results.values() if r.get("message")]
                self._set_progress(running=False, finished_at=iso_bj(), text="无可用文档")
                return {"ok": False, "not_due": True, "period": per,
                        "message": "；".join(reasons) or "区间内没有可汇总的每日文档"}
            missing = [KINDS[k]["name"] for k in KINDS
                       if not (results[k].get("ok") and (results[k].get("markdown") or "").strip())]
            label = _period_label(per, start, end)
            self._set_progress(step=3, text="AI 合成每日综合汇总" + ("并发送邮件" if plan.get("digest_send_mail") else ""))
            pieces = ["【%s】\n%s" % (KINDS[k]["name"], (results[k].get("markdown") or "").strip())
                      for k in KINDS if results[k].get("ok") and (results[k].get("markdown") or "").strip()]
            user = "区间：%s\n\n%s" % (label, "\n\n".join(pieces))
            if missing:
                user += "\n\n本期无文档的板块：" + "、".join(missing)
            ai = self._summarize_or_none(COMBINED_DIGEST_SYSTEM, user, 2400, force)
            title = "# 每日综合汇总 · %s" % label
            if ai.get("available"):
                body = _strip_title(ai.get("text") or "")
            else:
                body = ("## 阶段主线\n- 合并综述未生成（%s）；以下为三个板块的汇总摘录\n\n%s" %
                        (ai.get("reason") or "未知原因", "\n\n".join(pieces)))
            md = title + "\n\n" + body.strip() + "\n"
            if missing:
                md += "\n> 本期无文档的板块：%s\n" % "、".join(missing)
            path = os.path.join(self.doc_root(), "每日综合汇总", "%s至%s" % (start, end), per + ".md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self._write(path, md)
            if send_mail is None:
                send_mail = bool(plan.get("digest_send_mail", True))
            delivery = self._send_combined(
                "【%s】每日综合汇总 · %s（AI 汇总）" % (self._mail_name(), label), md, "digest_all") if send_mail else None
            self.db.event("info", "每日综合汇总：%s（%d 个模块%s%s）" % (
                label, len(good), "，缺：" + "、".join(missing) if missing else "",
                "，邮件已发送" if delivery and delivery.get("ok") else ""))
            self._set_progress(running=False, step=4, text="已完成", finished_at=iso_bj())
            return {"ok": True, "period": per, "start": start.isoformat(), "end": end.isoformat(),
                    "label": label, "results": results, "path": path, "markdown": md,
                    "missing": missing,
                    "ai": {"available": bool(ai.get("available")), "reason": ai.get("reason") or ""},
                    "delivery": delivery}
        except Exception as e:
            self._set_progress(running=False, error="%s: %s" % (type(e).__name__, str(e)[:200]),
                               finished_at=iso_bj(), text="失败")
            raise

    def _combined_html(self, md: str) -> str:
        return ('<div style="font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
                'font-size:14px;line-height:1.7;max-width:760px;color:#1F2430">%s'
                '<hr style="border:none;border-top:1px solid #E3E6EC;margin:20px 0">'
                '<p style="font-size:12px;color:#5B6472">%s</p></div>') % (
            md_to_html(md), _html.escape(DISCLAIMER_MD.strip("*")))

    def mail_preview(self, mode: str = "day", period: str | None = None) -> dict:
        """读最近一份「每日综合 / 每日综合汇总」Markdown，返回与真实邮件一致的 HTML。"""
        cfg = self.cfg.get()
        if mode == "digest":
            root = os.path.join(self.doc_root(), "每日综合汇总")
            target = None
            for base, _dirs, files in os.walk(root):
                for fn in files:
                    if fn == (period or "1m") + ".md":
                        p = os.path.join(base, fn)
                        if target is None or os.path.getmtime(p) > os.path.getmtime(target):
                            target = p
            prefix = "每日综合汇总"
        else:
            root = os.path.join(self.doc_root(), "每日综合")
            target = None
            for base, _dirs, files in os.walk(root):
                for fn in files:
                    if fn.endswith(".md"):
                        p = os.path.join(base, fn)
                        if target is None or os.path.getmtime(p) > os.path.getmtime(target):
                            target = p
            prefix = "每日综合"
        if not target or not os.path.isfile(target):
            return {"ok": False, "message": "还没有生成过%s文档，先生成一次再来预览" % prefix}
        with open(target, "r", encoding="utf-8") as f:
            md = f.read()
        title = md.strip().splitlines()[0].lstrip("# ").strip() if md.strip() else prefix
        subject = "【%s】%s" % (self._mail_name(), title)
        return {"ok": True, "mode": mode, "path": target, "subject": subject,
                "html": self._combined_html(md), "markdown": md}

    def _send_combined(self, subject: str, md: str, report_type: str) -> dict:
        html = self._combined_html(md)
        report = {"report_date": now_bj().strftime("%Y-%m-%d"), "report_type": report_type,
                  "subject": subject, "html": html, "text_body": md, "signal_id": None}
        report["id"] = self.db.insert_report(report)
        delivery = self.mailer.send_report(report)
        self.db.audit("daily_mail", "每日综合邮件 %s：%s" % (
            subject[:40], "已发送" if delivery.get("ok") else delivery.get("message", "失败")))
        return delivery

    def _ai_day(self, kind: str, day: str, rows: list[dict], sec: dict, force: bool,
                sig: dict | None = None) -> dict:
        if not sec.get("ai_enabled", True):
            return {"available": False, "reason": "已在设置中关闭 AI 总结", "text": ""}
        lines = []
        if kind == "news":
            for c, cname in CCTV_COLUMNS:
                part = [r for r in rows if r["src"] == "cctv_" + c]
                if part:
                    lines.append("【央视·%s】" % cname)
                    lines += ["- %s%s" % (r["title"], "：%s" % r["summary"][:80] if r.get("summary") else "")
                              for r in part[:12]]
            for s in HOT_SOURCES:
                part = [r for r in rows if r["src"] == s["id"]]
                if part:
                    lines.append("【%s】" % s["name"])
                    lines += ["- %s" % r["title"] for r in part[:10]]
        elif kind == "mil":
            labels = [("mil_81", "国内 · 中国军网要闻"), ("mil_people", "国内 · 人民网军事"),
                      ("mil_cctv", "央视军事频道（含国际）"),
                      ("mil_cctv_intl", "国际 · 央视国际军事")] + \
                     [(s["id"], "国际 · " + s["name"]) for s in MIL_RSS_SOURCES]
            for sid, lb in labels:
                part = [r for r in rows if r["src"] == sid]
                if part:
                    lines.append("【%s】" % lb)
                    lines += ["- %s%s" % (r["title"], "：%s" % r["summary"][:90] if r.get("summary") else "")
                              for r in part[:8]]
        else:
            lines += ["- [%s] %s%s" % (r.get("source") or "—", r.get("title") or "",
                                       "：%s" % r["summary"][:80] if r.get("summary") else "")
                      for r in rows[:40]]
            s = _signal_lines(sig)
            if s:
                lines += ["【当日 AI 研判（来自资讯与 AI 研判页）】"] + s
        if not lines:
            return {"available": False, "reason": "当日没有可归纳的条目", "text": ""}
        system = {"news": NEWS_DAY_SYSTEM, "mil": MIL_DAY_SYSTEM}.get(kind, BRIEF_DAY_SYSTEM)
        user = "日期：%s（%s）\n\n材料：\n%s" % (day, _weekday(day), "\n".join(lines))
        r = self.ai.summarize(system, user, 1400, force)
        if not r.get("ok"):
            return {"available": False, "reason": _reason_text(r.get("reason")), "text": ""}
        text = r["text"]
        if kind == "brief" and not sig:
            # 没有当日研判时，模型偶尔仍会写一节「材料中未附…」，直接去掉这一节
            text = _drop_empty_review(text)
        return {"available": True, "text": text, "usage": r.get("usage")}

    # ---------- 周期汇总 ----------
    def _d(self, s: str) -> date:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()

    def _last_covered(self, kind: str, per: str | None = None) -> date | None:
        """某个类型在某个周期上「已经汇总到的截止日期」。多选周期时各周期各记各的。
        旧版本的汇总记录起止日期为空，这些行不作为基准（end_date IS NOT NULL）。"""
        sql = "SELECT end_date FROM digest_log WHERE kind=? AND status='ok' AND end_date IS NOT NULL"
        args = [kind]
        if per:
            sql += " AND period=?"
            args.append(per)
        r = self.db.one(sql + " ORDER BY end_date DESC, id DESC LIMIT 1", args)
        return self._d(r["end_date"]) if r else None

    def _first_doc_day(self, kind: str) -> date | None:
        r = self.db.one("SELECT MIN(day) d FROM daily_doc WHERE kind=?", (kind,))
        return self._d(r["d"]) if r and r.get("d") else None

    def _has_doc(self, kind: str, d: date) -> bool:
        return bool(self.db.daily_docs(kind, d.isoformat(), d.isoformat(), 1))

    @staticmethod
    def _week_start(d: date) -> date:
        return d - timedelta(days=d.weekday())

    @classmethod
    def _calendar_boundary(cls, per: str, today: date):
        """最近一个「已经走完」的自然周期（起, 止）。每周 = 周一至周日。"""
        y, m = today.year, today.month
        if per == "1w":
            mon = cls._week_start(today)
            return mon - timedelta(days=7), mon - timedelta(days=1)
        if per == "1m":
            py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
            return date(py, pm, 1), date(py, pm, monthrange(py, pm)[1])
        if per == "6m":
            if m >= 7:
                return date(y, 1, 1), date(y, 6, 30)
            return date(y - 1, 7, 1), date(y - 1, 12, 31)
        if per == "1y":
            return date(y - 1, 1, 1), date(y - 1, 12, 31)
        return None, None

    @classmethod
    def _period_start(cls, per: str, today: date) -> date:
        """today 所在自然周期的第一天。"""
        y, m = today.year, today.month
        if per == "1w":
            return cls._week_start(today)
        if per == "1m":
            return date(y, m, 1)
        if per == "6m":
            return date(y, 1, 1) if m >= 7 else date(y - 1, 7, 1)
        return date(y, 1, 1)

    def due_range(self, kind: str, sec: dict, today: date | None = None, per: str | None = None):
        """返回某个周期当前「应汇总而尚未汇总」的区间 (start, end)。没有则 None。

        以该周期最近一次已汇总到的日期为基准：只要一个完整周期已经过去且尚未覆盖，就算到期，
        因此电脑关机错过了周一 / 月初，开机后也会自动补做，不依赖「必须正好是那一天」。
        per 省略时取已勾选的第一个周期。
        """
        today = today or now_bj().date()
        per = per or (digest_periods(sec) or ["1m"])[0]
        last = self._last_covered(kind, per)
        if last is None:
            first = self._first_doc_day(kind)
            if first is None or first >= today:
                return None
            last = first - timedelta(days=1)
        end = today if self._has_doc(kind, today) else today - timedelta(days=1)
        if per == "1d":
            return (last + timedelta(days=1), end) if end > last else None
        if per == "custom":
            n = max(1, int(sec.get("digest_custom_days") or 15))
            return (last + timedelta(days=1), end) if (end - last).days >= n else None
        b_start, b_end = self._calendar_boundary(per, today)
        if b_end is None or b_end <= last:
            return None
        return max(b_start, last + timedelta(days=1), self._first_doc_day(kind) or b_start), b_end

    def due_periods(self, kind: str, sec: dict, today: date | None = None) -> list:
        """已勾选的周期里，当前所有「已到期且未汇总」的 [(周期, 区间)]。"""
        out = []
        for per in digest_periods(sec):
            rng = self.due_range(kind, sec, today, per)
            if rng:
                out.append((per, rng))
        return out

    def force_range(self, kind: str, sec: dict, today: date | None = None, per: str | None = None):
        """手动「立即汇总」：按周期取最近一个完整周期（不管是否已汇总过）。"""
        today = today or now_bj().date()
        per = per or (digest_periods(sec) or ["1m"])[0]
        end = today if self._has_doc(kind, today) else today - timedelta(days=1)
        if per in ("1d", "custom"):
            n = 1 if per == "1d" else max(1, int(sec.get("digest_custom_days") or 15))
            return end - timedelta(days=n - 1), end
        b_start, b_end = self._calendar_boundary(per, today)
        if b_start and self.db.daily_docs(kind, b_start.isoformat(), b_end.isoformat(), 1):
            return b_start, b_end
        # 上一个完整周期内没有文档（刚开始用）：改为汇总「本周期至今」
        first = self._first_doc_day(kind)
        start = max(first, self._period_start(per, today)) if first else end - timedelta(days=29)
        return (start, end) if end >= start else (end, end)

    def _next_digest_date(self, kind: str, sec: dict, per: str, today: date) -> date:
        """某个周期下一次「自然到期日」。"""
        if per == "1d":
            return today + timedelta(days=1)
        if per == "custom":
            n = max(1, int(sec.get("digest_custom_days") or 15))
            base = self._last_covered(kind, per) or (today - timedelta(days=1))
            return base + timedelta(days=n)
        if per == "1w":
            return self._week_start(today) + timedelta(days=7)
        if per == "6m":
            return date(today.year + (1 if today.month >= 7 else 0), 7 if today.month < 7 else 1, 1)
        if per == "1y":
            return date(today.year + 1, 1, 1)
        return (date(today.year + 1, 1, 1) if today.month == 12
                else date(today.year, today.month + 1, 1))

    def next_digest_text(self, kind: str, sec: dict) -> str:
        """界面上的「下一次汇总」：有已到期的先提示补做，否则按周期列出各自的下一次日期。"""
        today = now_bj().date()
        pers = digest_periods(sec)
        t = sec.get("digest_time") or ""
        mail = "并发信" if sec.get("digest_send_mail") else ""
        if not pers:
            return "已关闭（未勾选汇总周期）"
        due = [p for p, _ in self.due_periods(kind, sec, today)]
        if due:
            return "已到期（%s），等待 %s 到点生成%s" % (
                "、".join(DIGEST_PERIOD_CN.get(p, p) for p in due), t, mail)
        parts = []
        for p in pers:
            d = self._next_digest_date(kind, sec, p, today)
            if d <= today:
                d = today + timedelta(days=1)
            parts.append("%s %s" % (d.isoformat(), DIGEST_PERIOD_CN.get(p, p)))
        return "；".join(parts) + " %s%s" % (t, mail)

    def run_digest(self, kind: str, trigger: str = "manual", force: bool = False,
                   send_mail: bool | None = None, period: str | None = None,
                   today: date | None = None, rng: tuple | None = None) -> dict:
        """周期汇总。period 指定时只做这一个周期；否则对「已到期的周期」逐个汇总，
        force 时对全部已勾选周期各做一次。各周期各出各的文档、各记各的汇总记录。
        rng=(start,end) 时直接使用这个区间（每日综合用它统一三个模块的区间）。"""
        if kind not in KINDS:
            return {"ok": False, "message": "未知类型"}
        with self.lock:
            cfg = self.cfg.get()
            sec = _sec(cfg, kind)
            today = today or now_bj().date()
            pers = [period] if period else digest_periods(sec)
            if period and period not in DIGEST_PERIOD_CN:
                return {"ok": False, "message": "未知的汇总周期：%r" % period}
            if not pers:
                return {"ok": False, "not_due": True, "message": "未勾选任何汇总周期，请先到设置页选择"}
            plan = []
            if rng is not None:
                plan = [(pers[0], rng)]
            else:
                for per in pers:
                    rng1 = self.due_range(kind, sec, today, per)
                    if rng1 is None and force:
                        rng1 = self.force_range(kind, sec, today, per)
                    if rng1:
                        plan.append((per, rng1))
            if not plan:
                return {"ok": False, "not_due": True,
                        "message": "所选周期都已汇总过，尚未到下一个周期；点「立即汇总」可强制重新生成"}
            results = [self._digest_one(kind, sec, per, rng, trigger, force, send_mail)
                       for per, rng in plan]
            if len(results) == 1:
                results[0]["results"] = [dict(results[0])]   # 浅拷贝，避免自引用导致 JSON 序列化失败
                return results[0]
            good = [r for r in results if r.get("ok")]
            mails = [r["delivery"] for r in good if r.get("delivery")]
            return {"ok": bool(good), "not_due": not good, "kind": kind, "results": results,
                    "label": "、".join(r.get("label", "") for r in good),
                    "days": sum(int(r.get("days") or 0) for r in good),
                    "paths": [r.get("path") for r in good],
                    "ai": {"available": bool(good) and all((r.get("ai") or {}).get("available") for r in good),
                           "reason": "；".join(x for x in ((r.get("ai") or {}).get("reason") for r in results) if x)},
                    "delivery": ({"ok": all(m.get("ok") for m in mails), "count": len(mails)} if mails else None),
                    "message": "" if good else "；".join(
                        x for x in (r.get("message") for r in results) if x)}

    def _digest_one(self, kind: str, sec: dict, per: str, rng, trigger: str, force: bool,
                    send_mail: bool | None) -> dict:
        """对区间内各日总结做 AI 二次归纳，写汇总文档，按设置发信（单个周期）。"""
        start, end = rng
        if end < start:
            return {"ok": False, "not_due": True, "period": per, "message": "区间内还没有可汇总的每日文档"}
        days = sorted(self.db.daily_docs(kind, start.isoformat(), end.isoformat(), 4000),
                      key=lambda x: x["day"])
        if not days:
            return {"ok": False, "not_due": True, "period": per, "message": "区间内没有每日文档，请先生成每日文档"}
        started = now_bj()
        tid = self.db.start_task("digest:" + kind, trigger)
        try:
            pieces = self._digest_pieces(kind, days)
            ai = self._ai_digest(pieces, force, MIL_DIGEST_SYSTEM if kind == "mil" else DIGEST_SYSTEM) if pieces else \
                {"available": False, "reason": "区间内没有可归纳的内容", "text": ""}
            fallback = ""
            if not ai.get("available"):
                fallback = ("> AI 综述未生成（%s）。以下为区间内各日要点摘录：\n>\n> %s" %
                            (ai.get("reason") or "未知原因",
                             "\n> ".join(p.replace("\n", " ")[:200] for p in pieces[:25])))
            md = render_digest_doc(kind, per, start, end, days, ai, fallback)
            path = self.digest_path(kind, start, end, per)
            self._write(path, md)
            if send_mail is None:
                send_mail = bool(sec.get("digest_send_mail"))
            delivery = None
            if send_mail:
                delivery = self._send(
                    kind, "【%s】%s · %s（AI 汇总）" % (self._mail_name(), KINDS[kind]["name"], _period_label(per, start, end)),
                    md, "digest_" + kind)
            did = self.db.insert_digest({
                "kind": kind, "period": per, "start_date": start.isoformat(), "end_date": end.isoformat(),
                "label": _period_label(per, start, end), "days": len(days), "path": path,
                "ai_available": ai.get("available"), "mailed": delivery and delivery.get("ok")})
            self.db.end_task(tid, "ok", {}, started=started)
            self.db.event("info", "%s 汇总完成：%s（%d 天，AI %s%s）" % (
                KINDS[kind]["name"], path, len(days), "完成" if ai.get("available") else "未生成",
                "，邮件已发送" if delivery and delivery.get("ok") else ""))
            return {"ok": True, "kind": kind, "period": per, "start": start.isoformat(),
                    "end": end.isoformat(), "label": _period_label(per, start, end),
                    "days": len(days), "path": path, "markdown": md,
                    "ai": {"available": bool(ai.get("available")), "reason": ai.get("reason") or ""},
                    "delivery": delivery, "digest_id": did, "finished_at": iso_bj()}
        except Exception as e:
            msg = "%s: %s" % (type(e).__name__, str(e)[:300])
            self.db.end_task(tid, "error", {}, error=msg, started=started)
            self.db.event("error", "%s 汇总失败：%s" % (KINDS[kind]["name"], msg[:200]))
            self.log.exception("daily run_digest failed")
            return {"ok": False, "period": per, "message": msg}

    def _digest_pieces(self, kind: str, days: list[dict]) -> list[str]:
        """把区间内每天压成一小段素材：新闻用当日 AI 总结，资讯用当日标题。"""
        pieces = []
        for d in days:
            if kind == "news":
                s = (d.get("summary_md") or "").strip()
                if s:
                    pieces.append("【%s】\n%s" % (d["day"], s))
                    continue
                rows = self.db.daily_items(d["day"], "news", "cctv_news", 10)
                if rows:
                    pieces.append("【%s】\n%s" % (d["day"], "\n".join("- " + r["title"] for r in rows)))
            elif kind == "mil":
                s = (d.get("summary_md") or "").strip()
                if s:
                    pieces.append("【%s】\n%s" % (d["day"], s))
                    continue
                rows = self.db.daily_items(d["day"], "mil", None, 12)
                if rows:
                    pieces.append("【%s】\n%s" % (d["day"], "\n".join("- " + r["title"] for r in rows)))
            else:
                s = (d.get("summary_md") or "").strip()
                if s:
                    pieces.append("【%s】\n%s" % (d["day"], s))
                    continue
                rows = self.db.news_of_day(d["day"], 12)
                if rows:
                    pieces.append("【%s】\n%s" % (d["day"], "\n".join(
                        "- %s（%s）" % (r.get("title") or "", r.get("source") or "") for r in rows)))
        return pieces

    def _ai_digest(self, pieces: list[str], force: bool, system: str = DIGEST_SYSTEM) -> dict:
        total = sum(len(p) for p in pieces)
        if total <= CHUNK_CHARS:
            return self._summarize_or_none(system, "\n\n".join(pieces), 2000, force)
        # 超长区间：分段归纳（map）再合并（reduce）。每段额度随天数自动收紧
        per = max(200, min(600, CHUNK_CHARS * MAX_CHUNKS // max(1, len(pieces))))
        trimmed = [p if len(p) <= per else p[:per] + "…" for p in pieces]
        chunks, cur, cur_len = [], [], 0
        for p in trimmed:
            if cur and cur_len + len(p) > CHUNK_CHARS:
                chunks.append(cur)
                cur, cur_len = [], 0
            cur.append(p)
            cur_len += len(p)
        if cur:
            chunks.append(cur)
        notes = []
        for i, ch in enumerate(chunks, 1):
            r = self.ai.summarize(DIGEST_MAP_SYSTEM, "\n\n".join(ch), 900, force)
            if not r.get("ok"):
                # 预算用尽或调用失败：已归纳的部分照样可用，后面交给降级文本
                return {"available": False, "reason": r.get("reason") or "AI 未返回内容",
                        "text": "\n\n".join(notes), "partial": True}
            notes.append("【第 %d 段：%s 至 %s】\n%s" % (
                i, ch[0].split("】")[0].strip("【"), ch[-1].split("】")[0].strip("【"), r["text"]))
        return self._summarize_or_none(system, "\n\n".join(notes), 2000, force)

    def _summarize_or_none(self, system: str, user: str, max_tokens: int, force: bool) -> dict:
        r = self.ai.summarize(system, user, max_tokens, force)
        if not r.get("ok"):
            return {"available": False, "reason": _reason_text(r.get("reason")), "text": ""}
        return {"available": True, "text": r["text"], "usage": r.get("usage")}

    # ---------- 邮件 ----------
    def _send(self, kind: str, subject: str, md: str, report_type: str) -> dict:
        html = ('<div style="font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
                'font-size:14px;line-height:1.7;max-width:760px;color:#1F2430">%s'
                '<hr style="border:none;border-top:1px solid #E3E6EC;margin:20px 0">'
                '<p style="font-size:12px;color:#5B6472">%s</p></div>') % (md_to_html(md), _html.escape(DISCLAIMER_MD.strip("*")))
        report = {"report_date": now_bj().strftime("%Y-%m-%d"), "report_type": report_type,
                  "subject": subject, "html": html, "text_body": md, "signal_id": None}
        report["id"] = self.db.insert_report(report)
        delivery = self.mailer.send_report(report)
        self.db.audit("daily_mail", "%s 邮件 %s：%s" % (KINDS[kind]["name"], subject[:40],
                                                       "已发送" if delivery.get("ok") else delivery.get("message", "失败")))
        return delivery

    # ---------- 状态（界面用） ----------
    def state(self, kind: str) -> dict:
        cfg = self.cfg.get()
        sec = _sec(cfg, kind)
        now = now_bj()
        today = now.strftime("%Y-%m-%d")
        if kind == "news":
            rows = self.db.daily_items(today, "news", None, 400)
        elif kind == "mil":
            rows = self.db.daily_items(today, "mil", None, 300)
        else:
            rows = self.db.news_of_day(today, max(1, min(200, int(sec.get("max_items") or 40))))
        # 每条挂上正文缓存状态，界面据此决定点标题是看本机全文还是提示打开原文
        keys = [article_key(r.get("url") or "") for r in rows if r.get("url")]
        amap = self.db.article_many(keys)
        for r in rows:
            k = article_key(r.get("url") or "") if r.get("url") else ""
            c = amap.get(k) or {}
            r["akey"] = k
            r["art_status"] = c.get("status") or ""
            r["has_content"] = bool(c.get("status") == "ok" and (c.get("content") or ""))
        art_path = self.article_path(kind, today)
        doc = self.db.one("SELECT * FROM daily_doc WHERE day=? AND kind=?", (today, kind))
        docs = self.db.daily_docs(kind, None, None, 60)
        digests = self.db.recent_digests(kind, 12)
        hm = now.strftime("%H:%M")
        plan = cfg.get("daily_all") or {}
        enabled = plan.get("enabled") if plan else sec.get("enabled")
        t = str(plan.get("time") or sec.get("time") or "")
        next_daily = ("今天 %s" % t) if (t and t > hm) else ("明天 %s" % t)
        try:
            root = self.doc_root()
            next_digest = self.next_digest_text(kind, plan or sec)
        except Exception as e:
            root, next_digest = "", "文档目录不可用：%s" % str(e)[:80]
        return {"ok": True, "kind": kind, "name": KINDS[kind]["name"], "config": sec,
                "doc_dir": os.path.join(root, KINDS[kind]["folder"]), "doc_root": root,
                "today": today, "today_doc": doc, "items": rows,
                "article": {"with_content": len([r for r in rows if r.get("has_content")]),
                            "total": len(keys), "path": art_path,
                            "exists": os.path.isfile(art_path)},
                "docs": [{"day": d["day"], "path": d["path"], "ai_available": bool(d["ai_available"]),
                          "generated_at": d["generated_at"],
                          "counts": _loads(d.get("counts_json"))} for d in docs],
                "digests": digests, "next_daily": next_daily if enabled else "已关闭",
                "next_digest": next_digest if enabled else "已关闭",
                "digest_due": bool(enabled and any(self.due_range(k, plan or sec, None, p)
                                                  for k in KINDS for p in digest_periods(plan or sec))),
                "digest_periods": digest_periods(plan or sec)}

    def read_doc(self, kind: str, path: str) -> dict:
        """读取文档正文；只允许读文档目录之内的文件。"""
        root = os.path.realpath(self.doc_root())
        p = os.path.realpath(path)
        if not p.startswith(root + os.sep) or not os.path.isfile(p):
            return {"ok": False, "message": "文件不存在或不在文档目录内"}
        with open(p, "r", encoding="utf-8") as f:
            return {"ok": True, "kind": kind, "path": p, "markdown": f.read()}


def _loads(s):
    try:
        return json.loads(s or "{}")
    except (ValueError, TypeError):
        return {}


__all__ = ["DailyService", "collect_daily", "md_to_html", "CCTV_COLUMNS", "HOT_SOURCES",
           "KINDS", "DIGEST_PERIOD_CN"]
