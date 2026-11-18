#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多格式公文、多媒体资料与常备知识库提取器 (parser.py v3.0.0 - 适配银河麒麟 Kylin V10 + 海光 3350)
支持格式与增强特性：
- .ofd (党政机关电子公文标准格式：纯 Python 解析 ZIP 内 OFD.xml 与 Content.xml 的 TextCode 节点)
- .docx (纯 Python 顺序解析 word/document.xml 段落与内嵌表格 <w:tbl>，支持仿真公文直读预览)
- .pptx (纯 Python 零依赖解析 ppt/slides/slide*.xml 幻灯片标题、正文与备注，解决培训课件无法检索痛点)
- .xlsx / .csv (内置纯 Python 标准库零依赖解析 xl/sharedStrings.xml 与 sheet*.xml，支持前端交互式表格直读)
- .pdf (自适应 pypdf / pdfplumber / 系统 pdftotext)
- .doc / .wps / .et / .dps (自适应系统工具或纯 Python 二进制流文本抽提)
- .png / .jpg / .jpeg / .webp / .bmp (自适应系统 tesseract 中文 OCR 提取微信截图文字 + 图像尺寸解析)
- .mp3 / .m4a / .wav / .ogg / .aac / .flac / .mp4 / .webm (音视频媒体识别与元数据提取)
"""

import csv
import io
import os
import re
import struct
import zipfile
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Tuple, Dict, Any, List


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".aac", ".flac", ".wma", ".amr"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi"}


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
    p_texts: List[str] = []
    for sub in p_elem.iter():
        sub_tag = _local_tag(sub.tag)
        if sub_tag == "t" and sub.text:
            p_texts.append(sub.text)
        elif sub_tag in ("br", "cr"):
            p_texts.append("\n")
        elif sub_tag == "tab":
            p_texts.append("\t")
    return "".join(p_texts).strip()


def extract_docx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """
    顺序提取 .docx 中的段落与内嵌表格（纯 Python 标准库实现，零依赖）
    返回 (plain_text, blocks) 供 FTS5 索引与【重要事项记录】直读画布使用
    """
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
                for child in container:
                    ctag = _local_tag(child.tag)
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
                    elif ctag == "tbl":
                        rows_data: List[List[str]] = []
                        for tr in child:
                            if _local_tag(tr.tag) != "tr":
                                continue
                            row_cells: List[str] = []
                            for tc in tr:
                                if _local_tag(tc.tag) != "tc":
                                    continue
                                cell_lines = []
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
            return "\n".join(text_chunks).strip(), blocks
    except Exception as e:
        return f"[DOCX解析提示: {e}]", []


def extract_docx(file_path: Path) -> str:
    """提取 .docx 正文与表格内容"""
    txt, _ = extract_docx_structured(file_path)
    return txt


def extract_pptx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """
    纯 Python 标准库零依赖提取 .pptx 培训课件/汇报演示文稿正文与每页结构
    解析 ppt/slides/slide*.xml 与 ppt/notesSlides/notesSlide*.xml
    """
    slides_data: List[Dict[str, Any]] = []
    text_chunks: List[str] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            slide_files = [
                n for n in names
                if re.match(r"^ppt/slides/slide\d+\.xml$", n, flags=re.IGNORECASE)
            ]
            slide_files.sort(key=lambda x: int(re.findall(r"\d+", x)[-1]))

            for idx, sf in enumerate(slide_files, 1):
                root = ET.fromstring(zf.read(sf))
                slide_lines: List[str] = []
                for elem in root.iter():
                    if _local_tag(elem.tag) == "p":
                        runs = [
                            sub.text
                            for sub in elem.iter()
                            if _local_tag(sub.tag) == "t" and sub.text
                        ]
                        line = "".join(runs).strip()
                        if line and (not slide_lines or slide_lines[-1] != line):
                            slide_lines.append(line)
                if slide_lines:
                    slides_data.append({
                        "slide_no": idx,
                        "title": slide_lines[0],
                        "lines": slide_lines,
                    })
                    text_chunks.append(f"【第{idx}页】" + " / ".join(slide_lines))
        joined = "\n".join(text_chunks).strip()
        return (joined if joined else "[PPTX演示文稿，无内嵌文字层]"), slides_data
    except Exception as e:
        return f"[PPTX解析提示: {e}]", []


def _col_letters_to_index(col_str: str) -> int:
    idx = 0
    for ch in col_str.upper():
        if "A" <= ch <= "Z":
            idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def extract_xlsx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """
    提取 .xlsx 电子表格结构与全文（内置纯 Python 标准库 XML 解析，不强制依赖 openpyxl）
    返回 (plain_text, sheets_list) 供 FTS5 索引与【重要事项记录】交互式网格直读使用
    """
    sheets_out: List[Dict[str, Any]] = []
    lines_out: List[str] = []

    # 1. 优先使用纯 Python 标准库解析 OpenXML，速度快且零外部依赖
    if zipfile.is_zipfile(file_path):
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                names = set(zf.namelist())
                shared_strings: List[str] = []
                if "xl/sharedStrings.xml" in names:
                    ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
                    for si in ss_root:
                        if _local_tag(si.tag) == "si":
                            parts = [t.text for t in si.iter() if _local_tag(t.tag) == "t" and t.text]
                            shared_strings.append("".join(parts))

                # 解析工作表名称映射
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
                                    else:
                                        val_str = raw_v
                            if val_str:
                                row_map[c_idx] = val_str
                                if c_idx + 1 > max_col:
                                    max_col = c_idx + 1
                        if row_map:
                            parsed_rows_dict.append(row_map)

                    if parsed_rows_dict and max_col > 0:
                        capped_cols = min(max_col, 30)
                        lines_out.append(f"【工作表：{sname}】")
                        for rmap in parsed_rows_dict[:600]:
                            row_list = [rmap.get(ci, "") for ci in range(capped_cols)]
                            if any(row_list):
                                rows_matrix.append(row_list)
                                non_empty = [x for x in row_list if x]
                                lines_out.append(" | ".join(non_empty))
                        sheets_out.append({"name": sname, "rows": rows_matrix})

                if sheets_out:
                    return "\n".join(lines_out).strip(), sheets_out
        except Exception:
            pass

    # 2. 备选 openpyxl
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
            if s_rows:
                sheets_out.append({"name": sheet, "rows": s_rows[:600]})
        return "\n".join(lines_out).strip(), sheets_out
    except Exception:
        return "[Excel表格文件，可下载原件或用 WPS 查看]", []


def extract_ofd(file_path: Path) -> str:
    """
    提取党政机关 .ofd 电子公文正文（纯 Python 解析 ZIP + XML）
    兼容 OFD 1.0/1.1 标准中的 <ofd:TextCode> / <TextCode> 节点及公文元数据 CustomDatas
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            meta_lines = []
            if "OFD.xml" in names:
                try:
                    ofd_root = ET.fromstring(zf.read("OFD.xml"))
                    for elem in ofd_root.iter():
                        tag = _local_tag(elem.tag)
                        if tag == "CustomData" and elem.text and elem.text.strip():
                            field_name = elem.attrib.get("Name", "")
                            val = elem.text.strip()
                            if field_name:
                                meta_lines.append(f"【{field_name}】{val}")
                            else:
                                meta_lines.append(val)
                except Exception:
                    pass

            content_files = [
                n for n in names
                if n.lower().endswith(".xml") and ("content" in n.lower() or "page" in n.lower())
            ]

            def page_sort_key(path_str: str):
                nums = [int(x) for x in re.findall(r"\d+", path_str)]
                return nums if nums else [9999]

            content_files.sort(key=page_sort_key)

            page_texts = []
            for cf in content_files:
                try:
                    root = ET.fromstring(zf.read(cf))
                    codes = []
                    for elem in root.iter():
                        tag = _local_tag(elem.tag)
                        if tag in ("TextCode", "TextObject") and elem.text:
                            t = elem.text.strip()
                            if t:
                                codes.append(t)
                    if codes:
                        joined = "".join(codes)
                        joined = re.sub(r"([。；！？：])\s*", r"\1\n", joined)
                        page_texts.append(joined.strip())
                except Exception:
                    continue

            parts = []
            if meta_lines:
                parts.append("\n".join(meta_lines))
            if page_texts:
                parts.append("\n\n".join(page_texts))
            result = "\n\n".join(parts).strip()
            return result if result else "[OFD文件未检测到内嵌文字层，可能为纯图像签章扫描件]"
    except Exception as e:
        return f"[OFD解析提示: {e}]"


def extract_pdf(file_path: Path) -> str:
    """提取 PDF 文本（优先 pypdf/pdfplumber，次选系统 pdftotext）"""
    try:
        import pypdf
        reader = pypdf.PdfReader(str(file_path))
        pages = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(txt.strip())
        if pages:
            return "\n\n".join(pages)
    except Exception:
        pass

    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(str(file_path)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                if txt.strip():
                    pages.append(txt.strip())
        if pages:
            return "\n\n".join(pages)
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(file_path), "-"],
            capture_output=True,
            timeout=15,
        )
        if res.returncode == 0 and res.stdout:
            txt = res.stdout.decode("utf-8", errors="ignore").strip()
            if txt:
                return txt
    except Exception:
        pass

    return "[该 PDF 未提取到可复制文字层，可能是扫描件/图片版公文，可在预览区直接查看原版页面]"


def extract_legacy_doc_or_wps(file_path: Path) -> str:
    """提取老版本二进制 .doc 或 .wps/.et/.dps 文档正文"""
    ext = file_path.suffix.lower()
    if zipfile.is_zipfile(file_path):
        if ext == ".dps":
            txt, _ = extract_pptx_structured(file_path)
            if txt and not txt.startswith("[PPTX解析提示"):
                return txt
        elif ext == ".et":
            txt, _ = extract_xlsx_structured(file_path)
            if txt:
                return txt
        else:
            txt = extract_docx(file_path)
            if txt and not txt.startswith("[DOCX解析提示"):
                return txt

    for cmd in (["antiword", "-m", "UTF-8.txt", str(file_path)], ["catdoc", "-d", "utf-8", str(file_path)]):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=10)
            if res.returncode == 0 and res.stdout:
                out = res.stdout.decode("utf-8", errors="ignore").strip()
                if len(out) > 20:
                    return out
        except Exception:
            pass

    try:
        raw = file_path.read_bytes()
        candidates = []
        u16_text = raw.decode("utf-16le", errors="ignore")
        u16_matches = re.findall(
            r"[\u4e00-\u9fa5A-Za-z0-9，。、；：？！“”‘’（）《》【】—…·\-\s]{8,}",
            u16_text,
        )
        for m in u16_matches:
            clean = m.strip()
            if len(re.findall(r"[\u4e00-\u9fa5]", clean)) >= 4:
                candidates.append(clean)
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
        return f"[DOC/WPS解析提示: {e}]"

    return "[未能直接解析该二进制文档正文，可直接下载原件或用 WPS 查看]"


def _get_image_dimensions(raw: bytes) -> Tuple[int, int]:
    """零依赖读取 PNG / JPEG 基础分辨率"""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
            return int(w), int(h)
        if raw[:2] == b"\xff\xd8":
            idx = 2
            n = len(raw)
            while idx < n - 9:
                if raw[idx] != 0xFF:
                    idx += 1
                    continue
                marker = raw[idx + 1]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3):
                    h, w = struct.unpack(">HH", raw[idx + 5 : idx + 9])
                    return int(w), int(h)
                seg_len = struct.unpack(">H", raw[idx + 2 : idx + 4])[0]
                idx += 2 + seg_len
    except Exception:
        pass
    return 0, 0


def extract_image_ocr_and_meta(file_path: Path) -> str:
    """
    提取图片（微信通知截图、下沉现场照片等）文字与元数据：
    1. 若系统安装了 tesseract (如银河麒麟预装 tesseract-ocr-chi-sim)，自动执行 OCR 文字提取
    2. 附加图片分辨率与文件名描述，保障可检索性
    """
    raw = b""
    try:
        raw = file_path.read_bytes()
    except Exception:
        pass
    w, h = _get_image_dimensions(raw)
    dim_str = f" ({w}x{h}px)" if (w and h) else ""

    ocr_text = ""
    for lang in ("chi_sim+eng", "chi_sim", "eng"):
        try:
            res = subprocess.run(
                ["tesseract", str(file_path), "stdout", "-l", lang, "--psm", "6"],
                capture_output=True,
                timeout=8,
            )
            if res.returncode == 0 and res.stdout:
                txt = res.stdout.decode("utf-8", errors="ignore").strip()
                txt = re.sub(r"(?<=[\u4e00-\u9fa5])\s+(?=[\u4e00-\u9fa5])", "", txt)
                if len(txt) >= 4:
                    ocr_text = txt
                    break
        except Exception:
            break

    if ocr_text:
        return f"【图片OCR提取文字{dim_str}】\n{ocr_text}"
    return f"【影像图片文件：{file_path.stem}{dim_str}】"


def extract_document_text(file_path: Path) -> Tuple[str, str]:
    """
    统一入口：输入文件路径，返回 (extracted_text, file_type_label)
    """
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
    """
    为【📌 重要事项记录】直读画布与【预览抽屉】生成结构化多格式直读数据：
    - Word (.docx): 返回段落与内嵌表格块列表
    - Excel (.xlsx / .csv): 返回多工作表二维数组，前端直接渲染可搜索数据网格
    - PPT (.pptx): 返回幻灯片分页卡片列表
    - PDF (.pdf): 返回原生内嵌 URL + 提取文本
    - Image / Audio / Video: 返回媒体直读标识
    """
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
