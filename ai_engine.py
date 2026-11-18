#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与通知 AI 解析、两段式智能检索、文件角色智能判别与阶段总结引擎 (v1.2.0)
"""

import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple


def call_openai_compatible_api(
    config: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    timeout: int = 35,
) -> Optional[str]:
    """调用兼容 OpenAI Chat Completions 的接口，失败或未配置时返回 None 由本地规则引擎兜底"""
    api_base = (config.get("ai_api_base") or "").strip().rstrip("/")
    api_key = (config.get("ai_api_key") or "").strip()
    model = (config.get("ai_model") or "deepseek-chat").strip()

    if not api_base:
        return None
    if not api_key and ("localhost" not in api_base and "127.0.0.1" not in api_base and "192.168." not in api_base):
        return None

    url = api_base if api_base.endswith("/chat/completions") else f"{api_base}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return body["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _extract_json_from_llm(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw_text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", cleaned)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def infer_doc_role(filename: str, text_preview: str = "", in_task_folder: bool = False, cat_name: str = "") -> str:
    """
    智能判别本地新增或扫描文件的性质角色 ('incoming' | 'draft' | 'template' | 'historical')
    修复 v1.0 中“凡是不带[来文]前缀一律当成我写的材料”的死板逻辑
    """
    fn = (filename or "").strip()
    combined = f"{fn} {(text_preview or '')[:300]}"

    if fn.startswith(("[来文]", "【来文】", "来文_")):
        return "incoming"
    if fn.startswith(("[材料]", "【材料】", "材料_")):
        return "draft"
    if any(k in fn for k in ("范文", "模板", "参考样例", "通用格式", "[模板]", "【范文")):
        return "template"
    if "历史" in cat_name and not in_task_folder:
        return "historical"

    # 自撰材料强特征词
    draft_Feature = (
        "我县", "我局", "我科", "我办", "本单位", "初稿", "定稿", "修改稿",
        "汇报", "总结", "小结", "自查报告", "整改报告", "落实情况", "发言", "讲话稿", "心得",
    )
    # 上级来文强特征词
    incoming_feature = (
        "关于开展", "关于做好", "关于印发", "关于报送", "关于转发", "的通知", "的方案",
        "的函", "的通报", "上级", "来文", "明电",
    )
    if any(k in fn for k in draft_Feature):
        return "draft"
    if fn.lower().endswith(".ofd") or re.search(r"[〔\[\(（]20\d{2}[〕\]\)）]\s*\d+\s*号", combined):
        if not any(k in fn for k in ("初稿", "定稿", "我县", "我局", "汇报", "总结")):
            return "incoming"
    if any(k in fn for k in incoming_feature):
        return "incoming"

    return "draft" if in_task_folder else ("historical" if "历史" in cat_name else "draft")


def match_best_category(text_and_title: str, categories: Optional[List[str]] = None) -> str:
    """根据主题词智能推荐物理分类目录"""
    avail_cats = categories or [
        "01_防灾减灾",
        "02_隐患排查与安全生产",
        "03_综合汇报与总结",
        "04_上级日常通知",
        "05_历史公文归档",
    ]
    if not avail_cats:
        return "01_防灾减灾"

    # 1. 先检查分类目录名自身的核心词是否直接出现在文本中
    for c in avail_cats:
        clean_c = re.sub(r"^\d+[_\-\s]*", "", c)
        for seg in re.split(r"[与和及_\-\s/]+", clean_c):
            if len(seg) >= 2 and seg in text_and_title:
                return c

    # 2. 行业领域关键词库匹配
    if any(k in text_and_title for k in ("防灾", "减灾", "救灾", "防汛", "抗旱", "森林防火", "森林防灭火", "森防", "地震", "地质灾害", "低温雨雪", "冰冻", "台风", "自然灾害", "冬春")):
        for c in avail_cats:
            if "防灾" in c or "减灾" in c:
                return c
    if any(k in text_and_title for k in ("隐患", "安全生产", "重大事故", "危化", "工贸", "非煤矿山", "消防", "火灾", "排查", "整治", "督导", "执法", "安委")):
        for c in avail_cats:
            if "隐患" in c or "安全" in c:
                return c
    if any(k in text_and_title for k in ("总结", "汇报", "述职", "讲话", "工作要点", "计划", "季报", "月报", "年终", "范文", "模板")):
        for c in avail_cats:
            if "汇报" in c or "总结" in c:
                return c
    for c in avail_cats:
        if "日常" in c or "通知" in c:
            return c
    return avail_cats[0]


def _rule_based_parse_document(
    text: str,
    filename: str = "",
    source_hint: str = "",
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    内置政务公文与碎片通知高精度规则解析引擎（v1.2.0 增强版）
    针对微信群/电话碎片通知自动提炼干净的核心动作标题，杜绝把“【工作群通知】@所有人...”整段截断当标题
    """
    now = datetime.now()
    clean_text = (text or "").strip()
    lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]

    # 1. 提取发文字号
    doc_number = ""
    doc_num_match = re.search(
        r"([\u4e00-\u9fa5]{2,12}(?:发|字|函|办|办发|明电|通|号)[〔\[\(（]\d{4}[〕\]\)）]\s*\d+\s*号)",
        clean_text,
    )
    if doc_num_match:
        doc_number = doc_num_match.group(1).replace(" ", "")

    # 2. 判断来源渠道
    source_channel = source_hint
    if not source_channel:
        if any(k in clean_text[:120] for k in ("微信", "@所有人", "群通知", "接群通知", "工作群")):
            source_channel = "微信群"
        elif any(k in clean_text[:120] for k in ("来电", "电话通知", "口头通知", "电话要求")):
            source_channel = "电话通知"
        elif doc_number or filename.lower().endswith((".ofd", ".docx", ".pdf", ".doc", ".wps")):
            source_channel = "上级来文"
        else:
            source_channel = "上级来文" if len(clean_text) > 180 else "微信群"

    # 3. 提取事项标题（大幅优化微信群/电话通知标题提炼）
    title = ""
    if filename:
        stem = re.sub(r"\.[a-zA-Z0-9]+$", "", filename)
        stem = re.sub(r"^(\[[^\]]+\]|【[^】]+】|\d{4}-\d{2}-\d{2}_?)+", "", stem).strip()
        if len(stem) >= 4:
            title = stem

    if not title:
        # 优先匹配正式公文标题“关于……的通知/方案/函/请示/报告”
        about_match = re.search(
            r"(关于[\u4e00-\u9fa50-9A-Za-z《》“”‘’、，—\-]{4,42}?(?:的通知|的方案|的函|的请示|的报告|的工作要求|的提示|的通报))",
            clean_text,
        )
        if about_match:
            title = about_match.group(1)

    if not title:
        # 针对微信群/电话通知：匹配“报送/提交/填报/开展《……》……”或书名号核心材料
        action_book_match = re.search(
            r"((?:报送|提交|填报|上报|开展|组织|起草|汇总)[\u4e00-\u9fa50-9A-Za-z]*?《[^》]{3,30}》[\u4e00-\u9fa50-9A-Za-z及与和]{0,12})",
            clean_text,
        )
        if action_book_match:
            title = action_book_match.group(1).strip("，。；： ")
        else:
            book_only = re.search(r"《([^》]{4,32})》", clean_text)
            if book_only:
                title = f"办理《{book_only.group(1)}》相关工作"

    if not title:
        for ln in lines[:6]:
            if ln == doc_number:
                continue
            # 剥离微信群前缀噪音
            stripped = re.sub(
                r"^(\[[^\]]+\]|【[^】]+】|@所有人|@全体成员|同志们|各镇街|各科室|各有关单位|接[^：:]{2,15}[：:]|请各单位|请各科室|[\s，。：:])+",
                "",
                ln,
            ).strip()
            if len(stripped) >= 5:
                title = stripped[:38]
                break

    if not title:
        title = "未命名工作事项_" + now.strftime("%m%d")

    # 4. 提取来文单位
    issuing_org = ""
    org_patterns = [
        r"(?:来文单位|发文机关|发文单位|签发单位)[：:]\s*([\u4e00-\u9fa5]{3,20})",
        r"接([\u4e00-\u9fa5]{2,14}(?:政府办|安委办|应急局|应急管理局|森防办|防指|减灾委|市委办|县委办|办公室|局|委))(?:电话|通知|群通知|要求)",
        r"([\u4e00-\u9fa5]{2,14}(?:应急管理局|委员会办公室|人民政府办公室|安委会办公室|减灾委员会|森防指办公室|防汛抗旱指挥部|办公室|厅|局|委|部))(?=\s*关于|\n)",
    ]
    for pat in org_patterns:
        m = re.search(pat, clean_text)
        if m:
            issuing_org = m.group(1).strip()
            break
    if not issuing_org and lines:
        for ln in reversed(lines[-6:]):
            if re.search(r"(局|委|办公室|指挥部|委员会|中心|政府)$", ln) and 4 <= len(ln) <= 22:
                issuing_org = ln
                break

    # 5. 提取截止日期
    deadline = ""
    date_ctx_patterns = [
        r"(?:于|在|截止|截至)?\s*(\d{4})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*日?(?:\s*\d{1,2}[时点:：]\d{0,2})?\s*(?:前|之前|下班前|内|报送|提交)",
        r"(?:请于|务必于|于|在|截止)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*\d{1,2}[时点:：]\d{0,2})?\s*(?:前|之前|下班前|内|报送|提交|完成)",
        r"(\d{4})\s*[年\-]\s*(\d{1,2})\s*[月\-]\s*(\d{1,2})\s*日",
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*日",
    ]
    for pat in date_ctx_patterns:
        m = re.search(pat, clean_text)
        if m:
            groups = m.groups()
            try:
                if len(groups) == 3:
                    y, mo, d = int(groups[0]), int(groups[1]), int(groups[2])
                else:
                    y, mo, d = now.year, int(groups[0]), int(groups[1])
                dt = datetime(y, mo, d)
                deadline = dt.strftime("%Y-%m-%d")
                break
            except Exception:
                continue

    if not deadline:
        if any(k in clean_text for k in ("今日下班前", "今天下班前", "今日内", "今天内", "立即报送")):
            deadline = now.strftime("%Y-%m-%d")
        elif any(k in clean_text for k in ("明日", "明天")):
            deadline = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        elif "后天" in clean_text:
            deadline = (now + timedelta(days=2)).strftime("%Y-%m-%d")

    # 6. 判断优先级
    priority = "普通"
    if any(k in clean_text for k in ("特急", "加急", "立即", "务必于今日", "今日下班前", "马上", "主要领导批示", "迎检", "督查", "紧急")):
        priority = "紧急"
    elif any(k in clean_text for k in ("重要", "高度重视", "专项行动", "实施方案", "汇报", "总结")) or deadline:
        priority = "重要"

    # 7. 提取可勾选的具体待办子清单
    checklist_items: List[str] = []
    action_keywords = (
        "报送", "上报", "提交", "汇总", "填写", "填报", "起草", "撰写", "拟定", "制定",
        "排查", "自查", "整改", "摸底", "统计", "台账", "名单", "方案", "总结", "报告", "材料", "小结",
    )
    raw_sentences = re.split(r"[\n；;。！!]+", clean_text)
    for sent in raw_sentences:
        s = sent.strip()
        if len(s) < 6 or len(s) > 130:
            continue
        s_clean = re.sub(
            r"^([一二三四五六七八九十]+[、\.]|\([一二三四五六七八九十0-9]+\)|（[一二三四五六七八九十0-9]+）|\d+[、\.\)]|[①②③④⑤⑥⑦⑧]|【[^】]+】|@所有人\s*)\s*",
            "",
            s,
        ).strip()
        if any(kw in s_clean for kw in action_keywords):
            if not any(fluff in s_clean for fluff in ("重要意义", "指导思想", "以习近平", "深刻汲取")):
                if s_clean not in checklist_items:
                    checklist_items.append(s_clean)
        if len(checklist_items) >= 5:
            break

    if not checklist_items:
        # 兜底也不写“梳理办理要点”这类空话，直接给可执行动作
        submit_target = f"报{issuing_org}" if issuing_org else "报送上级"
        when = f"{deadline}前" if deadline else "按来文时限"
        checklist_items = [
            f"核对报送要求：材料名称、份数、报送方式与接收人（{submit_target}）",
            f"{when}完成材料起草、审签盖章并{submit_target}",
        ]

    # 8. 生成核心工作要求摘要
    summary_parts = []
    if issuing_org:
        summary_parts.append(f"来文单位：{issuing_org}")
    if deadline:
        summary_parts.append(f"截止期限：{deadline}")
    summary_parts.append("核心要求：" + "；".join(checklist_items[:3]))
    summary = " | ".join(summary_parts)

    # 9. 智能匹配推荐业务分类目录
    selected_cat = match_best_category(clean_text + " " + title, categories)

    return {
        "title": title,
        "source_channel": source_channel,
        "doc_number": doc_number,
        "issuing_org": issuing_org,
        "priority": priority,
        "deadline": deadline,
        "category": selected_cat,
        "summary": summary,
        "checklist": [
            {"id": f"chk_{i+1}", "text": item, "done": False}
            for i, item in enumerate(checklist_items)
        ],
        "engine": "rule_engine",
    }


def parse_incoming_content(
    text: str,
    filename: str = "",
    source_hint: str = "",
    categories: Optional[List[str]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    cfg = config or {}
    cats = categories or [
        "01_防灾减灾",
        "02_隐患排查与安全生产",
        "03_综合汇报与总结",
        "04_上级日常通知",
        "05_历史公文归档",
    ]
    rule_res = _rule_based_parse_document(text, filename, source_hint, cats)

    if not cfg.get("ai_api_base"):
        return rule_res

    today_str = datetime.now().strftime("%Y-%m-%d")
    sys_prompt = (
        "你是一个专业的政务公文与工作通知分析助手。请从用户提供的公文或微信/电话通知中提取工作台字段，"
        f"当前参考日期为 {today_str}。必须严格输出 JSON 对象，包含以下字段：\n"
        "- title: 简明事项名称（不超过35字，去掉@所有人等前缀）\n"
        "- source_channel: '上级来文' | '微信群' | '电话通知' | '自建'\n"
        "- doc_number: 发文字号（如无则为空字符串）\n"
        "- issuing_org: 来文或通知单位（如无则为空字符串）\n"
        "- priority: '普通' | '重要' | '紧急'\n"
        "- deadline: 截止日期，格式 YYYY-MM-DD（如无明确截止期则为空字符串）\n"
        f"- category: 从 {json.dumps(cats, ensure_ascii=False)} 中选择最匹配的一项\n"
        "- summary: 1-2句话说清核心要求：谁要什么、交几件、报给谁、什么时间、以什么形式（盖章PDF/电子版），不要罗列文件清单\n"
        "- checklist: 字符串数组，2~5 条可直接照做的待办动作。每条只写一个动作（核对/起草/填报/审签/盖章/报送/电话确认），必须带时间节点或报送对象。"
        "严禁出现“梳理办理要点”“研读文件”“高度重视”“研究部署”这类空泛动作，也不要把办事过程另存成文件"
    )
    user_prompt = f"文件名：{filename}\n来源提示：{source_hint}\n正文内容：\n{text[:6000]}"

    llm_raw = call_openai_compatible_api(cfg, sys_prompt, user_prompt)
    parsed = _extract_json_from_llm(llm_raw) if llm_raw else None
    if not parsed or not isinstance(parsed, dict):
        return rule_res

    raw_list = parsed.get("checklist") or []
    chk_formatted = []
    for idx, item in enumerate(raw_list):
        if isinstance(item, str) and item.strip():
            chk_formatted.append({"id": f"chk_{idx+1}", "text": item.strip(), "done": False})
        elif isinstance(item, dict) and item.get("text"):
            chk_formatted.append({"id": f"chk_{idx+1}", "text": str(item["text"]).strip(), "done": bool(item.get("done", False))})

    return {
        "title": parsed.get("title") or rule_res["title"],
        "source_channel": parsed.get("source_channel") or rule_res["source_channel"],
        "doc_number": parsed.get("doc_number") if parsed.get("doc_number") is not None else rule_res["doc_number"],
        "issuing_org": parsed.get("issuing_org") if parsed.get("issuing_org") is not None else rule_res["issuing_org"],
        "priority": parsed.get("priority") if parsed.get("priority") in ("普通", "重要", "紧急") else rule_res["priority"],
        "deadline": parsed.get("deadline") if parsed.get("deadline") is not None else rule_res["deadline"],
        "category": parsed.get("category") if parsed.get("category") in cats else rule_res["category"],
        "summary": parsed.get("summary") or rule_res["summary"],
        "checklist": chk_formatted if chk_formatted else rule_res["checklist"],
        "engine": "llm_api",
    }


def extract_search_keywords(natural_query: str) -> List[str]:
    """从自然语言搜索问句中拆解核心实词，用于两段式检索的第一阶段召回"""
    split_delimiters = (
        r"或者|还是|以及|关于|有关|请问|帮我|找一下|找出|搜索|查询|哪些|什么|"
        r"我写过的|我写过|我写的|以前的|以前|历史的|历史|今年的|今年|去年的|去年|"
        r"有没有|在哪里|的|了|和|与|或|及|[，。？！、；：""''（）《》【】\s]+"
    )
    parts = [p.strip() for p in re.split(split_delimiters, natural_query.strip()) if p and p.strip()]
    generic_tail = {"材料", "公文", "通知", "文件", "要求", "工作", "事项"}
    tokens = []
    for p in parts:
        if len(p) >= 2 and p not in generic_tail and p not in tokens:
            tokens.append(p)
        if len(p) >= 4:
            for sub_kw in (
                "防灾减灾", "森林防灭火", "森林防火", "冬春", "自然灾害",
                "隐患排查", "安全生产", "救灾物资", "总结", "汇报", "方案", "台账", "调度",
            ):
                if sub_kw in p and sub_kw not in tokens:
                    tokens.append(sub_kw)
    if not tokens and natural_query.strip():
        tokens = [natural_query.strip()]
    return tokens[:8]


def synthesize_ai_search_answer(
    query: str,
    keywords: List[str],
    matched_tasks: List[Dict[str, Any]],
    matched_docs: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """两段式 AI 搜索的第二阶段：根据初筛召回的工作事项与公文/材料片段，生成结构化解答与推荐引用"""
    cfg = config or {}
    context_blocks = []
    for t in matched_tasks[:10]:
        context_blocks.append(
            f"[事项 #{t['id']}] 标题：{t['title']} | 文号：{t.get('doc_number') or '无'} | 状态：{t['status']} | "
            f"截止：{t.get('deadline') or '无'} | 要求摘要：{t.get('summary') or ''} | 分类：{t.get('category') or ''}"
        )
    for d in matched_docs[:15]:
        snippet = (d.get("snippet") or d.get("content_preview") or "")[:300].replace("\n", " ")
        context_blocks.append(
            f"[文件 #{d['id']}] 名称：{d['filename']} | 类型：{d['doc_role']} | 分类目录：{d['category']} | "
            f"关联事项ID：{d.get('task_id') or '独立归档'} | 内容摘录：{snippet}"
        )

    if cfg.get("ai_api_base") and (context_blocks or query):
        sys_prompt = (
            "你是公文工作台的 AI 检索助手。请基于提供的本地工作台事项和公文材料检索结果，"
            "直接、简明地回答用户的查询问题，按【相关工作事项】、【上级来文依据】、【我写过的参考材料】分点梳理，"
            "并标明对应的文件名或事项名称。不要编造库中不存在的文件。"
        )
        user_prompt = (
            f"用户检索问题：{query}\n"
            f"提取检索词：{', '.join(keywords)}\n"
            f"本地库召回结果（共 {len(matched_tasks)} 条事项，{len(matched_docs)} 份文件）：\n"
            + ("\n".join(context_blocks) if context_blocks else "未召回到直接匹配项。")
        )
        llm_ans = call_openai_compatible_api(cfg, sys_prompt, user_prompt, temperature=0.2)
        if llm_ans:
            return {
                "answer": llm_ans,
                "keywords": keywords,
                "engine": "llm_api",
            }

    if not matched_tasks and not matched_docs:
        ans = (
            f"未在本地库中找到与 **“{query}”**（拆解词：`{' / '.join(keywords)}`）直接匹配的公文或材料。"
            "建议尝试缩短关键词，或让本地 Agent 调用 `/api/agent/context-search` 进行宽泛排查。"
        )
    else:
        incoming_docs = [d for d in matched_docs if d.get("doc_role") in ("incoming", "historical")]
        my_drafts = [d for d in matched_docs if d.get("doc_role") in ("draft", "template")]
        lines = [
            f"已围绕核心词 **`{'`、`'.join(keywords)}`** 完成全库扫描，共为您定位到 **{len(matched_tasks)} 项工作事项**、**{len(incoming_docs)} 份上级/历史来文** 以及 **{len(my_drafts)} 份您撰写的材料/范文**："
        ]
        if matched_tasks:
            lines.append("\n**1. 关联工作事项：**")
            for t in matched_tasks[:5]:
                status_label = {"pending": "待办", "in_progress": "进行中", "archived": "已完成归档"}.get(t["status"], t["status"])
                lines.append(f"- **《{t['title']}》**（{status_label}，分类：`{t.get('category', '')}`）—— {t.get('summary', '')[:90]}")
        if my_drafts:
            lines.append("\n**2. 您写过的相关材料 / 范文（可直接预览/复制段落或下载）：**")
            for d in my_drafts[:5]:
                lines.append(f"- ✍️ **{d['filename']}**（目录：`{d.get('category', '')}`）—— 摘录：{(d.get('snippet') or '')[:80]}")
        if incoming_docs:
            lines.append("\n**3. 相关上级来文 / 历史归档公文：**")
            for d in incoming_docs[:5]:
                lines.append(f"- 📄 **{d['filename']}**（目录：`{d.get('category', '')}`）—— 摘录：{(d.get('snippet') or '')[:80]}")
        ans = "\n".join(lines)

    return {
        "answer": ans,
        "keywords": keywords,
        "engine": "smart_local",
    }
