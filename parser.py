#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多格式公文、多媒体资料与常备知识库提取器 (parser.py v3.1.0 - 适配银河麒麟 Kylin V10 + Windows + macOS)
修复与增强：
- .ofd: 按 OFD.xml -> DocRoot -> Document.xml -> Pages/Page/@BaseLoc 标准链路定位页序，按 Y/X 坐标分行，保留 14:30 等冒号格式
- .docx: 递归解析 <w:sdt> 内容控件与 <w:txbxContent> 文本框，跳过 <w:del> 修订删除块与 <mc:Fallback>
- .pptx: 同步提取幻灯片正文与 ppt/notesSlides/notesSlide*.xml 演讲者备注
- .xlsx: 解析 xl/styles.xml 还原 Excel 日期序列号与布尔值，超限截断附加提示
- .pdf: 限制同步提取最大 50 页；前 3 页无文本时跳过 pdfplumber 避免扫描件双倍卡顿
- .doc/.wps: 限制最大二进制读取体积 10MB，优先探测 soffice/libreoffice 无头转换，Windows 加 CREATE_NO_WINDOW
- 图片 OCR: 仅读前 64KB 头部解析尺寸，修复 JPEG 标记遍历；启动缓存 tesseract --list-langs，杜绝 3x8s 重试与中文误降级为英文乱码
"""

import csv
import io
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("gw_workbench.parser")

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".wma", ".amr"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi"}

MAX_PDF_SYNC_PAGES = 50
MAX_LEGACY_BIN_BYTES = 10 * 1024 * 1024  # 10 MB

_TESSERACT_LANGS_CACHE: Optional[Set[str]] = None
_SOFFICE_BIN_CACHE: Optional[str] = None


def _subprocess_kwargs(timeout: int = 15) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kw


def read_text_with_fallback_encodings(raw_bytes: bytes) -> str:
    """自适应常见中文办公编码（UTF-8 / UTF-8-SIG / GB18030 / UTF-16）"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "utf-16le"):
        try:
            text = raw_bytes.decode(enc)
            if "\x00" not in text or enc.startswith("utf-16"):
                return text.replace("\x00", "")
        except Exception:
            continue
    return raw_bytes.decode("utf-8", errors="ignore").replace("\x00", "")


def _local_tag(tag_str: str) -> str:
    return tag_str.split("}")[-1] if "}" in tag_str else tag_str


def _extract_w_p_text(p_elem: ET.Element) -> str:
    """提取 Word 段落文本，跳过 <w:del> 删除修订与 <mc:Fallback> 冗余回退节点"""
    p_texts: List[str] = []

    def _walk(node: ET.Element) -> None:
        tag = _local_tag(node.tag)
        if tag in ("del", "delText", "Fallback", "instrText", "fldData"):
            return
        if tag == "t" and node.text:
            p_texts.append(node.text)
            return
        if tag in ("br", "cr"):
            p_texts.append("\n")
            return
        if tag == "tab":
            p_texts.append("\t")
            return
        for ch in node:
            _walk(ch)

    _walk(p_elem)
    return "".join(p_texts).strip()


def _walk_docx_container(
    container: ET.Element,
    is_main_doc: bool,
    text_chunks: List[str],
    blocks: List[Dict[str, Any]],
) -> None:
    """递归遍历 body / sdtContent / txbxContent，完整提取段落、表格与文本框"""
    for child in container:
        ctag = _local_tag(child.tag)
        if ctag in ("del", "Fallback"):
            continue
        if ctag == "p":
            line = _extract_w_p_text(child)
            if line:
                text_chunks.append(line)
                if is_main_doc:
                    btype = "p"
                    if re.match(r"^[一二三四五六七八九十]+、", line):
                        btype = "h1"
                    elif re.match(r"^[（\(][一二三四五六七八九十]+[）\)]", line):
                        btype = "h2"
                    elif re.match(r"^\d+[、\.]\s*", line) and len(line) < 60:
                        btype = "h3"
                    blocks.append({"type": btype, "text": line})
            # 检查段落内嵌文本框 <w:txbxContent>
            for sub in child.iter():
                if _local_tag(sub.tag) == "txbxContent":
                    _walk_docx_container(sub, is_main_doc, text_chunks, blocks)
        elif ctag == "tbl":
            rows_data: List[List[str]] = []
            for tr in child:
                if _local_tag(tr.tag) != "tr":
                    continue
                row_cells: List[str] = []
                for tc in tr:
                    if _local_tag(tc.tag) != "tc":
                        continue
                    cell_lines: List[str] = []
                    for sub_p in tc.iter():
                        if _local_tag(sub_p.tag) == "p":
                            pt = _extract_w_p_text(sub_p)
                            if pt:
                                cell_lines.append(pt)
                    row_cells.append("\n".join(cell_lines))
                if any(c.strip() for c in row_cells):
                    rows_data.append(row_cells)
                    text_chunks.append(" | ".join(c.replace("\n", " ") for c in row_cells if c.strip()))
            if rows_data and is_main_doc:
                blocks.append({"type": "table", "rows": rows_data})
        elif ctag in ("sdt", "sdtContent", "txbxContent", "customXml", "ins"):
            _walk_docx_container(child, is_main_doc, text_chunks, blocks)


def extract_docx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """顺序提取 .docx 中的段落、内容控件、文本框与内嵌表格（纯 Python 标准库实现）"""
    blocks: List[Dict[str, Any]] = []
    text_chunks: List[str] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            target_names = [
                n for n in zf.namelist()
                if n.startswith("word/") and n.endswith(".xml")
                and ("document" in n or "header" in n or "footer" in n)
            ]
            target_names.sort(key=lambda x: (0 if x == "word/document.xml" else 1, x))
            for name in target_names:
                raw_xml = zf.read(name)
                root = ET.fromstring(raw_xml)
                is_main_doc = name == "word/document.xml"
                body_node = None
                if is_main_doc:
                    for ch in root:
                        if _local_tag(ch.tag) == "body":
                            body_node = ch
                            break
                container = body_node if body_node is not None else root
                _walk_docx_container(container, is_main_doc, text_chunks, blocks)
            return "\n".join(text_chunks).strip(), blocks
    except Exception as e:
        logger.warning("DOCX parse failed for %s: %s", file_path.name, e)
        return "", []


def extract_docx(file_path: Path) -> str:
    txt, _ = extract_docx_structured(file_path)
    return txt


def extract_pptx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """提取 .pptx 幻灯片正文与演讲者备注 notesSlides"""
    slides_data: List[Dict[str, Any]] = []
    text_chunks: List[str] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = set(zf.namelist())
            slide_files = [
                n for n in names
                if re.match(r"^ppt/slides/slide\d+\.xml$", n, flags=re.IGNORECASE)
            ]
            slide_files.sort(key=lambda x: int(re.findall(r"\d+", x)[-1]))

            for idx, sf in enumerate(slide_files, 1):
                slide_num = re.findall(r"\d+", sf)[-1]
                root = ET.fromstring(zf.read(sf))
                slide_lines: List[str] = []
                for elem in root.iter():
                    if _local_tag(elem.tag) == "p":
                        runs = [sub.text for sub in elem.iter() if _local_tag(sub.tag) == "t" and sub.text]
                        line = "".join(runs).strip()
                        if line and (not slide_lines or slide_lines[-1] != line):
                            slide_lines.append(line)

                # 补全演讲者备注读取
                notes_path = f"ppt/notesSlides/notesSlide{slide_num}.xml"
                notes_lines: List[str] = []
                if notes_path in names:
                    try:
                        n_root = ET.fromstring(zf.read(notes_path))
                        for elem in n_root.iter():
                            if _local_tag(elem.tag) == "p":
                                runs = [sub.text for sub in elem.iter() if _local_tag(sub.tag) == "t" and sub.text]
                                line = "".join(runs).strip()
                                if line and not line.isdigit() and line not in slide_lines:
                                    notes_lines.append(line)
                    except Exception:
                        pass

                combined_lines = slide_lines + ([f"[备注] {' '.join(notes_lines)}"] if notes_lines else [])
                if combined_lines:
                    slides_data.append({
                        "slide_no": idx,
                        "title": combined_lines[0],
                        "lines": combined_lines,
                    })
                    text_chunks.append(f"【第{idx}页】" + " / ".join(combined_lines))
        joined = "\n".join(text_chunks).strip()
        return joined, slides_data
    except Exception as e:
        logger.warning("PPTX parse failed for %s: %s", file_path.name, e)
        return "", []


def _col_letters_to_index(col_str: str) -> int:
    idx = 0
    for ch in col_str.upper():
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _load_xlsx_date_style_indices(zf: zipfile.ZipFile, names: Set[str]) -> Set[int]:
    """从 xl/styles.xml 识别属于日期/时间格式的 cellXfs 样式索引"""
    date_xfs: Set[int] = set()
    builtin_date_ids = set(range(14, 23)) | set(range(45, 48)) | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 50, 51, 52, 53, 54, 55, 56, 57, 58}
    if "xl/styles.xml" not in names:
        return date_xfs
    try:
        s_root = ET.fromstring(zf.read("xl/styles.xml"))
        custom_date_ids: Set[int] = set()
        for el in s_root.iter():
            if _local_tag(el.tag) == "numFmt":
                nid = int(el.attrib.get("numFmtId", "-1"))
                code = (el.attrib.get("formatCode") or "").lower()
                if any(tok in code for tok in ("yy", "mm", "dd", "年", "月", "日")) and "#" not in code:
                    custom_date_ids.add(nid)
        for el in s_root.iter():
            if _local_tag(el.tag) == "cellXfs":
                idx = 0
                for xf in el:
                    if _local_tag(xf.tag) == "xf":
                        nfid = int(xf.attrib.get("numFmtId", "0"))
                        if nfid in builtin_date_ids or nfid in custom_date_ids:
                            date_xfs.add(idx)
                        idx += 1
                break
    except Exception:
        pass
    return date_xfs


def _excel_serial_to_date_str(val: str) -> str:
    try:
        num = float(val)
        if 20000 <= num <= 70000:
            base = datetime(1899, 12, 30)
            dt = base + timedelta(days=num)
            if abs(num - int(num)) < 1e-6:
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return val


def extract_xlsx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """提取 .xlsx 电子表格结构与全文（支持日期序列号还原、布尔值格式化与截断提示）"""
    sheets_out: List[Dict[str, Any]] = []
    lines_out: List[str] = []

    if zipfile.is_zipfile(file_path):
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                names = set(zf.namelist())
                date_xfs = _load_xlsx_date_style_indices(zf, names)
                shared_strings: List[str] = []
                if "xl/sharedStrings.xml" in names:
                    ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in ss_root:
                        if _local_tag(si.tag) == "si":
                            parts = [t.text for t in si.iter() if _local_tag(t.tag) == "t" and t.text]
                            shared_strings.append("".join(parts))

                sheet_entries: List[Tuple[str, str]] = []
                if "xl/workbook.xml" in names:
                    wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
                    rels_map: Dict[str, str] = {}
                    if "xl/_rels/workbook.xml.rels" in names:
                        rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
                        for rel in rels_root:
                            rid = rel.attrib.get("Id", "")
                            target = rel.attrib.get("Target", "").lstrip("/")
                            if not target.startswith("xl/"):
                                target = "xl/" + target
                            rels_map[rid] = target
                    for elem in wb_root.iter():
                        if _local_tag(elem.tag) == "sheet":
                            sname = elem.attrib.get("name") or "Sheet"
                            rid = ""
                            for k, v in elem.attrib.items():
                                if k.endswith("id"):
                                    rid = v
                                    break
                            target_xml = rels_map.get(rid, f"xl/worksheets/sheet{len(sheet_entries)+1}.xml")
                            if target_xml in names:
                                sheet_entries.append((sname, target_xml))

                if not sheet_entries:
                    ws_files = sorted(
                        [n for n in names if re.match(r"^xl/worksheets/sheet\d+\.xml$", n)],
                        key=lambda x: int(re.findall(r"\d+", x)[-1]),
                    )
                    sheet_entries = [(f"工作表{i+1}", wf) for i, wf in enumerate(ws_files)]

                for sname, sxml_path in sheet_entries:
                    ws_root = ET.fromstring(zf.read(sxml_path))
                    rows_matrix: List[List[str]] = []
                    max_col = 0
                    parsed_rows_dict: List[Dict[int, str]] = []

                    for row_el in ws_root.iter():
                        if _local_tag(row_el.tag) != "row":
                            continue
                        row_map: Dict[int, str] = {}
                        col_cursor = 0
                        for c_el in row_el:
                            if _local_tag(c_el.tag) != "c":
                                continue
                            ref = c_el.attrib.get("r", "")
                            m_col = re.match(r"^([A-Za-z]+)", ref)
                            c_idx = _col_letters_to_index(m_col.group(1)) if m_col else col_cursor
                            col_cursor = c_idx + 1
                            ctype = c_el.attrib.get("t", "")
                            style_idx = int(c_el.attrib.get("s", "-1")) if c_el.attrib.get("s", "").isdigit() else -1
                            val_str = ""
                            if ctype == "inlineStr":
                                t_parts = [t.text for t in c_el.iter() if _local_tag(t.tag) == "t" and t.text]
                                val_str = "".join(t_parts).strip()
                            else:
                                v_node = next((ch for ch in c_el if _local_tag(ch.tag) == "v"), None)
                                if v_node is not None and v_node.text is not None:
                                    raw_v = v_node.text.strip()
                                    if ctype == "s" and raw_v.isdigit():
                                        s_idx = int(raw_v)
                                        if 0 <= s_idx < len(shared_strings):
                                            val_str = shared_strings[s_idx].strip()
                                    elif ctype == "b":
                                        val_str = "是" if raw_v == "1" else "否"
                                    elif style_idx in date_xfs:
                                        val_str = _excel_serial_to_date_str(raw_v)
                                    else:
                                        val_str = raw_v
                            if val_str:
                                row_map[c_idx] = val_str
                                if c_idx + 1 > max_col:
                                    max_col = c_idx + 1
                        if row_map:
                            parsed_rows_dict.append(row_map)

                    if parsed_rows_dict and max_col > 0:
                        truncated = len(parsed_rows_dict) > 600 or max_col > 30
                        capped_cols = min(max_col, 30)
                        lines_out.append(f"【工作表：{sname}】")
                        for rmap in parsed_rows_dict[:600]:
                            row_list = [rmap.get(ci, "") for ci in range(capped_cols)]
                            if any(row_list):
                                rows_matrix.append(row_list)
                                non_empty = [x for x in row_list if x]
                                lines_out.append(" | ".join(non_empty))
                        if truncated:
                            lines_out.append(f"[已截断：原表共 {len(parsed_rows_dict)} 行 × {max_col} 列，仅展示前 600 行 × 30 列]")
                        sheets_out.append({"name": sname, "rows": rows_matrix, "truncated": truncated})

                if sheets_out:
                    return "\n".join(lines_out).strip(), sheets_out
        except Exception as e:
            logger.warning("XLSX XML parse failed for %s: %s", file_path.name, e)

    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(file_path), data_only=True, read_only=True)
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            lines_out.append(f"【工作表：{sheet}】")
            s_rows: List[List[str]] = []
            for row in ws.iter_rows(values_only=True):
                row_vals = ["" if v is None else str(v).strip() for v in row[:30]]
                if any(row_vals):
                    s_rows.append(row_vals)
                    lines_out.append(" | ".join([x for x in row_vals if x]))
                if len(s_rows) >= 600:
                    break
            if s_rows:
                sheets_out.append({"name": sheet, "rows": s_rows})
        return "\n".join(lines_out).strip(), sheets_out
    except Exception:
        return "", []


def _resolve_ofd_page_paths(zf: zipfile.ZipFile, names: List[str]) -> List[str]:
    """按 OFD.xml -> DocRoot -> Document.xml -> Pages/Page/@BaseLoc 标准链路解析页面 XML 列表"""
    name_set = set(names)
    ordered_pages: List[str] = []

    if "OFD.xml" in name_set:
        try:
            ofd_root = ET.fromstring(zf.read("OFD.xml"))
            doc_roots = [
                el.text.strip().lstrip("/")
                for el in ofd_root.iter()
                if _local_tag(el.tag) == "DocRoot" and el.text and el.text.strip()
            ]
            for dr in doc_roots:
                if dr not in name_set:
                    continue
                doc_dir = str(Path(dr).parent).replace("\\", "/")
                if doc_dir == ".":
                    doc_dir = ""
                doc_root = ET.fromstring(zf.read(dr))
                for el in doc_root.iter():
                    if _local_tag(el.tag) == "Page":
                        base_loc = (el.attrib.get("BaseLoc") or "").strip().lstrip("/")
                        if not base_loc:
                            continue
                        full_path = f"{doc_dir}/{base_loc}" if doc_dir and not base_loc.startswith(doc_dir + "/") else base_loc
                        if full_path in name_set and full_path not in ordered_pages:
                            ordered_pages.append(full_path)
        except Exception:
            pass

    if ordered_pages:
        return ordered_pages

    # 回退：仅匹配 Content.xml，排除 Pages.xml / Template / Annotation
    fallback = [
        n for n in names
        if n.lower().endswith("content.xml")
        and "template" not in n.lower()
        and "annot" not in n.lower()
    ]
    fallback.sort(key=lambda p: [int(x) for x in re.findall(r"\d+", p)] or [9999])
    return fallback


def extract_ofd(file_path: Path) -> str:
    """
    提取党政机关 .ofd 电子公文正文（纯 Python 解析 ZIP + XML）
    按标准 OFD 链路定位页序，按 TextCode 坐标 Y 聚类分行、X 排序，避免冒号强制断行破坏时间/比例。
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            meta_lines: List[str] = []
            if "OFD.xml" in names:
                try:
                    ofd_root = ET.fromstring(zf.read("OFD.xml"))
                    for elem in ofd_root.iter():
                        if _local_tag(elem.tag) == "CustomData" and elem.text and elem.text.strip():
                            field_name = elem.attrib.get("Name", "")
                            val = elem.text.strip()
                            meta_lines.append(f"【{field_name}】{val}" if field_name else val)
                except Exception:
                    pass

            page_files = _resolve_ofd_page_paths(zf, names)
            page_texts: List[str] = []
            for pf in page_files:
                try:
                    root = ET.fromstring(zf.read(pf))
                    items: List[Tuple[float, float, str]] = []
                    for obj in root.iter():
                        if _local_tag(obj.tag) == "TextObject":
                            b_str = obj.attrib.get("Boundary", "")
                            b_nums = [float(v) for v in b_str.split()[:2]] if b_str else [0.0, 0.0]
                            ox, oy = (b_nums[0], b_nums[1]) if len(b_nums) >= 2 else (0.0, 0.0)
                            for tc in obj:
                                if _local_tag(tc.tag) == "TextCode" and tc.text:
                                    t = tc.text.strip()
                                    if not t:
                                        continue
                                    try:
                                        tx = float(tc.attrib.get("X", "0"))
                                        ty = float(tc.attrib.get("Y", "0"))
                                    except ValueError:
                                        tx, ty = 0.0, 0.0
                                    items.append((oy + ty, ox + tx, t))
                    if not items:
                        # 兼容扁平 TextCode 结构
                        codes = [
                            el.text.strip()
                            for el in root.iter()
                            if _local_tag(el.tag) == "TextCode" and el.text and el.text.strip()
                        ]
                        if codes:
                            joined = "".join(codes)
                            joined = re.sub(r"([。；！？])\s*", r"\1\n", joined)
                            page_texts.append(joined.strip())
                        continue

                    # 按 Y 坐标容差 2.5mm 聚类成行，行内按 X 排序
                    items.sort(key=lambda it: (round(it[0] / 2.5), it[1]))
                    grouped_lines: List[str] = []
                    cur_y: Optional[float] = None
                    cur_buf: List[str] = []
                    for y, _x, txt in items:
                        if cur_y is None or abs(y - cur_y) <= 2.5:
                            cur_buf.append(txt)
                            if cur_y is None:
                                cur_y = y
                        else:
                            grouped_lines.append("".join(cur_buf))
                            cur_buf = [txt]
                            cur_y = y
                    if cur_buf:
                        grouped_lines.append("".join(cur_buf))
                    page_texts.append("\n".join(grouped_lines).strip())
                except Exception:
                    continue

            parts = []
            if meta_lines:
                parts.append("\n".join(meta_lines))
            if page_texts:
                parts.append("\n\n".join(page_texts))
            return "\n\n".join(parts).strip()
    except Exception as e:
        logger.warning("OFD parse failed for %s: %s", file_path.name, e)
        return ""


def extract_pdf(file_path: Path) -> str:
    """提取 PDF 文本（限 50 页；前 3 页无文本时跳过 pdfplumber 防扫描件卡顿）"""
    pypdf_scanned_hint = False
    try:
        import pypdf
        reader = pypdf.PdfReader(str(file_path))
        total_pages = len(reader.pages)
        pages: List[str] = []
        sample_limit = min(total_pages, 3)
        sample_has_text = False
        for i, page in enumerate(reader.pages[:MAX_PDF_SYNC_PAGES]):
            txt = (page.extract_text() or "").strip()
            if txt:
                pages.append(txt)
                if i < sample_limit:
                    sample_has_text = True
            elif i == sample_limit - 1 and not sample_has_text and total_pages > 3:
                # 抽检前 3 页完全无文字，大概率为红头扫描件，提前终止避免白耗 CPU
                pypdf_scanned_hint = True
                break
        if pages:
            out = "\n\n".join(pages)
            if total_pages > MAX_PDF_SYNC_PAGES:
                out += f"\n\n[已截断：该 PDF 共 {total_pages} 页，已提取前 {MAX_PDF_SYNC_PAGES} 页用于检索]"
            return out
    except Exception:
        pass

    if not pypdf_scanned_hint:
        try:
            import pdfplumber
            pages = []
            with pdfplumber.open(str(file_path)) as pdf:
                for page in pdf.pages[:MAX_PDF_SYNC_PAGES]:
                    txt = (page.extract_text() or "").strip()
                    if txt:
                        pages.append(txt)
            if pages:
                return "\n\n".join(pages)
        except Exception:
            pass

    try:
        res = subprocess.run(
            ["pdftotext", "-l", str(MAX_PDF_SYNC_PAGES), "-enc", "UTF-8", str(file_path), "-"],
            **_subprocess_kwargs(timeout=12),
        )
        if res.returncode == 0 and res.stdout:
            txt = res.stdout.decode("utf-8", errors="ignore").strip()
            if txt:
                return txt
    except Exception:
        pass

    return ""


def _find_soffice_bin() -> str:
    global _SOFFICE_BIN_CACHE
    if _SOFFICE_BIN_CACHE is not None:
        return _SOFFICE_BIN_CACHE
    for cand in ("soffice", "libreoffice"):
        found = shutil.which(cand)
        if found:
            _SOFFICE_BIN_CACHE = found
            return found
    _SOFFICE_BIN_CACHE = ""
    return ""


def extract_legacy_doc_or_wps(file_path: Path) -> str:
    """提取老版本二进制 .doc 或 .wps/.et/.dps 文档正文（支持无头 LibreOffice 与体积保护）"""
    ext = file_path.suffix.lower()
    if zipfile.is_zipfile(file_path):
        if ext == ".dps":
            txt, _ = extract_pptx_structured(file_path)
            if txt:
                return txt
        elif ext == ".et":
            txt, _ = extract_xlsx_structured(file_path)
            if txt:
                return txt
        else:
            txt = extract_docx(file_path)
            if txt:
                return txt

    # 优先尝试本机 LibreOffice/soffice 无头转换
    soffice = _find_soffice_bin()
    if soffice:
        try:
            with tempfile.TemporaryDirectory(prefix="gw_soffice_") as tmpdir:
                res = subprocess.run(
                    [soffice, "--headless", "--convert-to", "txt:Text (encoded):UTF8", "--outdir", tmpdir, str(file_path)],
                    **_subprocess_kwargs(timeout=15),
                )
                if res.returncode == 0:
                    out_txt = Path(tmpdir) / f"{file_path.stem}.txt"
                    if out_txt.exists():
                        content = read_text_with_fallback_encodings(out_txt.read_bytes()).strip()
                        if len(content) > 10:
                            return content
        except Exception:
            pass

    for cmd in (["antiword", "-m", "UTF-8.txt", str(file_path)], ["catdoc", "-d", "utf-8", str(file_path)]):
        try:
            res = subprocess.run(cmd, **_subprocess_kwargs(timeout=10))
            if res.returncode == 0 and res.stdout:
                out = res.stdout.decode("utf-8", errors="ignore").strip()
                if len(out) > 20:
                    return out
        except Exception:
            pass

    # 体积保护：最多读取 10MB 防止大文件内存飙升
    try:
        with open(file_path, "rb") as f:
            raw = f.read(MAX_LEGACY_BIN_BYTES)
        candidates: List[str] = []
        for offset in (0, 1):
            u16_text = raw[offset:].decode("utf-16le", errors="ignore")
            u16_matches = re.findall(
                r"[\u4e00-\u9fa5A-Za-z0-9，。、；：？！“”‘’（）《》【】—…·\-\s]{8,}",
                u16_text,
            )
            for m in u16_matches:
                clean = m.strip()
                if len(re.findall(r"[\u4e00-\u9fa5]", clean)) >= 4 and clean not in candidates:
                    candidates.append(clean)
            if len(candidates) >= 5:
                break
        if candidates:
            return "\n".join(candidates)

        gb_text = raw.decode("gb18030", errors="ignore")
        gb_matches = re.findall(
            r"[\u4e00-\u9fa5A-Za-z0-9，。、；：？！“”‘’（）《》【】—…·\-\s]{10,}",
            gb_text,
        )
        valid_gb = [m.strip() for m in gb_matches if len(re.findall(r"[\u4e00-\u9fa5]", m)) >= 5]
        if valid_gb:
            return "\n".join(valid_gb)
    except Exception as e:
        logger.warning("Legacy DOC/WPS parse failed for %s: %s", file_path.name, e)

    return ""


def _get_image_dimensions(raw: bytes) -> Tuple[int, int]:
    """零依赖读取 PNG / JPEG / GIF 基础分辨率（补全 JPEG 0xFF 填充与独立标记防护）"""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
            return int(w), int(h)
        if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
            w, h = struct.unpack("<HH", raw[6:10])
            return int(w), int(h)
        if raw[:2] == b"\xff\xd8":
            idx = 2
            n = len(raw)
            standalone_markers = {0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8))
            while idx < n - 1:
                if raw[idx] != 0xFF:
                    idx += 1
                    continue
                while idx < n and raw[idx] == 0xFF:
                    idx += 1
                if idx >= n:
                    break
                marker = raw[idx]
                idx += 1
                if marker in standalone_markers:
                    continue
                if idx + 2 > n:
                    break
                seg_len = struct.unpack(">H", raw[idx : idx + 2])[0]
                if seg_len < 2:
                    break
                if marker in (0xC0, 0xC1, 0xC2, 0xC3) and idx + 7 <= n:
                    h, w = struct.unpack(">HH", raw[idx + 3 : idx + 7])
                    return int(w), int(h)
                idx += seg_len
    except Exception:
        pass
    return 0, 0


def _get_available_tesseract_langs() -> Set[str]:
    """启动时缓存一次 tesseract 可用语言包，杜绝每次图片同步重复超时试错"""
    global _TESSERACT_LANGS_CACHE
    if _TESSERACT_LANGS_CACHE is not None:
        return _TESSERACT_LANGS_CACHE
    langs: Set[str] = set()
    if shutil.which("tesseract"):
        try:
            res = subprocess.run(["tesseract", "--list-langs"], **_subprocess_kwargs(timeout=4))
            if res.returncode == 0 and res.stdout:
                for ln in res.stdout.decode("utf-8", errors="ignore").splitlines():
                    ln = ln.strip()
                    if ln and not ln.lower().startswith("list of"):
                        langs.add(ln)
        except Exception:
            pass
    _TESSERACT_LANGS_CACHE = langs
    return langs


def extract_image_ocr_and_meta(file_path: Path) -> str:
    """
    提取图片文字与元数据：
    1. 仅读取前 64KB 头部获取分辨率，避免 10MB 照片整张读入内存
    2. 仅当系统确实安装了 chi_sim 语言包时执行中文 OCR，彻底避免无中文包时 3x8s 阻塞及英文乱码污染
    """
    head_bytes = b""
    try:
        with open(file_path, "rb") as f:
            head_bytes = f.read(65536)
    except Exception:
        pass
    w, h = _get_image_dimensions(head_bytes)
    dim_str = f" ({w}x{h}px)" if (w and h) else ""

    ocr_text = ""
    avail_langs = _get_available_tesseract_langs()
    target_lang = ""
    if "chi_sim" in avail_langs and "eng" in avail_langs:
        target_lang = "chi_sim+eng"
    elif "chi_sim" in avail_langs:
        target_lang = "chi_sim"

    if target_lang:
        try:
            res = subprocess.run(
                ["tesseract", str(file_path), "stdout", "-l", target_lang, "--psm", "6"],
                **_subprocess_kwargs(timeout=8),
            )
            if res.returncode == 0 and res.stdout:
                txt = res.stdout.decode("utf-8", errors="ignore").strip()
                txt = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])", "", txt)
                if len(txt) >= 4:
                    ocr_text = txt
        except Exception:
            pass

    if ocr_text:
        return f"【图片OCR提取文字{dim_str}】\n{ocr_text}"
    return f"【影像图片文件：{file_path.stem}{dim_str}】"


def extract_document_text(file_path: Path) -> Tuple[str, str]:
    """统一入口：输入文件路径，返回 (extracted_text, file_type_label)"""
    if not file_path.exists() or not file_path.is_file():
        return "", "未知"

    ext = file_path.suffix.lower()
    if ext in (".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".ini"):
        raw = file_path.read_bytes()
        return read_text_with_fallback_encodings(raw).strip(), "文本/Markdown"
    elif ext == ".csv":
        raw = file_path.read_bytes()
        return read_text_with_fallback_encodings(raw).strip(), "CSV表格(.csv)"
    elif ext == ".docx":
        return extract_docx(file_path), "Word公文(.docx)"
    elif ext in (".pptx", ".ppt"):
        if ext == ".pptx" or zipfile.is_zipfile(file_path):
            txt, _ = extract_pptx_structured(file_path)
            return txt, "演示课件(.pptx)"
        return extract_legacy_doc_or_wps(file_path), "演示课件(.ppt)"
    elif ext == ".ofd":
        return extract_ofd(file_path), "电子公文(.ofd)"
    elif ext == ".pdf":
        return extract_pdf(file_path), "PDF文档(.pdf)"
    elif ext in (".doc", ".wps", ".et", ".dps"):
        return extract_legacy_doc_or_wps(file_path), f"办公文档({ext})"
    elif ext in (".html", ".htm", ".xml"):
        raw = file_path.read_bytes()
        txt = read_text_with_fallback_encodings(raw)
        clean = re.sub(r"<[^>]+>", " ", txt)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean, "网页/XML"
    elif ext in (".xlsx", ".xls"):
        txt, _ = extract_xlsx_structured(file_path)
        return txt, f"电子表格({ext})"
    elif ext in IMAGE_EXTS:
        return extract_image_ocr_and_meta(file_path), f"现场照片/图片({ext})"
    elif ext in AUDIO_EXTS:
        size_kb = max(1, file_path.stat().st_size // 1024)
        return f"【录音音频文件：{file_path.stem} ({size_kb} KB)】", f"录音音频({ext})"
    elif ext in VIDEO_EXTS:
        size_mb = round(file_path.stat().st_size / (1024 * 1024), 1)
        return f"【视频影像文件：{file_path.stem} ({size_mb} MB)】", f"视频影像({ext})"
    else:
        return f"[附件文件：{file_path.name}]", f"附件({ext or '无后缀'})"


def extract_structured_preview(file_path: Path, doc_id: int = 0) -> Dict[str, Any]:
    """为【重要事项记录】直读画布与【预览抽屉】生成结构化多格式直读数据"""
    if not file_path.exists() or not file_path.is_file():
        return {"render_mode": "text", "blocks": [], "sheets": [], "slides": []}

    ext = file_path.suffix.lower()
    if ext == ".docx":
        _, blocks = extract_docx_structured(file_path)
        return {"render_mode": "docx", "blocks": blocks}
    elif ext == ".xlsx" or (ext == ".et" and zipfile.is_zipfile(file_path)):
        _, sheets = extract_xlsx_structured(file_path)
        return {"render_mode": "spreadsheet", "sheets": sheets}
    elif ext == ".csv":
        raw = file_path.read_bytes()
        txt = read_text_with_fallback_encodings(raw)
        rows = [r for r in csv.reader(io.StringIO(txt)) if any(c.strip() for c in r)]
        return {"render_mode": "spreadsheet", "sheets": [{"name": file_path.stem, "rows": rows[:600]}]}
    elif ext == ".pptx" or (ext == ".dps" and zipfile.is_zipfile(file_path)):
        _, slides = extract_pptx_structured(file_path)
        return {"render_mode": "pptx", "slides": slides}
    elif ext == ".pdf":
        return {"render_mode": "pdf", "media_url": f"/api/media/{doc_id}" if doc_id else ""}
    elif ext in IMAGE_EXTS:
        return {"render_mode": "image", "media_url": f"/api/media/{doc_id}" if doc_id else ""}
    elif ext in AUDIO_EXTS:
        return {"render_mode": "audio", "media_url": f"/api/media/{doc_id}" if doc_id else ""}
    elif ext in VIDEO_EXTS:
        return {"render_mode": "video", "media_url": f"/api/media/{doc_id}" if doc_id else ""}
    return {"render_mode": "text"}
