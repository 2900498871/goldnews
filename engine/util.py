# -*- coding: utf-8 -*-
"""通用工具：时区、日志脱敏、HTTP、哈希。仅使用 Python 标准库。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    BJ = ZoneInfo("Asia/Shanghai")
except Exception:
    # Windows 常缺 tzdata；北京时间 1991 年后无夏令时，固定 +08:00 等价且零依赖
    BJ = timezone(timedelta(hours=8), "CST")
UTC = timezone.utc
OZ_TO_GRAM = 31.1034768

# 固定免责声明（PRD §15）：所有对外输出必须原样附加，不得改写、缩减或省略
DISCLAIMER = (
    "免责声明：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。"
    "市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标独立判断，"
    "必要时咨询持牌专业机构。过往表现不预示未来收益。"
)
DISCLAIMER_MD = "**" + DISCLAIMER + "**"


def now_utc() -> datetime:
    return datetime.now(UTC)


def now_bj() -> datetime:
    return datetime.now(BJ)


def iso_bj(dt: datetime | None = None) -> str:
    return (dt or now_bj()).isoformat(timespec="seconds")


def bj_str(dt: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return (dt or now_bj()).strftime(fmt)


def to_bj(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(BJ)


def short_hash(obj, n: int = 16) -> str:
    raw = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:n]


def mask(secret: str | None, keep: int = 4) -> str:
    if not secret:
        return ""
    if len(secret) <= keep:
        return "*" * len(secret)
    return secret[:2] + "*" * min(10, len(secret) - keep - 2) + secret[-keep:]


def redact(text: str, secrets: list[str] | None = None) -> str:
    """日志脱敏：移除密钥原文以及常见的 key/token 结构。"""
    if not text:
        return ""
    out = str(text)
    for s in secrets or []:
        if s and len(s) >= 6:
            out = out.replace(s, "***REDACTED***")
    out = re.sub(r"(?i)(api[_-]?key|authorization|bearer|password|credential|授权码)"
                 r"([\"'\s:=]+)([A-Za-z0-9_\-\.]{6,})", r"\1\2***REDACTED***", out)
    return out


def get_logger(name: str, log_path: str | None = None, level=logging.INFO):
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = lambda *args: now_bj().timetuple()
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    if log_path:
        try:
            from logging.handlers import RotatingFileHandler
            fh = RotatingFileHandler(log_path, maxBytes=10 * 1024 * 1024, backupCount=5,
                                     encoding="utf-8")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
        except OSError:
            pass
    return logger


def _maybe_gunzip(data: bytes, headers) -> bytes:
    """调用方主动要求 gzip 时（部分站点拒绝 identity 编码），在这里解压。"""
    if data[:2] == b"\x1f\x8b":
        import gzip
        try:
            return gzip.decompress(data)
        except Exception:
            return data
    return data


def http_get(url: str, timeout: float = 10.0, headers: dict | None = None,
             encoding: str | None = None, data: str | bytes | None = None) -> tuple[int, str, str]:
    """返回 (http_status, text, final_url)。统一加 UA，禁用压缩以便标准库直接解码。

    传入 data 时按表单 POST 发送（少数行情站点的图表接口只接受 POST）。
    """
    hd = {
        "User-Agent": "GoldDesk/1.0 (personal, single-user; contact: local)",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if headers:
        hd.update(headers)
    body = data.encode("utf-8") if isinstance(data, str) else data
    if body is not None and "Content-Type" not in hd:
        hd["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=hd,
                                 method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data, status, final = _maybe_gunzip(resp.read(), resp.headers), resp.status, resp.geturl()
    except urllib.error.HTTPError as e:
        data, status, final = _maybe_gunzip(e.read(), e.headers), e.code, e.geturl()
    except Exception as e:
        raise RuntimeError("请求失败: %s | %s" % (url, type(e).__name__ + ": " + str(e)[:120]))
    charset = encoding
    if charset is None:
        m = re.search(r"charset=([\w\-]+)", data[:600].decode("latin-1", "ignore"), re.I)
        charset = m.group(1) if m else "utf-8"
    try:
        text = data.decode(charset, "replace")
    except (LookupError, TypeError):
        text = data.decode("utf-8", "replace")
    return status, text, final


def http_post_json(url: str, payload: dict, headers: dict, timeout: float = 60.0) -> tuple[int, str]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    hd = {"Content-Type": "application/json", "Accept-Encoding": "identity",
          "User-Agent": "GoldDesk/1.0 (personal, single-user)"}
    hd.update(headers)
    req = urllib.request.Request(url, data=body, headers=hd, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        raise RuntimeError("请求失败: %s | %s" % (type(e).__name__, str(e)[:160]))


def json_loads_loose(text: str):
    """容忍 ```json 围栏、前后空白、仅含空格的 content。"""
    if not text or not text.strip():
        raise ValueError("空 content")
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("响应中未找到 JSON 对象")
    return json.loads(s[start:end + 1])


def safe_float(v, default=None):
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def hhmm_valid(v: str) -> bool:
    if not isinstance(v, str) or not re.fullmatch(r"\d{2}:\d{2}", v):
        return False
    h, m = int(v[:2]), int(v[3:])
    return 0 <= h <= 23 and 0 <= m <= 59


def in_quiet_hours(hhmm: str, start: str, end: str) -> bool:
    """支持跨日静默时段，如 23:00-07:00。"""
    if not (hhmm_valid(hhmm) and hhmm_valid(start) and hhmm_valid(end)):
        return False
    t = int(hhmm[:2]) * 60 + int(hhmm[3:])
    a = int(start[:2]) * 60 + int(start[3:])
    b = int(end[:2]) * 60 + int(end[3:])
    if a == b:
        return False
    return (a <= t or t < b) if a > b else (a <= t < b)


def log_line(logger, task: str, status: str, trigger: str = "scheduled", **kw):
    parts = [f"task={task}", f"status={status}", f"trigger={trigger}"]
    for k, v in kw.items():
        parts.append(f"{k}={redact(str(v))}")
    logger.info(" | ".join(parts))
