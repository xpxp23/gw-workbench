#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GB/T 9704—2012 党政机关公文标准排版与 xkl-gw 联动引擎 (gw_formatter.py v3.0.0)
专为银河麒麟 Kylin V10 (海光 3350) + WPS Office Linux 打造：
1. 支持直接调用本机已安装的 `xkl-gw` 公文排版助手 CLI 脚本（若配置了路径或环境变量）
2. 内置零依赖纯 Python OpenXML (.docx) 国标公文生成器：
   - 严格遵循 GB/T 9704—2012 页面边距：天头 37mm、地脚 35mm、订口(左) 28mm、切口(右) 26mm
   - 支持【正式红头公文模式 (redhead)】与【综合汇报/方案材料模式 (report)】
   - 修复 v2.0.2 中因所属事项含来文字号导致 report 汇报模式误触发红头的 Bug
   - 自动识别 Markdown / 纯文本中的公文标题（二号方正小标宋简体）、主送机关、
     一级标题（一、-> 三号黑体）、二级标题（（一）-> 三号楷体_GB2312）、
     三级标题（1. -> 三号仿宋_GB2312加粗）、Markdown 表格（自动转为标准公文内嵌表格 <w:tbl>）
     及正文段落（三号仿宋_GB2312，首行缩进2字符，固定行距28磅）
   - 内置国标页脚 `word/footer1.xml`（四号宋体“— 1 —”自动页码域）与 `word/settings.xml` 版心网格对齐
"""

import html
import re
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Tuple


def _clean_md_inline(text: str) -> str:
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return s.strip()


def parse_draft_structure(
    raw_text: str,
    default_title: str = "",
    doc_number: str = "",
    issuing_org: str = "",
) -> Dict[str, Any]:
    """解析草稿结构，支持多级公文标题、正文段落、落款及 Markdown 表格自动转换"""
    lines = [ln.strip() for ln in (raw_text or "").splitlines()]
    non_empty = [ln for ln in lines if ln]

    title = default_title.strip()
    title = re.sub(r"^(\[材料\]_|\[来文\]_|【[^】]+】)+", "", title)
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
    while idx < len(non_empty):
        ln = non_empty[idx]
        if re.match(r"^(`{3,}|-{3,}|\*{3,})$", ln):
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

        ln_clean = re.sub(r"^[\-\*•]\s+", "", ln)
        ln_clean = _clean_md_inline(ln_clean)
        if not ln_clean:
            idx += 1
            continue

        if re.match(r"^[一二三四五六七八九十]+、", ln_clean):
            blocks.append(("h1", ln_clean))
        elif re.match(r"^[（\(][一二三四五六七八九十]+[）\)]", ln_clean):
            blocks.append(("h2", ln_clean))
        elif re.match(r"^\d+[、\.]\s*", ln_clean):
            blocks.append(("h3", ln_clean))
        elif idx >= len(non_empty) - 2 and re.match(r"^\d{4}年\d{1,2}月\d{1,2}日$", ln_clean):
            blocks.append(("sign_date", ln_clean))
        elif idx >= len(non_empty) - 3 and len(ln_clean) <= 22 and re.search(r"(局|委|办公室|指挥部|中心|政府|部|专班|科|处)$", ln_clean):
            blocks.append(("sign_org", ln_clean))
        else:
            blocks.append(("body", ln_clean))
        idx += 1

    has_sign_date = any(b[0] == "sign_date" for b in blocks)
    if not has_sign_date:
        if issuing_org:
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


def _make_w_p(
    text: str,
    font_east_asia: str = "仿宋_GB2312",
    font_ascii: str = "Times New Roman",
    size_half_pt: int = 32,
    bold: bool = False,
    color_hex: str = "000000",
    align: str = "both",
    first_line_indent_twips: int = 640,
    right_indent_twips: int = 0,
    line_twips: int = 560,
    space_before_twips: int = 0,
    space_after_twips: int = 0,
    bottom_red_border: bool = False,
) -> str:
    esc_text = html.escape(text)
    b_tag = "<w:b/><w:bCs/>" if bold else ""
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
    if right_indent_twips > 0:
        ind_attrs.append(f'w:right="{right_indent_twips}"')
    ind_xml = f'<w:ind {" ".join(ind_attrs)}/>' if ind_attrs else ""

    return (
        f"<w:p>"
        f"<w:pPr>"
        f"{p_bdr}"
        f'<w:spacing w:before="{space_before_twips}" w:after="{space_after_twips}" w:line="{line_twips}" w:lineRule="exact"/>'
        f"{ind_xml}"
        f'<w:jc w:val="{align}"/>'
        f"</w:pPr>"
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
        f"</w:p>"
    )


def _make_w_tbl(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    col_count = max(len(r) for r in rows)
    col_width = max(1200, 8800 // max(1, col_count))

    tbl_pr = (
        "<w:tblPr>"
        '<w:tblW w:w="8840" w:type="dxa"/>'
        '<w:jc w:val="center"/>'
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:left w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        '<w:bottom w:val="single" w:sz="8" w:space="0" w:color="000000"/>'
        '<w:right w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        '<w:insideH w:val="single" w:sz="6" w:space="0" w:color="000000"/>'
        '<w:insideV w:val="single" w:space="0" w:sz="6" w:color="000000"/>'
        "</w:tblBorders>"
        "</w:tblPr>"
    )
    tr_xmls = []
    for r_idx, row in enumerate(rows):
        is_header = r_idx == 0
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
                line_twips=400,
                space_before_twips=40,
                space_after_twips=40,
            )
            tc_xmls.append(
                f"<w:tc><w:tcPr><w:tcW w:w=\"{col_width}\" w:type=\"dxa\"/></w:tcPr>{p_cell}</w:tc>"
            )
        tr_xmls.append(f"<w:tr>{''.join(tc_xmls)}</w:tr>")
    return f"<w:tbl>{tbl_pr}{''.join(tr_xmls)}</w:tbl>"


def build_gb9704_docx(
    output_path: Path,
    raw_text: str,
    title: str = "",
    doc_number: str = "",
    issuing_org: str = "",
    mode: str = "report",
    xkl_gw_cli: str = "",
) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if xkl_gw_cli:
        cli_path = Path(xkl_gw_cli).expanduser()
        if cli_path.exists():
            try:
                tmp_in = output_path.with_suffix(".tmp.md")
                tmp_in.write_text(raw_text, encoding="utf-8")
                res = subprocess.run(
                    [str(cli_path), "--input", str(tmp_in), "--output", str(output_path)],
                    capture_output=True,
                    timeout=20,
                )
                tmp_in.unlink(missing_ok=True)
                if res.returncode == 0 and output_path.exists() and output_path.stat().st_size > 0:
                    return output_path
            except Exception:
                pass

    # 修复 BUG-02：仅当明确指定 redhead 模式时才生成红头与发文字号，避免汇报模式被上级来文字号污染
    effective_doc_num = doc_number if mode == "redhead" else ""
    parsed = parse_draft_structure(raw_text, default_title=title, doc_number=effective_doc_num, issuing_org=issuing_org)
    p_nodes: List[str] = []

    if mode == "redhead":
        header_org = (parsed["issuing_org"] or "工作专班")
        if not header_org.endswith("文件"):
            header_org += "文件"
        p_nodes.append(
            _make_w_p(
                header_org,
                font_east_asia="方正小标宋简体",
                size_half_pt=56,
                bold=True,
                color_hex="D40000",
                align="center",
                first_line_indent_twips=0,
                line_twips=720,
                space_after_twips=160,
            )
        )
        p_nodes.append(
            _make_w_p(
                parsed["doc_number"] or f"应急发〔{datetime.now().year}〕1号",
                font_east_asia="仿宋_GB2312",
                size_half_pt=32,
                align="center",
                first_line_indent_twips=0,
                line_twips=560,
                space_after_twips=80,
                bottom_red_border=True,
            )
        )

    p_nodes.append(
        _make_w_p(
            parsed["title"],
            font_east_asia="方正小标宋简体",
            size_half_pt=44,
            align="center",
            first_line_indent_twips=0,
            line_twips=660,
            space_before_twips=240,
            space_after_twips=240,
        )
    )

    if parsed["recipient"]:
        p_nodes.append(
            _make_w_p(
                parsed["recipient"],
                font_east_asia="仿宋_GB2312",
                size_half_pt=32,
                align="left",
                first_line_indent_twips=0,
                line_twips=560,
            )
        )

    for b_type, b_val in parsed["blocks"]:
        if b_type == "table":
            p_nodes.append(_make_w_tbl(b_val))
        elif b_type == "h1":
            p_nodes.append(_make_w_p(b_val, font_east_asia="黑体", size_half_pt=32, first_line_indent_twips=640, line_twips=560))
        elif b_type == "h2":
            p_nodes.append(_make_w_p(b_val, font_east_asia="楷体_GB2312", size_half_pt=32, first_line_indent_twips=640, line_twips=560))
        elif b_type == "h3":
            p_nodes.append(_make_w_p(b_val, font_east_asia="仿宋_GB2312", size_half_pt=32, bold=True, first_line_indent_twips=640, line_twips=560))
        elif b_type == "sign_org":
            p_nodes.append(_make_w_p(b_val, font_east_asia="仿宋_GB2312", size_half_pt=32, align="right", first_line_indent_twips=0, right_indent_twips=1280, space_before_twips=560, line_twips=560))
        elif b_type == "sign_date":
            p_nodes.append(_make_w_p(b_val, font_east_asia="仿宋_GB2312", size_half_pt=32, align="right", first_line_indent_twips=0, right_indent_twips=1280, line_twips=560))
        else:
            p_nodes.append(_make_w_p(b_val, font_east_asia="仿宋_GB2312", size_half_pt=32, first_line_indent_twips=640, line_twips=560))

    sect_pr = (
        '<w:sectPr>'
        '<w:footerReference w:type="default" r:id="rId2"/>'
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

    footer1_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:p>'
        '<w:pPr><w:jc w:val="center"/></w:pPr>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:t xml:space="preserve">— </w:t></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:fldChar w:fldCharType="begin"/></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:fldChar w:fldCharType="separate"/></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:t>1</w:t></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:fldChar w:fldCharType="end"/></w:r>'
        '<w:r><w:rPr><w:rFonts w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/><w:sz w:val="28"/><w:szCs w:val="28"/></w:rPr>'
        '<w:t xml:space="preserve"> —</w:t></w:r>'
        '</w:p>'
        '</w:ftr>'
    )

    settings_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:settings xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:displayHorizontalDrawingGridEvery w:val="0"/>'
        '<w:displayVerticalDrawingGridEvery w:val="2"/>'
        '<w:characterSpacingControl w:val="compressPunctuation"/>'
        '</w:settings>'
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>'
        '<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>'
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
        '</Relationships>'
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults>'
        '<w:rPrDefault><w:rPr>'
        '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="仿宋_GB2312" w:cs="仿宋_GB2312"/>'
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
        zf.writestr("word/footer1.xml", footer1_xml.encode("utf-8"))
        zf.writestr("word/document.xml", document_xml.encode("utf-8"))

    return output_path
