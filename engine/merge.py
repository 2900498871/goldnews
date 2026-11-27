# -*- coding: utf-8 -*-
"""合并导入另一台电脑的备份。

原则：只补充本机缺少的数据，不覆盖本机配置、密钥、持仓和调度状态。
同一业务键的记录保留本机版本；同名但内容不同的每日文档另存为副本。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile

from .export import _exports_dir, _unique, backup_zip
from .util import now_bj

DOC_PREFIX = "documents/"
MAX_DOC_BYTES = 40 * 1024 * 1024


def _norm(value) -> str:
    return "" if value is None else str(value)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute('PRAGMA table_info("%s")' % table)]


def _safe_rel(name: str) -> str:
    rel = name.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts) or ":" in parts[0]:
        raise ValueError("备份内含非法文档路径")
    return "/".join(parts)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_source(name: str, exports_dir: str):
    """把备份里的数据库和文档解到临时目录，返回 (临时目录, 数据库, 文档目录, manifest)。"""
    if not name or os.path.basename(name) != name or not name.lower().endswith(".zip"):
        raise ValueError("备份文件名不合法")
    path = os.path.join(exports_dir, name)
    if not os.path.isfile(path):
        raise ValueError("备份文件不存在，请先把另一台电脑的备份放入 exports 目录")
    tmp = tempfile.mkdtemp(prefix="gd-merge-")
    docs = os.path.join(tmp, "documents")
    os.makedirs(docs, exist_ok=True)
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if "backup_manifest.json" not in names or "golddesk.db" not in names:
                raise ValueError("不是本程序生成的备份（缺 manifest 或数据库）")
            manifest = json.loads(z.read("backup_manifest.json").decode("utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("备份 manifest 格式不对")
            if manifest.get("app") not in (None, "GoldDesk", "日常小助手"):
                raise ValueError("备份来源不是本程序")
            db_path = os.path.join(tmp, "golddesk.db")
            with z.open("golddesk.db") as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            total = 0
            for info in z.infolist():
                if info.is_dir() or not info.filename.startswith(DOC_PREFIX):
                    continue
                rel = _safe_rel(info.filename[len(DOC_PREFIX):])
                if not rel.lower().endswith((".md", ".html")):
                    continue
                if info.file_size > MAX_DOC_BYTES:
                    raise ValueError("备份内文档过大：%s" % rel)
                total += info.file_size
                if total > 512 * 1024 * 1024:
                    raise ValueError("备份内文档总量过大")
                dest = os.path.join(docs, *rel.split("/"))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        con = sqlite3.connect(db_path)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库完整性检查未通过")
        finally:
            con.close()
        return tmp, db_path, docs, manifest
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _local_key_set(con, table: str, cols: list[str], key_fn) -> set:
    if not _table_exists(con, table):
        return set()
    keys = set()
    for row in con.execute('SELECT %s FROM "%s"' % (",".join('"%s"' % c for c in cols), table)):
        d = dict(zip(cols, row))
        k = key_fn(d)
        if k is not None:
            keys.add(k)
    return keys


def _merge_table(local, src, table: str, key_fn, transform=None) -> dict:
    """按业务键补充缺失行；返回新增/重复/跳过数量。"""
    if not _table_exists(src, table) or not _table_exists(local, table):
        return {"added": 0, "duplicate": 0, "skipped": 0}
    scols = _columns(src, table)
    lcols = _columns(local, table)
    cols = [c for c in scols if c in lcols and c != "id"]
    if not cols:
        return {"added": 0, "duplicate": 0, "skipped": 0}
    keys = _local_key_set(local, table, cols, key_fn)
    added = duplicate = skipped = 0
    sql = 'INSERT OR IGNORE INTO "%s" (%s) VALUES (%s)' % (
        table, ",".join('"%s"' % c for c in cols), ",".join("?" for _ in cols))
    for row in src.execute('SELECT %s FROM "%s"' % (",".join('"%s"' % c for c in scols), table)):
        d = dict(zip(scols, row))
        if transform:
            d = transform(d)
            if d is None:
                skipped += 1
                continue
        k = key_fn(d)
        if k is None:
            skipped += 1
            continue
        if k in keys:
            duplicate += 1
            continue
        cur = local.execute(sql, [d.get(c) for c in cols])
        if cur.rowcount:
            added += 1
            keys.add(k)
        else:
            duplicate += 1
    return {"added": added, "duplicate": duplicate, "skipped": skipped}


def _doc_path_map(docs_root: str, local_root: str):
    """返回 archive_rel -> 本地绝对路径，以及文档补充/重复/冲突统计。"""
    mapping = {}
    stat = {"added": 0, "duplicate": 0, "conflict": 0, "conflicts": []}
    stamp = now_bj().strftime("%Y%m%d-%H%M%S")
    added_files = []
    for base, _dirs, files in os.walk(docs_root):
        for fn in files:
            src = os.path.join(base, fn)
            rel = os.path.relpath(src, docs_root).replace(os.sep, "/")
            parts = rel.split("/")
            dst = os.path.join(local_root, *parts)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            mapping[rel] = dst
            if not os.path.exists(dst):
                shutil.copy2(src, dst)
                added_files.append(dst)
                stat["added"] += 1
                continue
            if os.path.isfile(dst) and _sha256(src) == _sha256(dst):
                stat["duplicate"] += 1
                continue
            base_name, ext = os.path.splitext(dst)
            # 同一份备份重复合并时，不再生成新的冲突副本。
            src_hash = _sha256(src)
            folder = os.path.dirname(dst)
            prefix = os.path.basename(base_name) + "-另一台电脑-"
            for old in os.listdir(folder):
                if old.startswith(prefix) and old.lower().endswith(ext.lower()):
                    old_path = os.path.join(folder, old)
                    if os.path.isfile(old_path) and _sha256(old_path) == src_hash:
                        stat["duplicate"] += 1
                        return_marker = True
                        break
            else:
                return_marker = False
            if return_marker:
                continue
            alt = _unique("%s-另一台电脑-%s%s" % (base_name, stamp, ext))
            shutil.copy2(src, alt)
            added_files.append(alt)
            stat["conflict"] += 1
            if len(stat["conflicts"]) < 20:
                stat["conflicts"].append(os.path.relpath(alt, local_root).replace(os.sep, "/"))
    return mapping, stat, added_files


def _map_doc_path(path: str, mapping: dict) -> str:
    p = _norm(path).replace("\\", "/")
    best = ""
    for rel in mapping:
        if p == rel or p.endswith("/" + rel):
            if len(rel) > len(best):
                best = rel
    return mapping.get(best, "") if best else ""


def _merge_reports(local, src, report_map: dict) -> dict:
    """报告单独处理：保留本机同键记录，并把来源 report id 映射到本机 id。"""
    if not _table_exists(src, "daily_report") or not _table_exists(local, "daily_report"):
        return {"added": 0, "duplicate": 0, "skipped": 0}
    scols = _columns(src, "daily_report")
    lcols = _columns(local, "daily_report")
    cols = [c for c in scols if c in lcols and c != "id"]
    key_cols = [c for c in ("report_date", "report_type", "subject", "generated_at") if c in cols]
    if not key_cols:
        return {"added": 0, "duplicate": 0, "skipped": 0}
    scols_no_id = [c for c in scols if c != "id"]

    local_ids = {}
    for row in local.execute('SELECT id,%s FROM daily_report' %
                             ",".join('"%s"' % c for c in key_cols)):
        d = dict(zip(["id"] + key_cols, row))
        local_ids[tuple(_norm(d.get(c)) for c in key_cols)] = d["id"]
    added = duplicate = 0
    for row in src.execute('SELECT id,%s FROM daily_report' %
                           ",".join('"%s"' % c for c in scols_no_id)):
        d = dict(zip(["id"] + scols_no_id, row))
        k = tuple(_norm(d.get(c)) for c in key_cols)
        if k in local_ids:
            report_map[d["id"]] = local_ids[k]
            duplicate += 1
            continue
        vals = [None if c == "signal_id" else d.get(c) for c in cols]
        cur = local.execute('INSERT INTO daily_report (%s) VALUES (%s)' % (
            ",".join('"%s"' % c for c in cols), ",".join("?" for _ in cols)), vals)
        rid = cur.lastrowid
        report_map[d["id"]] = rid
        local_ids[k] = rid
        added += 1
    return {"added": added, "duplicate": duplicate, "skipped": 0}


def merge_backup(inst, name: str) -> dict:
    """合并另一台电脑的备份；成功返回各表新增数量与文档统计。"""
    exports_dir = _exports_dir(inst)
    tmp = None
    added_docs = []
    con = None
    try:
        tmp, src_db, docs_root, manifest = _extract_source(name, exports_dir)
        local_root = os.path.abspath(inst.daily.doc_root())

        # 合并前先给本机做一份完整备份，失败则中止。
        pre = backup_zip(inst, False, "")
        if not pre.get("ok"):
            raise RuntimeError("合并前自动备份失败，已中止：%s" % (pre.get("message") or "未知错误"))

        mapping, doc_stat, added_docs = _doc_path_map(docs_root, local_root)

        con = sqlite3.connect(inst.db.path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        src = sqlite3.connect(src_db)
        con.execute("BEGIN IMMEDIATE")
        try:
            by_table = {}

            by_table["news_item"] = _merge_table(
                con, src, "news_item",
                lambda d: ("news", _norm(d.get("dedup_hash")) or
                           (_norm(d.get("title")), _norm(d.get("url")), _norm(d.get("published_at_bj")))))
            by_table["daily_item"] = _merge_table(
                con, src, "daily_item",
                lambda d: ("daily", _norm(d.get("dedup_hash")) or
                           (_norm(d.get("day")), _norm(d.get("kind")), _norm(d.get("src")),
                            _norm(d.get("title")))))
            by_table["article_cache"] = _merge_table(
                con, src, "article_cache", lambda d: ("article", _norm(d.get("key"))))
            by_table["price_tick"] = _merge_table(
                con, src, "price_tick",
                lambda d: ("price", _norm(d.get("symbol")), _norm(d.get("fetched_at")),
                           _norm(d.get("source")), _norm(d.get("role")), _norm(d.get("is_alternate"))))
            by_table["task_run"] = _merge_table(
                con, src, "task_run",
                lambda d: ("task", _norm(d.get("task_name")), _norm(d.get("started_at")),
                           _norm(d.get("finished_at")), _norm(d.get("status")),
                           _norm(d.get("trigger_source")), _norm(d.get("error"))),
                transform=lambda d: None if _norm(d.get("status")) == "running" else d)
            by_table["ai_signal"] = _merge_table(
                con, src, "ai_signal",
                lambda d: ("signal", _norm(d.get("as_of_bj")), _norm(d.get("horizon")),
                           _norm(d.get("direction")), _norm(d.get("snapshot_hash")), _norm(d.get("model"))))
            by_table["daily_doc"] = _merge_table(
                con, src, "daily_doc", lambda d: ("doc", _norm(d.get("day")), _norm(d.get("kind"))),
                transform=lambda d: _with_local_path(d, mapping))
            by_table["digest_log"] = _merge_table(
                con, src, "digest_log",
                lambda d: ("digest", _norm(d.get("kind")), _norm(d.get("period")),
                           _norm(d.get("start_date")), _norm(d.get("end_date")), _norm(d.get("label"))),
                transform=lambda d: _with_local_path(d, mapping, required=False))
            by_table["history_analysis"] = _merge_table(
                con, src, "history_analysis",
                lambda d: ("history", _norm(d.get("start_date")), _norm(d.get("end_date")),
                           _norm(d.get("preset")), _norm(d.get("created_at"))),
                transform=lambda d: _with_report_id(d, None))
            by_table["config_log"] = _merge_table(
                con, src, "config_log",
                lambda d: ("cfg", _norm(d.get("version")), _norm(d.get("changed_at")), _norm(d.get("summary"))))
            by_table["audit_log"] = _merge_table(
                con, src, "audit_log",
                lambda d: ("audit", _norm(d.get("at")), _norm(d.get("action")), _norm(d.get("detail")),
                           _norm(d.get("instance_id"))))
            by_table["event_log"] = _merge_table(
                con, src, "event_log",
                lambda d: ("event", _norm(d.get("at")), _norm(d.get("level")), _norm(d.get("message"))))

            report_map = {}
            by_table["daily_report"] = _merge_reports(con, src, report_map)
            by_table["delivery_log"] = _merge_table(
                con, src, "delivery_log",
                lambda d: ("delivery", _norm(d.get("message_id"))),
                transform=lambda d: _with_report_id(d, report_map))

            con.commit()
        except Exception:
            con.rollback()
            raise

        # 合并后的审计只写本机，失败不影响主结果。
        for t in ("news_item", "daily_item", "price_tick", "task_run", "daily_report"):
            by_table.setdefault(t, {"added": 0, "duplicate": 0, "skipped": 0})
        summary = {
            "documents": doc_stat,
            "news": by_table["news_item"]["added"] + by_table["daily_item"]["added"],
            "articles": by_table["article_cache"]["added"],
            "prices": by_table["price_tick"]["added"],
            "tasks": by_table["task_run"]["added"],
            "signals": by_table["ai_signal"]["added"],
            "reports": by_table["daily_report"]["added"],
            "deliveries": by_table["delivery_log"]["added"],
            "history": by_table["history_analysis"]["added"],
            "logs": by_table["audit_log"]["added"] + by_table["event_log"]["added"] + by_table["config_log"]["added"],
        }
        msg = ("合并完成：文档 +%d（重复 %d、冲突另存 %d）；新闻/榜单 +%d；正文缓存 +%d；"
               "价格 +%d；任务记录 +%d；AI 研判 +%d；报告 +%d" % (
                   doc_stat["added"], doc_stat["duplicate"], doc_stat["conflict"],
                   summary["news"], summary["articles"], summary["prices"], summary["tasks"],
                   summary["signals"], summary["reports"]))
        try:
            inst.db.audit("merge_import", "from=%s docs=+%d/%d tasks=+%d" % (
                name, doc_stat["added"], doc_stat["conflict"], by_table["task_run"]["added"]))
            inst.db.event("info", msg)
        except Exception:
            pass
        return {
            "ok": True,
            "message": msg,
            "backup": name,
            "pre_merge_backup": pre.get("path") or "",
            "documents": doc_stat,
            "by_table": by_table,
            "summary": summary,
        }
    except Exception as e:
        # 数据库回滚后，撤销这次复制的文档（包括冲突副本）。
        for p in reversed(added_docs):
            try:
                os.remove(p)
            except OSError:
                pass
        return {"ok": False, "message": "合并失败：%s: %s" % (type(e).__name__, e)}
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def _with_local_path(d: dict, mapping: dict, required: bool = True):
    p = _map_doc_path(d.get("path") or "", mapping)
    if not p:
        if required:
            return None
        d = dict(d)
        d["path"] = ""
        return d
    d = dict(d)
    d["path"] = p
    return d


def _with_report_id(d: dict, report_map):
    d = dict(d)
    if report_map is not None:
        d["report_id"] = report_map.get(d.get("report_id"))
    else:
        d["report_id"] = None
    return d
