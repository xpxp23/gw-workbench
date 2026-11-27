#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与材料智能工作台 (GW-Workbench v3.3.0) 本地 Agent 专用命令行工具 & 原生 MCP Server
支持：
1. 命令行直接操作 (list / add / progress / attach / draft / format-gw / archive / record / recurring / search / sync)
2. 原生 Model Context Protocol (MCP) Server 协议 (`python3 wb.py mcp`)，基于标准 stdio JSON-RPC 2.0，
   内置 15 个原生政务工具（含【📦 资料存档】、【📌 重要事项记录】、任务附件与子目录挂载等全套 Agent 读写接口），
   可直接挂载至 Claude Desktop / Cline / Roo Code / Cursor / OpenCode 等本地 Agent 客户端！
"""

import argparse
import contextlib
import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from server import (
    CONFIG_PATH,
    DATA_DIR,
    DB_PATH,
    DEFAULT_LIBRARY_CATEGORY,
    DOC_LIGHT_COLS,
    VERSION as SERVER_VERSION,
    _bytes_content_sig,
    _file_content_sig,
    _is_safe_subpath,
    _move_task_folder_to_category,
    _sync_fts_doc,
    append_files_to_archive,
    build_highlight_snippet,
    check_and_spawn_all_recurring,
    create_archive_record,
    create_task_record,
    db_conn,
    ensure_category_dir,
    execute_search,
    get_workspace_root,
    init_db,
    list_physical_categories,
    load_config,
    resolve_unique_dest_path,
    sanitize_filename,
    serialize_archive,
    serialize_doc_light,
    serialize_record,
    serialize_task,
    spawn_task_from_recurring_rule,
    sync_filesystem,
    upsert_important_record,
    write_task_readme_file,
)
from ai_engine import extract_search_keywords, synthesize_ai_search_answer
from gw_formatter import build_gb9704_docx
from parser import extract_document_text, extract_structured_preview

VERSION = "3.3.0" if SERVER_VERSION == "3.3.0" else "3.3.0"

_CONN_REFUSED_ERRNOS = {
    errno.ECONNREFUSED,
    errno.ENOENT,
    61,     # macOS ECONNREFUSED
    111,    # Linux ECONNREFUSED
    10061,  # Windows WSAECONNREFUSED
}

_SENSITIVE_SYSTEM_DIRS = (
    Path("/etc"),
    Path("/private/etc"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/boot"),
    Path("/root"),
)

_SENSITIVE_HOME_REL_PATHS = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".docker",
    ".netrc",
    ".git-credentials",
    ".pypirc",
    ".npmrc",
    ".bash_history",
    ".zsh_history",
)

_SENSITIVE_FILENAMES = {
    "passwd",
    "shadow",
    "gshadow",
    "sudoers",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "authorized_keys",
    "known_hosts",
    ".env",
    "workbench.db",
    "workbench.db-wal",
    "workbench.db-shm",
}


def _is_sensitive_source_path(path: Any) -> bool:
    """
    安全拦截：阻止将系统敏感文件、用户凭据目录或工作台自身数据库/配置文件拷贝入工作区。
    """
    raw_str = str(path or "").strip()
    if not raw_str:
        return True

    norm_raw = raw_str.replace("\\", "/")
    # 拦截明确指向 data/workbench.db 或 data/config.json 的相对/绝对路径
    if norm_raw.endswith("/data/workbench.db") or norm_raw == "data/workbench.db":
        return True
    if norm_raw.endswith("/data/config.json") or norm_raw == "data/config.json":
        return True

    try:
        expanded = Path(raw_str).expanduser()
        resolved = expanded.resolve()
    except Exception:
        return True

    candidates = [expanded, resolved]
    # 1. 拦截工作台自身 data 目录下的所有内部文件（workbench.db / config.json 等）
    try:
        data_root = DATA_DIR.resolve()
        db_res = DB_PATH.resolve()
        cfg_res = CONFIG_PATH.resolve()
        for cand in candidates:
            try:
                c_res = cand.resolve()
            except Exception:
                c_res = cand
            if c_res == db_res or c_res == cfg_res or _is_safe_subpath(c_res, data_root):
                return True
    except Exception:
        pass

    # 2. 拦截操作系统敏感目录（/etc, /proc, /sys, /dev, /boot, /root 等）
    for cand in candidates:
        c_str = str(cand).replace("\\", "/")
        if re.match(r"^[a-zA-Z]:/windows/", c_str, flags=re.IGNORECASE):
            return True
        for sys_dir in _SENSITIVE_SYSTEM_DIRS:
            try:
                if cand == sys_dir or sys_dir in cand.parents:
                    return True
            except Exception:
                pass
            try:
                if sys_dir.exists() and _is_safe_subpath(cand, sys_dir.resolve()):
                    return True
            except Exception:
                pass

    # 3. 拦截用户家目录下的密钥/凭据目录与文件（~/.ssh, ~/.aws, ~/.gnupg 等）
    try:
        home_dir = Path.home().resolve()
        for rel in _SENSITIVE_HOME_REL_PATHS:
            sens_p = (home_dir / rel).resolve()
            for cand in candidates:
                try:
                    c_res = cand.resolve()
                except Exception:
                    c_res = cand
                if c_res == sens_p or _is_safe_subpath(c_res, sens_p):
                    return True
    except Exception:
        pass

    # 4. 拦截高危文件名（如 id_rsa / shadow / .env 等）
    for cand in candidates:
        if cand.name.lower() in _SENSITIVE_FILENAMES:
            return True

    return False


def _is_connection_refused_error(exc: BaseException) -> bool:
    """仅当底层异常明确为连接拒绝/套接字不存在（本地服务未启动）时返回 True。"""
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _CONN_REFUSED_ERRNOS:
        return True
    return False


def _stream_file_md5(path: Path, chunk_size: int = 65536) -> str:
    """分块计算文件 MD5，避免大文件整块读入内存导致 OOM。"""
    h = hashlib.md5()
    with open(path, "rb") as fp:
        while True:
            chunk = fp.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _resolve_unique_dest_path_stream(dest_dir: Path, filename: str, src_path: Path) -> Path:
    """
    流式防同名覆盖：基于文件大小与分块 MD5 比对内容，绝不调用 src_path.read_bytes() 避免大文件 OOM。
    """
    safe_name = sanitize_filename(filename)
    candidate = dest_dir / safe_name
    if not candidate.exists():
        return candidate

    try:
        src_size = src_path.stat().st_size
    except Exception:
        src_size = -1
    src_md5: Optional[str] = None

    def _same_content(other: Path) -> bool:
        nonlocal src_md5
        if src_size < 0 or not other.is_file():
            return False
        try:
            if other.stat().st_size != src_size:
                return False
            if src_md5 is None:
                src_md5 = _stream_file_md5(src_path)
            return _stream_file_md5(other) == src_md5
        except Exception:
            return False

    if _same_content(candidate):
        return candidate

    stem = Path(safe_name).stem
    suffix = Path(safe_name).suffix
    counter = 2
    while True:
        alt = dest_dir / f"{stem}_{counter}{suffix}"
        if not alt.exists():
            return alt
        if _same_content(alt):
            return alt
        counter += 1


def _collect_safe_local_files(
    file_paths: Optional[List[str]],
    doc_role: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """校验并收集本地文件路径，命中敏感路径时立即返回错误信息。"""
    files: List[Dict[str, Any]] = []
    for fp in (file_paths or []):
        if _is_sensitive_source_path(fp):
            return [], f"安全拦截：禁止导入敏感路径文件 ({fp})"
        p = Path(fp).expanduser().resolve()
        if p.is_file():
            item: Dict[str, Any] = {"filename": p.name, "local_source_path": str(p)}
            if doc_role:
                item["doc_role"] = doc_role
            files.append(item)
    return files, None


def api_call(method: str, path: str, payload: dict = None, timeout: int = 60) -> Optional[dict]:
    """
    所有 CLI / MCP 命令优先调用正在运行的本地 HTTP 服务：
    - 仅当底层异常明确为 ConnectionRefusedError（或其 reason 是 ConnectionRefusedError / ECONNREFUSED / ENOENT）时返回 None 退回离线直连；
    - 对于超时、ConnectionResetError、RemoteDisconnected、JSONDecodeError 或 HTTP 4xx/5xx，
      一律返回明确错误对象 {"ok": False, "error": ...}，绝不静默退回离线重复写库！
    """
    cfg = load_config()
    port = cfg.get("port", 8090)
    url = f"http://127.0.0.1:{port}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Token": cfg.get("agent_token", "gw-local-agent-token"),
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8")
            parsed = json.loads(raw_body)
            if isinstance(parsed, dict):
                return parsed
            return {"ok": False, "error": "本地服务返回非对象 JSON 响应，已取消离线重试以防双写"}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode("utf-8", errors="ignore"))
            if isinstance(err_body, dict):
                err_body.setdefault("ok", False)
                err_body.setdefault("error", f"本地服务返回 HTTP {e.code}")
                return err_body
        except Exception:
            pass
        return {"ok": False, "error": f"本地服务返回 HTTP {e.code}"}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if reason is not None and _is_connection_refused_error(reason):
            return None
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return {"ok": False, "error": f"本地服务响应超时（>{timeout}s），已取消离线重试以防双写"}
        if isinstance(reason, (ConnectionResetError, http.client.RemoteDisconnected)):
            return {"ok": False, "error": f"本地服务连接中断（{type(reason).__name__}），已取消离线重试以防双写"}
        return {"ok": False, "error": f"本地服务请求失败（{reason or e}），已取消离线重试以防双写"}
    except ConnectionRefusedError:
        return None
    except (TimeoutError, socket.timeout):
        return {"ok": False, "error": f"本地服务响应超时（>{timeout}s），已取消离线重试以防双写"}
    except (ConnectionResetError, http.client.RemoteDisconnected) as e:
        return {"ok": False, "error": f"本地服务连接中断（{type(e).__name__}），已取消离线重试以防双写"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"本地服务返回非法 JSON（{e}），已取消离线重试以防双写"}
    except OSError as e:
        if _is_connection_refused_error(e):
            return None
        return {"ok": False, "error": f"本地服务网络异常（{type(e).__name__}: {e}），已取消离线重试以防双写"}
    except Exception as e:
        return {"ok": False, "error": f"本地服务通信异常（{type(e).__name__}: {e}），已取消离线重试以防双写"}


def _do_list(status: str = "", category: str = "") -> Dict[str, Any]:
    init_db()
    qs = urllib.parse.urlencode({"status": status, "category": category})
    res = api_call("GET", f"/api/tasks?{qs}")
    if res is not None:
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        if "tasks" in res:
            return {"ok": True, "version": VERSION, "count": len(res["tasks"]), "tasks": res["tasks"]}
        return res

    sync_filesystem(force=False)
    ws_root = get_workspace_root()
    with db_conn() as conn:
        doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents ORDER BY is_starred DESC, id DESC").fetchall()
        docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
        rows = conn.execute("SELECT * FROM tasks ORDER BY is_pinned DESC, id DESC").fetchall()
        tasks = []
        for r in rows:
            if status and r["status"] != status:
                continue
            if category and r["category"] != category:
                continue
            tasks.append(serialize_task(conn, r, preloaded_docs=docs, ws_root=ws_root))
    return {"ok": True, "version": VERSION, "count": len(tasks), "tasks": tasks}


def _do_add(payload: Dict[str, Any]) -> Dict[str, Any]:
    init_db()
    res = api_call("POST", "/api/tasks", payload)
    if res is None:
        task_obj = create_task_record(payload)
        res = {"ok": True, "task": task_obj}
    return res


def _do_progress(task_id: int, text: str = "", status: str = "", check_id: str = "") -> Dict[str, Any]:
    init_db()
    payload: Dict[str, Any] = {}
    if text:
        payload["append_progress"] = text
    if status:
        payload["status"] = status
    if check_id:
        payload["toggle_check_id"] = check_id

    res = api_call("PUT", f"/api/tasks/{task_id}", payload)
    if res is None:
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return {"ok": False, "error": f"事项 #{task_id} 不存在"}
            prog = json.loads(row["progress_json"] or "[]")
            if text:
                prog.insert(0, {"time": datetime.now().strftime("%m-%d %H:%M"), "text": text})
            chk = json.loads(row["checklist_json"] or "[]")
            if check_id:
                for c in chk:
                    if c.get("id") == check_id:
                        c["done"] = not bool(c.get("done"))
            new_st = status or row["status"]
            comp_at = row["completed_at"] or ""
            if new_st == "archived" and row["status"] != "archived":
                comp_at = now_str
            elif new_st != "archived":
                comp_at = ""

            new_cat = row["category"]
            new_rel_folder = row["folder_rel_path"] or ""
            if new_st != row["status"] and bool(cfg.get("prefix_archived_folder")):
                new_cat, new_rel_folder = _move_task_folder_to_category(
                    conn,
                    ws_root,
                    task_id,
                    row["category"],
                    new_title=row["title"],
                    new_status=new_st,
                    prefix_archived=True,
                )

            conn.execute(
                """
                UPDATE tasks
                SET progress_json = ?, checklist_json = ?, status = ?, completed_at = ?,
                    category = ?, folder_rel_path = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(prog, ensure_ascii=False),
                    json.dumps(chk, ensure_ascii=False),
                    new_st,
                    comp_at,
                    new_cat,
                    new_rel_folder,
                    now_str,
                    task_id,
                ),
            )
            write_task_readme_file(conn, task_id)
            conn.commit()
            u_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            res = {"ok": True, "task": serialize_task(conn, u_row, ws_root=ws_root)}
    return res


def _do_attach(
    task_id: int,
    file_path: str,
    role: str = "draft",
    sub_folder: str = "",
) -> Dict[str, Any]:
    init_db()
    if _is_sensitive_source_path(file_path):
        return {"ok": False, "error": f"安全拦截：禁止导入敏感路径文件 ({file_path})"}
    p = Path(file_path).expanduser().resolve()
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在: {p}"}

    role_clean = role if role in ("draft", "incoming") else "draft"
    raw_sf = str(sub_folder or "").strip()
    sub_folder_clean = sanitize_filename(raw_sf) if raw_sf else ""

    # 在线分支优先使用本地路径引用（避免整文件读入内存 OOM），对齐 server.py v3.3.0
    payload = {
        "doc_role": role_clean,
        "sub_folder": sub_folder_clean,
        "files": [
            {
                "filename": p.name,
                "local_source_path": str(p),
                "doc_role": role_clean,
                "sub_folder": sub_folder_clean,
            }
        ],
    }
    res = api_call("POST", f"/api/tasks/{task_id}/files", payload)
    if res is None:
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_conn() as conn:
            t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not t_row:
                return {"ok": False, "error": f"事项 #{task_id} 不存在"}
            task_dir = ws_root / t_row["folder_rel_path"]
            target_dir = (task_dir / sub_folder_clean) if sub_folder_clean else task_dir
            if not _is_safe_subpath(target_dir, task_dir):
                return {"ok": False, "error": "非法子文件夹名称"}
            target_dir.mkdir(parents=True, exist_ok=True)
            if sub_folder_clean:
                gk = target_dir / ".gitkeep"
                if not gk.exists():
                    try:
                        gk.write_text("", encoding="utf-8")
                    except Exception:
                        pass

            dest = _resolve_unique_dest_path_stream(target_dir, sanitize_filename(p.name), p)
            if p.resolve() != dest.resolve():
                shutil.copy2(p, dest)

            st = dest.stat()
            ext_txt, ftype = extract_document_text(dest)
            csig = _file_content_sig(dest)
            rel_file = str(dest.relative_to(ws_root)).replace("\\", "/")

            conn.execute(
                """
                INSERT INTO documents (
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role, sub_folder,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at, content_hash, role_locked
                ) VALUES (?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', 0, 0, ?, ?, ?, 1)
                ON CONFLICT(rel_path) DO UPDATE SET
                    task_id=excluded.task_id,
                    filename=excluded.filename,
                    category=excluded.category,
                    doc_role=excluded.doc_role,
                    sub_folder=excluded.sub_folder,
                    file_type=excluded.file_type,
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    extracted_text=excluded.extracted_text,
                    content_hash=excluded.content_hash,
                    role_locked=1,
                    updated_at=excluded.updated_at
                """,
                (
                    task_id,
                    dest.name,
                    rel_file,
                    t_row["category"],
                    role_clean,
                    sub_folder_clean,
                    ftype,
                    st.st_size,
                    round(st.st_mtime, 2),
                    ext_txt,
                    now_str,
                    now_str,
                    csig,
                ),
            )
            r_doc = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
            if r_doc:
                _sync_fts_doc(conn, r_doc["id"])
            write_task_readme_file(conn, task_id)
            conn.commit()
            u_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            res = {
                "ok": True,
                "task": serialize_task(conn, u_row, include_refs=False, ws_root=ws_root),
                "document": serialize_doc_light(r_doc, ws_root=ws_root) if r_doc else None,
            }
    return res


def _do_write_draft(
    task_id: Optional[int],
    filename: str,
    content: str,
    category: str = "",
    export_gb9704_docx: bool = False,
    gw_mode: str = "report",
    sub_folder: str = "",
) -> Dict[str, Any]:
    init_db()
    raw_sf = str(sub_folder or "").strip()
    sub_folder_clean = sanitize_filename(raw_sf) if raw_sf else ""
    payload = {
        "task_id": task_id,
        "filename": filename,
        "content": content,
        "category": category,
        "sub_folder": sub_folder_clean,
    }
    res = api_call("POST", "/api/docs/save-text", payload)
    if res is None:
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fname = sanitize_filename(filename or "材料初稿.md")
        if not fname.lower().endswith((".md", ".txt", ".markdown")):
            fname += ".md"
        content_str = str(content or "")
        content_bytes = content_str.encode("utf-8")
        content_sig = _bytes_content_sig(content_bytes)

        with db_conn() as conn:
            if task_id:
                t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
                if not t_row:
                    return {"ok": False, "error": f"事项 #{task_id} 不存在"}
                task_folder = ws_root / t_row["folder_rel_path"]
                target_dir = (task_folder / sub_folder_clean) if sub_folder_clean else task_folder
                if not _is_safe_subpath(target_dir, task_folder):
                    return {"ok": False, "error": "非法子文件夹名称"}
                target_dir.mkdir(parents=True, exist_ok=True)
                if sub_folder_clean:
                    gk = target_dir / ".gitkeep"
                    if not gk.exists():
                        try:
                            gk.write_text("", encoding="utf-8")
                        except Exception:
                            pass
                dest_path = resolve_unique_dest_path(target_dir, fname, content_bytes)
                cat_use = t_row["category"]
                sf_use = sub_folder_clean
            else:
                cat_use = sanitize_filename(category or DEFAULT_LIBRARY_CATEGORY)
                cat_dir = ensure_category_dir(ws_root, cat_use, scope="task")
                dest_path = resolve_unique_dest_path(cat_dir, fname, content_bytes)
                sf_use = ""

            dest_path.write_bytes(content_bytes)
            st = dest_path.stat()
            rel_file = str(dest_path.relative_to(ws_root)).replace("\\", "/")
            conn.execute(
                """
                INSERT INTO documents (
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role, sub_folder,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at, content_hash
                ) VALUES (?, NULL, NULL, ?, ?, ?, 'draft', ?, '文本/Markdown', ?, ?, ?, '[]', 0, 0, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    task_id=excluded.task_id,
                    filename=excluded.filename,
                    category=excluded.category,
                    doc_role='draft',
                    sub_folder=excluded.sub_folder,
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    extracted_text=excluded.extracted_text,
                    content_hash=excluded.content_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    int(task_id) if task_id else None,
                    dest_path.name,
                    rel_file,
                    cat_use,
                    sf_use,
                    st.st_size,
                    round(st.st_mtime, 2),
                    content_str,
                    now_str,
                    now_str,
                    content_sig,
                ),
            )
            row_doc = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
            doc_id = int(row_doc["id"])
            _sync_fts_doc(conn, doc_id)
            u_task = None
            if task_id:
                write_task_readme_file(conn, int(task_id))
            conn.commit()
            if task_id:
                u_task = serialize_task(
                    conn,
                    conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone(),
                    include_refs=False,
                    ws_root=ws_root,
                )
            u_doc = serialize_doc_light(row_doc, ws_root=ws_root)
            u_doc["extracted_text"] = content_str
            res = {"ok": True, "document": u_doc}
            if task_id:
                res["task"] = u_task

    if export_gb9704_docx and res.get("ok") and res.get("document"):
        doc_id = res["document"]["id"]
        gw_res = _do_format_gw(doc_id, mode=gw_mode)
        res["gb9704_docx"] = gw_res.get("document")
    return res


def _do_format_gw(doc_id: int, mode: str = "report", doc_number: str = "", org: str = "") -> Dict[str, Any]:
    init_db()
    payload = {"mode": mode, "doc_number": doc_number, "issuing_org": org}
    res = api_call("POST", f"/api/docs/{doc_id}/format-gw", payload)
    if res is None:
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        default_org = cfg.get("default_org_name") or "县应急管理局"
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with db_conn() as conn:
            d_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if not d_row:
                return {"ok": False, "error": f"文档 #{doc_id} 不存在"}
            d_dict = dict(d_row)
            t_row = conn.execute("SELECT doc_number FROM tasks WHERE id = ?", (d_dict["task_id"],)).fetchone() if d_dict["task_id"] else None
            t_doc_num = (t_row["doc_number"] or "") if t_row else ""

        src_abs = ws_root / d_dict["rel_path"]
        stem = re.sub(r"^(\[材料\]_|\[来文\]_|【材料】_|【来文】_)+", "", Path(d_dict["filename"]).stem)
        out_abs = resolve_unique_dest_path(src_abs.parent, sanitize_filename(f"{stem}_国标排版.docx"), b"")
        effective_doc_num = doc_number if mode == "redhead" else ""
        if mode == "redhead" and not effective_doc_num:
            effective_doc_num = t_doc_num
        build_gb9704_docx(
            output_path=out_abs,
            raw_text=d_dict["extracted_text"] or "",
            title=stem,
            doc_number=effective_doc_num,
            issuing_org=org or default_org,
            mode=mode,
            xkl_gw_cli=cfg.get("xkl_gw_cli", ""),
        )
        st = out_abs.stat()
        ext_text, ftype = extract_document_text(out_abs)
        csig = _file_content_sig(out_abs)
        rel_file = str(out_abs.relative_to(ws_root)).replace("\\", "/")
        sf_val = d_dict.get("sub_folder") or ""

        with db_conn() as conn:
            conn.execute(
                """
                INSERT INTO documents (
                    task_id, archive_id, record_id, filename, rel_path, category, doc_role, sub_folder,
                    file_type, file_size, mtime, extracted_text,
                    tags_json, is_starred, is_pinned, created_at, updated_at, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                ON CONFLICT(rel_path) DO UPDATE SET
                    doc_role='draft',
                    sub_folder=excluded.sub_folder,
                    file_size=excluded.file_size,
                    mtime=excluded.mtime,
                    extracted_text=excluded.extracted_text,
                    content_hash=excluded.content_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    d_dict["task_id"],
                    d_dict["archive_id"],
                    d_dict["record_id"],
                    out_abs.name,
                    rel_file,
                    d_dict["category"],
                    sf_val,
                    ftype,
                    st.st_size,
                    round(st.st_mtime, 2),
                    ext_text,
                    json.dumps(["国标公文", "GB/T9704"], ensure_ascii=False),
                    now_str,
                    now_str,
                    csig,
                ),
            )
            r_new = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
            if r_new:
                _sync_fts_doc(conn, r_new["id"])
            if d_dict["task_id"]:
                write_task_readme_file(conn, d_dict["task_id"])
            conn.commit()
            res_doc = serialize_doc_light(r_new, ws_root=ws_root) if r_new else {"abs_path": str(out_abs)}
            res_task = (
                serialize_task(
                    conn,
                    conn.execute("SELECT * FROM tasks WHERE id = ?", (d_dict["task_id"],)).fetchone(),
                    include_refs=False,
                    ws_root=ws_root,
                )
                if d_dict["task_id"]
                else None
            )
            res = {"ok": True, "document": res_doc, "task": res_task}
    return res


# ==================== 📦 资料存档 与 📌 重要事项记录 操控函数 ====================
def _do_list_archives(category: str = "", query: str = "") -> Dict[str, Any]:
    init_db()
    qs = urllib.parse.urlencode({"category": category, "q": query})
    res = api_call("GET", f"/api/archives?{qs}")
    if res is not None:
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        if "archives" in res:
            res.setdefault("ok", True)
            return res
        return res
    sync_filesystem(force=False)
    ws_root = get_workspace_root()
    with db_conn() as conn:
        doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE archive_id IS NOT NULL ORDER BY id ASC").fetchall()
        docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
        rows = conn.execute("SELECT * FROM archives ORDER BY is_pinned DESC, archive_date DESC, id DESC").fetchall()
        q_low = (query or "").strip().lower()
        archives = []
        for r in rows:
            if category and r["category"] != category:
                continue
            a_obj = serialize_archive(conn, r, preloaded_docs=docs, ws_root=ws_root)
            if q_low:
                hay = f"{a_obj['title']} {a_obj['archive_date']} {a_obj['category']} {a_obj['notes_md']} {' '.join(f['filename'] for f in a_obj['files'])}".lower()
                if q_low not in hay:
                    continue
            archives.append(a_obj)
    return {"ok": True, "count": len(archives), "archives": archives, "categories": list_physical_categories(scope="archive")}


def _do_add_archive(
    title: str,
    category: str = "",
    archive_date: str = "",
    notes_md: str = "",
    tags: Optional[List[str]] = None,
    file_paths: Optional[List[str]] = None,
    is_pinned: bool = False,
) -> Dict[str, Any]:
    init_db()
    files, err = _collect_safe_local_files(file_paths)
    if err:
        return {"ok": False, "error": err}
    payload = {
        "title": title,
        "category": category,
        "archive_date": archive_date,
        "notes_md": notes_md,
        "tags": tags or [],
        "is_pinned": is_pinned,
        "files": files,
    }
    res = api_call("POST", "/api/archives", payload)
    if res is None:
        arch = create_archive_record(payload)
        res = {"ok": True, "archive": arch}
    return res


def _do_append_archive(archive_id: int, file_paths: Optional[List[str]] = None, notes_append: str = "") -> Dict[str, Any]:
    init_db()
    files, err = _collect_safe_local_files(file_paths)
    if err:
        return {"ok": False, "error": err}
    payload = {"files": files, "notes_append": notes_append}
    res = api_call("POST", f"/api/archives/{archive_id}/files", payload)
    if res is None:
        arch = append_files_to_archive(archive_id, files_list=files, notes_append=notes_append)
        if not arch:
            return {"ok": False, "error": f"资料存档包 #{archive_id} 不存在"}
        res = {"ok": True, "archive": arch}
    return res


def _do_list_records(category: str = "", query: str = "") -> Dict[str, Any]:
    init_db()
    qs = urllib.parse.urlencode({"category": category, "q": query})
    res = api_call("GET", f"/api/records?{qs}")
    if res is not None:
        if res.get("error"):
            return {"ok": False, "error": res["error"]}
        if "important_records" in res:
            res.setdefault("ok", True)
            return res
        return res
    sync_filesystem(force=False)
    ws_root = get_workspace_root()
    with db_conn() as conn:
        doc_rows = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE record_id IS NOT NULL ORDER BY id ASC").fetchall()
        docs = [serialize_doc_light(r, ws_root=ws_root) for r in doc_rows]
        rows = conn.execute("SELECT * FROM important_records ORDER BY is_pinned DESC, sort_order ASC, id DESC").fetchall()
        q_low = (query or "").strip().lower()
        records = []
        for r in rows:
            if category and r["category"] != category:
                continue
            r_obj = serialize_record(conn, r, preloaded_docs=docs, ws_root=ws_root, include_structured_preview=False)
            if q_low:
                hay = f"{r_obj['title']} {r_obj['category']} {r_obj['content_md']} {' '.join(f['filename'] for f in r_obj['files'])}".lower()
                if q_low not in hay:
                    continue
            records.append(r_obj)
    return {"ok": True, "count": len(records), "important_records": records, "categories": list_physical_categories(scope="record")}


def _do_get_record(id_or_title: str) -> Dict[str, Any]:
    init_db()
    val = str(id_or_title or "").strip()
    if val.isdigit():
        res = api_call("GET", f"/api/records/{int(val)}")
        if res is not None:
            if res.get("error"):
                return {"ok": False, "error": res["error"]}
            res.setdefault("ok", True)
            return res
    ws_root = get_workspace_root()
    with db_conn() as conn:
        row = None
        if val.isdigit():
            row = conn.execute("SELECT * FROM important_records WHERE id = ?", (int(val),)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM important_records WHERE title = ? OR title LIKE ?", (val, f"%{val}%")).fetchone()
        if not row:
            return {"ok": False, "error": f"未找到重要事项记录: {val}"}
        r_obj = serialize_record(conn, row, ws_root=ws_root, include_structured_preview=True)
    return {"ok": True, "record": r_obj}


def _do_upsert_record(
    title: str,
    category: str = "",
    content_md: str = "",
    tags: Optional[List[str]] = None,
    file_paths: Optional[List[str]] = None,
    is_pinned: bool = False,
    record_id: Optional[int] = None,
) -> Dict[str, Any]:
    init_db()
    files, err = _collect_safe_local_files(file_paths)
    if err:
        return {"ok": False, "error": err}
    payload: Dict[str, Any] = {
        "title": title,
        "category": category,
        "tags": tags or [],
        "is_pinned": is_pinned,
        "upsert_by_title": True,
        "files": files,
    }
    if content_md:
        payload["content_md"] = content_md
    if record_id:
        res = api_call("PUT", f"/api/records/{record_id}", payload)
    else:
        res = api_call("POST", "/api/records", payload)
    if res is None:
        rec = upsert_important_record(payload, record_id=record_id)
        res = {"ok": True, "record": rec}
    return res


def _do_search(query: str, scope: str = "full", ai: bool = False, category: str = "") -> Dict[str, Any]:
    init_db()
    if ai:
        res = api_call("POST", "/api/agent/context-search", {"query": query, "category": category})
        if res is None:
            sync_filesystem(force=False)
            kws = extract_search_keywords(query)
            tasks, docs, archives, records = execute_search(query, scope="full", category=category)
            ans = synthesize_ai_search_answer(
                query,
                kws,
                tasks[:10],
                docs[:15],
                matched_archives=archives[:8],
                matched_records=records[:8],
                config=load_config(),
            )
            res = {
                "ok": True,
                "query": query,
                "keywords": kws,
                "answer": ans["answer"],
                "tasks": tasks[:10],
                "documents": docs[:15],
                "archives": archives[:8],
                "important_records": records[:8],
            }
        elif res.get("error"):
            res["ok"] = False
        else:
            res.setdefault("ok", True)
        return res
    else:
        qs = urllib.parse.urlencode({"q": query, "scope": scope, "category": category})
        res = api_call("GET", f"/api/search?{qs}")
        if res is not None:
            if res.get("error"):
                return {"ok": False, "error": res["error"]}
            res["ok"] = True
            return res
        sync_filesystem(force=False)
        tasks, docs, archives, records = execute_search(query, scope=scope, category=category)
        return {
            "ok": True,
            "query": query,
            "scope": scope,
            "tasks": tasks,
            "documents": docs,
            "archives": archives,
            "important_records": records,
        }


# ==================== 原生 MCP Server (stdio JSON-RPC 2.0, 15 Tools) ====================
MCP_TOOLS_SPEC = [
    {
        "name": "wb_list_tasks",
        "description": "获取公文工作台中的待办与进行中工作事项列表（包含每个事项的本地文件夹路径、待办细项清单、已有关联材料及库内参考旧稿）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "archived"],
                    "description": "可选过滤状态: in_progress | pending | archived",
                },
                "category": {"type": "string", "description": "可选过滤分类目录名"},
            },
        },
    },
    {
        "name": "wb_add_task",
        "description": "将上级来文或微信群/电话通知填入公文工作台，自动建立本地事项专属文件夹与 00_事项要求与进展.md。只需填好 summary（核心要求）与 checklist（待办细项）两块内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "通知原文或公文正文内容"},
                "file_path": {"type": "string", "description": "可选：本机公文原件绝对路径 (.ofd/.docx/.pdf/.wps/.txt)"},
                "title": {"type": "string", "description": "可选：指定事项标题（留空则自动提取）"},
                "source_channel": {
                    "type": "string",
                    "enum": ["上级来文", "微信群", "电话通知", "自建"],
                    "description": "上级来文 | 微信群 | 电话通知 | 自建",
                },
                "doc_number": {"type": "string", "description": "发文字号"},
                "issuing_org": {"type": "string", "description": "来文单位"},
                "priority": {
                    "type": "string",
                    "enum": ["普通", "重要", "紧急"],
                    "description": "普通 | 重要 | 紧急",
                },
                "deadline": {"type": "string", "description": "截止日期 YYYY-MM-DD"},
                "category": {"type": "string", "description": "所属物理分类目录"},
                "summary": {"type": "string", "description": "核心要求：一段话说清谁要什么、交几件、报给谁、什么时间、什么形式"},
                "checklist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待办要求细项：逐条可勾选的动作，一条一个动作，带时间节点或报送对象",
                },
                "progress": {"type": "string", "description": "初始进展记录"},
                "auto_ai": {"type": "boolean", "description": "是否让内置规则引擎补全缺失字段"},
            },
        },
    },
    {
        "name": "wb_update_progress",
        "description": "更新某项工作的实时进展、切换待办细项勾选状态，或将其标记为已完成（归档）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "工作事项 ID"},
                "progress_text": {"type": "string", "description": "要追加的进展记录内容"},
                "status": {
                    "type": "string",
                    "enum": ["pending", "in_progress", "archived"],
                    "description": "可选新状态: pending | in_progress | archived",
                },
                "toggle_check_id": {"type": "string", "description": "可选要勾选/取消勾选的细项 ID（如 chk_1）"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "wb_attach_file",
        "description": "向指定工作事项挂载本机文件（支持指定角色为我写的材料或来文原件，以及写入任务下的单层材料子目录）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "工作事项 ID"},
                "file_path": {"type": "string", "description": "本机待挂载文件的绝对路径"},
                "role": {
                    "type": "string",
                    "enum": ["draft", "incoming"],
                    "description": "文件角色：draft (我写的材料，默认) | incoming (来文原件)",
                },
                "sub_folder": {"type": "string", "description": "可选：任务下的单层材料子目录名称（留空则放在任务根目录）"},
            },
            "required": ["task_id", "file_path"],
        },
    },
    {
        "name": "wb_write_draft",
        "description": "直接为指定工作事项（或独立分类目录）起草并保存材料草稿（.md/.txt），支持写入任务子目录并可一键排版导出为符合 GB/T 9704—2012 国标格式的 .docx 公文",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "关联的工作事项 ID（若为独立材料可不传）"},
                "filename": {"type": "string", "description": "保存的文件名，如 我县防灾减灾总结汇报_初稿.md"},
                "content": {"type": "string", "description": "撰写的完整材料正文内容"},
                "category": {"type": "string", "description": "独立材料的分类目录（如 03_综合汇报与总结）"},
                "sub_folder": {"type": "string", "description": "可选：任务下的单层材料子目录名称（仅当指定 task_id 时生效）"},
                "export_gb9704_docx": {"type": "boolean", "description": "是否同时自动生成符合 GB/T 9704—2012 国标格式的 .docx 正式文档"},
                "gw_mode": {
                    "type": "string",
                    "enum": ["report", "redhead"],
                    "description": "国标公文模式: report (汇报/总结/方案) | redhead (带红头与文号公文)",
                },
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "wb_format_gb9704_docx",
        "description": "将库内已有的任意公文或材料草稿一键排版转换为符合 GB/T 9704—2012 标准的 .docx 公文",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "integer", "description": "文档 ID"},
                "mode": {
                    "type": "string",
                    "enum": ["report", "redhead"],
                    "description": "report (综合材料模式) | redhead (红头文件模式)",
                },
                "doc_number": {"type": "string", "description": "可选发文字号"},
                "issuing_org": {"type": "string", "description": "可选发文机关署名"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "wb_list_archives",
        "description": "列出【📦 资料存档】栏目中的归档包（如 11.17会议记录、培训课件、下沉督导照片等，含每个包内的录音、照片、PPT、文档清单与本地目录路径）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "可选按存档分类筛选（如 01_会议记录 / 02_培训课件 / 03_下沉督导照片）"},
                "query": {"type": "string", "description": "可选关键词过滤（如 11.17 或 会议）"},
            },
        },
    },
    {
        "name": "wb_add_archive",
        "description": "在【📦 资料存档】栏目中按标题一次存入一个新的多媒体归档包（如“11.17会议记录”或“11.18xx事项下沉照片”），支持同时写入会议纪要/备注并挂载多份录音、照片、PPT课件、Word/Excel文件",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "存档标题，如 11.17会议记录 或 11.18危化品企业下沉照片"},
                "category": {"type": "string", "description": "存档分类（可传现有分类或自定义新分类名，如 01_会议记录 / 02_培训课件 / 03_下沉督导照片）"},
                "archive_date": {"type": "string", "description": "存档日期 YYYY-MM-DD（留空则自动从标题识别或取今日）"},
                "notes_md": {"type": "string", "description": "会议纪要、培训摘要或现场下沉情况文字说明（支持 Markdown）"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选归纳标签数组"},
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：要一并存入该归档包的本机文件绝对路径数组（支持录音.mp3/.m4a、照片.jpg/.png、课件.pptx、文档.docx/.pdf/.xlsx）",
                },
                "is_pinned": {"type": "boolean", "description": "是否置顶"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "wb_append_archive",
        "description": "向【📦 资料存档】中已有的某个归档包追加新文件（如补传现场照片、录音或整理好的会议纪要）或补充文字备注",
        "inputSchema": {
            "type": "object",
            "properties": {
                "archive_id": {"type": "integer", "description": "资料存档包 ID"},
                "file_paths": {"type": "array", "items": {"type": "string"}, "description": "要追加的本机文件绝对路径列表"},
                "notes_append": {"type": "string", "description": "要追加的会议纪要或备注说明"},
            },
            "required": ["archive_id"],
        },
    },
    {
        "name": "wb_list_records",
        "description": "列出【📌 重要事项记录】栏目中的常备知识条目（如高密汛情基本情况、地震应急预案、重点企业底数等）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "可选按分类筛选（如 01_汛情与防灾底数 / 02_突发事件应急预案）"},
                "query": {"type": "string", "description": "可选关键词过滤"},
            },
        },
    },
    {
        "name": "wb_get_record",
        "description": "读取【📌 重要事项记录】中某条记录（支持按 ID 或标题如“高密汛情基本情况”、“地震应急预案”查询）的完整正文与关联 Word/PDF/Excel 提取数据。起草汇报材料时请先调此工具获取准确业务底数！",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id_or_title": {"type": "string", "description": "记录 ID 或标题关键词（如 高密汛情基本情况）"},
            },
            "required": ["id_or_title"],
        },
    },
    {
        "name": "wb_upsert_record",
        "description": "在【📌 重要事项记录】中新建或更新一条常备知识/预案/底数记录（支持自定义分类、直接写入直读正文，或挂载 Word/PDF/Excel 原件）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "重要事项标题，如 高密汛情基本情况 或 地震应急预案"},
                "category": {"type": "string", "description": "所属分类（支持自定义新分类，如 01_汛情与防灾底数 / 02_突发事件应急预案）"},
                "content_md": {"type": "string", "description": "点开即读的正文内容（支持 Markdown 标题与表格）"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "可选标签数组"},
                "file_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选：关联挂载的本机 Word (.docx) / PDF (.pdf) / Excel (.xlsx) 文件绝对路径列表",
                },
                "is_pinned": {"type": "boolean", "description": "是否置顶速查"},
            },
            "required": ["title"],
        },
    },
    {
        "name": "wb_search",
        "description": "四维全库搜索：同时检索【工作台待办】、【资料存档】、【重要事项记录】与【公文与材料库】（支持仅标题、FTS5 全文检索、两段式 AI 语义检索）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或自然语言问题"},
                "scope": {
                    "type": "string",
                    "enum": ["title", "full"],
                    "description": "title (仅搜标题/文号) | full (标题+全文内容)",
                },
                "ai_context": {"type": "boolean", "description": "是否启用两段式 AI 语义检索并返回正文切片"},
                "category": {"type": "string", "description": "可选限定分类目录"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wb_read_document",
        "description": "读取指定公文或材料（doc_id）的完整纯文本内容、结构化表格/幻灯片预览数据与本机物理路径",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "integer", "description": "文件 ID"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "wb_sync_folders",
        "description": "立即触发本地物理文件夹（含待办事项、资料存档、重要事项记录）与工作台 SQLite/FTS5 索引库的双向增量同步，并检查周期性工作派生",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

_MCP_TOOLS_BY_NAME: Dict[str, Dict[str, Any]] = {spec["name"]: spec for spec in MCP_TOOLS_SPEC}


def _parse_int_param(t_args: Dict[str, Any], key: str, required: bool = True) -> Tuple[Optional[int], Optional[str]]:
    """安全解析整数参数，拒绝 bool 与非法字符串并给出清晰错误提示。"""
    if key not in t_args or t_args[key] is None or t_args[key] == "":
        if required:
            return None, f"缺少必填参数: '{key}'"
        return None, None
    val = t_args[key]
    if isinstance(val, bool):
        return None, f"参数 '{key}' 必须为正整数，不能为布尔值"
    if isinstance(val, int):
        parsed = val
    elif isinstance(val, str) and re.fullmatch(r"\s*-?\d+\s*", val):
        parsed = int(val.strip())
    else:
        return None, f"参数 '{key}' 必须为整数，当前值: {val!r}"
    if parsed <= 0:
        return None, f"参数 '{key}' 必须为正整数，当前值: {parsed}"
    return parsed, None


def _validate_mcp_tool_args(tool_name: str, t_args: Any) -> Optional[str]:
    """根据 MCP_TOOLS_SPEC 校验 arguments 类型、required 与 enum 约束。"""
    if not isinstance(t_args, dict):
        return "工具参数 arguments 必须为 JSON 对象"
    spec = _MCP_TOOLS_BY_NAME.get(tool_name)
    if not spec:
        return None
    schema = spec.get("inputSchema") or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []

    for req_key in required:
        if req_key not in t_args or t_args[req_key] is None:
            return f"缺少必填参数: '{req_key}'"
        if props.get(req_key, {}).get("type") == "string" and not str(t_args[req_key]).strip():
            # 允许 wb_write_draft 的 content 为空串（例如新建空白草稿），其余必填字符串需非空
            if not (tool_name == "wb_write_draft" and req_key == "content"):
                return f"必填参数 '{req_key}' 不能为空"

    for k, prop_schema in props.items():
        if k not in t_args or t_args[k] is None:
            continue
        val = t_args[k]
        if "enum" in prop_schema and val != "":
            allowed = prop_schema["enum"]
            if val not in allowed:
                return f"参数 '{k}' 取值无效（当前值: {val!r}），允许值: {', '.join(map(str, allowed))}"
        if prop_schema.get("type") == "integer":
            _, err = _parse_int_param(t_args, k, required=(k in required))
            if err:
                return err

    return None


def handle_mcp_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(req, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Invalid Request: expected JSON object"},
        }

    method = req.get("method", "")
    # JSON-RPC 2.0 规范：无 "id" 字段的请求为 Notification，严格不返回响应
    if "id" not in req:
        return None

    req_id = req.get("id")
    params = req.get("params") or {}
    if not isinstance(params, dict):
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32602, "message": "Invalid params: expected object"},
        }

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {
                    "name": "gw-workbench-mcp",
                    "version": VERSION,
                },
            },
        }

    if isinstance(method, str) and method.startswith("notifications/"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS_SPEC},
        }

    if method == "resources/list":
        data = _do_list(status="in_progress")
        recs = _do_list_records().get("important_records", [])
        resources = [
            {
                "uri": f"workbench://task/{t['id']}",
                "name": f"[待办 #{t['id']}] {t['title']}",
                "description": t.get("summary") or "",
                "mimeType": "application/json",
            }
            for t in data.get("tasks", [])
        ] + [
            {
                "uri": f"workbench://record/{r['id']}",
                "name": f"[重要事项 #{r['id']}] {r['title']}",
                "description": (r.get("content_md") or "")[:100],
                "mimeType": "application/json",
            }
            for r in recs
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}}

    if method == "resources/read":
        uri = str(params.get("uri") or "").strip()
        m = urllib.parse.urlparse(uri)
        scheme = m.scheme
        kind = m.netloc
        tid_str = m.path.lstrip("/")
        if scheme != "workbench" or kind not in ("task", "record") or not tid_str.isdigit():
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32602,
                    "message": f"Invalid params: invalid resource uri '{uri}' (expected workbench://task/<id> or workbench://record/<id>)",
                },
            }
        if kind == "record":
            res_obj = _do_get_record(tid_str)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(res_obj, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }
        else:
            tid = int(tid_str)
            api_res = api_call("GET", f"/api/tasks/{tid}")
            if api_res is not None:
                if api_res.get("error"):
                    t_obj = {"ok": False, "error": api_res["error"]}
                else:
                    t_obj = api_res.get("task", api_res)
            else:
                init_db()
                ws_root = get_workspace_root()
                with db_conn() as conn:
                    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone()
                    t_obj = serialize_task(conn, row, ws_root=ws_root) if row else {}
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(t_obj, ensure_ascii=False, indent=2),
                        }
                    ]
                },
            }

    if method == "tools/call":
        t_name = params.get("name")
        t_args = params.get("arguments")
        if t_args is None:
            t_args = {}

        try:
            if t_name not in _MCP_TOOLS_BY_NAME:
                res: Dict[str, Any] = {"ok": False, "error": f"未知的 MCP 工具: {t_name}"}
            else:
                val_err = _validate_mcp_tool_args(t_name, t_args)
                if val_err:
                    res = {"ok": False, "error": val_err}
                elif t_name == "wb_list_tasks":
                    res = _do_list(status=t_args.get("status", ""), category=t_args.get("category", ""))
                elif t_name == "wb_add_task":
                    files: List[Dict[str, Any]] = []
                    if t_args.get("file_path"):
                        files, f_err = _collect_safe_local_files([t_args["file_path"]], doc_role="incoming")
                        if f_err:
                            res = {"ok": False, "error": f_err}
                        else:
                            res = {}
                    else:
                        res = {}
                    if not res:
                        raw_chk = t_args.get("checklist")
                        if isinstance(raw_chk, str):
                            chk_items = [x.strip() for x in raw_chk.replace("；", ";").split(";") if x.strip()]
                        elif isinstance(raw_chk, list):
                            chk_items = [str(x).strip() for x in raw_chk if str(x).strip()]
                        else:
                            chk_items = None
                        res = _do_add({
                            "title": t_args.get("title") or "",
                            "raw_notice_text": t_args.get("text") or "",
                            "source_channel": t_args.get("source_channel") or "",
                            "doc_number": t_args.get("doc_number"),
                            "issuing_org": t_args.get("issuing_org"),
                            "priority": t_args.get("priority") or "",
                            "deadline": t_args.get("deadline"),
                            "category": t_args.get("category") or "",
                            "summary": t_args.get("summary") or "",
                            "checklist": chk_items,
                            "initial_progress": t_args.get("progress") or "",
                            "auto_ai": bool(t_args.get("auto_ai", True)),
                            "files": files,
                        })
                elif t_name == "wb_update_progress":
                    tid, err = _parse_int_param(t_args, "task_id", required=True)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        res = _do_progress(
                            task_id=tid,
                            text=t_args.get("progress_text") or "",
                            status=t_args.get("status") or "",
                            check_id=t_args.get("toggle_check_id") or "",
                        )
                elif t_name == "wb_attach_file":
                    tid, err = _parse_int_param(t_args, "task_id", required=True)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        res = _do_attach(
                            task_id=tid,
                            file_path=str(t_args.get("file_path") or ""),
                            role=str(t_args.get("role") or "draft"),
                            sub_folder=str(t_args.get("sub_folder") or ""),
                        )
                elif t_name == "wb_write_draft":
                    tid, err = _parse_int_param(t_args, "task_id", required=False)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        res = _do_write_draft(
                            task_id=tid,
                            filename=t_args.get("filename") or "新建材料草稿.md",
                            content=t_args.get("content") or "",
                            category=t_args.get("category") or "",
                            export_gb9704_docx=bool(t_args.get("export_gb9704_docx", False)),
                            gw_mode=t_args.get("gw_mode") or "report",
                            sub_folder=t_args.get("sub_folder") or "",
                        )
                elif t_name == "wb_format_gb9704_docx":
                    did, err = _parse_int_param(t_args, "doc_id", required=True)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        res = _do_format_gw(
                            doc_id=did,
                            mode=t_args.get("mode") or "report",
                            doc_number=t_args.get("doc_number") or "",
                            org=t_args.get("issuing_org") or "",
                        )
                elif t_name == "wb_list_archives":
                    res = _do_list_archives(
                        category=t_args.get("category") or "",
                        query=t_args.get("query") or "",
                    )
                elif t_name == "wb_add_archive":
                    res = _do_add_archive(
                        title=t_args.get("title") or "",
                        category=t_args.get("category") or "",
                        archive_date=t_args.get("archive_date") or "",
                        notes_md=t_args.get("notes_md") or "",
                        tags=t_args.get("tags") or [],
                        file_paths=t_args.get("file_paths") or [],
                        is_pinned=bool(t_args.get("is_pinned", False)),
                    )
                elif t_name == "wb_append_archive":
                    aid, err = _parse_int_param(t_args, "archive_id", required=True)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        res = _do_append_archive(
                            archive_id=aid,
                            file_paths=t_args.get("file_paths") or [],
                            notes_append=t_args.get("notes_append") or "",
                        )
                elif t_name == "wb_list_records":
                    res = _do_list_records(
                        category=t_args.get("category") or "",
                        query=t_args.get("query") or "",
                    )
                elif t_name == "wb_get_record":
                    res = _do_get_record(str(t_args.get("id_or_title") or ""))
                elif t_name == "wb_upsert_record":
                    res = _do_upsert_record(
                        title=t_args.get("title") or "",
                        category=t_args.get("category") or "",
                        content_md=t_args.get("content_md") or "",
                        tags=t_args.get("tags") or [],
                        file_paths=t_args.get("file_paths") or [],
                        is_pinned=bool(t_args.get("is_pinned", False)),
                    )
                elif t_name == "wb_search":
                    res = _do_search(
                        query=t_args.get("query") or "",
                        scope=t_args.get("scope") or "full",
                        ai=bool(t_args.get("ai_context", True)),
                        category=t_args.get("category") or "",
                    )
                elif t_name == "wb_read_document":
                    doc_id, err = _parse_int_param(t_args, "doc_id", required=True)
                    if err:
                        res = {"ok": False, "error": err}
                    else:
                        api_res = api_call("GET", f"/api/docs/{doc_id}/preview")
                        if api_res is None:
                            init_db()
                            ws_root = get_workspace_root()
                            with db_conn() as conn:
                                row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
                                if not row:
                                    res = {"ok": False, "error": "文件不存在"}
                                else:
                                    d = serialize_doc_light(row, ws_root=ws_root)
                                    d["extracted_text"] = row["extracted_text"] or ""
                                    d["structured_preview"] = extract_structured_preview(ws_root / row["rel_path"], doc_id=doc_id)
                                    res = {"ok": True, "document": d}
                        elif api_res.get("error"):
                            res = dict(api_res)
                            res["ok"] = False
                        else:
                            res = dict(api_res)
                            res.setdefault("ok", True)
                elif t_name == "wb_sync_folders":
                    api_res = api_call("POST", "/api/sync", {})
                    if api_res is None:
                        init_db()
                        st = sync_filesystem(force=True)
                        st["spawned_recurring"] = check_and_spawn_all_recurring()
                        res = {"ok": True, "sync_stats": st}
                    elif api_res.get("error"):
                        res = dict(api_res)
                        res["ok"] = False
                    else:
                        res = dict(api_res)
                        res.setdefault("ok", True)
                else:
                    res = {"ok": False, "error": f"未知的 MCP 工具: {t_name}"}

            is_err = (not res.get("ok", True)) or bool(res.get("error"))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}
                    ],
                    "isError": is_err,
                },
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"执行异常: {e}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def _write_mcp_frame(resp: Dict[str, Any], use_content_length: bool) -> None:
    out_bytes = json.dumps(resp, ensure_ascii=False).encode("utf-8")
    if use_content_length:
        header = f"Content-Length: {len(out_bytes)}\r\n\r\n".encode("ascii")
        sys.stdout.buffer.write(header + out_bytes)
    else:
        sys.stdout.buffer.write(out_bytes + b"\n")
    sys.stdout.buffer.flush()


def run_mcp_stdio_server() -> None:
    """运行标准 MCP stdio 协议循环（全程保护 stdout 不被日志污染）"""
    with contextlib.redirect_stdout(sys.stderr):
        init_db()
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break
        line_str = line.decode("utf-8", errors="ignore").strip()
        if not line_str:
            continue

        use_content_length = False
        if line_str.lower().startswith("content-length:"):
            use_content_length = True
            try:
                length = int(line_str.split(":", 1)[1].strip())
            except ValueError:
                _write_mcp_frame(
                    {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error: invalid Content-Length"}},
                    use_content_length=True,
                )
                continue
            while True:
                hdr = sys.stdin.buffer.readline().decode("utf-8", errors="ignore").strip()
                if not hdr:
                    break
            raw_body = sys.stdin.buffer.read(length).decode("utf-8", errors="ignore")
        else:
            raw_body = line_str

        try:
            req = json.loads(raw_body)
        except Exception as e:
            _write_mcp_frame(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {e}"},
                },
                use_content_length=use_content_length,
            )
            continue

        with contextlib.redirect_stdout(sys.stderr):
            resp = handle_mcp_request(req)
        if resp is not None:
            _write_mcp_frame(resp, use_content_length=use_content_length)


# ==================== CLI 命令入口 ====================
def cmd_list(args) -> None:
    print(json.dumps(_do_list(args.status or "", args.category or ""), ensure_ascii=False, indent=2))


def cmd_add(args) -> None:
    files: List[Dict[str, Any]] = []
    if args.file:
        files, err = _collect_safe_local_files(args.file, doc_role="incoming")
        if err:
            print(json.dumps({"ok": False, "error": err}, ensure_ascii=False, indent=2))
            sys.exit(1)
    checklist = None
    if args.checklist:
        checklist = [
            {"id": f"chk_{i+1}", "text": x.strip(), "done": False}
            for i, x in enumerate(args.checklist.replace("；", ";").split(";"))
            if x.strip()
        ]
    payload = {
        "title": args.title or "",
        "raw_notice_text": args.text or "",
        "source_channel": args.source or "",
        "doc_number": args.doc_number,
        "issuing_org": args.org,
        "priority": args.priority or "",
        "deadline": args.deadline,
        "category": args.category or "",
        "summary": args.summary or "",
        "checklist": checklist,
        "initial_progress": args.progress or "",
        "auto_ai": not args.no_ai,
        "files": files,
    }
    print(json.dumps(_do_add(payload), ensure_ascii=False, indent=2))


def cmd_progress(args) -> None:
    res = _do_progress(args.task_id, args.text or "", args.status or "", args.check_id or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_attach(args) -> None:
    res = _do_attach(
        task_id=args.task_id,
        file_path=args.file_path,
        role=args.role,
        sub_folder=getattr(args, "sub_dir", "") or "",
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not res.get("ok"):
        sys.exit(1)


def cmd_draft(args) -> None:
    res = _do_write_draft(
        task_id=args.task_id,
        filename=args.filename,
        content=args.content,
        category=args.category or "",
        export_gb9704_docx=args.to_docx,
        gw_mode=args.mode,
        sub_folder=getattr(args, "sub_dir", "") or "",
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_format_gw(args) -> None:
    res = _do_format_gw(args.doc_id, mode=args.mode, doc_number=args.doc_number or "", org=args.org or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_archive(args) -> None:
    action = args.archive_action
    if action == "list":
        print(json.dumps(_do_list_archives(category=args.category or "", query=args.query or ""), ensure_ascii=False, indent=2))
    elif action == "add":
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
        res = _do_add_archive(
            title=args.title,
            category=args.category or "",
            archive_date=args.date or "",
            notes_md=args.notes or "",
            tags=tags,
            file_paths=args.files or [],
            is_pinned=bool(args.pin),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif action == "append":
        res = _do_append_archive(
            archive_id=args.archive_id,
            file_paths=args.files or [],
            notes_append=args.notes or "",
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_record(args) -> None:
    action = args.record_action
    if action == "list":
        print(json.dumps(_do_list_records(category=args.category or "", query=args.query or ""), ensure_ascii=False, indent=2))
    elif action == "get":
        print(json.dumps(_do_get_record(args.target), ensure_ascii=False, indent=2))
    elif action == "set":
        tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
        res = _do_upsert_record(
            title=args.title,
            category=args.category or "",
            content_md=args.content or "",
            tags=tags,
            file_paths=args.files or [],
            is_pinned=bool(args.pin),
        )
        print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_recurring(args) -> None:
    if args.spawn:
        res = api_call("POST", f"/api/recurring/{args.spawn}/spawn", {})
        if res is None:
            init_db()
            t = spawn_task_from_recurring_rule(args.spawn, force_manual=True)
            res = {"ok": bool(t), "task": t}
            if not t:
                res["error"] = f"周期规则 #{args.spawn} 不存在"
        elif res.get("error"):
            res["ok"] = False
        else:
            res.setdefault("ok", True)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    res = api_call("GET", "/api/recurring")
    if res is not None:
        if res.get("error"):
            res = {"ok": False, "error": res["error"]}
        else:
            res.setdefault("ok", True)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    init_db()
    with db_conn() as conn:
        rec_rows = conn.execute("SELECT * FROM recurring_rules ORDER BY id ASC").fetchall()
        rows = []
        for rr in rec_rows:
            d = dict(rr)
            d["checklist"] = json.loads(d.pop("checklist_json", "[]") or "[]")
            d["enabled"] = bool(d.get("enabled", 1))
            rows.append(d)
    print(json.dumps({"ok": True, "recurring_rules": rows}, ensure_ascii=False, indent=2))


def cmd_search(args) -> None:
    res = _do_search(args.query, scope=args.scope, ai=args.ai, category=args.category or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_sync(args) -> None:
    res = api_call("POST", "/api/sync", {})
    if res is None:
        init_db()
        stats = sync_filesystem(force=True)
        stats["spawned_recurring"] = check_and_spawn_all_recurring()
        res = {"ok": True, "sync_stats": stats}
    elif res.get("error"):
        res["ok"] = False
    else:
        res.setdefault("ok", True)
    print(json.dumps(res, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=f"GW-Workbench v{VERSION} 本地 Agent CLI 与 MCP Server")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出工作台待办事项")
    p_list.add_argument("--status", choices=["pending", "in_progress", "archived"], help="按状态筛选")
    p_list.add_argument("--category", help="按分类目录筛选")
    p_list.set_defaults(func=cmd_list)

    p_add = sub.add_parser("add", help="新增工作事项（支持来文路径或通知文本，自动AI解析）")
    p_add.add_argument("--text", help="通知或来文文本内容")
    p_add.add_argument("--file", nargs="*", help="来文原件本地路径（可传多个）")
    p_add.add_argument("--title", help="指定事项标题")
    p_add.add_argument("--source", help="来源渠道：上级来文 / 微信群 / 电话通知 / 自建")
    p_add.add_argument("--doc-number", help="发文字号")
    p_add.add_argument("--org", help="来文单位")
    p_add.add_argument("--priority", choices=["普通", "重要", "紧急"], help="优先级")
    p_add.add_argument("--deadline", help="截止日期 YYYY-MM-DD")
    p_add.add_argument("--category", help="所属分类目录")
    p_add.add_argument("--summary", help="核心工作要求摘要")
    p_add.add_argument("--checklist", help="待办细项，多项用分号 ; 分隔")
    p_add.add_argument("--progress", help="初始进展记录")
    p_add.add_argument("--no-ai", action="store_true", help="跳过AI自动解析")
    p_add.set_defaults(func=cmd_add)

    p_prog = sub.add_parser("progress", help="更新事项进展、勾选清单或修改归档状态")
    p_prog.add_argument("task_id", type=int, help="事项 ID")
    p_prog.add_argument("text", nargs="?", default="", help="新增进展内容")
    p_prog.add_argument("--status", choices=["pending", "in_progress", "archived"], help="修改状态")
    p_prog.add_argument("--check-id", help="切换勾选状态的细项 ID（如 chk_1）")
    p_prog.set_defaults(func=cmd_progress)

    p_att = sub.add_parser("attach", help="向指定事项挂载【我写的材料】或【来文原件】")
    p_att.add_argument("task_id", type=int, help="事项 ID")
    p_att.add_argument("file_path", help="本地文件路径")
    p_att.add_argument("--role", choices=["draft", "incoming"], default="draft", help="draft=我写的材料, incoming=来文原件")
    p_att.add_argument("--sub-dir", default="", help="可选：任务下的单层材料子目录名称")
    p_att.set_defaults(func=cmd_attach)

    p_draft = sub.add_parser("draft", help="直接为指定事项写入材料草稿，并可选一键排版导出为国标 GB/T 9704 .docx 公文")
    p_draft.add_argument("task_id", type=int, help="事项 ID")
    p_draft.add_argument("--filename", required=True, help="草稿文件名，如 冬季防灾汇报.md")
    p_draft.add_argument("--content", required=True, help="材料正文内容")
    p_draft.add_argument("--category", help="分类目录")
    p_draft.add_argument("--sub-dir", default="", help="可选：任务下的单层材料子目录名称")
    p_draft.add_argument("--to-docx", action="store_true", help="同时生成符合 GB/T 9704—2012 国标格式的 .docx 正式公文")
    p_draft.add_argument("--mode", choices=["report", "redhead"], default="report", help="国标排版模式")
    p_draft.set_defaults(func=cmd_draft)

    p_fmt = sub.add_parser("format-gw", help="将库内文档一键排版导出为符合 GB/T 9704—2012 国标格式的 .docx 公文")
    p_fmt.add_argument("doc_id", type=int, help="文档 ID")
    p_fmt.add_argument("--mode", choices=["report", "redhead"], default="report", help="report=汇报材料模式, redhead=红头文件模式")
    p_fmt.add_argument("--doc-number", help="自定义发文字号")
    p_fmt.add_argument("--org", help="自定义发文机关署名")
    p_fmt.set_defaults(func=cmd_format_gw)

    # 资料存档 (archive) CLI 子命令
    p_arch = sub.add_parser("archive", help="管理【📦 资料存档】栏目（会议记录/培训课件/下沉照片等按次归档包）")
    arch_sub = p_arch.add_subparsers(dest="archive_action", required=True)
    p_arch_list = arch_sub.add_parser("list", help="列出资料存档包")
    p_arch_list.add_argument("--category", help="按分类筛选")
    p_arch_list.add_argument("--query", help="按关键词筛选")
    p_arch_add = arch_sub.add_parser("add", help="按标题一次存入资料存档包（支持多份录音/照片/PPT/文档）")
    p_arch_add.add_argument("--title", required=True, help="归档标题，如 11.17会议记录")
    p_arch_add.add_argument("--category", help="分类名称（如 01_会议记录 / 02_培训课件 / 03_下沉督导照片 或自定义）")
    p_arch_add.add_argument("--date", help="日期 YYYY-MM-DD")
    p_arch_add.add_argument("--notes", help="会议纪要或现场说明文本")
    p_arch_add.add_argument("--tags", help="标签，多个用逗号分隔")
    p_arch_add.add_argument("--files", nargs="*", help="本机文件绝对路径列表")
    p_arch_add.add_argument("--pin", action="store_true", help="置顶该存档包")
    p_arch_app = arch_sub.add_parser("append", help="向已有存档包追加文件或纪要")
    p_arch_app.add_argument("archive_id", type=int, help="存档包 ID")
    p_arch_app.add_argument("--files", nargs="*", help="要追加的文件路径列表")
    p_arch_app.add_argument("--notes", help="要追加的备注说明")
    p_arch.set_defaults(func=cmd_archive)

    # 重要事项记录 (record) CLI 子命令
    p_rec_kb = sub.add_parser("record", help="管理【📌 重要事项记录】栏目（如高密汛情基本情况、地震应急预案等常备直读库）")
    rec_sub = p_rec_kb.add_subparsers(dest="record_action", required=True)
    p_rec_list = rec_sub.add_parser("list", help="列出重要事项记录")
    p_rec_list.add_argument("--category", help="按分类筛选")
    p_rec_list.add_argument("--query", help="按关键词筛选")
    p_rec_get = rec_sub.add_parser("get", help="读取指定重要事项记录的正文与结构化附件内容")
    p_rec_get.add_argument("target", help="记录 ID 或标题关键词（如 高密汛情基本情况）")
    p_rec_set = rec_sub.add_parser("set", help="新建或更新重要事项记录")
    p_rec_set.add_argument("--title", required=True, help="事项标题，如 高密汛情基本情况")
    p_rec_set.add_argument("--category", help="分类名称（支持自定义）")
    p_rec_set.add_argument("--content", help="直读正文内容（Markdown）")
    p_rec_set.add_argument("--tags", help="标签，逗号分隔")
    p_rec_set.add_argument("--files", nargs="*", help="挂载的 Word/PDF/Excel 本机文件路径")
    p_rec_set.add_argument("--pin", action="store_true", help="设为置顶速查")
    p_rec_kb.set_defaults(func=cmd_record)

    p_rec = sub.add_parser("recurring", help="查看或手动触发周期性/常态化工作规则")
    p_rec.add_argument("--spawn", type=int, help="立即派生指定规则 ID 的本期工作事项")
    p_rec.set_defaults(func=cmd_recurring)

    p_search = sub.add_parser("search", help="四维全库搜索（待办+公文+资料存档+重要事项记录）")
    p_search.add_argument("query", help="搜索关键词或自然语言问题")
    p_search.add_argument("--scope", choices=["title", "full"], default="full", help="title=仅标题/文号, full=标题+全文内容")
    p_search.add_argument("--ai", action="store_true", help="启用两段式 AI 语义检索与上下文提取")
    p_search.add_argument("--category", help="限定物理分类目录")
    p_search.set_defaults(func=cmd_search)

    p_sync = sub.add_parser("sync", help="立即执行本地物理文件夹与数据库双向同步")
    p_sync.set_defaults(func=cmd_sync)

    p_mcp = sub.add_parser("mcp", help="启动原生 Model Context Protocol (MCP) stdio Server")
    p_mcp.set_defaults(func=lambda _args: run_mcp_stdio_server())

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
