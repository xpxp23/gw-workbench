#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与材料智能工作台 (GW-Workbench) 本地 Agent 专用命令行工具 (CLI)
供本地 AI Agent (如 Claude Code / Cline / Cursor / 自定义脚本) 或终端直接调用
支持直接通过 HTTP API 操作工作台，或直接离线操作本地 SQLite 与物理文件夹！
"""

import argparse
import json
import sys
import urllib.request
import urllib.parse
from pathlib import Path

from server import (
    load_config,
    init_db,
    get_db,
    sync_filesystem,
    create_task_record,
    execute_search,
    serialize_task,
    serialize_doc,
    build_highlight_snippet,
    write_task_readme_file,
    get_workspace_root,
    sanitize_filename,
)
from ai_engine import extract_search_keywords, synthesize_ai_search_answer


def api_call(method: str, path: str, payload: dict = None) -> dict:
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
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def cmd_list(args) -> None:
    init_db()
    sync_filesystem()
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    tasks = []
    for r in rows:
        if args.status and r["status"] != args.status:
            continue
        if args.category and r["category"] != args.category:
            continue
        tasks.append(serialize_task(conn, r))
    conn.close()
    print(json.dumps({"ok": True, "count": len(tasks), "tasks": tasks}, ensure_ascii=False, indent=2))


def cmd_add(args) -> None:
    init_db()
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
    res = api_call("POST", "/api/tasks", payload)
    if not res:
        task_obj = create_task_record(payload)
        res = {"ok": True, "task": task_obj}
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_progress(args) -> None:
    init_db()
    payload = {}
    if args.text:
        payload["append_progress"] = args.text
    if args.status:
        payload["status"] = args.status
    if args.check_id:
        payload["toggle_check_id"] = args.check_id

    res = api_call("PUT", f"/api/tasks/{args.task_id}", payload)
    if not res:
        from datetime import datetime
        conn = get_db()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not row:
            print(json.dumps({"ok": False, "error": f"事项 #{args.task_id} 不存在"}, ensure_ascii=False))
            sys.exit(1)
        prog = json.loads(row["progress_json"] or "[]")
        if args.text:
            prog.insert(0, {"time": datetime.now().strftime("%m-%d %H:%M"), "text": args.text})
        chk = json.loads(row["checklist_json"] or "[]")
        if args.check_id:
            for c in chk:
                if c.get("id") == args.check_id:
                    c["done"] = not bool(c.get("done"))
        new_st = args.status or row["status"]
        conn.execute(
            "UPDATE tasks SET progress_json = ?, checklist_json = ?, status = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(prog, ensure_ascii=False),
                json.dumps(chk, ensure_ascii=False),
                new_st,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                args.task_id,
            ),
        )
        write_task_readme_file(conn, args.task_id)
        conn.commit()
        u_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        res = {"ok": True, "task": serialize_task(conn, u_row)}
        conn.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_attach(args) -> None:
    init_db()
    p = Path(args.file_path).expanduser().resolve()
    if not p.is_file():
        print(json.dumps({"ok": False, "error": f"文件不存在: {p}"}, ensure_ascii=False))
        sys.exit(1)
    payload = {
        "doc_role": args.role,
        "files": [
            {
                "filename": p.name,
                "local_source_path": str(p),
                "doc_role": args.role,
            }
        ],
    }
    res = api_call("POST", f"/api/tasks/{args.task_id}/files", payload)
    if not res:
        import shutil
        conn = get_db()
        t_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        if not t_row:
            print(json.dumps({"ok": False, "error": "事项不存在"}, ensure_ascii=False))
            sys.exit(1)
        ws_root = get_workspace_root()
        task_dir = ws_root / t_row["folder_rel_path"]
        task_dir.mkdir(parents=True, exist_ok=True)
        prefix = "[来文]_" if args.role == "incoming" else "[材料]_"
        dest = task_dir / (prefix + sanitize_filename(p.name))
        shutil.copy2(p, dest)
        conn.close()
        sync_filesystem()
        conn = get_db()
        write_task_readme_file(conn, args.task_id)
        conn.commit()
        u_row = conn.execute("SELECT * FROM tasks WHERE id = ?", (args.task_id,)).fetchone()
        res = {"ok": True, "task": serialize_task(conn, u_row)}
        conn.close()
    print(json.dumps(res, ensure_ascii=False, indent=2))


def cmd_search(args) -> None:
    init_db()
    sync_filesystem()
    if args.ai:
        res = api_call("POST", "/api/agent/context-search", {"query": args.query, "category": args.category or ""})
        if not res:
            kws = extract_search_keywords(args.query)
            conn = get_db()
            all_tasks = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
            scored_tasks = []
            for r in all_tasks:
                if args.category and r["category"] != args.category:
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
                if args.category and r["category"] != args.category:
                    continue
                hay_title = (r["filename"] or "").lower()
                hay_body = (r["extracted_text"] or "").lower()
                hits = sum(3 if kw.lower() in hay_title else (1 if kw.lower() in hay_body else 0) for kw in kws)
                if hits > 0:
                    snip = build_highlight_snippet(r["extracted_text"] or r["filename"], kws)
                    d_obj = serialize_doc(r, snippet=snip)
                    d_obj["extracted_text_chunk"] = (r["extracted_text"] or "")[:2000]
                    scored_docs.append((hits, d_obj))
            scored_docs.sort(key=lambda x: (-x[0], -x[1]["id"]))
            top_docs = [x[1] for x in scored_docs[:18]]
            conn.close()

            ans = synthesize_ai_search_answer(args.query, kws, top_tasks, top_docs, config=load_config())
            res = {"query": args.query, "keywords": kws, "answer": ans["answer"], "tasks": top_tasks, "documents": top_docs}
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        tasks, docs = execute_search(args.query, scope=args.scope, category=args.category or "")
        print(json.dumps({
            "ok": True,
            "query": args.query,
            "scope": args.scope,
            "tasks": tasks,
            "documents": docs,
        }, ensure_ascii=False, indent=2))


def cmd_sync(args) -> None:
    init_db()
    stats = sync_filesystem()
    print(json.dumps({"ok": True, "sync_stats": stats}, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="GW-Workbench 本地 Agent 与命令行操控工具")
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
    p_add.add_argument("--no-ai", action="store_true", help="跳过AI自动解析，完全按传入参数创建")
    p_add.set_defaults(func=cmd_add)

    p_prog = sub.add_parser("progress", help="更新事项进展、勾选清单或修改归档状态")
    p_prog.add_argument("task_id", type=int, help="事项 ID")
    p_prog.add_argument("text", nargs="?", default="", help="新增进展内容")
    p_prog.add_argument("--status", choices=["pending", "in_progress", "archived"], help="修改状态（如 archived 归档）")
    p_prog.add_argument("--check-id", help="切换勾选状态的细项 ID（如 chk_1）")
    p_prog.set_defaults(func=cmd_progress)

    p_att = sub.add_parser("attach", help="向指定事项挂载【我写的材料】或【来文原件】")
    p_att.add_argument("task_id", type=int, help="事项 ID")
    p_att.add_argument("file_path", help="本地文件路径")
    p_att.add_argument("--role", choices=["draft", "incoming"], default="draft", help="draft=我写的材料, incoming=来文原件")
    p_att.set_defaults(func=cmd_attach)

    p_search = sub.add_parser("search", help="搜索工作台事项与公文材料库")
    p_search.add_argument("query", help="搜索关键词或自然语言问题")
    p_search.add_argument("--scope", choices=["title", "full"], default="full", help="title=仅标题/文号, full=标题+全文内容")
    p_search.add_argument("--ai", action="store_true", help="启用两段式 AI 语义检索与上下文提取")
    p_search.add_argument("--category", help="限定物理分类目录")
    p_search.set_defaults(func=cmd_search)

    p_sync = sub.add_parser("sync", help="立即执行本地物理文件夹与数据库双向同步")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
