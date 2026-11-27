#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GB/T 9704—2012 党政机关公文标准排版与 xkl-gw 联动引擎 (gw_formatter.py v3.2.0)
修复与增强：
1. P0 修复 >17 列表格负宽度损坏 .docx：按比例均匀分配列宽，确保每列与末列恒为正数
2. P0 增加 XML 1.0 非法控制字符清洗 _sanitize_xml_text（剔除控制字符并转义单引号为 &apos;）
3. P1 修正 GB/T 9704—2012 规范：三级标题默认不加粗（仅支持显式加粗）；落款以成文日期（右空四字）居中编排并排除抄送/报送/印发/联系误判；report 模式不强行追加假落款；红头模式绝不硬编码假机关/假文号，主标题空二行(1120 twips)，发文机关标志按字数自适应字号
4. P1 xkl_gw_cli 透传 mode/title/doc_number/issuing_org 参数并优雅降级，Windows 下加 CREATE_NO_WINDOW
5. P1 Markdown 解析增强：保留嵌套列表缩进、保留行内 **加粗** 为真实 <w:b/> Run、修正斜体正则防误伤乘号、支持 #/#####/###### 标题与多级 > 引用及 120 字联合主送机关、修复表格分隔行/转义 \|/无首尾 | GFM 表格、未闭合代码块回退为普通段落
6. P1 fontTable.xml 与 _make_w_tbl 完整响应 font_overrides，方正小标宋简体补充 Noto Serif CJK SC 衬线回退，页码字体跨平台兼容
"""

import html
import os
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_XML_ILLEGAL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# 仅匹配两侧不紧邻空白与星号的单个 *...* 斜体，避免误伤 3 * 5 * 2 等乘号表达式
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s|\*)([^*\n]+?)(?<!\s|\*)\*(?!\*)")
_MD_BOLD_SPLIT_RE = re.compile(r"\*\*([^\n*]+?)\*\*")


def _sanitize_xml_text(text: str) -> str:
    """剔除 XML 1.0 非法控制字符并做 XML 安全转义（单引号转为 &apos;）"""
    if not text:
        return ""
    cleaned = _XML_ILLEGAL_RE.sub("", str(text))
    return html.escape(cleaned, quote=True).replace("&#x27;", "&apos;")


def _subprocess_kwargs(timeout: int = 20) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kw


def _clean_md_inline_keep_bold(text: str) -> str:
    """清洗图片/链接/斜体/删除线/行内代码，但保留 **加粗** 标记供 Run 渲染"""
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = _MD_ITALIC_RE.sub(r"\1", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s.strip()


def _clean_md_inline(text: str) -> str:
    """将行内 Markdown 转为纯文本（不误伤 3 * 5 * 2 乘号）"""
    s = _clean_md_inline_keep_bold(text)
    s = _MD_BOLD_SPLIT_RE.sub(r"\1", s)
    return s.strip()


def _split_inline_bold_segments(text: str) -> List[Tuple[str, bool]]:
    """将保留 **...** 的文本拆分为 [(segment, is_bold), ...]"""
    if not text:
        return []
    segments: List[Tuple[str, bool]] = []
    pos = 0
    for m in _MD_BOLD_SPLIT_RE.finditer(text):
        if m.start() > pos:
            segments.append((text[pos : m.start()], False))
        segments.append((m.group(1), True))
        pos = m.end()
    if pos < len(text):
        segments.append((text[pos:], False))
    return segments or [(text, False)]


def _split_md_table_row(line: str) -> List[str]:
    """拆分 Markdown 表格行，支持单元格内 \\| 转义与无首尾 | 的 GFM 表格"""
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith(r"\|"):
        s = s[:-1]
    raw_cells = re.split(r"(?<!\\)\|", s)
    return [_clean_md_inline_keep_bold(c.replace(r"\|", "|")) for c in raw_cells]


def _is_md_table_separator(line: str) -> bool:
    """仅当每格均匹配 ^[:\\-\\s]+$ 且至少含一个 '-' 时才视为表格分隔行"""
    s = line.strip()
    if "|" not in s:
        return False
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith(r"\|"):
        s = s[:-1]
    raw_cells = re.split(r"(?<!\\)\|", s)
    if not raw_cells:
        return False
    for c in raw_cells:
        cs = c.strip()
        if not cs or not re.match(r"^[:\-\s]+$", cs) or "-" not in cs:
            return False
    return True


def _has_unescaped_pipe(line: str) -> bool:
    return bool(re.search(r"(?<!\\)\|", line))


def _estimate_text_twips(text: str, size_half_pt: int = 32) -> int:
    """估算文本显示宽度（twips），用于发文机关署名相对成文日期居中计算"""
    char_w = size_half_pt * 10  # 32 half-pt = 16pt = 320 twips
    total = 0
    for ch in text:
        if ord(ch) > 127:
            total += char_w
        else:
            total += char_w // 2
    return total


def parse_draft_structure(
    raw_text: str,
    default_title: str = "",
    doc_number: str = "",
    issuing_org: str = "",
    mode: str = "report",
) -> Dict[str, Any]:
    """解析草稿结构，支持多级公文标题、嵌套列表、行内加粗、GFM 表格、未闭合代码块保护与落款防误判"""
    raw_lines = [ln.rstrip("\r\n") for ln in (raw_text or "").splitlines()]
    # 保留原始前导缩进，仅过滤全空白行
    non_empty = [ln for ln in raw_lines if ln.strip()]

    title = (default_title or "").strip()
    # 仅剥离已知系统遗留前缀，不误伤公文正当标题前缀如【内部参考】
    title = re.sub(r"^(\[材料\]_|\[来文\]_|【材料】_|【来文】_|\[材料\]|\[来文\])+", "", title).strip()
    title = re.sub(r"\.(md|txt|docx|wps|ofd|pdf)$", "", title, flags=re.IGNORECASE)

    blocks: List[Tuple[str, Any]] = []
    recipient = ""
    start_idx = 0

    if non_empty:
        first_stripped = non_empty[0].strip()
        if first_stripped.startswith("# "):
            title = _clean_md_inline(first_stripped[2:])
            start_idx = 1
        elif not title or _clean_md_inline(first_stripped) == title:
            title = _clean_md_inline(first_stripped)
            start_idx = 1

    def _append_regular_line(raw_ln: str, line_idx: int, total_len: int) -> None:
        nonlocal recipient
        stripped = raw_ln.strip()
        if not stripped:
            return
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", stripped):
            return

        # 支持 # ~ ###### 标题
        m_heading = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if m_heading:
            h_level = len(m_heading.group(1))
            h_text = _clean_md_inline_keep_bold(m_heading.group(2))
            if h_level <= 2:
                blocks.append(("h1", h_text))
            elif h_level == 3:
                blocks.append(("h2", h_text))
            else:
                blocks.append(("h3", h_text))
            return

        # 主送机关（支持超过 35 字的多部门联合主送，放宽至 120 字）
        plain_stripped = _clean_md_inline(stripped)
        if (
            not blocks
            and not recipient
            and not stripped.startswith((">", "-", "*", "•"))
            and re.match(r"^[\u4e00-\u9fa5A-Za-z0-9、，（）()·\s]{2,120}[：:]$", plain_stripped)
        ):
            recipient = plain_stripped
            return

        # 检测嵌套列表（保留前导缩进）
        m_list = re.match(r"^([ \t]*)(?:>\s*)*([-*•])\s+(.+)$", raw_ln)
        if m_list:
            lead_ws = m_list.group(1).expandtabs(2)
            indent_level = min(4, len(lead_ws) // 2)
            item_text = _clean_md_inline_keep_bold(m_list.group(3))
            if item_text:
                if indent_level > 0:
                    blocks.append(("list_item", (indent_level, item_text)))
                else:
                    blocks.append(("list_item", (0, item_text)))
            return

        # 剥离多级 > > 引用前缀
        ln_no_quote = re.sub(r"^(?:>\s*)+", "", stripped)
        ln_rich = _clean_md_inline_keep_bold(ln_no_quote)
        ln_plain = _clean_md_inline(ln_no_quote)
        if not ln_plain:
            return

        if re.match(r"^[一二三四五六七八九十]+、", ln_plain):
            blocks.append(("h1", ln_rich))
        elif re.match(r"^[（\(][一二三四五六七八九十]+[）\)]", ln_plain):
            blocks.append(("h2", ln_rich))
        elif re.match(r"^\d+[、\.]\s*", ln_plain):
            # 国标 GB/T 9704：三级标题默认不加粗，仅支持显式 **加粗**
            period_idx = ln_plain.find("。")
            if period_idx != -1 and period_idx <= 42 and period_idx < len(ln_plain) - 1:
                blocks.append(("h3", ln_rich))
            elif len(ln_plain) <= 42 and period_idx == -1:
                blocks.append(("h3", ln_rich))
            else:
                blocks.append(("body", ln_rich))
        elif (
            line_idx >= total_len - 2
            and re.match(
                r"^(?:\d{4}[年\.\-/]\d{1,2}[月\.\-/]\d{1,2}日?|[二〇○零一二三四五六七八九十]+年[一二三四五六七八九十]+月[一二三四五六七八九十]+日)$",
                ln_plain,
            )
        ):
            blocks.append(("sign_date", ln_plain))
        elif (
            line_idx >= total_len - 3
            and len(ln_plain) <= 24
            and not re.match(r"^(抄送|报送|印发|联系)", ln_plain)
            and "：" not in ln_plain
            and ":" not in ln_plain
            and re.search(r"(局|委|办公室|指挥部|中心|政府|部|专班|科|处)$", ln_plain)
        ):
            blocks.append(("sign_org", ln_plain))
        else:
            blocks.append(("body", ln_rich))

    idx = start_idx
    in_code_fence = False
    code_fence_buf: List[Tuple[int, str]] = []
    total_len = len(non_empty)

    while idx < total_len:
        raw_ln = non_empty[idx]
        ln = raw_ln.strip()

        if re.match(r"^`{3,}", ln):
            if not in_code_fence:
                in_code_fence = True
                code_fence_buf = []
            else:
                # 正常闭合的代码块过滤丢弃
                in_code_fence = False
                code_fence_buf = []
            idx += 1
            continue

        if in_code_fence:
            code_fence_buf.append((idx, raw_ln))
            idx += 1
            continue

        # 表格检测：支持首尾带 | 的表格，以及无首尾 | 的 GFM 表格（下一行为合法分隔行）
        is_bordered_table = ln.startswith("|") and ln.endswith("|") and len(ln) >= 3 and _has_unescaped_pipe(ln[1:-1])
        is_gfm_header = (
            not is_bordered_table
            and _has_unescaped_pipe(ln)
            and (idx + 1 < total_len)
            and _is_md_table_separator(non_empty[idx + 1])
        )
        if is_bordered_table or is_gfm_header:
            table_rows: List[List[str]] = []
            while idx < total_len:
                cur = non_empty[idx].strip()
                if not cur or re.match(r"^`{3,}", cur) or re.match(r"^#{1,6}\s+", cur):
                    break
                if is_bordered_table:
                    if not (cur.startswith("|") and _has_unescaped_pipe(cur)):
                        break
                else:
                    if not _has_unescaped_pipe(cur):
                        break
                if _is_md_table_separator(cur):
                    idx += 1
                    continue
                cells = _split_md_table_row(cur)
                if any(c.strip() for c in cells):
                    table_rows.append(cells)
                idx += 1
            if table_rows:
                blocks.append(("table", table_rows))
            continue

        _append_regular_line(raw_ln, idx, total_len)
        idx += 1

    # 未闭合代码块保护：将未闭合缓冲作为普通段落输出，不吞噬后续全文
    if in_code_fence and code_fence_buf:
        for buf_idx, buf_ln in code_fence_buf:
            clean_buf = _clean_md_inline_keep_bold(buf_ln.strip())
            if clean_buf:
                blocks.append(("body", clean_buf))

    # 仅红头模式下在缺落款日期时自动补齐（report 模式不强行篡改提纲/表格文档追加假落款）
    if mode == "redhead":
        has_sign_date = any(b[0] == "sign_date" for b in blocks)
        has_sign_org = any(b[0] == "sign_org" for b in blocks)
        if not has_sign_date:
            if issuing_org and not has_sign_org:
                blocks.append(("sign_org", issuing_org.strip()))
            now = datetime.now()
            blocks.append(("sign_date", f"{now.year}年{now.month}月{now.day}日"))

    return {
        "title": title or "公文材料",
        "doc_number": (doc_number or "").strip(),
        "issuing_org": (issuing_org or "").strip(),
        "recipient": recipient,
        "blocks": blocks,
    }


def _make_w_run(
    text: str,
    font_east_asia: str = "仿宋_GB2312",
    font_ascii: str = "Times New Roman",
    size_half_pt: int = 32,
    bold: bool = False,
    color_hex: str = "000000",
) -> str:
    esc_text = _sanitize_xml_text(text)
    esc_ea = _sanitize_xml_text(font_east_asia)
    esc_ascii = _sanitize_xml_text(font_ascii)
    esc_color = _sanitize_xml_text(color_hex)
    b_tag = "<w:b/><w:bCs/>" if bold else ""
    return (
        f"<w:r>"
        f"<w:rPr>"
        f'<w:rFonts w:ascii="{esc_ascii}" w:hAnsi="{esc_ascii}" w:eastAsia="{esc_ea}" w:cs="{esc_ea}"/>'
        f"{b_tag}"
        f'<w:color w:val="{esc_color}"/>'
        f'<w:sz w:val="{size_half_pt}"/>'
        f'<w:szCs w:val="{size_half_pt}"/>'
        f"</w:rPr>"
        f'<w:t xml:space="preserve">{esc_text}</w:t>'
        f"</w:r>"
    )


def _make_w_runs_rich(
    text: str,
    font_east_asia: str = "仿宋_GB2312",
    font_ascii: str = "Times New Roman",
    size_half_pt: int = 32,
    bold: bool = False,
    color_hex: str = "000000",
) -> str:
    """将含 **加粗** 的行内文本渲染为多个 <w:r>（显式加粗生成真实 <w:b/>）"""
    segments = _split_inline_bold_segments(text)
    if not segments:
        return _make_w_run(
            "",
            font_east_asia=font_east_asia,
            font_ascii=font_ascii,
            size_half_pt=size_half_pt,
            bold=bold,
            color_hex=color_hex,
        )
    return "".join(
        _make_w_run(
            seg,
            font_east_asia=font_east_asia,
            font_ascii=font_ascii,
            size_half_pt=size_half_pt,
            bold=(bold or seg_bold),
            color_hex=color_hex,
        )
        for seg, seg_bold in segments
    )


def _make_w_p(
    text: str,
    font_east_asia: str = "仿宋_GB2312",
    font_ascii: str = "Times New Roman",
    size_half_pt: int = 32,
    bold: bool = False,
    color_hex: str = "000000",
    align: str = "both",
    first_line_indent_twips: int = 640,
    first_line_chars: int = 200,
    left_indent_twips: int = 0,
    right_indent_twips: int = 0,
    line_twips: int = 560,
    space_before_twips: int = 0,
    space_after_twips: int = 0,
    bottom_red_border: bool = False,
    extra_runs: str = "",
    parse_inline_bold: bool = True,
) -> str:
    p_bdr = ""
    if bottom_red_border:
        p_bdr = (
            '<w:pBdr>'
            '<w:bottom w:val="single" w:sz="18" w:space="4" w:color="D40000"/>'
            '</w:pBdr>'
        )
    ind_attrs = []
    if left_indent_twips > 0:
        ind_attrs.append(f'w:left="{left_indent_twips}"')
    if first_line_indent_twips > 0:
        ind_attrs.append(f'w:firstLine="{first_line_indent_twips}"')
        if first_line_chars > 0:
            ind_attrs.append(f'w:firstLineChars="{first_line_chars}"')
    if right_indent_twips > 0:
        ind_attrs.append(f'w:right="{right_indent_twips}"')
    ind_xml = f'<w:ind {" ".join(ind_attrs)}/>' if ind_attrs else ""

    if parse_inline_bold:
        main_runs = _make_w_runs_rich(
            text,
            font_east_asia=font_east_asia,
            font_ascii=font_ascii,
            size_half_pt=size_half_pt,
            bold=bold,
            color_hex=color_hex,
        )
    else:
        main_runs = _make_w_run(
            text,
            font_east_asia=font_east_asia,
            font_ascii=font_ascii,
            size_half_pt=size_half_pt,
            bold=bold,
            color_hex=color_hex,
        )

    return (
        f"<w:p>"
        f"<w:pPr>"
        f"{p_bdr}"
        f'<w:spacing w:before="{space_before_twips}" w:after="{space_after_twips}" w:line="{line_twips}" w:lineRule="exact"/>'
        f"{ind_xml}"
        f'<w:jc w:val="{align}"/>'
        f"</w:pPr>"
        f"{main_runs}"
        f"{extra_runs}"
        f"</w:p>"
    )


def _make_w_tbl(
    rows: List[List[str]],
    font_header: str = "黑体",
    font_body: str = "仿宋_GB2312",
) -> str:
    """生成符合版心宽度的标准公文表格（支持 >17 列正宽度均匀分配，响应 font_overrides）"""
    if not rows:
        return ""
    col_count = max(1, max(len(r) for r in rows))
    total_w = 8840
    if col_count * 500 > total_w:
        col_width = max(120, total_w // col_count)
    else:
        col_width = max(500, total_w // col_count)
    col_widths = [col_width] * col_count
    rem = total_w - sum(col_widths)
    if rem > 0:
        col_widths[-1] += rem
    tbl_total_w = sum(col_widths)

    tbl_grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in col_widths) + "</w:tblGrid>"
    tbl_pr = (
        "<w:tblPr>"
        f'<w:tblW w:w="{tbl_total_w}" w:type="dxa"/>'
        '<w:tblLayout w:type="fixed"/>'
        '<w:jc w:val="center"/>'
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:left w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        '<w:bottom w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:right w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        '<w:insideH w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        '<w:insideV w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        "</w:tblBorders>"
        "</w:tblPr>"
    )
    tr_xmls = []
    for r_idx, row in enumerate(rows):
        is_header = r_idx == 0
        tr_pr = "<w:trPr><w:cantSplit/>" + ("<w:tblHeader/>" if is_header else "") + "</w:trPr>"
        tc_xmls = []
        for c_idx in range(col_count):
            cell_txt = row[c_idx] if c_idx < len(row) else ""
            p_cell = _make_w_p(
                cell_txt,
                font_east_asia=font_header if is_header else font_body,
                size_half_pt=24,
                bold=is_header,
                align="center" if is_header else "left",
                first_line_indent_twips=0,
                first_line_chars=0,
                line_twips=400,
                space_before_twips=40,
                space_after_twips=40,
            )
            tc_xmls.append(
                f'<w:tc><w:tcPr><w:tcW w:w="{col_widths[c_idx]}" w:type="dxa"/></w:tcPr>{p_cell}</w:tc>'
            )
        tr_xmls.append(f"<w:tr>{tr_pr}{''.join(tc_xmls)}</w:tr>")
    return f"<w:tbl>{tbl_pr}{tbl_grid}{''.join(tr_xmls)}</w:tbl>"


def _make_footer_xml(
    align: str = "right",
    indent_twips: int = 280,
    font_east_asia: str = "宋体",
    font_ascii: str = "Times New Roman",
) -> str:
    ind = f'<w:ind w:right="{indent_twips}"/>' if align == "right" else (f'<w:ind w:left="{indent_twips}"/>' if align == "left" else "")
    esc_ea = _sanitize_xml_text(font_east_asia)
    esc_ascii = _sanitize_xml_text(font_ascii)
    rpr = (
        f'<w:rPr>'
        f'<w:rFonts w:ascii="{esc_ascii}" w:hAnsi="{esc_ascii}" w:eastAsia="{esc_ea}" w:cs="{esc_ea}"/>'
        f'<w:sz w:val="28"/><w:szCs w:val="28"/>'
        f'</w:rPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:p>"
        f'<w:pPr>{ind}<w:jc w:val="{align}"/></w:pPr>'
        f'<w:r>{rpr}<w:t xml:space="preserve">— </w:t></w:r>'
        f'<w:r>{rpr}<w:fldChar w:fldCharType="begin"/></w:r>'
        f'<w:r>{rpr}<w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        f'<w:r>{rpr}<w:fldChar w:fldCharType="separate"/></w:r>'
        f'<w:r>{rpr}<w:t>1</w:t></w:r>'
        f'<w:r>{rpr}<w:fldChar w:fldCharType="end"/></w:r>'
        f'<w:r>{rpr}<w:t xml:space="preserve"> —</w:t></w:r>'
        "</w:p>"
        "</w:ftr>"
    )


def _build_font_table_xml(
    f_title: str,
    f_body: str,
    f_h1: str,
    f_h2: str,
    f_page: str,
) -> str:
    """构建响应 font_overrides 的 fontTable.xml，为麒麟/Linux 提供完整衬线/页码跨平台回退"""
    default_specs: List[Tuple[str, str, str, str]] = [
        ("仿宋_GB2312", "FangSong", "86", "roman"),
        ("楷体_GB2312", "KaiTi", "86", "script"),
        ("方正小标宋简体", "Noto Serif CJK SC", "86", "roman"),
        ("黑体", "SimHei", "86", "swiss"),
        ("宋体", "Noto Serif CJK SC", "86", "roman"),
        ("Times New Roman", "Liberation Serif", "00", "roman"),
    ]
    seen: Dict[str, Tuple[str, str, str]] = {
        name: (alt, charset, family) for name, alt, charset, family in default_specs
    }

    override_fallbacks: List[Tuple[str, str, str, str]] = [
        (f_title, "Noto Serif CJK SC", "86", "roman"),
        (f_body, "FangSong", "86", "roman"),
        (f_h1, "SimHei", "86", "swiss"),
        (f_h2, "KaiTi", "86", "script"),
        (f_page, "Noto Serif CJK SC", "86", "roman"),
    ]
    for fname, alt, charset, family in override_fallbacks:
        if fname and fname not in seen:
            seen[fname] = (alt, charset, family)

    entries = []
    for name, (alt, charset, family) in seen.items():
        esc_name = _sanitize_xml_text(name)
        esc_alt = _sanitize_xml_text(alt)
        entries.append(
            f'<w:font w:name="{esc_name}">'
            f'<w:altName w:val="{esc_alt}"/>'
            f'<w:charset w:val="{charset}"/>'
            f'<w:family w:val="{family}"/>'
            f'</w:font>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        + "".join(entries)
        + "</w:fonts>"
    )


def build_gb9704_docx(
    output_path: Path,
    raw_text: str,
    title: str = "",
    doc_number: str = "",
    issuing_org: str = "",
    mode: str = "report",
    xkl_gw_cli: str = "",
    font_overrides: Optional[Dict[str, str]] = None,
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if xkl_gw_cli:
        cli_path = Path(xkl_gw_cli).expanduser()
        if cli_path.exists():
            tmp_in = output_path.with_suffix(".tmp.md")
            try:
                tmp_in.write_text(raw_text, encoding="utf-8")
                cmd = [str(cli_path), "--input", str(tmp_in), "--output", str(output_path)]
                if mode:
                    cmd.extend(["--mode", mode])
                if title:
                    cmd.extend(["--title", title])
                if doc_number:
                    cmd.extend(["--doc-number", doc_number])
                if issuing_org:
                    cmd.extend(["--issuing-org", issuing_org])
                res = subprocess.run(cmd, **_subprocess_kwargs(timeout=20))
                if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                    return output_path
                # 优雅降级：兼容仅支持 --input/--output 的旧版 xkl_gw_cli
                res_basic = subprocess.run(
                    [str(cli_path), "--input", str(tmp_in), "--output", str(output_path)],
                    **_subprocess_kwargs(timeout=20),
                )
                if res_basic.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                    return output_path
            except Exception:
                pass
            finally:
                tmp_in.unlink(missing_ok=True)

    fmap = font_overrides or {}
    f_title = fmap.get("title", "方正小标宋简体")
    f_body = fmap.get("body", "仿宋_GB2312")
    f_h1 = fmap.get("h1", "黑体")
    f_h2 = fmap.get("h2", "楷体_GB2312")
    f_page = fmap.get("page", "宋体")

    effective_doc_num = doc_number if mode == "redhead" else ""
    parsed = parse_draft_structure(
        raw_text,
        default_title=title,
        doc_number=effective_doc_num,
        issuing_org=issuing_org,
        mode=mode,
    )
    p_nodes: List[str] = []

    if mode == "redhead":
        raw_org = parsed["issuing_org"].strip()
        raw_doc_num = parsed["doc_number"].strip()
        if raw_org:
            header_org = raw_org
            if not header_org.endswith(("文件", "函", "纪要", "通告")):
                header_org += "文件"
            # 发文机关标志字号按字数自适应：≤10 字 28pt(56)，11-16 字 24pt(48)，>16 字 20pt(40)
            org_len = len(header_org)
            if org_len <= 10:
                org_sz_half_pt = 56
            elif org_len <= 16:
                org_sz_half_pt = 48
            else:
                org_sz_half_pt = 40
            p_nodes.append(
                _make_w_p(
                    header_org,
                    font_east_asia=f_title,
                    size_half_pt=org_sz_half_pt,
                    bold=True,
                    color_hex="D40000",
                    align="center",
                    first_line_indent_twips=0,
                    first_line_chars=0,
                    line_twips=720,
                    space_after_twips=160,
                    bottom_red_border=(not raw_doc_num),
                    parse_inline_bold=False,
                )
            )
        # 绝不硬编码假文号，仅在明确提供 doc_number 时渲染发文字号与红线
        if raw_doc_num:
            p_nodes.append(
                _make_w_p(
                    raw_doc_num,
                    font_east_asia=f_body,
                    size_half_pt=32,
                    align="center",
                    first_line_indent_twips=0,
                    first_line_chars=0,
                    line_twips=560,
                    space_after_twips=80,
                    bottom_red_border=True,
                    parse_inline_bold=False,
                )
            )

    # 主标题：红头模式下按国标空二行（1120 twips）
    title_space_before = 1120 if mode == "redhead" else 240
    p_nodes.append(
        _make_w_p(
            parsed["title"],
            font_east_asia=f_title,
            size_half_pt=44,
            align="center",
            first_line_indent_twips=0,
            first_line_chars=0,
            line_twips=660,
            space_before_twips=title_space_before,
            space_after_twips=240,
            parse_inline_bold=False,
        )
    )

    if parsed["recipient"]:
        p_nodes.append(
            _make_w_p(
                parsed["recipient"],
                font_east_asia=f_body,
                size_half_pt=32,
                align="left",
                first_line_indent_twips=0,
                first_line_chars=0,
                line_twips=560,
                parse_inline_bold=False,
            )
        )

    # 预扫描成文日期，用于发文机关署名相对成文日期（右空四字=1280 twips）居中编排
    sign_date_text = next((val for bt, val in parsed["blocks"] if bt == "sign_date"), "")
    date_right_indent = 1280

    for b_type, b_val in parsed["blocks"]:
        if b_type == "table":
            p_nodes.append(_make_w_tbl(b_val, font_header=f_h1, font_body=f_body))
        elif b_type == "h1":
            p_nodes.append(
                _make_w_p(b_val, font_east_asia=f_h1, size_half_pt=32, first_line_indent_twips=640, line_twips=560)
            )
        elif b_type == "h2":
            p_nodes.append(
                _make_w_p(b_val, font_east_asia=f_h2, size_half_pt=32, first_line_indent_twips=640, line_twips=560)
            )
        elif b_type == "h3":
            # 国标三级标题默认不加粗（bold=False），行内显式 **加粗** 自动保留 <w:b/>
            p_nodes.append(
                _make_w_p(
                    b_val,
                    font_east_asia=f_body,
                    size_half_pt=32,
                    bold=False,
                    first_line_indent_twips=640,
                    line_twips=560,
                )
            )
        elif b_type == "list_item":
            level, item_text = b_val
            left_ind = level * 320
            p_nodes.append(
                _make_w_p(
                    item_text,
                    font_east_asia=f_body,
                    size_half_pt=32,
                    left_indent_twips=left_ind,
                    first_line_indent_twips=640,
                    first_line_chars=200 if left_ind == 0 else 0,
                    line_twips=560,
                )
            )
        elif b_type == "sign_org":
            if sign_date_text:
                date_w = _estimate_text_twips(sign_date_text, 32)
                org_w = _estimate_text_twips(b_val, 32)
                org_right_indent = max(0, int(date_right_indent + (date_w - org_w) / 2))
            else:
                org_right_indent = date_right_indent
            p_nodes.append(
                _make_w_p(
                    b_val,
                    font_east_asia=f_body,
                    size_half_pt=32,
                    align="right",
                    first_line_indent_twips=0,
                    first_line_chars=0,
                    right_indent_twips=org_right_indent,
                    space_before_twips=560,
                    line_twips=560,
                    parse_inline_bold=False,
                )
            )
        elif b_type == "sign_date":
            p_nodes.append(
                _make_w_p(
                    b_val,
                    font_east_asia=f_body,
                    size_half_pt=32,
                    align="right",
                    first_line_indent_twips=0,
                    first_line_chars=0,
                    right_indent_twips=date_right_indent,
                    line_twips=560,
                    parse_inline_bold=False,
                )
            )
        else:
            p_nodes.append(
                _make_w_p(b_val, font_east_asia=f_body, size_half_pt=32, first_line_indent_twips=640, line_twips=560)
            )

    # GB/T 9704—2012 7.5：单页码居右空一字，双页码居左空一字
    sect_pr = (
        '<w:sectPr>'
        '<w:footerReference w:type="default" r:id="rId2"/>'
        '<w:footerReference w:type="even" r:id="rId5"/>'
        '<w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="2098" w:right="1474" w:bottom="1984" w:left="1587" w:header="851" w:footer="1417" w:gutter="0"/>'
        '<w:docGrid w:type="linesAndChars" w:linePitch="560" w:charSpace="0"/>'
        '</w:sectPr>'
    )

    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        "<w:body>"
        + "".join(p_nodes)
        + sect_pr
        + "</w:body></w:document>"
    )

    footer_odd_xml = _make_footer_xml(align="right", indent_twips=280, font_east_asia=f_page)
    footer_even_xml = _make_footer_xml(align="left", indent_twips=280, font_east_asia=f_page)

    settings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:evenAndOddHeaders/>'
        '<w:displayHorizontalDrawingGridEvery w:val="0"/>'
        '<w:displayVerticalDrawingGridEvery w:val="2"/>'
        '<w:characterSpacingControl w:val="compressPunctuation"/>'
        '</w:settings>'
    )

    font_table_xml = _build_font_table_xml(f_title, f_body, f_h1, f_h2, f_page)

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>'
        '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
        '<Override PartName="/word/footer2.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
        '</Types>'
    )

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>'
    )

    word_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable" Target="fontTable.xml"/>'
        '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer" Target="footer2.xml"/>'
        '</Relationships>'
    )

    esc_f_body = _sanitize_xml_text(f_body)
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults>'
        '<w:rPrDefault><w:rPr>'
        f'<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="{esc_f_body}" w:cs="{esc_f_body}"/>'
        '<w:sz w:val="32"/><w:szCs w:val="32"/>'
        '</w:rPr></w:rPrDefault>'
        '</w:docDefaults>'
        '</w:styles>'
    )

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml.encode("utf-8"))
        zf.writestr("_rels/.rels", rels_xml.encode("utf-8"))
        zf.writestr("word/_rels/document.xml.rels", word_rels_xml.encode("utf-8"))
        zf.writestr("word/styles.xml", styles_xml.encode("utf-8"))
        zf.writestr("word/settings.xml", settings_xml.encode("utf-8"))
        zf.writestr("word/fontTable.xml", font_table_xml.encode("utf-8"))
        zf.writestr("word/footer1.xml", footer_odd_xml.encode("utf-8"))
        zf.writestr("word/footer2.xml", footer_even_xml.encode("utf-8"))
        zf.writestr("word/document.xml", document_xml.encode("utf-8"))

    return output_path
