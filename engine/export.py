# -*- coding: utf-8 -*-
"""导出与备份：日报 zip / Word / PDF、持仓 CSV、整库备份 zip。"""
from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import time
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

BACKUP_PREFIXES = ("GoldDesk-备份-", "日常小助手-备份-")
BACKUP_MANIFEST_VERSION = 2


def _exports_dir(inst) -> str:
    d = os.path.join(inst.data_dir, "exports")
    os.makedirs(d, exist_ok=True)
    return d


def _stamp() -> str:
    return now_bj().strftime("%Y%m%d-%H%M%S")


def _safe(name: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", name).strip("_") or "export"


def _unique(path: str) -> str:
    """同一秒内多次导出/备份时追加序号，避免互相覆盖。"""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists("%s-%d%s" % (base, i, ext)):
        i += 1
    return "%s-%d%s" % (base, i, ext)


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out = bytearray()
    block = 0
    while len(out) < n:
        out += hmac.new(key, nonce + block.to_bytes(4, "big"), hashlib.sha256).digest()
        block += 1
    return bytes(out[:n])


def _encrypt_json(obj: dict, password: str) -> bytes:
    """密码加密（scrypt 派生 + HMAC-SHA256 计数器流 + HMAC 校验），不依赖第三方库。"""
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=64)
    k_enc, k_mac = dk[:32], dk[32:]
    ct = bytes(a ^ b for a, b in zip(raw, _keystream(k_enc, nonce, len(raw))))
    tag = hmac.new(k_mac, b"GDSE1" + salt + nonce + ct, hashlib.sha256).digest()
    return json.dumps({"v": 1, "salt": base64.b64encode(salt).decode(),
                       "nonce": base64.b64encode(nonce).decode(),
                       "data": base64.b64encode(ct).decode(),
                       "tag": base64.b64encode(tag).decode()}, ensure_ascii=False).encode("utf-8")


def _decrypt_json(blob: bytes, password: str) -> dict:
    try:
        obj = json.loads(blob.decode("utf-8"))
        salt = base64.b64decode(obj["salt"])
        nonce = base64.b64decode(obj["nonce"])
        ct = base64.b64decode(obj["data"])
        tag = base64.b64decode(obj["tag"])
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=64)
        k_enc, k_mac = dk[:32], dk[32:]
        good = hmac.new(k_mac, b"GDSE1" + salt + nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(good, tag):
            raise ValueError("密码错误或备份已损坏")
        raw = bytes(a ^ b for a, b in zip(ct, _keystream(k_enc, nonce, len(ct))))
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise
    except Exception:
        raise ValueError("密码错误或备份已损坏")


FOLDERS = {"news": "每日新闻", "brief": "每日资讯", "mil": "每日军事新闻"}


def export_daily_range(inst, kind: str = "news", start: str = "", end: str = "") -> dict:
    """按日期区间打包日报与正文；kind=all 时三个板块一起打。"""
    types = ["news", "brief", "mil"] if kind == "all" else [kind if kind in FOLDERS else "news"]
    start = start or (now_bj().strftime("%Y-%m") + "-01")
    end = end or now_bj().strftime("%Y-%m-%d")
    root = inst.daily.doc_root()
    out = _unique(os.path.join(_exports_dir(inst),
                               _safe("日报-%s至%s-%s" % (start, end, _stamp())) + ".zip"))
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for k in types:
            folder = FOLDERS[k]
            base = os.path.join(root, folder)
            if not os.path.isdir(base):
                continue
            for ym in sorted(os.listdir(base)):
                if not re.fullmatch(r"\d{4}-\d{2}", ym):
                    continue
                for name in sorted(os.listdir(os.path.join(base, ym))):
                    day = name[:10]
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or day < start or day > end:
                        continue
                    if name.endswith(".md") or name.lower().endswith(".正文.html"):
                        p = os.path.join(base, ym, name)
                        z.write(p, "%s/%s/%s" % (folder, ym, name))
                        n += 1
    if n == 0:
        try:
            os.remove(out)
        except OSError:
            pass
        return {"ok": False, "message": "所选日期区间没有文档"}
    return {"ok": True, "path": out, "files": n, "size": os.path.getsize(out),
            "start": start, "end": end}


def export_daily_month(inst, kind: str, month: str) -> dict:
    """把某月某模块的 Markdown 与同日正文 HTML 打包成 zip。"""
    month = month or now_bj().strftime("%Y-%m")
    return export_daily_range(inst, kind, month + "-01", month + "-31")


def _markdown_to_docx(md: str, path: str, title: str) -> None:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Microsoft YaHei"
    style.font.size = Pt(10.5)
    try:
        style.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    except Exception:
        pass
    doc.add_heading(title, level=1)
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("```"):
            continue
        if line.startswith("#"):
            level = min(4, len(line) - len(line.lstrip("#")))
            doc.add_heading(re.sub(r"^#+\s*", "", line), level=level)
        elif re.match(r"^[-*+]\s+", line):
            doc.add_paragraph(re.sub(r"^[-*+]\s+", "", line), style="List Bullet")
        elif re.match(r"^\d+[.、]\s+", line):
            doc.add_paragraph(re.sub(r"^\d+[.、]\s+", "", line), style="List Number")
        elif line.startswith(">"):
            p = doc.add_paragraph(line.lstrip("> ").strip())
            p.italic = True
        else:
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
            text = re.sub(r"`(.+?)`", r"\1", text)
            doc.add_paragraph(text)
    doc.save(path)


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
    if fmt in ("doc", "docx"):
        from docx import Document  # noqa: F401
        out = _unique(os.path.join(_exports_dir(inst), _safe(title) + "-" + _stamp() + ".docx"))
        _markdown_to_docx(md, out, title)
        return {"ok": True, "path": out, "format": "docx", "size": os.path.getsize(out)}
    if fmt == "pdf":
        edge = next((x for x in EDGE_CANDIDATES if os.path.isfile(x)), None)
        if not edge:
            html_path = _unique(os.path.join(_exports_dir(inst),
                                             _safe(title) + "-" + _stamp() + "-打印.html"))
            with open(html_path, "w", encoding="utf-8") as f:
                f.write('<html><head><meta charset="utf-8"><title>%s</title></head><body>%s</body></html>' %
                        (html.escape(title), body))
            try:
                os.startfile(html_path)  # noqa: S606 - 本端导出目录内文件
            except Exception:
                pass
            return {"ok": False, "fallback": "print", "path": html_path,
                    "message": "没找到 Edge/Chrome，已打开可打印的 HTML；请在浏览器里 Ctrl+P 另存为 PDF"}
        tmpdir = tempfile.mkdtemp(prefix="golddesk-pdf-")
        html_path = os.path.join(tmpdir, "doc.html")
        pdf_path = _unique(os.path.join(_exports_dir(inst), _safe(title) + "-" + _stamp() + ".pdf"))
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


def _collect_secrets(inst) -> dict:
    from .config import SENSITIVE_FIELDS
    out = {}
    for dotted in SENSITIVE_FIELDS:
        val = inst.cfg.secret(dotted)
        if val:
            out[dotted] = val
    return out


def _prune_backups(inst):
    keep = int((inst.cfg.get().get("storage") or {}).get("backup_keep", 10) or 10)
    try:
        files = [os.path.join(_exports_dir(inst), n) for n in os.listdir(_exports_dir(inst))
                 if n.startswith(BACKUP_PREFIXES) and n.endswith(".zip")]
        files.sort(key=os.path.getmtime, reverse=True)
        for p in files[max(1, keep):]:
            os.remove(p)
    except OSError:
        pass


def backup_zip(inst, include_secrets: bool = False, password: str = "") -> dict:
    """打包 config、数据库（热备份）与全部每日文档；可选密码加密密钥。"""
    if include_secrets and len(password or "") < 6:
        return {"ok": False, "message": "包含密钥的备份必须设置至少 6 位密码"}
    out = _unique(os.path.join(_exports_dir(inst), "日常小助手-备份-%s.zip" % _stamp()))
    tmpdir = tempfile.mkdtemp(prefix="golddesk-db-")
    tmpdb = os.path.join(tmpdir, "golddesk.db")
    try:
        src = sqlite3.connect(os.path.join(inst.data_dir, "golddesk.db"))
        dst = sqlite3.connect(tmpdb)
        with dst:
            src.backup(dst)
        schema_version = int(dst.execute("PRAGMA user_version").fetchone()[0] or 0)
        src.close(); dst.close()
        doc_root = inst.daily.doc_root()
        files = []
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for name in ("config.json", "config.backup.json"):
                p = os.path.join(inst.data_dir, name)
                if os.path.isfile(p):
                    z.write(p, name)
            z.write(tmpdb, "golddesk.db")
            if os.path.isdir(doc_root):
                skip = {"browser-profile", "exports", "logs", "cache", "probe", "webview2"}
                for base, dirs, fs in os.walk(doc_root):
                    dirs[:] = [d for d in dirs if d not in skip]
                    for fn in fs:
                        if not (fn.endswith(".md") or fn.lower().endswith(".html")):
                            continue
                        p = os.path.join(base, fn)
                        arc = os.path.join("documents", os.path.relpath(p, doc_root))
                        z.write(p, arc)
                        files.append(arc)
            if include_secrets:
                sec = _collect_secrets(inst)
                z.writestr("secrets.enc", _encrypt_json(sec, password))
            manifest = {
                "app": "GoldDesk", "version": BACKUP_MANIFEST_VERSION,
                "schema_version": schema_version,
                "instance_id": inst.cfg.instance_id,
                "created_at": now_bj().isoformat(),
                "include_secrets": bool(include_secrets),
                "db_sha256": hashlib.sha256(open(tmpdb, "rb").read()).hexdigest(),
                "documents": len(files), "doc_root": os.path.basename(doc_root.rstrip(os.sep)),
            }
            z.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        _prune_backups(inst)
        return {"ok": True, "path": out, "size": os.path.getsize(out),
                "include_secrets": bool(include_secrets),
                "message": "备份完成（含加密密钥）" if include_secrets else "备份完成（不含密钥）"}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def list_backups(inst) -> dict:
    rows = []
    for name in os.listdir(_exports_dir(inst)):
        if not (name.startswith(BACKUP_PREFIXES) and name.endswith(".zip")):
            continue
        p = os.path.join(_exports_dir(inst), name)
        include_secrets = False
        try:
            with zipfile.ZipFile(p) as z:
                if "backup_manifest.json" in z.namelist():
                    include_secrets = bool(json.loads(
                        z.read("backup_manifest.json").decode("utf-8")).get("include_secrets"))
        except Exception:
            pass
        rows.append({"name": name, "size": os.path.getsize(p),
                     "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S"),
                     "include_secrets": include_secrets})
    rows.sort(key=lambda x: x["mtime"], reverse=True)
    return {"ok": True, "backups": rows[:50]}


def stage_restore(inst, name: str, password: str = "") -> dict:
    """校验备份并暂存到 data/restore_pending，重启后由 apply_pending_restore 应用。"""
    if not name or os.path.basename(name) != name or not name.endswith(".zip"):
        return {"ok": False, "message": "备份文件名不合法"}
    src = os.path.join(_exports_dir(inst), name)
    if not os.path.isfile(src):
        return {"ok": False, "message": "备份文件不存在"}
    pending = os.path.join(inst.data_dir, "restore_pending")
    tmp = pending + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    try:
        with zipfile.ZipFile(src) as z:
            names = set(z.namelist())
            if "backup_manifest.json" not in names or "golddesk.db" not in names:
                return {"ok": False, "message": "不是本程序生成的备份（缺 manifest 或数据库）"}
            manifest = json.loads(z.read("backup_manifest.json").decode("utf-8"))
            if manifest.get("db_sha256"):
                db_blob = z.read("golddesk.db")
                if hashlib.sha256(db_blob).hexdigest() != manifest["db_sha256"]:
                    return {"ok": False, "message": "备份数据库校验失败，可能已损坏"}
                with open(os.path.join(tmp, "golddesk.db"), "wb") as f:
                    f.write(db_blob)
            if "config.json" not in names:
                return {"ok": False, "message": "备份缺少配置文件"}
            with open(os.path.join(tmp, "config.json"), "wb") as f:
                f.write(z.read("config.json"))
            try:
                conf = json.loads(z.read("config.json").decode("utf-8"))
            except Exception:
                return {"ok": False, "message": "备份配置不是合法 JSON"}
            if not isinstance(conf, dict):
                return {"ok": False, "message": "备份配置格式不对"}
            # 校验备份库能否打开
            con = sqlite3.connect(os.path.join(tmp, "golddesk.db"))
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    return {"ok": False, "message": "备份数据库完整性检查未通过"}
            finally:
                con.close()
            doc_prefix = "documents/"
            for n in z.namelist():
                if not n.startswith(doc_prefix) or n.endswith("/"):
                    continue
                rel = os.path.normpath(n[len(doc_prefix):]).replace("\\", "/")
                if rel.startswith("../") or rel.startswith("/") or ":" in rel:
                    return {"ok": False, "message": "备份内含非法路径"}
                dest = os.path.join(tmp, "documents", rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(z.read(n))
            if "secrets.enc" in names:
                if len(password or "") < 6:
                    return {"ok": False, "message": "该备份含加密密钥，请输入备份密码"}
                sec = _decrypt_json(z.read("secrets.enc"), password)
                from .config import SENSITIVE_FIELDS
                patch = {}
                for dotted, val in sec.items():
                    if dotted not in SENSITIVE_FIELDS or not val:
                        continue
                    section, field = dotted.split(".", 1)
                    patch.setdefault(section, {})[field] = val
                if patch:
                    inst.cfg.update(patch)
            with open(os.path.join(tmp, "restore.json"), "w", encoding="utf-8") as f:
                conf_doc = str(conf.get("doc_dir") or "").strip()
                doc_root = os.path.abspath(os.path.expanduser(conf_doc)) if conf_doc else inst.data_dir
                cur_root = os.path.abspath(inst.daily.doc_root())
                if doc_root != inst.data_dir and doc_root != cur_root:
                    return {"ok": False, "message": "备份里的文档目录不在本端允许范围"}
                json.dump({"created_at": now_bj().isoformat(), "source": name,
                           "doc_root": doc_root,
                           "include_secrets": "secrets.enc" in names}, f, ensure_ascii=False)
        shutil.rmtree(pending, ignore_errors=True)
        os.replace(tmp, pending)
        return {"ok": True, "path": pending, "requires_restart": True,
                "include_secrets": "secrets.enc" in names,
                "message": "备份已校验并暂存；请退出程序后重新启动以完成恢复"}
    except ValueError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "message": str(e)}
    except Exception as e:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": False, "message": "恢复准备失败：%s: %s" % (type(e).__name__, e)}


def apply_pending_restore(data_dir: str) -> dict | None:
    """启动时应用暂存恢复；先把当前配置/库/文档移到 pre-restore 目录。"""
    pending = os.path.join(data_dir, "restore_pending")
    marker = os.path.join(pending, "restore.json")
    if not os.path.isfile(marker):
        return None
    try:
        meta = json.loads(open(marker, "r", encoding="utf-8").read()) or {}
    except Exception:
        meta = {}
    doc_root = os.path.abspath(meta.get("doc_root") or data_dir)
    try:
        cur_conf = json.loads(open(os.path.join(data_dir, "config.json"), "r", encoding="utf-8").read()) or {}
    except Exception:
        cur_conf = {}
    cur_root = os.path.abspath(os.path.expanduser(str(cur_conf.get("doc_dir") or "").strip())) \
        if str(cur_conf.get("doc_dir") or "").strip() else data_dir
    if doc_root not in (data_dir, cur_root):
        return None
    stamp = now_bj().strftime("%Y%m%d-%H%M%S")
    pre = os.path.join(data_dir, "pre-restore-" + stamp)
    os.makedirs(pre, exist_ok=True)
    moved = []
    for name in ("config.json", "config.backup.json", "golddesk.db",
                 "golddesk.db-wal", "golddesk.db-shm"):
        p = os.path.join(data_dir, name)
        if os.path.exists(p):
            shutil.move(p, os.path.join(pre, name))
            moved.append(name)
    docs = os.path.join(pending, "documents")
    if os.path.isdir(docs):
        if doc_root == data_dir:
            for top in os.listdir(docs):
                p = os.path.join(docs, top)
                cur = os.path.join(data_dir, top)
                if os.path.exists(cur):
                    if os.path.isdir(cur) and os.path.isdir(p):
                        shutil.move(cur, os.path.join(pre, top))
                    elif os.path.isfile(cur) and os.path.isfile(p):
                        shutil.move(cur, os.path.join(pre, top))
                if os.path.isdir(p):
                    shutil.copytree(p, cur, dirs_exist_ok=True)
                elif os.path.isfile(p):
                    shutil.copy2(p, cur)
        else:
            os.makedirs(os.path.dirname(doc_root), exist_ok=True)
            if os.path.exists(doc_root):
                shutil.move(doc_root, os.path.join(pre, os.path.basename(doc_root)))
            shutil.copytree(docs, doc_root, dirs_exist_ok=True)
    for name in ("config.json", "golddesk.db"):
        p = os.path.join(pending, name)
        if os.path.isfile(p):
            shutil.move(p, os.path.join(data_dir, name))
    shutil.rmtree(pending, ignore_errors=True)
    return {"applied": True, "pre_restore": pre, "moved": moved}


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
