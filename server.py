#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与材料智能工作台 (GW-Workbench v3.0.0) 核心服务端
专为银河麒麟 Kylin V10 (海光 3350) 与局域网跨平台协同设计
v3.0.0 架构升级与全量缺陷清零特性：
1. 四维政务知识资产模型：
   - 📋 工作台待办 (tasks)
   - 📦 资料存档 (archives：按标题/次聚合会议记录、培训课件、下沉照片、录音等多媒体归档包)
   - 📌 重要事项记录 (important_records：左树右读零跳转直读知识库，如高密汛情基本情况、地震应急预案，支持文本+Word/PDF/Excel内嵌直读)
   - 🗂️ 公文与材料库 (documents) + 🔁 周期性工作派生 + 📊 台账导出与总结
2. 修复 12 处深层逻辑与性能缺陷：
   - 修复 FTS5 混合词检索丢弃数字前缀（如 11.17）及 IN (...) 999 变量上限问题，AI 检索全面接入 FTS5 预筛
   - 修复 00_事项要求与进展.md 本地删空最后一条细项不生效及标题/期限未反向同步问题
   - 修复周期性规则并发重复派生问题（全程 RLock 原子锁保护）
   - 修复路径前缀穿越漏洞（统一使用 _is_safe_subpath 校验）与转待办文件冗余问题
   - 新增 /api/media/<doc_id> 支持 HTTP Range (206 Partial Content) 音视频拖动播放与 PDF/高清图片流式直读
"""

import base64
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
import tempfile
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from parser import (
    extract_document_text,
    extract_structured_preview,
    IMAGE_EXTS,
    AUDIO_EXTS,
    VIDEO_EXTS,
)
from ai_engine import (
    parse_incoming_content,
    extract_search_keywords,
    synthesize_ai_search_answer,
    call_openai_compatible_api,
    infer_doc_role,
    match_best_category,
)
from gw_formatter import build_gb9704_docx

VERSION = "3.0.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "workbench.db"
CONFIG_PATH = DATA_DIR / "config.json"
STATIC_DIR = BASE_DIR / "static"

ARCHIVE_ROOT_DIRNAME = "资料存档"
RECORD_ROOT_DIRNAME = "重要事项记录"
RESERVED_TOP_DIRS = {ARCHIVE_ROOT_DIRNAME, RECORD_ROOT_DIRNAME}

DEFAULT_CATEGORIES = [
    "01_防灾减灾",
    "02_隐患排查与安全生产",
    "03_综合汇报与总结",
    "04_上级日常通知",
    "05_历史公文归档",
]

DEFAULT_ARCHIVE_CATEGORIES = [
    "01_会议记录",
    "02_培训课件",
    "03_下沉督导照片",
    "04_应急演练档案",
    "05_宣传与专项资料",
]

DEFAULT_RECORD_CATEGORIES = [
    "01_汛情与防灾底数",
    "02_突发事件应急预案",
    "03_重点行业企业底数",
    "04_指挥体系与通讯录",
    "05_政策法规与标准",
]

DEFAULT_CONFIG = {
    "port": 8090,
    "view_password": "123456",
    "edit_password": "8888",
    "agent_token": "gw-local-agent-token",
    "workspace_root": "workspace_files",
    "default_org_name": "县应急管理局",
    "prefix_archived_folder": False,
    "ai_api_base": "",
    "ai_api_key": "",
    "ai_model": "deepseek-chat",
    "xkl_gw_cli": "",
    "fts_version": 3,
    "lan_notice": "📌 欢迎访问科室公文与材料智能工作台！现已支持【📦 资料存档】与【📌 重要事项记录】直读速查。",
}

DB_LOCK = threading.RLock()
_LAST_SYNC_TIME: float = 0.0
_FILE_STAT_SNAPSHOT: Dict[str, Tuple[float, int]] = {}

DOC_LIGHT_COLS = """
    id, task_id, archive_id, record_id, filename, rel_path, category, doc_role, file_type,
    file_size, mtime, substr(extracted_text, 1, 240) AS content_preview,
    tags_json, is_starred, is_pinned, created_at, updated_at
"""


def _is_safe_subpath(target_path: Path, base_root: Path) -> bool:
    """严格判断 target_path 是否位于 base_root 目录内部，彻底杜绝同名前缀目录穿越漏洞 (BUG-06)"""
    try:
        t_res = target_path.resolve()
        b_res = base_root.resolve()
        if hasattr(t_res, "is_relative_to"):
            return t_res.is_relative_to(b_res)
        return os.path.commonpath([str(t_res), str(b_res)]) == str(b_res)
    except Exception:
        return False


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


def ensure_category_dir(ws_root: Path, cat_name: str, scope: str = "task") -> Path:
    safe_cat = sanitize_filename(cat_name)
    if scope == "archive":
        cat_dir = ws_root / ARCHIVE_ROOT_DIRNAME / safe_cat
    elif scope == "record":
        cat_dir = ws_root / RECORD_ROOT_DIRNAME / safe_cat
    else:
        cat_dir = ws_root / safe_cat
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
    if cleaned in ("", ".", ".."):
        return "未命名"
    return cleaned[:85]


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
    为 SQLite FTS5 (unicode61) 提供零依赖中文字符二元切分 (Bigram) + 数字编号词元 + 原词混合索引流
    修复 BUG-03：确保像 11.17、2026、G204 这样的数字/日期编号与中文双字词同时被索引！
    """
    if not text:
        return ""
    tokens: List[str] = [text]
    for num_tok in re.findall(r"[A-Za-z0-9]+(?:[\.\-][A-Za-z0-9]+)*", text):
        tokens.append(num_tok)
        if "." in num_tok:
            tokens.append(num_tok.replace(".", "_"))
    for cjk_run in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
        n = len(cjk_run)
        for i in range(n - 1):
            tokens.append(cjk_run[i : i + 2])
    return " ".join(tokens)


def _build_fts_match_expr(keywords: List[str], join_op: str = "AND") -> str:
    """
    将用户输入的多关键词转化为 FTS5 MATCH 查询表达式
    修复 BUG-03：同时提取关键词中的英数编号词元（如 11.17）与中文 Bigram，不再因含中文而丢弃数字前缀！
    """
    clauses = []
    for kw in keywords:
        clean_kw = re.sub(r'["\'()*+:\^?\\]+', " ", kw).strip()
        if not clean_kw:
            continue
        cjk_runs = re.findall(r"[\u4e00-\u9fa5]{2,}", clean_kw)
        alnum_runs = re.findall(r"[A-Za-z0-9]+(?:[\.\-][A-Za-z0-9]+)*", clean_kw)
        if cjk_runs or alnum_runs:
            for ar in alnum_runs:
                if "." in ar:
                    clauses.append(f'"{ar.replace(".", "_")}"*')
                else:
                    clauses.append(f'"{ar}"*')
            for run in cjk_runs:
                for i in range(len(run) - 1):
                    bg = run[i : i + 2]
                    clauses.append(f'"{bg}"')
        else:
            # 单个汉字或特殊短词
            for part in clean_kw.split():
                if not re.match(r"^[\u4e00-\u9fa5]$", part):
                    clauses.append(f'"{part}"*')
    seen = set()
    uniq = []
    for c in clauses:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    if not uniq:
        return ""
    return f" {join_op} ".join(uniq)


def _sync_fts_doc(conn: sqlite3.Connection, doc_id: int) -> None:
    """同步单条文档记录到 SQLite FTS5 全文检索虚拟表"""
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

            CREATE TABLE IF NOT EXISTS archives (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                archive_date TEXT NOT NULL,
                category TEXT DEFAULT '01_会议记录',
                notes_md TEXT DEFAULT '',
                tags_json TEXT DEFAULT '[]',
                is_pinned INTEGER DEFAULT 0,
                folder_rel_path TEXT NOT NULL UNIQUE,
                readme_mtime REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS important_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                category TEXT DEFAULT '01_汛情与防灾底数',
                content_md TEXT DEFAULT '',
                tags_json TEXT DEFAULT '[]',
                primary_doc_id INTEGER DEFAULT NULL,
                is_pinned INTEGER DEFAULT 0,
                sort_order INTEGER DEFAULT 0,
                folder_rel_path TEXT NOT NULL UNIQUE,
                readme_mtime REAL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS custom_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope TEXT NOT NULL,
                name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                UNIQUE(scope, name)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER,
                archive_id INTEGER DEFAULT NULL,
                record_id INTEGER DEFAULT NULL,
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
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE SET NULL,
                FOREIGN KEY(archive_id) REFERENCES archives(id) ON DELETE SET NULL,
                FOREIGN KEY(record_id) REFERENCES important_records(id) ON DELETE SET NULL
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
            CREATE INDEX IF NOT EXISTS idx_archives_cat ON archives(category);
            CREATE INDEX IF NOT EXISTS idx_records_cat ON important_records(category);
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

        doc_cols = {r["name"] for r in cur.execute("PRAGMA table_info(documents)").fetchall()}
        if "archive_id" not in doc_cols:
            cur.execute("ALTER TABLE documents ADD COLUMN archive_id INTEGER DEFAULT NULL")
        if "record_id" not in doc_cols:
            cur.execute("ALTER TABLE documents ADD COLUMN record_id INTEGER DEFAULT NULL")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_archive_id ON documents(archive_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_docs_record_id ON documents(record_id)")

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
            fts_cnt = cur.execute("SELECT COUNT(*) FROM documents_fts").fetchone()[0]
            doc_rows = cur.execute("SELECT id FROM documents").fetchall()
            sample_fts = cur.execute("SELECT filename FROM documents_fts LIMIT 1").fetchone()
            needs_rebuild = (fts_cnt != len(doc_rows)) or (
                sample_fts and " " not in (sample_fts["filename"] or "") and len(sample_fts["filename"] or "") >= 4
            )
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
        ensure_category_dir(ws_root, cat, scope="task")
    for acat in DEFAULT_ARCHIVE_CATEGORIES:
        ensure_category_dir(ws_root, acat, scope="archive")
    for rcat in DEFAULT_RECORD_CATEGORIES:
        ensure_category_dir(ws_root, rcat, scope="record")


def list_physical_categories(scope: str = "task") -> List[str]:
    ws_root = get_workspace_root()
    if scope == "archive":
        base = ws_root / ARCHIVE_ROOT_DIRNAME
        base.mkdir(parents=True, exist_ok=True)
        dirs = [d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not dirs:
            for c in DEFAULT_ARCHIVE_CATEGORIES:
                ensure_category_dir(ws_root, c, scope="archive")
            dirs = list(DEFAULT_ARCHIVE_CATEGORIES)
        dirs.sort()
        return dirs
    elif scope == "record":
        base = ws_root / RECORD_ROOT_DIRNAME
        base.mkdir(parents=True, exist_ok=True)
        dirs = [d.name for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
        if not dirs:
            for c in DEFAULT_RECORD_CATEGORIES:
                ensure_category_dir(ws_root, c, scope="record")
            dirs = list(DEFAULT_RECORD_CATEGORIES)
        dirs.sort()
        return dirs
    else:
        for cat in DEFAULT_CATEGORIES:
            if not (ws_root / cat).exists():
                ensure_category_dir(ws_root, cat, scope="task")
        dirs = [
            d.name for d in ws_root.iterdir()
            if d.is_dir() and not d.name.startswith(".") and d.name not in RESERVED_TOP_DIRS
        ]
        dirs.sort()
        return dirs if dirs else list(DEFAULT_CATEGORIES)


# ==================== 物理目录说明文件双向读写引擎 ====================
def write_task_readme_file(conn: sqlite3.Connection, task_id: int) -> None:
    """在工作事项对应的本地物理文件夹中生成/更新 `00_事项要求与进展.md`"""
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


def write_archive_readme_file(conn: sqlite3.Connection, archive_id: int) -> None:
    """在【资料存档】归档包物理文件夹中生成/更新 `00_存档说明与纪要.md`"""
    row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
    if not row:
        return
    ws_root = get_workspace_root()
    folder_rel = row["folder_rel_path"]
    if not folder_rel:
        return
    folder_path = ws_root / folder_rel
    folder_path.mkdir(parents=True, exist_ok=True)

    docs = conn.execute(
        "SELECT filename, file_type, file_size, updated_at FROM documents WHERE archive_id = ? ORDER BY id ASC",
        (archive_id,),
    ).fetchall()
    tags = json.loads(row["tags_json"] or "[]")

    md_lines = [
        f"<!-- gw_archive_id: {row['id']} -->",
        f"# {row['title']}",
        "",
        f"- **存档编号**：`#A{row['id']}`",
        f"- **存档日期**：{row['archive_date'] or ''}",
        f"- **存档分类**：{row['category'] or ''}",
        f"- **标签归纳**：{'、'.join(tags) if tags else '无'}",
        f"- **更新时间**：{row['updated_at']}",
        "",
        "## 一、会议纪要 / 现场说明与备注",
        (row["notes_md"] or "").strip() or "（暂无文字说明）",
        "",
        "## 二、本归档包内多媒体与文档清单",
    ]
    if docs:
        for d in docs:
            kb = max(1, int(d["file_size"] or 0) // 1024)
            md_lines.append(f"- `{d['filename']}` （{d['file_type']}，{kb} KB）")
    else:
        md_lines.append("- （可将录音、现场照片、PPT课件或Word纪要直接放入此文件夹）")

    readme_path = folder_path / "00_存档说明与纪要.md"
    readme_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    new_mtime = round(readme_path.stat().st_mtime, 2)
    conn.execute("UPDATE archives SET readme_mtime = ? WHERE id = ?", (new_mtime, archive_id))


def write_record_readme_file(conn: sqlite3.Connection, record_id: int) -> None:
    """在【重要事项记录】物理文件夹中生成/更新 `00_重要事项内容.md`"""
    row = conn.execute("SELECT * FROM important_records WHERE id = ?", (record_id,)).fetchone()
    if not row:
        return
    ws_root = get_workspace_root()
    folder_rel = row["folder_rel_path"]
    if not folder_rel:
        return
    folder_path = ws_root / folder_rel
    folder_path.mkdir(parents=True, exist_ok=True)

    tags = json.loads(row["tags_json"] or "[]")
    md_lines = [
        f"<!-- gw_record_id: {row['id']} -->",
        f"# {row['title']}",
        "",
        f"- **记录编号**：`#R{row['id']}`",
        f"- **所属分类**：{row['category'] or ''}",
        f"- **标签归纳**：{'、'.join(tags) if tags else '无'}",
        f"- **更新时间**：{row['updated_at']}",
        "",
        "## 正文内容",
        (row["content_md"] or "").strip() or "（详见本目录下挂载的 Word / PDF / Excel 原件）",
    ]
    readme_path = folder_path / "00_重要事项内容.md"
    readme_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    new_mtime = round(readme_path.stat().st_mtime, 2)
    conn.execute("UPDATE important_records SET readme_mtime = ? WHERE id = ?", (new_mtime, record_id))


def _extract_anchor_id(readme_path: Path, tag_name: str = "gw_task_id") -> Optional[int]:
    if not readme_path.is_file():
        return None
    try:
        head = readme_path.read_text(encoding="utf-8", errors="ignore")[:300]
        m = re.search(rf"<!--\s*{tag_name}:\s*(\d+)\s*-->", head)
        if m:
            return int(m.group(1))
    except Exception:
        pass
    return None


def _parse_and_reverse_sync_readme(conn: sqlite3.Connection, task_id: int, readme_path: Path) -> bool:
    """
    修复 BUG-04：
    1. 支持在本地编辑器中修改标题、发文字号、来文单位、紧急程度、截止日期并反向写回数据库
    2. 当用户在本地删光最后一条待办细项或进展记录时，允许清空数据库列表而非保留旧数据
    """
    try:
        raw = readme_path.read_text(encoding="utf-8", errors="ignore")
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return False

        new_title = row["title"]
        m_title = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
        if m_title and m_title.group(1).strip():
            new_title = sanitize_filename(m_title.group(1).strip())

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

        new_doc_num = row["doc_number"]
        m_dn = re.search(r"\*\*发文字号\*\*[：:]\s*([^\n]+)", raw)
        if m_dn:
            val = m_dn.group(1).strip()
            new_doc_num = "" if val == "无" else val

        new_org = row["issuing_org"]
        m_org = re.search(r"\*\*来文单位\*\*[：:]\s*([^\n]+)", raw)
        if m_org:
            val = m_org.group(1).strip()
            new_org = "" if val == "无" else val

        new_prio = row["priority"]
        m_prio = re.search(r"\*\*紧急程度\*\*[：:]\s*([^\n]+)", raw)
        if m_prio and m_prio.group(1).strip() in ("普通", "重要", "紧急"):
            new_prio = m_prio.group(1).strip()

        new_dl = row["deadline"]
        m_dl = re.search(r"\*\*截止日期\*\*[：:]\s*([^\n]+)", raw)
        if m_dl:
            val = m_dl.group(1).strip()
            new_dl = "" if ("常态" in val or val == "无") else val[:10]

        new_summary = row["summary"]
        m_sum = re.search(r"## 一、核心工作要求摘要\s*\n+([\s\S]*?)(?=\n+## 二、|$)", raw)
        if m_sum:
            s_val = m_sum.group(1).strip()
            new_summary = "" if s_val == "（暂无摘要）" else s_val

        new_checklist = json.loads(row["checklist_json"] or "[]")
        m_chk = re.search(r"## 二、待办细项清单[^\n]*\n+([\s\S]*?)(?=\n+## 三、|$)", raw)
        if m_chk is not None:
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
            new_checklist = parsed_chk

        new_progress = json.loads(row["progress_json"] or "[]")
        m_prog = re.search(r"## 三、实时进展流水[^\n]*\n+([\s\S]*?)(?=\n+## 四、|$)", raw)
        if m_prog is not None:
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
            new_progress = parsed_prog

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_mtime = round(readme_path.stat().st_mtime, 2)
        comp_at = row["completed_at"] or ""
        if new_status == "archived" and row["status"] != "archived":
            comp_at = now_str

        conn.execute(
            """
            UPDATE tasks SET
                title = ?,
                status = ?,
                doc_number = ?,
                issuing_org = ?,
                priority = ?,
                deadline = ?,
                summary = ?,
                checklist_json = ?,
                progress_json = ?,
                completed_at = ?,
                readme_mtime = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                new_title,
                new_status,
                new_doc_num,
                new_org,
                new_prio,
                new_dl,
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


def _parse_and_reverse_sync_archive_readme(conn: sqlite3.Connection, archive_id: int, readme_path: Path) -> bool:
    """当用户在本地直接修改 `00_存档说明与纪要.md` 时反向同步回 archives 表"""
    try:
        raw = readme_path.read_text(encoding="utf-8", errors="ignore")
        row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
        if not row:
            return False
        new_title = row["title"]
        m_title = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
        if m_title and m_title.group(1).strip():
            new_title = sanitize_filename(m_title.group(1).strip())

        new_notes = row["notes_md"]
        m_notes = re.search(r"## 一、会议纪要[^\n]*\n+([\s\S]*?)(?=\n+## 二、|$)", raw)
        if m_notes is not None:
            val = m_notes.group(1).strip()
            new_notes = "" if val == "（暂无文字说明）" else val

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_mtime = round(readme_path.stat().st_mtime, 2)
        conn.execute(
            "UPDATE archives SET title = ?, notes_md = ?, readme_mtime = ?, updated_at = ? WHERE id = ?",
            (new_title, new_notes, new_mtime, now_str, archive_id),
        )
        return True
    except Exception:
        return False


def _parse_and_reverse_sync_record_readme(conn: sqlite3.Connection, record_id: int, readme_path: Path) -> bool:
    """当用户在本地直接修改 `00_重要事项内容.md` 时反向同步回 important_records 表"""
    try:
        raw = readme_path.read_text(encoding="utf-8", errors="ignore")
        row = conn.execute("SELECT * FROM important_records WHERE id = ?", (record_id,)).fetchone()
        if not row:
            return False
        new_title = row["title"]
        m_title = re.search(r"^#\s+(.+)$", raw, flags=re.MULTILINE)
        if m_title and m_title.strip() if isinstance(m_title, str) else (m_title and m_title.group(1).strip()):
            new_title = sanitize_filename(m_title.group(1).strip())

        new_content = row["content_md"]
        m_body = re.search(r"## 正文内容\s*\n+([\s\S]*)$", raw)
        if m_body is not None:
            val = m_body.group(1).strip()
            new_content = "" if val.startswith("（详见本目录下") else val

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        new_mtime = round(readme_path.stat().st_mtime, 2)
        conn.execute(
            "UPDATE important_records SET title = ?, content_md = ?, readme_mtime = ?, updated_at = ? WHERE id = ?",
            (new_title, new_content, new_mtime, now_str, record_id),
        )
        return True
    except Exception:
        return False


def _collect_fast_file_snapshot(ws_root: Path) -> Dict[str, Tuple[float, int]]:
    """快速采集工作区所有文件与目录的 (mtime, size) 元组"""
    snap: Dict[str, Tuple[float, int]] = {}
    try:
        for top_dir in ws_root.iterdir():
            if not top_dir.is_dir() or top_dir.name.startswith("."):
                continue
            st_c = top_dir.stat()
            snap[str(top_dir)] = (round(st_c.st_mtime, 2), 0)
            for f in top_dir.rglob("*"):
                if f.name.startswith(".") or f.name.startswith("~$") or f.name == ".gitkeep":
                    continue
                st_f = f.stat()
                snap[str(f)] = (round(st_f.st_mtime, 2), st_f.st_size)
    except Exception:
        pass
    return snap


def _upsert_synced_doc(
    conn: sqlite3.Connection,
    sub_file: Path,
    ws_root: Path,
    existing_docs: Dict[str, Dict[str, Any]],
    seen_rel_paths: set,
    cat_name: str,
    now_str: str,
    stats: Dict[str, int],
    task_id: Optional[int] = None,
    archive_id: Optional[int] = None,
    record_id: Optional[int] = None,
    forced_role: Optional[str] = None,
    auto_tags: Optional[List[str]] = None,
) -> int:
    rel_file = str(sub_file.relative_to(ws_root)).replace("\\", "/")
    seen_rel_paths.add(rel_file)
    st = sub_file.stat()
    mtime = round(st.st_mtime, 2)
    fsize = st.st_size

    if rel_file in existing_docs:
        doc_rec = existing_docs[rel_file]
        need_update = (
            abs(doc_rec["mtime"] - mtime) > 0.5
            or doc_rec["file_size"] != fsize
            or doc_rec["category"] != cat_name
            or (task_id is not None and doc_rec.get("task_id") != task_id)
            or (archive_id is not None and doc_rec.get("archive_id") != archive_id)
            or (record_id is not None and doc_rec.get("record_id") != record_id)
        )
        if need_update:
            extracted, ftype = extract_document_text(sub_file)
            conn.execute(
                """
                UPDATE documents
                SET category = ?, task_id = ?, archive_id = ?, record_id = ?,
                    file_size = ?, mtime = ?, extracted_text = ?, file_type = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    cat_name,
                    task_id if task_id is not None else doc_rec.get("task_id"),
                    archive_id if archive_id is not None else doc_rec.get("archive_id"),
                    record_id if record_id is not None else doc_rec.get("record_id"),
                    fsize,
                    mtime,
                    extracted,
                    ftype,
                    now_str,
                    doc_rec["id"],
                ),
            )
            _sync_fts_doc(conn, doc_rec["id"])
            stats["updated"] += 1
        return int(doc_rec["id"])
    else:
        fname = sub_file.name
        extracted, ftype = extract_document_text(sub_file)
        role = forced_role or infer_doc_role(fname, extracted[:300], in_task_folder=bool(task_id), cat_name=cat_name)
        tags_list = auto_tags or []
        cur = conn.execute(
            """
            INSERT INTO documents (
                task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                file_type, file_size, mtime, extracted_text,
                tags_json, is_starred, is_pinned, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                task_id,
                archive_id,
                record_id,
                fname,
                rel_file,
                cat_name,
                role,
                ftype,
                fsize,
                mtime,
                extracted,
                json.dumps(tags_list, ensure_ascii=False),
                1 if role == "template" else 0,
                now_str,
                now_str,
            ),
        )
        new_id = int(cur.lastrowid)
        _sync_fts_doc(conn, new_id)
        stats["added"] += 1
        return new_id


def sync_filesystem(force: bool = False) -> Dict[str, int]:
    """
    真·四维双向对等物理文件夹同步引擎（v3.0.0）：
    1. 同步普通业务分类下的【待办事项】与【公文与材料库】
    2. 同步 `workspace_files/资料存档/<分类>/<归档包>/` 下的多媒体归档包（支持本地直接建文件夹拖入照片/录音自动成包！）
    3. 同步 `workspace_files/重要事项记录/<分类>/<条目>/` 下的常备知识条目（支持本地直接放入 Word/PDF/Excel 自动建条目！）
    """
    global _LAST_SYNC_TIME, _FILE_STAT_SNAPSHOT
    stats = {"added": 0, "updated": 0, "removed": 0, "moved_tasks": 0, "reverse_synced_tasks": 0, "skipped": 0}
    ws_root = get_workspace_root()
    now_ts = time.time()

    current_snap = _collect_fast_file_snapshot(ws_root)
    if not force and (now_ts - _LAST_SYNC_TIME) < 6.0 and _FILE_STAT_SNAPSHOT and current_snap == _FILE_STAT_SNAPSHOT:
        stats["skipped"] = 1
        return stats

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    README_NAMES = {"00_事项要求与进展.md", "00_存档说明与纪要.md", "00_重要事项内容.md"}

    with DB_LOCK:
        conn = get_db()
        existing_docs = {
            r["rel_path"]: dict(r)
            for r in conn.execute(
                "SELECT id, task_id, archive_id, record_id, filename, rel_path, category, doc_role, file_size, mtime FROM documents"
            ).fetchall()
        }
        seen_rel_paths = set()

        # ---------- A. 同步【工作台待办】与【公文与材料库】分类目录 ----------
        tasks = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()}
        task_folder_map: Dict[str, Dict[str, Any]] = {}

        for cat_dir in sorted(ws_root.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith(".") or cat_dir.name in RESERVED_TOP_DIRS:
                continue
            cat_name = cat_dir.name
            for entry in sorted(cat_dir.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    rel_dir = str(entry.relative_to(ws_root)).replace("\\", "/")
                    readme_f = entry / "00_事项要求与进展.md"
                    anchored_tid = _extract_anchor_id(readme_f, "gw_task_id")
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
                    if not readme_f.exists() or _extract_anchor_id(readme_f, "gw_task_id") is None:
                        write_task_readme_file(conn, tid)

        for cat_dir in sorted(ws_root.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith(".") or cat_dir.name in RESERVED_TOP_DIRS:
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
                        if sub_file.name in README_NAMES:
                            continue
                        auto_tags = [entry.name] if (not task_obj and entry.name != cat_name) else []
                        _upsert_synced_doc(
                            conn, sub_file, ws_root, existing_docs, seen_rel_paths,
                            cat_name=cat_name, now_str=now_str, stats=stats,
                            task_id=task_obj["id"] if task_obj else None,
                            auto_tags=auto_tags,
                        )
                elif entry.is_file():
                    if entry.name in README_NAMES:
                        continue
                    _upsert_synced_doc(
                        conn, entry, ws_root, existing_docs, seen_rel_paths,
                        cat_name=cat_name, now_str=now_str, stats=stats,
                    )

        # ---------- B. 同步【📦 资料存档 (archives)】目录 (`workspace_files/资料存档/<分类>/<归档包>/`) ----------
        arch_root = ws_root / ARCHIVE_ROOT_DIRNAME
        if arch_root.exists() and arch_root.is_dir():
            archives_by_id = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM archives").fetchall()}
            archives_by_rel = {r["folder_rel_path"]: dict(r) for r in archives_by_id.values()}
            seen_archive_ids = set()

            for acat_dir in sorted(arch_root.iterdir()):
                if not acat_dir.is_dir() or acat_dir.name.startswith("."):
                    continue
                acat_name = acat_dir.name
                for bundle_dir in sorted(acat_dir.iterdir()):
                    if bundle_dir.name.startswith(".") or bundle_dir.name.startswith("~$") or bundle_dir.name == ".gitkeep":
                        continue
                    if bundle_dir.is_dir():
                        rel_bundle = str(bundle_dir.relative_to(ws_root)).replace("\\", "/")
                        rm_path = bundle_dir / "00_存档说明与纪要.md"
                        aid = _extract_anchor_id(rm_path, "gw_archive_id")
                        arch_obj = archives_by_id.get(aid) if aid else archives_by_rel.get(rel_bundle)

                        if arch_obj:
                            aid = int(arch_obj["id"])
                            if arch_obj["folder_rel_path"] != rel_bundle or arch_obj["category"] != acat_name:
                                conn.execute(
                                    "UPDATE archives SET folder_rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                                    (rel_bundle, acat_name, now_str, aid),
                                )
                            if rm_path.is_file():
                                disk_mtime = round(rm_path.stat().st_mtime, 2)
                                db_mtime = float(arch_obj.get("readme_mtime") or 0.0)
                                if db_mtime > 0 and abs(disk_mtime - db_mtime) > 0.5:
                                    _parse_and_reverse_sync_archive_readme(conn, aid, rm_path)
                        else:
                            # 用户在本地直接新建了子文件夹（如 11.17会议记录），自动识别并创建归档包记录！
                            folder_title = bundle_dir.name
                            m_dt = re.match(r"^(\d{4}-\d{2}-\d{2})[_\s]+(.+)$", folder_title)
                            if m_dt:
                                arch_date = m_dt.group(1)
                                clean_title = m_dt.group(2)
                            else:
                                m_short = re.match(r"^(\d{1,2})[\.\-](\d{1,2})", folder_title)
                                if m_short:
                                    arch_date = f"{datetime.now().year}-{int(m_short.group(1)):02d}-{int(m_short.group(2)):02d}"
                                else:
                                    arch_date = datetime.now().strftime("%Y-%m-%d")
                                clean_title = folder_title
                            cur_a = conn.execute(
                                """
                                INSERT INTO archives (
                                    title, archive_date, category, notes_md, tags_json,
                                    is_pinned, folder_rel_path, readme_mtime, created_at, updated_at
                                ) VALUES (?, ?, ?, '', '[]', 0, ?, 0, ?, ?)
                                """,
                                (clean_title, arch_date, acat_name, rel_bundle, now_str, now_str),
                            )
                            aid = int(cur_a.lastrowid)

                        seen_archive_ids.add(aid)
                        for sub_f in sorted(bundle_dir.rglob("*")):
                            if not sub_f.is_file() or sub_f.name.startswith(".") or sub_f.name.startswith("~$") or sub_f.name in README_NAMES:
                                continue
                            _upsert_synced_doc(
                                conn, sub_f, ws_root, existing_docs, seen_rel_paths,
                                cat_name=acat_name, now_str=now_str, stats=stats,
                                archive_id=aid, forced_role="archive",
                            )
                        if not rm_path.exists():
                            write_archive_readme_file(conn, aid)

        # ---------- C. 同步【📌 重要事项记录 (important_records)】目录 (`workspace_files/重要事项记录/<分类>/<条目>/`) ----------
        rec_root = ws_root / RECORD_ROOT_DIRNAME
        if rec_root.exists() and rec_root.is_dir():
            records_by_id = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM important_records").fetchall()}
            records_by_rel = {r["folder_rel_path"]: dict(r) for r in records_by_id.values()}

            for rcat_dir in sorted(rec_root.iterdir()):
                if not rcat_dir.is_dir() or rcat_dir.name.startswith("."):
                    continue
                rcat_name = rcat_dir.name
                for item_entry in sorted(rcat_dir.iterdir()):
                    if item_entry.name.startswith(".") or item_entry.name.startswith("~$") or item_entry.name == ".gitkeep":
                        continue
                    if item_entry.is_dir():
                        rel_rec = str(item_entry.relative_to(ws_root)).replace("\\", "/")
                        rm_path = item_entry / "00_重要事项内容.md"
                        rid = _extract_anchor_id(rm_path, "gw_record_id")
                        rec_obj = records_by_id.get(rid) if rid else records_by_rel.get(rel_rec)
                        if rec_obj:
                            rid = int(rec_obj["id"])
                            if rec_obj["folder_rel_path"] != rel_rec or rec_obj["category"] != rcat_name:
                                conn.execute(
                                    "UPDATE important_records SET folder_rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                                    (rel_rec, rcat_name, now_str, rid),
                                )
                            if rm_path.is_file():
                                disk_mtime = round(rm_path.stat().st_mtime, 2)
                                db_mtime = float(rec_obj.get("readme_mtime") or 0.0)
                                if db_mtime > 0 and abs(disk_mtime - db_mtime) > 0.5:
                                    _parse_and_reverse_sync_record_readme(conn, rid, rm_path)
                        else:
                            cur_r = conn.execute(
                                """
                                INSERT INTO important_records (
                                    title, category, content_md, tags_json, primary_doc_id,
                                    is_pinned, sort_order, folder_rel_path, readme_mtime, created_at, updated_at
                                ) VALUES (?, ?, '', '[]', NULL, 0, 0, ?, 0, ?, ?)
                                """,
                                (item_entry.name, rcat_name, rel_rec, now_str, now_str),
                            )
                            rid = int(cur_r.lastrowid)

                        first_doc_id = None
                        for sub_f in sorted(item_entry.rglob("*")):
                            if not sub_f.is_file() or sub_f.name.startswith(".") or sub_f.name.startswith("~$") or sub_f.name in README_NAMES:
                                continue
                            did = _upsert_synced_doc(
                                conn, sub_f, ws_root, existing_docs, seen_rel_paths,
                                cat_name=rcat_name, now_str=now_str, stats=stats,
                                record_id=rid, forced_role="record",
                            )
                            if first_doc_id is None:
                                first_doc_id = did
                        if first_doc_id is not None:
                            conn.execute(
                                "UPDATE important_records SET primary_doc_id = COALESCE(primary_doc_id, ?) WHERE id = ?",
                                (first_doc_id, rid),
                            )
                        if not rm_path.exists():
                            write_record_readme_file(conn, rid)

        # 清理磁盘上已移除的文件索引
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

        target_cat_dir = ensure_category_dir(ws_root, cat_use, scope="task")
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
        if d.get("doc_role") in ("draft", "template", "record"):
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


def _get_media_kind(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext == ".pdf":
        return "pdf"
    if ext in (".xlsx", ".xls", ".csv", ".et"):
        return "spreadsheet"
    if ext in (".pptx", ".ppt", ".dps"):
        return "presentation"
    if ext in (".docx", ".doc", ".wps", ".ofd"):
        return "document"
    return "text"


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
    d["media_kind"] = _get_media_kind(d.get("filename", ""))
    d["media_url"] = f"/api/media/{d['id']}"
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
        if include_refs and t.get("status") != "archived":
            all_rows = conn.execute(
                f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC LIMIT 200"
            ).fetchall()
            all_light_docs = [serialize_doc_light(r, ws_root=root) for r in all_rows]
        else:
            all_light_docs = []

    t["incoming_files"] = [d for d in task_docs if d["doc_role"] == "incoming"]
    t["drafted_files"] = [d for d in task_docs if d["doc_role"] != "incoming"]
    t["folder_abs_path"] = str((root / t["folder_rel_path"]).resolve()) if t.get("folder_rel_path") else ""

    # 性能优化：仅对非归档事项计算相似旧稿，避免历史事项拖慢 Bootstrap
    if include_refs and t.get("status") != "archived":
        t["reference_docs"] = find_similar_library_docs_fast(
            all_light_docs, t["title"], t.get("summary", ""), exclude_task_id=t["id"], limit=4
        )
    else:
        t["reference_docs"] = []
    return t


def serialize_archive(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    preloaded_docs: Optional[List[Dict[str, Any]]] = None,
    ws_root: Optional[Path] = None,
) -> Dict[str, Any]:
    a = dict(row)
    a.pop("readme_mtime", None)
    a["tags"] = json.loads(a.pop("tags_json", "[]") or "[]")
    a["is_pinned"] = bool(a.get("is_pinned", 0))
    root = ws_root or get_workspace_root()
    if preloaded_docs is not None:
        files = [d for d in preloaded_docs if d.get("archive_id") == a["id"]]
    else:
        d_rows = conn.execute(
            f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE archive_id = ? ORDER BY id ASC",
            (a["id"],),
        ).fetchall()
        files = [serialize_doc_light(r, ws_root=root) for r in d_rows]
    a["files"] = files
    a["images"] = [f for f in files if f.get("media_kind") == "image"]
    a["audios"] = [f for f in files if f.get("media_kind") == "audio"]
    a["videos"] = [f for f in files if f.get("media_kind") == "video"]
    a["other_docs"] = [f for f in files if f.get("media_kind") not in ("image", "audio", "video")]
    a["folder_abs_path"] = str((root / a["folder_rel_path"]).resolve()) if a.get("folder_rel_path") else ""
    return a


def serialize_record(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    preloaded_docs: Optional[List[Dict[str, Any]]] = None,
    ws_root: Optional[Path] = None,
    include_structured_preview: bool = False,
) -> Dict[str, Any]:
    rec = dict(row)
    rec.pop("readme_mtime", None)
    rec["tags"] = json.loads(rec.pop("tags_json", "[]") or "[]")
    rec["is_pinned"] = bool(rec.get("is_pinned", 0))
    root = ws_root or get_workspace_root()
    if preloaded_docs is not None:
        files = [d for d in preloaded_docs if d.get("record_id") == rec["id"]]
    else:
        d_rows = conn.execute(
            f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE record_id = ? ORDER BY id ASC",
            (rec["id"],),
        ).fetchall()
        files = [serialize_doc_light(r, ws_root=root) for r in d_rows]

    if include_structured_preview:
        enriched_files = []
        for f in files:
            f_copy = dict(f)
            f_abs = root / f_copy["rel_path"]
            f_copy["structured_preview"] = extract_structured_preview(f_abs, doc_id=f_copy["id"])
            full_row = conn.execute("SELECT extracted_text FROM documents WHERE id = ?", (f_copy["id"],)).fetchone()
            f_copy["extracted_text"] = (full_row["extracted_text"] or "") if full_row else ""
            enriched_files.append(f_copy)
        rec["files"] = enriched_files
    else:
        rec["files"] = files

    rec["folder_abs_path"] = str((root / rec["folder_rel_path"]).resolve()) if rec.get("folder_rel_path") else ""
    return rec


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
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    四维全库检索：同时返回 (matched_tasks, matched_docs, matched_archives, matched_records)
    修复 BUG-03：使用子查询 `id IN (SELECT doc_id FROM documents_fts WHERE documents_fts MATCH ?)` 消除 999 参数上限！
    """
    q_clean = (query or "").strip()
    keywords = [k for k in re.split(r"\s+", q_clean) if k] if q_clean else []
    ws_root = get_workspace_root()

    with DB_LOCK:
        conn = get_db()
        all_doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
        preloaded_light_docs = [serialize_doc_light(r, ws_root=ws_root) for r in all_doc_rows]

        # 1. 检索工作事项 (tasks)
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

        # 2. 检索公文与材料文件 (documents)
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
            match_expr = _build_fts_match_expr(keywords, join_op="AND")
            sql = (
                "SELECT id, task_id, archive_id, record_id, filename, rel_path, category, doc_role, "
                "file_type, file_size, mtime, extracted_text, tags_json, is_starred, is_pinned, created_at, updated_at "
                "FROM documents WHERE 1=1"
            )
            params: List[Any] = []
            if match_expr:
                sql += " AND id IN (SELECT doc_id FROM documents_fts WHERE documents_fts MATCH ?)"
                params.append(match_expr)
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
            try:
                rows_found = conn.execute(sql, params).fetchall()
            except Exception:
                # 若 FTS 语法极端特殊回退纯 LIKE
                sql_fb = (
                    "SELECT id, task_id, archive_id, record_id, filename, rel_path, category, doc_role, "
                    "file_type, file_size, mtime, extracted_text, tags_json, is_starred, is_pinned, created_at, updated_at "
                    "FROM documents WHERE 1=1"
                )
                params_fb: List[Any] = []
                for kw in keywords:
                    sql_fb += " AND (filename LIKE ? OR tags_json LIKE ? OR category LIKE ? OR extracted_text LIKE ?)"
                    lp = f"%{kw}%"
                    params_fb.extend([lp, lp, lp, lp])
                rows_found = conn.execute(sql_fb, params_fb).fetchall()

            for r in rows_found:
                snip = build_highlight_snippet(r["extracted_text"] or r["filename"], keywords)
                matched_docs.append(serialize_doc_light(r, snippet=snip, ws_root=ws_root))

        matched_doc_archive_ids = {d.get("archive_id") for d in matched_docs if d.get("archive_id")}
        matched_doc_record_ids = {d.get("record_id") for d in matched_docs if d.get("record_id")}

        # 3. 检索【📦 资料存档】归档包 (archives)
        arch_rows = conn.execute("SELECT * FROM archives ORDER BY is_pinned DESC, archive_date DESC, id DESC").fetchall()
        matched_archives = []
        for ar in arch_rows:
            if category and ar["category"] != category:
                continue
            a_obj = serialize_archive(conn, ar, preloaded_docs=preloaded_light_docs, ws_root=ws_root)
            if not keywords:
                matched_archives.append(a_obj)
                continue
            file_names_str = " ".join(f["filename"] for f in a_obj["files"])
            if scope == "title":
                hay = f"{ar['title']} {ar['archive_date']} {ar['category']} {file_names_str}"
            else:
                hay = f"{ar['title']} {ar['archive_date']} {ar['category']} {ar['notes_md']} {ar['tags_json']} {file_names_str}"
            if all(kw.lower() in hay.lower() for kw in keywords) or (ar["id"] in matched_doc_archive_ids):
                a_obj["match_snippet"] = build_highlight_snippet(hay, keywords)
                matched_archives.append(a_obj)

        # 4. 检索【📌 重要事项记录】知识库 (important_records)
        rec_rows = conn.execute("SELECT * FROM important_records ORDER BY is_pinned DESC, sort_order ASC, id DESC").fetchall()
        matched_records = []
        for rr in rec_rows:
            if category and rr["category"] != category:
                continue
            r_obj = serialize_record(conn, rr, preloaded_docs=preloaded_light_docs, ws_root=ws_root)
            if not keywords:
                matched_records.append(r_obj)
                continue
            file_names_str = " ".join(f["filename"] for f in r_obj["files"])
            if scope == "title":
                hay = f"{rr['title']} {rr['category']} {file_names_str}"
            else:
                hay = f"{rr['title']} {rr['category']} {rr['content_md']} {rr['tags_json']} {file_names_str}"
            if all(kw.lower() in hay.lower() for kw in keywords) or (rr["id"] in matched_doc_record_ids):
                r_obj["match_snippet"] = build_highlight_snippet(hay, keywords)
                matched_records.append(r_obj)

        conn.close()
    return matched_tasks, matched_docs, matched_archives, matched_records


def _decode_file_item_bytes(fitem: Dict[str, Any]) -> Tuple[str, bytes, str]:
    fname = sanitize_filename(fitem.get("filename") or "附件.txt")
    b64_data = fitem.get("content_base64") or ""
    raw_bytes = b""
    if b64_data:
        if "," in b64_data and b64_data.startswith("data:"):
            b64_data = b64_data.split(",", 1)[1]
        raw_bytes = base64.b64decode(b64_data)
    elif fitem.get("local_source_path"):
        src_p = Path(fitem["local_source_path"]).expanduser()
        if src_p.exists() and src_p.is_file():
            raw_bytes = src_p.read_bytes()
            fname = sanitize_filename(src_p.name)
    role = fitem.get("doc_role") or "incoming"
    return fname, raw_bytes, role


def create_task_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    cfg = load_config()
    ws_root = get_workspace_root(cfg)
    categories = list_physical_categories(scope="task")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_prefix = datetime.now().strftime("%Y-%m-%d")

    raw_text = (payload.get("raw_notice_text") or payload.get("text") or "").strip()
    uploaded_files: List[Dict[str, Any]] = payload.get("files") or []
    auto_ai = bool(payload.get("auto_ai", True))
    reuse_doc_id = payload.get("reuse_doc_id")

    combined_text_parts = []
    if raw_text:
        combined_text_parts.append(raw_text)

    first_filename = ""
    extracted_file_blobs = []
    for fitem in uploaded_files:
        fname, raw_bytes, role = _decode_file_item_bytes(fitem)
        if not first_filename:
            first_filename = fname
        extracted_file_blobs.append((fname, raw_bytes, role))

    if extracted_file_blobs and auto_ai:
        for fname, raw_bytes, role in extracted_file_blobs:
            if raw_bytes and role == "incoming":
                suffix = Path(fname).suffix
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
                    tf.write(raw_bytes)
                    tmp_p = Path(tf.name)
                try:
                    ext_txt, _ = extract_document_text(tmp_p)
                    if ext_txt and not ext_txt.startswith("【影像图片文件："):
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

    cat_dir = ensure_category_dir(ws_root, category, scope="task")
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
        task_id = int(cur.lastrowid)

        # 修复 BUG-09：如果是从公文库一键“转待办”，直接物理移动并复用原文档记录，避免冗余双份
        if reuse_doc_id:
            old_doc = conn.execute("SELECT * FROM documents WHERE id = ?", (int(reuse_doc_id),)).fetchone()
            if old_doc:
                old_abs = ws_root / old_doc["rel_path"]
                fname = old_doc["filename"]
                save_name = fname if fname.startswith(("[来文]", "【来文】")) else f"[来文]_{fname}"
                dest_path = resolve_unique_dest_path(task_folder, save_name)
                if old_abs.is_file():
                    shutil.move(str(old_abs), str(dest_path))
                    st = dest_path.stat()
                    rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
                    conn.execute(
                        """
                        UPDATE documents
                        SET task_id = ?, filename = ?, rel_path = ?, category = ?, doc_role = 'incoming', mtime = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (task_id, dest_path.name, rel_file, category, round(st.st_mtime, 2), now_str, int(reuse_doc_id)),
                    )
                    _sync_fts_doc(conn, int(reuse_doc_id))
                    extracted_file_blobs = []

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
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at
                ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
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
            _sync_fts_doc(conn, int(d_cur.lastrowid))

        write_task_readme_file(conn, task_id)
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = serialize_task(conn, row, ws_root=ws_root)
        conn.close()
    return result


# ==================== 新增栏目一：📦 资料存档 (archives) 核心 CRUD ====================
def create_archive_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    创建按次/按主题聚合的【资料存档包】（如 11.17会议记录、11.18xx事项下沉照片）
    """
    cfg = load_config()
    ws_root = get_workspace_root(cfg)
    avail_cats = list_physical_categories(scope="archive")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    raw_title = (payload.get("title") or "").strip()
    uploaded_files: List[Dict[str, Any]] = payload.get("files") or []
    if not raw_title:
        if uploaded_files:
            raw_title = f"{datetime.now().strftime('%m.%d')}_{Path(uploaded_files[0].get('filename') or '资料存档').stem}"
        else:
            raw_title = f"{datetime.now().strftime('%m.%d')}资料存档"
    title = sanitize_filename(raw_title)

    # 智能提取或补齐归档日期
    archive_date = (payload.get("archive_date") or "").strip()
    if not archive_date:
        m_full = re.search(r"(\d{4})[\-\./年](\d{1,2})[\-\./月](\d{1,2})", title)
        m_short = re.match(r"^(\d{1,2})[\.\-](\d{1,2})", title)
        if m_full:
            archive_date = f"{int(m_full.group(1)):04d}-{int(m_full.group(2)):02d}-{int(m_full.group(3)):02d}"
        elif m_short:
            archive_date = f"{datetime.now().year}-{int(m_short.group(1)):02d}-{int(m_short.group(2)):02d}"
        else:
            archive_date = datetime.now().strftime("%Y-%m-%d")

    category = sanitize_filename(payload.get("category") or "")
    if not category:
        # 根据标题关键词自动推荐分类
        if any(k in title for k in ("会议", "纪要", "会商", "例会", "座谈")):
            category = "01_会议记录"
        elif any(k in title for k in ("培训", "课件", "宣讲", "学习", "讲义")):
            category = "02_培训课件"
        elif any(k in title for k in ("下沉", "照片", "督导", "检查", "巡查", "现场")):
            category = "03_下沉督导照片"
        elif any(k in title for k in ("演练", "拉练", "实操")):
            category = "04_应急演练档案"
        else:
            category = avail_cats[0] if avail_cats else "01_会议记录"

    notes_md = (payload.get("notes_md") or payload.get("summary") or "").strip()
    raw_tags = payload.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in re.split(r"[\s,，、;；]+", raw_tags) if t.strip()]
    else:
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]

    cat_dir = ensure_category_dir(ws_root, category, scope="archive")
    folder_base = sanitize_filename(f"{archive_date}_{title}" if not re.match(r"^\d{4}-\d{2}-\d{2}", title) else title)
    bundle_dir = cat_dir / folder_base
    counter = 2
    while bundle_dir.exists():
        bundle_dir = cat_dir / f"{folder_base}_{counter}"
        counter += 1
    bundle_dir.mkdir(parents=True, exist_ok=True)
    folder_rel_path = str(bundle_dir.relative_to(ws_root)).replace("\\", "/")

    with DB_LOCK:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO archives (
                title, archive_date, category, notes_md, tags_json,
                is_pinned, folder_rel_path, readme_mtime, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            """,
            (
                title,
                archive_date,
                category,
                notes_md,
                json.dumps(tags, ensure_ascii=False),
                1 if payload.get("is_pinned") else 0,
                folder_rel_path,
                now_str,
                now_str,
            ),
        )
        archive_id = int(cur.lastrowid)

        for fitem in uploaded_files:
            fname, raw_bytes, _ = _decode_file_item_bytes(fitem)
            dest_path = resolve_unique_dest_path(bundle_dir, fname, raw_bytes)
            dest_path.write_bytes(raw_bytes)
            st = dest_path.stat()
            ext_text, ftype = extract_document_text(dest_path)
            rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
            d_cur = cur.execute(
                """
                INSERT INTO documents (
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at
                ) VALUES (NULL, ?, NULL, ?, ?, ?, 'archive', ?, ?, ?, ?, ?, 0, 0, ?, ?)
                """,
                (
                    archive_id,
                    dest_path.name,
                    rel_file,
                    category,
                    ftype,
                    st.st_size,
                    round(st.st_mtime, 2),
                    ext_text,
                    json.dumps(tags, ensure_ascii=False),
                    now_str,
                    now_str,
                ),
            )
            _sync_fts_doc(conn, int(d_cur.lastrowid))

        write_archive_readme_file(conn, archive_id)
        conn.commit()
        row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
        res = serialize_archive(conn, row, ws_root=ws_root)
        conn.close()
    return res


def append_files_to_archive(archive_id: int, files_list: List[Dict[str, Any]], notes_append: str = "") -> Optional[Dict[str, Any]]:
    cfg = load_config()
    ws_root = get_workspace_root(cfg)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with DB_LOCK:
        conn = get_db()
        a_row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
        if not a_row:
            conn.close()
            return None
        bundle_dir = ws_root / a_row["folder_rel_path"]
        bundle_dir.mkdir(parents=True, exist_ok=True)
        tags = json.loads(a_row["tags_json"] or "[]")

        if notes_append.strip():
            old_notes = (a_row["notes_md"] or "").strip()
            new_notes = (old_notes + "\n\n" + notes_append.strip()).strip() if old_notes else notes_append.strip()
            conn.execute("UPDATE archives SET notes_md = ?, updated_at = ? WHERE id = ?", (new_notes, now_str, archive_id))

        for fitem in files_list:
            fname, raw_bytes, _ = _decode_file_item_bytes(fitem)
            dest_path = resolve_unique_dest_path(bundle_dir, fname, raw_bytes)
            dest_path.write_bytes(raw_bytes)
            st = dest_path.stat()
            ext_text, ftype = extract_document_text(dest_path)
            rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
            d_cur = conn.execute(
                """
                INSERT INTO documents (
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at
                ) VALUES (NULL, ?, NULL, ?, ?, ?, 'archive', ?, ?, ?, ?, ?, 0, 0, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    extracted_text=excluded.extracted_text,
                    updated_at=excluded.updated_at
                """,
                (
                    archive_id,
                    dest_path.name,
                    rel_file,
                    a_row["category"],
                    ftype,
                    st.st_size,
                    round(st.st_mtime, 2),
                    ext_text,
                    json.dumps(tags, ensure_ascii=False),
                    now_str,
                    now_str,
                ),
            )
            r_doc = conn.execute("SELECT id FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
            if r_doc:
                _sync_fts_doc(conn, int(r_doc["id"]))

        conn.execute("UPDATE archives SET updated_at = ? WHERE id = ?", (now_str, archive_id))
        write_archive_readme_file(conn, archive_id)
        conn.commit()
        u_row = conn.execute("SELECT * FROM archives WHERE id = ?", (archive_id,)).fetchone()
        res = serialize_archive(conn, u_row, ws_root=ws_root)
        conn.close()
    return res


# ==================== 新增栏目二：📌 重要事项记录 (important_records) 核心 CRUD ====================
def upsert_important_record(payload: Dict[str, Any], record_id: Optional[int] = None) -> Dict[str, Any]:
    """
    创建或更新【重要事项记录】（如 高密汛情基本情况、地震应急预案），支持直读正文 + Word/PDF/Excel 原件挂载
    """
    cfg = load_config()
    ws_root = get_workspace_root(cfg)
    avail_cats = list_physical_categories(scope="record")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    uploaded_files: List[Dict[str, Any]] = payload.get("files") or []
    raw_title = (payload.get("title") or "").strip()
    if not raw_title and uploaded_files:
        raw_title = Path(uploaded_files[0].get("filename") or "未命名重要事项").stem
    title = sanitize_filename(raw_title or "未命名重要事项")

    category = sanitize_filename(payload.get("category") or "")
    if not category:
        if any(k in title for k in ("汛", "洪", "雨", "旱", "水库", "河", "防灾", "灾情")):
            category = "01_汛情与防灾底数"
        elif any(k in title for k in ("预案", "地震", "响应", "突发", "处置", "办法")):
            category = "02_突发事件应急预案"
        elif any(k in title for k in ("企业", "危化", "矿山", "工贸", "清单", "底数")):
            category = "03_重点行业企业底数"
        elif any(k in title for k in ("通讯录", "电话", "指挥部", "职责", "联络")):
            category = "04_指挥体系与通讯录"
        else:
            category = avail_cats[0] if avail_cats else "01_汛情与防灾底数"

    content_md = payload.get("content_md") if payload.get("content_md") is not None else payload.get("content")
    raw_tags = payload.get("tags") or []
    if isinstance(raw_tags, str):
        tags = [t.strip() for t in re.split(r"[\s,，、;；]+", raw_tags) if t.strip()]
    else:
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]

    with DB_LOCK:
        conn = get_db()
        existing_row = None
        if record_id:
            existing_row = conn.execute("SELECT * FROM important_records WHERE id = ?", (int(record_id),)).fetchone()
        elif payload.get("upsert_by_title"):
            existing_row = conn.execute("SELECT * FROM important_records WHERE title = ?", (title,)).fetchone()

        if existing_row:
            rid = int(existing_row["id"])
            old_cat = existing_row["category"]
            old_title = existing_row["title"]
            old_rel = existing_row["folder_rel_path"]
            new_cat = sanitize_filename(payload.get("category") or old_cat)
            new_title = sanitize_filename(payload.get("title") or old_title)
            new_content = content_md if content_md is not None else existing_row["content_md"]
            new_tags = tags if "tags" in payload else json.loads(existing_row["tags_json"] or "[]")
            new_pinned = 1 if payload.get("is_pinned", bool(existing_row["is_pinned"])) else 0

            new_rel = old_rel
            old_abs = ws_root / old_rel
            target_cat_dir = ensure_category_dir(ws_root, new_cat, scope="record")
            new_abs = target_cat_dir / new_title
            if old_abs.exists() and old_abs.resolve() != new_abs.resolve():
                counter = 2
                while new_abs.exists() and new_abs.resolve() != old_abs.resolve():
                    new_abs = target_cat_dir / f"{new_title}_{counter}"
                    counter += 1
                shutil.move(str(old_abs), str(new_abs))
                new_rel = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                for dr in conn.execute("SELECT id, rel_path FROM documents WHERE record_id = ?", (rid,)).fetchall():
                    if dr["rel_path"].startswith(old_rel + "/"):
                        u_rel = new_rel + dr["rel_path"][len(old_rel):]
                        conn.execute(
                            "UPDATE documents SET rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                            (u_rel, new_cat, now_str, dr["id"]),
                        )
                        _sync_fts_doc(conn, int(dr["id"]))

            conn.execute(
                """
                UPDATE important_records SET
                    title = ?, category = ?, content_md = ?, tags_json = ?,
                    is_pinned = ?, folder_rel_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    new_title,
                    new_cat,
                    new_content,
                    json.dumps(new_tags, ensure_ascii=False),
                    new_pinned,
                    new_rel,
                    now_str,
                    rid,
                ),
            )
            rec_folder = ws_root / new_rel
            rec_category = new_cat
        else:
            cat_dir = ensure_category_dir(ws_root, category, scope="record")
            rec_folder = cat_dir / title
            counter = 2
            while rec_folder.exists():
                rec_folder = cat_dir / f"{title}_{counter}"
                counter += 1
            rec_folder.mkdir(parents=True, exist_ok=True)
            folder_rel_path = str(rec_folder.relative_to(ws_root)).replace("\\", "/")
            cur = conn.execute(
                """
                INSERT INTO important_records (
                    title, category, content_md, tags_json, primary_doc_id,
                    is_pinned, sort_order, folder_rel_path, readme_mtime, created_at, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, 0, ?, 0, ?, ?)
                """,
                (
                    title,
                    category,
                    content_md or "",
                    json.dumps(tags, ensure_ascii=False),
                    1 if payload.get("is_pinned") else 0,
                    folder_rel_path,
                    now_str,
                    now_str,
                ),
            )
            rid = int(cur.lastrowid)
            rec_category = category

        first_new_doc_id = None
        extracted_texts_for_empty_md = []
        for fitem in uploaded_files:
            fname, raw_bytes, _ = _decode_file_item_bytes(fitem)
            dest_path = resolve_unique_dest_path(rec_folder, fname, raw_bytes)
            dest_path.write_bytes(raw_bytes)
            st = dest_path.stat()
            ext_text, ftype = extract_document_text(dest_path)
            if ext_text and not ext_text.startswith("["):
                extracted_texts_for_empty_md.append(ext_text)
            rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
            conn.execute(
                """
                INSERT INTO documents (
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at
                ) VALUES (NULL, NULL, ?, ?, ?, ?, 'record', ?, ?, ?, ?, ?, 1, 0, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    extracted_text=excluded.extracted_text,
                    updated_at=excluded.updated_at
                """,
                (
                    rid,
                    dest_path.name,
                    rel_file,
                    rec_category,
                    ftype,
                    st.st_size,
                    round(st.st_mtime, 2),
                    ext_text,
                    json.dumps(tags, ensure_ascii=False),
                    now_str,
                    now_str,
                ),
            )
            r_doc = conn.execute("SELECT id FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
            if r_doc:
                did = int(r_doc["id"])
                _sync_fts_doc(conn, did)
                if first_new_doc_id is None:
                    first_new_doc_id = did

        if first_new_doc_id is not None:
            conn.execute("UPDATE important_records SET primary_doc_id = ? WHERE id = ?", (first_new_doc_id, rid))

        # 如果用户只上传了 Word/PDF/Excel 而未手写 content_md，自动将提取的文本摘要填入以便全文搜索
        curr_rec = conn.execute("SELECT content_md FROM important_records WHERE id = ?", (rid,)).fetchone()
        if curr_rec and not (curr_rec["content_md"] or "").strip() and extracted_texts_for_empty_md:
            conn.execute(
                "UPDATE important_records SET content_md = ? WHERE id = ?",
                ("\n\n".join(extracted_texts_for_empty_md)[:8000], rid),
            )

        write_record_readme_file(conn, rid)
        conn.commit()
        final_row = conn.execute("SELECT * FROM important_records WHERE id = ?", (rid,)).fetchone()
        res = serialize_record(conn, final_row, ws_root=ws_root, include_structured_preview=True)
        conn.close()
    return res


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
    """
    修复 BUG-05：全程持有可重入锁 DB_LOCK，彻底杜绝并发请求下重复派生双份事项与文件夹！
    """
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
        # 先原子更新 last_spawned_period 占位，防止重入
        conn.execute(
            "UPDATE recurring_rules SET last_spawned_period = ?, last_spawned_at = ? WHERE id = ?",
            (actual_pkey, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), rule_id),
        )
        conn.commit()

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


def _build_minimal_xlsx_bytes(sheet_name: str, rows: List[List[str]]) -> bytes:
    """零依赖生成标准 OpenXML .xlsx 文件字节流，用于初始化预置重要底数表"""
    buf = io.BytesIO()
    shared_strs: List[str] = []
    str_map: Dict[str, int] = {}

    def get_ss_idx(s: str) -> int:
        if s not in str_map:
            str_map[s] = len(shared_strs)
            shared_strs.append(s)
        return str_map[s]

    row_xmls = []
    for r_i, row in enumerate(rows, 1):
        c_xmls = []
        for c_i, val in enumerate(row):
            col_letter = chr(ord("A") + c_i)
            ref = f"{col_letter}{r_i}"
            s_val = str(val or "")
            if re.match(r"^\d+(\.\d+)?$", s_val):
                c_xmls.append(f'<c r="{ref}"><v>{s_val}</v></c>')
            else:
                idx = get_ss_idx(s_val)
                c_xmls.append(f'<c r="{ref}" t="s"><v>{idx}</v></c>')
        row_xmls.append(f'<row r="{r_i}">{"".join(c_xmls)}</row>')

    import html as _html
    ss_items = "".join(f"<si><t>{_html.escape(s)}</t></si>" for s in shared_strs)
    ss_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="{len(shared_strs)}" uniqueCount="{len(shared_strs)}">{ss_items}</sst>'
    ws_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{"".join(row_xmls)}</sheetData></worksheet>'
    wb_xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="{_html.escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>'
    wb_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>'
    root_rels = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'
    ctypes = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>'

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ctypes.encode("utf-8"))
        zf.writestr("_rels/.rels", root_rels.encode("utf-8"))
        zf.writestr("xl/workbook.xml", wb_xml.encode("utf-8"))
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels.encode("utf-8"))
        zf.writestr("xl/worksheets/sheet1.xml", ws_xml.encode("utf-8"))
        zf.writestr("xl/sharedStrings.xml", ss_xml.encode("utf-8"))
    return buf.getvalue()


def _build_minimal_pptx_bytes(slides_content: List[Tuple[str, List[str]]]) -> bytes:
    """零依赖生成标准 OpenXML .pptx 课件文件字节流"""
    import html as _html
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        ctypes_overrides = []
        for idx, (stitle, sbullets) in enumerate(slides_content, 1):
            p_nodes = [f'<a:p><a:r><a:t>{_html.escape(stitle)}</a:t></a:r></a:p>']
            for b in sbullets:
                p_nodes.append(f'<a:p><a:r><a:t>{_html.escape(b)}</a:t></a:r></a:p>')
            slide_xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                '<p:cSld><p:spTree><p:sp><p:txBody>'
                + "".join(p_nodes)
                + '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>'
            )
            zf.writestr(f"ppt/slides/slide{idx}.xml", slide_xml.encode("utf-8"))
            ctypes_overrides.append(
                f'<Override PartName="/ppt/slides/slide{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            )
        ctypes = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="xml" ContentType="application/xml"/>'
            + "".join(ctypes_overrides)
            + '</Types>'
        )
        zf.writestr("[Content_Types].xml", ctypes.encode("utf-8"))
    return buf.getvalue()


def seed_demo_data_if_empty() -> None:
    with DB_LOCK:
        conn = get_db()
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

        task_cnt = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        arch_cnt = conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0]
        imp_cnt = conn.execute("SELECT COUNT(*) FROM important_records").fetchone()[0]
        conn.commit()
        conn.close()

    # 1. 初始化示例【📦 资料存档】归档包（若为空）
    if arch_cnt == 0:
        pptx_bytes = _build_minimal_pptx_bytes([
            ("冬春防灾减灾与应急信息报送业务培训课件", ["主讲单位：综合防灾减灾科", "时间：2026年11月17日", "参训范围：各镇街、开发区应急办业务骨干"]),
            ("第一部分：灾情初报与续报时限要求", ["1. 突发灾情发生后 1 小时内完成初报", "2. 严禁迟报、谎报、瞒报、漏报", "3. 现场照片需附带时间地点水印与经纬度底数"]),
            ("第二部分：冬春救助台账规范化管理", ["1. 严格落实“户报、村评、乡审、县定”程序", "2. 棉大衣、棉被等物资出入库账物相符率必须达到 100%"]),
        ])
        create_archive_record({
            "title": "11.17冬春防灾减灾会商暨业务培训会议记录",
            "archive_date": "2026-11-17",
            "category": "01_会议记录",
            "notes_md": (
                "### 会议核心纪要\n"
                "- **会议时间**：2026年11月17日 14:30\n"
                "- **参会范围**：局分管领导、防灾减灾科、气象局、水利局及各镇街应急办主任\n"
                "- **议定事项**：\n"
                "  1. 各镇街于 11月20日前完成辖区低温雨雪冰冻易受灾点位与独居老人温暖过冬摸底；\n"
                "  2. 现场发放《冬春防灾减灾与应急信息报送业务培训课件.pptx》，统一规范灾情直报口径；\n"
                "  3. 建立气象橙色预警直达村（社区）网格员的“叫应”抽查台账。"
            ),
            "tags": ["会议记录", "冬春防灾", "业务培训"],
            "is_pinned": True,
            "files": [
                {
                    "filename": "冬春防灾减灾与应急信息报送业务培训课件.pptx",
                    "content_base64": base64.b64encode(pptx_bytes).decode("ascii"),
                }
            ],
        })

        svg_photo_1 = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="400" viewBox="0 0 640 400">'
            '<rect width="640" height="400" fill="#1E293B"/>'
            '<rect x="24" y="24" width="592" height="352" rx="8" fill="#0F172A" stroke="#3B82F6" stroke-width="2"/>'
            '<text x="48" y="80" fill="#60A5FA" font-family="sans-serif" font-size="22" font-weight="bold">📸 下沉督导现场记录照片 (01)</text>'
            '<text x="48" y="130" fill="#F8FAFC" font-family="sans-serif" font-size="18">巡查点位：县级救灾物资储备中心二号库区</text>'
            '<text x="48" y="170" fill="#CBD5E1" font-family="sans-serif" font-size="15">核验内容：棉大衣 2,200 件、棉被 2,800 床、应急发电机组 16 台</text>'
            '<text x="48" y="210" fill="#34D399" font-family="sans-serif" font-size="15">核查结论：物资码放规范、出入库二维码台账清晰、设备点火测试正常</text>'
            '<text x="48" y="340" fill="#94A3B8" font-family="monospace" font-size="13">拍摄时间：2026-11-18 10:15 | 督导组：第一联合检查组</text>'
            '</svg>'
        ).encode("utf-8")
        create_archive_record({
            "title": "11.18救灾物资库与重点企业隐患排查下沉照片",
            "archive_date": "2026-11-18",
            "category": "03_下沉督导照片",
            "notes_md": (
                "### 现场下沉督导记录\n"
                "- **下沉时间**：2026年11月18日上午\n"
                "- **检查点位**：县级救灾物资储备中心、开发区 2 家重点工贸企业\n"
                "- **现场情况**：救灾物资库帐篷、棉被、折叠床账物相符；对工业园区消防通道及防冻保温措施进行了现场拍照取证留存。"
            ),
            "tags": ["下沉照片", "物资核查", "隐患排查"],
            "is_pinned": False,
            "files": [
                {
                    "filename": "11.18_县救灾物资储备库下沉核查现场图.svg",
                    "content_base64": base64.b64encode(svg_photo_1).decode("ascii"),
                }
            ],
        })

    # 2. 初始化示例【📌 重要事项记录】常备知识库（若为空）
    if imp_cnt == 0:
        flood_xlsx = _build_minimal_xlsx_bytes(
            "防汛水库与重点部位底数",
            [
                ["序号", "水库/河道名称", "所在镇街", "工程规模", "总库容(万m³)", "汛限水位(m)", "防汛行政责任人", "技术责任人", "24h值班电话"],
                ["1", "王吴水库", "王吴镇", "大型水库", "15800", "68.50", "张建国", "李海涛", "0536-2321001"],
                ["2", "峡山水库高密库区", "注沟社区", "重点拦蓄区", "8600", "42.10", "王志远", "刘明辉", "0536-2321002"],
                ["3", "胶河防洪重点段", "朝阳街道", "骨干河道", "设计流量850m³/s", "警戒水位28.2", "赵鹏飞", "陈卫东", "0536-2321003"],
                ["4", "北胶莱河排涝段", "柏城镇", "骨干河道", "设计流量620m³/s", "警戒水位24.8", "孙立军", "周晓峰", "0536-2321004"],
                ["5", "城区铁路桥下穿涵洞", "密水街道", "城区易涝点", "泵站排水1.2万m³/h", "积水预警25cm", "林国强", "吴建华", "0536-2321005"],
            ],
        )
        upsert_important_record({
            "title": "高密汛情基本情况",
            "category": "01_汛情与防灾底数",
            "is_pinned": True,
            "tags": ["汛情底数", "水库河道", "防汛责任人", "常备速查"],
            "content_md": (
                "# 高密汛情与防洪工程基本情况速查\n\n"
                "## 一、水系与流域总体格局\n"
                "辖区主要属胶莱河水系，境内共有骨干河道 **7 条**（胶河、北胶莱河、五龙河、墨水河、张鲁河、秀水河、白羊河），"
                "大中小型水库及重点塘坝 **32 座**（其中大型水库 1 座、中型水库 2 座、小型水库 29 座），"
                "多年平均汛期（6月1日—9月30日）降水量约 **485 毫米**，占全年降水量的 72% 以上。\n\n"
                "## 二、防汛应急响应四级阈值速查\n"
                "| 响应等级 | 启动条件核心指标 | 指挥部坐镇调度要求 | 抢险物资与队伍指令 |\n"
                "| :--- | :--- | :--- | :--- |\n"
                "| **Ⅳ级（一般）** | 24小时降雨量达 50~100mm 或发布暴雨蓝色/黄色预警 | 防办主任坐镇指挥部联合值守 | 镇街巡河员全员上岗，城区排涝泵站预抽排 |\n"
                "| **Ⅲ级（较大）** | 24小时降雨量达 100~150mm 或骨干河道超警戒水位 | 分管副指挥长坐镇，气象水利每2小时会商 | 县级综合救援队前置备勤，危旧房人员提前转移 |\n"
                "| **Ⅱ级（重大）** | 24小时降雨量超 150mm 或大型水库逼近汛限水位 | 指挥长坐镇，全县启动“关停撤转”硬措施 | 调集机械化抢险队、冲锋舟与防汛块石袋前置驻守 |\n"
                "| **Ⅰ级（特别重大）** | 遭遇超标准洪水、漫坝管涌重大险情 | 主要领导一线靠前指挥，全社会总动员 | 全力组织受威胁群众扩面转移安置，确保零伤亡 |\n\n"
                "## 三、重点抢险救援力量与物资常备底数\n"
                "- **专业及社会应急队伍**：共 **14 支、680 人**（含综合应急救援大队 120 人、水利抢险队 160 人、市政排涝队 110 人）。\n"
                "- **防汛物资储备底数**：防汛编织袋 **28 万条**、土工布 **4.5 万平方米**、救生衣 **3,600 件**、大流量移动排涝泵车 **8 台**、冲锋舟及橡皮艇 **24 艘**。\n"
                "- **附表**：下方已挂载《高密防汛重点水库与易涝点责任人底数表.xlsx》，可直接页内搜索任意镇街或水库名称。"
            ),
            "files": [
                {
                    "filename": "高密防汛重点水库与易涝点责任人底数表.xlsx",
                    "content_base64": base64.b64encode(flood_xlsx).decode("ascii"),
                }
            ],
        })

        eq_md = (
            "# 全县地震应急预案与黄金一小时处置操作指南\n\n"
            "## 一、应急响应分级与自动触发标准\n"
            "| 响应级别 | 震级与烈度触发条件 | 启动权限 | 第一时间核心动作 |\n"
            "| :--- | :--- | :--- | :--- |\n"
            "| **Ⅰ级 / Ⅱ级响应** | 辖区内发生 $M \\ge 5.0$ 级地震，或造成显著人员伤亡 | 县委、县政府主要领导 | 立即成立抗震救灾现场指挥部，断电断气排险，全员搜救 |\n"
            "| **Ⅲ级响应** | 辖区内发生 $4.0 \\le M < 5.0$ 级地震，震感强烈 | 县抗震救灾指挥部指挥长 | 15分钟内集结应急、消防、卫健、住建、交通赶赴震中 |\n"
            "| **Ⅳ级响应** | 辖区内发生 $3.0 \\le M < 4.0$ 级有感地震，或周边波及 | 县应急管理局主要负责人 | 30分钟内完成各镇街震情速报摸底，发布权威震情通报 |\n\n"
            "## 二、震后“黄金 1 小时”工作清单（按分钟推进）\n"
            "1. **震后 0~15 分钟（信息核实与响应启动）**：\n"
            "   - 值班室立即向省市地震台网核实发震时刻、震中经纬度、震级与震源深度；\n"
            "   - 通过应急广播、微信工作群、政务短信一键启动“震情零报告”叫应机制。\n"
            "2. **震后 15~30 分钟（力量集结与生命线排查）**：\n"
            "   - 消防救援大队、综合应急救援队携带生命探测仪、破拆装备出动；\n"
            "   - 供电、燃气、危化品科立即排查危化园区管网、变电站、水库大坝有无次生灾害。\n"
            "3. **震后 30~60 分钟（灾情初报与避难场所启用）**：\n"
            "   - 汇总各镇街第一轮人员伤亡、房屋倒损情况，按程序向上级报送《震情灾情初报》；\n"
            "   - 全面开放全县 **18 处** 规范化地震应急避难场所（文体公园、市民广场、各中小学操场）。"
        )
        tmp_docx = Path(tempfile.gettempdir()) / "地震应急预案速查版.docx"
        build_gb9704_docx(
            output_path=tmp_docx,
            raw_text=eq_md,
            title="全县地震应急预案与黄金一小时处置指南",
            issuing_org="县抗震救灾指挥部办公室",
            mode="report",
        )
        eq_docx_bytes = tmp_docx.read_bytes()
        tmp_docx.unlink(missing_ok=True)

        upsert_important_record({
            "title": "地震应急预案",
            "category": "02_突发事件应急预案",
            "is_pinned": True,
            "tags": ["地震预案", "黄金一小时", "避难场所", "响应分级"],
            "content_md": eq_md,
            "files": [
                {
                    "filename": "全县地震应急预案与黄金一小时处置指南_标准排版.docx",
                    "content_base64": base64.b64encode(eq_docx_bytes).decode("ascii"),
                }
            ],
        })

    if task_cnt > 0:
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
        if _is_safe_subpath(target_static, STATIC_DIR) and target_static.is_file():
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
            categories = list_physical_categories(scope="task")
            archive_categories = list_physical_categories(scope="archive")
            record_categories = list_physical_categories(scope="record")
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

                arch_rows = conn.execute("SELECT * FROM archives ORDER BY is_pinned DESC, archive_date DESC, id DESC").fetchall()
                archives = [serialize_archive(conn, ar, preloaded_docs=docs, ws_root=ws_root) for ar in arch_rows]

                imp_rows = conn.execute("SELECT * FROM important_records ORDER BY is_pinned DESC, sort_order ASC, id DESC").fetchall()
                important_records = [serialize_record(conn, ir, preloaded_docs=docs, ws_root=ws_root, include_structured_preview=False) for ir in imp_rows]

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
                "default_org_name": cfg.get("default_org_name", "县应急管理局"),
                "lan_notice": cfg.get("lan_notice", ""),
                "ai_configured": bool(cfg.get("ai_api_base")),
                "ai_model": cfg.get("ai_model", "deepseek-chat"),
                "ai_api_base": cfg.get("ai_api_base", ""),
                "xkl_gw_cli": cfg.get("xkl_gw_cli", ""),
                "prefix_archived_folder": bool(cfg.get("prefix_archived_folder", False)),
                "categories": categories,
                "archive_categories": archive_categories,
                "record_categories": record_categories,
                "tasks": tasks,
                "documents": docs,
                "archives": archives,
                "important_records": important_records,
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

        if path == "/api/archives":
            cat_filter = (qs.get("category") or [""])[0]
            q_filter = (qs.get("q") or [""])[0].strip().lower()
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE archive_id IS NOT NULL ORDER BY id ASC").fetchall()
                docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
                rows = conn.execute("SELECT * FROM archives ORDER BY is_pinned DESC, archive_date DESC, id DESC").fetchall()
                archives = []
                for r in rows:
                    if cat_filter and r["category"] != cat_filter:
                        continue
                    a_obj = serialize_archive(conn, r, preloaded_docs=docs, ws_root=ws_root)
                    if q_filter:
                        hay = f"{a_obj['title']} {a_obj['archive_date']} {a_obj['category']} {a_obj['notes_md']} {' '.join(a_obj['tags'])} {' '.join(f['filename'] for f in a_obj['files'])}".lower()
                        if q_filter not in hay:
                            continue
                    archives.append(a_obj)
                conn.close()
            self._send_json({"ok": True, "archives": archives, "categories": list_physical_categories(scope="archive")})
            return

        if path == "/api/records":
            cat_filter = (qs.get("category") or [""])[0]
            q_filter = (qs.get("q") or [""])[0].strip().lower()
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE record_id IS NOT NULL ORDER BY id ASC").fetchall()
                docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
                rows = conn.execute("SELECT * FROM important_records ORDER BY is_pinned DESC, sort_order ASC, id DESC").fetchall()
                records = []
                for r in rows:
                    if cat_filter and r["category"] != cat_filter:
                        continue
                    r_obj = serialize_record(conn, r, preloaded_docs=docs, ws_root=ws_root, include_structured_preview=False)
                    if q_filter:
                        hay = f"{r_obj['title']} {r_obj['category']} {r_obj['content_md']} {' '.join(r_obj['tags'])} {' '.join(f['filename'] for f in r_obj['files'])}".lower()
                        if q_filter not in hay:
                            continue
                    records.append(r_obj)
                conn.close()
            self._send_json({"ok": True, "important_records": records, "categories": list_physical_categories(scope="record")})
            return

        m_rec_detail = re.match(r"^/api/records/(\d+)$", path)
        if m_rec_detail:
            rid = int(m_rec_detail.group(1))
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM important_records WHERE id = ?", (rid,)).fetchone()
                if not row:
                    conn.close()
                    self._send_json({"error": "重要事项记录不存在"}, status=404)
                    return
                r_obj = serialize_record(conn, row, ws_root=ws_root, include_structured_preview=True)
                conn.close()
            self._send_json({"ok": True, "record": r_obj})
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
            ws_root = get_workspace_root(cfg)
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                conn.close()
            if not row:
                self._send_json({"error": "文件不存在"}, status=404)
                return
            d = serialize_doc_light(row, ws_root=ws_root)
            d["extracted_text"] = row["extracted_text"] or ""
            d["is_editable_text"] = Path(row["filename"]).suffix.lower() in (".md", ".txt", ".markdown", ".csv", ".json")
            d["structured_preview"] = extract_structured_preview(ws_root / row["rel_path"], doc_id=doc_id)
            self._send_json({"document": d})
            return

        # 内联流式媒体/PDF直读接口（支持 HTTP Range 206 Partial Content，支持音频拖动与 PDF 内嵌直读）
        m_media = re.match(r"^/api/media/(\d+)$", path)
        if m_media:
            doc_id = int(m_media.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT filename, rel_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
                conn.close()
            if not row:
                self.send_error(404, "Media Not Found")
                return
            file_path = get_workspace_root(cfg) / row["rel_path"]
            if not file_path.is_file():
                self.send_error(404, "Physical File Not Found")
                return

            ctype, _ = mimetypes.guess_type(str(file_path))
            if not ctype:
                ext = file_path.suffix.lower()
                ctype = {
                    ".mp3": "audio/mpeg",
                    ".m4a": "audio/mp4",
                    ".wav": "audio/wav",
                    ".ogg": "audio/ogg",
                    ".aac": "audio/aac",
                    ".flac": "audio/flac",
                    ".mp4": "video/mp4",
                    ".webm": "video/webm",
                    ".pdf": "application/pdf",
                    ".svg": "image/svg+xml",
                    ".webp": "image/webp",
                }.get(ext, "application/octet-stream")

            file_size = file_path.stat().st_size
            range_hdr = self.headers.get("Range")
            encoded_name = urllib.parse.quote(row["filename"])

            if range_hdr and range_hdr.startswith("bytes="):
                try:
                    r_part = range_hdr.split("=", 1)[1].split(",")[0].strip()
                    start_s, end_s = r_part.split("-", 1)
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else (file_size - 1)
                    end = min(end, file_size - 1)
                    if 0 <= start <= end < file_size:
                        chunk_len = end - start + 1
                        with open(file_path, "rb") as f:
                            f.seek(start)
                            chunk = f.read(chunk_len)
                        self.send_response(206)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Accept-Ranges", "bytes")
                        self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                        self.send_header("Content-Length", str(chunk_len))
                        self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{encoded_name}")
                        self.end_headers()
                        self.wfile.write(chunk)
                        return
                except Exception:
                    pass

            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f"inline; filename*=UTF-8''{encoded_name}")
            self.end_headers()
            self.wfile.write(data)
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

        m_azip = re.match(r"^/api/archives/(\d+)/zip$", path)
        if m_azip:
            aid = int(m_azip.group(1))
            with DB_LOCK:
                conn = get_db()
                write_archive_readme_file(conn, aid)
                row = conn.execute("SELECT * FROM archives WHERE id = ?", (aid,)).fetchone()
                conn.close()
            if not row:
                self.send_error(404, "Archive Not Found")
                return
            folder_path = get_workspace_root(cfg) / row["folder_rel_path"]
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                if folder_path.exists():
                    for f in folder_path.rglob("*"):
                        if f.is_file() and not f.name.startswith("~$") and f.name != ".gitkeep":
                            zf.write(f, arcname=f.relative_to(folder_path))
            zip_bytes = buf.getvalue()
            zip_name = urllib.parse.quote(f"{row['title']}_资料存档包.zip")
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
            tasks, docs, archives, records = execute_search(q, scope=scope, category=cat, doc_role=role)
            self._send_json({
                "query": q,
                "scope": scope,
                "tasks": tasks,
                "documents": docs,
                "archives": archives,
                "important_records": records,
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

                # 1. 检索待办事项
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
                top_tasks = [x[1] for x in scored_tasks[:10]]

                # 2. 检索重要事项记录
                all_recs = conn.execute("SELECT * FROM important_records ORDER BY is_pinned DESC, id DESC").fetchall()
                scored_records = []
                for r in all_recs:
                    if category and r["category"] != category:
                        continue
                    hay = f"{r['title']} {r['category']} {r['content_md']} {r['tags_json']}".lower()
                    hits = sum(3 if kw.lower() in r["title"].lower() else (1 if kw.lower() in hay else 0) for kw in kws)
                    if hits > 0:
                        r_obj = serialize_record(conn, r, preloaded_docs=preloaded_light_docs, ws_root=ws_root)
                        r_obj["match_snippet"] = build_highlight_snippet(r["content_md"] or r["title"], kws)
                        scored_records.append((hits, r_obj))
                scored_records.sort(key=lambda x: (-x[0], -x[1]["id"]))
                top_records = [x[1] for x in scored_records[:8]]

                # 3. 检索资料存档包
                all_archs = conn.execute("SELECT * FROM archives ORDER BY is_pinned DESC, archive_date DESC, id DESC").fetchall()
                scored_archives = []
                for r in all_archs:
                    if category and r["category"] != category:
                        continue
                    hay = f"{r['title']} {r['archive_date']} {r['category']} {r['notes_md']} {r['tags_json']}".lower()
                    hits = sum(3 if kw.lower() in r["title"].lower() else (1 if kw.lower() in hay else 0) for kw in kws)
                    if hits > 0:
                        a_obj = serialize_archive(conn, r, preloaded_docs=preloaded_light_docs, ws_root=ws_root)
                        a_obj["match_snippet"] = build_highlight_snippet(r["notes_md"] or r["title"], kws)
                        scored_archives.append((hits, a_obj))
                scored_archives.sort(key=lambda x: (-x[0], -x[1]["id"]))
                top_archives = [x[1] for x in scored_archives[:8]]

                # 4. 检索文档（优先利用 FTS5 OR 表达式做候选集预筛，避免全表扫描内存暴涨）
                fts_or_expr = _build_fts_match_expr(kws, join_op="OR")
                candidate_docs = []
                if fts_or_expr:
                    try:
                        candidate_docs = conn.execute(
                            "SELECT * FROM documents WHERE id IN (SELECT doc_id FROM documents_fts WHERE documents_fts MATCH ?) ORDER BY is_starred DESC, id DESC LIMIT 120",
                            (fts_or_expr,),
                        ).fetchall()
                    except Exception:
                        candidate_docs = []
                if not candidate_docs:
                    candidate_docs = conn.execute("SELECT * FROM documents ORDER BY is_starred DESC, id DESC LIMIT 200").fetchall()

                scored_docs = []
                for r in candidate_docs:
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
                top_docs = [x[1] for x in scored_docs[:15]]
                conn.close()

            ai_res = synthesize_ai_search_answer(
                query, kws, top_tasks, top_docs,
                matched_archives=top_archives,
                matched_records=top_records,
                config=cfg,
            )
            self._send_json({
                "query": query,
                "keywords": kws,
                "answer": ai_res["answer"],
                "engine": ai_res["engine"],
                "tasks": top_tasks,
                "documents": top_docs,
                "archives": top_archives,
                "important_records": top_records,
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
            if not _is_safe_subpath(target_abs, ws_root) or not target_abs.exists():
                self._send_json({"error": f"本地路径不存在或越界: {target_abs}"}, status=404)
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
                    elif ext in (".pptx", ".ppt", ".dps"):
                        candidates = ["wpp", "wps", "xdg-open"]
                    elif ext in IMAGE_EXTS or ext in AUDIO_EXTS or ext in VIDEO_EXTS or ext == ".pdf":
                        candidates = ["xdg-open", "wps"]
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
                            task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (?, NULL, NULL, ?, ?, ?, 'draft', '文本/Markdown', ?, ?, ?, '[]', 0, 0, ?, ?)
                        """,
                        (int(task_id), dest_path.name, rel_file, t_row["category"], st.st_size, round(st.st_mtime, 2), content, now_str, now_str),
                    )
                    new_doc_id = int(cur.lastrowid)
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
                    cat_dir = ensure_category_dir(ws_root, cat, scope="task")
                    dest_path = resolve_unique_dest_path(cat_dir, filename, content.encode("utf-8"))
                    dest_path.write_text(content, encoding="utf-8")
                    st = dest_path.stat()
                    rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
                    cur = conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (NULL, NULL, NULL, ?, ?, ?, 'draft', '文本/Markdown', ?, ?, ?, '[]', 0, 0, ?, ?)
                        """,
                        (dest_path.name, rel_file, cat, st.st_size, round(st.st_mtime, 2), content, now_str, now_str),
                    )
                    new_doc_id = int(cur.lastrowid)
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
            default_org = cfg.get("default_org_name") or "县应急管理局"
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

                # 修复 BUG-02：report 汇报模式不自动继承上级来文的 doc_number，且署名优先使用本单位默认名称
                doc_num = custom_doc_num if mode == "redhead" else ""
                if mode == "redhead" and not doc_num and t_row:
                    doc_num = t_row["doc_number"] or ""
                org_name = custom_org or default_org
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
                        task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                        file_type, file_size, mtime, extracted_text,
                        tags_json, is_starred, is_pinned, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, 0, 0, ?, ?)
                    ON CONFLICT(rel_path) DO UPDATE SET
                        file_size=excluded.file_size,
                        mtime=excluded.mtime,
                        extracted_text=excluded.extracted_text,
                        updated_at=excluded.updated_at
                    """,
                    (
                        d_row["task_id"],
                        d_row["archive_id"],
                        d_row["record_id"],
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
                b64 = body["content_base64"]
                if "," in b64 and b64.startswith("data:"):
                    b64 = b64.split(",", 1)[1]
                raw_bytes = base64.b64decode(b64)
                with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tf:
                    tf.write(raw_bytes)
                    tmp_p = Path(tf.name)
                try:
                    ext_txt, _ = extract_document_text(tmp_p)
                    if ext_txt and not ext_txt.startswith("【影像图片文件："):
                        text = (text + "\n\n" + ext_txt).strip()
                finally:
                    tmp_p.unlink(missing_ok=True)

            parsed = parse_incoming_content(
                text,
                filename=filename,
                source_hint=str(body.get("source_channel") or ""),
                categories=list_physical_categories(scope="task"),
                config=cfg,
            )
            parsed["extracted_text"] = text
            self._send_json({"ok": True, "parsed": parsed})
            return

        if path == "/api/tasks":
            created = create_task_record(body)
            self._send_json({"ok": True, "task": created})
            return

        if path == "/api/archives":
            created_arch = create_archive_record(body)
            self._send_json({"ok": True, "archive": created_arch, "archive_categories": list_physical_categories(scope="archive")})
            return

        m_afiles = re.match(r"^/api/archives/(\d+)/files$", path)
        if m_afiles:
            aid = int(m_afiles.group(1))
            updated_arch = append_files_to_archive(
                aid,
                files_list=body.get("files") or [],
                notes_append=str(body.get("notes_append") or ""),
            )
            if not updated_arch:
                self._send_json({"error": "资料存档包不存在"}, status=404)
                return
            self._send_json({"ok": True, "archive": updated_arch})
            return

        if path == "/api/records":
            created_rec = upsert_important_record(body)
            self._send_json({"ok": True, "record": created_rec, "record_categories": list_physical_categories(scope="record")})
            return

        m_rfiles = re.match(r"^/api/records/(\d+)/files$", path)
        if m_rfiles:
            rid = int(m_rfiles.group(1))
            updated_rec = upsert_important_record({"files": body.get("files") or []}, record_id=rid)
            self._send_json({"ok": True, "record": updated_rec})
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
                for fitem in files_list:
                    fname, raw_bytes, f_role = _decode_file_item_bytes(fitem)
                    role = fitem.get("doc_role") or doc_role or f_role
                    prefix = "[来文]_" if role == "incoming" else "[材料]_"
                    if not fname.startswith(("[来文]", "[材料]", "【来文】", "【材料】")):
                        save_name = prefix + fname
                    else:
                        save_name = fname
                    dest = resolve_unique_dest_path(task_folder, save_name, raw_bytes)
                    dest.write_bytes(raw_bytes)
                    st = dest.stat()
                    ext_txt, ftype = extract_document_text(dest)
                    rel_file = str(dest.relative_to(ws_root)).replace("\\", "/")
                    conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
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
            cat_dir = ensure_category_dir(ws_root, category, scope="task")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            added_docs = []
            with DB_LOCK:
                conn = get_db()
                for fitem in files_list:
                    fname, raw_bytes, _ = _decode_file_item_bytes(fitem)
                    dest = resolve_unique_dest_path(cat_dir, fname, raw_bytes)
                    dest.write_bytes(raw_bytes)
                    st = dest.stat()
                    ext_txt, ftype = extract_document_text(dest)
                    rel_file = str(dest.relative_to(ws_root)).replace("\\", "/")
                    conn.execute(
                        """
                        INSERT INTO documents (
                            task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                            file_type, file_size, mtime, extracted_text,
                            tags_json, is_starred, is_pinned, created_at, updated_at
                        ) VALUES (NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                                new_cat_val, new_rel_folder = _move_task_folder_to_category(
                                    conn, ws_root, int(row["task_id"]), target_cat, prefix_archived=bool(cfg.get("prefix_archived_folder"))
                                )
                                conn.execute(
                                    "UPDATE tasks SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?",
                                    (new_cat_val, new_rel_folder, now_str, int(row["task_id"])),
                                )
                                write_task_readme_file(conn, int(row["task_id"]))
                            else:
                                target_dir = ensure_category_dir(ws_root, target_cat, scope="task")
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
                            if f_abs.is_file() and _is_safe_subpath(f_abs, ws_root):
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
            avail_cats = list_physical_categories(scope="task")
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            allowed_exts = {".docx", ".doc", ".wps", ".ofd", ".pdf", ".txt", ".md", ".xlsx", ".xls", ".csv", ".pptx", ".ppt", ".et", ".dps"}
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
                            dest_cat_dir = ensure_category_dir(ws_root, chosen_cat, scope="task")
                            target_f = resolve_unique_dest_path(dest_cat_dir, f.name, raw_bytes)
                            if target_f.resolve() != f.resolve():
                                shutil.copy2(f, target_f)
                            st = target_f.stat()
                            rel_file = str(target_f.relative_to(ws_root)).replace("\\", "/")
                            role = infer_doc_role(target_f.name, ext_txt[:300], in_task_folder=False, cat_name=chosen_cat)

                            conn.execute(
                                """
                                INSERT INTO documents (
                                    task_id, archive_id, record_id, filename, rel_path, category, doc_role,
                                    file_type, file_size, mtime, extracted_text,
                                    tags_json, is_starred, is_pinned, created_at, updated_at
                                ) VALUES (NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
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
                "category": d_row["category"] if d_row["category"] in list_physical_categories("task") else "01_防灾减灾",
                "source_channel": "上级来文",
                "auto_ai": True,
                "reuse_doc_id": doc_id if d_row["task_id"] is None and d_row["archive_id"] is None and d_row["record_id"] is None else None,
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
            scope = str(body.get("scope") or "task").strip()
            if not cat_name or cat_name in RESERVED_TOP_DIRS:
                self._send_json({"error": "分类目录名称无效"}, status=400)
                return
            ws_root = get_workspace_root(cfg)
            ensure_category_dir(ws_root, cat_name, scope=scope)
            self._send_json({
                "ok": True,
                "scope": scope,
                "categories": list_physical_categories(scope=scope),
            })
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
                arch_rows = conn.execute("SELECT * FROM archives ORDER BY archive_date DESC").fetchall()
                archives = [serialize_archive(conn, ar, preloaded_docs=docs, ws_root=ws_root) for ar in arch_rows]
                conn.close()

            completed = [t for t in tasks if t["status"] == "archived"]
            ongoing = [t for t in tasks if t["status"] != "archived"]
            all_drafts = []
            for t in tasks:
                for d in t["drafted_files"]:
                    all_drafts.append(f"《{d['filename']}》（对应事项：{t['title']}）")

            prompt_context = [
                f"统计周期：{period_label}",
                f"共承办各类来文与通知事项 {len(tasks)} 项（其中已办结归档 {len(completed)} 项，正在推进 {len(ongoing)} 项），"
                f"累计起草报送各类材料 {len(all_drafts)} 份，开展会议/培训/下沉督导等资料存档活动 {len(archives)} 次。",
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
            prompt_context.append("\n三、会议、培训与一线督导资料存档情况：")
            for idx, a in enumerate(archives[:10], 1):
                prompt_context.append(
                    f"{idx}. 【{a['category']}】{a['title']}（日期：{a['archive_date']}，归档附件 {len(a['files'])} 份）：{(a['notes_md'] or '')[:100]}"
                )
            prompt_context.append("\n四、期间撰写与留存的核心材料清单：")
            for d_str in all_drafts:
                prompt_context.append(f"- {d_str}")

            raw_summary_data = "\n".join(prompt_context)
            llm_text = None
            if cfg.get("ai_api_base"):
                sys_p = "你是机关单位综合文字工作专家，请根据提供的工作台办文台账、会议培训下沉存档及材料清单，撰写一份条理清晰、用词规范的阶段性工作总结（包含：一、工作总体开展情况；二、重点工作与会议督导落实成效；三、下一步工作计划）。"
                llm_text = call_openai_compatible_api(cfg, sys_p, raw_summary_data)

            if not llm_text:
                llm_text = (
                    f"# {period_label}工作总结汇报（初稿）\n\n"
                    f"## 一、总体办文与工作开展情况\n"
                    f"本阶段共梳理承办上级来文及各渠道工作通知 **{len(tasks)}** 项，其中已圆满办结归档 **{len(completed)}** 项，"
                    f"正在有序推进 **{len(ongoing)}** 项；累计组织会议、业务培训及一线督导排查存档 **{len(archives)}** 次，"
                    f"起草报送各类方案、汇报及统计材料 **{len(all_drafts)}** 份，确保件件有着落、事事有回音。\n\n"
                    f"## 二、重点事项落实情况\n"
                    + "\n".join(
                        f"- **{t['title']}**（{t.get('doc_number') or t['source_channel']}）：{t.get('summary') or ''}。"
                        f"目前状态：{'已办结归档' if t['status'] == 'archived' else '推进中'}，"
                        f"进展记录：{t['progress_logs'][0]['text'] if t['progress_logs'] else '已落实相关材料报送'}。"
                        for t in tasks[:12]
                    )
                    + "\n\n## 三、会议培训与下沉督导开展情况\n"
                    + (
                        "\n".join(
                            f"- **{a['title']}**（{a['archive_date']} · `{a['category']}`）：已归档 {len(a['files'])} 份现场记录/课件/影像资料。"
                            for a in archives[:8]
                        )
                        if archives
                        else "- 详见资料存档各分类目录。"
                    )
                    + "\n\n## 四、期间起草与归档材料明细\n"
                    + ("\n".join(f"- {x}" for x in all_drafts) if all_drafts else "- 详见公文与材料库各分类目录")
                    + "\n\n## 五、下一步工作计划\n"
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
            for k in ("workspace_root", "default_org_name", "prefix_archived_folder", "ai_api_base", "ai_model", "lan_notice", "xkl_gw_cli"):
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
            scope = str(body.get("scope") or "task").strip()
            old_name = sanitize_filename(body.get("old_name") or "")
            new_name = sanitize_filename(body.get("new_name") or "")
            if not old_name or not new_name or old_name == new_name:
                self._send_json({"error": "请输入有效的新分类目录名"}, status=400)
                return

            if scope == "archive":
                base_dir = ws_root / ARCHIVE_ROOT_DIRNAME
            elif scope == "record":
                base_dir = ws_root / RECORD_ROOT_DIRNAME
            else:
                base_dir = ws_root

            old_dir = base_dir / old_name
            new_dir = base_dir / new_name
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
                ensure_category_dir(ws_root, new_name, scope=scope)

                if scope == "archive":
                    old_prefix = f"{ARCHIVE_ROOT_DIRNAME}/{old_name}/"
                    new_prefix = f"{ARCHIVE_ROOT_DIRNAME}/{new_name}/"
                    for ar in conn.execute("SELECT id, folder_rel_path FROM archives WHERE category = ?", (old_name,)).fetchall():
                        orel = ar["folder_rel_path"] or ""
                        nrel = (new_prefix + orel[len(old_prefix):]) if orel.startswith(old_prefix) else orel
                        conn.execute("UPDATE archives SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?", (new_name, nrel, now_str, ar["id"]))
                    for dr in conn.execute("SELECT id, rel_path FROM documents WHERE archive_id IS NOT NULL AND category = ?", (old_name,)).fetchall():
                        orel = dr["rel_path"] or ""
                        nrel = (new_prefix + orel[len(old_prefix):]) if orel.startswith(old_prefix) else orel
                        conn.execute("UPDATE documents SET category = ?, rel_path = ?, updated_at = ? WHERE id = ?", (new_name, nrel, now_str, dr["id"]))
                        _sync_fts_doc(conn, dr["id"])
                elif scope == "record":
                    old_prefix = f"{RECORD_ROOT_DIRNAME}/{old_name}/"
                    new_prefix = f"{RECORD_ROOT_DIRNAME}/{new_name}/"
                    for rr in conn.execute("SELECT id, folder_rel_path FROM important_records WHERE category = ?", (old_name,)).fetchall():
                        orel = rr["folder_rel_path"] or ""
                        nrel = (new_prefix + orel[len(old_prefix):]) if orel.startswith(old_prefix) else orel
                        conn.execute("UPDATE important_records SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?", (new_name, nrel, now_str, rr["id"]))
                    for dr in conn.execute("SELECT id, rel_path FROM documents WHERE record_id IS NOT NULL AND category = ?", (old_name,)).fetchall():
                        orel = dr["rel_path"] or ""
                        nrel = (new_prefix + orel[len(old_prefix):]) if orel.startswith(old_prefix) else orel
                        conn.execute("UPDATE documents SET category = ?, rel_path = ?, updated_at = ? WHERE id = ?", (new_name, nrel, now_str, dr["id"]))
                        _sync_fts_doc(conn, dr["id"])
                else:
                    t_rows = conn.execute("SELECT id, folder_rel_path FROM tasks WHERE category = ?", (old_name,)).fetchall()
                    for tr in t_rows:
                        old_rel = tr["folder_rel_path"] or ""
                        new_rel = (new_name + old_rel[len(old_name):]) if old_rel.startswith(old_name + "/") else old_rel
                        conn.execute(
                            "UPDATE tasks SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?",
                            (new_name, new_rel, now_str, tr["id"]),
                        )

                    d_rows = conn.execute("SELECT id, rel_path FROM documents WHERE archive_id IS NULL AND record_id IS NULL AND category = ?", (old_name,)).fetchall()
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
            self._send_json({"ok": True, "scope": scope, "categories": list_physical_categories(scope=scope)})
            return

        m_arch = re.match(r"^/api/archives/(\d+)$", path)
        if m_arch:
            aid = int(m_arch.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM archives WHERE id = ?", (aid,)).fetchone()
                if not row:
                    conn.close()
                    self._send_json({"error": "资料存档包不存在"}, status=404)
                    return
                a = dict(row)
                new_title = sanitize_filename(body.get("title") if body.get("title") is not None else a["title"])
                new_date = str(body.get("archive_date") if body.get("archive_date") is not None else a["archive_date"]).strip()
                new_cat = sanitize_filename(body.get("category") if body.get("category") is not None else a["category"])
                new_notes = str(body.get("notes_md") if body.get("notes_md") is not None else a["notes_md"])
                new_pinned = 1 if body.get("is_pinned", bool(a["is_pinned"])) else 0
                if "tags" in body:
                    raw_t = body["tags"]
                    new_tags = [t.strip() for t in re.split(r"[\s,，、;；]+", raw_t) if t.strip()] if isinstance(raw_t, str) else [str(t).strip() for t in (raw_t or []) if str(t).strip()]
                else:
                    new_tags = json.loads(a["tags_json"] or "[]")

                old_rel = a["folder_rel_path"]
                new_rel = old_rel
                old_abs = ws_root / old_rel
                target_cat_dir = ensure_category_dir(ws_root, new_cat, scope="archive")
                folder_leaf = sanitize_filename(f"{new_date}_{new_title}" if not re.match(r"^\d{4}-\d{2}-\d{2}", new_title) else new_title)
                new_abs = target_cat_dir / folder_leaf
                if old_abs.exists() and old_abs.resolve() != new_abs.resolve():
                    counter = 2
                    while new_abs.exists() and new_abs.resolve() != old_abs.resolve():
                        new_abs = target_cat_dir / f"{folder_leaf}_{counter}"
                        counter += 1
                    shutil.move(str(old_abs), str(new_abs))
                    new_rel = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                    for dr in conn.execute("SELECT id, rel_path FROM documents WHERE archive_id = ?", (aid,)).fetchall():
                        if dr["rel_path"].startswith(old_rel + "/"):
                            u_rel = new_rel + dr["rel_path"][len(old_rel):]
                            conn.execute(
                                "UPDATE documents SET rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                                (u_rel, new_cat, now_str, dr["id"]),
                            )
                            _sync_fts_doc(conn, int(dr["id"]))

                conn.execute(
                    """
                    UPDATE archives SET
                        title = ?, archive_date = ?, category = ?, notes_md = ?,
                        tags_json = ?, is_pinned = ?, folder_rel_path = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        new_title,
                        new_date,
                        new_cat,
                        new_notes,
                        json.dumps(new_tags, ensure_ascii=False),
                        new_pinned,
                        new_rel,
                        now_str,
                        aid,
                    ),
                )
                write_archive_readme_file(conn, aid)
                conn.commit()
                u_row = conn.execute("SELECT * FROM archives WHERE id = ?", (aid,)).fetchone()
                res_arch = serialize_archive(conn, u_row, ws_root=ws_root)
                conn.close()
            self._send_json({"ok": True, "archive": res_arch})
            return

        m_rec_put = re.match(r"^/api/records/(\d+)$", path)
        if m_rec_put:
            rid = int(m_rec_put.group(1))
            updated = upsert_important_record(body, record_id=rid)
            self._send_json({"ok": True, "record": updated})
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

                if d["task_id"] is not None and new_cat != d["category"]:
                    moved_cat, moved_rel_folder = _move_task_folder_to_category(
                        conn, ws_root, int(d["task_id"]), new_cat, prefix_archived=bool(cfg.get("prefix_archived_folder"))
                    )
                    conn.execute(
                        "UPDATE tasks SET category = ?, folder_rel_path = ?, updated_at = ? WHERE id = ?",
                        (moved_cat, moved_rel_folder, now_str, int(d["task_id"])),
                    )
                    row_refreshed = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                    if row_refreshed:
                        d = dict(row_refreshed)
                        old_abs = ws_root / d["rel_path"]

                new_rel_path = d["rel_path"]
                if old_abs.exists():
                    if d["task_id"] is None and d["archive_id"] is None and d["record_id"] is None and (new_cat != d["category"] or new_filename != d["filename"]):
                        target_dir = ensure_category_dir(ws_root, new_cat, scope="task")
                        new_abs = target_dir / new_filename
                        if old_abs.resolve() != new_abs.resolve():
                            new_abs = resolve_unique_dest_path(target_dir, new_filename)
                            shutil.move(str(old_abs), str(new_abs))
                            new_filename = new_abs.name
                            new_rel_path = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                    elif new_filename != d["filename"]:
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
            scope = (qs.get("scope") or ["task"])[0]
            if not cat_name:
                self._send_json({"error": "分类名不能为空"}, status=400)
                return

            with DB_LOCK:
                conn = get_db()
                if scope == "archive":
                    cnt = conn.execute("SELECT COUNT(*) FROM archives WHERE category = ?", (cat_name,)).fetchone()[0]
                    cat_dir = ws_root / ARCHIVE_ROOT_DIRNAME / cat_name
                    if cnt > 0:
                        conn.close()
                        self._send_json({"error": f"分类【{cat_name}】下仍有 {cnt} 个存档包，请先移入其他分类后再删除"}, status=400)
                        return
                elif scope == "record":
                    cnt = conn.execute("SELECT COUNT(*) FROM important_records WHERE category = ?", (cat_name,)).fetchone()[0]
                    cat_dir = ws_root / RECORD_ROOT_DIRNAME / cat_name
                    if cnt > 0:
                        conn.close()
                        self._send_json({"error": f"分类【{cat_name}】下仍有 {cnt} 条重要事项记录，请先移入其他分类后再删除"}, status=400)
                        return
                else:
                    t_cnt = conn.execute("SELECT COUNT(*) FROM tasks WHERE category = ?", (cat_name,)).fetchone()[0]
                    d_cnt = conn.execute("SELECT COUNT(*) FROM documents WHERE archive_id IS NULL AND record_id IS NULL AND category = ?", (cat_name,)).fetchone()[0]
                    cat_dir = ws_root / cat_name
                    if t_cnt > 0 or d_cnt > 0:
                        conn.close()
                        self._send_json({"error": f"分类【{cat_name}】下仍有 {t_cnt} 项工作和 {d_cnt} 份文件，请先点击【重命名/合并】将其归入其他目录后再删除"}, status=400)
                        return
                conn.close()
            if cat_dir.exists() and cat_dir.is_dir() and _is_safe_subpath(cat_dir, ws_root):
                shutil.rmtree(cat_dir, ignore_errors=True)
            self._send_json({"ok": True, "scope": scope, "categories": list_physical_categories(scope=scope)})
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

        m_arch = re.match(r"^/api/archives/(\d+)$", path)
        if m_arch:
            aid = int(m_arch.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM archives WHERE id = ?", (aid,)).fetchone()
                if row:
                    folder_abs = ws_root / row["folder_rel_path"]
                    if delete_disk and row["folder_rel_path"] and folder_abs.exists() and _is_safe_subpath(folder_abs, ws_root):
                        shutil.rmtree(folder_abs, ignore_errors=True)
                    else:
                        rm_f = folder_abs / "00_存档说明与纪要.md"
                        if rm_f.is_file():
                            rm_f.unlink(missing_ok=True)
                    doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE archive_id = ?", (aid,)).fetchall()]
                    for did in doc_ids:
                        conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (did,))
                    conn.execute("DELETE FROM documents WHERE archive_id = ?", (aid,))
                    conn.execute("DELETE FROM archives WHERE id = ?", (aid,))
                    conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        m_imp = re.match(r"^/api/records/(\d+)$", path)
        if m_imp:
            rid = int(m_imp.group(1))
            with DB_LOCK:
                conn = get_db()
                row = conn.execute("SELECT * FROM important_records WHERE id = ?", (rid,)).fetchone()
                if row:
                    folder_abs = ws_root / row["folder_rel_path"]
                    if delete_disk and row["folder_rel_path"] and folder_abs.exists() and _is_safe_subpath(folder_abs, ws_root):
                        shutil.rmtree(folder_abs, ignore_errors=True)
                    else:
                        rm_f = folder_abs / "00_重要事项内容.md"
                        if rm_f.is_file():
                            rm_f.unlink(missing_ok=True)
                    doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE record_id = ?", (rid,)).fetchall()]
                    for did in doc_ids:
                        conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (did,))
                    conn.execute("DELETE FROM documents WHERE record_id = ?", (rid,))
                    conn.execute("DELETE FROM important_records WHERE id = ?", (rid,))
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
                    folder_abs = ws_root / row["folder_rel_path"] if row["folder_rel_path"] else None
                    if delete_disk and folder_abs and folder_abs.exists() and _is_safe_subpath(folder_abs, ws_root):
                        shutil.rmtree(folder_abs, ignore_errors=True)
                        doc_ids = [r["id"] for r in conn.execute("SELECT id FROM documents WHERE task_id = ?", (task_id,)).fetchall()]
                        for did in doc_ids:
                            conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (did,))
                        conn.execute("DELETE FROM documents WHERE task_id = ?", (task_id,))
                    else:
                        # 修复 BUG-10：未勾选删盘时，清理 00_事项要求与进展.md 锚点，使内部文件转为独立归档材料
                        if folder_abs and folder_abs.is_dir():
                            rm_f = folder_abs / "00_事项要求与进展.md"
                            if rm_f.is_file():
                                rm_f.unlink(missing_ok=True)
                        conn.execute("UPDATE documents SET task_id = NULL WHERE task_id = ?", (task_id,))
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
                        if f_abs.exists() and f_abs.is_file() and _is_safe_subpath(f_abs, ws_root):
                            f_abs.unlink(missing_ok=True)
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                    conn.execute("DELETE FROM documents_fts WHERE doc_id = ?", (doc_id,))
                    if row["task_id"]:
                        write_task_readme_file(conn, row["task_id"])
                    if row["archive_id"]:
                        write_archive_readme_file(conn, row["archive_id"])
                    if row["record_id"]:
                        write_record_readme_file(conn, row["record_id"])
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
