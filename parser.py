#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多格式公文、多媒体资料与常备知识库提取器 (parser.py v3.2.0 - 适配银河麒麟 Kylin V10 + Windows + macOS)
修复与增强：
- .docx: 修复 <w:p> 内嵌 <w:txbxContent> 双倍提取；<w:tc> 遍历跳过 <w:del>/<mc:Fallback>
- 纯文本/HTML/XML/ZIP: 5MB 读取上限防 OOM 与正则卡死；ZIP 单 XML 50MB Zip Bomb 防护；.xlsx 流式 iterparse 读满 601 行立即释放
- .xls: 正确路由至 LibreOffice 转换流程，不再静默返回空；openpyxl 回退确保 wb.close()
- .pptx: 按 presentation.xml sldIdLst + rels 还原真实页序，并通过各页 .rels 精确绑定 notesSlide
- .ofd: 单 TextObject Boundary 异常隔离；精确 Y 排序 + 2.5mm 容差聚类消除量化抖动；反斜杠路径规范化
- .pdf: 不提前 break 跳过 pdfplumber；异常记录 logging.warning；超 50 页统一截断提示；前 5 页全空且各引擎无输出时才提示扫描件
- 图片 OCR: 跳过 .svg 不送 tesseract；JPEG 循环跳过超长 EXIF/ICC 段；支持 BMP/WebP 尺寸；tesseract 语言包线程安全 TTL(300s) 缓存
- LibreOffice: 独立临时 UserInstallation 配置目录隔离并发，finally 清理
"""

import csv
import io
import html
import logging
import os
import posixpath
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import uuid
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
MAX_TEXT_READ_BYTES = 5 * 1024 * 1024    # 5 MB
MAX_ZIP_XML_BYTES = 50 * 1024 * 1024     # 50 MB per XML entry (Zip Bomb protection)
TESSERACT_LANGS_TTL = 300.0              # 300 seconds

_TESSERACT_LANGS_LOCK = threading.Lock()
_TESSERACT_LANGS_CACHE: Optional[Set[str]] = None
_TESSERACT_LANGS_CACHE_TS: float = 0.0
_SOFFICE_BIN_CACHE: Optional[str] = None


def _subprocess_kwargs(timeout: int = 15) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"capture_output": True, "timeout": timeout}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kw


def read_text_with_fallback_encodings(raw_bytes: bytes) -> str:
    """自适应常见中文办公编码（UTF-8 / UTF-8-SIG / GB18030 / UTF-16LE / UTF-16BE）"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "utf-16le", "utf-16be"):
        try:
            text = raw_bytes.decode(enc)
            if "\x00" not in text or enc.startswith("utf-16"):
                return text.replace("\x00", "")
        except Exception:
            continue
    return raw_bytes.decode("utf-8", errors="ignore").replace("\x00", "")


def _trim_incomplete_utf8_tail(data: bytes) -> bytes:
    """截断大文件时剥离末尾残缺的 UTF-8 多字节序列，避免整篇误回落 GB18030 变乱码"""
    if not data:
        return data
    n = len(data)
    for back in range(1, min(4, n) + 1):
        b = data[n - back]
        if (b & 0x80) == 0:
            return data
        if (b & 0xC0) == 0xC0:
            if (b & 0xE0) == 0xC0:
                expected = 2
            elif (b & 0xF0) == 0xE0:
                expected = 3
            elif (b & 0xF8) == 0xF0:
                expected = 4
            else:
                return data
            if back < expected:
                return data[: n - back]
            return data
    return data


def _read_file_bytes_limited(
    file_path: Path, max_bytes: int = MAX_TEXT_READ_BYTES
) -> Tuple[bytes, bool, int]:
    """安全读取文本/XML/CSV 等文件，限制最大读取体积并保护 UTF-8 截断边界"""
    total_size = 0
    try:
        total_size = file_path.stat().st_size
    except Exception:
        pass
    with open(file_path, "rb") as f:
        raw = f.read(max_bytes + 1)
    if len(raw) > max_bytes:
        return _trim_incomplete_utf8_tail(raw[:max_bytes]), True, max(total_size, len(raw))
    return raw, False, max(total_size, len(raw))


def _check_zip_entry_size(
    zf: zipfile.ZipFile, name: str, max_bytes: int = MAX_ZIP_XML_BYTES
) -> zipfile.ZipInfo:
    """Zip Bomb 防护：检查 ZIP 内单文件声明解压体积是否超限"""
    info = zf.getinfo(name)
    if info.file_size > max_bytes:
        raise ValueError(
            f"ZIP entry '{name}' uncompressed size ({info.file_size} bytes) exceeds limit ({max_bytes} bytes)"
        )
    return info


MAX_ZIP_TOTAL_UNCOMPRESSED = 120 * 1024 * 1024


def _check_zip_archive_budget(zf: zipfile.ZipFile, max_total: int = MAX_ZIP_TOTAL_UNCOMPRESSED) -> None:
    """校验 ZIP 压缩包所有条目累计声明解压体积与条目数，防止多文件组合 Zip Bomb"""
    infos = zf.infolist()
    if len(infos) > 5000:
        raise ValueError(f"ZIP entry count ({len(infos)}) exceeds safety limit")
    total = sum(getattr(i, "file_size", 0) for i in infos)
    if total > max_total:
        raise ValueError(f"ZIP total uncompressed size ({total} bytes) exceeds limit ({max_total} bytes)")


def _safe_zip_read(
    zf: zipfile.ZipFile, name: str, max_bytes: int = MAX_ZIP_XML_BYTES
) -> bytes:
    """带 Zip Bomb 防护的 ZIP 条目读取（同时校验头部声明体积与实际解压读取量）"""
    info = _check_zip_entry_size(zf, name, max_bytes=max_bytes)
    with zf.open(info, "r") as stream:
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(
            f"ZIP entry '{name}' actual uncompressed stream exceeds limit ({max_bytes} bytes)"
        )
    return data


def _local_tag(tag_str: str) -> str:
    return tag_str.split("}")[-1] if "}" in tag_str else tag_str


def _extract_w_p_text(p_elem: ET.Element) -> str:
    """提取 Word 段落文本，跳过 <w:del> 删除修订、<mc:Fallback> 与内嵌 <w:txbxContent> 文本框"""
    p_texts: List[str] = []

    def _walk(node: ET.Element) -> None:
        tag = _local_tag(node.tag)
        if tag in ("del", "delText", "Fallback", "instrText", "fldData", "txbxContent"):
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


def _find_txbx_contents(node: ET.Element) -> List[ET.Element]:
    """在节点下查找直属 <w:txbxContent>，跳过 <w:del> 与 <mc:Fallback> 避免提取已删或重复内容"""
    found: List[ET.Element] = []

    def _scan(cur: ET.Element) -> None:
        for ch in cur:
            tag = _local_tag(ch.tag)
            if tag in ("del", "Fallback"):
                continue
            if tag == "txbxContent":
                found.append(ch)
                continue
            _scan(ch)

    _scan(node)
    return found


def _collect_tc_lines(tc_elem: ET.Element) -> List[str]:
    """收集单元格 <w:tc> 内段落与文本框文本，严格跳过 <w:del> 与 <mc:Fallback>"""
    cell_lines: List[str] = []

    def _scan_tc(node: ET.Element) -> None:
        for ch in node:
            tag = _local_tag(ch.tag)
            if tag in ("del", "Fallback"):
                continue
            if tag == "p":
                pt = _extract_w_p_text(ch)
                if pt:
                    cell_lines.append(pt)
                for txbx in _find_txbx_contents(ch):
                    _scan_tc(txbx)
            elif tag in ("sdt", "sdtContent", "txbxContent", "customXml", "ins", "tbl", "tr", "tc"):
                _scan_tc(ch)

    _scan_tc(tc_elem)
    return cell_lines


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
            # 检查段落内嵌文本框 <w:txbxContent>（跳过 <w:del> 与 <mc:Fallback>）
            for txbx in _find_txbx_contents(child):
                _walk_docx_container(txbx, is_main_doc, text_chunks, blocks)
        elif ctag == "tbl":
            rows_data: List[List[str]] = []
            for tr in child:
                if _local_tag(tr.tag) != "tr":
                    continue
                row_cells: List[str] = []
                for tc in tr:
                    if _local_tag(tc.tag) != "tc":
                        continue
                    cell_lines = _collect_tc_lines(tc)
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
                raw_xml = _safe_zip_read(zf, name)
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


def _resolve_zip_rel_target(base_dir: str, target: str) -> str:
    """将 OOXML 关系中的 Target 规范化为 ZIP 内标准 POSIX 路径"""
    t = (target or "").strip().replace("\\", "/")
    if not t:
        return ""
    if t.startswith("/"):
        return posixpath.normpath(t.lstrip("/"))
    joined = posixpath.join(base_dir, t) if base_dir else t
    return posixpath.normpath(joined).lstrip("/")


def _resolve_pptx_ordered_slides(zf: zipfile.ZipFile, names: Set[str]) -> List[str]:
    """按 ppt/presentation.xml 的 sldIdLst + presentation.xml.rels 获取真实幻灯片顺序"""
    ordered: List[str] = []
    pres_path = "ppt/presentation.xml"
    pres_rels_path = "ppt/_rels/presentation.xml.rels"
    if pres_path in names and pres_rels_path in names:
        try:
            rels_root = ET.fromstring(_safe_zip_read(zf, pres_rels_path))
            rels_map: Dict[str, str] = {}
            for rel in rels_root:
                if _local_tag(rel.tag) == "Relationship":
                    rid = rel.attrib.get("Id", "")
                    target = rel.attrib.get("Target", "")
                    if rid and target:
                        rels_map[rid] = _resolve_zip_rel_target("ppt", target)

            pres_root = ET.fromstring(_safe_zip_read(zf, pres_path))
            for elem in pres_root.iter():
                if _local_tag(elem.tag) == "sldId":
                    rid = ""
                    for k, v in elem.attrib.items():
                        if k == "id" and "}" not in k:
                            continue
                        if _local_tag(k) == "id":
                            rid = v
                            break
                    if rid and rid in rels_map:
                        slide_path = rels_map[rid]
                        if slide_path in names and slide_path not in ordered:
                            ordered.append(slide_path)
        except Exception as e:
            logger.warning("PPTX presentation order resolve failed: %s", e)

    if ordered:
        return ordered

    # 回退：按文件名数字排序
    slide_files = [
        n for n in names
        if re.match(r"^ppt/slides/slide\d+\.xml$", n, flags=re.IGNORECASE)
    ]
    slide_files.sort(key=lambda x: int(re.findall(r"\d+", x)[-1]))
    return slide_files


def _resolve_pptx_notes_path(zf: zipfile.ZipFile, names: Set[str], slide_path: str) -> Optional[str]:
    """通过幻灯片对应的 _rels/slideX.xml.rels 定位真实 notesSlide 路径"""
    slide_dir = posixpath.dirname(slide_path)
    slide_base = posixpath.basename(slide_path)
    rels_path = posixpath.join(slide_dir, "_rels", f"{slide_base}.rels")
    if rels_path in names:
        try:
            rels_root = ET.fromstring(_safe_zip_read(zf, rels_path))
            for rel in rels_root:
                if _local_tag(rel.tag) == "Relationship":
                    rtype = (rel.attrib.get("Type") or "").lower()
                    if rtype.endswith("/notesslide") or "notesslide" in rtype:
                        target = _resolve_zip_rel_target(slide_dir, rel.attrib.get("Target", ""))
                        if target in names:
                            return target
            # 关系文件存在但未绑定备注页，不猜测误绑
            return None
        except Exception as e:
            logger.warning("PPTX slide rels parse failed for %s: %s", rels_path, e)
            return None

    # 仅在缺失 rels 文件时回退旧式按编号匹配
    nums = re.findall(r"\d+", slide_base)
    if nums:
        cand = f"ppt/notesSlides/notesSlide{nums[-1]}.xml"
        if cand in names:
            return cand
    return None


def extract_pptx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """提取 .pptx 幻灯片正文与演讲者备注 notesSlides（按真实页序与关系绑定）"""
    slides_data: List[Dict[str, Any]] = []
    text_chunks: List[str] = []
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = set(zf.namelist())
            slide_files = _resolve_pptx_ordered_slides(zf, names)

            for idx, sf in enumerate(slide_files, 1):
                root = ET.fromstring(_safe_zip_read(zf, sf))
                slide_lines: List[str] = []
                for elem in root.iter():
                    if _local_tag(elem.tag) == "p":
                        runs = [sub.text for sub in elem.iter() if _local_tag(sub.tag) == "t" and sub.text]
                        line = "".join(runs).strip()
                        if line and (not slide_lines or slide_lines[-1] != line):
                            slide_lines.append(line)

                # 通过幻灯片 .rels 精确绑定演讲者备注
                notes_path = _resolve_pptx_notes_path(zf, names, sf)
                notes_lines: List[str] = []
                if notes_path:
                    try:
                        n_root = ET.fromstring(_safe_zip_read(zf, notes_path))
                        for elem in n_root.iter():
                            if _local_tag(elem.tag) == "p":
                                runs = [sub.text for sub in elem.iter() if _local_tag(sub.tag) == "t" and sub.text]
                                line = "".join(runs).strip()
                                if line and not line.isdigit() and line not in slide_lines:
                                    notes_lines.append(line)
                    except Exception as e:
                        logger.warning("PPTX notes parse failed for %s: %s", notes_path, e)

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
    builtin_date_ids = (
        set(range(14, 23))
        | set(range(45, 48))
        | {27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 50, 51, 52, 53, 54, 55, 56, 57, 58}
    )
    if "xl/styles.xml" not in names:
        return date_xfs
    try:
        s_root = ET.fromstring(_safe_zip_read(zf, "xl/styles.xml"))
        custom_date_ids: Set[int] = set()
        for el in s_root.iter():
            if _local_tag(el.tag) == "numFmt":
                try:
                    nid = int(el.attrib.get("numFmtId", "-1"))
                except (ValueError, TypeError):
                    continue
                code = (el.attrib.get("formatCode") or "").lower()
                if any(tok in code for tok in ("yy", "mm", "dd", "hh", "ss", "年", "月", "日", "时", "分")) and "#" not in code:
                    custom_date_ids.add(nid)
        for el in s_root.iter():
            if _local_tag(el.tag) == "cellXfs":
                idx = 0
                for xf in el:
                    if _local_tag(xf.tag) == "xf":
                        try:
                            nfid = int(xf.attrib.get("numFmtId", "0"))
                            if nfid in builtin_date_ids or nfid in custom_date_ids:
                                date_xfs.add(idx)
                        except (ValueError, TypeError):
                            pass
                        idx += 1
                break
    except Exception as e:
        logger.warning("XLSX styles.xml parse failed: %s", e)
    return date_xfs


def _excel_serial_to_date_str(val: str) -> str:
    """还原 Excel 日期/时间序列号（支持纯时间值 0<=num<1 与宽范围日期 1<=num<=2958465）"""
    try:
        num = float(val)
        if 0.0 <= num < 1.0:
            total_sec = int(round(num * 86400))
            total_sec %= 86400
            hh = total_sec // 3600
            mm = (total_sec % 3600) // 60
            ss = total_sec % 60
            if ss > 0:
                return f"{hh:02d}:{mm:02d}:{ss:02d}"
            return f"{hh:02d}:{mm:02d}"
        if 1.0 <= num <= 2958465.0:
            base = datetime(1899, 12, 30)
            dt = base + timedelta(days=num)
            if abs(num - int(num)) < 1e-6:
                return dt.strftime("%Y-%m-%d")
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return val


def extract_xlsx_structured(file_path: Path) -> Tuple[str, List[Dict[str, Any]]]:
    """提取 .xlsx 电子表格结构与全文（流式 iterparse 防 OOM、日期/时间还原、布尔值格式化与截断提示）"""
    sheets_out: List[Dict[str, Any]] = []
    lines_out: List[str] = []

    if zipfile.is_zipfile(file_path):
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                _check_zip_archive_budget(zf)
                names = set(zf.namelist())
                date_xfs = _load_xlsx_date_style_indices(zf, names)
                shared_strings: List[str] = []
                if "xl/sharedStrings.xml" in names:
                    ss_bytes = _safe_zip_read(zf, "xl/sharedStrings.xml")
                    for _ev, si in ET.iterparse(io.BytesIO(ss_bytes), events=("end",)):
                        if _local_tag(si.tag) == "si":
                            parts = [t.text for t in si.iter() if _local_tag(t.tag) == "t" and t.text]
                            shared_strings.append("".join(parts))
                            si.clear()
                            if len(shared_strings) >= 200000:
                                break

                sheet_entries: List[Tuple[str, str]] = []
                if "xl/workbook.xml" in names:
                    wb_root = ET.fromstring(_safe_zip_read(zf, "xl/workbook.xml"))
                    rels_map: Dict[str, str] = {}
                    if "xl/_rels/workbook.xml.rels" in names:
                        rels_root = ET.fromstring(_safe_zip_read(zf, "xl/_rels/workbook.xml.rels"))
                        for rel in rels_root:
                            if _local_tag(rel.tag) != "Relationship":
                                continue
                            rid = rel.attrib.get("Id", "")
                            raw_target = rel.attrib.get("Target", "")
                            if rid and raw_target:
                                norm_target = _resolve_zip_rel_target("xl", raw_target)
                                rels_map[rid] = norm_target
                    for elem in wb_root.iter():
                        if _local_tag(elem.tag) == "sheet":
                            sname = elem.attrib.get("name") or "Sheet"
                            rid = ""
                            for k, v in elem.attrib.items():
                                if _local_tag(k) == "id" and "}" in k:
                                    rid = v
                                    break
                            if not rid:
                                for k, v in elem.attrib.items():
                                    kl = _local_tag(k).lower()
                                    if kl == "id" and k != "sheetId":
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
                    ws_bytes = _safe_zip_read(zf, sxml_path)
                    rows_matrix: List[List[str]] = []
                    max_col = 0
                    parsed_rows_dict: List[Dict[int, str]] = []

                    for _event, elem in ET.iterparse(io.BytesIO(ws_bytes), events=("end",)):
                            if _local_tag(elem.tag) != "row":
                                continue
                            row_map: Dict[int, str] = {}
                            col_cursor = 0
                            for c_el in elem:
                                if _local_tag(c_el.tag) != "c":
                                    continue
                                ref = c_el.attrib.get("r", "")
                                m_col = re.match(r"^([A-Za-z]+)", ref)
                                c_idx = _col_letters_to_index(m_col.group(1)) if m_col else col_cursor
                                col_cursor = c_idx + 1
                                ctype = c_el.attrib.get("t", "")
                                s_attr = c_el.attrib.get("s", "")
                                style_idx = int(s_attr) if s_attr.isdigit() else -1
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
                            elem.clear()
                            if row_map:
                                parsed_rows_dict.append(row_map)
                                if len(parsed_rows_dict) >= 601:
                                    break

                    if parsed_rows_dict and max_col > 0:
                        row_truncated = len(parsed_rows_dict) > 600
                        col_truncated = max_col > 30
                        truncated = row_truncated or col_truncated
                        capped_cols = min(max_col, 30)
                        lines_out.append(f"【工作表：{sname}】")
                        for rmap in parsed_rows_dict[:600]:
                            row_list = [rmap.get(ci, "") for ci in range(capped_cols)]
                            if any(row_list):
                                rows_matrix.append(row_list)
                                non_empty = [x for x in row_list if x]
                                lines_out.append(" | ".join(non_empty))
                        if truncated:
                            row_desc = "超过 600 行" if row_truncated else f"共 {len(parsed_rows_dict)} 行"
                            lines_out.append(f"[已截断：原表{row_desc} × {max_col} 列，仅展示前 600 行 × 30 列]")
                        sheets_out.append({"name": sname, "rows": rows_matrix, "truncated": truncated})

                if sheets_out:
                    return "\n".join(lines_out).strip(), sheets_out
        except Exception as e:
            logger.warning("XLSX XML parse failed for %s: %s", file_path.name, e)

    wb = None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(file_path), data_only=True, read_only=True)
        for sheet in wb.sheetnames:
            ws = wb[sheet]
            lines_out.append(f"【工作表：{sheet}】")
            s_rows: List[List[str]] = []
            truncated = False
            for row in ws.iter_rows(values_only=True):
                if len(row) > 30:
                    truncated = True
                row_vals = ["" if v is None else str(v).strip() for v in row[:30]]
                if any(row_vals):
                    if len(s_rows) >= 600:
                        truncated = True
                        break
                    s_rows.append(row_vals)
                    lines_out.append(" | ".join([x for x in row_vals if x]))
            if truncated:
                lines_out.append("[已截断：原表超过 600 行或 30 列，仅展示前 600 行 × 30 列]")
            if s_rows:
                sheets_out.append({"name": sheet, "rows": s_rows, "truncated": truncated})
        return "\n".join(lines_out).strip(), sheets_out
    except Exception as e:
        logger.warning("XLSX openpyxl fallback failed for %s: %s", file_path.name, e)
        return "", []
    finally:
        if wb is not None:
            try:
                wb.close()
            except Exception:
                pass


def _resolve_ofd_page_paths(zf: zipfile.ZipFile, names: List[str]) -> List[str]:
    """按 OFD.xml -> DocRoot -> Document.xml -> Pages/Page/@BaseLoc 标准链路解析页面 XML 列表（兼容反斜杠）"""
    # 规范化键名到原始 ZIP 成员名的映射，兼容国产签章软件产生的反斜杠路径
    norm_to_orig: Dict[str, str] = {}
    norm_lower_to_orig: Dict[str, str] = {}
    for n in names:
        norm = posixpath.normpath(n.replace("\\", "/")).lstrip("/")
        norm_to_orig[norm] = n
        norm_lower_to_orig[norm.lower()] = n

    ordered_pages: List[str] = []
    ofd_orig = norm_to_orig.get("OFD.xml") or norm_lower_to_orig.get("ofd.xml")

    if ofd_orig:
        try:
            ofd_root = ET.fromstring(_safe_zip_read(zf, ofd_orig))
            doc_roots = [
                posixpath.normpath(el.text.strip().replace("\\", "/")).lstrip("/")
                for el in ofd_root.iter()
                if _local_tag(el.tag) == "DocRoot" and el.text and el.text.strip()
            ]
            for dr in doc_roots:
                dr_orig = norm_to_orig.get(dr)
                if not dr_orig:
                    continue
                doc_dir = posixpath.dirname(dr)
                doc_root = ET.fromstring(_safe_zip_read(zf, dr_orig))
                for el in doc_root.iter():
                    if _local_tag(el.tag) == "Page":
                        base_loc = (el.attrib.get("BaseLoc") or "").strip().replace("\\", "/").lstrip("/")
                        if not base_loc:
                            continue
                        if doc_dir and not base_loc.startswith(doc_dir + "/"):
                            full_norm = posixpath.normpath(posixpath.join(doc_dir, base_loc)).lstrip("/")
                        else:
                            full_norm = posixpath.normpath(base_loc).lstrip("/")
                        orig_path = norm_to_orig.get(full_norm)
                        if orig_path and orig_path not in ordered_pages:
                            ordered_pages.append(orig_path)
        except Exception as e:
            logger.warning("OFD page path resolve failed: %s", e)

    if ordered_pages:
        return ordered_pages

    # 回退：仅匹配 Content.xml，排除 Pages.xml / Template / Annotation
    fallback = [
        n for n in names
        if n.replace("\\", "/").lower().endswith("content.xml")
        and "template" not in n.lower()
        and "annot" not in n.lower()
    ]
    fallback.sort(key=lambda p: [int(x) for x in re.findall(r"\d+", p)] or [9999])
    return fallback


def extract_ofd(file_path: Path) -> str:
    """
    提取党政机关 .ofd 电子公文正文（纯 Python 解析 ZIP + XML）
    按标准 OFD 链路定位页序，精确 Y 排序后按 <=2.5mm 容差聚类成行再按 X 排序，消除量化边界抖动。
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            norm_to_orig = {posixpath.normpath(n.replace("\\", "/")).lstrip("/"): n for n in names}
            meta_lines: List[str] = []
            ofd_orig = norm_to_orig.get("OFD.xml")
            if ofd_orig:
                try:
                    ofd_root = ET.fromstring(_safe_zip_read(zf, ofd_orig))
                    for elem in ofd_root.iter():
                        if _local_tag(elem.tag) == "CustomData" and elem.text and elem.text.strip():
                            field_name = elem.attrib.get("Name", "")
                            val = elem.text.strip()
                            meta_lines.append(f"【{field_name}】{val}" if field_name else val)
                except Exception as e:
                    logger.warning("OFD metadata parse failed for %s: %s", file_path.name, e)

            page_files = _resolve_ofd_page_paths(zf, names)
            page_texts: List[str] = []
            for pf in page_files:
                try:
                    root = ET.fromstring(_safe_zip_read(zf, pf))
                    items: List[Tuple[float, float, str]] = []
                    for obj in root.iter():
                        if _local_tag(obj.tag) == "TextObject":
                            ox, oy = 0.0, 0.0
                            b_str = obj.attrib.get("Boundary", "")
                            if b_str:
                                try:
                                    b_parts = b_str.split()
                                    if len(b_parts) >= 2:
                                        ox, oy = float(b_parts[0]), float(b_parts[1])
                                except (ValueError, TypeError):
                                    ox, oy = 0.0, 0.0
                            for tc in obj:
                                if _local_tag(tc.tag) == "TextCode" and tc.text:
                                    t = tc.text.strip()
                                    if not t:
                                        continue
                                    try:
                                        tx = float(tc.attrib.get("X", "0"))
                                        ty = float(tc.attrib.get("Y", "0"))
                                    except (ValueError, TypeError):
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

                    # 先按精确 Y 排序，再按容差 <= 2.5mm 聚类成行，每行内按 X 排序
                    items.sort(key=lambda it: (it[0], it[1]))
                    clusters: List[List[Tuple[float, float, str]]] = []
                    cur_cluster: List[Tuple[float, float, str]] = []
                    cur_y: Optional[float] = None
                    for item in items:
                        y = item[0]
                        if cur_y is None or abs(y - cur_y) <= 2.5:
                            cur_cluster.append(item)
                            if cur_y is None:
                                cur_y = y
                        else:
                            clusters.append(cur_cluster)
                            cur_cluster = [item]
                            cur_y = y
                    if cur_cluster:
                        clusters.append(cur_cluster)

                    grouped_lines: List[str] = []
                    for cl in clusters:
                        cl.sort(key=lambda it: it[1])
                        line_parts: List[str] = []
                        prev_x: Optional[float] = None
                        for _y, x_pos, seg_txt in cl:
                            if prev_x is not None and (x_pos - prev_x) > 8.0 and line_parts and not line_parts[-1].endswith(" "):
                                line_parts.append(" ")
                            line_parts.append(seg_txt)
                            prev_x = x_pos
                        grouped_lines.append("".join(line_parts))
                    page_texts.append("\n".join(grouped_lines).strip())
                except Exception as e:
                    logger.warning("OFD page parse failed (%s in %s): %s", pf, file_path.name, e)
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


def _append_pdf_truncation_notice(text: str, total_pages: int) -> str:
    if total_pages > MAX_PDF_SYNC_PAGES:
        return text + f"\n\n[已截断：该 PDF 共 {total_pages} 页，已提取前 {MAX_PDF_SYNC_PAGES} 页用于检索]"
    return text


def extract_pdf(file_path: Path) -> str:
    """提取 PDF 文本（限 50 页；不跳过 pdfplumber；异常记录警告；仅前 5 页全空且各引擎无输出时提示扫描件）"""
    total_pages = 0
    first_five_checked = 0
    first_five_has_text = False

    pypdf_scanned_empty = False
    try:
        import pypdf
        reader = pypdf.PdfReader(str(file_path))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")
            except Exception:
                return "[提示：该 PDF 已加密受密码保护，无法提取正文]"
        total_pages = len(reader.pages)
        pages: List[str] = []
        for i, page in enumerate(reader.pages[:MAX_PDF_SYNC_PAGES]):
            txt = (page.extract_text() or "").strip()
            if i < 5:
                first_five_checked += 1
                if txt:
                    first_five_has_text = True
            if txt:
                pages.append(txt)
        if pages:
            return _append_pdf_truncation_notice("\n\n".join(pages), total_pages)
        if total_pages > 0 and first_five_checked >= min(total_pages, 5) and not first_five_has_text:
            pypdf_scanned_empty = True
    except Exception as e:
        logger.warning("PDF pypdf parse failed for %s: %s", file_path.name, e)

    if not pypdf_scanned_empty:
      try:
        import pdfplumber
        pages = []
        with pdfplumber.open(str(file_path)) as pdf:
            if total_pages == 0:
                total_pages = len(pdf.pages)
            for i, page in enumerate(pdf.pages[:MAX_PDF_SYNC_PAGES]):
                txt = (page.extract_text() or "").strip()
                if i < 5:
                    first_five_checked = max(first_five_checked, i + 1)
                    if txt:
                        first_five_has_text = True
                if txt:
                    pages.append(txt)
        if pages:
            return _append_pdf_truncation_notice("\n\n".join(pages), total_pages)
      except Exception as e:
        logger.warning("PDF pdfplumber parse failed for %s: %s", file_path.name, e)

    try:
        res = subprocess.run(
            ["pdftotext", "-l", str(MAX_PDF_SYNC_PAGES), "-enc", "UTF-8", str(file_path), "-"],
            **_subprocess_kwargs(timeout=12),
        )
        if res.returncode == 0 and res.stdout:
            txt = res.stdout.decode("utf-8", errors="ignore").strip()
            if txt:
                return _append_pdf_truncation_notice(txt, total_pages)
    except Exception as e:
        logger.warning("PDF pdftotext fallback failed for %s: %s", file_path.name, e)

    if total_pages > 0 and first_five_checked >= min(total_pages, 5) and not first_five_has_text:
        return f"[提示：该 PDF 共 {total_pages} 页，前 {min(total_pages, 5)} 页均未检测到可提取文字，可能为扫描件或纯图片 PDF]"

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


def _run_soffice_convert(soffice: str, file_path: Path, convert_to: str, outdir: str, timeout: int = 15) -> subprocess.CompletedProcess:
    """带独立临时 UserInstallation 配置目录的 LibreOffice 无头转换，防止并发锁死"""
    lo_profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex}"
    lo_profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        profile_uri = lo_profile_dir.as_uri()
        return subprocess.run(
            [
                soffice,
                f"-env:UserInstallation={profile_uri}",
                "--headless",
                "--convert-to",
                convert_to,
                "--outdir",
                outdir,
                str(file_path),
            ],
            **_subprocess_kwargs(timeout=timeout),
        )
    finally:
        shutil.rmtree(lo_profile_dir, ignore_errors=True)


def extract_legacy_doc_or_wps(file_path: Path) -> str:
    """提取老版本二进制 .doc/.xls/.wps/.et/.dps 文档正文（支持无头 LibreOffice 并发隔离与体积保护）"""
    ext = file_path.suffix.lower()
    if zipfile.is_zipfile(file_path):
        if ext == ".dps":
            txt, _ = extract_pptx_structured(file_path)
            if txt:
                return txt
        elif ext in (".et", ".xls"):
            txt, _ = extract_xlsx_structured(file_path)
            if txt:
                return txt
        else:
            txt = extract_docx(file_path)
            if txt:
                return txt

    # 优先尝试本机 LibreOffice/soffice 无头转换（隔离 UserInstallation）
    soffice = _find_soffice_bin()
    if soffice:
        try:
            with tempfile.TemporaryDirectory(prefix="gw_soffice_") as tmpdir:
                if ext in (".xls", ".et"):
                    # 电子表格优先转 xlsx 保留多工作表，失败再转 csv
                    res = _run_soffice_convert(soffice, file_path, "xlsx", tmpdir, timeout=20)
                    if res.returncode == 0:
                        out_xlsx = Path(tmpdir) / f"{file_path.stem}.xlsx"
                        if out_xlsx.exists():
                            txt, _ = extract_xlsx_structured(out_xlsx)
                            if txt:
                                return txt
                    res_csv = _run_soffice_convert(soffice, file_path, "csv", tmpdir, timeout=15)
                    if res_csv.returncode == 0:
                        out_csv = Path(tmpdir) / f"{file_path.stem}.csv"
                        if out_csv.exists():
                            content = read_text_with_fallback_encodings(out_csv.read_bytes()).strip()
                            if content:
                                return content
                else:
                    res = _run_soffice_convert(
                        soffice, file_path, "txt:Text (encoded):UTF8", tmpdir, timeout=15
                    )
                    if res.returncode == 0:
                        out_txt = Path(tmpdir) / f"{file_path.stem}.txt"
                        if out_txt.exists():
                            content = read_text_with_fallback_encodings(out_txt.read_bytes()).strip()
                            if len(content) > 10:
                                return content
        except Exception as e:
            logger.warning("LibreOffice convert failed for %s: %s", file_path.name, e)

    if ext not in (".xls", ".et"):
        for cmd in (["antiword", "-m", "UTF-8.txt", str(file_path)], ["catdoc", "-d", "utf-8", str(file_path)]):
            try:
                res = subprocess.run(cmd, **_subprocess_kwargs(timeout=10))
                if res.returncode == 0 and res.stdout:
                    out = res.stdout.decode("utf-8", errors="ignore").strip()
                    if len(out) > 20:
                        return out
            except Exception:
                pass

    # 体积保护：最多读取 10MB 防止大文件内存飙升；UTF-16LE 比较 offset=0 与 offset=1 择优取文本更长者
    try:
        with open(file_path, "rb") as f:
            raw = f.read(MAX_LEGACY_BIN_BYTES)
        best_u16_text = ""
        for offset in (0, 1):
            u16_text = raw[offset:].decode("utf-16le", errors="ignore")
            u16_matches = re.findall(
                r"[\u4e00-\u9fa5A-Za-z0-9，。、；：？！“”‘’（）《》【】—…·\-\s]{8,}",
                u16_text,
            )
            seen: Set[str] = set()
            candidates: List[str] = []
            for m in u16_matches:
                clean = m.strip()
                if len(re.findall(r"[\u4e00-\u9fa5]", clean)) >= 4 and clean not in seen:
                    seen.add(clean)
                    candidates.append(clean)
            joined = "\n".join(candidates)
            if len(joined) > len(best_u16_text):
                best_u16_text = joined
        if best_u16_text:
            return best_u16_text

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


def _parse_webp_dimensions(raw: bytes) -> Tuple[int, int]:
    """解析 WebP (VP8 / VP8L / VP8X) 宽高"""
    if len(raw) < 30 or raw[:4] != b"RIFF" or raw[8:12] != b"WEBP":
        return 0, 0
    chunk = raw[12:16]
    if chunk == b"VP8X" and len(raw) >= 30:
        w = 1 + int.from_bytes(raw[24:27], "little")
        h = 1 + int.from_bytes(raw[27:30], "little")
        return w, h
    if chunk == b"VP8L" and len(raw) >= 25 and raw[20] == 0x2F:
        b0, b1, b2, b3 = raw[21], raw[22], raw[23], raw[24]
        w = 1 + (((b1 & 0x3F) << 8) | b0)
        h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return w, h
    if chunk == b"VP8 " and len(raw) >= 30 and raw[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", raw[26:30])
        return int(w & 0x3FFF), int(h & 0x3FFF)
    return 0, 0


def _get_image_dimensions(raw: bytes) -> Tuple[int, int]:
    """零依赖读取 PNG / JPEG / GIF / BMP / WebP 基础分辨率（基于字节缓冲）"""
    try:
        if raw[:8] == b"\x89PNG\r\n\x1a\n" and len(raw) >= 24:
            w, h = struct.unpack(">II", raw[16:24])
            return int(w), int(h)
        if raw[:6] in (b"GIF87a", b"GIF89a") and len(raw) >= 10:
            w, h = struct.unpack("<HH", raw[6:10])
            return int(w), int(h)
        if raw[:2] == b"BM" and len(raw) >= 26:
            w, h = struct.unpack("<ii", raw[18:26])
            return abs(int(w)), abs(int(h))
        if raw[:4] == b"RIFF" and len(raw) >= 30 and raw[8:12] == b"WEBP":
            return _parse_webp_dimensions(raw)
        if raw[:2] == b"\xff\xd8":
            idx = 2
            n = len(raw)
            standalone_markers = {0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8))
            sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
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
                if marker == 0x00 or marker in standalone_markers:
                    continue
                if marker == 0xDA:  # SOS
                    break
                if idx + 2 > n:
                    break
                seg_len = struct.unpack(">H", raw[idx : idx + 2])[0]
                if seg_len < 2:
                    break
                if marker in sof_markers and idx + 7 <= n:
                    h, w = struct.unpack(">HH", raw[idx + 3 : idx + 7])
                    return int(w), int(h)
                idx += seg_len
    except Exception:
        pass
    return 0, 0


def _get_image_dimensions_from_file(file_path: Path) -> Tuple[int, int]:
    """流式读取图片尺寸：JPEG 支持循环 seek 跳过超长 EXIF/ICC 段（不局限前 64KB）"""
    try:
        with open(file_path, "rb") as f:
            head = f.read(64)
            if not head:
                return 0, 0
            if head[:2] != b"\xff\xd8":
                return _get_image_dimensions(head)

            # JPEG 流式段扫描，支持任意长度 EXIF/ICC/XMP 段
            f.seek(2)
            standalone_markers = {0x01, 0xD8, 0xD9} | set(range(0xD0, 0xD8))
            sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            max_segments = 4096
            for _ in range(max_segments):
                b = f.read(1)
                if not b:
                    break
                if b[0] != 0xFF:
                    continue
                while b and b[0] == 0xFF:
                    b = f.read(1)
                if not b:
                    break
                marker = b[0]
                if marker == 0x00 or marker in standalone_markers:
                    continue
                if marker == 0xDA:  # Start of Scan
                    break
                len_bytes = f.read(2)
                if len(len_bytes) < 2:
                    break
                seg_len = struct.unpack(">H", len_bytes)[0]
                if seg_len < 2:
                    break
                if marker in sof_markers:
                    sof_data = f.read(5)
                    if len(sof_data) == 5:
                        h, w = struct.unpack(">HH", sof_data[1:5])
                        return int(w), int(h)
                    break
                f.seek(seg_len - 2, os.SEEK_CUR)
    except Exception:
        pass
    return 0, 0


def _get_available_tesseract_langs() -> Set[str]:
    """线程安全 + TTL(300s) 缓存 tesseract 可用语言包，杜绝每次图片同步重复超时试错"""
    global _TESSERACT_LANGS_CACHE, _TESSERACT_LANGS_CACHE_TS
    now = time.monotonic()
    if _TESSERACT_LANGS_CACHE is not None and (now - _TESSERACT_LANGS_CACHE_TS) < TESSERACT_LANGS_TTL:
        return _TESSERACT_LANGS_CACHE
    with _TESSERACT_LANGS_LOCK:
        now = time.monotonic()
        if _TESSERACT_LANGS_CACHE is not None and (now - _TESSERACT_LANGS_CACHE_TS) < TESSERACT_LANGS_TTL:
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
            except Exception as e:
                logger.warning("tesseract --list-langs failed: %s", e)
        _TESSERACT_LANGS_CACHE = langs
        _TESSERACT_LANGS_CACHE_TS = time.monotonic()
        return langs


def extract_image_ocr_and_meta(file_path: Path) -> str:
    """
    提取图片文字与元数据：
    1. 跳过 .svg 纯文本矢量图不送 tesseract
    2. 流式解析图片尺寸（JPEG 支持跳过超长 EXIF/ICC 段，支持 PNG/GIF/BMP/WebP）
    3. 仅当系统确实安装了 chi_sim 语言包时执行中文 OCR
    """
    ext = file_path.suffix.lower()
    if ext == ".svg":
        return f"【矢量图片文件：{file_path.stem}】"

    w, h = _get_image_dimensions_from_file(file_path)
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
        except Exception as e:
            logger.warning("Image OCR failed for %s: %s", file_path.name, e)

    if ocr_text:
        return f"【图片OCR提取文字{dim_str}】\n{ocr_text}"
    return f"【影像图片文件：{file_path.stem}{dim_str}】"


def extract_document_text(file_path: Path) -> Tuple[str, str]:
    """统一入口：输入文件路径，返回 (extracted_text, file_type_label)"""
    if not file_path.exists() or not file_path.is_file():
        return "", "未知"

    ext = file_path.suffix.lower()
    if ext in (".txt", ".md", ".markdown", ".log", ".json", ".yaml", ".yml", ".ini"):
        raw, truncated, total_size = _read_file_bytes_limited(file_path, MAX_TEXT_READ_BYTES)
        txt = read_text_with_fallback_encodings(raw).strip()
        if truncated:
            size_mb = total_size / (1024 * 1024)
            txt += f"\n\n[已截断：原文件体积约 {size_mb:.1f} MB，超过 5MB 读取上限，仅提取前 5MB 内容]"
        return txt, "文本/Markdown"
    elif ext == ".csv":
        raw, truncated, total_size = _read_file_bytes_limited(file_path, MAX_TEXT_READ_BYTES)
        txt = read_text_with_fallback_encodings(raw).strip()
        if truncated:
            size_mb = total_size / (1024 * 1024)
            txt += f"\n\n[已截断：原文件体积约 {size_mb:.1f} MB，超过 5MB 读取上限，仅提取前 5MB 内容]"
        return txt, "CSV表格(.csv)"
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
        raw, truncated, total_size = _read_file_bytes_limited(file_path, MAX_TEXT_READ_BYTES)
        txt = read_text_with_fallback_encodings(raw)
        txt = re.sub(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", " ", txt, flags=re.I)
        clean = re.sub(r"<[^>]{0,4096}>", " ", txt)
        clean = html.unescape(clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        if truncated:
            size_mb = total_size / (1024 * 1024)
            clean += f"\n\n[已截断：原文件体积约 {size_mb:.1f} MB，超过 5MB 读取上限，仅提取前 5MB 内容]"
        return clean, "网页/XML"
    elif ext == ".xlsx":
        txt, _ = extract_xlsx_structured(file_path)
        return txt, "电子表格(.xlsx)"
    elif ext == ".xls":
        if zipfile.is_zipfile(file_path):
            txt, _ = extract_xlsx_structured(file_path)
            if txt:
                return txt, "电子表格(.xls)"
        return extract_legacy_doc_or_wps(file_path), "电子表格(.xls)"
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
    elif ext == ".xlsx" or (ext in (".et", ".xls") and zipfile.is_zipfile(file_path)):
        _, sheets = extract_xlsx_structured(file_path)
        return {"render_mode": "spreadsheet", "sheets": sheets}
    elif ext == ".csv":
        raw, truncated, _ = _read_file_bytes_limited(file_path, MAX_TEXT_READ_BYTES)
        txt = read_text_with_fallback_encodings(raw)
        rows = [r for r in csv.reader(io.StringIO(txt)) if any(c.strip() for c in r)]
        return {
            "render_mode": "spreadsheet",
            "sheets": [{"name": file_path.stem, "rows": rows[:600], "truncated": truncated or len(rows) > 600}],
        }
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
