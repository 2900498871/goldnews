# -*- coding: utf-8 -*-
"""导出与备份：日报 zip / Word / PDF、持仓 CSV、整库备份 zip。"""
from __future__ import annotations

import csv
import html
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import datetime

from .daily import md_to_html
from .util import now_bj

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def _exports_dir(inst) -> str:
    d = os.path.join(inst.data_dir, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def _stamp() -> str:
    return now_bj().strftime("%Y%m%d-%H%M%S")


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", name).strip("_") or "export"


def export_daily_month(inst, kind: str, month: str) -> dict:
    """把某月某模块的 Markdown 与同日正文 HTML 打包成 zip。"""
    if kind not in ("news", "brief", "mil"):
        kind = "news"
    month = month or now_bj().strftime("%Y-%m")
    root = inst.daily.doc_root()
    folder = {"news": "每日新闻", "brief": "每日资讯", "mil": "每日军事新闻"}.get(kind, "每日新闻")
    base = os.path.join(root, folder, month)
    if not os.path.isdir(base):
        return {"ok": False, "message": "这个月份还没有文档：%s" % base}
    out = os.path.join(_exports_dir(inst), _safe("%s-%s-%s" % (folder, month, _stamp())) + ".zip")
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(base)):
            if name.endswith(".md") or name.lower().endswith(".正文.html"):
                z.write(os.path.join(base, name), "%s/%s" % (month, name))
                n += 1
    return {"ok": True, "path": out, "files": n, "size": os.path.getsize(out)}


def export_doc(inst, path: str, fmt: str) -> dict:
    """单份 Markdown 导出为 .doc（Word 可直接打开）或 PDF（调用 Edge 无头打印）。"""
    root = os.path.abspath(inst.daily.doc_root())
    p = os.path.abspath(path or "")
    if not p.startswith(root + os.sep) or not os.path.isfile(p):
        return {"ok": False, "message": "只能导出本端文档目录内的文件"}
    if not p.lower().endswith(".md"):
        return {"ok": False, "message": "目前只支持导出 Markdown 文档"}
    with open(p, "r", encoding="utf-8") as f:
        md = f.read()
    title = os.path.splitext(os.path.basename(p))[0]
    body = '<div style="font-family:\'Microsoft YaHei\',sans-serif;font-size:14px;line-height:1.7;max-width:760px">%s</div>' % md_to_html(md)
    if fmt == "doc":
        out = os.path.join(_exports_dir(inst), _safe(title) + "-" + _stamp() + ".doc")
        with open(out, "w", encoding="utf-8") as f:
            f.write('<html><head><meta charset="utf-8"><title>%s</title></head><body>%s</body></html>' %
                    (html.escape(title), body))
        return {"ok": True, "path": out, "format": "doc", "size": os.path.getsize(out)}
    if fmt == "pdf":
        edge = next((x for x in EDGE_CANDIDATES if os.path.isfile(x)), None)
        if not edge:
            return {"ok": False, "message": "没找到 Edge/Chrome，无法生成 PDF；可先导出 Word"}
        tmpdir = tempfile.mkdtemp(prefix="golddesk-pdf-")
        html_path = os.path.join(tmpdir, "doc.html")
        pdf_path = os.path.join(_exports_dir(inst), _safe(title) + "-" + _stamp() + ".pdf")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write('<html><head><meta charset="utf-8"><title>%s</title></head><body>%s</body></html>' %
                    (html.escape(title), body))
        try:
            subprocess.run([edge, "--headless=new", "--disable-gpu", "--no-first-run",
                            "--print-to-pdf=" + pdf_path, "file:///" + html_path.replace("\\", "/")],
                           capture_output=True, timeout=60)
        except Exception as e:
            return {"ok": False, "message": "PDF 生成失败：%s" % e}
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) < 1000:
            return {"ok": False, "message": "PDF 生成失败（浏览器没有输出文件）"}
        return {"ok": True, "path": pdf_path, "format": "pdf", "size": os.path.getsize(pdf_path)}
    return {"ok": False, "message": "未知导出格式"}


def export_trades_csv(inst) -> dict:
    """导出持仓快照与交易审计为 CSV。"""
    out = os.path.join(_exports_dir(inst), "持仓与交易-%s.csv" % _stamp())
    h = inst.db.holdings() or {}
    rows = [["类型", "时间", "克数", "成本单价", "备注"],
            ["当前持仓", h.get("updated_at") or "", h.get("grams") or "", h.get("cost_per_gram") or "",
             h.get("note") or ""]]
    for a in inst.db.recent_audit(200):
        if a.get("action") in ("trade_simulated", "holdings"):
            rows.append(["审计", a.get("at") or "", "", "", a.get("detail") or ""])
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    return {"ok": True, "path": out, "size": os.path.getsize(out)}


def backup_zip(inst) -> dict:
    """打包 config、数据库（热备份）与全部每日文档。"""
    out = os.path.join(_exports_dir(inst), "GoldDesk-备份-%s.zip" % _stamp())
    tmpdb = os.path.join(tempfile.mkdtemp(prefix="golddesk-db-"), "golddesk.db")
    try:
        src = sqlite3.connect(os.path.join(inst.data_dir, "golddesk.db"))
        dst = sqlite3.connect(tmpdb)
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        doc_root = inst.daily.doc_root()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("config.json", "config.backup.json"):
                p = os.path.join(inst.data_dir, name)
                if os.path.isfile(p):
                    z.write(p, name)
            z.write(tmpdb, "golddesk.db")
            if os.path.isdir(doc_root):
                skip = {"browser-profile", "exports", "logs", "cache", "probe"}
                for base, dirs, files in os.walk(doc_root):
                    dirs[:] = [d for d in dirs if d not in skip]
                    for fn in files:
                        if not (fn.endswith(".md") or fn.lower().endswith(".html")):
                            continue
                        p = os.path.join(base, fn)
                        z.write(p, os.path.join("documents", os.path.relpath(p, doc_root)))
        return {"ok": True, "path": out, "size": os.path.getsize(out)}
    finally:
        shutil.rmtree(os.path.dirname(tmpdb), ignore_errors=True)


def open_path(inst, path: str) -> dict:
    """只在数据目录（含导出目录）内打开文件夹，防止任意路径调用。"""
    p = os.path.abspath(path or "")
    root = os.path.abspath(inst.data_dir)
    if not (p == root or p.startswith(root + os.sep)):
        return {"ok": False, "message": "只能打开本端数据目录内的文件"}
    if not os.path.exists(p):
        return {"ok": False, "message": "文件不存在"}
    if os.path.isfile(p):
        p = os.path.dirname(p)
    try:
        os.startfile(p)
        return {"ok": True, "path": p}
    except Exception as e:
        return {"ok": False, "message": str(e)}
