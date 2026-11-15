#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与材料智能工作台 (GW-Workbench) 核心服务端
专为银河麒麟 Kylin V10 (海光 3350) 与局域网跨平台协同设计
零强制第三方依赖（基于 Python 3 标准库 + SQLite3 + 内置多格式公文解析）
"""

import csv
import io
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import urllib.parse
import zipfile
from datetime import datetime, date
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
)

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
    "workspace_root": str(BASE_DIR / "workspace_files"),
    "prefix_archived_folder": False,
    "ai_api_base": "",
    "ai_api_key": "",
    "ai_model": "deepseek-chat",
    "lan_notice": "📌 欢迎访问科室公文与材料工作台！支持按【标题】或【标题+正文】秒搜公文，点开任意文件可直接预览复制全文或打包下载。",
}

DB_LOCK = threading.RLock()


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
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_workspace_root(cfg: Optional[Dict[str, Any]] = None) -> Path:
    c = cfg or load_config()
    raw = c.get("workspace_root")
    if not raw or "/working_dir/" in str(raw):
        p = (BASE_DIR / "workspace_files").resolve()
    else:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (BASE_DIR / p).resolve()
        elif not p.exists() and (BASE_DIR / "workspace_files").exists():
            p = (BASE_DIR / "workspace_files").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


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


def get_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


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
                folder_rel_path TEXT DEFAULT '',
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

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category);
            CREATE INDEX IF NOT EXISTS idx_docs_task_id ON documents(task_id);
            CREATE INDEX IF NOT EXISTS idx_docs_category ON documents(category);
            CREATE INDEX IF NOT EXISTS idx_docs_role ON documents(doc_role);
            """
        )
        conn.commit()
        conn.close()

    # 确保默认分类物理文件夹存在
    ws_root = get_workspace_root()
    for cat in DEFAULT_CATEGORIES:
        (ws_root / cat).mkdir(parents=True, exist_ok=True)


def list_physical_categories() -> List[str]:
    ws_root = get_workspace_root()
    dirs = [
        d.name for d in ws_root.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]
    dirs.sort()
    return dirs if dirs else list(DEFAULT_CATEGORIES)


def write_task_readme_file(conn: sqlite3.Connection, task_id: int) -> None:
    """
    在工作事项对应的本地物理文件夹中生成/更新 `00_事项要求与进展.md`
    保证用户不开工作台系统、直接打开本地文件夹时，也能清楚看到微信通知原文、AI提炼要求、截止时间、进展流水和文件清单
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
        f"# {row['title']}",
        "",
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


def sync_filesystem() -> Dict[str, int]:
    """
    双向物理文件夹同步引擎：
    1. 扫描每个工作事项的物理文件夹：如果你直接在本地用 WPS 新建或修改保存了材料，自动感知文件增改并重新抽提全文索引！
    2. 扫描每个分类目录的顶层独立文件：如果你在不开系统时直接把历史公文或材料放进了分类文件夹，自动入库建索引！
    3. 清理本地已删除文件的孤儿索引记录。
    """
    stats = {"added": 0, "updated": 0, "removed": 0}
    ws_root = get_workspace_root()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with DB_LOCK:
        conn = get_db()
        # 1. 建立现有文档 rel_path 映射
        existing_docs = {
            r["rel_path"]: dict(r)
            for r in conn.execute("SELECT * FROM documents").fetchall()
        }
        seen_rel_paths = set()

        # 2. 建立事项文件夹 rel_path -> task 映射
        tasks = [dict(r) for r in conn.execute("SELECT * FROM tasks").fetchall()]
        task_folder_map = {}
        for t in tasks:
            if t["folder_rel_path"]:
                folder_abs = ws_root / t["folder_rel_path"]
                folder_abs.mkdir(parents=True, exist_ok=True)
                task_folder_map[t["folder_rel_path"]] = t

        # 3. 遍历 workspace_root 下所有分类文件夹
        for cat_dir in sorted(ws_root.iterdir()):
            if not cat_dir.is_dir() or cat_dir.name.startswith("."):
                continue
            cat_name = cat_dir.name

            for entry in sorted(cat_dir.iterdir()):
                if entry.name.startswith(".") or entry.name.startswith("~$"):
                    # 忽略 WPS/Word 临时锁文件 ~$xxx.docx
                    continue

                if entry.is_dir():
                    # 检查是否是某个事项的专属文件夹
                    rel_dir = str(entry.relative_to(ws_root)).replace("\\", "/")
                    task_obj = task_folder_map.get(rel_dir)

                    for sub_file in sorted(entry.rglob("*")):
                        if not sub_file.is_file():
                            continue
                        if sub_file.name.startswith(".") or sub_file.name.startswith("~$"):
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
                            if abs(doc_rec["mtime"] - mtime) > 0.5 or doc_rec["file_size"] != fsize:
                                extracted, ftype = extract_document_text(sub_file)
                                conn.execute(
                                    """
                                    UPDATE documents
                                    SET file_size = ?, mtime = ?, extracted_text = ?, file_type = ?, updated_at = ?
                                    WHERE id = ?
                                    """,
                                    (fsize, mtime, extracted, ftype, now_str, doc_rec["id"]),
                                )
                                stats["updated"] += 1
                        else:
                            # 本地新增进事项文件夹的文件：自动判断是【来文】还是【我写的材料】
                            fname = sub_file.name
                            if fname.startswith(("[来文]", "【来文】", "来文_")):
                                role = "incoming"
                            elif task_obj:
                                role = "draft"
                            else:
                                role = "historical" if "历史" in cat_name else "draft"

                            extracted, ftype = extract_document_text(sub_file)
                            conn.execute(
                                """
                                INSERT INTO documents (
                                    task_id, filename, rel_path, category, doc_role,
                                    file_type, file_size, mtime, extracted_text,
                                    tags_json, is_starred, is_pinned, created_at, updated_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
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
                                    now_str,
                                    now_str,
                                ),
                            )
                            stats["added"] += 1

                elif entry.is_file():
                    # 分类目录顶层的独立公文或材料
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
                            stats["updated"] += 1
                    else:
                        fname = entry.name
                        if "历史" in cat_name:
                            role = "historical"
                        elif fname.startswith(("[来文]", "【来文】")) or "通知" in fname or "方案" in fname and "汇报" not in fname:
                            role = "incoming"
                        else:
                            role = "draft"
                        extracted, ftype = extract_document_text(entry)
                        conn.execute(
                            """
                            INSERT INTO documents (
                                task_id, filename, rel_path, category, doc_role,
                                file_type, file_size, mtime, extracted_text,
                                tags_json, is_starred, is_pinned, created_at, updated_at
                            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
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
                                now_str,
                                now_str,
                            ),
                        )
                        stats["added"] += 1

        # 4. 清理已在本地被移走或删除的文件记录
        for rel_p, doc_rec in existing_docs.items():
            if rel_p not in seen_rel_paths:
                abs_p = ws_root / rel_p
                if not abs_p.exists():
                    conn.execute("DELETE FROM documents WHERE id = ?", (doc_rec["id"],))
                    stats["removed"] += 1

        conn.commit()
        conn.close()
    return stats


def compute_deadline_badge(deadline_str: str, status: str) -> Dict[str, Any]:
    """计算事项剩余天数与醒目预警色标（红/黄/蓝/灰）"""
    if status == "archived":
        return {"level": "archived", "label": "已归档", "days_left": 9999}
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


def find_similar_library_docs(
    conn: sqlite3.Connection,
    title: str,
    summary: str,
    exclude_task_id: Optional[int] = None,
    limit: int = 4,
) -> List[Dict[str, Any]]:
    """
    为工作事项自动匹配【公文与材料库】中相似的历史旧稿/参考范文（Q16）
    """
    kws = extract_search_keywords(f"{title} {summary}")
    all_docs = conn.execute(
        """
        SELECT id, task_id, filename, rel_path, category, doc_role, file_type,
               is_starred, substr(extracted_text, 1, 240) AS preview, updated_at
        FROM documents
        ORDER BY is_starred DESC, id DESC
        """
    ).fetchall()

    scored = []
    for d in all_docs:
        if exclude_task_id is not None and d["task_id"] == exclude_task_id:
            continue
        score = 0
        hay_title = (d["filename"] or "").lower()
        hay_body = (d["preview"] or "").lower()
        for kw in kws:
            kl = kw.lower()
            if kl in hay_title:
                score += 6
            if kl in hay_body:
                score += 2
        # 优先加权“我写的材料”和“星标常用范文”
        if d["doc_role"] in ("draft", "template"):
            score += 2 if score > 0 else 0
        if d["is_starred"]:
            score += 3 if score > 0 else 1
        if score > 0:
            scored.append((score, dict(d)))

    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    return [item[1] for item in scored[:limit]]


def serialize_task(conn: sqlite3.Connection, row: sqlite3.Row, include_refs: bool = True) -> Dict[str, Any]:
    t = dict(row)
    t["checklist"] = json.loads(t.pop("checklist_json", "[]") or "[]")
    t["progress_logs"] = json.loads(t.pop("progress_json", "[]") or "[]")
    t["is_pinned"] = bool(t.get("is_pinned", 0))
    t["deadline_info"] = compute_deadline_badge(t.get("deadline", ""), t.get("status", "in_progress"))

    docs = conn.execute(
        """
        SELECT id, task_id, filename, rel_path, category, doc_role, file_type,
               file_size, is_starred, is_pinned, tags_json,
               substr(extracted_text, 1, 200) AS preview, updated_at
        FROM documents
        WHERE task_id = ?
        ORDER BY id ASC
        """,
        (t["id"],),
    ).fetchall()

    incoming_files = []
    drafted_files = []
    for d in docs:
        dd = dict(d)
        dd["tags"] = json.loads(dd.pop("tags_json", "[]") or "[]")
        dd["is_starred"] = bool(dd.get("is_starred", 0))
        dd["is_pinned"] = bool(dd.get("is_pinned", 0))
        if dd["doc_role"] == "incoming":
            incoming_files.append(dd)
        else:
            drafted_files.append(dd)

    t["incoming_files"] = incoming_files
    t["drafted_files"] = drafted_files
    ws_root = get_workspace_root()
    t["folder_abs_path"] = str((ws_root / t["folder_rel_path"]).resolve()) if t.get("folder_rel_path") else ""

    if include_refs:
        t["reference_docs"] = find_similar_library_docs(
            conn, t["title"], t.get("summary", ""), exclude_task_id=t["id"], limit=4
        )
    else:
        t["reference_docs"] = []
    return t


def serialize_doc(row: sqlite3.Row, snippet: str = "") -> Dict[str, Any]:
    d = dict(row)
    full_text = d.pop("extracted_text", "") or ""
    d["tags"] = json.loads(d.pop("tags_json", "[]") or "[]")
    d["is_starred"] = bool(d.get("is_starred", 0))
    d["is_pinned"] = bool(d.get("is_pinned", 0))
    d["content_preview"] = full_text[:260]
    d["snippet"] = snippet or full_text[:180].replace("\n", " ")
    ws_root = get_workspace_root()
    d["abs_path"] = str((ws_root / d["rel_path"]).resolve())
    return d


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
    执行高精度搜索：
    - scope == 'title'：仅搜事项标题/发文字号/文件名/标签
    - scope == 'full'：搜标题 + 文号 + 要求摘要 + 进展 + 公文与材料全文内容
    """
    q_clean = (query or "").strip()
    keywords = [k for k in re.split(r"\s+", q_clean) if k] if q_clean else []

    with DB_LOCK:
        conn = get_db()
        task_rows = conn.execute("SELECT * FROM tasks ORDER BY is_pinned DESC, id DESC").fetchall()
        matched_tasks = []
        for r in task_rows:
            if category and r["category"] != category:
                continue
            if not keywords:
                matched_tasks.append(serialize_task(conn, r, include_refs=False))
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
                t_obj = serialize_task(conn, r, include_refs=False)
                t_obj["match_snippet"] = build_highlight_snippet(haystack, keywords)
                matched_tasks.append(t_obj)

        doc_rows = conn.execute("SELECT * FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
        matched_docs = []
        for r in doc_rows:
            if category and r["category"] != category:
                continue
            if doc_role and r["doc_role"] != doc_role:
                continue
            if not keywords:
                matched_docs.append(serialize_doc(r))
                continue

            if scope == "title":
                haystack = f"{r['filename']} {r['tags_json']} {r['category']}"
            else:
                haystack = f"{r['filename']} {r['tags_json']} {r['category']} {r['extracted_text']}"
            hay_lower = haystack.lower()
            if all(kw.lower() in hay_lower for kw in keywords):
                snip = build_highlight_snippet(r["extracted_text"] or r["filename"], keywords)
                matched_docs.append(serialize_doc(r, snippet=snip))

        conn.close()
    return matched_tasks, matched_docs


def create_task_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    核心创建工作事项逻辑（支持网页一键投喂、AI 自动填表、多文件挂载与本地专属文件夹创建）
    """
    cfg = load_config()
    ws_root = get_workspace_root(cfg)
    categories = list_physical_categories()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_prefix = datetime.now().strftime("%Y-%m-%d")

    raw_text = (payload.get("raw_notice_text") or payload.get("text") or "").strip()
    uploaded_files: List[Dict[str, Any]] = payload.get("files") or []
    auto_ai = bool(payload.get("auto_ai", True))

    # 如果上传了公文文件，先临时提取首个来文的正文与 raw_text 合并供 AI 分析
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

    # 创建事项前若有文件且需 AI 解析，用临时文件提取文本
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

    checklist = payload.get("checklist")
    if checklist is None:
        checklist = ai_parsed.get("checklist") or [
            {"id": "chk_1", "text": f"办理《{title}》相关要求", "done": False}
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

    # 在对应分类目录下创建事项专属物理文件夹
    cat_dir = ws_root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
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
                progress_json, status, is_pinned, folder_rel_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                folder_rel_path,
                now_str,
                now_str,
            ),
        )
        task_id = cur.lastrowid

        # 保存挂载的文件至事项物理文件夹并建立全文索引
        for fname, raw_bytes, role in extracted_file_blobs:
            prefix = "[来文]_" if role == "incoming" else "[材料]_"
            if not fname.startswith(("[来文]", "[材料]", "【来文】", "【材料】")):
                save_name = prefix + fname
            else:
                save_name = fname
            dest_path = task_folder / save_name
            dest_path.write_bytes(raw_bytes)
            st = dest_path.stat()
            ext_text, ftype = extract_document_text(dest_path)
            rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
            cur.execute(
                """
                INSERT INTO documents (
                    task_id, filename, rel_path, category, doc_role,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?)
                """,
                (
                    task_id,
                    save_name,
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

        write_task_readme_file(conn, task_id)
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        result = serialize_task(conn, row)
        conn.close()
    return result


class WorkbenchHTTPRequestHandler(BaseHTTPRequestHandler):
    server_version = "GWWorkbench/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # 保持后台日志简洁
        pass

    def _is_localhost(self) -> bool:
        client_ip = self.client_address[0]
        return client_ip in ("127.0.0.1", "::1", "localhost")

    def _get_cookies(self) -> Dict[str, str]:
        raw_cookie = self.headers.get("Cookie", "")
        c = cookies.SimpleCookie()
        try:
            c.load(raw_cookie)
            return {k: v.value for k, v in c.items()}
        except Exception:
            return {}

    def _check_auth(self) -> Tuple[bool, bool]:
        """
        返回 (can_view, can_edit)
        - 本机 127.0.0.1 访问或携带正确 X-Agent-Token：默认自动具备 can_view + can_edit
        - 局域网其他电脑：验证 Cookie 或 Header 中的访问密码与主理人编辑密码
        """
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
    ) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if set_cookies:
            for k, v in set_cookies.items():
                # 记住密码 365 天
                self.send_header(
                    "Set-Cookie",
                    f"{k}={urllib.parse.quote(v)}; Path=/; Max-Age=31536000; SameSite=Lax",
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

        # 静态资源服务
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
            # 每次进入工作台自动做一次轻量双向文件变更扫描
            sync_stats = sync_filesystem()
            categories = list_physical_categories()
            with DB_LOCK:
                conn = get_db()
                rows = conn.execute("SELECT * FROM tasks ORDER BY is_pinned DESC, id DESC").fetchall()
                tasks = [serialize_task(conn, r) for r in rows]

                # 排序：进行中与待办按紧急度和剩余天数排在最前
                def task_sort_key(t: Dict[str, Any]):
                    status_order = {"in_progress": 0, "pending": 1, "archived": 2}.get(t["status"], 1)
                    pinned_order = 0 if t["is_pinned"] else 1
                    days_left = t["deadline_info"]["days_left"]
                    prio_order = {"紧急": 0, "重要": 1, "普通": 2}.get(t.get("priority", "普通"), 2)
                    return (status_order, pinned_order, days_left, prio_order, -t["id"])

                tasks.sort(key=task_sort_key)

                doc_rows = conn.execute("SELECT * FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                docs = [serialize_doc(r) for r in doc_rows]
                conn.close()

            self._send_json({
                "can_view": can_view,
                "can_edit": can_edit,
                "is_localhost": self._is_localhost(),
                "lan_url": f"http://{get_lan_ip()}:{cfg.get('port', 8090)}",
                "workspace_root": str(get_workspace_root(cfg)),
                "lan_notice": cfg.get("lan_notice", ""),
                "ai_configured": bool(cfg.get("ai_api_base")),
                "ai_model": cfg.get("ai_model", "deepseek-chat"),
                "ai_api_base": cfg.get("ai_api_base", ""),
                "prefix_archived_folder": bool(cfg.get("prefix_archived_folder", False)),
                "categories": categories,
                "tasks": tasks,
                "documents": docs,
                "sync_stats": sync_stats,
            })
            return

        if path == "/api/tasks":
            status_filter = (qs.get("status") or [""])[0]
            cat_filter = (qs.get("category") or [""])[0]
            with DB_LOCK:
                conn = get_db()
                rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
                tasks = [
                    serialize_task(conn, r)
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
            with DB_LOCK:
                conn = get_db()
                rows = conn.execute("SELECT * FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
                docs = []
                for r in rows:
                    if cat_filter and r["category"] != cat_filter:
                        continue
                    if role_filter and r["doc_role"] != role_filter:
                        continue
                    if starred_only and not r["is_starred"]:
                        continue
                    docs.append(serialize_doc(r))
                conn.close()
            self._send_json({"documents": docs})
            return

        # 预览单个文件完整正文
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
            d = serialize_doc(row)
            d["extracted_text"] = row["extracted_text"] or ""
            self._send_json({"document": d})
            return

        # 下载单个原文件
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

        # 打包下载整个工作事项文件夹 (.zip)
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
                        if f.is_file() and not f.name.startswith("~$"):
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

        # 搜索接口 (支持仅标题 or 标题+全文)
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

        # 导出工作台账 (.csv / .md)
        if path == "/api/export/ledger":
            fmt = (qs.get("format") or ["csv"])[0].lower()
            status_filter = (qs.get("status") or [""])[0]
            cat_filter = (qs.get("category") or [""])[0]
            with DB_LOCK:
                conn = get_db()
                rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
                tasks = [
                    serialize_task(conn, r, include_refs=False)
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
                    "事项名称", "优先级", "截止日期", "核心要求摘要", "待办清单完成情况",
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
                        t.get("summary", ""),
                        chk_str,
                        prog_str,
                        inc_str,
                        drf_str,
                        t.get("folder_rel_path", ""),
                        t.get("created_at", ""),
                    ])
                # 使用 UTF-8-SIG 带 BOM 编码，确保银河麒麟 WPS 表格与 Windows Excel 直接双击打开不乱码
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

        can_view, can_edit = self._check_auth()
        if not can_view:
            self._send_json({"error": "未登录"}, status=401)
            return

        # AI 搜索与本地 Agent 搜索对所有已登录用户开放
        if path in ("/api/ai/search", "/api/agent/context-search"):
            query = str(body.get("query") or "").strip()
            category = str(body.get("category") or "").strip()
            kws = extract_search_keywords(query)
            # 多关键词召回（匹配任意核心词即可进入初筛池，按命中词数排序）
            with DB_LOCK:
                conn = get_db()
                all_tasks = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
                scored_tasks = []
                for r in all_tasks:
                    if category and r["category"] != category:
                        continue
                    hay = f"{r['title']} {r['doc_number']} {r['issuing_org']} {r['summary']} {r['raw_notice_text']} {r['progress_json']}".lower()
                    hits = sum(2 if kw.lower() in r["title"].lower() else (1 if kw.lower() in hay else 0) for kw in kws)
                    if hits > 0:
                        t_obj = serialize_task(conn, r, include_refs=False)
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
                        d_obj = serialize_doc(r, snippet=snip)
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

        # 以下为写操作，需编辑权限
        if not can_edit:
            self._send_json({"error": "当前为【浏览与下载模式】，请点击右上角开启【主理人编辑模式】后再修改"}, status=403)
            return

        if path == "/api/sync":
            stats = sync_filesystem()
            self._send_json({"ok": True, "stats": stats})
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
                    dest = task_folder / save_name
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
                            save_name,
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
                write_task_readme_file(conn, task_id)
                conn.commit()
                updated_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                res_task = serialize_task(conn, updated_row)
                conn.close()
            self._send_json({"ok": True, "task": res_task})
            return

        if path == "/api/docs/upload":
            # 在独立【公文与材料库】直接上传文件或批量拖入历史公文
            category = sanitize_filename(body.get("category") or "05_历史公文归档")
            doc_role = body.get("doc_role") or "draft"
            tags = body.get("tags") or []
            files_list = body.get("files") or []
            ws_root = get_workspace_root(cfg)
            cat_dir = ws_root / category
            cat_dir.mkdir(parents=True, exist_ok=True)
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
                    dest = cat_dir / fname
                    dest.write_bytes(raw_bytes)
                    st = dest.stat()
                    ext_txt, ftype = extract_document_text(dest)
                    rel_file = str(dest.relative_to(ws_root)).replace("\\", "/")
                    cur = conn.execute(
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
                            fname,
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
                    row = conn.execute("SELECT * FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
                    if row:
                        added_docs.append(serialize_doc(row))
                conn.commit()
                conn.close()
            self._send_json({"ok": True, "documents": added_docs})
            return

        if path == "/api/docs/scan-local-dir":
            # 批量扫描本机某个历史公文文件夹并导入归档库（Q6 & Q11）
            src_dir_str = str(body.get("source_dir") or "").strip()
            target_cat = sanitize_filename(body.get("category") or "05_历史公文归档")
            doc_role = body.get("doc_role") or "historical"
            if not src_dir_str:
                self._send_json({"error": "请输入本机文件夹路径"}, status=400)
                return
            src_dir = Path(src_dir_str).expanduser().resolve()
            if not src_dir.exists() or not src_dir.is_dir():
                self._send_json({"error": f"本机目录不存在: {src_dir}"}, status=400)
                return

            ws_root = get_workspace_root(cfg)
            dest_cat_dir = ws_root / target_cat
            dest_cat_dir.mkdir(parents=True, exist_ok=True)
            allowed_exts = {".docx", ".doc", ".wps", ".ofd", ".pdf", ".txt", ".md", ".xlsx", ".xls", ".csv"}
            imported_count = 0
            for f in src_dir.rglob("*"):
                if f.is_file() and not f.name.startswith(".") and not f.name.startswith("~$"):
                    if f.suffix.lower() in allowed_exts:
                        target_f = dest_cat_dir / sanitize_filename(f.name)
                        if target_f.resolve() != f.resolve():
                            shutil.copy2(f, target_f)
                        imported_count += 1
            sync_stats = sync_filesystem()
            self._send_json({
                "ok": True,
                "imported_count": imported_count,
                "sync_stats": sync_stats,
                "target_category": target_cat,
            })
            return

        # 历史公文/库内文件一键转为工作台待办事项
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
            (ws_root / cat_name).mkdir(parents=True, exist_ok=True)
            self._send_json({"ok": True, "categories": list_physical_categories()})
            return

        if path == "/api/ai/summary":
            # 阶段性工作总结生成（Q20）
            period_label = str(body.get("period_label") or "近期阶段工作").strip()
            category_filter = str(body.get("category") or "").strip()
            with DB_LOCK:
                conn = get_db()
                t_rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
                tasks = [
                    serialize_task(conn, r, include_refs=False)
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
            for k in (
                "view_password", "edit_password", "workspace_root",
                "prefix_archived_folder", "ai_api_base", "ai_api_key",
                "ai_model", "lan_notice",
            ):
                if k in body and body[k] is not None:
                    cfg[k] = body[k]
            save_config(cfg)
            init_db()
            self._send_json({"ok": True, "config": cfg})
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

                # 处理快速追加进展
                progress_list = json.loads(t["progress_json"] or "[]")
                if body.get("append_progress"):
                    progress_list.insert(0, {
                        "time": datetime.now().strftime("%m-%d %H:%M"),
                        "text": str(body["append_progress"]).strip(),
                    })
                elif body.get("progress_logs") is not None:
                    progress_list = body["progress_logs"]

                # 处理子清单勾选或修改
                checklist = json.loads(t["checklist_json"] or "[]")
                if body.get("toggle_check_id"):
                    cid = body["toggle_check_id"]
                    for item in checklist:
                        if item.get("id") == cid:
                            item["done"] = not bool(item.get("done"))
                elif body.get("append_check_text"):
                    checklist.append({
                        "id": f"chk_{int(time.time()*1000)}",
                        "text": str(body["append_check_text"]).strip(),
                        "done": False,
                    })
                elif body.get("checklist") is not None:
                    checklist = body["checklist"]

                # 如果修改了所属分类目录，真实移动本地物理文件夹到新分类目录下（Q13 & Q19）
                old_rel_folder = t["folder_rel_path"]
                new_rel_folder = old_rel_folder
                if old_rel_folder:
                    old_abs = ws_root / old_rel_folder
                    folder_leaf = Path(old_rel_folder).name
                    if cfg.get("prefix_archived_folder"):
                        if new_status == "archived" and not folder_leaf.startswith("[已办]_"):
                            folder_leaf = "[已办]_" + folder_leaf
                        elif new_status != "archived" and folder_leaf.startswith("[已办]_"):
                            folder_leaf = folder_leaf.replace("[已办]_", "", 1)

                    target_cat_dir = ws_root / new_cat
                    target_cat_dir.mkdir(parents=True, exist_ok=True)
                    new_abs = target_cat_dir / folder_leaf
                    if old_abs.exists() and old_abs.resolve() != new_abs.resolve():
                        if not new_abs.exists():
                            shutil.move(str(old_abs), str(new_abs))
                            new_rel_folder = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                            # 同步更新该事项下所有文档的 rel_path 与 category
                            docs_in_task = conn.execute(
                                "SELECT id, rel_path FROM documents WHERE task_id = ?", (task_id,)
                            ).fetchall()
                            for dr in docs_in_task:
                                old_doc_rel = dr["rel_path"]
                                if old_doc_rel.startswith(old_rel_folder + "/"):
                                    updated_doc_rel = new_rel_folder + old_doc_rel[len(old_rel_folder):]
                                    conn.execute(
                                        "UPDATE documents SET rel_path = ?, category = ?, updated_at = ? WHERE id = ?",
                                        (updated_doc_rel, new_cat, now_str, dr["id"]),
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
                        new_rel_folder,
                        now_str,
                        task_id,
                    ),
                )
                write_task_readme_file(conn, task_id)
                conn.commit()
                updated_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
                res_task = serialize_task(conn, updated_row)
                conn.close()
            self._send_json({"ok": True, "task": res_task})
            return

        # 更新【公文与材料库】中的单份文件属性（重命名、移动分类目录、打标签、星标常用范文、置顶）
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

                new_rel_path = d["rel_path"]
                if old_abs.exists():
                    # 若为独立库文件且改了分类或文件名，同步移动本地物理文件
                    if d["task_id"] is None and (new_cat != d["category"] or new_filename != d["filename"]):
                        target_dir = ws_root / new_cat
                        target_dir.mkdir(parents=True, exist_ok=True)
                        new_abs = target_dir / new_filename
                        if old_abs.resolve() != new_abs.resolve():
                            shutil.move(str(old_abs), str(new_abs))
                            new_rel_path = str(new_abs.relative_to(ws_root)).replace("\\", "/")
                    elif d["task_id"] is not None and new_filename != d["filename"]:
                        new_abs = old_abs.parent / new_filename
                        if old_abs.resolve() != new_abs.resolve():
                            shutil.move(str(old_abs), str(new_abs))
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
                if d["task_id"]:
                    write_task_readme_file(conn, d["task_id"])
                conn.commit()
                u_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                res_doc = serialize_doc(u_row)
                conn.close()
            self._send_json({"ok": True, "document": res_doc})
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
                    if row["task_id"]:
                        write_task_readme_file(conn, row["task_id"])
                    conn.commit()
                conn.close()
            self._send_json({"ok": True})
            return

        self._send_json({"error": f"未知接口: {path}"}, status=404)


def seed_demo_data_if_empty() -> None:
    """首次启动时，若数据库为空，自动生成贴合防灾减灾与日常办文场景的示范事项与本地公文材料"""
    with DB_LOCK:
        conn = get_db()
        cnt = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        conn.close()
    if cnt > 0:
        return

    # 1. 创建一条示范上级来文事项（含模拟来文与已起草汇报材料）
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
        "1. 强化监测预警与部门联合叫应机制；\n"
        "2. 已完成县级救灾物资库清点，现储备棉被2800床、棉大衣2200件、折叠床1000张，帐篷600顶，账物相符；\n"
        "3. 针对山区道路、老旧大棚及敬老院开展防冻防风专项巡查。"
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

    # 2. 创建一条微信群通知示范事项
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

    # 3. 在【03_综合汇报与总结】及【05_历史公文归档】放入示范历史参考范文，方便测试“历史旧稿智能推荐”与全文搜索
    ws_root = get_workspace_root()
    ref_file = ws_root / "03_综合汇报与总结" / "【范文模板】全县防灾减灾救灾工作阶段总结汇报模板.md"
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
    sync_filesystem()
    # 将该范文标记为星标常用范文
    with DB_LOCK:
        conn = get_db()
        conn.execute(
            "UPDATE documents SET is_starred = 1, doc_role = 'template', tags_json = ? WHERE filename LIKE '%阶段总结汇报模板%'",
            (json.dumps(["防灾减灾", "总结汇报", "常用范文"], ensure_ascii=False),),
        )
        conn.commit()
        conn.close()


def run_server(port: Optional[int] = None) -> None:
    cfg = load_config()
    init_db()
    seed_demo_data_if_empty()
    sync_filesystem()

    listen_port = port or int(cfg.get("port", 8090))
    lan_ip = get_lan_ip()
    server = ThreadingHTTPServer(("0.0.0.0", listen_port), WorkbenchHTTPRequestHandler)
    print("=" * 68)
    print("  🚀 公文与材料智能工作台 (GW-Workbench v1.0.0) 已启动")
    print(f"  👉 本机主理人直连 (自动免密编辑): http://127.0.0.1:{listen_port}")
    print(f"  👉 局域网同事访问地址 (默认密码 {cfg.get('view_password')}): http://{lan_ip}:{listen_port}")
    print(f"  📂 本地物理资料库目录: {get_workspace_root(cfg)}")
    print("=" * 68)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
        server.server_close()


if __name__ == "__main__":
    import sys
    custom_port = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    run_server(custom_port)
