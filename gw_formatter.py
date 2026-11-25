#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GB/T 9704—2012 党政机关公文标准排版与 xkl-gw 联动引擎 (gw_formatter.py v3.1.0)
修复与增强：
1. 补全 word/fontTable.xml 字体回退声明，解决银河麒麟 Kylin V10 / WPS Linux 缺方正字体时错退为黑体问题
2. 修复三级标题 h3 正则误伤整段正文加粗的 Bug：仅对句号前小标题（或 ≤40 字无句号行）加粗
3. 修复 Markdown 清洗不彻底（支持 ####、> 引用、[链接](url)、~~删除线~~ 与代码块过滤）及双署名重复追加
4. 表格补全 <w:tblGrid>、<w:tblHeader/>、<w:cantSplit/>，严格约束总宽 ≤8840 twips 不撑破版心
5. 段落首行缩进补齐 w:firstLineChars="200"，支持奇偶页左右空一字国标页码（evenAndOddHeaders）
6. 调用外部 xkl_gw_cli 使用 try...finally 保证临时文件不泄漏
"""

import html
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def _clean_md_inline(text: str) -> str:
    s = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s.strip()


def parse_draft_structure(
    raw_text: str,
    default_title: str = "",
    doc_number: str = "",
    issuing_org: str = "",
) -> Dict[str, Any]:
    """解析草稿结构，支持多级公文标题、句号前小标题拆分、落款防重及 Markdown 表格自动转换"""
    lines = [ln.strip() for ln in (raw_text or "").splitlines()]
    non_empty = [ln for ln in lines if ln]

    title = (default_title or "").strip()
    # 仅剥离已知系统遗留前缀，不误伤公文正当标题前缀如【内部参考】
    title = re.sub(r"^(\[材料\]_|\[来文\]_|【材料】_|【来文】_|\[材料\]|\[来文\])+", "", title).strip()
    title = re.sub(r"\.(md|txt|docx|wps|ofd|pdf)$", "", title, flags=re.IGNORECASE)

    blocks: List[Tuple[str, Any]] = []
    recipient = ""
    start_idx = 0

    if non_empty:
        first_line = non_empty[0]
        if first_line.startswith("# "):
            title = _clean_md_inline(first_line[2:])
            start_idx = 1
        elif not title or first_line == title:
            title = _clean_md_inline(first_line)
            start_idx = 1

    idx = start_idx
    in_code_fence = False
    while idx < len(non_empty):
        ln = non_empty[idx]
        if re.match(r"^`{3,}", ln):
            in_code_fence = not in_code_fence
            idx += 1
            continue
        if in_code_fence:
            idx += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", ln):
            idx += 1
            continue

        if ln.startswith("|") and ln.endswith("|") and ln.count("|") >= 2:
            table_rows: List[List[str]] = []
            while idx < len(non_empty) and non_empty[idx].startswith("|") and non_empty[idx].endswith("|"):
                row_str = non_empty[idx].strip("|")
                cells = [_clean_md_inline(c) for c in row_str.split("|")]
                if not all(re.match(r"^[:\-\s]+$", c) for c in cells if c):
                    table_rows.append(cells)
                idx += 1
            if table_rows:
                blocks.append(("table", table_rows))
            continue

        if not blocks and not recipient and re.match(r"^[\u4e00-\u9fa50-9、，（）()]{2,35}[：:]$", ln):
            recipient = _clean_md_inline(ln)
            idx += 1
            continue

        if ln.startswith("## "):
            blocks.append(("h1", _clean_md_inline(ln[3:])))
            idx += 1
            continue
        elif ln.startswith("### "):
            blocks.append(("h2", _clean_md_inline(ln[4:])))
            idx += 1
            continue
        elif ln.startswith("#### "):
            blocks.append(("h3", _clean_md_inline(ln[5:])))
            idx += 1
            continue

        ln_clean = re.sub(r"^>\s*", "", ln)
        ln_clean = re.sub(r"^[\-\*•]\s+", "", ln_clean)
        ln_clean = _clean_md_inline(ln_clean)
        if not ln_clean:
            idx += 1
            continue

        if re.match(r"^[一二三四五六七八九十]+、", ln_clean):
            blocks.append(("h1", ln_clean))
        elif re.match(r"^[（\(][一二三四五六七八九十]+[）\)]", ln_clean):
            blocks.append(("h2", ln_clean))
        elif re.match(r"^\d+[、\.]\s*", ln_clean):
            # 国标 GB/T 9704：若句号前有短小标题，仅加粗小标题，后接常规仿宋正文
            period_idx = ln_clean.find("。")
            if period_idx != -1 and period_idx <= 42 and period_idx < len(ln_clean) - 1:
                blocks.append(("h3_lead", (ln_clean[: period_idx + 1], ln_clean[period_idx + 1 :])))
            elif len(ln_clean) <= 42 and period_idx == -1:
                blocks.append(("h3", ln_clean))
            else:
                blocks.append(("body", ln_clean))
        elif idx >= len(non_empty) - 2 and re.match(r"^(?:\d{4}[年\.\-/]\d{1,2}[月\.\-/]\d{1,2}日?|[二〇○零一二三四五六七八九十]+年[一二三四五六七八九十]+月[一二三四五六七八九十]+日)$", ln_clean):
            blocks.append(("sign_date", ln_clean))
        elif idx >= len(non_empty) - 3 and len(ln_clean) <= 24 and re.search(r"(局|委|办公室|指挥部|中心|政府|部|专班|科|处)$", ln_clean):
            blocks.append(("sign_org", ln_clean))
        else:
            blocks.append(("body", ln_clean))
        idx += 1

    has_sign_date = any(b[0] == "sign_date" for b in blocks)
    has_sign_org = any(b[0] == "sign_org" for b in blocks)
    if not has_sign_date:
        if issuing_org and not has_sign_org:
            blocks.append(("sign_org", issuing_org))
        now = datetime.now()
        blocks.append(("sign_date", f"{now.year}年{now.month}月{now.day}日"))

    return {
        "title": title or "公文材料",
        "doc_number": doc_number or "",
        "issuing_org": issuing_org or "",
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
    esc_text = html.escape(text)
    b_tag = "<w:b/><w:bCs/>" if bold else ""
    return (
        f"<w:r>"
        f"<w:rPr>"
        f'<w:rFonts w:ascii="{font_ascii}" w:hAnsi="{font_ascii}" w:eastAsia="{font_east_asia}" w:cs="{font_east_asia}"/>'
        f"{b_tag}"
        f'<w:color w:val="{color_hex}"/>'
        f'<w:sz w:val="{size_half_pt}"/>'
        f'<w:szCs w:val="{size_half_pt}"/>'
        f"</w:rPr>"
        f'<w:t xml:space="preserve">{esc_text}</w:t>'
        f"</w:r>"
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
    right_indent_twips: int = 0,
    line_twips: int = 560,
    space_before_twips: int = 0,
    space_after_twips: int = 0,
    bottom_red_border: bool = False,
    extra_runs: str = "",
) -> str:
    p_bdr = ""
    if bottom_red_border:
        p_bdr = (
            '<w:pBdr>'
            '<w:bottom w:val="single" w:sz="18" w:space="4" w:color="D40000"/>'
            '</w:pBdr>'
        )
    ind_attrs = []
    if first_line_indent_twips > 0:
        ind_attrs.append(f'w:firstLine="{first_line_indent_twips}"')
        if first_line_chars > 0:
            ind_attrs.append(f'w:firstLineChars="{first_line_chars}"')
    if right_indent_twips > 0:
        ind_attrs.append(f'w:right="{right_indent_twips}"')
    ind_xml = f'<w:ind {" ".join(ind_attrs)}/>' if ind_attrs else ""

    main_run = _make_w_run(
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
        f"{main_run}"
        f"{extra_runs}"
        f"</w:p>"
    )


def _make_w_tbl(rows: List[List[str]]) -> str:
    """生成符合版心宽度的标准公文表格（补全 <w:tblGrid>、表头跨页重复与防断行）"""
    if not rows:
        return ""
    col_count = max(1, max(len(r) for r in rows))
    total_w = 8840
    col_width = max(500, total_w // col_count)
    # 末列吸收整除余数确保精确等于版心
    col_widths = [col_width] * col_count
    col_widths[-1] += total_w - sum(col_widths)

    tbl_grid = "<w:tblGrid>" + "".join(f'<w:gridCol w:w="{w}"/>' for w in col_widths) + "</w:tblGrid>"
    tbl_pr = (
        "<w:tblPr>"
        f'<w:tblW w:w="{total_w}" w:type="dxa"/>'
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
                font_east_asia="黑体" if is_header else "仿宋_GB2312",
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


def _make_footer_xml(align: str = "right", indent_twips: int = 280) -> str:
    ind = f'<w:ind w:right="{indent_twips}"/>' if align == "right" else (f'<w:ind w:left="{indent_twips}"/>' if align == "left" else "")
    rpr = '<w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
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
                res = subprocess.run(
                    [str(cli_path), "--input", str(tmp_in), "--output", str(output_path)],
                    capture_output=True,
                    timeout=20,
                )
                if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
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

    effective_doc_num = doc_number if mode == "redhead" else ""
    parsed = parse_draft_structure(raw_text, default_title=title, doc_number=effective_doc_num, issuing_org=issuing_org)
    p_nodes: List[str] = []

    if mode == "redhead":
        header_org = (parsed["issuing_org"] or "工作专班").strip()
        if not header_org.endswith(("文件", "函", "纪要", "通告")):
            header_org += "文件"
        p_nodes.append(
            _make_w_p(
                header_org,
                font_east_asia=f_title,
                size_half_pt=56,
                bold=True,
                color_hex="D40000",
                align="center",
                first_line_indent_twips=0,
                first_line_chars=0,
                line_twips=720,
                space_after_twips=160,
            )
        )
        p_nodes.append(
            _make_w_p(
                parsed["doc_number"] or f"应急发〔{datetime.now().year}〕1号",
                font_east_asia=f_body,
                size_half_pt=32,
                align="center",
                first_line_indent_twips=0,
                first_line_chars=0,
                line_twips=560,
                space_after_twips=80,
                bottom_red_border=True,
            )
        )

    p_nodes.append(
        _make_w_p(
            parsed["title"],
            font_east_asia=f_title,
            size_half_pt=44,
            align="center",
            first_line_indent_twips=0,
            first_line_chars=0,
            line_twips=660,
            space_before_twips=240,
            space_after_twips=240,
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
            )
        )

    for b_type, b_val in parsed["blocks"]:
        if b_type == "table":
            p_nodes.append(_make_w_tbl(b_val))
        elif b_type == "h1":
            p_nodes.append(_make_w_p(b_val, font_east_asia=f_h1, size_half_pt=32, first_line_indent_twips=640, line_twips=560))
        elif b_type == "h2":
            p_nodes.append(_make_w_p(b_val, font_east_asia=f_h2, size_half_pt=32, first_line_indent_twips=640, line_twips=560))
        elif b_type == "h3":
            p_nodes.append(_make_w_p(b_val, font_east_asia=f_body, size_half_pt=32, bold=True, first_line_indent_twips=640, line_twips=560))
        elif b_type == "h3_lead":
            lead, rest = b_val
            rest_run = _make_w_run(rest, font_east_asia=f_body, size_half_pt=32, bold=False)
            p_nodes.append(_make_w_p(lead, font_east_asia=f_body, size_half_pt=32, bold=True, first_line_indent_twips=640, line_twips=560, extra_runs=rest_run))
        elif b_type == "sign_org":
            p_nodes.append(_make_w_p(b_val, font_east_asia=f_body, size_half_pt=32, align="right", first_line_indent_twips=0, first_line_chars=0, right_indent_twips=1280, space_before_twips=560, line_twips=560))
        elif b_type == "sign_date":
            p_nodes.append(_make_w_p(b_val, font_east_asia=f_body, size_half_pt=32, align="right", first_line_indent_twips=0, first_line_chars=0, right_indent_twips=1280, line_twips=560))
        else:
            p_nodes.append(_make_w_p(b_val, font_east_asia=f_body, size_half_pt=32, first_line_indent_twips=640, line_twips=560))

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

    footer_odd_xml = _make_footer_xml(align="right", indent_twips=280)
    footer_even_xml = _make_footer_xml(align="left", indent_twips=280)

    settings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:evenAndOddHeaders/>'
        '<w:displayHorizontalDrawingGridEvery w:val="0"/>'
        '<w:displayVerticalDrawingGridEvery w:val="2"/>'
        '<w:characterSpacingControl w:val="compressPunctuation"/>'
        '</w:settings>'
    )

    # 补全 fontTable.xml，为麒麟/Linux WPS 提供优雅的衬线字体回退声明
    font_table_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:fonts xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:font w:name="仿宋_GB2312"><w:altName w:val="FangSong"/><w:charset w:val="86"/><w:family w:val="roman"/></w:font>'
        '<w:font w:name="楷体_GB2312"><w:altName w:val="KaiTi"/><w:charset w:val="86"/><w:family w:val="script"/></w:font>'
        '<w:font w:name="方正小标宋简体"><w:altName w:val="SimSun"/><w:charset w:val="86"/><w:family w:val="roman"/></w:font>'
        '<w:font w:name="黑体"><w:altName w:val="SimHei"/><w:charset w:val="86"/><w:family w:val="swiss"/></w:font>'
        '<w:font w:name="宋体"><w:altName w:val="Noto Serif CJK SC"/><w:charset w:val="86"/><w:family w:val="roman"/></w:font>'
        '<w:font w:name="Times New Roman"><w:altName w:val="Liberation Serif"/><w:charset w:val="00"/><w:family w:val="roman"/></w:font>'
        '</w:fonts>'
    )

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

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults>'
        '<w:rPrDefault><w:rPr>'
        f'<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="{f_body}" w:cs="{f_body}"/>'
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
