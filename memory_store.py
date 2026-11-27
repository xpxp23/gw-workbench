#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
memory_store.py — 透明常驻业务记忆与红头规则库 (GW-Workbench v3.3.5)

设计目标：
1. 透明可控：SQLite 结构化存储 + 本地物理 Markdown 镜像（workspace_files/00_业务记忆与规则.md），
   用户在 Web UI 或本地编辑器中随时可查、可改、可一键启停。
2. Agent 易懂防误解：按 contact(联络名片) / redhead(红头配置) / recipient(发文对象) / rule(注意事项与红线)
   四类清晰划分，单条规则精炼直白。
3. 极低 Token 成本：L0 常驻简报（≤15条、≤500 Tokens）+ L1 按关键词召回 + since_rev 版本号短路
   （规则未变更时仅返回 ~10 Tokens）。
4. 联动排版引擎：redhead 类型的 meta_json 可直接驱动 gw_formatter.py 实现“一岗多红头”精准排版。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VALID_KINDS = ("contact", "redhead", "recipient", "rule")
KIND_LABELS = {
    "contact": "联络名片",
    "redhead": "红头配置",
    "recipient": "发文对象",
    "rule": "注意事项",
}

VALID_SCOPES = ("global", "draft", "task", "report", "archive", "mobile")
SCOPE_LABELS = {
    "global": "全局常驻",
    "draft": "起草排版",
    "task": "收文建档",
    "report": "汇报总结",
    "archive": "资料存档",
    "mobile": "手机端",
}

VALID_REVIEW_STATUS = ("approved", "pending_review")
MIRROR_FILENAME = "00_业务记忆与规则.md"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_memory_tables(conn: sqlite3.Connection) -> None:
    """幂等初始化常驻记忆表与版本号元表。"""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS agent_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mem_key TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'rule',
            scope TEXT NOT NULL DEFAULT 'global',
            priority INTEGER NOT NULL DEFAULT 2,
            title TEXT NOT NULL,
            rule_text TEXT NOT NULL,
            meta_json TEXT NOT NULL DEFAULT '{}',
            triggers_json TEXT NOT NULL DEFAULT '[]',
            enabled INTEGER NOT NULL DEFAULT 1,
            review_status TEXT NOT NULL DEFAULT 'approved',
            source TEXT NOT NULL DEFAULT 'user_ui',
            hit_count INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_meta (
            k TEXT PRIMARY KEY,
            v TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_memories_active
            ON agent_memories(enabled, review_status, scope, priority ASC, id ASC);

        CREATE INDEX IF NOT EXISTS idx_memories_kind
            ON agent_memories(kind, enabled, priority ASC);
        """
    )
    row = conn.execute("SELECT v FROM memory_meta WHERE k = 'revision'").fetchone()
    if row is None:
        conn.execute("INSERT OR IGNORE INTO memory_meta(k, v) VALUES ('revision', '1')")


def get_memory_revision(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT v FROM memory_meta WHERE k = 'revision'").fetchone()
        return int(row[0]) if row else 1
    except Exception:
        return 1


def bump_memory_revision(conn: sqlite3.Connection) -> int:
    cur = get_memory_revision(conn) + 1
    conn.execute(
        "INSERT INTO memory_meta(k, v) VALUES ('revision', ?) "
        "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (str(cur),),
    )
    return cur


def _safe_json_loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        val = json.loads(str(raw))
        return val if isinstance(val, type(default)) else default
    except Exception:
        return default


def _normalize_mem_key(mem_key: str, kind: str, title: str) -> str:
    k = re.sub(r"[^a-zA-Z0-9._\-]+", "_", (mem_key or "").strip()).strip("._-")
    if k:
        return k[:64]
    digest = hashlib.sha1(f"{kind}:{title}".encode("utf-8")).hexdigest()[:8]
    return f"{kind}.{digest}"


def _normalize_triggers(raw: Any) -> List[str]:
    if isinstance(raw, list):
        items = [str(x).strip() for x in raw if str(x).strip()]
    elif isinstance(raw, str):
        items = [x.strip() for x in re.split(r"[,，、;\s]+", raw) if x.strip()]
    else:
        items = []
    seen = set()
    out: List[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it[:24])
    return out[:12]


def _serialize_memory_row(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    meta = _safe_json_loads(d.get("meta_json"), {})
    triggers = _safe_json_loads(d.get("triggers_json"), [])
    return {
        "id": d["id"],
        "mem_key": d["mem_key"],
        "kind": d["kind"],
        "kind_label": KIND_LABELS.get(d["kind"], d["kind"]),
        "scope": d["scope"],
        "scope_label": SCOPE_LABELS.get(d["scope"], d["scope"]),
        "priority": int(d.get("priority", 2)),
        "title": d["title"],
        "rule_text": d["rule_text"],
        "meta": meta,
        "triggers": triggers,
        "enabled": bool(d.get("enabled", 1)),
        "review_status": d.get("review_status", "approved"),
        "source": d.get("source", "user_ui"),
        "hit_count": int(d.get("hit_count", 0)),
        "last_used_at": d.get("last_used_at") or "",
        "created_at": d.get("created_at") or "",
        "updated_at": d.get("updated_at") or "",
    }


def sync_memories_to_markdown(conn: sqlite3.Connection, ws_root: Optional[Path]) -> Optional[Path]:
    """将全部业务记忆同步输出为人类可读的本地 Markdown 镜像。"""
    if ws_root is None:
        return None
    try:
        ws_root = Path(ws_root)
        if not ws_root.exists():
            return None
        rev = get_memory_revision(conn)
        rows = conn.execute(
            "SELECT * FROM agent_memories ORDER BY kind ASC, priority ASC, id ASC"
        ).fetchall()
        items = [_serialize_memory_row(r) for r in rows]

        lines: List[str] = [
            "# 🧠 科室常驻业务记忆与红头规则库",
            f"<!-- gw_mem_rev: {rev} | updated_at: {_now_str()} -->",
            "",
            "> 本文件由工作台自动同步生成，供人工随时查阅。建议在工作台【🧠 记忆与规则】页签或通过 Agent 指令实时修改。",
            "",
        ]
        by_kind: Dict[str, List[Dict[str, Any]]] = {k: [] for k in VALID_KINDS}
        for it in items:
            by_kind.setdefault(it["kind"], []).append(it)

        for kind in VALID_KINDS:
            group = by_kind.get(kind) or []
            lines.append(f"## {KIND_LABELS[kind]}（{len(group)}）")
            lines.append("")
            if not group:
                lines.append("_暂无条目_")
                lines.append("")
                continue
            for it in group:
                status_tag = "启用" if it["enabled"] else "已停用"
                if it["review_status"] == "pending_review":
                    status_tag += " · 待审核"
                lines.append(
                    f"- **[P{it['priority']}][{it['mem_key']}] {it['title']}** "
                    f"（{it['scope_label']}｜{status_tag}）"
                )
                lines.append(f"  - 内容：{it['rule_text']}")
                if it["kind"] == "redhead" and it["meta"]:
                    m = it["meta"]
                    parts = []
                    if m.get("display_header"):
                        parts.append(f"抬头={m['display_header']}")
                    if m.get("doc_num_prefix"):
                        parts.append(f"代字={m['doc_num_prefix']}")
                    if m.get("default_recipients"):
                        parts.append(f"主送={m['default_recipients']}")
                    if m.get("default_cc"):
                        parts.append(f"抄送={m['default_cc']}")
                    if m.get("issuer_name"):
                        parts.append(f"印发={m['issuer_name']}")
                    if parts:
                        lines.append(f"  - 红头参数：{' ｜ '.join(parts)}")
                if it["triggers"]:
                    lines.append(f"  - 触发词：{', '.join(it['triggers'])}")
            lines.append("")

        target = ws_root / MIRROR_FILENAME
        tmp = ws_root / f".{MIRROR_FILENAME}.tmp"
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(target)
        return target
    except Exception:
        return None


def list_memories(
    conn: sqlite3.Connection,
    kind: str = "",
    scope: str = "",
    enabled_only: bool = False,
    include_pending: bool = True,
    query: str = "",
) -> Dict[str, Any]:
    init_memory_tables(conn)
    sql = "SELECT * FROM agent_memories WHERE 1=1"
    params: List[Any] = []
    if kind and kind in VALID_KINDS:
        sql += " AND kind = ?"
        params.append(kind)
    if scope and scope in VALID_SCOPES:
        sql += " AND scope = ?"
        params.append(scope)
    if enabled_only:
        sql += " AND enabled = 1"
    if not include_pending:
        sql += " AND review_status = 'approved'"
    sql += " ORDER BY CASE review_status WHEN 'pending_review' THEN 0 ELSE 1 END, enabled DESC, priority ASC, id ASC"

    rows = conn.execute(sql, params).fetchall()
    items = [_serialize_memory_row(r) for r in rows]

    q = (query or "").strip().lower()
    if q:
        terms = [t for t in re.split(r"\s+", q) if t]
        filtered = []
        for it in items:
            hay = " ".join(
                [
                    it["mem_key"],
                    it["title"],
                    it["rule_text"],
                    " ".join(it["triggers"]),
                    json.dumps(it["meta"], ensure_ascii=False),
                ]
            ).lower()
            if all(t in hay for t in terms):
                filtered.append(it)
        items = filtered

    all_rows = conn.execute(
        "SELECT kind, enabled, review_status FROM agent_memories"
    ).fetchall()
    counts = {k: 0 for k in VALID_KINDS}
    active_count = 0
    pending_count = 0
    for r in all_rows:
        k = r["kind"]
        if k in counts:
            counts[k] += 1
        if r["enabled"] and r["review_status"] == "approved":
            active_count += 1
        if r["review_status"] == "pending_review":
            pending_count += 1

    return {
        "ok": True,
        "revision": get_memory_revision(conn),
        "total": len(items),
        "active_count": active_count,
        "pending_count": pending_count,
        "counts_by_kind": counts,
        "memories": items,
    }


def upsert_memory(
    conn: sqlite3.Connection,
    payload: Dict[str, Any],
    ws_root: Optional[Path] = None,
) -> Dict[str, Any]:
    init_memory_tables(conn)
    title = str(payload.get("title") or "").strip()
    rule_text = str(payload.get("rule_text") or payload.get("content") or "").strip()
    if not title:
        raise ValueError("标题 title 不能为空")
    if not rule_text:
        raise ValueError("规则/记忆内容 rule_text 不能为空")

    kind = str(payload.get("kind") or "rule").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError(f"无效的分类 kind: {kind}，可选: {', '.join(VALID_KINDS)}")

    scope = str(payload.get("scope") or "global").strip().lower()
    if scope not in VALID_SCOPES:
        scope = "global"

    try:
        priority = int(payload.get("priority", 2))
    except Exception:
        priority = 2
    priority = max(1, min(3, priority))

    enabled = 1 if payload.get("enabled", True) else 0
    source = str(payload.get("source") or "user_ui").strip()[:32] or "user_ui"
    review_status = str(payload.get("review_status") or "approved").strip()
    if review_status not in VALID_REVIEW_STATUS:
        review_status = "approved"

    meta_raw = payload.get("meta")
    if isinstance(meta_raw, str):
        meta = _safe_json_loads(meta_raw, {})
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        meta = {}

    triggers = _normalize_triggers(payload.get("triggers"))
    now = _now_str()

    target_id = payload.get("id")
    existing = None
    if target_id is not None and str(target_id).isdigit():
        existing = conn.execute(
            "SELECT * FROM agent_memories WHERE id = ?", (int(target_id),)
        ).fetchone()

    mem_key = _normalize_mem_key(str(payload.get("mem_key") or payload.get("key") or ""), kind, title)
    if existing is None:
        existing = conn.execute(
            "SELECT * FROM agent_memories WHERE mem_key = ?", (mem_key,)
        ).fetchone()

    if existing is not None:
        # 保留原 mem_key（除非显式改名且不冲突）
        final_key = mem_key if payload.get("mem_key") or payload.get("key") else existing["mem_key"]
        conn.execute(
            """
            UPDATE agent_memories
            SET mem_key=?, kind=?, scope=?, priority=?, title=?, rule_text=?,
                meta_json=?, triggers_json=?, enabled=?, review_status=?, source=?, updated_at=?
            WHERE id=?
            """,
            (
                final_key,
                kind,
                scope,
                priority,
                title[:48],
                rule_text[:600],
                json.dumps(meta, ensure_ascii=False),
                json.dumps(triggers, ensure_ascii=False),
                enabled,
                review_status,
                source,
                now,
                existing["id"],
            ),
        )
        mem_id = existing["id"]
        action = "updated"
    else:
        cur = conn.execute(
            """
            INSERT INTO agent_memories
                (mem_key, kind, scope, priority, title, rule_text, meta_json, triggers_json,
                 enabled, review_status, source, hit_count, last_used_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?, ?)
            """,
            (
                mem_key,
                kind,
                scope,
                priority,
                title[:48],
                rule_text[:600],
                json.dumps(meta, ensure_ascii=False),
                json.dumps(triggers, ensure_ascii=False),
                enabled,
                review_status,
                source,
                now,
                now,
            ),
        )
        mem_id = cur.lastrowid
        action = "created"

    rev = bump_memory_revision(conn)
    row = conn.execute("SELECT * FROM agent_memories WHERE id = ?", (mem_id,)).fetchone()
    sync_memories_to_markdown(conn, ws_root)
    return {
        "ok": True,
        "action": action,
        "revision": rev,
        "memory": _serialize_memory_row(row),
    }


def _find_memory_row(conn: sqlite3.Connection, id_or_key: Any) -> Optional[sqlite3.Row]:
    val = str(id_or_key or "").strip()
    if not val:
        return None
    if val.isdigit():
        row = conn.execute("SELECT * FROM agent_memories WHERE id = ?", (int(val),)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM agent_memories WHERE mem_key = ?", (val,)).fetchone()


def toggle_memory(
    conn: sqlite3.Connection,
    id_or_key: Any,
    enabled: Optional[bool] = None,
    ws_root: Optional[Path] = None,
) -> Dict[str, Any]:
    init_memory_tables(conn)
    row = _find_memory_row(conn, id_or_key)
    if not row:
        raise KeyError(f"未找到记忆条目: {id_or_key}")
    new_enabled = (0 if row["enabled"] else 1) if enabled is None else (1 if enabled else 0)
    conn.execute(
        "UPDATE agent_memories SET enabled=?, updated_at=? WHERE id=?",
        (new_enabled, _now_str(), row["id"]),
    )
    rev = bump_memory_revision(conn)
    updated = conn.execute("SELECT * FROM agent_memories WHERE id = ?", (row["id"],)).fetchone()
    sync_memories_to_markdown(conn, ws_root)
    return {"ok": True, "revision": rev, "memory": _serialize_memory_row(updated)}


def approve_memory(
    conn: sqlite3.Connection,
    id_or_key: Any,
    ws_root: Optional[Path] = None,
) -> Dict[str, Any]:
    init_memory_tables(conn)
    row = _find_memory_row(conn, id_or_key)
    if not row:
        raise KeyError(f"未找到记忆条目: {id_or_key}")
    conn.execute(
        "UPDATE agent_memories SET review_status='approved', enabled=1, updated_at=? WHERE id=?",
        (_now_str(), row["id"]),
    )
    rev = bump_memory_revision(conn)
    updated = conn.execute("SELECT * FROM agent_memories WHERE id = ?", (row["id"],)).fetchone()
    sync_memories_to_markdown(conn, ws_root)
    return {"ok": True, "revision": rev, "memory": _serialize_memory_row(updated)}


def delete_memory(
    conn: sqlite3.Connection,
    id_or_key: Any,
    ws_root: Optional[Path] = None,
) -> Dict[str, Any]:
    init_memory_tables(conn)
    row = _find_memory_row(conn, id_or_key)
    if not row:
        raise KeyError(f"未找到记忆条目: {id_or_key}")
    conn.execute("DELETE FROM agent_memories WHERE id = ?", (row["id"],))
    rev = bump_memory_revision(conn)
    sync_memories_to_markdown(conn, ws_root)
    return {"ok": True, "revision": rev, "deleted_id": row["id"], "deleted_key": row["mem_key"]}


def _format_memory_line(it: Dict[str, Any]) -> str:
    line = f"[P{it['priority']}][{it['kind']}][{it['mem_key']}] {it['title']}：{it['rule_text']}"
    if it["kind"] == "redhead" and it["meta"]:
        m = it["meta"]
        extras = []
        if m.get("display_header"):
            extras.append(f"抬头={m['display_header']}")
        if m.get("doc_num_prefix"):
            extras.append(f"代字={m['doc_num_prefix']}")
        if m.get("default_recipients"):
            extras.append(f"主送={m['default_recipients']}")
        if m.get("default_cc"):
            extras.append(f"抄送={m['default_cc']}")
        if extras:
            line += "（" + "｜".join(extras) + "）"
    return line


def build_memory_brief(
    conn: sqlite3.Connection,
    scene: str = "",
    since_rev: int = 0,
    max_items: int = 15,
    max_chars: int = 1200,
    record_hits: bool = True,
) -> Dict[str, Any]:
    """
    L0 常驻精简简报：
    - 传入 since_rev 且未变更时，零冗余短路返回 `OK rev=N (unchanged)`（~10 Tokens）。
    - 否则返回 global 常驻项 + 当前 scene 专项，严格控制字符预算。
    """
    init_memory_tables(conn)
    rev = get_memory_revision(conn)
    try:
        since_val = int(since_rev or 0)
    except Exception:
        since_val = 0

    if since_val > 0 and since_val == rev:
        return {
            "ok": True,
            "unchanged": True,
            "revision": rev,
            "count": 0,
            "text": f"OK rev={rev} (unchanged)",
            "memories": [],
        }

    scene_norm = (scene or "").strip().lower()
    if scene_norm not in VALID_SCOPES:
        scene_norm = ""

    if scene_norm and scene_norm != "global":
        rows = conn.execute(
            """
            SELECT * FROM agent_memories
            WHERE enabled = 1 AND review_status = 'approved'
              AND (scope = 'global' OR scope = ? OR priority = 1)
            ORDER BY CASE WHEN scope = ? THEN 0 ELSE 1 END, priority ASC, id ASC
            """,
            (scene_norm, scene_norm),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM agent_memories
            WHERE enabled = 1 AND review_status = 'approved'
              AND (scope = 'global' OR priority = 1)
            ORDER BY priority ASC, id ASC
            """
        ).fetchall()

    selected: List[Dict[str, Any]] = []
    lines: List[str] = []
    used_chars = 0
    max_items = max(1, min(30, int(max_items or 15)))
    max_chars = max(200, min(4000, int(max_chars or 1200)))

    for r in rows:
        if len(selected) >= max_items:
            break
        it = _serialize_memory_row(r)
        line = _format_memory_line(it)
        if used_chars + len(line) + 1 > max_chars and selected:
            break
        selected.append(it)
        lines.append(line)
        used_chars += len(line) + 1

    if record_hits and selected:
        now = _now_str()
        ids = [it["id"] for it in selected]
        placeholders = ",".join("?" for _ in ids)
        try:
            conn.execute(
                f"UPDATE agent_memories SET hit_count = hit_count + 1, last_used_at = ? WHERE id IN ({placeholders})",
                [now] + ids,
            )
        except Exception:
            pass

    header = f"[GW-MEM rev={rev} | count={len(selected)}" + (
        f" | scene={scene_norm}]" if scene_norm else "]"
    )
    if not lines:
        text = f"{header}\n(暂无启用的常驻业务记忆；用户交代邮箱/电话/红头/发文对象等规则时请用 wb_mem_set 写入)"
    else:
        text = header + "\n" + "\n".join(lines)

    return {
        "ok": True,
        "unchanged": False,
        "revision": rev,
        "count": len(selected),
        "text": text,
        "memories": selected,
    }


def search_memories(
    conn: sqlite3.Connection,
    query: str,
    kind: str = "",
    scope: str = "",
    top_k: int = 5,
) -> Dict[str, Any]:
    """L1 按关键词/触发词检索相关业务记忆与红头模板。"""
    init_memory_tables(conn)
    sql = "SELECT * FROM agent_memories WHERE enabled = 1 AND review_status = 'approved'"
    params: List[Any] = []
    if kind and kind in VALID_KINDS:
        sql += " AND kind = ?"
        params.append(kind)
    if scope and scope in VALID_SCOPES:
        sql += " AND (scope = ? OR scope = 'global')"
        params.append(scope)

    rows = conn.execute(sql, params).fetchall()
    q = (query or "").strip().lower()
    terms = [t for t in re.split(r"[\s,，、/]+", q) if t]

    scored: List[Tuple[int, Dict[str, Any]]] = []
    for r in rows:
        it = _serialize_memory_row(r)
        if not terms:
            scored.append((10 - it["priority"], it))
            continue
        score = 0
        title_l = it["title"].lower()
        key_l = it["mem_key"].lower()
        rule_l = it["rule_text"].lower()
        trig_l = " ".join(it["triggers"]).lower()
        meta_l = json.dumps(it["meta"], ensure_ascii=False).lower()
        for t in terms:
            if t in trig_l:
                score += 6
            if t in title_l or t in key_l:
                score += 5
            if t in rule_l:
                score += 3
            if t in meta_l:
                score += 3
        if score > 0:
            score += max(0, 4 - it["priority"])
            scored.append((score, it))

    scored.sort(key=lambda x: (-x[0], x[1]["priority"], x[1]["id"]))
    top_k = max(1, min(20, int(top_k or 5)))
    hits = [it for _, it in scored[:top_k]]

    rev = get_memory_revision(conn)
    if not hits:
        text = f"[GW-MEM-SEARCH rev={rev} | query={query} | hits=0]"
    else:
        text = (
            f"[GW-MEM-SEARCH rev={rev} | query={query} | hits={len(hits)}]\n"
            + "\n".join(_format_memory_line(it) for it in hits)
        )
    return {
        "ok": True,
        "revision": rev,
        "count": len(hits),
        "text": text,
        "memories": hits,
    }


def get_redhead_profiles(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """返回所有启用的红头配置，供 gw_formatter.py 与前端导出弹窗直接联动。"""
    init_memory_tables(conn)
    rows = conn.execute(
        """
        SELECT * FROM agent_memories
        WHERE kind = 'redhead' AND enabled = 1 AND review_status = 'approved'
        ORDER BY priority ASC, id ASC
        """
    ).fetchall()
    profiles: List[Dict[str, Any]] = []
    for r in rows:
        it = _serialize_memory_row(r)
        m = it["meta"] or {}
        profiles.append(
            {
                "id": it["id"],
                "mem_key": it["mem_key"],
                "title": it["title"],
                "rule_text": it["rule_text"],
                "display_header": str(m.get("display_header") or it["title"]).strip(),
                "doc_num_prefix": str(m.get("doc_num_prefix") or "").strip(),
                "style_type": str(m.get("style_type") or "redhead").strip(),
                "default_recipients": str(m.get("default_recipients") or "").strip(),
                "default_cc": str(m.get("default_cc") or "").strip(),
                "issuer_name": str(m.get("issuer_name") or "").strip(),
                "triggers": it["triggers"],
            }
        )
    return profiles


def get_active_memory_prompt(
    conn: sqlite3.Connection,
    scope: str = "",
    max_chars: int = 800,
) -> Tuple[str, List[Dict[str, Any]]]:
    """供 ai_engine.py 注入 System Prompt 的轻量常驻记忆片段与命中列表。"""
    brief = build_memory_brief(
        conn,
        scene=scope,
        since_rev=0,
        max_items=12,
        max_chars=max_chars,
        record_hits=False,
    )
    mems = brief.get("memories") or []
    if not mems:
        return "", []
    lines = ["【本单位常驻业务记忆与办件规则（必须严格遵守）】"]
    applied: List[Dict[str, Any]] = []
    for it in mems:
        lines.append("- " + _format_memory_line(it))
        applied.append({"id": it["id"], "mem_key": it["mem_key"], "title": it["title"], "kind": it["kind"]})
    return "\n".join(lines), applied
