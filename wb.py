#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与材料智能工作台 (GW-Workbench v1.2.0) 本地 Agent 专用命令行工具 & 原生 MCP Server
支持：
1. 命令行直接操作 (list / add / progress / attach / draft / format-gw / recurring / search / sync)
2. 原生 Model Context Protocol (MCP) Server 协议 (`python3 wb.py mcp`)，基于标准 stdio JSON-RPC 2.0，
   可直接挂载至 Claude Desktop / Cline / Roo Code / Cursor / OpenCode 等本地 Agent 客户端！
"""

import argparse
import json
import shutil
import sys
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from server import (
    VERSION,
    DOC_LIGHT_COLS,
    load_config,
    init_db,
    get_db,
    sync_filesystem,
    create_task_record,
    execute_search,
    serialize_task,
    serialize_doc_light,
    build_highlight_snippet,
    write_task_readme_file,
    get_workspace_root,
    sanitize_filename,
    resolve_unique_dest_path,
    _sync_fts_doc,
    spawn_task_from_recurring_rule,
    check_and_spawn_all_recurring,
)
from ai_engine import extract_search_keywords, synthesize_ai_search_answer
from gw_formatter import build_gb9704_docx
from parser import extract_document_text


def api_call(method: str, path: str, payload: dict = None) -> Optional[dict]:
    """优先调用正在运行的本地服务，若服务未启动则自动无缝退回本地直连数据库执行"""
    cfg = load_config()
    port = cfg.get("port", 8090)
    url = f"http://127.0.0.1:{port}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-Agent-Token": cfg.get("agent_token", "gw-local-agent-token"),
    }
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _do_list(status: str = "", category: str = "") -> Dict[str, Any]:
    init_db()
    sync_filesystem(force=False)
    ws_root = get_workspace_root()
    conn = get_db()
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
    conn.close()
    return {"ok": True, "version": VERSION, "count": len(tasks), "tasks": tasks}


def _do_add(payload: Dict[str, Any]) -> Dict[str, Any]:
    init_db()
    res = api_call("POST", "/api/tasks", payload)
    if not res:
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
    if not res:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
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
        conn.execute(
            "UPDATE tasks SET progress_json = ?, checklist_json = ?, status = ?, completed_at = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(prog, ensure_ascii=False),
                json.dumps(chk, ensure_ascii=False),
                new_st,
                comp_at,
                now_str,
                task_id,
            ),
        )
        write_task_readme_file(conn, task_id)
        conn.commit()
        u_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        res = {"ok": True, "task": serialize_task(conn, u_row)}
        conn.close()
    return res


def _do_write_draft(
    task_id: Optional[int],
    filename: str,
    content: str,
    category: str = "",
    export_gb9704_docx: bool = False,
    gw_mode: str = "report",
) -> Dict[str, Any]:
    """在线或由本地 Agent 直接写入材料草稿，并可选一键生成 GB/T 9704—2012 标准 .docx 公文"""
    init_db()
    payload = {
        "task_id": task_id,
        "filename": filename,
        "content": content,
        "category": category,
    }
    res = api_call("POST", "/api/docs/save-text", payload)
    if not res:
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fname = sanitize_filename(filename or "材料初稿.md")
        if not fname.lower().endswith((".md", ".txt", ".markdown")):
            fname += ".md"
        conn = get_db()
        if task_id:
            t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
            if not t_row:
                conn.close()
                return {"ok": False, "error": f"事项 #{task_id} 不存在"}
            task_folder = ws_root / t_row["folder_rel_path"]
            task_folder.mkdir(parents=True, exist_ok=True)
            if not fname.startswith(("[材料]", "【材料】")):
                fname = "[材料]_" + fname
            dest_path = resolve_unique_dest_path(task_folder, fname, content.encode("utf-8"))
            cat_use = t_row["category"]
        else:
            cat_use = sanitize_filename(category or "03_综合汇报与总结")
            cat_dir = ws_root / cat_use
            cat_dir.mkdir(parents=True, exist_ok=True)
            dest_path = resolve_unique_dest_path(cat_dir, fname, content.encode("utf-8"))

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
            (task_id, dest_path.name, rel_file, cat_use, st.st_size, round(st.st_mtime, 2), content, now_str, now_str),
        )
        doc_id = cur.lastrowid
        _sync_fts_doc(conn, doc_id)
        if task_id:
            write_task_readme_file(conn, int(task_id))
        conn.commit()
        u_doc = serialize_doc_light(conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE id = ?", (doc_id,)).fetchone(), ws_root=ws_root)
        conn.close()
        res = {"ok": True, "document": u_doc}

    if export_gb9704_docx and res.get("ok") and res.get("document"):
        doc_id = res["document"]["id"]
        gw_res = _do_format_gw(doc_id, mode=gw_mode)
        res["gb9704_docx"] = gw_res.get("document")
    return res


def _do_format_gw(doc_id: int, mode: str = "report", doc_number: str = "", org: str = "") -> Dict[str, Any]:
    init_db()
    payload = {"mode": mode, "doc_number": doc_number, "issuing_org": org}
    res = api_call("POST", f"/api/docs/{doc_id}/format-gw", payload)
    if not res:
        cfg = load_config()
        ws_root = get_workspace_root(cfg)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db()
        d_row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if not d_row:
            conn.close()
            return {"ok": False, "error": f"文档 #{doc_id} 不存在"}
        t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (d_row["task_id"],)).fetchone() if d_row["task_id"] else None
        src_abs = ws_root / d_row["rel_path"]
        stem = Path(d_row["filename"]).stem.replace("[材料]_", "").replace("[来文]_", "")
        out_abs = src_abs.parent / sanitize_filename(f"[材料]_{stem}_国标排版.docx")
        build_gb9704_docx(
            output_path=out_abs,
            raw_text=d_row["extracted_text"] or "",
            title=stem,
            doc_number=doc_number or (t_row["doc_number"] if t_row else ""),
            issuing_org=org or (t_row["issuing_org"] if t_row and t_row["issuing_org"] else "县应急管理局"),
            mode=mode,
            xkl_gw_cli=cfg.get("xkl_gw_cli", ""),
        )
        conn.close()
        sync_filesystem(force=True)
        conn = get_db()
        rel_file = str(out_abs.relative_to(ws_root)).replace("\\", "/")
        r_new = conn.execute(f"SELECT {DOC_LIGHT_COLS} FROM documents WHERE rel_path = ?", (rel_file,)).fetchone()
        res = {"ok": True, "document": serialize_doc_light(r_new, ws_root=ws_root) if r_new else {"abs_path": str(out_abs)}}
        conn.close()
    return res


def _do_search(query: str, scope: str = "full", ai: bool = False, category: str = "") -> Dict[str, Any]:
    init_db()
    sync_filesystem(force=False)
    if ai:
        res = api_call("POST", "/api/agent/context-search", {"query": query, "category": category})
        if not res:
            kws = extract_search_keywords(query)
            ws_root = get_workspace_root()
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
                    d_obj["extracted_text_chunk"] = (r["extracted_text"] or "")[:2000]
                    scored_docs.append((hits, d_obj))
            scored_docs.sort(key=lambda x: (-x[0], -x[1]["id"]))
            top_docs = [x[1] for x in scored_docs[:18]]
            conn.close()

            ans = synthesize_ai_search_answer(query, kws, top_tasks, top_docs, config=load_config())
            res = {"query": query, "keywords": kws, "answer": ans["answer"], "tasks": top_tasks, "documents": top_docs}
        return res
    else:
        tasks, docs = execute_search(query, scope=scope, category=category)
        return {
            "ok": True,
            "query": query,
            "scope": scope,
            "tasks": tasks,
            "documents": docs,
        }


# ==================== 原生 MCP Server (stdio JSON-RPC 2.0) ====================
MCP_TOOLS_SPEC = [
    {
        "name": "wb_list_tasks",
        "description": "获取公文工作台中的待办与进行中工作事项列表（包含每个事项的本地文件夹路径、待办细项清单、已有关联材料及库内参考旧稿）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "可选过滤状态: in_progress | pending | archived"},
                "category": {"type": "string", "description": "可选过滤分类目录名"},
            },
        },
    },
    {
        "name": "wb_add_task",
        "description": "将上级来文或微信群/电话通知填入公文工作台，自动建立本地事项专属文件夹与 00_事项要求与进展.md。只需填好 summary（核心要求）与 checklist（待办细项）两块内容——不要另建“办理要点/说明书骨架/职责原文摘录/分办清单”之类文件塞进材料栏。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "通知原文或公文正文内容"},
                "file_path": {"type": "string", "description": "可选：本机公文原件绝对路径 (.ofd/.docx/.pdf/.wps/.txt)"},
                "title": {"type": "string", "description": "可选：指定事项标题（留空则自动提取）"},
                "source_channel": {"type": "string", "description": "上级来文 | 微信群 | 电话通知 | 自建"},
                "doc_number": {"type": "string", "description": "发文字号"},
                "issuing_org": {"type": "string", "description": "来文单位"},
                "priority": {"type": "string", "description": "普通 | 重要 | 紧急"},
                "deadline": {"type": "string", "description": "截止日期 YYYY-MM-DD"},
                "category": {"type": "string", "description": "所属物理分类目录"},
                "summary": {"type": "string", "description": "核心要求：一段话说清谁要什么、交几件、报给谁、什么时间、什么形式"},
                "checklist": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "待办要求细项：逐条可勾选的动作，一条一个动作（核对/起草/填报/审签/盖章/报送/电话确认），带时间节点或报送对象；不要写“梳理办理要点”“高度重视”这类空话",
                },
                "progress": {"type": "string", "description": "初始进展记录（办事过程写这里，不要写成文件）"},
                "auto_ai": {"type": "boolean", "description": "是否让内置规则引擎补全缺失字段；自己已写好 summary/checklist 时建议传 false"},
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
                "status": {"type": "string", "description": "可选新状态: pending | in_progress | archived"},
                "toggle_check_id": {"type": "string", "description": "可选要勾选/取消勾选的细项 ID（如 chk_1）"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "wb_write_draft",
        "description": "直接为指定工作事项（或独立分类目录）起草并保存材料草稿（.md/.txt），并可一键同时排版导出为符合 GB/T 9704—2012 国标格式的 .docx 公文",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "关联的工作事项 ID（若为独立材料可不传）"},
                "filename": {"type": "string", "description": "保存的文件名，如 我县防灾减灾总结汇报_初稿.md"},
                "content": {"type": "string", "description": "撰写的完整材料正文内容"},
                "category": {"type": "string", "description": "独立材料的分类目录（如 03_综合汇报与总结）"},
                "export_gb9704_docx": {"type": "boolean", "description": "是否同时自动生成符合 GB/T 9704—2012 国标格式的 .docx 正式文档"},
                "gw_mode": {"type": "string", "description": "国标公文模式: report (汇报/总结/方案) | redhead (带红头与文号公文)"},
            },
            "required": ["filename", "content"],
        },
    },
    {
        "name": "wb_format_gb9704_docx",
        "description": "将库内已有的任意公文或材料草稿一键排版转换为符合 GB/T 9704—2012 标准的 .docx 公文（支持联动 xkl-gw）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "integer", "description": "文档 ID"},
                "mode": {"type": "string", "description": "report (综合材料模式) | redhead (红头文件模式)"},
                "doc_number": {"type": "string", "description": "可选发文字号"},
                "issuing_org": {"type": "string", "description": "可选发文机关署名"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "wb_search",
        "description": "搜索工作台中的事项、上级来文、历史公文与以前写过的材料（支持仅标题、全文检索、两段式 AI 上下文检索）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词或自然语言问题"},
                "scope": {"type": "string", "description": "title (仅搜标题/文号) | full (标题+全文内容)"},
                "ai_context": {"type": "boolean", "description": "是否启用两段式 AI 语义检索并返回正文切片"},
                "category": {"type": "string", "description": "可选限定分类目录"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "wb_read_document",
        "description": "读取指定公文或材料（doc_id）的完整纯文本内容与本机物理路径",
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
        "description": "立即触发本地物理文件夹与工作台 SQLite/FTS5 索引库的双向增量同步，并检查周期性工作派生",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def handle_mcp_request(req: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = req.get("method", "")
    req_id = req.get("id")
    params = req.get("params") or {}

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

    if method.startswith("notifications/"):
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
        resources = [
            {
                "uri": f"workbench://task/{t['id']}",
                "name": f"[事项 #{t['id']}] {t['title']}",
                "description": t.get("summary") or "",
                "mimeType": "application/json",
            }
            for t in data.get("tasks", [])
        ]
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": resources}}

    if method == "resources/read":
        uri = str(params.get("uri") or "")
        m = urllib.parse.urlparse(uri)
        tid_str = m.path.lstrip("/")
        if tid_str.isdigit():
            init_db()
            conn = get_db()
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (int(tid_str),)).fetchone()
            t_obj = serialize_task(conn, row) if row else {}
            conn.close()
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
        t_args = params.get("arguments") or {}
        try:
            if t_name == "wb_list_tasks":
                res = _do_list(status=t_args.get("status", ""), category=t_args.get("category", ""))
            elif t_name == "wb_add_task":
                files = []
                if t_args.get("file_path"):
                    fp = Path(t_args["file_path"]).expanduser().resolve()
                    if fp.is_file():
                        files.append({"filename": fp.name, "local_source_path": str(fp), "doc_role": "incoming"})
                # 待办细项：支持数组或分号（中英文）分隔的字符串
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
                res = _do_progress(
                    task_id=int(t_args["task_id"]),
                    text=t_args.get("progress_text") or "",
                    status=t_args.get("status") or "",
                    check_id=t_args.get("toggle_check_id") or "",
                )
            elif t_name == "wb_write_draft":
                res = _do_write_draft(
                    task_id=t_args.get("task_id"),
                    filename=t_args.get("filename") or "新建材料草稿.md",
                    content=t_args.get("content") or "",
                    category=t_args.get("category") or "",
                    export_gb9704_docx=bool(t_args.get("export_gb9704_docx", False)),
                    gw_mode=t_args.get("gw_mode") or "report",
                )
            elif t_name == "wb_format_gb9704_docx":
                res = _do_format_gw(
                    doc_id=int(t_args["doc_id"]),
                    mode=t_args.get("mode") or "report",
                    doc_number=t_args.get("doc_number") or "",
                    org=t_args.get("issuing_org") or "",
                )
            elif t_name == "wb_search":
                res = _do_search(
                    query=t_args.get("query") or "",
                    scope=t_args.get("scope") or "full",
                    ai=bool(t_args.get("ai_context", True)),
                    category=t_args.get("category") or "",
                )
            elif t_name == "wb_read_document":
                init_db()
                conn = get_db()
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (int(t_args["doc_id"]),)).fetchone()
                conn.close()
                if not row:
                    res = {"ok": False, "error": "文件不存在"}
                else:
                    d = serialize_doc_light(row)
                    d["extracted_text"] = row["extracted_text"] or ""
                    res = {"ok": True, "document": d}
            elif t_name == "wb_sync_folders":
                init_db()
                st = sync_filesystem(force=True)
                st["spawned_recurring"] = check_and_spawn_all_recurring()
                res = {"ok": True, "sync_stats": st}
            else:
                res = {"ok": False, "error": f"未知的 MCP 工具: {t_name}"}

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(res, ensure_ascii=False, indent=2)}
                    ],
                    "isError": not res.get("ok", True),
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


def run_mcp_stdio_server() -> None:
    """运行标准 MCP stdio 协议循环（自适应换行分隔 JSON-RPC 与 Content-Length 头部模式）"""
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
            length = int(line_str.split(":", 1)[1].strip())
            while True:
                hdr = sys.stdin.buffer.readline().decode("utf-8", errors="ignore").strip()
                if not hdr:
                    break
            raw_body = sys.stdin.buffer.read(length).decode("utf-8", errors="ignore")
        else:
            raw_body = line_str

        try:
            req = json.loads(raw_body)
        except Exception:
            continue

        resp = handle_mcp_request(req)
        if resp is not None:
            out_bytes = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            if use_content_length:
                header = f"Content-Length: {len(out_bytes)}\r\n\r\n".encode("ascii")
                sys.stdout.buffer.write(header + out_bytes)
            else:
                sys.stdout.buffer.write(out_bytes + b"\n")
            sys.stdout.buffer.flush()


# ==================== CLI 命令入口 ====================
def cmd_list(args) -> None:
    print(json.dumps(_do_list(args.status or "", args.category or ""), ensure_ascii=False, indent=2))


def cmd_add(args) -> None:
    files = []
    if args.file:
        for fp in args.file:
            p = Path(fp).expanduser().resolve()
            if p.is_file():
                files.append({
                    "filename": p.name,
                    "local_source_path": str(p),
                    "doc_role": "incoming",
                })
    checklist = None
    if args.checklist:
        checklist = [
            {"id": f"chk_{i+1}", "text": x.strip(), "done": False}
            for i, x in enumerate(args.checklist.split(";"))
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
    init_db()
    p = Path(args.file_path).expanduser().resolve()
    if not p.is_file():
        print(json.dumps({"ok": False, "error": f"文件不存在: {p}"}, ensure_ascii=False))
        sys.exit(1)
    payload = {
        "doc_role": args.role,
        "files": [{"filename": p.name, "local_source_path": str(p), "doc_role": args.role}],
    }
    res = api_call("POST", f"/api/tasks/{args.task_id}/files", payload)
    if not res:
        conn = get_db()
        t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not t_row:
            print(json.dumps({"ok": False, "error": "事项不存在"}, ensure_ascii=False))
            sys.exit(1)
        ws_root = get_workspace_root()
        task_dir = ws_root / t_row["folder_rel_path"]
        task_dir.mkdir(parents=True, exist_ok=True)
        prefix = "[来文]_" if args.role == "incoming" else "[材料]_"
        dest = resolve_unique_dest_path(task_dir, prefix + sanitize_filename(p.name), p.read_bytes())
        shutil.copy2(p, dest)
        conn.close()
        sync_filesystem(force=True)
        conn = get_db()
        write_task_readme_file(conn, args.task_id)
        conn.commit()
        u_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        res = {"ok": True, "task": serialize_task(conn, u_row)}
        conn.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_draft(args) -> None:
    res = _do_write_draft(
        task_id=args.task_id,
        filename=args.filename,
        content=args.content,
        category=args.category or "",
        export_gb9704_docx=args.to_docx,
        gw_mode=args.mode,
    )
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_format_gw(args) -> None:
    res = _do_format_gw(args.doc_id, mode=args.mode, doc_number=args.doc_number or "", org=args.org or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_recurring(args) -> None:
    init_db()
    if args.spawn:
        t = spawn_task_from_recurring_rule(args.spawn, force_manual=True)
        print(json.dumps({"ok": bool(t), "task": t}, ensure_ascii=False, indent=2))
        return
    conn = get_db()
    rows = [dict(r) for r in conn.execute("SELECT * FROM recurring_rules ORDER BY id ASC").fetchall()]
    conn.close()
    print(json.dumps({"ok": True, "recurring_rules": rows}, ensure_ascii=False, indent=2))


def cmd_search(args) -> None:
    res = _do_search(args.query, scope=args.scope, ai=args.ai, category=args.category or "")
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_sync(args) -> None:
    init_db()
    stats = sync_filesystem(force=True)
    stats["spawned_recurring"] = check_and_spawn_all_recurring()
    print(json.dumps({"ok": True, "sync_stats": stats}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=f"GW-Workbench v{VERSION} 本地 Agent CLI 与 MCP Server")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="列出工作台事项")
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
    p_att.set_defaults(func=cmd_attach)

    p_draft = sub.add_parser("draft", help="直接为指定事项写入材料草稿，并可选一键排版导出为国标 GB/T 9704 .docx 公文")
    p_draft.add_argument("task_id", type=int, help="事项 ID")
    p_draft.add_argument("--filename", required=True, help="草稿文件名，如 冬季防灾汇报.md")
    p_draft.add_argument("--content", required=True, help="材料正文内容")
    p_draft.add_argument("--category", help="分类目录")
    p_draft.add_argument("--to-docx", action="store_true", help="同时生成符合 GB/T 9704—2012 国标格式的 .docx 正式公文")
    p_draft.add_argument("--mode", choices=["report", "redhead"], default="report", help="国标排版模式")
    p_draft.set_defaults(func=cmd_draft)

    p_fmt = sub.add_parser("format-gw", help="将库内文档一键排版导出为符合 GB/T 9704—2012 国标格式的 .docx 公文")
    p_fmt.add_argument("doc_id", type=int, help="文档 ID")
    p_fmt.add_argument("--mode", choices=["report", "redhead"], default="report", help="report=汇报材料模式, redhead=红头文件模式")
    p_fmt.add_argument("--doc-number", help="自定义发文字号")
    p_fmt.add_argument("--org", help="自定义发文机关署名")
    p_fmt.set_defaults(func=cmd_format_gw)

    p_rec = sub.add_parser("recurring", help="查看或手动触发周期性/常态化工作规则")
    p_rec.add_argument("--spawn", type=int, help="立即派生指定规则 ID 的本期工作事项")
    p_rec.set_defaults(func=cmd_recurring)

    p_search = sub.add_parser("search", help="搜索工作台事项与公文材料库")
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
