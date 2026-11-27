#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
digest_service.py — 外出手机端低成本待办摘要导出引擎 (GW-Workbench v3.3.5)

设计目标：
1. 零扫盘、严格只读：不调用 serialize_task() 的磁盘 iterdir()，不读 raw_notice_text 原文，
   附件数量通过单条聚合 SQL 统计，支持强 ETag（304 零流量刷新）。
2. 极低 Token 成本：提供 4 套面向手机竖屏/微信转发的纯文本轻量模板（mobile / checklist / report / detail），
   无需生成文件、无需 Agent 二次改写，单次拉取较 wb_list_tasks 节省 90%+ Token。
3. 单一事实源：server.py (/api/tasks/digest) 与 wb.py (digest / MCP wb_task_digest) 共用同一纯函数，消除双端分歧。
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

VALID_TEMPLATES = ("mobile", "checklist", "report", "detail")
VALID_PRESETS = ("all", "urgent", "today", "week")
VALID_STATUSES = ("active", "in_progress", "pending", "archived", "all")

PRIORITY_RANK = {"紧急": 0, "重要": 1, "普通": 2}


def _parse_date_safe(s: str) -> Optional[date]:
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def _deadline_info(deadline_str: str, today: date) -> Tuple[Optional[int], str]:
    """返回 (剩余天数, 紧凑倒计时标签)。负数表示已逾期。"""
    d = _parse_date_safe(deadline_str)
    if d is None:
        return None, "无期限"
    delta = (d - today).days
    mmdd = d.strftime("%m-%d")
    if delta < 0:
        return delta, f"{mmdd} 逾期{abs(delta)}天"
    if delta == 0:
        return 0, f"{mmdd} 今日截止"
    if delta == 1:
        return 1, f"{mmdd} 明日截止"
    return delta, f"{mmdd} 剩{delta}天"


def _safe_json_list(raw: Any) -> List[Any]:
    if isinstance(raw, list):
        return raw
    if not raw:
        return []
    try:
        val = json.loads(str(raw))
        return val if isinstance(val, list) else []
    except Exception:
        return []


def compute_digest_etag(conn: sqlite3.Connection) -> str:
    """基于 tasks 最新更新时间与总数生成轻量 ETag。"""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(updated_at), ''), COALESCE(SUM(CASE WHEN status='archived' THEN 1 ELSE 0 END), 0) FROM tasks"
    ).fetchone()
    raw = f"{row[0]}:{row[1]}:{row[2]}" if row else "0::0"
    return 'W/"' + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] + '"'


def _parse_ids(ids_param: Any) -> List[int]:
    if not ids_param:
        return []
    if isinstance(ids_param, (list, tuple)):
        out = []
        for x in ids_param:
            if str(x).strip().isdigit():
                out.append(int(str(x).strip()))
        return out
    return [int(x) for x in re.findall(r"\d+", str(ids_param))]


def _match_due(delta: Optional[int], d_obj: Optional[date], due_filter: str, today: date) -> bool:
    f = (due_filter or "").strip().lower()
    if not f:
        return True
    if f == "overdue":
        return delta is not None and delta < 0
    if f == "today":
        return delta is not None and delta <= 0
    m_days = re.match(r"^(\d+)d$", f)
    if m_days:
        n = int(m_days.group(1))
        return delta is not None and delta <= n
    if ".." in f:
        left, right = f.split("..", 1)
        d1 = _parse_date_safe(left)
        d2 = _parse_date_safe(right)
        if d_obj is None:
            return False
        if d1 and d_obj < d1:
            return False
        if d2 and d_obj > d2:
            return False
        return True
    exact = _parse_date_safe(f)
    if exact is not None:
        return d_obj == exact
    return True


def _shorten(text: str, max_len: int) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= max_len:
        return t
    return t[: max(1, max_len - 1)] + "…"


def build_tasks_digest(
    conn: sqlite3.Connection,
    preset: str = "all",
    ids: Any = None,
    category: str = "",
    due: str = "",
    status: str = "active",
    priority: str = "",
    query: str = "",
    template: str = "mobile",
    limit: int = 15,
    max_chars: int = 1500,
    include_done_today: bool = False,
) -> Dict[str, Any]:
    """
    构建手机端低成本待办摘要（纯只读、零扫盘）。
    返回: {ok, template, total_matched, shown_count, omitted_count, etag, text, items}
    """
    today = date.today()
    today_str = today.strftime("%Y-%m-%d")
    mmdd_today = today.strftime("%m-%d")

    tpl = (template or "mobile").strip().lower()
    if tpl not in VALID_TEMPLATES:
        tpl = "mobile"

    id_list = _parse_ids(ids)
    preset_norm = (preset or "all").strip().lower()
    if preset_norm not in VALID_PRESETS:
        preset_norm = "all"

    status_norm = (status or "active").strip().lower()
    if status_norm not in VALID_STATUSES:
        status_norm = "active"

    # 若显式指定了 IDs，则默认查全状态，且不套用 preset 过滤
    sql = (
        "SELECT id, title, source_channel, doc_number, issuing_org, priority, deadline, "
        "category, summary, checklist_json, progress_json, status, is_pinned, updated_at, completed_at "
        "FROM tasks WHERE 1=1"
    )
    params: List[Any] = []
    if id_list:
        ph = ",".join("?" for _ in id_list)
        sql += f" AND id IN ({ph})"
        params.extend(id_list)
    else:
        if status_norm == "active":
            sql += " AND status != 'archived'"
        elif status_norm in ("in_progress", "pending", "archived"):
            sql += " AND status = ?"
            params.append(status_norm)

    rows = conn.execute(sql, params).fetchall()

    # 单条聚合 SQL 统计附件数与产出材料文件名（零扫盘）
    task_ids = [r["id"] for r in rows]
    doc_stats: Dict[int, Dict[str, Any]] = {}
    if task_ids:
        ph = ",".join("?" for _ in task_ids)
        drows = conn.execute(
            f"""
            SELECT task_id,
                   SUM(CASE WHEN doc_role = 'incoming' THEN 1 ELSE 0 END) AS in_cnt,
                   SUM(CASE WHEN doc_role != 'incoming' THEN 1 ELSE 0 END) AS out_cnt,
                   GROUP_CONCAT(CASE WHEN doc_role != 'incoming' THEN filename END, '、') AS draft_names
            FROM documents
            WHERE task_id IN ({ph})
            GROUP BY task_id
            """,
            task_ids,
        ).fetchall()
        for dr in drows:
            doc_stats[dr["task_id"]] = {
                "in_cnt": int(dr["in_cnt"] or 0),
                "out_cnt": int(dr["out_cnt"] or 0),
                "draft_names": dr["draft_names"] or "",
            }

    prio_set = {p.strip() for p in re.split(r"[,，、\s]+", priority or "") if p.strip()}
    cat_q = (category or "").strip().lower()
    kw_q = (query or "").strip().lower()

    enriched: List[Dict[str, Any]] = []
    overdue_cnt = 0
    urgent_cnt = 0
    due3_cnt = 0

    for r in rows:
        delta, due_label = _deadline_info(r["deadline"], today)
        d_obj = _parse_date_safe(r["deadline"])
        checklist = _safe_json_list(r["checklist_json"])
        progress = _safe_json_list(r["progress_json"])

        done_cnt = sum(1 for c in checklist if isinstance(c, dict) and c.get("done"))
        total_chk = len(checklist)
        pending_chks = [
            c for c in checklist if isinstance(c, dict) and not c.get("done") and str(c.get("text") or "").strip()
        ]
        latest_prog = ""
        if progress:
            last = progress[-1]
            if isinstance(last, dict):
                t_s = str(last.get("time") or "")
                t_short = t_s[5:16] if len(t_s) >= 16 else t_s
                latest_prog = f"[{t_short}] {str(last.get('text') or '').strip()}".strip()
            else:
                latest_prog = str(last).strip()

        is_overdue = delta is not None and delta < 0
        is_urgent = (r["priority"] == "紧急") or is_overdue or (delta is not None and delta <= 3)

        if not id_list:
            if cat_q and cat_q not in (r["category"] or "").lower():
                continue
            if prio_set and (r["priority"] or "") not in prio_set:
                continue
            if kw_q:
                hay = f"{r['title']} {r['summary']} {r['issuing_org']} {r['doc_number']}".lower()
                if kw_q not in hay:
                    continue
            if due and not _match_due(delta, d_obj, due, today):
                continue
            if preset_norm == "urgent" and not (is_urgent or r["is_pinned"]):
                continue
            if preset_norm == "today" and not (delta is not None and delta <= 0):
                continue
            if preset_norm == "week" and not (delta is not None and delta <= 7):
                continue

        if is_overdue:
            overdue_cnt += 1
        if r["priority"] == "紧急":
            urgent_cnt += 1
        if delta is not None and 0 <= delta <= 3:
            due3_cnt += 1

        ds = doc_stats.get(r["id"], {"in_cnt": 0, "out_cnt": 0, "draft_names": ""})
        enriched.append(
            {
                "id": r["id"],
                "title": r["title"],
                "source_channel": r["source_channel"] or "",
                "doc_number": r["doc_number"] or "",
                "issuing_org": r["issuing_org"] or "",
                "priority": r["priority"] or "普通",
                "deadline": r["deadline"] or "",
                "delta_days": delta,
                "due_label": due_label,
                "is_overdue": is_overdue,
                "is_urgent": is_urgent,
                "category": r["category"] or "",
                "summary": (r["summary"] or "").strip(),
                "status": r["status"] or "in_progress",
                "is_pinned": bool(r["is_pinned"]),
                "chk_done": done_cnt,
                "chk_total": total_chk,
                "checklist": checklist,
                "pending_chks": pending_chks,
                "progress": progress,
                "latest_prog": latest_prog,
                "in_cnt": ds["in_cnt"],
                "out_cnt": ds["out_cnt"],
                "draft_names": ds["draft_names"],
                "updated_at": r["updated_at"] or "",
            }
        )

    # 排序：已逾期最前 → 紧急优先 → 剩余天数近优先 → 置顶 → ID 降序
    enriched.sort(
        key=lambda x: (
            0 if x["is_overdue"] else 1,
            PRIORITY_RANK.get(x["priority"], 2),
            x["delta_days"] if x["delta_days"] is not None else 9999,
            0 if x["is_pinned"] else 1,
            -x["id"],
        )
    )

    total_matched = len(enriched)
    limit_n = max(1, min(100, int(limit or 15)))
    max_chars_n = max(300, min(8000, int(max_chars or 1500)))
    sliced = enriched[:limit_n]

    # 可选：今日刚办结事项
    done_today_rows: List[sqlite3.Row] = []
    if include_done_today and not id_list:
        done_today_rows = conn.execute(
            "SELECT id, title, completed_at FROM tasks WHERE status = 'archived' AND completed_at LIKE ? ORDER BY id DESC LIMIT 10",
            (f"{today_str}%",),
        ).fetchall()

    # 渲染对应模板
    text, shown_count = _render_digest_template(
        tpl=tpl,
        items=sliced,
        total_matched=total_matched,
        mmdd_today=mmdd_today,
        today_str=today_str,
        overdue_cnt=overdue_cnt,
        urgent_cnt=urgent_cnt,
        due3_cnt=due3_cnt,
        category=category,
        status_norm=status_norm,
        max_chars=max_chars_n,
        done_today_rows=done_today_rows,
    )

    omitted_count = max(0, total_matched - shown_count)
    compact_items = [
        {
            "id": x["id"],
            "title": x["title"],
            "priority": x["priority"],
            "deadline": x["deadline"],
            "due_label": x["due_label"],
            "category": x["category"],
            "status": x["status"],
            "chk": f"{x['chk_done']}/{x['chk_total']}",
            "latest_prog": _shorten(x["latest_prog"], 60),
            "out_cnt": x["out_cnt"],
        }
        for x in sliced[:shown_count]
    ]

    return {
        "ok": True,
        "template": tpl,
        "total_matched": total_matched,
        "shown_count": shown_count,
        "omitted_count": omitted_count,
        "etag": compute_digest_etag(conn),
        "text": text,
        "items": compact_items,
    }


def _render_digest_template(
    tpl: str,
    items: List[Dict[str, Any]],
    total_matched: int,
    mmdd_today: str,
    today_str: str,
    overdue_cnt: int,
    urgent_cnt: int,
    due3_cnt: int,
    category: str,
    status_norm: str,
    max_chars: int,
    done_today_rows: List[sqlite3.Row],
) -> Tuple[str, int]:
    if not items and not done_today_rows:
        return f"📋 待办摘要（{mmdd_today}）：当前筛选条件下暂无匹配事项。", 0

    lines: List[str] = []
    shown = 0

    if tpl == "mobile":
        scope_note = f"｜{category}" if category else ""
        lines.append(
            f"📋 待办速览（{mmdd_today}{scope_note}｜共{total_matched}项：逾期{overdue_cnt} 紧急{urgent_cnt} 3日内{due3_cnt}）"
        )
        urgent_group = [x for x in items if x["is_urgent"]]
        normal_group = [x for x in items if not x["is_urgent"]]

        def append_group(header: str, group: List[Dict[str, Any]], start_idx: int) -> int:
            nonlocal shown
            if not group:
                return start_idx
            group_lines = ["", header]
            idx = start_idx
            for x in group:
                next_step = ""
                if x["pending_chks"]:
                    next_step = _shorten(str(x["pending_chks"][0].get("text") or ""), 30)
                elif x["latest_prog"]:
                    next_step = _shorten(x["latest_prog"], 30)
                elif x["summary"]:
                    next_step = _shorten(x["summary"], 30)
                chk_tag = f"未完{x['chk_total'] - x['chk_done']}/{x['chk_total']}" if x["chk_total"] else ""
                block = f"{idx}. #{x['id']} {_shorten(x['title'], 24)} [{x['due_label']}｜{x['priority']}]"
                sub_parts = []
                if next_step:
                    sub_parts.append(f"下步：{next_step}")
                if chk_tag:
                    sub_parts.append(chk_tag)
                if sub_parts:
                    block += "\n   " + "（" + "｜".join(sub_parts) + "）"
                projected = len("\n".join(lines + group_lines + [block]))
                if projected > max_chars - 60 and shown > 0:
                    break
                group_lines.append(block)
                shown += 1
                idx += 1
            if len(group_lines) > 2:
                lines.extend(group_lines)
            return idx

        next_i = append_group("🔴 必盯（逾期/紧急/3日内）", urgent_group, 1)
        append_group("🟡 近期推进", normal_group, next_i)

    elif tpl == "checklist":
        lines.append(f"✅ 待办清单（{mmdd_today}｜共{total_matched}项｜状态：{status_norm}）")
        for x in items:
            pend = "；".join(_shorten(str(c.get("text") or ""), 22) for c in x["pending_chks"][:2])
            tail = f" → 待办：{pend}" if pend else (f" → 进展：{_shorten(x['latest_prog'], 26)}" if x["latest_prog"] else "")
            line = f"- [#{x['id']}][{x['priority']}][{x['due_label']}] {_shorten(x['title'], 24)}（{x['chk_done']}/{x['chk_total']}）{tail}"
            if len("\n".join(lines + [line])) > max_chars - 50 and shown > 0:
                break
            lines.append(line)
            shown += 1

    elif tpl == "report":
        scope_note = f"｜范围：{category}" if category else ""
        lines.append(f"📊 工作进展汇报（截至 {today_str}{scope_note}｜共{total_matched}项）")
        cn_nums = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
        for i, x in enumerate(items):
            num_s = cn_nums[i] if i < len(cn_nums) else str(i + 1)
            block_lines = [
                "",
                f"{num_s}、#{x['id']} {x['title']}（{x['priority']}｜{x['due_label']}）",
            ]
            if x["summary"]:
                block_lines.append(f"- 要求：{_shorten(x['summary'], 70)}")
            if x["latest_prog"]:
                block_lines.append(f"- 进展：{_shorten(x['latest_prog'], 70)}")
            if x["draft_names"]:
                block_lines.append(f"- 材料：{_shorten(x['draft_names'], 60)}")
            if x["pending_chks"]:
                pend = " → ".join(_shorten(str(c.get("text") or ""), 24) for c in x["pending_chks"][:3])
                block_lines.append(f"- 下步：{pend}")
            block = "\n".join(block_lines)
            if len("\n".join(lines + [block])) > max_chars - 60 and shown > 0:
                break
            lines.append(block)
            shown += 1

    else:  # detail
        for x in items:
            block_lines = [
                f"📌 #{x['id']} {x['title']} [{x['status']}｜{x['priority']}｜{x['category']}｜{x['due_label']}]"
            ]
            meta_parts = []
            if x["issuing_org"]:
                meta_parts.append(f"来文：{x['issuing_org']}")
            if x["doc_number"]:
                meta_parts.append(f"文号：{x['doc_number']}")
            if meta_parts:
                block_lines.append("｜".join(meta_parts))
            if x["summary"]:
                block_lines.append(f"核心要求：{x['summary']}")
            if x["checklist"]:
                block_lines.append(f"待办细项（{x['chk_done']}/{x['chk_total']}）：")
                for c in x["checklist"][:10]:
                    if not isinstance(c, dict):
                        continue
                    mark = "x" if c.get("done") else " "
                    cid = c.get("id") or ""
                    block_lines.append(f"  [{mark}] {cid} {str(c.get('text') or '').strip()}".strip())
            if x["progress"]:
                block_lines.append("近期进展（近3条）：")
                for p in x["progress"][-3:]:
                    if isinstance(p, dict):
                        t_s = str(p.get("time") or "")
                        t_short = t_s[5:16] if len(t_s) >= 16 else t_s
                        block_lines.append(f"  - {t_short} {str(p.get('text') or '').strip()}")
                    else:
                        block_lines.append(f"  - {str(p).strip()}")
            block_lines.append(
                f"关联文件：来文 {x['in_cnt']} 份｜我写材料 {x['out_cnt']} 份"
                + (f"（{_shorten(x['draft_names'], 50)}）" if x["draft_names"] else "")
            )
            block = "\n".join(block_lines)
            if len("\n".join(lines + ["", block])) > max_chars and shown > 0:
                break
            if lines:
                lines.append("")
            lines.append(block)
            shown += 1

    if done_today_rows:
        lines.append("")
        lines.append(f"✅ 今日已办结（{len(done_today_rows)}项）：" + "；".join(f"#{r['id']} {_shorten(r['title'], 16)}" for r in done_today_rows[:5]))

    omitted = max(0, total_matched - shown)
    if tpl != "detail":
        footer = "💡 回复 #ID 看详情（如“看 #12”）"
        if omitted > 0:
            footer += f"；另有 {omitted} 项未展开"
        lines.append("")
        lines.append(footer + "。")

    return "\n".join(lines), shown
