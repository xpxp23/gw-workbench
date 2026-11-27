#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
公文与通知 AI 解析、四维知识检索（待办+公文+资料存档+重要事项记录）、文件角色智能判别与阶段总结引擎 (ai_engine.py v3.3.0)
"""

import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("gw_workbench.ai_engine")

# HTTP 响应体最大读取上限（2MB），防止异常接口打爆内存
MAX_API_RESPONSE_BYTES = 2 * 1024 * 1024

# AI 检索上下文总字符预算，防止小上下文本地模型溢出
MAX_SEARCH_CONTEXT_CHARS = 6000

# 线程隔离的最近一次 API 调用错误信息，避免多线程并发覆写
_API_ERR_TLS = threading.local()


def _set_last_api_error(msg: str) -> None:
    _API_ERR_TLS.last_error = msg
    call_openai_compatible_api.last_error = msg

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


def _safe_str(val: Any) -> str:
    """安全将 LLM 返回的任意类型字段转换为去首尾空白的字符串，绝不因非字符串抛出 AttributeError"""
    if val is None or isinstance(val, bool):
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (int, float)):
        return str(val).strip()
    if isinstance(val, (list, tuple)):
        parts = [_safe_str(item) for item in val]
        return "、".join(p for p in parts if p)
    if isinstance(val, dict):
        if not val:
            return ""
        try:
            return json.dumps(val, ensure_ascii=False).strip()
        except Exception:
            return str(val).strip()
    return str(val).strip()


def _validate_deadline(val: Any) -> str:
    """严格校验 YYYY-MM-DD 合法公历日期；非法字符串返回空字符串"""
    s = _safe_str(val)
    if not s or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return ""
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return ""


def get_last_api_error() -> str:
    """获取当前线程最近一次 LLM API 调用的错误描述（成功时为空字符串）"""
    return getattr(_API_ERR_TLS, "last_error", "")


def _is_openai_o_series(model: str) -> bool:
    """判断是否为 OpenAI o1/o3/o4 推理模型系列（避免误伤自定义 o10 等模型名）"""
    m = (model or "").strip().lower()
    return bool(re.search(r"(?:^|[/:\-_])o[134](?:$|[\-_](?:mini|preview|pro|high|low|\d{4}))", m))


def is_reasoning_model(model: str) -> bool:
    """判断是否为推理模型（支持 OpenAI o1/o3/o4、DeepSeek-R1/Reasoner、QwQ）"""
    m = (model or "").strip().lower()
    if not m:
        return False
    if _is_openai_o_series(m):
        return True
    return any(tag in m for tag in ("deepseek-r1", "deepseek-reasoner", "qwq"))


def _is_private_or_local_url(api_base: str) -> bool:
    """判断 API 地址是否属于本机或 RFC1918/内网地址（支持 IP、localhost、.local/.lan/.internal 及无点号单段主机名）"""
    try:
        raw = (api_base or "").strip()
        if not raw:
            return False
        parsed = urllib.parse.urlparse(raw if "://" in raw else f"http://{raw}")
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False
        if (
            host in ("localhost", "127.0.0.1", "::1")
            or host.endswith(".local")
            or host.endswith(".lan")
            or host.endswith(".internal")
        ):
            return True
        # 单段无点号主机名（如 Docker/内网 DNS 中的 ollama、vllm、llm-server）
        if "." not in host and ":" not in host:
            return True
        ip_obj = ipaddress.ip_address(host)
        return bool(ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local)
    except ValueError:
        return False
    except Exception:
        return False


def _normalize_chat_completions_url(api_base: str) -> str:
    """规范化 OpenAI 兼容 Chat Completions URL，自动补全 http:// 与路径，兼容 Ollama 裸端口与带查询参数端点"""
    base = (api_base or "").strip()
    if not base:
        return ""
    if "://" not in base:
        base = f"http://{base}"
    parsed = urllib.parse.urlsplit(base)
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment))
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
    timeout: int = 20,
    max_tokens: int = 1200,
    retries: int = 1,
) -> Optional[str]:
    """
    调用兼容 OpenAI Chat Completions 的接口：
    - 默认超时预算 timeout=20, retries=1，最坏总耗时 <= 42s（远低于 wb.py 60s 阈值）
    - 读取响应体上限 2MB，防止超大异常包撑爆内存
    - HTTP 400/401/403/404 直接返回不重试，并记录清晰错误日志与 last_error
    - 兼容推理模型（o1/o3/o4 传 max_completion_tokens，推理模型自动提升 token 上限 >= 3000）
    """
    _set_last_api_error("")

    api_base = _safe_str((config or {}).get("ai_api_base")).rstrip("/")
    api_key = _safe_str((config or {}).get("ai_api_key"))
    model = _safe_str((config or {}).get("ai_model")) or "deepseek-chat"

    if not api_base:
        _set_last_api_error("missing ai_api_base")
        return None
    if not api_key and not _is_private_or_local_url(api_base):
        _set_last_api_error(f"missing api_key for non-private endpoint {api_base}")
        logger.warning("AI API skipped: %s", get_last_api_error())
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

    reasoning = is_reasoning_model(model)
    if reasoning:
        effective_max_tokens = max(max_tokens, 3000) if max_tokens > 0 else 3000
        if _is_openai_o_series(model):
            payload["max_completion_tokens"] = effective_max_tokens
        else:
            payload["max_tokens"] = effective_max_tokens
    else:
        payload["temperature"] = temperature
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens

    data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    attempts = max(1, retries + 1)
    start_ts = time.monotonic()
    total_budget = 42.0

    for attempt in range(attempts):
        elapsed = time.monotonic() - start_ts
        remaining_budget = total_budget - elapsed
        if remaining_budget <= 1.0:
            _set_last_api_error("total timeout budget exceeded")
            return None
        eff_timeout = max(3.0, min(float(timeout), remaining_budget))
        try:
            req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=eff_timeout) as resp:
                raw_bytes = resp.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_API_RESPONSE_BYTES:
                    _set_last_api_error("API response exceeds 2MB safety limit")
                    logger.warning("AI API response truncated (>2MB) on %s", url)
                    return None
                body = json.loads(raw_bytes.decode("utf-8", errors="replace"))
                choices = body.get("choices") if isinstance(body, dict) else None
                if not isinstance(choices, list) or not choices:
                    _set_last_api_error("empty choices in API response")
                    logger.warning("AI API returned empty choices on %s", url)
                    return None
                c0 = choices[0] if isinstance(choices[0], dict) else {}
                msg = c0.get("message") if isinstance(c0, dict) else None
                content = _safe_str(msg.get("content") if isinstance(msg, dict) else "")
                if not content and isinstance(msg, dict) and msg.get("reasoning_content"):
                    _set_last_api_error("reasoning model exhausted max_tokens before emitting content")
                    logger.warning("AI API empty content with reasoning_content (finish_reason=%s)", c0.get("finish_reason"))
                    return None
                return content
        except urllib.error.HTTPError as e:
            status = getattr(e, "code", 0)
            err_body = ""
            try:
                err_body = e.read(512).decode("utf-8", errors="replace").strip()
            except Exception:
                pass
            _set_last_api_error(f"HTTP {status}" + (f": {err_body[:200]}" if err_body else ""))
            if status in (400, 401, 403, 404):
                logger.error("AI API non-retryable %s on %s", get_last_api_error(), url)
                return None
            logger.warning("AI API HTTP %s (attempt %d/%d) on %s", status, attempt + 1, attempts, url)
            if status in (429, 500, 502, 503, 504) and attempt + 1 < attempts:
                wait_sec = 0.8 * (attempt + 1)
                if status == 429 and getattr(e, "headers", None):
                    ra = e.headers.get("Retry-After")
                    if ra and str(ra).strip().isdigit():
                        wait_sec = min(6.0, max(0.5, float(ra)))
                if time.monotonic() - start_ts + wait_sec < total_budget:
                    time.sleep(wait_sec)
                    continue
            return None
        except Exception as e:
            _set_last_api_error(f"{type(e).__name__}: {e}")
            logger.warning("AI API error (attempt %d/%d) on %s: %s", attempt + 1, attempts, url, get_last_api_error())
            if attempt + 1 < attempts and (time.monotonic() - start_ts + 0.6 < total_budget):
                time.sleep(0.6)
                continue
            return None
    return None


call_openai_compatible_api.last_error = ""


def _strip_trailing_commas(text: str) -> str:
    """安全移除 JSON 对象/数组中的尾随逗号，绝不破坏字符串字面量内部的 `, }` 或 `, ]`"""
    out: List[str] = []
    n = len(text)
    in_str = False
    escaped = False
    i = 0
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _extract_json_from_llm(raw_text: str) -> Optional[Dict[str, Any]]:
    """健壮提取 LLM 返回的 JSON：支持剥离 <think> 块、遍历多个代码块逐个解析、安全去尾逗号（不破坏字符串字面量）"""
    if not raw_text:
        return None
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", raw_text, flags=re.IGNORECASE).strip()
    if "<think>" in cleaned.lower():
        cleaned = re.sub(r"<think>[\s\S]*$", "", cleaned, flags=re.IGNORECASE).strip()

    def _try_parse(candidate: str) -> Optional[Dict[str, Any]]:
        cand = (candidate or "").strip()
        if not cand:
            return None
        try:
            obj = json.loads(cand)
            return obj if isinstance(obj, dict) else None
        except Exception:
            fixed = _strip_trailing_commas(cand)
            try:
                obj = json.loads(fixed)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None

    def _scan_balanced_objects(source: str) -> Optional[Dict[str, Any]]:
        start_idx = -1
        depth = 0
        in_str = False
        escaped = False
        for i, ch in enumerate(source):
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
                        res = _try_parse(source[start_idx : i + 1])
                        if res is not None:
                            return res
                        start_idx = -1
        return None

    # 1. 遍历所有 Markdown 代码块逐个尝试解析合法 JSON 对象
    for fence_match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE):
        block = fence_match.group(1).strip()
        parsed = _try_parse(block) or _scan_balanced_objects(block)
        if parsed is not None:
            return parsed

    # 2. 直接整体解析
    direct = _try_parse(cleaned)
    if direct is not None:
        return direct

    # 3. 栈匹配提取最外层完整 JSON 对象，避免贪婪正则跨段污染
    return _scan_balanced_objects(cleaned)


# 待办目录内判定为“来文原件”的强特征。用户约定：拿不准就当自己写的材料（v3.1.1）
_INCOMING_STRONG = (
    "关于开展", "关于做好", "关于印发", "关于报送", "关于转发", "关于召开", "关于调度",
    "的通报", "上级", "来文", "明电", "微信通知截图", "紧急通知",
)
# 弱特征：仅“公文与材料库”内参与判断；待办目录内一律不认，
# 否则我自己起草的《关于……的通知》《关于征求……的函》会被误判成来文。
_INCOMING_WEAK = ("的通知", "的方案", "的函")
# 正文套话特征（各镇街……现将有关事项通知如下）是我方发文的开头，
# 只在库内做兜底，绝不能在待办目录内当来文判据。
_INCOMING_BODY = ("各镇街", "各区县", "各市应急管理局", "各有关单位", "现将有关事项通知如下")


def infer_doc_role(
    filename: str,
    text_preview: str = "",
    in_task_folder: bool = False,
    cat_name: str = "",
    sub_folder: str = "",
) -> str:
    """
    智能判别本地新增或扫描文件的性质角色 ('incoming' | 'draft' | 'template' | 'historical' | 'archive' | 'record')

    v3.3.0：
    - 支持 sub_folder 参数：当 in_task_folder=True 且 sub_folder 非空且文件名非 [来文] 开头时，直接返回 'draft'
    - 清理历史不可达死代码分支
    """
    fn = (filename or "").strip()
    preview = (text_preview or "")[:400]
    combined = f"{fn} {preview}"
    # 待办目录内只看文件名里的文号，避免正文引用上级文号被误判
    sig_text = fn if in_task_folder else combined

    # 只读兼容历史遗留前缀
    if fn.startswith(("[来文]", "【来文】", "来文_")):
        return "incoming"
    if fn.startswith(("[材料]", "【材料】", "材料_")):
        return "draft"

    # 任务子目录内的非 [来文] 文件，直接视为办理过程材料/文稿
    if in_task_folder and (sub_folder or "").strip():
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
    if any(k in fn for k in draft_feature):
        return "draft"
    if fn.lower().endswith(".ofd") or re.search(r"[〔\[\(（]20\d{2}[〕\]\)）]\s*\d+\s*号", sig_text):
        return "incoming"
    if fn.startswith(("附件", "附表")):
        return "incoming"
    if any(k in fn for k in _INCOMING_STRONG):
        return "incoming"

    if not in_task_folder:
        if any(k in fn for k in _INCOMING_WEAK):
            return "incoming"
        if any(k in preview for k in _INCOMING_BODY):
            return "incoming"

    return "draft"


def match_best_category(text_and_title: str, categories: Optional[List[str]] = None) -> str:
    """根据实际分类名动态匹配优先，结合通用政务业务语义推荐物理分类目录（弱化地域词硬编码）"""
    avail_cats = list(categories) if categories else [
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

    # 1. 优先基于实际分类名动态匹配（拆解分类名中的业务词与地域/层级词并按命中长度计分）
    best_cat = ""
    best_score = 0
    for c in avail_cats:
        clean_c = re.sub(r"^\d+[_\-\s]*", "", c).strip()
        if not clean_c:
            continue
        score = 0
        if len(clean_c) >= 2 and clean_c in txt:
            score += len(clean_c) * 3
        raw_segs = [s.strip() for s in re.split(r"[与和及_\-\s/、]+", clean_c) if s.strip()]
        for seg in raw_segs:
            if len(seg) >= 2 and seg in txt:
                score += len(seg) * 2
            # 提取分类名内不含通用后缀（来文/工作/通知/文件）的核心短语（如 省及潍坊来文 -> 潍坊、高密部门来文 -> 高密）
            core = re.sub(r"(?:来文|工作|通知|文件|材料|部门)$", "", seg).strip()
            if len(core) >= 2 and core != seg and core in txt:
                score += len(core) * 2
        if score > best_score:
            best_score = score
            best_cat = c
    if best_cat:
        return best_cat

    # 2. 通用政务业务语义规则（不依赖具体地名硬编码）
    semantic_rules: List[Tuple[Tuple[str, ...], Tuple[str, ...]]] = [
        (("征求意见", "反馈意见", "修改意见", "书面意见", "无意见"), ("反馈", "意见")),
        (("会议", "培训", "视频会", "调度会", "座谈会", "参会", "会场", "讲座"), ("会议", "培训")),
        (("督查", "考核", "整改", "暗访", "通报", "约谈", "巡查", "回头看", "销号"), ("督查", "考核", "整改")),
        (("协助", "配合", "协办", "联合开展", "抽调"), ("协助", "配合")),
        (("省应急", "省厅", "省政府", "省安委", "市应急", "市局", "市政府", "市安委", "上级"), ("省", "市", "上级", "来文")),
        (("本级", "各镇街", "镇街", "街道", "镇政府", "本部门", "县直", "区直", "部门"), ("部门", "本级", "镇街")),
        # 兼容业务条线分类体系
        (("防灾", "减灾", "救灾", "防汛", "汛情", "抗旱", "森林防火", "森林防灭火", "森防", "地震", "地质灾害", "低温雨雪", "冰冻", "台风", "自然灾害", "冬春"), ("防灾", "减灾")),
        (("隐患", "安全生产", "重大事故", "危化", "工贸", "非煤矿山", "消防", "火灾", "排查", "整治", "执法", "安委"), ("隐患", "安全")),
        (("总结", "汇报", "述职", "讲话", "工作要点", "计划", "季报", "月报", "年终", "范文", "模板"), ("汇报", "总结")),
    ]
    for keywords, cat_hints in semantic_rules:
        if any(k in txt for k in keywords):
            for c in avail_cats:
                if any(h in c for h in cat_hints):
                    return c

    # 3. 通用兜底：优先选择“来文/日常/通知/综合”类目录，否则返回首个分类
    for c in avail_cats:
        if any(h in c for h in ("来文", "日常", "通知", "综合")):
            return c
    return avail_cats[0]


def _clean_checklist_items(raw_items: List[Any], max_items: int = 3, max_len: int = 36) -> List[Dict[str, Any]]:
    """统一清洗、去重、截断待办细项，剔除空泛套话与超长原文"""
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for item in raw_items or []:
        if isinstance(item, dict):
            text = _safe_str(item.get("text"))
            done = bool(item.get("done", False))
        elif isinstance(item, str):
            text = item.strip()
            done = False
        else:
            text = _safe_str(item)
            done = False
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


def _safe_date_for_year(y: int, month: int, day: int) -> datetime:
    """构造日期，平年遇到 2 月 29 日时安全回退至 2 月 28 日或寻找下个闰年"""
    try:
        return datetime(y, month, day)
    except ValueError:
        if month == 2 and day == 29:
            return datetime(y, 2, 28)
        raise


def _resolve_inferred_date(year: Optional[int], month: int, day: int, now: datetime) -> Optional[str]:
    """根据提取到的年月日构建 YYYY-MM-DD；缺省年份时若早于当前日期超过 60 天，自动推算为下一年（修复闰年 2 月 29 日崩溃）"""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        if year is not None:
            dt = _safe_date_for_year(year, month, day)
        else:
            y = today.year
            dt = _safe_date_for_year(y, month, day)
            if (today - dt).days > 60:
                dt = _safe_date_for_year(dt.year + 1, month, day)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


def _extract_rule_deadline(clean_text: str, now: Optional[datetime] = None) -> str:
    """
    规则引擎截止日期提取：
    1. 优先抓取靠近“前报送/截止/之前/限期”等强触发词的日期，避免误抓文末成文日期
    2. 缺省年份时若推断日期早于当前日期超过 60 天（如 12 月收到“1 月 5 日前”），自动推算为下一年
    """
    ref_now = now or datetime.now()
    if not clean_text:
        return ""

    time_opt = r"(?:\s*(?:上午|下午|中午|晚上)?\s*\d{1,2}\s*[时点:：]\s*\d{0,2}\s*分?)?"
    strong_suffix = r"(?:前报送|前上报|前提交|前反馈|前完成|前报|之前|以前|下班前|前|截止|限期)"
    strong_prefix = r"(?:截止(?:时间|日期)?(?:为|至)?|截至|限期|不晚于|最迟(?:于)?|最晚(?:于)?)"

    # Tier 1: 带强截止触发词的日期（优先后置“前报送/之前/截止”或前置“截止/限期/不晚于”）
    tier1_patterns: List[Tuple[str, bool]] = [
        (rf"(?:请于|务必于|于|在|{strong_prefix})?\s*(\d{{4}})\s*[年\-/\.]\s*(\d{{1,2}})\s*[月\-/\.]\s*(\d{{1,2}})\s*日?{time_opt}\s*{strong_suffix}", True),
        (rf"{strong_prefix}[：:\s]*(\d{{4}})\s*[年\-/\.]\s*(\d{{1,2}})\s*[月\-/\.]\s*(\d{{1,2}})\s*日?", True),
        (rf"(?:请于|务必于|于|在|{strong_prefix})?\s*(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*日{time_opt}\s*{strong_suffix}", False),
        (rf"{strong_prefix}[：:\s]*(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*日", False),
    ]
    for pat, has_year in tier1_patterns:
        for m in re.finditer(pat, clean_text):
            groups = m.groups()
            if has_year:
                res = _resolve_inferred_date(int(groups[0]), int(groups[1]), int(groups[2]), ref_now)
            else:
                res = _resolve_inferred_date(None, int(groups[0]), int(groups[1]), ref_now)
            if res:
                return res

    # Tier 2: 次强触发词（请于/务必于 ... 内/报送/提交/反馈/完成）
    tier2_patterns: List[Tuple[str, bool]] = [
        (rf"(?:请于|务必于)\s*(\d{{4}})\s*[年\-/\.]\s*(\d{{1,2}})\s*[月\-/\.]\s*(\d{{1,2}})\s*日?{time_opt}\s*(?:内|报送|提交|反馈|完成)", True),
        (rf"(?:请于|务必于)\s*(\d{{1,2}})\s*月\s*(\d{{1,2}})\s*日{time_opt}\s*(?:内|报送|提交|反馈|完成)", False),
    ]
    for pat, has_year in tier2_patterns:
        for m in re.finditer(pat, clean_text):
            groups = m.groups()
            if has_year:
                res = _resolve_inferred_date(int(groups[0]), int(groups[1]), int(groups[2]), ref_now)
            else:
                res = _resolve_inferred_date(None, int(groups[0]), int(groups[1]), ref_now)
            if res:
                return res

    # Tier 3: 相对时间词（今日/明日/后天）优先于裸成文日期
    if any(k in clean_text for k in ("今日下班前", "今天下班前", "今日内", "今天内", "立即报送")):
        return ref_now.strftime("%Y-%m-%d")
    if any(k in clean_text for k in ("明日", "明天")):
        return (ref_now + timedelta(days=1)).strftime("%Y-%m-%d")
    if "后天" in clean_text:
        return (ref_now + timedelta(days=2)).strftime("%Y-%m-%d")

    # Tier 4: 兜底扫描非落款成文日期的普通日期（剔除文末独立成行的成文日期）
    lines = [ln.strip() for ln in clean_text.splitlines() if ln.strip()]
    body_lines = list(lines)
    while body_lines and re.fullmatch(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日", body_lines[-1]):
        body_lines.pop()
    fallback_text = "\n".join(body_lines)

    for pat, has_year in (
        (r"(\d{4})\s*[年\-]\s*(\d{1,2})\s*[月\-]\s*(\d{1,2})\s*日", True),
        (r"(\d{1,2})\s*月\s*(\d{1,2})\s*日", False),
    ):
        for m in re.finditer(pat, fallback_text):
            groups = m.groups()
            if has_year:
                res = _resolve_inferred_date(int(groups[0]), int(groups[1]), int(groups[2]), ref_now)
            else:
                res = _resolve_inferred_date(None, int(groups[0]), int(groups[1]), ref_now)
            if res:
                return res

    return ""


def _sample_long_document(clean_body: str, max_len: int = 4000) -> str:
    """
    长公文三段智能采样：
    超过 4000 字时采用“首 2000 字 + 中段（命中任务分工/时间节点/报送要求关键词处）1200 字 + 尾 1000 字”
    """
    if len(clean_body) <= max_len:
        return clean_body

    head_len = 2000
    mid_len = 1200
    tail_len = 1000

    head = clean_body[:head_len]
    tail = clean_body[-tail_len:]
    mid_region = clean_body[head_len : len(clean_body) - tail_len]

    if len(mid_region) <= mid_len:
        return clean_body

    mid_keywords = (
        "任务分工", "职责分工", "责任分工", "工作分工",
        "时间节点", "时间安排", "进度安排", "工作步骤",
        "报送要求", "上报要求", "填报要求", "反馈要求",
        "截止时间", "前报送", "工作要求", "具体要求", "参会要求",
    )
    hit_pos = -1
    for kw in mid_keywords:
        idx = mid_region.find(kw)
        if idx != -1:
            hit_pos = idx
            break

    if hit_pos != -1:
        start = max(0, min(hit_pos - 120, len(mid_region) - mid_len))
    else:
        start = (len(mid_region) - mid_len) // 2

    mid = mid_region[start : start + mid_len]
    return f"{head}\n...\n{mid}\n...\n{tail}"


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
        r"([\u4e00-\u9fa5]{2,12}[〔\[\(（]\d{4}[〕\]\)）]\s*\d+\s*号)",
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

    deadline = _extract_rule_deadline(clean_text, now)

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
    memory_prompt: str = "",
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
    default_org = _safe_str(cfg.get("default_org_name")) or "本科室"
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
    if memory_prompt and memory_prompt.strip():
        sys_prompt = sys_prompt + "\n\n" + memory_prompt.strip()
    # 超过 4000 字长公文采用首 2000 + 中段关键词 1200 + 尾 1000 三段智能采样
    clean_body = (text or "").strip()
    clipped = _sample_long_document(clean_body, max_len=4000)
    user_prompt = f"文件名：{filename}\n来源提示：{source_hint}\n正文内容：\n{clipped}"

    llm_raw = call_openai_compatible_api(cfg, sys_prompt, user_prompt)
    parsed = _extract_json_from_llm(llm_raw) if llm_raw else None
    if not parsed or not isinstance(parsed, dict):
        return rule_res

    llm_doc_kind = _safe_str(parsed.get("doc_kind"))
    doc_kind = llm_doc_kind if llm_doc_kind in ("action", "meeting", "feedback", "fyi") else rule_res.get("doc_kind", "action")

    raw_checklist = parsed.get("checklist")
    if "checklist" in parsed and isinstance(raw_checklist, list):
        # 修复 L399 反向兜底陷阱：模型显式返回 [] 或判定为纯阅知件时尊重空列表
        if doc_kind == "fyi" and len(raw_checklist) == 0:
            chk_formatted = []
        else:
            chk_formatted = _clean_checklist_items(raw_checklist, max_items=3, max_len=36)
    else:
        chk_formatted = [] if doc_kind == "fyi" else rule_res["checklist"]

    llm_title = _safe_str(parsed.get("title"))
    llm_source_channel = _safe_str(parsed.get("source_channel"))
    llm_doc_number = _safe_str(parsed.get("doc_number"))
    llm_issuing_org = _safe_str(parsed.get("issuing_org"))
    llm_priority = _safe_str(parsed.get("priority"))
    llm_deadline = _validate_deadline(parsed.get("deadline"))
    llm_category = _safe_str(parsed.get("category"))
    llm_summary = _safe_str(parsed.get("summary"))

    rule_deadline = _validate_deadline(rule_res.get("deadline"))

    return {
        "title": (llm_title or rule_res["title"])[:60],
        "doc_kind": doc_kind,
        "source_channel": llm_source_channel if llm_source_channel in ("上级来文", "微信群", "电话通知", "自建") else rule_res["source_channel"],
        "doc_number": llm_doc_number or rule_res["doc_number"],
        "issuing_org": llm_issuing_org or rule_res["issuing_org"],
        "priority": llm_priority if llm_priority in ("普通", "重要", "紧急") else rule_res["priority"],
        "deadline": llm_deadline or rule_deadline,
        "category": llm_category if llm_category in cats else rule_res["category"],
        "summary": (llm_summary or rule_res["summary"])[:200],
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
    """
    从自然语言搜索问句中拆解核心实词：
    - 仅在标点与多字停用短语边界切分，绝不用单字（的/了/和/与/或/及）腰斩“及时报送/涉及危化品/了解情况”等词
    - 保留用户显式输入的年份词（如 2025、2026）与具体日期/编号（如 11.17 / G204）
    """
    q = (natural_query or "").strip()
    if not q:
        return []

    # 仅在词边界停用短语与标点空白处切分，避免单字腰斩实词内部
    split_delimiters = (
        r"或者|还是|以及|关于|有关|请问|帮我|找一下|找出|搜索|查询|哪些|什么|"
        r"我写过的|我写过|我写的|以前的|以前|历史的|历史|今年的|今年|去年的|去年|"
        r"有没有|在哪里|[，。？！、；：\"\"''（）《》【】\s]+"
    )
    raw_parts = [p.strip() for p in re.split(split_delimiters, q) if p and p.strip()]

    # 对片段内“……的(材料|公文|通知|文件|方案|报告|总结|汇报|台账|预案|清单|记录|要求|工作|情况)”在结构助词边界拆分
    parts: List[str] = []
    for rp in raw_parts:
        sub_parts = re.split(
            r"的(?=(?:材料|公文|通知|文件|要求|工作|事项|记录|情况|方案|报告|总结|汇报|台账|预案|会议|培训|制度|清单|名单))",
            rp,
        )
        for sp in sub_parts:
            sp = sp.strip()
            if len(sp) >= 3 and sp.endswith("的") and not sp.endswith(("目的", "标的")):
                sp = sp[:-1].strip()
            if len(sp) >= 3 and sp.startswith("的") and not sp.startswith("的确"):
                sp = sp[1:].strip()
            if sp:
                parts.append(sp)

    generic_tail = {"材料", "公文", "通知", "文件", "要求", "工作", "事项", "记录", "情况"}
    tokens: List[str] = []

    # 先提取中文核心词，同时兼容前置年份/编号（如“2025年防汛总结” -> “防汛总结” + “2025”）
    for p in parts:
        cjk_only = re.sub(r"^[0-9\.\-_]+年?", "", p).strip()
        for candidate in (cjk_only, p):
            if len(candidate) >= 2 and candidate not in generic_tail and candidate not in GOV_STOPWORDS:
                if candidate not in tokens:
                    tokens.append(candidate)
        if len(p) >= 4:
            for sub_kw in (
                "灾害民生", "民生保险", "防灾减灾", "森林防灭火", "森林防火", "冬春", "自然灾害",
                "汛情", "防汛", "地震", "预案", "隐患排查", "安全生产", "救灾物资", "危化品",
                "总结", "汇报", "方案", "台账", "调度", "督导", "会议记录", "培训课件", "下沉",
            ):
                if sub_kw in p and sub_kw not in tokens:
                    tokens.append(sub_kw)

    # 补充显式年份（如 2025 / 2026）与编号/月日（如 11.17 / G204），不再过滤 20\d{2}
    for num_tok in re.findall(r"[A-Za-z0-9]+(?:[\.\-][A-Za-z0-9]+)*", q):
        if len(num_tok) >= 2 and num_tok not in tokens:
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
    memory_prompt: str = "",
) -> Dict[str, Any]:
    """
    两段式 AI 搜索的第二阶段：覆盖【重要事项记录】、【资料存档】、【工作台待办】与【公文与材料库】四维知识解答
    内置总上下文字符预算（默认 6000 字）按相关度截断，防止小上下文本地模型溢出。
    """
    cfg = config or {}
    archives = matched_archives or []
    records = matched_records or []

    max_ctx_chars = MAX_SEARCH_CONTEXT_CHARS
    try:
        cfg_budget = int(cfg.get("ai_search_context_budget") or 0)
        if cfg_budget >= 500:
            max_ctx_chars = cfg_budget
    except Exception:
        pass

    raw_blocks: List[str] = []
    for r in records[:8]:
        snippet = _safe_str(r.get("match_snippet") or r.get("content_md"))[:320].replace("\n", " ")
        raw_blocks.append(
            f"[重要事项记录 #{r['id']}] 标题：{r['title']} | 分类：{r.get('category', '')} | 内容摘录：{snippet}"
        )
    for a in archives[:8]:
        f_names = "、".join(_safe_str(f.get("filename")) for f in (a.get("files") or [])[:6] if isinstance(f, dict))
        snippet = _safe_str(a.get("match_snippet") or a.get("notes_md"))[:240].replace("\n", " ")
        raw_blocks.append(
            f"[资料存档 #{a['id']}] 标题：{a['title']} | 日期：{a.get('archive_date', '')} | 分类：{a.get('category', '')} | "
            f"内含文件：{f_names or '无'} | 纪要摘录：{snippet}"
        )
    for t in matched_tasks[:8]:
        raw_blocks.append(
            f"[待办事项 #{t['id']}] 标题：{t['title']} | 文号：{t.get('doc_number') or '无'} | 状态：{t['status']} | "
            f"截止：{t.get('deadline') or '无'} | 要求摘要：{_safe_str(t.get('summary'))[:240]} | 分类：{t.get('category') or ''}"
        )
    for d in matched_docs[:12]:
        snippet = _safe_str(d.get("snippet") or d.get("content_preview"))[:260].replace("\n", " ")
        raw_blocks.append(
            f"[文件 #{d['id']}] 名称：{d['filename']} | 类型：{d['doc_role']} | 分类目录：{d['category']} | 内容摘录：{snippet}"
        )

    # 按相关度顺序装入上下文，严格控制在总字符预算内
    context_blocks: List[str] = []
    used_chars = 0
    for block in raw_blocks:
        sep = 1 if context_blocks else 0
        if used_chars + sep + len(block) <= max_ctx_chars:
            context_blocks.append(block)
            used_chars += sep + len(block)
        else:
            remaining = max_ctx_chars - used_chars - sep
            if remaining >= 80:
                context_blocks.append(block[: remaining - 1] + "…")
                used_chars += sep + remaining
            break

    # 修复：仅当确实召回到本地上下文时才调用 LLM，避免 0 命中白耗 Token 或诱发编造
    if cfg.get("ai_api_base") and context_blocks:
        sys_prompt = (
            "你是公文与材料智能工作台的 AI 检索助手。请严格基于提供的本地工作台检索结果（含重要事项记录、资料存档包、工作待办事项及公文材料），"
            "直接、简明地回答用户的查询问题，按【重要事项底数与预案】、【相关资料存档】、【关联工作事项】、【相关公文与材料】分点梳理，"
            "并标明对应的标题或文件名。不要使用 Emoji，也不要编造库中不存在的数据。"
        )
        if memory_prompt and memory_prompt.strip():
            sys_prompt = sys_prompt + "\n\n" + memory_prompt.strip()
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
