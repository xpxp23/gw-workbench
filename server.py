#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与材料智能工作台 (GW-Workbench v2.0.0) 核心服务端
专为银河麒麟 Kylin V10 (海光 3350) 与局域网跨平台协同设计
v2.0.0 终极完善特性：
1. 真正启用 SQLite FTS5 全文倒排索引查询（内置中文字符 Bigram 二元分词索引 + MATCH 高速初筛）
2. 实现 `00_事项要求与进展.md` 的双向对等同步（在本地用编辑器直接改勾选或加进展，自动反向同步回数据库）
3. 文件级 (rel_path, mtime, size) 极速快照检测，彻底消除 Linux Ext4/XFS 原地改稿漏检盲区
4. 修复在【公文与材料库】拖拽或移动“事项内文件”导致分类被回滚的逻辑矛盾（自动同步迁移所属事项物理文件夹）
5. 支持卡片待办细项（Checklist）行内直接改字与删除、支持周期性规则全字段编辑回填
"""

import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from parser import extract_document_text
from ai_engine import (
    parse_incoming_content,
    extract_search_keywords,
    synthesize_ai_search_answer,
    call_openai_compatible_api,
    infer_doc_role,
    match_best_category,
)
from gw_formatter import build_gb9704_docx

VERSION = "2.0.2"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "workbench.db"
CONFIG_PATH = DATA_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"

DEFAULT_CATEGORIES = [
    "01_防灾减灾",
    "02_隐患排查与安全生产",
    "03_综合汇报与总结",
    "04_上级日常通知",
    "05_历史公文归档",
]

DEFAULT_CONFIG = {
    "port": 8090,
    "view_password": "123456",
    "edit_password": "8888",
    "agent_token": "gw-local-agent-token",
    "workspace_root": "workspace_files",
    "prefix_archived_folder": False,
    "ai_api_base": "",
    "ai_api_key": "",
    "ai_model": "deepseek-chat",
    "xkl_gw_cli": "",
    "fts_version": 2,
    "lan_notice": "📌 欢迎访问科室公文与材料工作台！支持按【标题】或【标题+正文】秒搜公文，点开任意文件可直接预览复制全文或打包下载。",
}

DB_LOCK = threading.RLock()
_LAST_SYNC_TIME: float = 0.0
_FILE_STAT_SNAPSHOT: Dict[str, Tuple[float, int]] = {}

DOC_LIGHT_COLS = """
    id, task_id, filename, rel_path, category, doc_role, file_type,
    file_size, mtime, substr(extracted_text, 1, 240) AS content_preview,
    tags_json, is_starred, is_pinned, created_at, updated_at
"""


def load_config() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            merged = {**DEFAULT_CONFIG, **cfg}
            return merged
        except Exception:
            pass
    save_config(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ws_raw = str(cfg.get("workspace_root") or "workspace_files").strip()
    try:
        ws_abs = Path(ws_raw).expanduser().resolve()
        if ws_abs == (BASE_DIR / "workspace_files").resolve():
            cfg["workspace_root"] = "workspace_files"
    except Exception:
        pass
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get_workspace_root(cfg: Optional[Dict[str, Any]] = None) -> Path:
    c = cfg or load_config()
    raw = c.get("workspace_root")
    if not raw or "/working_dir/" in str(raw):
        p = (BASE_DIR / "workspace_files").resolve()
    else:
        p = Path(str(raw)).expanduser()
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        elif not p.exists() and (BASE_DIR / "workspace_files").exists():
            p = (BASE_DIR / "workspace_files").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def ensure_category_dir(ws_root: Path, cat_name: str) -> Path:
    cat_dir = ws_root / cat_name
    cat_dir.mkdir(parents=True, exist_ok=True)
    gitkeep = cat_dir / ".gitkeep"
    if not gitkeep.exists():
        try:
            gitkeep.write_text("", encoding="utf-8")
        except Exception:
            pass
    return cat_dir


def get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (name or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(". ")
    return cleaned[:80] or "未命名"


def resolve_unique_dest_path(dest_dir: Path, filename: str, incoming_bytes: Optional[bytes] = None) -> Path:
    """防止同名文件静默覆盖"""
    safe_name = sanitize_filename(filename)
    candidate = dest_dir / safe_name
    if not candidate.exists():
        return candidate

    if incoming_bytes is not None and candidate.is_file():
        try:
            if candidate.stat().st_size == len(incoming_bytes):
                if hashlib.md5(candidate.read_bytes()).hexdigest() == hashlib.md5(incoming_bytes).hexdigest():
                    return candidate
        except Exception:
            pass

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 2
    while True:
        alt = dest_dir / f"{stem}_{counter}{suffix}"
        if not alt.exists():
            return alt
        if incoming_bytes is not None and alt.is_file():
            try:
                if alt.stat().st_size == len(incoming_bytes) and hashlib.md5(alt.read_bytes()).hexdigest() == hashlib.md5(incoming_bytes).hexdigest():
                    return alt
            except Exception:
                pass
        counter += 1


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _cjk_segment_for_fts(text: str) -> str:
    """
    为 SQLite FTS5 (unicode61) 提供零依赖中文字符二元切分 (Bigram) + 单字/原词混合索引流
    使得任意中文双字词、多字词短语都能通过 FTS5 MATCH 倒排索引在 < 1ms 内精准召回！
    """
    if not text:
        return ""
    tokens: List[str] = [text]
    for cjk_run in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        n = len(cjk_run)
        for i in range(n - 1):
            tokens.append(cjk_run[i : i + 2])
    return " ".join(tokens)


def _build_fts_match_expr(keywords: List[str]) -> str:
    """将用户输入的多关键词转化为 FTS5 MATCH 查询表达式"""
    clauses = []
    for kw in keywords:
        clean_kw = re.sub(r'["\'()*+:\-\^?\\]+', " ", kw).strip()
        if not clean_kw:
            continue
        cjk_runs = re.findall(r"[\u4e00-\u9fa5]{2,}", clean_kw)
        if cjk_runs:
            for run in cjk_runs:
                for i in range(len(run) - 1):
                    bg = run[i : i + 2]
                    clauses.append(f'"{bg}"')
        else:
            for part in clean_kw.split():
                clauses.append(f'"{part}"*')
    # 去重保序
    seen = set()
    uniq = []
    for c in clauses:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return " AND ".join(uniq)


def _sync_fts_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    """同步单条文档记录到 SQLite FTS5 全文检索虚拟表（含 CJK 二元分词增强）"""
    try:
        conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
        row = conn.execute(
            "SELECT id, filename, category, tags_json, extracted_text FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT INTO documents_fts (doc_id, filename, category, tags, extracted_text) VALUES (?, ?, ?, ?, ?)",
                (
                    row["id"],
                    _cjk_segment_for_fts(row["filename"] or ""),
                    _cjk_segment_for_fts(row["category"] or ""),
                    _cjk_segment_for_fts(row["tags_json"] or ""),
                    _cjk_segment_for_fts(row["extracted_text"] or ""),
                ),
            )
    except Exception:
        pass


def init_db() -> None:
    with DB_LOCK:
        conn = get_db()
        cur = conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                source_channel TEXT DEFAULT '上级来文',
                doc_number TEXT DEFAULT '',
                issuing_org TEXT DEFAULT '',
                priority TEXT DEFAULT '普通',
                deadline TEXT DEFAULT '',
                category TEXT DEFAULT '01_防灾减灾',
                summary TEXT DEFAULT '',
                raw_notice_text TEXT DEFAULT '',
                checklist_json TEXT DEFAULT '[]',
                progress_json TEXT DEFAULT '[]',
                status TEXT DEFAULT 'in_progress',
                is_pinned INTEGER DEFAULT 0,
                is_collapsed INTEGER DEFAULT 0,
                completed_at TEXT DEFAULT '',
                recurring_id INTEGER DEFAULT NULL,
                folder_rel_path TEXT DEFAULT '',
                readme_mtime REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                filename TEXT NOT NULL,
                rel_path TEXT NOT NULL UNIQUE,
                category TEXT DEFAULT '01_防灾减灾',
                doc_role TEXT DEFAULT 'incoming',
                file_type TEXT DEFAULT '',
                file_size INTEGER DEFAULT 0,
                mtime REAL DEFAULT 0,
                extracted_text TEXT DEFAULT '',
                tags_json TEXT DEFAULT '[]',
                is_starred INTEGER DEFAULT 0,
                is_pinned INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS recurring_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title_template TEXT NOT NULL,
                category TEXT DEFAULT '01_防灾减灾',
                source_channel TEXT DEFAULT '上级来文',
                issuing_org TEXT DEFAULT '',
                priority TEXT DEFAULT '重要',
                freq_type TEXT DEFAULT 'weekly',
                freq_value INTEGER DEFAULT 4,
                deadline_offset_days INTEGER DEFAULT 1,
                summary TEXT DEFAULT '',
                checklist_json TEXT DEFAULT '[]',
                reference_doc_id INTEGER DEFAULT NULL,
                enabled INTEGER DEFAULT 1,
                last_spawned_period TEXT DEFAULT '',
                last_spawned_at TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category);
            CREATE INDEX IF NOT EXISTS idx_docs_task_id ON documents(task_id);
            CREATE INDEX IF NOT EXISTS idx_docs_category ON documents(category);
            CREATE INDEX IF NOT EXISTS idx_docs_role ON documents(doc_role);
            """
        )

        task_cols = {r["name"] for r in cur.execute("PRAGMA table_info(tasks)").fetchall()}
        if "completed_at" not in task_cols:
            cur.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT DEFAULT ''")
        if "is_collapsed" not in task_cols:
            cur.execute("ALTER TABLE tasks ADD COLUMN is_collapsed INTEGER DEFAULT 0")
        if "recurring_id" not in task_cols:
            cur.execute("ALTER TABLE tasks ADD COLUMN recurring_id INTEGER DEFAULT NULL")
        if "readme_mtime" not in task_cols:
            cur.execute("ALTER TABLE tasks ADD COLUMN readme_mtime REAL DEFAULT 0")

        try:
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
                    doc_id UNINDEXED,
                    filename,
                    category,
                    tags,
                    extracted_text,
                    tokenize='unicode61'
                );
                """
            )
            # 检查是否已按 v2.0 CJK Bigram 重建 FTS5 索引
            fts_cnt = cur.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
            doc_rows = cur.execute("SELECT id FROM documents").fetchall()
            sample_fts = cur.execute("SELECT filename FROM documents_fts LIMIT 1").fetchone()
            needs_rebuild = (fts_cnt != len(doc_rows)) or (sample_fts and " " not in (sample_fts["filename"] or "") and len(sample_fts["filename"] or "") >= 4)
            if needs_rebuild:
                cur.execute("DELETE FROM documents_fts")
                for dr in doc_rows:
                    _sync_fts_doc(conn, dr["id"])
        except Exception:
            pass

        conn.commit()
        conn.close()

    ws_root = get_workspace_root()
    for cat in DEFAULT_CATEGORIES:
        ensure_category_dir(ws_root, cat)


def list_physical_categories() -> List[str]:
    ws_root = get_workspace_root()
    for cat in DEFAULT_CATEGORIES:
        if not (ws_root / cat).exists():
            ensure_category_dir(ws_root, cat)
    dirs = [
        d.name for d in ws_root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]
    dirs.sort()
    return dirs if dirs else list(DEFAULT_CATEGORIES)


def write_task_readme_file(conn: sqlite3.Connection, task_id: int) -> None:
    """
    在工作事项对应的本地物理文件夹中生成/更新 `00_事项要求与进展.md`，并记录 `readme_mtime`
    以便区分“系统写入”与“用户在本地直接用编辑器修改了 00_事项要求与进展.md”
    """
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return
    ws_root = get_workspace_root()
    folder_rel = row["folder_rel_path"]
    if not folder_rel:
        return
    folder_path = ws_root / folder_rel
    folder_path.mkdir(parents=True, exist_ok=True)

    docs = conn.execute(
        "SELECT filename, doc_role, updated_at FROM documents WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()

    status_map = {"pending": "⏳ 待办", "in_progress": "🔄 进行中", "archived": "✅ 已完成（归档）"}
    checklist = json.loads(row["checklist_json"] or "[]")
    progress_list = json.loads(row["progress_json"] or "[]")

    md_lines = [
        f"<!-- gw_task_id: {row['id']} -->",
        f"# {row['title']}",
        "",
        f"- **事项编号**：`#{row['id']}`",
        f"- **当前状态**：{status_map.get(row['status'], row['status'])}",
        f"- **来源渠道**：{row['source_channel'] or '无'}",
        f"- **发文字号**：{row['doc_number'] or '无'}",
        f"- **来文单位**：{row['issuing_org'] or '无'}",
        f"- **紧急程度**：{row['priority'] or '普通'}",
        f"- **截止日期**：{row['deadline'] or '常态化/无明确截止期'}",
        f"- **所属分类**：{row['category'] or ''}",
        f"- **更新时间**：{row['updated_at']}",
        "",
        "## 一、核心工作要求摘要",
        row["summary"] or "（暂无摘要）",
        "",
        "## 二、待办细项清单 (Checklist)",
    ]
    if checklist:
        for item in checklist:
            mark = "x" if item.get("done") else " "
            md_lines.append(f"- [{mark}] {item.get('text', '')}")
    else:
        md_lines.append("- [ ] 暂无细项")

    md_lines.extend(["", "## 三、实时进展流水"])
    if progress_list:
        for p in progress_list:
            md_lines.append(f"- **`{p.get('time', '')}`**：{p.get('text', '')}")
    else:
        md_lines.append("- （暂无进展记录）")

    md_lines.extend(["", "## 四、本文件夹内关联公文与材料清单"])
    incoming_files = [d for d in docs if d["doc_role"] == "incoming"]
    draft_files = [d for d in docs if d["doc_role"] != "incoming"]
    md_lines.append("### 1. 来文/通知原件")
    if incoming_files:
        for d in incoming_files:
            md_lines.append(f"- 📄 `{d['filename']}`")
    else:
        md_lines.append("- （无独立原件，详见下方原始通知文本）")

    md_lines.append("### 2. 我撰写的材料")
    if draft_files:
        for d in draft_files:
            md_lines.append(f"- ✍️ `{d['filename']}` （更新于 {d['updated_at']}）")
    else:
        md_lines.append("- （暂未放入产出材料，可直接将写好的 WPS/Word 文档拖入本文件夹）")

    if (row["raw_notice_text"] or "").strip():
        md_lines.extend([
            "",
            "## 五、原始来文 / 微信电话通知内容留存",
            "```text",
            row["raw_notice_text"].strip(),
            "```",
        ])

    readme_path = folder_path / "00_事项要求与进展.md"
    readme_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    new_mtime = round(readme_path.stat().st_mtime, 2)
    conn.execute("UPDATE tasks SET readme_mtime = ? WHERE id = ?", (new_mtime, task_id))


def _extract_task_id_from_readme(readme_path: Path) -> Optional[int]:
    if not readme_path.is_file():
        return None
    try:
        head = readme_path.read_text(encoding="utf-8", errors="ignore")[:300]
        m = re.search(r"<!--\s*gw_task_id:\s*(\d+)\s*-->", head)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _parse_and_reverse_sync_readme(conn: sqlite3.Connection, task_id: int, readme_path: Path) -> bool:
    """
    当检测到用户在本地直接用编辑器（如 Quilla / Typora / WPS / 记事本）修改了 `00_事项要求与进展.md` 时，
    自动解析其中的状态、截止期、核心要求摘要、待办清单勾选状态（- [x] / - [ ]）及新增进展流水，反向写回 SQLite 数据库！
    """
    try:
        raw = readme_path.read_text(encoding="utf-8", errors="ignore")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False

        new_status = row["status"]
        m_st = re.search(r"\*\*当前状态\*\*[：:]\s*([^\n]+)", raw)
        if m_st:
            st_txt = m_st.group(1)
            if "已完成" in st_txt or "归档" in st_txt:
                new_status = "archived"
            elif "进行中" in st_txt:
                new_status = "in_progress"
            elif "待办" in st_txt:
                new_status = "pending"

        new_summary = row["summary"]
        m_sum = re.search(r"## 一、核心工作要求摘要\s*\n+([\s\S]*?)(?=\n+## 二、|$)", raw)
        if m_sum:
            s_val = m_sum.group(1).strip()
            if s_val and s_val != "（暂无摘要）":
                new_summary = s_val

        # 解析 ## 二、待办细项清单 (Checklist)
        new_checklist = json.loads(row["checklist_json"] or "[]")
        m_chk = re.search(r"## 二、待办细项清单[^\n]*\n+([\s\S]*?)(?=\n+## 三、|$)", raw)
        if m_chk:
            parsed_chk = []
            for idx, ln in enumerate(m_chk.group(1).splitlines()):
                cm = re.match(r"^\s*-\s*\[([xX√\s])\]\s*(.+)$", ln.strip())
                if cm:
                    done_flag = cm.group(1).strip().lower() in ("x", "√")
                    txt_val = cm.group(2).strip()
                    if txt_val and txt_val != "暂无细项":
                        parsed_chk.append({
                            "id": f"chk_{idx+1}",
                            "text": txt_val,
                            "done": done_flag,
                        })
            if parsed_chk:
                new_checklist = parsed_chk

        # 解析 ## 三、实时进展流水
        new_progress = json.loads(row["progress_json"] or "[]")
        m_prog = re.search(r"## 三、实时进展流水[^\n]*\n+([\s\S]*?)(?=\n+## 四、|$)", raw)
        if m_prog:
            parsed_prog = []
            for ln in m_prog.group(1).splitlines():
                ln_s = ln.strip()
                if not ln_s.startswith("-") or "暂无进展记录" in ln_s:
                    continue
                pm = re.match(r"^-\s*\*\*`?([^`*]+)`?\*\*[：:]\s*(.+)$", ln_s)
                if pm:
                    parsed_prog.append({"time": pm.group(1).strip(), "text": pm.group(2).strip()})
                else:
                    plain_p = re.sub(r"^-\s*", "", ln_s).strip()
                    if plain_p:
                        parsed_prog.append({"time": datetime.now().strftime("%m-%d %H:%M"), "text": plain_p})
            if parsed_prog:
                new_progress = parsed_prog

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_mtime = round(readme_path.stat().st_mtime, 2)
        comp_at = row["completed_at"] or ""
        if new_status == "archived" and row["status"] != "archived":
            comp_at = now_str

        conn.execute(
            """
            UPDATE tasks SET
                status = ?,
                summary = ?,
                checklist_json = ?,
                progress_json = ?,
                completed_at = ?,
                readme_mtime = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_status,
                new_summary,
                json.dumps(new_checklist, ensure_ascii=False),
                json.dumps(new_progress, ensure_ascii=False),
                comp_at,
                new_mtime,
                now_str,
                task_id,
            ),
        )
        return True
    except Exception:
        return False


def _collect_fast_file_snapshot(ws_root: Path) -> Dict[str, Tuple[float, int]]:
    """快速采集工作区所有文件与目录的 (mtime, size) 元组，耗时 < 2ms，不漏检任何原地修改"""
    snap: Dict[str, Tuple[float, int]] = {}
    try:
        for cat_dir in ws_root.iterdir():
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            st_c = cat_dir.stat()
            snap[str(cat_dir)] = (round(st_c.st_mtime, 2), 0)
            for f in cat_dir.rglob("*"):
                if f.name.startswith(".") or f.name.startswith("~$") or f.name == ".gitkeep":
                    continue
                st_f = f.stat()
                snap[str(f)] = (round(st_f.st_mtime, 2), st_f.st_size)
    except Exception:
        pass
    return snap


def sync_filesystem(force: bool = False) -> Dict[str, int]:
    """
    真·双向对等物理文件夹同步引擎（v2.0.0）：
    1. 文件级 `(mtime, size)` 快照比对，修复 Linux Ext4/XFS 原地修改文件不更新父目录 mtime 的漏检盲区
    2. 支持 `00_事项要求与进展.md` 本地修改反向同步回 SQLite 数据库
    3. 智能追踪本地被改名或跨分类移动的事项文件夹，零幽灵目录复活
    """
    global _LAST_SYNC_TIME, _FILE_STAT_SNAPSHOT
    stats = {"added": 0, "updated": 0, "removed": 0, "moved_tasks": 0, "reverse_synced_tasks": 0, "skipped": 0}
    ws_root = get_workspace_root()
    now_ts = time.time()

    current_snap = _collect_file_snapshot_if_needed = _collect_fast_file_snapshot(ws_root)
    if not force and (now_ts - _LAST_SYNC_TIME) < 6.0 and _FILE_STAT_SNAPSHOT and current_snap == _FILE_STAT_SNAPSHOT:
        stats["skipped"] = 1
        return stats

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with DB_LOCK:
        conn = get_db()
        existing_docs = {
            r["rel_path"]: dict(r)
            for r in conn.execute(
                "SELECT id, task_id, filename, rel_path, category, doc_role, file_size, mtime FROM documents"
            ).fetchall()
        }
        seen_rel_paths = set()

        tasks = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()}
        task_folder_map: Dict[str, Dict[str, Any]] = {}

        # 第一遍：扫描磁盘上的所有事项子文件夹，识别重命名/移动 & 检查 00_事项要求与进展.md 是否在本地被编辑
        for cat_dir in sorted(ws_root.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            cat_name = cat_dir.name
            for entry in sorted(cat_dir.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    rel_dir = str(entry.relative_to(ws_root)).replace("\\", "/")
                    readme_f = entry / "00_事项要求与进展.md"
                    anchored_tid = _extract_task_id_from_readme(readme_f)
                    if anchored_tid and anchored_tid in tasks:
                        t_obj = tasks[anchored_tid]
                        if t_obj["folder_rel_path"] != rel_dir or t_obj["category"] != cat_name:
                            conn.execute(
                                "UPDATE tasks SET folder_rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                                (rel_dir, cat_name, now_str, anchored_tid),
                            )
                            t_obj["folder_rel_path"] = rel_dir
                            t_obj["category"] = cat_name
                            stats["moved_tasks"] += 1

                        # 检查 00_事项要求与进展.md 是否在本地被用户直接编辑过
                        if readme_f.is_file():
                            disk_rm_mtime = round(readme_f.stat().st_mtime, 2)
                            db_rm_mtime = float(t_obj.get("readme_mtime") or 0.0)
                            if db_rm_mtime > 0 and abs(disk_rm_mtime - db_rm_mtime) > 0.5:
                                if _parse_and_reverse_sync_readme(conn, anchored_tid, readme_f):
                                    stats["reverse_synced_tasks"] += 1
                            elif db_rm_mtime == 0:
                                conn.execute("UPDATE tasks SET readme_mtime = ? WHERE id = ?", (disk_rm_mtime, anchored_tid))

                        task_folder_map[rel_dir] = t_obj

        for tid, t_obj in tasks.items():
            f_rel = t_obj.get("folder_rel_path") or ""
            if f_rel and f_rel not in task_folder_map:
                f_abs = ws_root / f_rel
                if f_abs.exists() and f_abs.is_dir():
                    task_folder_map[f_rel] = t_obj
                    readme_f = f_abs / "00_事项要求与进展.md"
                    if not readme_f.exists() or _extract_task_id_from_readme(readme_f) is None:
                        write_task_readme_file(conn, tid)

        # 第二遍：遍历分类目录与事项子目录，增量同步所有文档与材料
        for cat_dir in sorted(ws_root.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            cat_name = cat_dir.name

            for entry in sorted(cat_dir.iterdir()):
                if entry.name.startswith(".") or entry.name.startswith("~$") or entry.name == ".gitkeep":
                    continue

                if entry.is_dir():
                    rel_dir = str(entry.relative_to(ws_root)).replace("\\", "/")
                    task_obj = task_folder_map.get(rel_dir)

                    for sub_file in sorted(entry.rglob("*")):
                        if not sub_file.is_file():
                            continue
                        if sub_file.name.startswith(".") or sub_file.name.startswith("~$") or sub_file.name == ".gitkeep":
                            continue
                        if sub_file.name == "00_事项要求与进展.md":
                            continue

                        rel_file = str(sub_file.relative_to(ws_root)).replace("\\", "/")
                        seen_rel_paths.add(rel_file)
                        st = sub_file.stat()
                        mtime = round(st.st_mtime, 2)
                        fsize = st.st_size

                        if rel_file in existing_docs:
                            doc_rec = existing_docs[rel_file]
                            if abs(doc_rec["mtime"] - mtime) > 0.5 or doc_rec["file_size"] != fsize or doc_rec["category"] != cat_name:
                                extracted, ftype = extract_document_text(sub_file)
                                conn.execute(
                                    """
                                    UPDATE documents
                                    SET category = ?, task_id = ?, file_size = ?, mtime = ?, extracted_text = ?, file_type = ?, updated_at = ?
                                    WHERE id = ?
                                    """,
                                    (cat_name, task_obj["id"] if task_obj else doc_rec["task_id"], fsize, mtime, extracted, ftype, now_str, doc_rec["id"]),
                                )
                                _sync_fts_doc(conn, doc_rec["id"])
                                stats["updated"] += 1
                        else:
                            fname = sub_file.name
                            extracted, ftype = extract_document_text(sub_file)
                            role = infer_doc_role(fname, extracted[:300], in_task_folder=bool(task_obj), cat_name=cat_name)
                            auto_tags = []
                            if not task_obj and entry.name != cat_name:
                                auto_tags.append(entry.name)

                            cur = conn.execute(
                                """
                                INSERT INTO documents (
                                    task_id, filename, rel_path, category, doc_role,
                                    file_type, file_size, mtime, extracted_text,
                                    tags_json, is_starred, is_pinned, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                                """,
                                (
                                    task_obj["id"] if task_obj else None,
                                    fname,
                                    rel_file,
                                    cat_name,
                                    role,
                                    ftype,
                                    fsize,
                                    mtime,
                                    extracted,
                                    json.dumps(auto_tags, ensure_ascii=False),
                                    1 if role == "template" else 0,
                                    now_str,
                                    now_str,
                                ),
                            )
                            _sync_fts_doc(conn, cur.lastrowid)
                            stats["added"] += 1

                elif entry.is_file():
                    if entry.name == "00_事项要求与进展.md":
                        continue
                    rel_file = str(entry.relative_to(ws_root)).replace("\\", "/")
                    seen_rel_paths.add(rel_file)
                    st = entry.stat()
                    mtime = round(st.st_mtime, 2)
                    fsize = st.st_size

                    if rel_file in existing_docs:
                        doc_rec = existing_docs[rel_file]
                        if abs(doc_rec["mtime"] - mtime) > 0.5 or doc_rec["file_size"] != fsize:
                            extracted, ftype = extract_document_text(entry)
                            conn.execute(
                                """
                                UPDATE documents
                                SET file_size = ?, mtime = ?, extracted_text = ?, file_type = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (fsize, mtime, extracted, ftype, now_str, doc_rec["id"]),
                            )
                            _sync_fts_doc(conn, doc_rec["id"])
                            stats["updated"] += 1
                    else:
                        fname = entry.name
                        extracted, ftype = extract_document_text(entry)
                        role = infer_doc_role(fname, extracted[:300], in_task_folder=False, cat_name=cat_name)
                        cur = conn.execute(
                            """
                            INSERT INTO documents (
                                task_id, filename, rel_path, category, doc_role,
                                file_type, file_size, mtime, extracted_text,
                                tags_json, is_starred, is_pinned, created_at, updated_at
                            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, 0, ?, ?)
                            """,
                            (
                                fname,
                                rel_file,
                                cat_name,
                                role,
                                ftype,
                                fsize,
                                mtime,
                                extracted,
                                1 if role == "template" else 0,
                                now_str,
                                now_str,
                            ),
                        )
                        _sync_fts_doc(conn, cur.lastrowid)
                        stats["added"] += 1

        for rel_p, doc_rec in existing_docs.items():
            if rel_p not in seen_rel_paths:
                abs_p = ws_root / rel_p
                if not abs_p.exists():
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_rec["id"],))
                    conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_rec["id"],))
                    stats["removed"] += 1

        conn.commit()
        conn.close()

    _LAST_SYNC_TIME = time.time()
    _FILE_STAT_SNAPSHOT = _collect_fast_file_snapshot(ws_root)
    return stats


def _move_task_folder_to_category(
    conn: sqlite3.Connection,
    ws_root: Path,
    task_id: int,
    new_cat: str,
    new_title: Optional[str] = None,
    new_status: Optional[str] = None,
    prefix_archived: bool = False,
) -> Tuple[str, str]:
    """
    原子化移动或重命名事项的物理文件夹，并同步更新该事项下所有关联文档的 rel_path、category 与 FTS5 索引
    """
    t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not t_row:
        return "", ""
    t = dict(t_row)
    title_use = sanitize_filename(new_title if new_title is not None else t["title"])
    cat_use = sanitize_filename(new_cat or t["category"])
    status_use = new_status if new_status is not None else t["status"]
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    old_rel_folder = t.get("folder_rel_path") or ""
    new_rel_folder = old_rel_folder
    if old_rel_folder:
        old_abs = ws_root / old_rel_folder
        old_leaf = Path(old_rel_folder).name
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", old_leaf)
        date_prefix = date_match.group(1) if date_match else (t["created_at"][:10] if t.get("created_at") else datetime.now().strftime("%Y-%m-%d"))

        if title_use != t["title"]:
            folder_leaf = sanitize_filename(f"{date_prefix}_{title_use}")
        else:
            folder_leaf = re.sub(r"^\[已办\]_", "", old_leaf)

        if prefix_archived and status_use == "archived":
            if not folder_leaf.startswith("[已办]_"):
                folder_leaf = "[已办]_" + folder_leaf

        target_cat_dir = ensure_category_dir(ws_root, cat_use)
        new_abs = target_cat_dir / folder_leaf
        if old_abs.exists() and old_abs.resolve() != new_abs.resolve():
            counter = 2
            while new_abs.exists() and new_abs.resolve() != old_abs.resolve():
                new_abs = target_cat_dir / f"{folder_leaf}_{counter}"
                counter += 1
            shutil.move(str(old_abs), str(new_abs))
            new_rel_folder = str(new_abs.relative_to(ws_root)).replace("\\", "/")
            docs_in_task = conn.execute("SELECT id, rel_path FROM documents WHERE task_id = ?", (task_id,)).fetchall()
            for dr in docs_in_task:
                old_doc_rel = dr["rel_path"]
                if old_doc_rel.startswith(old_rel_folder + "/"):
                    updated_doc_rel = new_rel_folder + old_doc_rel[len(old_rel_folder):]
                    conn.execute(
                        "UPDATE documents SET rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                        (updated_doc_rel, cat_use, now_str, dr["id"]),
                    )
                    _sync_fts_doc(conn, dr["id"])
    return cat_use, new_rel_folder


def compute_deadline_badge(deadline_str: str, status: str) -> Dict[str, Any]:
    if status == "archived":
        return {"level": "archived", "label": "已办结归档", "days_left": 9999}
    if not deadline_str:
        return {"level": "routine", "label": "常态化/持续", "days_left": 999}
    try:
        dl = datetime.strptime(deadline_str[:10], "%Y-%m-%d").date()
        today = date.today()
        diff = (dl - today).days
        if diff < 0:
            return {"level": "danger", "label": f"已超期 {abs(diff)} 天 ({deadline_str})", "days_left": diff}
        elif diff == 0:
            return {"level": "danger", "label": f"今日到期 ({deadline_str})", "days_left": 0}
        elif diff <= 3:
            return {"level": "warning", "label": f"剩 {diff} 天 ({deadline_str})", "days_left": diff}
        else:
            return {"level": "normal", "label": f"剩 {diff} 天 ({deadline_str})", "days_left": diff}
    except Exception:
        return {"level": "routine", "label": deadline_str, "days_left": 999}


def find_similar_library_docs_fast(
    light_docs: List[Dict[str, Any]],
    title: str,
    summary: str,
    exclude_task_id: Optional[int] = None,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    kws = extract_search_keywords(f"{title} {summary}")
    if not kws:
        return []
    scored = []
    for d in light_docs:
        if exclude_task_id is not None and d.get("task_id") == exclude_task_id:
            continue
        score = 0
        hay_title = (d.get("filename") or "").lower()
        hay_body = (d.get("content_preview") or "").lower()
        for kw in kws:
            kl = kw.lower()
            if kl in hay_title:
                score += 6
            if kl in hay_body:
                score += 2
        if d.get("doc_role") in ("draft", "template"):
            score += 2 if score > 0 else 0
        if d.get("is_starred"):
            score += 3 if score > 0 else 1
        if score > 0:
            scored.append((score, {
                "id": d["id"],
                "task_id": d.get("task_id"),
                "filename": d["filename"],
                "rel_path": d["rel_path"],
                "category": d["category"],
                "doc_role": d["doc_role"],
                "is_starred": d["is_starred"],
            }))

    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [item[1] for item in scored[:limit]]


def serialize_doc_light(row: Any, snippet: str = "", ws_root: Optional[Path] = None) -> Dict[str, Any]:
    d = dict(row)
    preview = d.get("content_preview")
    if preview is None:
        full_text = d.pop("extracted_text", "") or ""
        preview = full_text[:240]
    else:
        d.pop("extracted_text", None)

    d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
    d["is_starred"] = bool(d.get("is_starred", 0))
    d["is_pinned"] = bool(d.get("is_pinned", 0))
    d["content_preview"] = preview
    d["snippet"] = snippet or (preview[:180].replace("\n", " ") if preview else "")
    root = ws_root or get_workspace_root()
    d["abs_path"] = str((root / d["rel_path"]).resolve())
    return d


def serialize_task(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    include_refs: bool = True,
    preloaded_docs: Optional[List[Dict[str, Any]]] = None,
    ws_root: Optional[Path] = None,
) -> Dict[str, Any]:
    t = dict(row)
    t.pop("readme_mtime", None)
    t["checklist"] = json.loads(t.pop("checklist_json", "[]") or "[]")
    t["progress_logs"] = json.loads(t.pop("progress_json", "[]") or "[]")
    t["is_pinned"] = bool(t.get("is_pinned", 0))
    t["is_collapsed"] = bool(t.get("is_collapsed", 0))
    t["deadline_info"] = compute_deadline_badge(t.get("deadline", ""), t.get("status", "in_progress"))
    root = ws_root or get_workspace_root()

    if preloaded_docs is not None:
        task_docs = [d for d in preloaded_docs if d.get("task_id") == t["id"]]
        all_light_docs = preloaded_docs
    else:
        doc_rows = conn.execute(
            f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE task_id = ? ORDER BY id ASC",
            (t["id"],),
        ).fetchall()
        task_docs = [serialize_doc_light(r, ws_root=root) for r in doc_rows]
        if include_refs:
            all_rows = conn.execute(
                f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC LIMIT 300"
            ).fetchall()
            all_light_docs = [serialize_doc_light(r, ws_root=root) for r in all_rows]
        else:
            all_light_docs = []

    t["incoming_files"] = [d for d in task_docs if d["doc_role"] == "incoming"]
    t["drafted_files"] = [d for d in task_docs if d["doc_role"] != "incoming"]
    t["folder_abs_path"] = str((root / t["folder_rel_path"]).resolve()) if t.get("folder_rel_path") else ""

    if include_refs:
        t["reference_docs"] = find_similar_library_docs_fast(
            all_light_docs, t["title"], t.get("summary", ""), exclude_task_id=t["id"], limit=4
        )
    else:
        t["reference_docs"] = []
    return t


def build_highlight_snippet(text: str, keywords: List[str], max_len: int = 160) -> str:
    if not text:
        return ""
    clean = re.sub(r"\s+", " ", text).strip()
    first_pos = -1
    for kw in keywords:
        pos = clean.lower().find(kw.lower())
        if pos != -1 and (first_pos == -1 or pos < first_pos):
            first_pos = pos
    if first_pos == -1:
        return clean[:max_len]
    start = max(0, first_pos - 35)
    end = min(len(clean), start + max_len)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


def execute_search(
    query: str,
    scope: str = "full",
    category: str = "",
    doc_role: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    真正启用 SQLite FTS5 全文倒排索引表 (`documents_fts` MATCH) 进行高速初筛 + 精准子串验证！
    """
    q_clean = (query or "").strip()
    keywords = [k for k in re.split(r"\s+", q_clean) if k] if q_clean else []
    ws_root = get_workspace_root()

    with DB_LOCK:
        conn = get_db()
        all_doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
        preloaded_light_docs = [serialize_doc_light(r, ws_root=ws_root) for r in all_doc_rows]

        task_rows = conn.execute("SELECT * FROM tasks ORDER BY is_pinned DESC, id DESC").fetchall()
        matched_tasks = []
        for r in task_rows:
            if category and r["category"] != category:
                continue
            if not keywords:
                matched_tasks.append(serialize_task(conn, r, include_refs=False, preloaded_docs=preloaded_light_docs, ws_root=ws_root))
                continue

            if scope == "title":
                haystack = f"{r['title']} {r['doc_number']} {r['issuing_org']}"
            else:
                haystack = (
                    f"{r['title']} {r['doc_number']} {r['issuing_org']} "
                    f"{r['summary']} {r['raw_notice_text']} {r['checklist_json']} {r['progress_json']}"
                )
            hay_lower = haystack.lower()
            if all(kw.lower() in hay_lower for kw in keywords):
                t_obj = serialize_task(conn, r, include_refs=False, preloaded_docs=preloaded_light_docs, ws_root=ws_root)
                t_obj["match_snippet"] = build_highlight_snippet(haystack, keywords)
                matched_tasks.append(t_obj)

        matched_docs = []
        if not keywords:
            for d in preloaded_light_docs:
                if category and d["category"] != category:
                    continue
                if doc_role and d["doc_role"] != doc_role:
                    continue
                matched_docs.append(d)
        elif scope == "title":
            for d in preloaded_light_docs:
                if category and d["category"] != category:
                    continue
                if doc_role and d["doc_role"] != doc_role:
                    continue
                haystack = f"{d['filename']} {' '.join(d['tags'])} {d['category']}"
                if all(kw.lower() in haystack.lower() for kw in keywords):
                    d_copy = dict(d)
                    d_copy["snippet"] = build_highlight_snippet(d["content_preview"] or d["filename"], keywords)
                    matched_docs.append(d_copy)
        else:
            # 第一步：优先通过 SQLite FTS5 倒排虚拟表 (`documents_fts MATCH`) 极速召回候选 doc_id 集合
            fts_candidate_ids: Optional[List[int]] = None
            match_expr = _build_fts_match_expr(keywords)
            if match_expr:
                try:
                    fts_rows = conn.execute(
                        "SELECT doc_id FROM documents_fts WHERE documents_fts MATCH ?",
                        (match_expr,),
                    ).fetchall()
                    fts_candidate_ids = [int(r["doc_id"]) for r in fts_rows]
                except Exception:
                    fts_candidate_ids = None

            sql = "SELECT id, task_id, filename, rel_path, category, doc_role, file_type, file_size, mtime, extracted_text, tags_json, is_starred, is_pinned, created_at, updated_at FROM documents WHERE 1=1"
            params: List[Any] = []
            if fts_candidate_ids is not None:
                if len(fts_candidate_ids) == 0:
                    conn.close()
                    return matched_tasks, []
                placeholders = ",".join("?" for _ in fts_candidate_ids)
                sql += f" AND id IN ({placeholders})"
                params.extend(fts_candidate_ids)

            if category:
                sql += " AND category = ?"
                params.append(category)
            if doc_role:
                sql += " AND doc_role = ?"
                params.append(doc_role)
            for kw in keywords:
                sql += " AND (filename LIKE ? OR tags_json LIKE ? OR category LIKE ? OR extracted_text LIKE ?)"
                like_pat = f"%{kw}%"
                params.extend([like_pat, like_pat, like_pat, like_pat])
            sql += " ORDER BY is_starred DESC, id DESC"
            for r in conn.execute(sql, params).fetchall():
                snip = build_highlight_snippet(r["extracted_text"] or r["filename"], keywords)
                matched_docs.append(serialize_doc_light(r, snippet=snip, ws_root=ws_root))

        conn.close()
    return matched_tasks, matched_docs


def create_task_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    ws_root = get_workspace_root(cfg)
    categories = list_physical_categories()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_prefix = datetime.now().strftime("%Y-%m-%d")

    raw_text = (payload.get("raw_notice_text") or payload.get("text") or "").strip()
    uploaded_files: List[Dict[str, Any]] = payload.get("files") or []
    auto_ai = bool(payload.get("auto_ai", True))

    combined_text_parts = []
    if raw_text:
        combined_text_parts.append(raw_text)

    first_filename = ""
    extracted_file_blobs = []
    for fitem in uploaded_files:
        fname = sanitize_filename(fitem.get("filename") or "附件.txt")
        if not first_filename:
            first_filename = fname
        b64_data = fitem.get("content_base64") or ""
        raw_bytes = b""
        if b64_data:
            import base64
            if "," in b64_data and b64_data.startswith("data:"):
                b64_data = b64_data.split(",", 1)[1]
            raw_bytes = base64.b64decode(b64_data)
        elif fitem.get("local_source_path"):
            src_p = Path(fitem["local_source_path"]).expanduser()
            if src_p.exists() and src_p.is_file():
                raw_bytes = src_p.read_bytes()
                fname = sanitize_filename(src_p.name)

        role = fitem.get("doc_role") or "incoming"
        extracted_file_blobs.append((fname, raw_bytes, role))

    if extracted_file_blobs and auto_ai:
        import tempfile
        for fname, raw_bytes, role in extracted_file_blobs:
            if raw_bytes and role == "incoming":
                suffix = Path(fname).suffix
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(raw_bytes)
                    tmp_p = Path(tf.name)
                try:
                    ext_txt, _ = extract_document_text(tmp_p)
                    if ext_txt:
                        combined_text_parts.append(ext_txt)
                finally:
                    tmp_p.unlink(missing_ok=True)

    full_source_text = "\n\n".join(combined_text_parts).strip()

    ai_parsed = {}
    if auto_ai and (full_source_text or first_filename):
        ai_parsed = parse_incoming_content(
            full_source_text,
            filename=first_filename,
            source_hint=payload.get("source_channel") or "",
            categories=categories,
            config=cfg,
        )

    title = sanitize_filename(payload.get("title") or ai_parsed.get("title") or first_filename or "新建工作事项")
    source_channel = payload.get("source_channel") or ai_parsed.get("source_channel") or "上级来文"
    doc_number = payload.get("doc_number") if payload.get("doc_number") is not None else ai_parsed.get("doc_number", "")
    issuing_org = payload.get("issuing_org") if payload.get("issuing_org") is not None else ai_parsed.get("issuing_org", "")
    priority = payload.get("priority") or ai_parsed.get("priority") or "普通"
    deadline = payload.get("deadline") if payload.get("deadline") is not None else ai_parsed.get("deadline", "")
    category = payload.get("category") or ai_parsed.get("category") or categories[0]
    summary = payload.get("summary") or ai_parsed.get("summary") or full_source_text[:160]
    status = payload.get("status") or "in_progress"
    completed_at = now_str if status == "archived" else ""

    checklist = payload.get("checklist")
    if checklist is None:
        checklist = ai_parsed.get("checklist") or [
            {"id": "chk_1", "text": "核对报送要求：材料名称、份数、报送方式与截止时间", "done": False}
        ]
    elif isinstance(checklist, list):
        norm_chk = []
        for idx, item in enumerate(checklist):
            if isinstance(item, str):
                norm_chk.append({"id": f"chk_{idx+1}", "text": item, "done": False})
            elif isinstance(item, dict):
                norm_chk.append({
                    "id": item.get("id") or f"chk_{idx+1}",
                    "text": item.get("text") or "",
                    "done": bool(item.get("done", False)),
                })
        checklist = norm_chk

    progress_logs = payload.get("progress_logs") or []
    if payload.get("initial_progress"):
        progress_logs.insert(0, {
            "time": datetime.now().strftime("%m-%d %H:%M"),
            "text": str(payload["initial_progress"]).strip(),
        })

    cat_dir = ensure_category_dir(ws_root, category)
    folder_base_name = sanitize_filename(f"{date_prefix}_{title}")
    task_folder = cat_dir / folder_base_name
    counter = 2
    while task_folder.exists():
        task_folder = cat_dir / f"{folder_base_name}_{counter}"
        counter += 1
    task_folder.mkdir(parents=True, exist_ok=True)
    folder_rel_path = str(task_folder.relative_to(ws_root)).replace("\\", "/")

    with DB_LOCK:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks (
                title, source_channel, doc_number, issuing_org, priority,
                deadline, category, summary, raw_notice_text, checklist_json,
                progress_json, status, is_pinned, is_collapsed, completed_at,
                recurring_id, folder_rel_path, readme_mtime, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 0, ?, ?)
            """,
            (
                title,
                source_channel,
                doc_number,
                issuing_org,
                priority,
                deadline,
                category,
                summary,
                full_source_text,
                json.dumps(checklist, ensure_ascii=False),
                json.dumps(progress_logs, ensure_ascii=False),
                status,
                1 if payload.get("is_pinned") else 0,
                completed_at,
                payload.get("recurring_id"),
                folder_rel_path,
                now_str,
                now_str,
            ),
        )
        task_id = cur.lastrowid

        for fname, raw_bytes, role in extracted_file_blobs:
            prefix = "[来文]_" if role == "incoming" else "[材料]_"
            if not fname.startswith(("[来文]", "[材料]", "【来文】", "【材料】")):
                save_name = prefix + fname
            else:
                save_name = fname
            dest_path = resolve_unique_dest_path(task_folder, save_name, raw_bytes)
            dest_path.write_bytes(raw_bytes)
            st = dest_path.stat()
            ext_text, ftype = extract_document_text(dest_path)
            rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
            d_cur = cur.execute(
                """
                INSERT INTO documents (
                    task_id, filename, rel_path, category, doc_role,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
                """,
                (
                    task_id,
                    dest_path.name,
                    rel_file,
                    category,
                    role,
                    ftype,
                    st.st_size,
                    round(st.st_mtime, 2),
                    ext_text,
                    now_str,
                    now_str,
                ),
            )
            _sync_fts_doc(conn, d_cur.lastrowid)

        write_task_readme_file(conn, task_id)
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = serialize_task(conn, row, ws_root=ws_root)
        conn.close()
    return result


# ==================== 周期性/常态化工作自动派生引擎 ====================
def compute_current_period_key(freq_type: str, freq_value: int, today: Optional[date] = None) -> Tuple[str, bool, str]:
    d = today or date.today()
    if freq_type == "weekly":
        iso_y, iso_w, iso_d = d.isocalendar()
        pkey = f"{iso_y}-W{iso_w:02d}"
        target_weekday = max(1, min(7, int(freq_value or 4)))
        monday = d - timedelta(days=iso_d - 1)
        target_date = monday + timedelta(days=target_weekday - 1)
        is_due = d >= target_date
        return pkey, is_due, target_date.strftime("%Y-%m-%d")
    elif freq_type == "monthly":
        pkey = f"{d.year}-{d.month:02d}"
        target_day = max(1, min(28, int(freq_value or 25)))
        target_date = date(d.year, d.month, target_day)
        is_due = d.day >= target_day
        return pkey, is_due, target_date.strftime("%Y-%m-%d")
    elif freq_type == "quarterly":
        q = (d.month - 1) // 3 + 1
        pkey = f"{d.year}-Q{q}"
        end_month = q * 3
        target_day = max(1, min(28, int(freq_value or 25)))
        target_date = date(d.year, end_month, target_day)
        is_due = d >= (target_date - timedelta(days=7))
        return pkey, is_due, target_date.strftime("%Y-%m-%d")
    else:
        interval = max(1, int(freq_value or 7))
        epoch_days = (d - date(2026, 1, 1)).days
        bucket = epoch_days // interval
        pkey = f"D{interval}-{bucket}"
        return pkey, True, (d + timedelta(days=1)).strftime("%Y-%m-%d")


def spawn_task_from_recurring_rule(rule_id: int, force_manual: bool = False) -> Optional[Dict[str, Any]]:
    with DB_LOCK:
        conn = get_db()
        rule = conn.execute("SELECT * FROM recurring_rules WHERE id = ?", (rule_id,)).fetchone()
        if not rule:
            conn.close()
            return None

        pkey, is_due, base_deadline = compute_current_period_key(rule["freq_type"], rule["freq_value"])
        if not force_manual and (not rule["enabled"] or not is_due or rule["last_spawned_period"] == pkey):
            conn.close()
            return None

        actual_pkey = pkey if rule["last_spawned_period"] != pkey else f"{pkey}_{datetime.now().strftime('%m%d%H%M')}"
        title_rendered = (
            rule["title_template"]
            .replace("{period}", pkey)
            .replace("{month}", f"{date.today().month}月")
            .replace("{date}", date.today().strftime("%m-%d"))
        )
        if pkey not in title_rendered and "{period}" not in rule["title_template"]:
            title_rendered = f"【{pkey}】{title_rendered}"

        offset_days = int(rule["deadline_offset_days"] or 0)
        try:
            dl_dt = datetime.strptime(base_deadline, "%Y-%m-%d").date() + timedelta(days=offset_days)
            deadline_str = dl_dt.strftime("%Y-%m-%d")
        except Exception:
            deadline_str = base_deadline

        prev_draft_hint = "周期性工作自动派生"
        prev_doc = conn.execute(
            """
            SELECT d.filename, d.rel_path FROM documents d
            JOIN tasks t ON d.task_id = t.id
            WHERE t.recurring_id = ? AND d.doc_role IN ('draft', 'template')
            ORDER BY d.id DESC LIMIT 1
            """,
            (rule_id,),
        ).fetchone()
        if prev_doc:
            prev_draft_hint = f"已由周期规则自动派生，可参考上期定稿《{prev_doc['filename']}》起草本期材料"

        chk_list = json.loads(rule["checklist_json"] or "[]")
        conn.close()

    created_task = create_task_record({
        "title": title_rendered,
        "source_channel": rule["source_channel"] or "周期性工作",
        "issuing_org": rule["issuing_org"] or "",
        "priority": rule["priority"] or "重要",
        "deadline": deadline_str,
        "category": rule["category"] or "01_防灾减灾",
        "summary": rule["summary"] or f"周期性常态化工作（周期：{pkey}）",
        "checklist": chk_list,
        "initial_progress": prev_draft_hint,
        "recurring_id": rule_id,
        "auto_ai": False,
    })

    with DB_LOCK:
        conn = get_db()
        conn.execute(
            "UPDATE recurring_rules SET last_spawned_period = ?, last_spawned_at = ? WHERE id = ?",
            (actual_pkey, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), rule_id),
        )
        conn.commit()
        conn.close()
    return created_task


def check_and_spawn_all_recurring() -> int:
    with DB_LOCK:
        conn = get_db()
        rules = conn.execute("SELECT id FROM recurring_rules WHERE enabled = 1").fetchall()
        conn.close()
    spawned = 0
    for r in rules:
        if spawn_task_from_recurring_rule(r["id"], force_manual=False):
            spawned += 1
    return spawned


def seed_demo_data_if_empty() -> None:
    with DB_LOCK:
        conn = get_db()
        bad_rows = conn.execute(
            "SELECT id, title, category, folder_rel_path, created_at FROM tasks WHERE title LIKE '【工作群通知】@所有人%'"
        ).fetchall()
        ws_root_init = get_workspace_root()
        for br in bad_rows:
            clean_title = "报送《重点行业领域重大事故隐患排查整治周调度简表》及工作亮点小结"
            old_rel = br["folder_rel_path"] or ""
            new_rel = old_rel
            if old_rel and (ws_root_init / old_rel).exists():
                date_p = (br["created_at"] or "2026-11-15")[:10]
                target_abs = (ws_root_init / br["category"]) / sanitize_filename(f"{date_p}_{clean_title}")
                if not target_abs.exists():
                    shutil.move(str(ws_root_init / old_rel), str(target_abs))
                    new_rel = str(target_abs.relative_to(ws_root_init)).replace("\\", "/")
            conn.execute("UPDATE tasks SET title = ?, folder_rel_path = ? WHERE id = ?", (clean_title, new_rel, br["id"]))
            write_task_readme_file(conn, br["id"])

        rec_cnt = conn.execute("SELECT COUNT(*) FROM recurring_rules").fetchone()[0]
        if rec_cnt == 0:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cur_wk_key, _, _ = compute_current_period_key("weekly", 4)
            cur_mo_key, _, _ = compute_current_period_key("monthly", 25)
            conn.execute(
                """
                INSERT INTO recurring_rules (
                    title_template, category, source_channel, issuing_org, priority,
                    freq_type, freq_value, deadline_offset_days, summary, checklist_json,
                    enabled, last_spawned_period, last_spawned_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    "报送《重点行业领域重大事故隐患排查整治周调度简表》({period})",
                    "02_隐患排查与安全生产",
                    "上级来文",
                    "县安委办",
                    "重要",
                    "weekly",
                    4,
                    1,
                    "每周常态化调度：汇总工贸、危化、非煤矿山等重点领域重大事故隐患排查数据并撰写300字工作亮点。",
                    json.dumps([
                        {"id": "chk_1", "text": "汇总各领域本周重大事故隐患排查数据填写周调度简表", "done": False},
                        {"id": "chk_2", "text": "撰写300字以内本周工作亮点小结报综合科", "done": False},
                    ], ensure_ascii=False),
                    cur_wk_key,
                    now_str,
                    now_str,
                ),
            )
            conn.execute(
                """
                INSERT INTO recurring_rules (
                    title_template, category, source_channel, issuing_org, priority,
                    freq_type, freq_value, deadline_offset_days, summary, checklist_json,
                    enabled, last_spawned_period, last_spawned_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    "{month}全县自然灾害风险形势联合会商与月度防灾减灾工作总结",
                    "01_防灾减灾",
                    "上级来文",
                    "市应急管理局",
                    "重要",
                    "monthly",
                    25,
                    3,
                    "每月底常态化会商：联合气象、水利、自然资源开展下月自然灾害风险研判，并报送本月防灾减灾工作总结。",
                    json.dumps([
                        {"id": "chk_1", "text": "收集气象、水利、自然资源、森防部门下月风险研判意见", "done": False},
                        {"id": "chk_2", "text": "起草月度自然灾害风险形势会商报告与防灾减灾工作总结", "done": False},
                    ], ensure_ascii=False),
                    cur_mo_key,
                    now_str,
                    now_str,
                ),
            )

        cnt = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        conn.commit()
        conn.close()

    if cnt > 0:
        return

    sample_incoming_text = (
        "鲁应急发〔2026〕42号\n"
        "市应急管理局关于开展冬春自然灾害风险形势会商与低温雨雪冰冻防范工作的通知\n\n"
        "各区县应急管理局：\n"
        "为扎实做好今冬明春自然灾害防范应对工作，请各区县结合辖区实际开展以下工作：\n"
        "一、请于2026年11月18日前组织气象、水利、自然资源、住建等部门完成一轮冬春自然灾害风险形势联合会商，形成会商分析报告。\n"
        "二、全面摸排救灾物资储备库点（棉大衣、棉被、应急照明设备等）库存底数，于11月20日前报送《冬春救灾物资储备摸底统计表》。\n"
        "三、严格落实24小时值班和领导带班制度，遇突发灾情第一时间通过国家自然灾害灾情管理系统报送。"
    )
    sample_draft_text = (
        "关于我县2026—2027年度冬春自然灾害风险形势会商分析与防范工作方案（初稿）\n\n"
        "一、基本态势研判\n"
        "根据县气象局、水利局、自然资源和规划局联合会商意见，预计今冬明春我县气温较常年略偏低，存在阶段性寒潮与低温雨雪冰冻天气过程，森林火险等级在12月至次年2月处于较高水平。\n\n"
        "二、重点防范措施\n"
        "（一）强化监测预警与部门联合叫应机制\n"
        "建立气象、水利、应急、交通多部门全天候灾情会商机制，遇橙色及以上预警立即启动直达基层责任人的“叫应”闭环。\n"
        "（二）扎实做好救灾物资与装备统筹保障\n"
        "已完成县级救灾物资库清点，现储备棉被2800床、棉大衣2200件、折叠床1000张、帐篷600顶，账物相符。\n"
        "（三）加强重点区域隐患排查\n"
        "针对山区道路、老旧大棚及敬老院开展防冻防风专项巡查。"
    )

    import base64
    create_task_record({
        "title": "关于开展冬春自然灾害风险形势会商与低温雨雪冰冻防范工作的通知",
        "source_channel": "上级来文",
        "doc_number": "鲁应急发〔2026〕42号",
        "issuing_org": "市应急管理局",
        "priority": "紧急",
        "deadline": datetime.now().strftime("%Y-%m-%d"),
        "category": "01_防灾减灾",
        "raw_notice_text": sample_incoming_text,
        "auto_ai": True,
        "is_pinned": True,
        "initial_progress": "已联系气象、水利、自然资源科室收集会商意见，救灾物资储备底数正在核对。",
        "files": [
            {
                "filename": "鲁应急发2026-42号_关于开展冬春自然灾害风险形势会商的通知.txt",
                "content_base64": base64.b64encode(sample_incoming_text.encode("utf-8")).decode("ascii"),
                "doc_role": "incoming",
            },
            {
                "filename": "我县冬春自然灾害风险形势会商分析报告_初稿.md",
                "content_base64": base64.b64encode(sample_draft_text.encode("utf-8")).decode("ascii"),
                "doc_role": "draft",
            },
        ],
    })

    wechat_notice = (
        "【工作群通知】@所有人 接县政府办电话及群通知：请各单位结合本周安全生产与重大事故隐患排查整治情况，"
        "于11月19日下班前报送《重点行业领域重大事故隐患排查整治周调度简表》及300字以内工作亮点小结（电子版发综合科邮箱）。"
    )
    create_task_record({
        "raw_notice_text": wechat_notice,
        "source_channel": "微信群",
        "category": "02_隐患排查与安全生产",
        "auto_ai": True,
        "initial_progress": "已整理工贸与危化品企业本周排查数据，待补充亮点小结。",
    })

    ws_root = get_workspace_root()
    ref_file = ws_root / "03_综合汇报与总结" / "【范文模板】全县防灾减灾救灾工作阶段总结汇报模板.md"
    if not ref_file.exists():
        ref_file.write_text(
            "# 全县防灾减灾救灾工作阶段总结汇报（通用参考范文）\n\n"
            "## 一、工作总体开展情况\n"
            "今年以来，县应急管理局深入贯彻落实上级关于防灾减灾救灾决策部署，坚持“预防为主、防抗救相结合”，扎实推进自然灾害风险普查成果应用、冬春救助、防汛抗旱及森林防灭火工作。\n\n"
            "## 二、主要做法与成效\n"
            "1. **健全会商研判机制**：每季度联合气象、水利、自然资源、森防等部门开展综合风险会商。\n"
            "2. **夯实救灾物资与避难场所保障**：全县规范化管理应急避难场所，完成县乡两级救灾物资出入库台账数字化清点。\n"
            "3. **广泛开展防灾减灾宣传演练**：结合5·12防灾减灾宣传周与10·13国际减灾日开展进社区、进校园演练。\n\n"
            "## 三、存在问题与下一步打算\n"
            "持续紧盯极端天气防范，完善基层应急叫应闭环机制。\n",
            encoding="utf-8",
        )
    sync_filesystem(force=True)
    with DB_LOCK:
        conn = get_db()
        conn.execute(
            "UPDATE documents SET is_starred = 1, doc_role = 'template', tags_json = ? WHERE filename LIKE '%阶段总结汇报模板%'",
            (json.dumps(["防灾减灾", "总结汇报", "常用范文"], ensure_ascii=False),),
        )
        conn.commit()
        conn.close()


class WorkbenchHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = f"GWWorkbench/{VERSION}"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _is_localhost(self) -> bool:
        client_ip = self.client_address[0]
        return client_ip in ("127.0.0.1", "::1", "localhost")

    def _get_cookies(self) -> Dict[str, str]:
        raw_cookie = self.headers.get("Cookie", "")
        c = cookies.SimpleCookie()
        try:
            c.load(raw_cookie)
            return {k: urllib.parse.unquote(v.value) for k, v in c.items()}
        except Exception:
            return {}

    def _check_auth(self) -> Tuple[bool, bool]:
        cfg = load_config()
        agent_tok = self.headers.get("X-Agent-Token", "")
        if self._is_localhost() or (agent_tok and agent_tok == cfg.get("agent_token")):
            return True, True

        ck = self._get_cookies()
        view_pwd = str(cfg.get("view_password") or "")
        edit_pwd = str(cfg.get("edit_password") or "")

        header_view = self.headers.get("X-View-Password", "")
        header_edit = self.headers.get("X-Edit-Password", "")

        can_view = (not view_pwd) or (ck.get("wb_view_pwd") == view_pwd) or (header_view == view_pwd)
        can_edit = (ck.get("wb_edit_pwd") == edit_pwd) or (header_edit == edit_pwd)
        if can_edit:
            can_view = True
        return can_view, can_edit

    def _send_json(
        self,
        data: Any,
        status: int = 200,
        set_cookies: Optional[Dict[str, str]] = None,
        clear_cookies: Optional[List[str]] = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookies:
            for k, v in set_cookies.items():
                self.send_header(
                    "Set-Cookie",
                    f"{k}={urllib.parse.quote(v)}; Path=/; Max-Age=31536000; SameSite=Lax",
                )
        if clear_cookies:
            for k in clear_cookies:
                self.send_header(
                    "Set-Cookie",
                    f"{k}=; Path=/; Max-Age=0; SameSite=Lax",
                )
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        qs = urllib.parse.parse_qs(parsed_url.query)

        if path.startswith("/api/"):
            self.handle_api_get(path, qs)
            return

        if path == "/" or path == "/index.html":
            index_file = STATIC_DIR / "index.html"
            if index_file.exists():
                content = index_file.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return

        rel_static = path.lstrip("/")
        target_static = (STATIC_DIR / rel_static).resolve()
        if str(target_static).startswith(str(STATIC_DIR.resolve())) and target_static.is_file():
            ctype, _ = mimetypes.guess_type(str(target_static))
            content = target_static.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        self.send_error(404, "Not Found")

    def do_POST(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path.startswith("/api/"):
            self.handle_api_post(parsed_url.path)
        else:
            self.send_error(404, "Not Found")

    def do_PUT(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path.startswith("/api/"):
            self.handle_api_put(parsed_url.path)
        else:
            self.send_error(404, "Not Found")

    def do_DELETE(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path.startswith("/api/"):
            self.handle_api_delete(parsed_url.path)
        else:
            self.send_error(404, "Not Found")

    # ---------------- API GET ----------------
    def handle_api_get(self, path: str, qs: Dict[str, List[str]]) -> None:
        can_view, can_edit = self._check_auth()
        cfg = load_config()

        if path == "/api/auth/status":
            self._send_json({
                "version": VERSION,
                "can_view": can_view,
                "can_edit": can_edit,
                "is_localhost": self._is_localhost(),
                "lan_url": f"http://{get_lan_ip()}:{cfg.get('port', 8090)}",
            })
            return

        if not can_view:
            self._send_json({"error": "需要输入局域网访问密码", "need_login": True}, status=401)
            return

        if path == "/api/bootstrap":
            sync_stats = sync_filesystem(force=False)
            check_and_spawn_all_recurring()
            categories = list_physical_categories()
            ws_root = get_workspace_root(cfg)

            with DB_LOCK:
                conn = get_db()
                doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]

                rows = conn.execute("SELECT * FROM tasks ORDER BY is_pinned DESC, id DESC").fetchall()
                tasks = [
                    serialize_task(conn, r, include_refs=True, preloaded_docs=docs, ws_root=ws_root)
                    for r in rows
                ]

                def task_sort_key(t: Dict[str, Any]):
                    status_order = {"in_progress": 0, "pending": 1, "archived": 2}.get(t["status"], 1)
                    pinned_order = 0 if t["is_pinned"] else 1
                    if t["status"] == "archived":
                        comp_ts = t.get("completed_at") or t.get("updated_at") or ""
                        return (status_order, pinned_order, 0, -int(t["id"]), comp_ts)
                    days_left = t["deadline_info"]["days_left"]
                    prio_order = {"紧急": 0, "重要": 1, "普通": 2}.get(t.get("priority", "普通"), 2)
                    return (status_order, pinned_order, days_left, prio_order, -int(t["id"]))

                tasks.sort(key=task_sort_key)

                rec_rows = conn.execute("SELECT * FROM recurring_rules ORDER BY id ASC").fetchall()
                recurring_rules = []
                for rr in rec_rows:
                    r_dict = dict(rr)
                    r_dict["checklist"] = json.loads(r_dict.pop("checklist_json", "[]") or "[]")
                    r_dict["enabled"] = bool(r_dict.get("enabled", 1))
                    pkey, is_due, next_dl = compute_current_period_key(r_dict["freq_type"], r_dict["freq_value"])
                    r_dict["current_period"] = pkey
                    r_dict["next_deadline"] = next_dl
                    recurring_rules.append(r_dict)

                conn.close()

            self._send_json({
                "version": VERSION,
                "can_view": can_view,
                "can_edit": can_edit,
                "is_localhost": self._is_localhost(),
                "lan_url": f"http://{get_lan_ip()}:{cfg.get('port', 8090)}",
                "workspace_root": str(ws_root),
                "lan_notice": cfg.get("lan_notice", ""),
                "ai_configured": bool(cfg.get("ai_api_base")),
                "ai_model": cfg.get("ai_model", "deepseek-chat"),
                "ai_api_base": cfg.get("ai_api_base", ""),
                "xkl_gw_cli": cfg.get("xkl_gw_cli", ""),
                "prefix_archived_folder": bool(cfg.get("prefix_archived_folder", False)),
                "categories": categories,
                "tasks": tasks,
                "documents": docs,
                "recurring_rules": recurring_rules,
                "sync_stats": sync_stats,
            })
            return

        if path == "/api/tasks":
            status_filter = (qs.get("status") or [""])[0]
            cat_filter = (qs.get("category") or [""])[0]
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
                rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
                tasks = [
                    serialize_task(conn, r, preloaded_docs=docs, ws_root=ws_root)
                    for r in rows
                    if (not status_filter or r["status"] == status_filter)
                    and (not cat_filter or r["category"] == cat_filter)
                ]
                conn.close()
            self._send_json({"tasks": tasks})
            return

        if path == "/api/docs":
            cat_filter = (qs.get("category") or [""])[0]
            role_filter = (qs.get("doc_role") or [""])[0]
            starred_only = (qs.get("starred") or [""])[0] == "1"
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                docs = []
                for r in rows:
                    if cat_filter and r["category"] != cat_filter:
                        continue
                    if role_filter and r["doc_role"] != role_filter:
                        continue
                    if starred_only and not r["is_starred"]:
                        continue
                    docs.append(serialize_doc_light(r, ws_root=ws_root))
                conn.close()
            self._send_json({"documents": docs})
            return

        if path == "/api/recurring":
            with DB_LOCK:
                conn = get_db()
                rec_rows = conn.execute("SELECT * FROM recurring_rules ORDER BY id ASC").fetchall()
                rules = []
                for rr in rec_rows:
                    d = dict(rr)
                    d["checklist"] = json.loads(d.pop("checklist_json", "[]") or "[]")
                    d["enabled"] = bool(d.get("enabled", 1))
                    rules.append(d)
                conn.close()
            self._send_json({"recurring_rules": rules})
            return

        m_prev = re.match(r"^/api/docs/(\d+)/preview$", path)
        if m_prev:
            doc_id = int(m_prev.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                conn.close()
            if not row:
                self._send_json({"error": "文件不存在"}, status=404)
                return
            d = serialize_doc_light(row)
            d["extracted_text"] = row["extracted_text"] or ""
            d["is_editable_text"] = Path(row["filename"]).suffix.lower() in (".md", ".txt", ".markdown", ".csv", ".json")
            self._send_json({"document": d})
            return

        m_dl = re.match(r"^/api/docs/(\d+)/download$", path)
        if m_dl:
            doc_id = int(m_dl.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                conn.close()
            if not row:
                self.send_error(404, "File Not Found")
                return
            file_path = get_workspace_root(cfg) / row["rel_path"]
            if not file_path.exists():
                self.send_error(404, "Physical File Not Found")
                return
            data = file_path.read_bytes()
            ctype, _ = mimetypes.guess_type(str(file_path))
            encoded_name = urllib.parse.quote(row["filename"])
            self.send_response(200)
            self.send_header("Content-Type", ctype or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{encoded_name}\"; filename*=UTF-8''{encoded_name}",
            )
            self.end_headers()
            self.wfile.write(data)
            return

        if path == "/api/docs/batch-zip":
            ids_raw = (qs.get("ids") or [""])[0]
            doc_ids = [int(x) for x in ids_raw.split(",") if x.strip().isdigit()]
            ws_root = get_workspace_root(cfg)
            buf = io.BytesIO()
            with DB_LOCK:
                conn = get_db()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for did in doc_ids:
                        r = conn.execute("SELECT filename, rel_path, category FROM documents WHERE id = ?", (did,)).fetchone()
                        if r:
                            fp = ws_root / r["rel_path"]
                            if fp.is_file():
                                zf.write(fp, arcname=f"{r['category']}/{r['filename']}")
                conn.close()
            zip_bytes = buf.getvalue()
            zip_name = urllib.parse.quote(f"公文与材料批量打包_{datetime.now().strftime('%Y%m%d_%H%M')}.zip")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{zip_name}")
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        m_tzip = re.match(r"^/api/tasks/(\d+)/zip$", path)
        if m_tzip:
            task_id = int(m_tzip.group(1))
            with DB_LOCK:
                conn = get_db()
                write_task_readme_file(conn, task_id)
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                conn.close()
            if not row:
                self.send_error(404, "Task Not Found")
                return
            folder_path = get_workspace_root(cfg) / row["folder_rel_path"]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                if folder_path.exists():
                    for f in folder_path.rglob("*"):
                        if f.is_file() and not f.name.startswith("~$") and f.name != ".gitkeep":
                            zf.write(f, arcname=f.relative_to(folder_path))
            zip_bytes = buf.getvalue()
            zip_name = urllib.parse.quote(f"{row['title']}_全套公文与材料.zip")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(zip_bytes)))
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=\"{zip_name}\"; filename*=UTF-8''{zip_name}",
            )
            self.end_headers()
            self.wfile.write(zip_bytes)
            return

        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            scope = (qs.get("scope") or ["full"])[0]
            cat = (qs.get("category") or [""])[0]
            role = (qs.get("doc_role") or [""])[0]
            tasks, docs = execute_search(q, scope=scope, category=cat, doc_role=role)
            self._send_json({
                "query": q,
                "scope": scope,
                "tasks": tasks,
                "documents": docs,
            })
            return

        if path == "/api/export/ledger":
            fmt = (qs.get("format") or ["csv"])[0].lower()
            status_filter = (qs.get("status") or [""])[0]
            cat_filter = (qs.get("category") or [""])[0]
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY id ASC").fetchall()
                docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
                rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
                tasks = [
                    serialize_task(conn, r, include_refs=False, preloaded_docs=docs, ws_root=ws_root)
                    for r in rows
                    if (not status_filter or r["status"] == status_filter)
                    and (not cat_filter or r["category"] == cat_filter)
                ]
                conn.close()

            status_cn = {"pending": "待办", "in_progress": "进行中", "archived": "已完成(归档)"}
            if fmt == "md":
                lines = [
                    f"# 办文与工作台账汇总（导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}）",
                    "",
                    "| 序号 | 状态 | 来源渠道 | 发文字号/单位 | 事项名称 | 截止日期 | 核心工作要求 | 最新进展 | 产出材料清单 |",
                    "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
                ]
                for i, t in enumerate(tasks, 1):
                    latest_prog = t["progress_logs"][0]["text"] if t["progress_logs"] else "暂无"
                    draft_names = "、".join(d["filename"] for d in t["drafted_files"]) or "无"
                    org_num = " ".join(x for x in [t.get("issuing_org"), t.get("doc_number")] if x) or "-"
                    lines.append(
                        f"| {i} | {status_cn.get(t['status'], t['status'])} | {t['source_channel']} | "
                        f"{org_num} | {t['title']} | {t.get('deadline') or '常态'} | "
                        f"{(t.get('summary') or '').replace('|', '｜')[:60]} | {latest_prog} | {draft_names} |"
                    )
                md_bytes = ("\n".join(lines) + "\n").encode("utf-8")
                fname = urllib.parse.quote(f"办文与工作台账_{datetime.now().strftime('%Y%m%d')}.md")
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fname}")
                self.end_headers()
                self.wfile.write(md_bytes)
                return
            else:
                out = io.StringIO()
                writer = csv.writer(out)
                writer.writerow([
                    "序号", "状态", "所属分类", "来源渠道", "来文单位", "发文字号",
                    "事项名称", "优先级", "截止日期", "办结归档时间", "核心要求摘要", "待办清单完成情况",
                    "最新进展记录", "来文原件", "我撰写的材料", "本地文件夹路径", "创建时间",
                ])
                for i, t in enumerate(tasks, 1):
                    chk_str = "；".join(
                        f"[{'√' if c.get('done') else ' '}] {c.get('text', '')}"
                        for c in t["checklist"]
                    )
                    prog_str = "；".join(
                        f"({p.get('time', '')}) {p.get('text', '')}"
                        for p in t["progress_logs"]
                    )
                    inc_str = "、".join(d["filename"] for d in t["incoming_files"])
                    drf_str = "、".join(d["filename"] for d in t["drafted_files"])
                    writer.writerow([
                        i,
                        status_cn.get(t["status"], t["status"]),
                        t.get("category", ""),
                        t.get("source_channel", ""),
                        t.get("issuing_org", ""),
                        t.get("doc_number", ""),
                        t.get("title", ""),
                        t.get("priority", ""),
                        t.get("deadline", ""),
                        t.get("completed_at", ""),
                        t.get("summary", ""),
                        chk_str,
                        prog_str,
                        inc_str,
                        drf_str,
                        t.get("folder_rel_path", ""),
                        t.get("created_at", ""),
                    ])
                csv_bytes = out.getvalue().encode("utf-8-sig")
                fname = urllib.parse.quote(f"办文与工作台账_{datetime.now().strftime('%Y%m%d')}.csv")
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8-sig")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{fname}")
                self.end_headers()
                self.wfile.write(csv_bytes)
                return

        self._send_json({"error": f"未知接口: {path}"}, status=404)

    # ---------------- API POST ----------------
    def handle_api_post(self, path: str) -> None:
        body = self._read_json_body()
        cfg = load_config()

        if path == "/api/auth/login":
            pwd = str(body.get("password") or "").strip()
            cookies_to_set = {}
            if pwd == str(cfg.get("edit_password")):
                cookies_to_set["wb_view_pwd"] = str(cfg.get("view_password"))
                cookies_to_set["wb_edit_pwd"] = str(cfg.get("edit_password"))
                self._send_json({"ok": True, "can_view": True, "can_edit": True}, set_cookies=cookies_to_set)
                return
            elif pwd == str(cfg.get("view_password")):
                cookies_to_set["wb_view_pwd"] = pwd
                self._send_json({"ok": True, "can_view": True, "can_edit": False}, set_cookies=cookies_to_set)
                return
            else:
                self._send_json({"ok": False, "error": "访问密码不正确"}, status=403)
                return

        if path == "/api/auth/unlock-edit":
            pwd = str(body.get("password") or "").strip()
            if pwd == str(cfg.get("edit_password")) or self._is_localhost():
                self._send_json(
                    {"ok": True, "can_edit": True},
                    set_cookies={"wb_edit_pwd": str(cfg.get("edit_password"))},
                )
            else:
                self._send_json({"ok": False, "error": "主理人编辑口令不正确"}, status=403)
            return

        if path == "/api/auth/lock-edit":
            self._send_json({"ok": True, "can_edit": False}, clear_cookies=["wb_edit_pwd"])
            return

        can_view, can_edit = self._check_auth()
        if not can_view:
            self._send_json({"error": "未登录"}, status=401)
            return

        if path in ("/api/ai/search", "/api/agent/context-search"):
            query = str(body.get("query") or "").strip()
            category = str(body.get("category") or "").strip()
            kws = extract_search_keywords(query)
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                all_doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                preloaded_light_docs = [serialize_doc_light(r, ws_root=ws_root) for r in all_doc_rows]

                all_tasks = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
                scored_tasks = []
                for r in all_tasks:
                    if category and r["category"] != category:
                        continue
                    hay = f"{r['title']} {r['doc_number']} {r['issuing_org']} {r['summary']} {r['raw_notice_text']} {r['progress_json']}".lower()
                    hits = sum(2 if kw.lower() in r["title"].lower() else (1 if kw.lower() in hay else 0) for kw in kws)
                    if hits > 0:
                        t_obj = serialize_task(conn, r, include_refs=False, preloaded_docs=preloaded_light_docs, ws_root=ws_root)
                        t_obj["match_snippet"] = build_highlight_snippet(hay, kws)
                        scored_tasks.append((hits, t_obj))
                scored_tasks.sort(key=lambda x: (-x[0], -x[1]["id"]))
                top_tasks = [x[1] for x in scored_tasks[:12]]

                all_docs = conn.execute("SELECT * FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                scored_docs = []
                for r in all_docs:
                    if category and r["category"] != category:
                        continue
                    hay_title = (r["filename"] or "").lower()
                    hay_body = (r["extracted_text"] or "").lower()
                    hits = sum(3 if kw.lower() in hay_title else (1 if kw.lower() in hay_body else 0) for kw in kws)
                    if hits > 0:
                        snip = build_highlight_snippet(r["extracted_text"] or r["filename"], kws)
                        d_obj = serialize_doc_light(r, snippet=snip, ws_root=ws_root)
                        if path == "/api/agent/context-search":
                            d_obj["extracted_text_chunk"] = (r["extracted_text"] or "")[:2000]
                        scored_docs.append((hits, d_obj))
                scored_docs.sort(key=lambda x: (-x[0], -x[1]["id"]))
                top_docs = [x[1] for x in scored_docs[:18]]
                conn.close()

            ai_res = synthesize_ai_search_answer(query, kws, top_tasks, top_docs, config=cfg)
            self._send_json({
                "query": query,
                "keywords": kws,
                "answer": ai_res["answer"],
                "engine": ai_res["engine"],
                "tasks": top_tasks,
                "documents": top_docs,
            })
            return

        if not can_edit:
            self._send_json({"error": "当前为【浏览与下载模式】，请点击右上角开启【主理人编辑模式】后再修改"}, status=403)
            return

        if path == "/api/sync":
            stats = sync_filesystem(force=True)
            spawned = check_and_spawn_all_recurring()
            stats["spawned_recurring"] = spawned
            self._send_json({"ok": True, "stats": stats})
            return

        if path == "/api/local/open":
            if not self._is_localhost():
                self._send_json({"error": "仅限在工作台主机（127.0.0.1）上直接调起本机 WPS 或文件管理器，局域网同事请使用【下载】或【在线预览】"}, status=403)
                return
            ws_root = get_workspace_root(cfg)
            target_type = body.get("type") or "file"
            rel_path = str(body.get("rel_path") or "").strip()
            target_abs = (ws_root / rel_path).resolve()
            if not str(target_abs).startswith(str(ws_root.resolve())) or not target_abs.exists():
                self._send_json({"error": f"本地路径不存在: {target_abs}"}, status=404)
                return

            launched_cmd = ""
            try:
                if target_type == "folder" or target_abs.is_dir():
                    folder_to_open = target_abs if target_abs.is_dir() else target_abs.parent
                    for fm in ("peony", "caja", "dde-file-manager", "nautilus", "xdg-open"):
                        if shutil.which(fm):
                            subprocess.Popen([fm, str(folder_to_open)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            launched_cmd = f"{fm} {folder_to_open}"
                            break
                else:
                    ext = target_abs.suffix.lower()
                    candidates = ["wps", "xdg-open"]
                    if ext in (".xlsx", ".xls", ".csv", ".et"):
                        candidates = ["et", "wps", "xdg-open"]
                    for app in candidates:
                        if shutil.which(app):
                            subprocess.Popen([app, str(target_abs)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            launched_cmd = f"{app} {target_abs}"
                            break
            except Exception as e:
                self._send_json({"ok": False, "error": f"调起本地应用异常: {e}", "abs_path": str(target_abs)}, status=500)
                return

            self._send_json({
                "ok": True,
                "launched": bool(launched_cmd),
                "command": launched_cmd or "未检测到桌面图形启动器，已为您复制本地绝对路径",
                "abs_path": str(target_abs),
            })
            return

        if path == "/api/docs/save-text":
            ws_root = get_workspace_root(cfg)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            doc_id = body.get("doc_id")
            task_id = body.get("task_id")
            content = str(body.get("content") or "")
            filename = sanitize_filename(body.get("filename") or "新建材料草稿.md")
            if not filename.lower().endswith((".md", ".txt", ".markdown")):
                filename += ".md"

            with DB_LOCK:
                conn = get_db()
                if doc_id:
                    d_row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()
                    if not d_row:
                        conn.close()
                        self._send_json({"error": "文档不存在"}, status=404)
                        return
                    dest_path = ws_root / d_row["rel_path"]
                    dest_path.write_text(content, encoding="utf-8")
                    st = dest_path.stat()
                    conn.execute(
                        "UPDATE documents SET file_size = ?, mtime = ?, extracted_text = ?, updated_at = ? WHERE id = ?",
                        (st.st_size, round(st.st_mtime, 2), content, now_str, int(doc_id)),
                    )
                    _sync_fts_doc(conn, int(doc_id))
                    u_task = None
                    if d_row["task_id"]:
                        write_task_readme_file(conn, d_row["task_id"])
                        u_task = serialize_task(conn, conn.execute("SELECT * FROM tasks WHERE id = ?", (d_row["task_id"],)).fetchone(), ws_root=ws_root)
                    conn.commit()
                    u_row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(doc_id),)).fetchone()
                    res_doc = serialize_doc_light(u_row, ws_root=ws_root)
                    res_doc["extracted_text"] = content
                    conn.close()
                    self._send_json({"ok": True, "document": res_doc, "task": u_task})
                    return
                elif task_id:
                    t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
                    if not t_row:
                        conn.close()
                        self._send_json({"error": "关联事项不存在"}, status=404)
                        return
                    task_folder = ws_root / t_row["folder_rel_path"]
                    task_folder.mkdir(parents=True, exist_ok=True)
                    if not filename.startswith(("[材料]", "【材料】")):
                        filename = "[材料]_" + filename
                    dest_path = resolve_unique_dest_path(task_folder, filename, content.encode("utf-8"))
                    dest_path.write_text(content, encoding="utf-8")
                    st = dest_path.stat()
                    rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
                    cur = conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'draft', '文本/Markdown', ?, ?, ?, '[]', 0, 0, ?, ?)
                        """,
                        (int(task_id), dest_path.name, rel_file, t_row["category"], st.st_size, round(st.st_mtime, 2), content, now_str, now_str),
                    )
                    new_doc_id = cur.lastrowid
                    _sync_fts_doc(conn, new_doc_id)
                    write_task_readme_file(conn, int(task_id))
                    conn.commit()
                    u_task = serialize_task(conn, conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone(), ws_root=ws_root)
                    u_doc = serialize_doc_light(conn.execute("SELECT * FROM documents WHERE id = ?", (new_doc_id,)).fetchone(), ws_root=ws_root)
                    u_doc["extracted_text"] = content
                    conn.close()
                    self._send_json({"ok": True, "document": u_doc, "task": u_task})
                    return
                else:
                    cat = sanitize_filename(body.get("category") or "03_综合汇报与总结")
                    cat_dir = ensure_category_dir(ws_root, cat)
                    dest_path = resolve_unique_dest_path(cat_dir, filename, content.encode("utf-8"))
                    dest_path.write_text(content, encoding="utf-8")
                    st = dest_path.stat()
                    rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
                    cur = conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (NULL, ?, ?, ?, 'draft', '文本/Markdown', ?, ?, ?, '[]', 0, 0, ?, ?)
                        """,
                        (dest_path.name, rel_file, cat, st.st_size, round(st.st_mtime, 2), content, now_str, now_str),
                    )
                    new_doc_id = cur.lastrowid
                    _sync_fts_doc(conn, new_doc_id)
                    conn.commit()
                    u_doc = serialize_doc_light(conn.execute("SELECT * FROM documents WHERE id = ?", (new_doc_id,)).fetchone(), ws_root=ws_root)
                    u_doc["extracted_text"] = content
                    conn.close()
                    self._send_json({"ok": True, "document": u_doc})
                    return

        m_fmt = re.match(r"^/api/docs/(\d+)/format-gw$", path)
        if m_fmt:
            doc_id = int(m_fmt.group(1))
            mode = body.get("mode") or "report"
            custom_doc_num = str(body.get("doc_number") or "").strip()
            custom_org = str(body.get("issuing_org") or "").strip()
            ws_root = get_workspace_root(cfg)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            with DB_LOCK:
                conn = get_db()
                d_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                if not d_row:
                    conn.close()
                    self._send_json({"error": "原文档不存在"}, status=404)
                    return

                t_row = None
                if d_row["task_id"]:
                    t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (d_row["task_id"],)).fetchone()

                src_abs = ws_root / d_row["rel_path"]
                stem = re.sub(r"^(\[材料\]_|\[来文\]_)+", "", Path(d_row["filename"]).stem)
                out_name = f"[材料]_{stem}_国标排版.docx"
                out_abs = src_abs.parent / sanitize_filename(out_name)

                doc_num = custom_doc_num or (t_row["doc_number"] if t_row else "")
                org_name = custom_org or (t_row["issuing_org"] if t_row and t_row["issuing_org"] else "县应急管理局")
                raw_text = d_row["extracted_text"] or ""

                build_gb9704_docx(
                    output_path=out_abs,
                    raw_text=raw_text,
                    title=stem,
                    doc_number=doc_num,
                    issuing_org=org_name,
                    mode=mode,
                    xkl_gw_cli=cfg.get("xkl_gw_cli", ""),
                )

                st = out_abs.stat()
                ext_text, ftype = extract_document_text(out_abs)
                rel_file = str(out_abs.relative_to(ws_root)).replace("\\", "/")
                conn.execute(
                    """
                    INSERT INTO documents (
                        task_id, filename, rel_path, category, doc_role,
                        file_type, file_size, mtime, extracted_text,
                        tags_json, is_starred, is_pinned, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    ON CONFLICT(rel_path) DO UPDATE SET
                        file_size=excluded.file_size,
                        mtime=excluded.mtime,
                        extracted_text=excluded.extracted_text,
                        updated_at=excluded.updated_at
                    """,
                    (
                        d_row["task_id"],
                        out_abs.name,
                        rel_file,
                        d_row["category"],
                        ftype,
                        st.st_size,
                        round(st.st_mtime, 2),
                        ext_text,
                        json.dumps(["国标公文", "GB/T9704"], ensure_ascii=False),
                        now_str,
                        now_str,
                    ),
                )
                row_new = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
                if row_new:
                    _sync_fts_doc(conn, row_new["id"])
                if d_row["task_id"]:
                    write_task_readme_file(conn, d_row["task_id"])
                conn.commit()
                res_doc = serialize_doc_light(row_new, ws_root=ws_root)
                res_task = (
                    serialize_task(conn, conn.execute("SELECT * FROM tasks WHERE id = ?", (d_row["task_id"],)).fetchone(), ws_root=ws_root)
                    if d_row["task_id"]
                    else None
                )
                conn.close()

            self._send_json({"ok": True, "document": res_doc, "task": res_task})
            return

        if path == "/api/ai/parse":
            text = str(body.get("text") or "")
            filename = str(body.get("filename") or "")
            if body.get("content_base64") and filename:
                import base64
                import tempfile
                b64 = body["content_base64"]
                if "," in b64 and b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                raw_bytes = base64.b64decode(b64)
                with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tf:
                    tf.write(raw_bytes)
                    tmp_p = Path(tf.name)
                try:
                    ext_txt, _ = extract_document_text(tmp_p)
                    text = (text + "\n\n" + ext_txt).strip()
                finally:
                    tmp_p.unlink(missing_ok=True)

            parsed = parse_incoming_content(
                text,
                filename=filename,
                source_hint=str(body.get("source_channel") or ""),
                categories=list_physical_categories(),
                config=cfg,
            )
            parsed["extracted_text"] = text
            self._send_json({"ok": True, "parsed": parsed})
            return

        if path == "/api/tasks":
            created = create_task_record(body)
            self._send_json({"ok": True, "task": created})
            return

        m_tfiles = re.match(r"^/api/tasks/(\d+)/files$", path)
        if m_tfiles:
            task_id = int(m_tfiles.group(1))
            files_list = body.get("files") or []
            doc_role = body.get("doc_role") or "draft"
            ws_root = get_workspace_root(cfg)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with DB_LOCK:
                conn = get_db()
                t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not t_row:
                    conn.close()
                    self._send_json({"error": "事项不存在"}, status=404)
                    return
                task_folder = ws_root / t_row["folder_rel_path"]
                task_folder.mkdir(parents=True, exist_ok=True)
                import base64
                for fitem in files_list:
                    fname = sanitize_filename(fitem.get("filename") or "未命名文件.docx")
                    role = fitem.get("doc_role") or doc_role
                    prefix = "[来文]_" if role == "incoming" else "[材料]_"
                    if not fname.startswith(("[来文]", "[材料]", "【来文】", "【材料】")):
                        save_name = prefix + fname
                    else:
                        save_name = fname
                    b64 = fitem.get("content_base64") or ""
                    if "," in b64 and b64.startswith("data:"):
                        b64 = b64.split(",", 1)[1]
                    raw_bytes = base64.b64decode(b64) if b64 else b""
                    if fitem.get("local_source_path") and not raw_bytes:
                        sp = Path(fitem["local_source_path"]).expanduser()
                        if sp.is_file():
                            raw_bytes = sp.read_bytes()
                    dest = resolve_unique_dest_path(task_folder, save_name, raw_bytes)
                    dest.write_bytes(raw_bytes)
                    st = dest.stat()
                    ext_txt, ftype = extract_document_text(dest)
                    rel_file = str(dest.relative_to(ws_root)).replace("\\", "/")
                    conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
                        ON CONFLICT(rel_path) DO UPDATE SET
                            file_size=excluded.file_size,
                            mtime=excluded.mtime,
                            extracted_text=excluded.extracted_text,
                            updated_at=excluded.updated_at
                        """,
                        (
                            task_id,
                            dest.name,
                            rel_file,
                            t_row["category"],
                            role,
                            ftype,
                            st.st_size,
                            round(st.st_mtime, 2),
                            ext_txt,
                            now_str,
                            now_str,
                        ),
                    )
                    r_doc = conn.execute("SELECT id FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
                    if r_doc:
                        _sync_fts_doc(conn, r_doc["id"])
                write_task_readme_file(conn, task_id)
                conn.commit()
                updated_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                res_task = serialize_task(conn, updated_row, ws_root=ws_root)
                conn.close()
            self._send_json({"ok": True, "task": res_task})
            return

        if path == "/api/docs/upload":
            category = sanitize_filename(body.get("category") or "05_历史公文归档")
            doc_role = body.get("doc_role") or "draft"
            tags = body.get("tags") or []
            files_list = body.get("files") or []
            ws_root = get_workspace_root(cfg)
            cat_dir = ensure_category_dir(ws_root, category)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            added_docs = []
            import base64
            with DB_LOCK:
                conn = get_db()
                for fitem in files_list:
                    fname = sanitize_filename(fitem.get("filename") or "未命名公文.docx")
                    b64 = fitem.get("content_base64") or ""
                    if "," in b64 and b64.startswith("data:"):
                        b64 = b64.split(",", 1)[1]
                    raw_bytes = base64.b64decode(b64) if b64 else b""
                    dest = resolve_unique_dest_path(cat_dir, fname, raw_bytes)
                    dest.write_bytes(raw_bytes)
                    st = dest.stat()
                    ext_txt, ftype = extract_document_text(dest)
                    rel_file = str(dest.relative_to(ws_root)).replace("\\", "/")
                    conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                        ON CONFLICT(rel_path) DO UPDATE SET
                            doc_role=excluded.doc_role,
                            file_size=excluded.file_size,
                            mtime=excluded.mtime,
                            extracted_text=excluded.extracted_text,
                            updated_at=excluded.updated_at
                        """,
                        (
                            dest.name,
                            rel_file,
                            category,
                            doc_role,
                            ftype,
                            st.st_size,
                            round(st.st_mtime, 2),
                            ext_txt,
                            json.dumps(tags, ensure_ascii=False),
                            1 if body.get("is_starred") else 0,
                            now_str,
                            now_str,
                        ),
                    )
                    row = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
                    if row:
                        _sync_fts_doc(conn, row["id"])
                        added_docs.append(serialize_doc_light(row, ws_root=ws_root))
                conn.commit()
                conn.close()
            self._send_json({"ok": True, "documents": added_docs})
            return

        # 【公文与材料库】多选批量归纳操作（修复 Bug 2：移动关联事项的文件时，自动同步迁移所属事项文件夹，保证物理路径与分类 100% 一致）
        if path == "/api/docs/batch":
            doc_ids = [int(x) for x in (body.get("doc_ids") or []) if str(x).isdigit()]
            action = str(body.get("action") or "").strip()
            ws_root = get_workspace_root(cfg)
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if not doc_ids:
                self._send_json({"error": "请先勾选至少一份文件"}, status=400)
                return

            updated_count = 0
            with DB_LOCK:
                conn = get_db()
                for did in doc_ids:
                    row = conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
                    if not row:
                        continue
                    if action == "move_category":
                        target_cat = sanitize_filename(body.get("category") or row["category"])
                        if target_cat != row["category"]:
                            if row["task_id"] is not None:
                                # 同步移动该文件所属事项的物理文件夹至目标分类目录！
                                new_cat_val, new_rel_folder = _move_task_folder_to_category(
                                    conn, ws_root, int(row["task_id"]), target_cat, prefix_archived=bool(cfg.get("prefix_archived_folder"))
                                )
                                conn.execute(
                                    "UPDATE tasks SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?",
                                    (new_cat_val, new_rel_folder, now_str, int(row["task_id"])),
                                )
                                write_task_readme_file(conn, int(row["task_id"]))
                            else:
                                target_dir = ensure_category_dir(ws_root, target_cat)
                                old_abs = ws_root / row["rel_path"]
                                new_rel = row["rel_path"]
                                if old_abs.is_file():
                                    new_abs = resolve_unique_dest_path(target_dir, row["filename"])
                                    shutil.move(str(old_abs), str(new_abs))
                                    new_rel = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                                conn.execute(
                                    "UPDATE documents SET category = ?, rel_path = ?, filename = ?, updated_at = ? WHERE id = ?",
                                    (target_cat, new_rel, Path(new_rel).name, now_str, did),
                                )
                                _sync_fts_doc(conn, did)
                            updated_count += 1
                    elif action == "add_tags":
                        new_tags_in = [t.strip() for t in (body.get("tags") or []) if str(t).strip()]
                        curr_tags = json.loads(row["tags_json"] or "[]")
                        for nt in new_tags_in:
                            if nt not in curr_tags:
                                curr_tags.append(nt)
                        conn.execute(
                            "UPDATE documents SET tags_json = ?, updated_at = ? WHERE id = ?",
                            (json.dumps(curr_tags, ensure_ascii=False), now_str, did),
                        )
                        _sync_fts_doc(conn, did)
                        updated_count += 1
                    elif action == "star":
                        val = 1 if body.get("value", True) else 0
                        conn.execute("UPDATE documents SET is_starred = ?, updated_at = ? WHERE id = ?", (val, now_str, did))
                        updated_count += 1
                    elif action == "change_role":
                        new_role = body.get("doc_role") or "draft"
                        conn.execute("UPDATE documents SET doc_role = ?, updated_at = ? WHERE id = ?", (new_role, now_str, did))
                        updated_count += 1
                    elif action == "delete":
                        if body.get("delete_disk") and row["rel_path"]:
                            f_abs = ws_root / row["rel_path"]
                            if f_abs.is_file():
                                f_abs.unlink(missing_ok=True)
                        conn.execute("DELETE FROM documents WHERE id = ?", (did,))
                        conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (did,))
                        updated_count += 1
                conn.commit()
                conn.close()
            self._send_json({"ok": True, "updated_count": updated_count})
            return

        if path == "/api/docs/scan-local-dir":
            src_dir_str = str(body.get("source_dir") or "").strip()
            target_cat = sanitize_filename(body.get("category") or "05_历史公文归档")
            smart_auto_cat = bool(body.get("smart_auto_category", False))
            if not src_dir_str:
                self._send_json({"error": "请输入本机文件夹路径"}, status=400)
                return
            src_dir = Path(src_dir_str).expanduser().resolve()
            if not src_dir.exists() or not src_dir.is_dir():
                self._send_json({"error": f"本机目录不存在: {src_dir}"}, status=400)
                return

            ws_root = get_workspace_root(cfg)
            avail_cats = list_physical_categories()
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            allowed_exts = {".docx", ".doc", ".wps", ".ofd", ".pdf", ".txt", ".md", ".xlsx", ".xls", ".csv"}
            imported_count = 0

            with DB_LOCK:
                conn = get_db()
                for f in sorted(src_dir.rglob("*")):
                    if f.is_file() and not f.name.startswith(".") and not f.name.startswith("~$"):
                        if f.suffix.lower() in allowed_exts:
                            raw_bytes = f.read_bytes()
                            ext_txt, ftype = extract_document_text(f)
                            rel_parts = list(f.relative_to(src_dir).parts[:-1])
                            sub_tags = [p for p in rel_parts if p and not p.startswith(".")]

                            chosen_cat = match_best_category(f"{f.name} {ext_txt[:400]}", avail_cats) if smart_auto_cat else target_cat
                            dest_cat_dir = ensure_category_dir(ws_root, chosen_cat)
                            target_f = resolve_unique_dest_path(dest_cat_dir, f.name, raw_bytes)
                            if target_f.resolve() != f.resolve():
                                shutil.copy2(f, target_f)
                            st = target_f.stat()
                            rel_file = str(target_f.relative_to(ws_root)).replace("\\", "/")
                            role = infer_doc_role(target_f.name, ext_txt[:300], in_task_folder=False, cat_name=chosen_cat)

                            conn.execute(
                                """
                                INSERT INTO documents (
                                    task_id, filename, rel_path, category, doc_role,
                                    file_type, file_size, mtime, extracted_text,
                                    tags_json, is_starred, is_pinned, created_at, updated_at
                                ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                                ON CONFLICT(rel_path) DO UPDATE SET
                                    tags_json=excluded.tags_json,
                                    extracted_text=excluded.extracted_text,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    target_f.name,
                                    rel_file,
                                    chosen_cat,
                                    role,
                                    ftype,
                                    st.st_size,
                                    round(st.st_mtime, 2),
                                    ext_txt,
                                    json.dumps(sub_tags, ensure_ascii=False),
                                    1 if role == "template" else 0,
                                    now_str,
                                    now_str,
                                ),
                            )
                            r_doc = conn.execute("SELECT id FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
                            if r_doc:
                                _sync_fts_doc(conn, r_doc["id"])
                            imported_count += 1
                conn.commit()
                conn.close()

            sync_stats = sync_filesystem(force=True)
            self._send_json({
                "ok": True,
                "imported_count": imported_count,
                "sync_stats": sync_stats,
                "target_category": target_cat,
            })
            return

        m_totask = re.match(r"^/api/docs/(\d+)/to-task$", path)
        if m_totask:
            doc_id = int(m_totask.group(1))
            with DB_LOCK:
                conn = get_db()
                d_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                conn.close()
            if not d_row:
                self._send_json({"error": "文件不存在"}, status=404)
                return
            ws_root = get_workspace_root(cfg)
            src_file = ws_root / d_row["rel_path"]
            task_obj = create_task_record({
                "raw_notice_text": d_row["extracted_text"] or "",
                "category": d_row["category"],
                "source_channel": "上级来文",
                "auto_ai": True,
                "files": [
                    {
                        "filename": d_row["filename"],
                        "local_source_path": str(src_file),
                        "doc_role": "incoming",
                    }
                ],
            })
            self._send_json({"ok": True, "task": task_obj})
            return

        if path == "/api/categories":
            cat_name = sanitize_filename(body.get("name") or "")
            if not cat_name:
                self._send_json({"error": "分类目录名称不能为空"}, status=400)
                return
            ws_root = get_workspace_root(cfg)
            ensure_category_dir(ws_root, cat_name)
            self._send_json({"ok": True, "categories": list_physical_categories()})
            return

        if path == "/api/recurring":
            title_tpl = str(body.get("title_template") or "").strip()
            if not title_tpl:
                self._send_json({"error": "规则标题模板不能为空"}, status=400)
                return
            chk_raw = body.get("checklist") or []
            norm_chk = []
            for idx, item in enumerate(chk_raw):
                if isinstance(item, str) and item.strip():
                    norm_chk.append({"id": f"chk_{idx+1}", "text": item.strip(), "done": False})
                elif isinstance(item, dict) and item.get("text"):
                    norm_chk.append({"id": f"chk_{idx+1}", "text": str(item["text"]).strip(), "done": False})
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with DB_LOCK:
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO recurring_rules (
                        title_template, category, source_channel, issuing_org, priority,
                        freq_type, freq_value, deadline_offset_days, summary, checklist_json,
                        enabled, last_spawned_period, last_spawned_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', '', ?)
                    """,
                    (
                        title_tpl,
                        sanitize_filename(body.get("category") or "01_防灾减灾"),
                        body.get("source_channel") or "上级来文",
                        body.get("issuing_org") or "",
                        body.get("priority") or "重要",
                        body.get("freq_type") or "weekly",
                        int(body.get("freq_value") or 4),
                        int(body.get("deadline_offset_days") or 1),
                        body.get("summary") or "",
                        json.dumps(norm_chk, ensure_ascii=False),
                        now_str,
                    ),
                )
                rid = cur.lastrowid
                conn.commit()
                conn.close()
            self._send_json({"ok": True, "id": rid})
            return

        m_spawn = re.match(r"^/api/recurring/(\d+)/spawn$", path)
        if m_spawn:
            rid = int(m_spawn.group(1))
            task_obj = spawn_task_from_recurring_rule(rid, force_manual=True)
            if not task_obj:
                self._send_json({"error": "规则不存在"}, status=404)
                return
            self._send_json({"ok": True, "task": task_obj})
            return

        if path == "/api/ai/summary":
            period_label = str(body.get("period_label") or "近期阶段工作").strip()
            category_filter = str(body.get("category") or "").strip()
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY id ASC").fetchall()
                docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
                t_rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
                tasks = [
                    serialize_task(conn, r, include_refs=False, preloaded_docs=docs, ws_root=ws_root)
                    for r in t_rows
                    if (not category_filter or r["category"] == category_filter)
                ]
                conn.close()

            completed = [t for t in tasks if t["status"] == "archived"]
            ongoing = [t for t in tasks if t["status"] != "archived"]
            all_drafts = []
            for t in tasks:
                for d in t["drafted_files"]:
                    all_drafts.append(f"《{d['filename']}》（对应事项：{t['title']}）")

            prompt_context = [
                f"统计周期：{period_label}",
                f"共承办各类来文与通知事项 {len(tasks)} 项（其中已办结归档 {len(completed)} 项，正在推进 {len(ongoing)} 项），累计起草报送各类材料 {len(all_drafts)} 份。",
                "\n一、已办结重点工作与公文落实情况：",
            ]
            for idx, t in enumerate(completed, 1):
                prog = t["progress_logs"][0]["text"] if t["progress_logs"] else "已按期办结"
                prompt_context.append(
                    f"{idx}. 【{t['category']}】《{t['title']}》（{t.get('doc_number') or t['source_channel']}）：{t.get('summary') or ''}。完成情况：{prog}。"
                )
            prompt_context.append("\n二、正在推进的重点工作：")
            for idx, t in enumerate(ongoing, 1):
                prog = t["progress_logs"][0]["text"] if t["progress_logs"] else "稳步推进中"
                prompt_context.append(
                    f"{idx}. 【{t['category']}】《{t['title']}》（截止：{t.get('deadline') or '常态'}）：当前进展为“{prog}”。"
                )
            prompt_context.append("\n三、期间撰写与留存的核心材料清单：")
            for d_str in all_drafts:
                prompt_context.append(f"- {d_str}")

            raw_summary_data = "\n".join(prompt_context)
            llm_text = None
            if cfg.get("ai_api_base"):
                sys_p = "你是机关单位综合文字工作专家，请根据提供的工作台办文与材料台账，撰写一份条理清晰、用词规范的阶段性工作总结（包含：一、工作总体开展情况；二、重点工作落实成效；三、下一步工作计划）。"
                llm_text = call_openai_compatible_api(cfg, sys_p, raw_summary_data)

            if not llm_text:
                llm_text = (
                    f"# {period_label}工作总结汇报（初稿）\n\n"
                    f"## 一、总体办文与工作开展情况\n"
                    f"本阶段共梳理承办上级来文及各渠道工作通知 **{len(tasks)}** 项，其中已圆满办结归档 **{len(completed)}** 项，"
                    f"正在有序推进 **{len(ongoing)}** 项；围绕防灾减灾、隐患排查及日常综合业务累计起草、报送各类方案、汇报及统计材料 **{len(all_drafts)}** 份，确保件件有着落、事事有回音。\n\n"
                    f"## 二、重点事项落实情况\n"
                    + "\n".join(
                        f"- **{t['title']}**（{t.get('doc_number') or t['source_channel']}）：{t.get('summary') or ''}。"
                        f"目前状态：{'已办结归档' if t['status'] == 'archived' else '推进中'}，"
                        f"进展记录：{t['progress_logs'][0]['text'] if t['progress_logs'] else '已落实相关材料报送'}。"
                        for t in tasks[:12]
                    )
                    + "\n\n## 三、期间起草与归档材料明细\n"
                    + ("\n".join(f"- {x}" for x in all_drafts) if all_drafts else "- 详见公文与材料库各分类目录")
                    + "\n\n## 四、下一步工作计划\n"
                    + (
                        "\n".join(
                            f"- 紧盯《{t['title']}》（截止期：{t.get('deadline') or '持续推进'}），按节点完成后续材料汇总与报送。"
                            for t in ongoing[:6]
                        )
                        if ongoing
                        else "- 持续做好上级来文跟踪督办与日常防灾减灾、安全防范材料归口整理。"
                    )
                )

            self._send_json({
                "ok": True,
                "summary_markdown": llm_text,
                "raw_context": raw_summary_data,
            })
            return

        if path == "/api/settings":
            for k in ("workspace_root", "prefix_archived_folder", "ai_api_base", "ai_model", "lan_notice", "xkl_gw_cli"):
                if k in body and body[k] is not None:
                    cfg[k] = body[k]
            for pwd_key in ("view_password", "edit_password", "ai_api_key"):
                val = str(body.get(pwd_key) or "").strip()
                if val:
                    cfg[pwd_key] = val
            save_config(cfg)
            init_db()
            self._send_json({"ok": True})
            return

        self._send_json({"error": f"未知接口: {path}"}, status=404)

    # ---------------- API PUT ----------------
    def handle_api_put(self, path: str) -> None:
        can_view, can_edit = self._check_auth()
        if not can_edit:
            self._send_json({"error": "需要开启【主理人编辑模式】"}, status=403)
            return

        body = self._read_json_body()
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if path == "/api/categories":
            old_name = sanitize_filename(body.get("old_name") or "")
            new_name = sanitize_filename(body.get("new_name") or "")
            if not old_name or not new_name or old_name == new_name:
                self._send_json({"error": "请输入有效的新分类目录名"}, status=400)
                return
            old_dir = ws_root / old_name
            new_dir = ws_root / new_name
            if not old_dir.exists():
                self._send_json({"error": "原分类目录不存在"}, status=404)
                return

            with DB_LOCK:
                conn = get_db()
                if not new_dir.exists():
                    shutil.move(str(old_dir), str(new_dir))
                else:
                    for item in old_dir.iterdir():
                        if item.name == ".gitkeep":
                            continue
                        dest_item = new_dir / item.name
                        if not dest_item.exists():
                            shutil.move(str(item), str(dest_item))
                    shutil.rmtree(old_dir, ignore_errors=True)
                ensure_category_dir(ws_root, new_name)

                t_rows = conn.execute("SELECT id, folder_rel_path FROM tasks WHERE category = ?", (old_name,)).fetchall()
                for tr in t_rows:
                    old_rel = tr["folder_rel_path"] or ""
                    new_rel = (new_name + old_rel[len(old_name):]) if old_rel.startswith(old_name + "/") else old_rel
                    conn.execute(
                        "UPDATE tasks SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?",
                        (new_name, new_rel, now_str, tr["id"]),
                    )

                d_rows = conn.execute("SELECT id, rel_path FROM documents WHERE category = ?", (old_name,)).fetchall()
                for dr in d_rows:
                    old_rel = dr["rel_path"] or ""
                    new_rel = (new_name + old_rel[len(old_name):]) if old_rel.startswith(old_name + "/") else old_rel
                    conn.execute(
                        "UPDATE documents SET category = ?, rel_path = ?, updated_at = ? WHERE id = ?",
                        (new_name, new_rel, now_str, dr["id"]),
                    )
                    _sync_fts_doc(conn, dr["id"])

                conn.execute("UPDATE recurring_rules SET category = ? WHERE category = ?", (new_name, old_name))
                conn.commit()
                conn.close()
            self._send_json({"ok": True, "categories": list_physical_categories()})
            return

        m_rec = re.match(r"^/api/recurring/(\d+)$", path)
        if m_rec:
            rid = int(m_rec.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM recurring_rules WHERE id = ?", (rid,)).fetchone()
                if not row:
                    conn.close()
                    self._send_json({"error": "规则不存在"}, status=404)
                    return
                r_curr = dict(row)
                enabled_val = 1 if body.get("enabled", bool(r_curr["enabled"])) else 0
                chk_json = r_curr["checklist_json"]
                if "checklist" in body and isinstance(body["checklist"], list):
                    norm_chk = [
                        {"id": f"chk_{i+1}", "text": (x.get("text") if isinstance(x, dict) else str(x)).strip(), "done": False}
                        for i, x in enumerate(body["checklist"])
                        if (x.get("text") if isinstance(x, dict) else str(x)).strip()
                    ]
                    chk_json = json.dumps(norm_chk, ensure_ascii=False)
                conn.execute(
                    """
                    UPDATE recurring_rules SET
                        title_template = ?,
                        category = ?,
                        issuing_org = ?,
                        freq_type = ?,
                        freq_value = ?,
                        summary = ?,
                        checklist_json = ?,
                        enabled = ?
                    WHERE id = ?
                    """,
                    (
                        body.get("title_template") or r_curr["title_template"],
                        sanitize_filename(body.get("category") or r_curr["category"]),
                        body.get("issuing_org", r_curr["issuing_org"]),
                        body.get("freq_type") or r_curr["freq_type"],
                        int(body.get("freq_value") or r_curr["freq_value"]),
                        body.get("summary", r_curr["summary"]),
                        chk_json,
                        enabled_val,
                        rid,
                    ),
                )
                conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        m_task = re.match(r"^/api/tasks/(\d+)$", path)
        if m_task:
            task_id = int(m_task.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if not row:
                    conn.close()
                    self._send_json({"error": "事项不存在"}, status=404)
                    return

                t = dict(row)
                new_title = sanitize_filename(body.get("title") if body.get("title") is not None else t["title"])
                new_cat = sanitize_filename(body.get("category") if body.get("category") is not None else t["category"])
                new_status = body.get("status") if body.get("status") is not None else t["status"]
                new_collapsed = 1 if body.get("is_collapsed", bool(t.get("is_collapsed", 0))) else 0

                completed_at = t.get("completed_at") or ""
                if new_status == "archived" and t["status"] != "archived":
                    completed_at = now_str
                elif new_status != "archived":
                    completed_at = ""

                progress_list = json.loads(t["progress_json"] or "[]")
                if body.get("append_progress"):
                    progress_list.insert(0, {
                        "time": datetime.now().strftime("%m-%d %H:%M"),
                        "text": str(body["append_progress"]).strip(),
                    })
                elif body.get("progress_logs") is not None:
                    progress_list = body["progress_logs"]

                checklist = json.loads(t["checklist_json"] or "[]")
                if body.get("toggle_check_id"):
                    cid = body["toggle_check_id"]
                    for item in checklist:
                        if item.get("id") == cid:
                            item["done"] = not bool(item.get("done"))
                elif body.get("edit_check_item") and isinstance(body["edit_check_item"], dict):
                    ec = body["edit_check_item"]
                    cid = ec.get("id")
                    new_txt = str(ec.get("text") or "").strip()
                    if cid and new_txt:
                        for item in checklist:
                            if item.get("id") == cid:
                                item["text"] = new_txt
                elif body.get("delete_check_id"):
                    dcid = body["delete_check_id"]
                    checklist = [item for item in checklist if item.get("id") != dcid]
                elif body.get("append_check_text"):
                    checklist.append({
                        "id": f"chk_{int(time.time()*1000)}",
                        "text": str(body["append_check_text"]).strip(),
                        "done": False,
                    })
                elif body.get("checklist") is not None:
                    checklist = body["checklist"]

                new_cat, new_rel_folder = _move_task_folder_to_category(
                    conn,
                    ws_root,
                    task_id,
                    new_cat,
                    new_title=new_title,
                    new_status=new_status,
                    prefix_archived=bool(cfg.get("prefix_archived_folder")),
                )

                conn.execute(
                    """
                    UPDATE tasks SET
                        title = ?,
                        source_channel = ?,
                        doc_number = ?,
                        issuing_org = ?,
                        priority = ?,
                        deadline = ?,
                        category = ?,
                        summary = ?,
                        raw_notice_text = ?,
                        checklist_json = ?,
                        progress_json = ?,
                        status = ?,
                        is_pinned = ?,
                        is_collapsed = ?,
                        completed_at = ?,
                        folder_rel_path = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_title,
                        body.get("source_channel", t["source_channel"]),
                        body.get("doc_number", t["doc_number"]),
                        body.get("issuing_org", t["issuing_org"]),
                        body.get("priority", t["priority"]),
                        body.get("deadline", t["deadline"]),
                        new_cat,
                        body.get("summary", t["summary"]),
                        body.get("raw_notice_text", t["raw_notice_text"]),
                        json.dumps(checklist, ensure_ascii=False),
                        json.dumps(progress_list, ensure_ascii=False),
                        new_status,
                        1 if body.get("is_pinned", bool(t["is_pinned"])) else 0,
                        new_collapsed,
                        completed_at,
                        new_rel_folder,
                        now_str,
                        task_id,
                    ),
                )
                write_task_readme_file(conn, task_id)
                conn.commit()
                updated_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                res_task = serialize_task(conn, updated_row, ws_root=ws_root)
                conn.close()
            self._send_json({"ok": True, "task": res_task})
            return

        m_doc = re.match(r"^/api/docs/(\d+)$", path)
        if m_doc:
            doc_id = int(m_doc.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                if not row:
                    conn.close()
                    self._send_json({"error": "文件不存在"}, status=404)
                    return

                d = dict(row)
                old_abs = ws_root / d["rel_path"]
                new_filename = sanitize_filename(body.get("filename") or d["filename"])
                new_cat = sanitize_filename(body.get("category") or d["category"])
                new_role = body.get("doc_role") or d["doc_role"]
                new_tags = body.get("tags") if body.get("tags") is not None else json.loads(d["tags_json"] or "[]")
                new_starred = 1 if body.get("is_starred", bool(d["is_starred"])) else 0
                new_pinned = 1 if body.get("is_pinned", bool(d["is_pinned"])) else 0

                if d["task_id"] is not None and new_role != d["doc_role"] and "filename" not in body:
                    stripped_name = re.sub(r"^(\[来文\]_|\[材料\]_|【来文】|【材料】)+", "", new_filename)
                    if new_role == "incoming":
                        new_filename = "[来文]_" + stripped_name
                    elif new_role == "draft":
                        new_filename = "[材料]_" + stripped_name

                # 如果文件属于某个事项且用户修改了其所属分类目录，同步将该事项文件夹迁移至新分类（修复 Bug 2）
                if d["task_id"] is not None and new_cat != d["category"]:
                    moved_cat, moved_rel_folder = _move_task_folder_to_category(
                        conn, ws_root, int(d["task_id"]), new_cat, prefix_archived=bool(cfg.get("prefix_archived_folder"))
                    )
                    conn.execute(
                        "UPDATE tasks SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?",
                        (moved_cat, moved_rel_folder, now_str, int(d["task_id"])),
                    )
                    # 重新读取更新后的文件路径
                    row_refreshed = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                    if row_refreshed:
                        d = dict(row_refreshed)
                        old_abs = ws_root / d["rel_path"]

                new_rel_path = d["rel_path"]
                if old_abs.exists():
                    if d["task_id"] is None and (new_cat != d["category"] or new_filename != d["filename"]):
                        target_dir = ensure_category_dir(ws_root, new_cat)
                        new_abs = target_dir / new_filename
                        if old_abs.resolve() != new_abs.resolve():
                            new_abs = resolve_unique_dest_path(target_dir, new_filename)
                            shutil.move(str(old_abs), str(new_abs))
                            new_filename = new_abs.name
                            new_rel_path = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                    elif d["task_id"] is not None and new_filename != d["filename"]:
                        new_abs = old_abs.parent / new_filename
                        if old_abs.resolve() != new_abs.resolve():
                            new_abs = resolve_unique_dest_path(old_abs.parent, new_filename)
                            shutil.move(str(old_abs), str(new_abs))
                            new_filename = new_abs.name
                            new_rel_path = str(new_abs.relative_to(ws_root)).replace("\\", "/")

                conn.execute(
                    """
                    UPDATE documents SET
                        filename = ?,
                        rel_path = ?,
                        category = ?,
                        doc_role = ?,
                        tags_json = ?,
                        is_starred = ?,
                        is_pinned = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_filename,
                        new_rel_path,
                        new_cat,
                        new_role,
                        json.dumps(new_tags, ensure_ascii=False),
                        new_starred,
                        new_pinned,
                        now_str,
                        doc_id,
                    ),
                )
                _sync_fts_doc(conn, doc_id)
                updated_task = None
                if d["task_id"]:
                    write_task_readme_file(conn, d["task_id"])
                    t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (d["task_id"],)).fetchone()
                    if t_row:
                        updated_task = serialize_task(conn, t_row, ws_root=ws_root)
                conn.commit()
                u_row = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE id = ?", (doc_id,)).fetchone()
                res_doc = serialize_doc_light(u_row, ws_root=ws_root)
                conn.close()
            self._send_json({"ok": True, "document": res_doc, "task": updated_task})
            return

        self._send_json({"error": f"未知接口: {path}"}, status=404)

    # ---------------- API DELETE ----------------
    def handle_api_delete(self, path: str) -> None:
        can_view, can_edit = self._check_auth()
        if not can_edit:
            self._send_json({"error": "需要开启【主理人编辑模式】"}, status=403)
            return

        parsed_url = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed_url.query)
        delete_disk = (qs.get("delete_disk") or ["0"])[0] == "1"
        ws_root = get_workspace_root()

        if path == "/api/categories":
            cat_name = sanitize_filename((qs.get("name") or [""])[0])
            if not cat_name:
                self._send_json({"error": "分类名不能为空"}, status=400)
                return
            cat_dir = ws_root / cat_name
            with DB_LOCK:
                conn = get_db()
                t_cnt = conn.execute("SELECT COUNT(*) FROM tasks WHERE category = ?", (cat_name,)).fetchone()[0]
                d_cnt = conn.execute("SELECT COUNT(*) FROM documents WHERE category = ?", (cat_name,)).fetchone()[0]
                conn.close()
            if t_cnt > 0 or d_cnt > 0:
                self._send_json({"error": f"分类【{cat_name}】下仍有 {t_cnt} 项工作和 {d_cnt} 份文件，请先点击【重命名/合并】将其归入其他目录后再删除"}, status=400)
                return
            if cat_dir.exists() and cat_dir.is_dir():
                shutil.rmtree(cat_dir, ignore_errors=True)
            self._send_json({"ok": True, "categories": list_physical_categories()})
            return

        m_rec = re.match(r"^/api/recurring/(\d+)$", path)
        if m_rec:
            rid = int(m_rec.group(1))
            with DB_LOCK:
                conn = get_db()
                conn.execute("DELETE FROM recurring_rules WHERE id = ?", (rid,))
                conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        m_task = re.match(r"^/api/tasks/(\d+)$", path)
        if m_task:
            task_id = int(m_task.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                if row:
                    if delete_disk and row["folder_rel_path"]:
                        folder_abs = ws_root / row["folder_rel_path"]
                        if folder_abs.exists() and str(folder_abs.resolve()).startswith(str(ws_root.resolve())):
                            shutil.rmtree(folder_abs, ignore_errors=True)
                        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE task_id = ?", (task_id,)).fetchall()]
                        for did in doc_ids:
                            conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (did,))
                        conn.execute("DELETE FROM documents WHERE task_id = ?", (task_id,))
                    conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
                    conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        m_doc = re.match(r"^/api/docs/(\d+)$", path)
        if m_doc:
            doc_id = int(m_doc.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                if row:
                    if delete_disk and row["rel_path"]:
                        f_abs = ws_root / row["rel_path"]
                        if f_abs.exists() and f_abs.is_file():
                            f_abs.unlink(missing_ok=True)
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                    conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
                    if row["task_id"]:
                        write_task_readme_file(conn, row["task_id"])
                    conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        self._send_json({"error": f"未知接口: {path}"}, status=404)


def run_server(port: Optional[int] = None) -> None:
    cfg = load_config()
    init_db()
    seed_demo_data_if_empty()
    sync_filesystem(force=True)
    check_and_spawn_all_recurring()

    listen_port = port or int(cfg.get("port", 8090))
    lan_ip = get_lan_ip()
    server = ThreadingHTTPServer(("0.0.0.0", listen_port), WorkbenchHTTPRequestHandler)
    print("=" * 72)
    print(f"  🚀 公文与材料智能工作台 (GW-Workbench v{VERSION}) 已启动")
    print(f"  👉 本机主理人直连 (自动免密编辑 & WPS直连): http://127.0.0.1:{listen_port}")
    print(f"  👉 局域网同事访问地址 (默认密码 {cfg.get('view_password')}): http://{lan_ip}:{listen_port}")
    print(f"  📂 本地物理资料库目录: {get_workspace_root(cfg)}")
    print(f"  🤖 原生 MCP Server 启动命令: python3 {(BASE_DIR / 'wb.py')} mcp")
    print("=" * 72)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
        server.server_close()


if __name__ == "__main__":
    import sys
    custom_port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    run_server(custom_port)
