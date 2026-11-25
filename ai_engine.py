#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与通知 AI 解析、四维知识检索（待办+公文+资料存档+重要事项记录）、文件角色智能判别与阶段总结引擎 (ai_engine.py v3.1.0)
"""

import ipaddress
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gw_workbench.ai_engine")

# 政务公文高频虚词/停用词（用于旧稿精准关联与关键词清洗）
GOV_STOPWORDS = frozenset({
    "关于", "开展", "召开", "做好", "加强", "进一步", "切实", "认真", "深入", "全面",
    "扎实", "持续", "统筹", "组织", "推进", "落实", "贯彻", "执行", "印发", "转发",
    "报送", "上报", "提交", "通知", "紧急通知", "工作", "有关", "相关", "事项", "情况",
    "要求", "精神", "意见", "方案", "计划", "总结", "汇报", "报告", "材料", "公文",
    "文件", "记录", "附件", "内容", "如下", "现将", "请各", "各单位", "各部门", "各镇街",
    "各科室", "有关单位", "来文单位", "截止期限", "核心要求", "应急管理局", "办公室",
    "人民政府", "委员会", "指挥部", "全县", "全市", "全省", "我县", "我市", "我局",
    "年度", "今年", "去年", "近期", "当前", "阶段", "期间", "下一步", "工作要求",
})

CHECKLIST_FLUFF_RE = re.compile(
    r"^(?:核对报送要求|研读文件|学习领会|高度重视|加强领导|提高站位|研究部署|梳理办理要点|"
    r"统筹安排|周密部署|强化责任|压实责任|认真贯彻|深入学习|深刻汲取|以习近平|指导思想|重要意义)"
)


def _is_private_or_local_url(api_base: str) -> bool:
    """判断 API 地址是否属于本机或 RFC1918/内网地址（支持 10/8、172.16/12、192.168/16、localhost）"""
    try:
        parsed = urllib.parse.urlparse(api_base if "://" in api_base else f"http://{api_base}")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False
        if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local") or host.endswith(".lan"):
            return True
        ip_obj = ipaddress.ip_address(host)
        return bool(ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local)
    except ValueError:
        return False
    except Exception:
        return False


def _normalize_chat_completions_url(api_base: str) -> str:
    """规范化 OpenAI 兼容 Chat Completions URL，兼容 Ollama 裸端口与带查询参数端点"""
    base = (api_base or "").strip()
    if not base:
        return ""
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return base
    if path == "" or path == "/":
        # 如 Ollama 默认 http://172.19.51.69:11434 -> 自动补 /v1/chat/completions
        new_path = "/v1/chat/completions"
    elif path.endswith("/v1") or path.endswith("/v1beta") or "/openai" in path:
        new_path = f"{path}/chat/completions"
    else:
        new_path = f"{path}/chat/completions"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment))


def call_openai_compatible_api(
    config: Dict[str, Any],
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.2,
    timeout: int = 35,
    max_tokens: int = 1200,
    retries: int = 1,
) -> Optional[str]:
    """调用兼容 OpenAI Chat Completions 的接口，支持内网全私有网段与瞬时错误重试"""
    api_base = (config.get("ai_api_base") or "").strip().rstrip("/")
    api_key = (config.get("ai_api_key") or "").strip()
    model = (config.get("ai_model") or "deepseek-chat").strip()

    if not api_base:
        return None
    if not api_key and not _is_private_or_local_url(api_base):
        logger.warning("AI API skipped: missing api_key for non-private endpoint %s", api_base)
        return None

    url = _normalize_chat_completions_url(api_base)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: Dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    model_lower = model.lower()
    is_reasoning_model = any(tag in model_lower for tag in ("o1-", "o3-", "o4-", "deepseek-r1", "qwq"))
    if not is_reasoning_model:
        payload["temperature"] = temperature
    if max_tokens > 0:
        payload["max_tokens"] = max_tokens

    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    attempts = max(1, retries + 1)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace")
                body = json.loads(raw_resp)
                content = body["choices"][0]["message"].get("content") or ""
                return content.strip()
        except urllib.error.HTTPError as e:
            status = getattr(e, "code", 0)
            logger.warning("AI API HTTP %s (attempt %d/%d) on %s", status, attempt + 1, attempts, url)
            if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                time.sleep(0.8 * (attempt + 1))
                continue
            return None
        except Exception as e:
            logger.warning("AI API error (attempt %d/%d) on %s: %s", attempt + 1, attempts, url, e)
            if attempt + 1 < attempts:
                time.sleep(0.6)
                continue
            return None
    return None


def _extract_json_from_llm(raw_text: str) -> Optional[Dict[str, Any]]:
    """健壮提取 LLM 返回的 JSON：兼容 DeepSeek-R1/QwQ <think> 块、代码块围栏、尾逗号与前后废话"""
    if not raw_text:
        return None
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.IGNORECASE).strip()
    if "<think>" in cleaned.lower():
        cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE).strip()

    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    def _try_parse(candidate: str) -> Optional[Dict[str, Any]]:
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except Exception:
            fixed = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                obj = json.loads(fixed)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None

    direct = _try_parse(cleaned)
    if direct is not None:
        return direct

    # 栈匹配提取最外层完整 JSON 对象，避免贪婪正则跨段污染
    start_idx = -1
    depth = 0
    in_str = False
    escaped = False
    for i, ch in enumerate(cleaned):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx != -1:
                    res = _try_parse(cleaned[start_idx : i + 1])
                    if res is not None:
                        return res
                    start_idx = -1
    return None


def infer_doc_role(filename: str, text_preview: str = "", in_task_folder: bool = False, cat_name: str = "") -> str:
    """
    智能判别本地新增或扫描文件的性质角色 ('incoming' | 'draft' | 'template' | 'historical' | 'archive' | 'record')
    保留对历史 [来文]/[材料] 前缀的只读兼容，但不依赖前缀即可准确判别。
    """
    fn = (filename or "").strip()
    preview = (text_preview or "")[:400]
    combined = f"{fn} {preview}"

    # 只读兼容历史遗留前缀
    if fn.startswith(("[来文]", "【来文】", "来文_")):
        return "incoming"
    if fn.startswith(("[材料]", "【材料】", "材料_")):
        return "draft"
    if any(k in fn for k in ("范文", "模板", "参考样例", "通用格式", "[模板]", "【范文")):
        return "template"
    if "资料存档" in cat_name:
        return "archive"
    if "重要事项" in cat_name:
        return "record"
    if "历史" in cat_name and not in_task_folder:
        return "historical"

    draft_feature = (
        "我县", "我市", "我局", "我科", "我办", "本单位", "初稿", "定稿", "修改稿", "国标排版",
        "汇报", "总结", "小结", "自查报告", "整改报告", "落实情况", "发言", "讲话稿", "心得", "反馈意见",
    )
    incoming_feature = (
        "关于开展", "关于做好", "关于印发", "关于报送", "关于转发", "关于召开", "的通知", "的方案",
        "的函", "的通报", "上级", "来文", "明电", "附件1", "附件2", "附件：", "微信通知截图",
    )
    if any(k in fn for k in draft_feature):
        return "draft"
    if fn.lower().endswith(".ofd") or re.search(r"[〔\[\(（]20\d{2}[〕\]\)）]\s*\d+\s*号", combined):
        if not any(k in fn for k in ("初稿", "定稿", "我县", "我局", "汇报", "总结", "国标排版")):
            return "incoming"
    if any(k in fn for k in incoming_feature):
        return "incoming"
    if any(k in preview for k in ("各镇街", "各区县", "各市应急管理局", "各有关单位", "现将有关事项通知如下")):
        return "incoming"

    return "draft" if in_task_folder else ("historical" if "历史" in cat_name else "draft")


def match_best_category(text_and_title: str, categories: Optional[List[str]] = None) -> str:
    """根据主题词智能推荐物理分类目录（同时适配新版 01~06 分类与旧版分类）"""
    avail_cats = categories or [
        "01_需反馈意见",
        "02_省及潍坊来文",
        "03_高密部门来文",
        "04_协助潍坊工作",
        "05_会议培训通知",
        "06_督查考核整改",
    ]
    if not avail_cats:
        return "02_省及潍坊来文"

    txt = text_and_title or ""

    # 1. 优先精确命中目录名关键词
    for c in avail_cats:
        clean_c = re.sub(r"^\d+[_\-\s]*", "", c)
        for seg in re.split(r"[与和及_\-\s/]+", clean_c):
            if len(seg) >= 2 and seg in txt:
                return c

    # 2. 适配新版默认 6 分类语义规则
    semantic_rules: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = [
        (("征求意见", "反馈意见", "修改意见", "书面意见", "无意见"), ("反馈", "意见")),
        (("会议", "培训", "视频会", "调度会", "座谈会", "参会", "会场", "讲座"), ("会议", "培训")),
        (("督查", "考核", "整改", "暗访", "通报", "约谈", "巡查", "回头看", "销号"), ("督查", "考核", "整改")),
        (("协助", "配合", "协办", "联合开展", "抽调"), ("协助",)),
        (("省应急", "山东省", "鲁应急", "潍坊市", "潍应急", "市应急局", "市安委办"), ("省及潍坊", "上级", "潍坊来文")),
        (("高密", "本级", "各镇街", "街道", "镇政府", "高密市"), ("高密部门", "本级")),
        # 兼容旧版分类体系
        (("防灾", "减灾", "救灾", "防汛", "汛情", "抗旱", "森林防火", "森林防灭火", "森防", "地震", "地质灾害", "低温雨雪", "冰冻", "台风", "自然灾害", "冬春"), ("防灾", "减灾")),
        (("隐患", "安全生产", "重大事故", "危化", "工贸", "非煤矿山", "消防", "火灾", "排查", "整治", "执法", "安委"), ("隐患", "安全")),
        (("总结", "汇报", "述职", "讲话", "工作要点", "计划", "季报", "月报", "年终", "范文", "模板"), ("汇报", "总结")),
    ]
    for keywords, cat_hints in semantic_rules:
        if any(k in txt for k in keywords):
            for c in avail_cats:
                if any(h in c for h in cat_hints):
                    return c

    for c in avail_cats:
        if "省及潍坊" in c or "日常" in c or "通知" in c:
            return c
    return avail_cats[0]


def _clean_checklist_items(raw_items: List[Any], max_items: int = 3, max_len: int = 36) -> List[Dict[str, Any]]:
    """统一清洗、去重、截断待办细项，剔除空泛套话与超长原文"""
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_items or []:
        if isinstance(item, dict):
            text = str(item.get("text") or "").strip()
            done = bool(item.get("done", False))
        elif isinstance(item, str):
            text = item.strip()
            done = False
        else:
            continue
        if not text or len(text) < 4:
            continue
        text = re.sub(r"\s+", " ", text)
        text = re.sub(
            r"^([一二三四五六七八九十]+[、\.]|\([一二三四五六七八九十0-9]+\)|（[一二三四五六七八九十0-9]+）|\d+[、\.\)]|[①②③④⑤⑥⑦⑧]|【[^】]+】|@所有人\s*)\s*",
            "",
            text,
        ).strip(" ，。；：")
        if not text or len(text) < 4 or CHECKLIST_FLUFF_RE.search(text):
            continue
        if len(text) > max_len:
            text = text[: max_len - 1].rstrip("，、；： ") + "…"
        norm_key = re.sub(r"[\s，。、；：！？]", "", text)
        if norm_key in seen:
            continue
        seen.add(norm_key)
        cleaned.append({"id": f"chk_{len(cleaned) + 1}", "text": text, "done": done})
        if len(cleaned) >= max_items:
            break
    return cleaned


def _detect_doc_kind(title: str, text: str) -> str:
    """粗粒度判定公文类型：meeting | feedback | fyi | action"""
    head = f"{title} {(text or '')[:400]}"
    if any(k in head for k in ("征求意见", "反馈意见", "修改意见")):
        return "feedback"
    if any(k in head for k in ("召开", "会议的通知", "培训的通知", "视频会", "调度会", "参会名单")):
        return "meeting"
    if any(k in head for k in ("阅知", "现印发给你们，请认真贯彻", "转发给你们，请结合实际")) and not any(
        k in (text or "") for k in ("报送", "上报", "提交", "填报", "反馈", "前报", "回执")
    ):
        return "fyi"
    return "action"


def _rule_based_parse_document(
    text: str,
    filename: str = "",
    source_hint: str = "",
    categories: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """内置政务公文与碎片通知精简规则解析引擎"""
    now = datetime.now()
    clean_text = (text or "").strip()
    lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]

    doc_number = ""
    doc_num_match = re.search(
        r"([\u4e00-\u9fa5]{2,12}(?:发|字|函|办|办发|明电|通|号)[〔\[\(（]\d{4}[〕\]\)）]\s*\d+\s*号)",
        clean_text,
    )
    if doc_num_match:
        doc_number = doc_num_match.group(1).replace(" ", "")

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

    title = ""
    if filename and not filename.startswith("微信通知截图_"):
        stem = re.sub(r"\.[a-zA-Z0-9]+$", "", filename)
        stem = re.sub(r"^(\[[^\]]+\]|【[^】]+】|\d{4}-\d{2}-\d{2}_?)+", "", stem).strip()
        if len(stem) >= 4:
            title = stem

    if not title:
        about_match = re.search(
            r"(关于[\u4e00-\u9fa50-9A-Za-z《》“”‘’、，—\-]{4,42}?(?:的通知|的方案|的函|的请示|的报告|的工作要求|的提示|的通报))",
            clean_text,
        )
        if about_match:
            title = about_match.group(1)

    if not title:
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
            if ln == doc_number or ln.startswith("【图片OCR") or ln.startswith("【影像图片"):
                continue
            stripped = re.sub(
                r"^(\[[^\]]+\]|【[^】]+】|@所有人|@全体成员|同志们|各镇街|各科室|各有关单位|接[^：:]{2,15}[：:]|请各单位|请各科室|[\s，。：:])+",
                "",
                ln,
            ).strip()
            if len(stripped) >= 5:
                title = stripped[:38]
                break

    if not title:
        if filename.startswith("微信通知截图_"):
            title = f"微信通知待办事项_{now.strftime('%m-%d_%H%M')}"
        else:
            title = "未命名工作事项_" + now.strftime("%m%d")

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

    deadline = ""
    date_ctx_patterns = [
        r"(?:请于|务必于|于|在|截止|截至)?\s*(\d{4})\s*[年\-/\.]\s*(\d{1,2})\s*[月\-/\.]\s*(\d{1,2})\s*日?(?:\s*\d{1,2}[时点:：]\d{0,2})?\s*(?:前|之前|下班前|内|报送|提交|反馈)",
        r"(?:请于|务必于|于|在|截止)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日(?:\s*\d{1,2}[时点:：]\d{0,2})?\s*(?:前|之前|下班前|内|报送|提交|反馈|完成)",
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

    priority = "普通"
    if any(k in clean_text for k in ("特急", "加急", "立即", "务必于今日", "今日下班前", "马上", "主要领导批示", "迎检", "督查", "紧急")):
        priority = "紧急"
    elif any(k in clean_text for k in ("重要", "专项行动", "实施方案", "汇报", "总结")) or deadline:
        priority = "重要"

    doc_kind = _detect_doc_kind(title, clean_text)

    # 精简规则提取：仅抓取含明确强指令动词的实质交付句子，排除标题行与 @所有人 原文，最多 3 条
    strong_verbs = ("报送", "上报", "提交", "反馈", "填报", "汇总", "参会", "参加", "报回执", "报名单", "核实", "整改")
    raw_sentences = re.split(r"[\n；;。！!]+", clean_text)
    scored_sents: List[Tuple[int, str]] = []
    for idx_s, sent in enumerate(raw_sentences):
        s = sent.strip()
        if len(s) < 6 or len(s) > 90:
            continue
        if s.startswith(("【公文标题", "【影像图片", "【图片OCR", "@所有人", "@全体成员")):
            continue
        if title and (s == title or s.endswith(title)):
            continue
        s_clean = re.sub(
            r"^([一二三四五六七八九十]+[、\.]|\([一二三四五六七八九十0-9]+\)|（[一二三四五六七八九十0-9]+）|\d+[、\.\)]|[①②③④⑤⑥⑦⑧]|【[^】]+】)\s*",
            "",
            s,
        ).strip()
        if not s_clean or CHECKLIST_FLUFF_RE.search(s_clean):
            continue
        score = 0
        if any(v in s_clean for v in strong_verbs):
            score += 3
        if re.search(r"(?:请于|务必于|于|前|下班前|回执|名单|盖章|电子版|PDF)", s_clean):
            score += 2
        if idx_s >= len(raw_sentences) // 2:
            score += 1  # 公文具体报送要求通常在后半段
        if score >= 3:
            scored_sents.append((score, s_clean))

    scored_sents.sort(key=lambda x: -x[0])
    checklist_formatted = _clean_checklist_items([s for _, s in scored_sents], max_items=3, max_len=36)

    # 仅在特定文种下提供 1 条极简提示；纯阅知件允许为空，不再硬塞机械流水账
    if not checklist_formatted:
        if doc_kind == "meeting":
            when_hint = f"{deadline}" if deadline else "按通知时间"
            checklist_formatted = _clean_checklist_items([f"{when_hint}落实参会人员并按时参会"], max_items=1, max_len=36)
        elif doc_kind == "feedback":
            when_hint = f"{deadline}前" if deadline else "按时限"
            checklist_formatted = _clean_checklist_items([f"{when_hint}书面反馈修改意见（无意见需零报告）"], max_items=1, max_len=36)

    # 精简核心要求摘要（不再照抄全部 checklist）
    summary_parts = []
    if issuing_org:
        summary_parts.append(f"来文：{issuing_org}")
    if deadline:
        summary_parts.append(f"截止：{deadline}")
    if checklist_formatted:
        summary_parts.append(checklist_formatted[0]["text"])
    elif lines:
        first_body = next((ln for ln in lines if ln != title and ln != doc_number and len(ln) >= 8), "")
        if first_body:
            summary_parts.append(first_body[:50])
    summary = " | ".join(summary_parts) if summary_parts else title[:60]

    selected_cat = match_best_category(clean_text + " " + title, categories)

    return {
        "title": title,
        "doc_kind": doc_kind,
        "source_channel": source_channel,
        "doc_number": doc_number,
        "issuing_org": issuing_org,
        "priority": priority,
        "deadline": deadline,
        "category": selected_cat,
        "summary": summary,
        "checklist": checklist_formatted,
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
        "01_需反馈意见",
        "02_省及潍坊来文",
        "03_高密部门来文",
        "04_协助潍坊工作",
        "05_会议培训通知",
        "06_督查考核整改",
    ]
    rule_res = _rule_based_parse_document(text, filename, source_hint, cats)

    if not cfg.get("ai_api_base"):
        return rule_res

    today_str = datetime.now().strftime("%Y-%m-%d")
    default_org = (cfg.get("default_org_name") or "本科室").strip()
    sys_prompt = (
        f"你是基层应急管理科室（{default_org}）的公文办件助手，当前日期 {today_str}。"
        "请以本单位经办人视角从公文或通知中提取工作台 JSON，严格输出 JSON 对象，包含字段：\n"
        "- doc_kind: 'action'(需办件) | 'meeting'(会议培训) | 'feedback'(征求意见) | 'fyi'(纯阅知)\n"
        "- title: 简明事项名称（≤30字，去文号与@所有人前缀）\n"
        "- source_channel: '上级来文' | '微信群' | '电话通知' | '自建'\n"
        "- doc_number: 发文字号（无则空字符串）\n"
        "- issuing_org: 来文或通知单位（无则空字符串）\n"
        "- priority: '普通' | '重要' | '紧急'\n"
        "- deadline: 截止日期 YYYY-MM-DD（无则空字符串）\n"
        f"- category: 从 {json.dumps(cats, ensure_ascii=False)} 中选择最匹配的一项\n"
        "- summary: 1句话（≤75字）说清谁要什么、何时交、报给谁或在何处开会\n"
        "- checklist: 字符串数组，严格 0~3 条，每条 ≤28 字。只写本单位必须对外交付或确认的实质动作："
        "同一材料的起草/审签/盖章/报送合并为 1 条；会议通知仅保留明确要求动作（如报名单、按时参会）；"
        "纯阅知件直接返回空数组 []；严禁拆出“研读文件/核对要求/高度重视/审签盖章/归档”等常识性内部步骤。"
    )
    # 截取头尾关键信息减少冗余 Token（保留前 2600 字 + 尾 1200 字，覆盖发文事由与文末报送时限）
    clean_body = (text or "").strip()
    if len(clean_body) > 4000:
        clipped = clean_body[:2600] + "\n...\n" + clean_body[-1200:]
    else:
        clipped = clean_body
    user_prompt = f"文件名：{filename}\n来源提示：{source_hint}\n正文内容：\n{clipped}"

    llm_raw = call_openai_compatible_api(cfg, sys_prompt, user_prompt)
    parsed = _extract_json_from_llm(llm_raw) if llm_raw else None
    if not parsed or not isinstance(parsed, dict):
        return rule_res

    doc_kind = parsed.get("doc_kind") if parsed.get("doc_kind") in ("action", "meeting", "feedback", "fyi") else rule_res.get("doc_kind", "action")
    if "checklist" in parsed and isinstance(parsed.get("checklist"), list):
        # 修复 L399 反向兜底陷阱：模型显式返回 [] 或判定为纯阅知件时尊重空列表
        if doc_kind == "fyi" and len(parsed["checklist"]) == 0:
            chk_formatted = []
        else:
            chk_formatted = _clean_checklist_items(parsed["checklist"], max_items=3, max_len=36)
    else:
        chk_formatted = [] if doc_kind == "fyi" else rule_res["checklist"]

    return {
        "title": (parsed.get("title") or rule_res["title"]).strip()[:60],
        "doc_kind": doc_kind,
        "source_channel": parsed.get("source_channel") or rule_res["source_channel"],
        "doc_number": parsed.get("doc_number") if parsed.get("doc_number") is not None else rule_res["doc_number"],
        "issuing_org": parsed.get("issuing_org") if parsed.get("issuing_org") is not None else rule_res["issuing_org"],
        "priority": parsed.get("priority") if parsed.get("priority") in ("普通", "重要", "紧急") else rule_res["priority"],
        "deadline": parsed.get("deadline") if parsed.get("deadline") is not None else rule_res["deadline"],
        "category": parsed.get("category") if parsed.get("category") in cats else rule_res["category"],
        "summary": (parsed.get("summary") or rule_res["summary"]).strip()[:200],
        "checklist": chk_formatted,
        "engine": "llm_api",
    }


def extract_reference_terms(title: str, summary: str = "") -> Dict[str, float]:
    """
    专为“库内关联参考旧稿”设计的政务业务特征词提取器：
    - 彻底过滤年份（202x）、日期（2026-11-25）、纯数字与政务虚词
    - 对标题（权重 3.0）与摘要（权重 1.0）提取高辨识度中文 2-gram / 3-gram 及专业短语
    返回 {term: weight}
    """
    def _clean_text_for_terms(s: str) -> str:
        s = s or ""
        s = re.sub(r"20\d{2}\s*[年\-/\.]\s*\d{1,2}\s*[月\-/\.]\s*\d{1,2}\s*日?", " ", s)
        s = re.sub(r"\b20\d{2}\b", " ", s)
        s = re.sub(r"[〔\[\(（]\d{4}[〕\]\)）]\s*\d+\s*号", " ", s)
        s = re.sub(r"(?:来文单位|截止期限|核心要求|来文|截止)[：:]", " ", s)
        return s

    weights: Dict[str, float] = {}
    domain_phrases = (
        "灾害民生综合保险", "灾害民生保险", "民生保险", "森林防灭火", "森林防火", "防灾减灾",
        "自然灾害", "低温雨雪", "雨雪冰冻", "防汛抗旱", "地质灾害", "地震应急", "隐患排查",
        "安全生产", "重大事故隐患", "危化品", "工贸企业", "非煤矿山", "消防安全", "救灾物资",
        "应急预案", "应急演练", "督导调度", "风险形势会商", "风险会商", "值班值守", "受灾群众",
        "冬春救助", "灾情统计", "综合保险", "防灾减灾救灾", "安全监管", "行政执法",
    )

    for raw_src, base_w in ((title, 3.0), (summary, 1.0)):
        cleaned = _clean_text_for_terms(raw_src)
        for ph in domain_phrases:
            if ph in cleaned:
                weights[ph] = max(weights.get(ph, 0.0), base_w * 2.2)

        # 提取中文连续片段并剔除虚词
        cjk_chunks = re.findall(r"[\u4e00-\u9fa5]{2,}", cleaned)
        for chunk in cjk_chunks:
            # 切掉首尾常见政务虚词
            sub = chunk
            for sw in ("关于召开", "关于开展", "关于做好", "关于印发", "关于报送", "关于转发", "关于", "的紧急通知", "的通知", "的方案", "的函", "的通报", "工作"):
                sub = sub.replace(sw, " ")
            for part in sub.split():
                part = part.strip()
                if len(part) < 2 or part in GOV_STOPWORDS:
                    continue
                if 2 <= len(part) <= 6:
                    weights[part] = max(weights.get(part, 0.0), base_w * 1.4)
                # 滑动生成 2-gram / 3-gram 克服无分词器导致的长串零命中
                if len(part) >= 3:
                    for n in (2, 3):
                        for i in range(len(part) - n + 1):
                            gram = part[i : i + n]
                            if gram in GOV_STOPWORDS:
                                continue
                            # 过滤首尾是虚字的无意义切分
                            if gram[0] in "的了和与及在为向对把被将由从" or gram[-1] in "的了和与及在为向对把被将由从":
                                continue
                            w = base_w * (1.15 if n == 3 else 0.85)
                            weights[gram] = max(weights.get(gram, 0.0), w)

    # 仅保留权重最高的 24 个有效词元
    sorted_items = sorted(weights.items(), key=lambda x: (-x[1], -len(x[0])))[:24]
    return dict(sorted_items)


def extract_search_keywords(natural_query: str) -> List[str]:
    """从自然语言搜索问句中拆解核心实词（过滤纯年份噪声，保留具体日期/编号如 11.17 与中文核心词）"""
    q = (natural_query or "").strip()
    if not q:
        return []
    split_delimiters = (
        r"或者|还是|以及|关于|有关|请问|帮我|找一下|找出|搜索|查询|哪些|什么|"
        r"我写过的|我写过|我写的|以前的|以前|历史的|历史|今年的|今年|去年的|去年|"
        r"有没有|在哪里|的|了|和|与|或|及|[，。？！、；：\"\"''（）《》【】\s]+"
    )
    parts = [p.strip() for p in re.split(split_delimiters, q) if p and p.strip()]
    generic_tail = {"材料", "公文", "通知", "文件", "要求", "工作", "事项", "记录", "情况"}
    tokens: List[str] = []

    # 先提取中文核心词，避免数字/年份挤占前 10 槽位
    for p in parts:
        cjk_only = re.sub(r"^[0-9\.\-_]+", "", p).strip()
        for candidate in (cjk_only, p):
            if len(candidate) >= 2 and candidate not in generic_tail and candidate not in GOV_STOPWORDS:
                if not re.fullmatch(r"20\d{2}", candidate) and candidate not in tokens:
                    tokens.append(candidate)
        if len(p) >= 4:
            for sub_kw in (
                "灾害民生", "民生保险", "防灾减灾", "森林防灭火", "森林防火", "冬春", "自然灾害",
                "汛情", "防汛", "地震", "预案", "隐患排查", "安全生产", "救灾物资", "总结",
                "汇报", "方案", "台账", "调度", "督导", "会议记录", "培训课件", "下沉",
            ):
                if sub_kw in p and sub_kw not in tokens:
                    tokens.append(sub_kw)

    # 再补充显式编号/月日（如 11.17 / G204），过滤纯 4 位年份
    for num_tok in re.findall(r"[A-Za-z0-9]+(?:[\.\-][A-Za-z0-9]+)*", q):
        if len(num_tok) >= 2 and not re.fullmatch(r"20\d{2}", num_tok) and num_tok not in tokens:
            tokens.append(num_tok)

    if not tokens:
        tokens = [q]
    return tokens[:10]


def synthesize_ai_search_answer(
    query: str,
    keywords: List[str],
    matched_tasks: List[Dict[str, Any]],
    matched_docs: List[Dict[str, Any]],
    matched_archives: Optional[List[Dict[str, Any]]] = None,
    matched_records: Optional[List[Dict[str, Any]]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    两段式 AI 搜索的第二阶段：覆盖【重要事项记录】、【资料存档】、【工作台待办】与【公文与材料库】四维知识解答
    """
    cfg = config or {}
    archives = matched_archives or []
    records = matched_records or []
    context_blocks = []

    for r in records[:8]:
        snippet = (r.get("match_snippet") or r.get("content_md") or "")[:320].replace("\n", " ")
        context_blocks.append(
            f"[重要事项记录 #{r['id']}] 标题：{r['title']} | 分类：{r.get('category', '')} | 内容摘录：{snippet}"
        )
    for a in archives[:8]:
        f_names = "、".join(f.get("filename", "") for f in (a.get("files") or [])[:6])
        snippet = (a.get("match_snippet") or a.get("notes_md") or "")[:240].replace("\n", " ")
        context_blocks.append(
            f"[资料存档 #{a['id']}] 标题：{a['title']} | 日期：{a.get('archive_date', '')} | 分类：{a.get('category', '')} | "
            f"内含文件：{f_names or '无'} | 纪要摘录：{snippet}"
        )
    for t in matched_tasks[:8]:
        context_blocks.append(
            f"[待办事项 #{t['id']}] 标题：{t['title']} | 文号：{t.get('doc_number') or '无'} | 状态：{t['status']} | "
            f"截止：{t.get('deadline') or '无'} | 要求摘要：{t.get('summary') or ''} | 分类：{t.get('category') or ''}"
        )
    for d in matched_docs[:12]:
        snippet = (d.get("snippet") or d.get("content_preview") or "")[:260].replace("\n", " ")
        context_blocks.append(
            f"[文件 #{d['id']}] 名称：{d['filename']} | 类型：{d['doc_role']} | 分类目录：{d['category']} | 内容摘录：{snippet}"
        )

    # 修复：仅当确实召回到本地上下文时才调用 LLM，避免 0 命中白耗 Token 或诱发编造
    if cfg.get("ai_api_base") and context_blocks:
        sys_prompt = (
            "你是公文与材料智能工作台的 AI 检索助手。请严格基于提供的本地工作台检索结果（含重要事项记录、资料存档包、工作待办事项及公文材料），"
            "直接、简明地回答用户的查询问题，按【重要事项底数与预案】、【相关资料存档】、【关联工作事项】、【相关公文与材料】分点梳理，"
            "并标明对应的标题或文件名。不要使用 Emoji，也不要编造库中不存在的数据。"
        )
        user_prompt = (
            f"用户检索问题：{query}\n"
            f"提取检索词：{', '.join(keywords)}\n"
            f"本地库召回结果（重要事项 {len(records)} 条，资料存档 {len(archives)} 包，待办事项 {len(matched_tasks)} 项，文件 {len(matched_docs)} 份）：\n"
            + "\n".join(context_blocks)
        )
        llm_ans = call_openai_compatible_api(cfg, sys_prompt, user_prompt, temperature=0.2)
        if llm_ans:
            return {
                "answer": llm_ans,
                "keywords": keywords,
                "engine": "llm_api",
            }

    if not matched_tasks and not matched_docs and not archives and not records:
        ans = (
            f"未在本地库中找到与 **“{query}”**（拆解词：`{' / '.join(keywords)}`）直接匹配的事项、存档或材料。"
            "建议尝试缩短关键词，或检查分类筛选条件。"
        )
    else:
        incoming_docs = [d for d in matched_docs if d.get("doc_role") in ("incoming", "historical")]
        my_drafts = [d for d in matched_docs if d.get("doc_role") not in ("incoming", "historical")]
        lines = [
            f"已围绕核心词 **`{'`、`'.join(keywords)}`** 完成四维全库扫描，共为您定位到 "
            f"**{len(records)} 条重要事项记录**、**{len(archives)} 个资料存档包**、**{len(matched_tasks)} 项工作待办** 以及 **{len(matched_docs)} 份公文与材料**："
        ]
        if records:
            lines.append("\n**1. 重要事项记录（底数 / 预案直读）：**")
            for r in records[:5]:
                lines.append(f"- **《{r['title']}》**（分类：`{r.get('category', '')}`）—— {(r.get('match_snippet') or r.get('content_md') or '')[:90]}")
        if archives:
            lines.append("\n**2. 资料存档（会议记录 / 培训课件 / 下沉照片）：**")
            for a in archives[:5]:
                fcnt = len(a.get("files") or [])
                lines.append(f"- **{a['title']}**（分类：`{a.get('category', '')}`，共 {fcnt} 份多媒体附件）—— {(a.get('notes_md') or '')[:80]}")
        if matched_tasks:
            lines.append("\n**3. 关联工作待办事项：**")
            for t in matched_tasks[:5]:
                status_label = {"pending": "待办", "in_progress": "进行中", "archived": "已完成归档"}.get(t["status"], t["status"])
                lines.append(f"- **《{t['title']}》**（{status_label}，分类：`{t.get('category', '')}`）—— {t.get('summary', '')[:90]}")
        if my_drafts:
            lines.append("\n**4. 相关材料 / 存档附件 / 范文：**")
            for d in my_drafts[:5]:
                lines.append(f"- **{d['filename']}**（目录：`{d.get('category', '')}`）—— 摘录：{(d.get('snippet') or '')[:80]}")
        if incoming_docs:
            lines.append("\n**5. 相关上级来文 / 历史归档公文：**")
            for d in incoming_docs[:5]:
                lines.append(f"- **{d['filename']}**（目录：`{d.get('category', '')}`）—— 摘录：{(d.get('snippet') or '')[:80]}")
        ans = "\n".join(lines)

    return {
        "answer": ans,
        "keywords": keywords,
        "engine": "smart_local",
    }
