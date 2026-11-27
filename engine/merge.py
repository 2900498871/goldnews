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
import threading
import zipfile

from .export import BACKUP_MANIFEST_VERSION, _exports_dir, _unique, backup_zip
from .util import now_bj

DOC_PREFIX = "documents/"
MAX_DOC_BYTES = 40 * 1024 * 1024

# 界面上的勾选项；顺序也是界面展示顺序。
SECTIONS = (
    ("news", "新闻/榜单"),
    ("articles", "正文缓存"),
    ("prices", "黄金价格"),
    ("tasks", "任务记录"),
    ("signals", "AI 研判"),
    ("reports", "报告/发送记录"),
    ("history", "历史分析"),
    ("logs", "运行日志"),
    ("documents", "每日文档"),
)
DEFAULT_SECTIONS = tuple(k for k, _ in SECTIONS)
SECTION_LABELS = dict(SECTIONS)
SECTION_TABLES = {
    "news": ("news_item", "daily_item"),
    "articles": ("article_cache",),
    "prices": ("price_tick",),
    "tasks": ("task_run",),
    "signals": ("ai_signal",),
    "reports": ("daily_report", "delivery_log"),
    "history": ("history_analysis",),
    "logs": ("config_log", "audit_log", "event_log"),
}


def _norm(value) -> str:
    return "" if value is None else str(value)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _columns(con: sqlite3.Connection, table: str) -> list:
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


def _db_user_version(con: sqlite3.Connection) -> int:
    try:
        return int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
    except Exception:
        return 0


def _normalize_sections(value) -> list:
    allowed = list(DEFAULT_SECTIONS)
    if value is None or value == "":
        return allowed
    if isinstance(value, str):
        value = [x.strip() for x in value.split(",") if x.strip()]
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("合并类别格式不对")
    unknown = [x for x in value if x not in allowed]
    if unknown:
        raise ValueError("未知合并类别：%s" % "、".join(str(x) for x in unknown))
    selected = [x for x in allowed if x in value]
    if not selected:
        raise ValueError("至少选择一个合并类别")
    return selected


def _merge_lock(inst):
    lock = getattr(inst, "merge_lock", None)
    if lock is None:
        lock = threading.Lock()
        setattr(inst, "merge_lock", lock)
    return lock


def _merge_state(inst) -> dict:
    st = getattr(inst, "merge_state", None)
    if not isinstance(st, dict):
        st = {"running": False, "phase": "", "message": "", "percent": 0,
              "detail": "", "started_at": "", "finished_at": "", "error": ""}
        setattr(inst, "merge_state", st)
    return st


def _set_progress(inst, phase: str, message: str, percent: int, **extra):
    st = _merge_state(inst)
    st.update({
        "phase": phase, "message": message,
        "percent": max(0, min(100, int(percent))),
    })
    if extra:
        st.update(extra)


def merge_status(inst) -> dict:
    """供界面轮询的合并进度；不包含备份内容。"""
    st = dict(_merge_state(inst))
    st["ok"] = True
    return st


def _source_meta(name: str, manifest: dict, path: str) -> dict:
    return {
        "name": name,
        "size": os.path.getsize(path),
        "created_at": manifest.get("created_at") or "",
        "version": manifest.get("version"),
        "schema_version": manifest.get("schema_version"),
        "include_secrets": bool(manifest.get("include_secrets")),
        "documents": manifest.get("documents"),
        "instance_id": manifest.get("instance_id") or "",
        "app": manifest.get("app") or "",
    }


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
            try:
                manifest = json.loads(z.read("backup_manifest.json").decode("utf-8"))
            except Exception:
                raise ValueError("备份 manifest 不是合法 JSON")
            if not isinstance(manifest, dict):
                raise ValueError("备份 manifest 格式不对")
            if manifest.get("app") not in (None, "GoldDesk", "日常小助手"):
                raise ValueError("备份来源不是本程序")
            try:
                version = int(manifest.get("version"))
            except Exception:
                version = 0
            if version != BACKUP_MANIFEST_VERSION:
                raise ValueError("备份版本不兼容（备份 v%s，本机要求 v%s）" % (
                    version or "未知", BACKUP_MANIFEST_VERSION))
            expected_hash = _norm(manifest.get("db_sha256")).strip().lower()
            if not expected_hash:
                raise ValueError("备份 manifest 缺少数据库哈希，无法校验")
            declared_docs = manifest.get("documents")
            if not isinstance(declared_docs, int) or declared_docs < 0:
                raise ValueError("备份 manifest 缺少有效的文档数量")

            db_path = os.path.join(tmp, "golddesk.db")
            with z.open("golddesk.db") as src, open(db_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if _sha256(db_path) != expected_hash:
                raise ValueError("备份数据库哈希校验失败，文件可能不完整或被改动")

            total = 0
            extracted = 0
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
                extracted += 1
            if extracted != declared_docs:
                raise ValueError("备份文档数量与 manifest 不一致（manifest %d，实际 %d）" % (
                    declared_docs, extracted))

        con = sqlite3.connect(db_path)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份数据库完整性检查未通过")
            schema_version = _db_user_version(con)
        finally:
            con.close()

        declared_schema = manifest.get("schema_version")
        if declared_schema is not None:
            try:
                declared_schema = int(declared_schema)
            except Exception:
                raise ValueError("备份 manifest 的数据库版本不合法")
            if declared_schema != schema_version:
                raise ValueError("备份数据库版本与 manifest 不一致")
        manifest = dict(manifest)
        manifest["schema_version"] = schema_version
        manifest["_source_name"] = name
        manifest["_source_size"] = os.path.getsize(path)
        return tmp, db_path, docs, manifest
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _local_key_set(con, table: str, cols: list, key_fn) -> set:
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


def _doc_path_map(docs_root: str, local_root: str, write: bool = True, on_progress=None):
    """返回 archive_rel -> 本地绝对路径，以及文档补充/重复/冲突统计。

    write=False 时只做比对，不复制、不创建目录，用于合并预览。
    """
    mapping = {}
    stat = {"added": 0, "duplicate": 0, "conflict": 0, "conflicts": []}
    stamp = now_bj().strftime("%Y%m%d-%H%M%S")
    added_files = []
    files = []
    for base, _dirs, names in os.walk(docs_root):
        for fn in names:
            files.append((base, fn))
    total = len(files)
    try:
        for index, (base, fn) in enumerate(files, 1):
            src = os.path.join(base, fn)
            rel = os.path.relpath(src, docs_root).replace(os.sep, "/")
            parts = rel.split("/")
            dst = os.path.join(local_root, *parts)
            mapping[rel] = dst

            if not os.path.exists(dst):
                if write:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    added_files.append(dst)
                stat["added"] += 1
            elif os.path.isfile(dst) and _sha256(src) == _sha256(dst):
                stat["duplicate"] += 1
            else:
                src_hash = _sha256(src)
                existing_alt = None
                folder = os.path.dirname(dst)
                prefix = os.path.basename(os.path.splitext(dst)[0]) + "-另一台电脑-"
                try:
                    for old in os.listdir(folder):
                        if not old.startswith(prefix):
                            continue
                        old_path = os.path.join(folder, old)
                        if os.path.isfile(old_path) and _sha256(old_path) == src_hash:
                            existing_alt = old_path
                            break
                except OSError:
                    pass
                if existing_alt:
                    mapping[rel] = existing_alt
                    stat["duplicate"] += 1
                else:
                    base_name, ext = os.path.splitext(dst)
                    alt = _unique("%s-另一台电脑-%s%s" % (base_name, stamp, ext))
                    mapping[rel] = alt
                    if write:
                        os.makedirs(os.path.dirname(alt), exist_ok=True)
                        shutil.copy2(src, alt)
                        added_files.append(alt)
                    stat["conflict"] += 1
                    if len(stat["conflicts"]) < 20:
                        stat["conflicts"].append(os.path.relpath(alt, local_root).replace(os.sep, "/"))
            if on_progress:
                on_progress(index, total, rel)
    except Exception:
        for p in reversed(added_files):
            try:
                os.remove(p)
            except OSError:
                pass
        raise
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


def _merge_database(local, src, sections: list, mapping: dict) -> dict:
    by_table = {}
    if "news" in sections:
        by_table["news_item"] = _merge_table(
            local, src, "news_item",
            lambda d: ("news", _norm(d.get("dedup_hash")) or
                       (_norm(d.get("title")), _norm(d.get("url")), _norm(d.get("published_at_bj")))))
        by_table["daily_item"] = _merge_table(
            local, src, "daily_item",
            lambda d: ("daily", _norm(d.get("dedup_hash")) or
                       (_norm(d.get("day")), _norm(d.get("kind")), _norm(d.get("src")),
                        _norm(d.get("title")))))
    if "articles" in sections:
        by_table["article_cache"] = _merge_table(
            local, src, "article_cache", lambda d: ("article", _norm(d.get("key"))))
    if "prices" in sections:
        by_table["price_tick"] = _merge_table(
            local, src, "price_tick",
            lambda d: ("price", _norm(d.get("symbol")), _norm(d.get("fetched_at")),
                       _norm(d.get("source")), _norm(d.get("role")), _norm(d.get("is_alternate"))))
    if "tasks" in sections:
        by_table["task_run"] = _merge_table(
            local, src, "task_run",
            lambda d: ("task", _norm(d.get("task_name")), _norm(d.get("started_at")),
                       _norm(d.get("finished_at")), _norm(d.get("status")),
                       _norm(d.get("trigger_source")), _norm(d.get("error"))),
            transform=lambda d: None if _norm(d.get("status")) == "running" else d)
    if "signals" in sections:
        by_table["ai_signal"] = _merge_table(
            local, src, "ai_signal",
            lambda d: ("signal", _norm(d.get("as_of_bj")), _norm(d.get("horizon")),
                       _norm(d.get("direction")), _norm(d.get("snapshot_hash")), _norm(d.get("model"))))

    report_map = {}
    if "reports" in sections:
        by_table["daily_report"] = _merge_reports(local, src, report_map)
        by_table["delivery_log"] = _merge_table(
            local, src, "delivery_log",
            lambda d: ("delivery", _norm(d.get("message_id"))),
            transform=lambda d: _with_report_id(d, report_map))

    if "history" in sections:
        by_table["history_analysis"] = _merge_table(
            local, src, "history_analysis",
            lambda d: ("history", _norm(d.get("start_date")), _norm(d.get("end_date")),
                       _norm(d.get("preset")), _norm(d.get("created_at"))),
            transform=lambda d: _with_report_id(d, report_map if "reports" in sections else None))

    if "logs" in sections:
        by_table["config_log"] = _merge_table(
            local, src, "config_log",
            lambda d: ("cfg", _norm(d.get("version")), _norm(d.get("changed_at")), _norm(d.get("summary"))))
        by_table["audit_log"] = _merge_table(
            local, src, "audit_log",
            lambda d: ("audit", _norm(d.get("at")), _norm(d.get("action")), _norm(d.get("detail")),
                       _norm(d.get("instance_id"))))
        by_table["event_log"] = _merge_table(
            local, src, "event_log",
            lambda d: ("event", _norm(d.get("at")), _norm(d.get("level")), _norm(d.get("message"))))

    if "documents" in sections:
        by_table["daily_doc"] = _merge_table(
            local, src, "daily_doc", lambda d: ("doc", _norm(d.get("day")), _norm(d.get("kind"))),
            transform=lambda d: _with_local_path(d, mapping))
        by_table["digest_log"] = _merge_table(
            local, src, "digest_log",
            lambda d: ("digest", _norm(d.get("kind")), _norm(d.get("period")),
                       _norm(d.get("start_date")), _norm(d.get("end_date")), _norm(d.get("label"))),
            transform=lambda d: _with_local_path(d, mapping, required=False))
    return by_table


def _empty_table_stat():
    return {"added": 0, "duplicate": 0, "skipped": 0}


def _empty_doc_stat():
    return {"added": 0, "duplicate": 0, "conflict": 0, "conflicts": []}


def _category_stats(by_table: dict, doc_stat: dict, sections: list) -> list:
    rows = []
    for key, label in SECTIONS:
        selected = key in sections
        if key == "documents":
            added = doc_stat["added"] if selected else 0
            duplicate = doc_stat["duplicate"] if selected else 0
            skipped = 0
            conflict = doc_stat["conflict"] if selected else 0
        else:
            tables = SECTION_TABLES.get(key, ())
            added = sum((by_table.get(t) or _empty_table_stat())["added"] for t in tables) if selected else 0
            duplicate = sum((by_table.get(t) or _empty_table_stat())["duplicate"] for t in tables) if selected else 0
            skipped = sum((by_table.get(t) or _empty_table_stat())["skipped"] for t in tables) if selected else 0
            conflict = 0
        rows.append({"key": key, "label": label, "selected": selected,
                     "added": added, "duplicate": duplicate, "skipped": skipped,
                     "conflict": conflict})
    return rows


def _summary(by_table: dict, doc_stat: dict) -> dict:
    def added(*tables):
        return sum((by_table.get(t) or _empty_table_stat())["added"] for t in tables)

    return {
        "documents": doc_stat,
        "news": added("news_item", "daily_item"),
        "articles": added("article_cache"),
        "prices": added("price_tick"),
        "tasks": added("task_run"),
        "signals": added("ai_signal"),
        "reports": added("daily_report"),
        "deliveries": added("delivery_log"),
        "history": added("history_analysis"),
        "logs": added("config_log", "audit_log", "event_log"),
    }


def _result_message(doc_stat: dict, by_table: dict, dry_run: bool = False) -> str:
    prefix = "预览：" if dry_run else "合并完成："
    return ("%s文档 新增%d、重复%d、冲突另存%d；新闻/榜单 +%d；正文缓存 +%d；"
            "价格 +%d；任务 +%d；AI 研判 +%d；报告 +%d；发送记录 +%d；历史 +%d；日志 +%d") % (
        prefix, doc_stat.get("added", 0), doc_stat.get("duplicate", 0), doc_stat.get("conflict", 0),
        _summary(by_table, doc_stat)["news"], _summary(by_table, doc_stat)["articles"],
        _summary(by_table, doc_stat)["prices"], _summary(by_table, doc_stat)["tasks"],
        _summary(by_table, doc_stat)["signals"], _summary(by_table, doc_stat)["reports"],
        _summary(by_table, doc_stat)["deliveries"], _summary(by_table, doc_stat)["history"],
        _summary(by_table, doc_stat)["logs"])


def _pause_collection(inst) -> bool:
    sch = getattr(inst, "scheduler", None)
    if sch is None:
        return False
    if hasattr(sch, "is_busy") and sch.is_busy():
        raise ValueError("正在生成每日文档或汇总，请等本次任务结束后再合并")
    if hasattr(sch, "pause"):
        sch.pause("正在合并另一台电脑备份")
        return True
    return False


def _resume_collection(inst, paused: bool):
    if not paused:
        return
    sch = getattr(inst, "scheduler", None)
    if sch is not None and hasattr(sch, "resume"):
        sch.resume()


def _run_merge(inst, name: str, sections: list, dry_run: bool = False) -> dict:
    tmp = None
    src = None
    con = None
    added_docs = []
    pipe_lock = None
    pipe_locked = False
    paused = False
    pre = None
    try:
        _set_progress(inst, "validate", "正在读取并校验备份…", 4, running=not dry_run)
        tmp, src_db, docs_root, manifest = _extract_source(name, _exports_dir(inst))
        meta = _source_meta(name, manifest, os.path.join(_exports_dir(inst), name))
        local_root = os.path.abspath(inst.daily.doc_root())

        if not dry_run:
            if _pause_collection(inst):
                paused = True
            _set_progress(inst, "wait", "正在暂停采集并等待当前任务结束…", 12)
            pipe = getattr(inst, "pipeline", None)
            pipe_lock = getattr(pipe, "lock", None) if pipe is not None else None
            if pipe_lock is not None:
                pipe_lock.acquire()
                pipe_locked = True

            _set_progress(inst, "backup", "正在生成合并前本机备份…", 16)
            pre = backup_zip(inst, False, "")
            if not pre.get("ok"):
                raise RuntimeError("合并前自动备份失败，已中止：%s" % (pre.get("message") or "未知错误"))

        if "documents" in sections:
            _set_progress(inst, "documents", "正在比对每日文档…", 24 if not dry_run else 20)
            mapping, doc_stat, added_docs = _doc_path_map(
                docs_root, local_root, write=not dry_run,
                on_progress=lambda done, total, rel: _set_progress(
                    inst, "documents", "正在处理每日文档 %d/%d：%s" % (done, total, rel),
                    24 + int(32 * done / max(1, total)) if not dry_run else 20 + int(30 * done / max(1, total)),
                    detail="%d/%d" % (done, total)))
        else:
            mapping, doc_stat = {}, _empty_doc_stat()

        _set_progress(inst, "database", "正在合并数据库记录…", 62 if not dry_run else 55)
        con = sqlite3.connect(inst.db.path, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        src = sqlite3.connect(src_db)
        local_schema = _db_user_version(con)
        src_schema = _db_user_version(src)
        if src_schema != local_schema:
            raise ValueError("备份数据库版本 v%s 与本机 v%s 不一致，请使用同版本程序生成的备份" % (
                src_schema, local_schema))
        con.execute("BEGIN IMMEDIATE")
        try:
            by_table = _merge_database(con, src, sections, mapping)
            if dry_run:
                con.rollback()
            else:
                con.commit()
        except Exception:
            con.rollback()
            raise

        summary = _summary(by_table, doc_stat)
        categories = _category_stats(by_table, doc_stat, sections)
        msg = _result_message(doc_stat, by_table, dry_run=dry_run)

        if not dry_run:
            _set_progress(inst, "audit", "正在写入合并记录…", 90)
            try:
                inst.db.audit("merge_import", "from=%s sections=%s docs=+%d/%d tasks=+%d" % (
                    name, ",".join(sections), doc_stat["added"], doc_stat["conflict"],
                    (by_table.get("task_run") or _empty_table_stat())["added"]))
                inst.db.event("info", msg)
            except Exception:
                pass

        _set_progress(inst, "done", "合并完成" if not dry_run else "预览完成", 100,
                      running=False, finished_at=now_bj().isoformat())
        result = {
            "ok": True,
            "preview": bool(dry_run),
            "message": msg,
            "backup": name,
            "source": meta,
            "sections": sections,
            "pre_merge_backup": (pre or {}).get("path") or "" if not dry_run else "",
            "documents": doc_stat,
            "by_table": by_table,
            "categories": categories,
            "summary": summary,
            "has_changes": bool(summary["news"] or summary["articles"] or summary["prices"] or
                                summary["tasks"] or summary["signals"] or summary["reports"] or
                                summary["deliveries"] or summary["history"] or summary["logs"] or
                                doc_stat["added"] or doc_stat["conflict"]),
        }
        return result
    except Exception as e:
        for p in reversed(added_docs):
            try:
                os.remove(p)
            except OSError:
                pass
        _set_progress(inst, "error", "合并失败：%s" % str(e), 0, running=False,
                      finished_at=now_bj().isoformat(), error=str(e))
        return {"ok": False, "message": "合并失败：%s: %s" % (type(e).__name__, e)}
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        if pipe_locked and pipe_lock is not None:
            try:
                pipe_lock.release()
            except Exception:
                pass
        _resume_collection(inst, paused)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        if dry_run:
            st = _merge_state(inst)
            st["running"] = False


def preview_backup(inst, name: str, sections=None) -> dict:
    """只读预演：不写数据库、不复制文档、不生成合并前备份。"""
    lock = _merge_lock(inst)
    if not lock.acquire(False):
        return {"ok": False, "message": "已有合并任务正在执行，请稍候"}
    try:
        sections = _normalize_sections(sections)
        return _run_merge(inst, name, sections, dry_run=True)
    except Exception as e:
        return {"ok": False, "message": "预览失败：%s: %s" % (type(e).__name__, e)}
    finally:
        lock.release()


def merge_backup(inst, name: str, sections=None) -> dict:
    """合并另一台电脑的备份；默认合并全部类别。"""
    lock = _merge_lock(inst)
    if not lock.acquire(False):
        return {"ok": False, "message": "已有合并任务正在执行，请稍候"}
    if not name:
        lock.release()
        return {"ok": False, "message": "请先选择一个备份"}
    try:
        sections = _normalize_sections(sections)
        _set_progress(inst, "start", "开始合并…", 2, running=True,
                      started_at=now_bj().isoformat(), finished_at="", error="", detail="")
        return _run_merge(inst, name, sections, dry_run=False)
    except Exception as e:
        _set_progress(inst, "error", "合并失败：%s" % str(e), 0, running=False,
                      finished_at=now_bj().isoformat(), error=str(e))
        return {"ok": False, "message": "合并失败：%s: %s" % (type(e).__name__, e)}
    finally:
        _merge_state(inst)["running"] = False
        lock.release()
