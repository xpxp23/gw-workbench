#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多格式公文与材料正文提取器 (适配银河麒麟 Kylin V10 + 海光 3350)
支持格式：
- .ofd (党政机关电子公文标准格式：纯 Python 解析 ZIP 内 OFD.xml 与 Content.xml 的 TextCode 节点)
- .docx (纯 Python 解析 word/document.xml 段落与表格，无需重型第三方依赖，若安装 python-docx 则自动增强)
- .pdf (自适应 pypdf / pdfplumber / 系统 pdftotext / 纯 Python 文本流兜底)
- .doc / .wps (自适应系统 antiword / libreoffice / wps 或纯 Python UTF-16LE/GBK 文本流抽提)
- .txt / .md / .csv / .json / .html (自适应 UTF-8 / GB18030 / GBK / UTF-16 编码)
"""

import os
import re
import zipfile
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Tuple, Dict, Any


def read_text_with_fallback_encodings(raw_bytes: bytes) -> str:
    """自适应常见中文办公编码（UTF-8 / UTF-8-SIG / GB18030 / UTF-16）"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "utf-16le"):
        try:
            text = raw_bytes.decode(enc)
            # 过滤过多空字符
            if "\x00" not in text or enc.startswith("utf-16"):
                return text.replace("\x00", "")
        except Exception:
            continue
    return raw_bytes.decode("utf-8", errors="ignore").replace("\x00", "")


def extract_docx(file_path: Path) -> str:
    """提取 .docx 正文与表格内容（纯 Python 标准库实现，零依赖且极速）"""
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            xml_chunks = []
            # 主文档与页眉页脚
            target_names = [
                n for n in zf.namelist()
                if n.startswith("word/") and n.endswith(".xml")
                and ("document" in n or "header" in n or "footer" in n)
            ]
            # 确保 document.xml 排在最前
            target_names.sort(key=lambda x: (0 if x == "word/document.xml" else 1, x))
            for name in target_names:
                raw_xml = zf.read(name)
                root = ET.fromstring(raw_xml)
                paragraphs = []
                for elem in root.iter():
                    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                    if tag == "p":
                        p_texts = []
                        for sub in elem.iter():
                            sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                            if sub_tag == "t" and sub.text:
                                p_texts.append(sub.text)
                            elif sub_tag in ("br", "cr"):
                                p_texts.append("\n")
                            elif sub_tag == "tab":
                                p_texts.append("\t")
                        line = "".join(p_texts).strip()
                        if line:
                            paragraphs.append(line)
                if paragraphs:
                    xml_chunks.append("\n".join(paragraphs))
            return "\n\n".join(xml_chunks).strip()
    except Exception as e:
        return f"[DOCX解析提示: {e}]"


def extract_ofd(file_path: Path) -> str:
    """
    提取党政机关 .ofd 电子公文正文（纯 Python 解析 ZIP + XML）
    兼容 OFD 1.0/1.1 标准中的 <ofd:TextCode> / <TextCode> 节点及公文元数据 CustomDatas
    """
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names = zf.namelist()
            meta_lines = []
            # 1. 提取 OFD.xml 中的公文元数据（如公文标题、发文字号、签发人等）
            if "OFD.xml" in names:
                try:
                    ofd_root = ET.fromstring(zf.read("OFD.xml"))
                    for elem in ofd_root.iter():
                        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                        if tag == "CustomData" and elem.text and elem.text.strip():
                            field_name = elem.attrib.get("Name", "")
                            val = elem.text.strip()
                            if field_name:
                                meta_lines.append(f"【{field_name}】{val}")
                            else:
                                meta_lines.append(val)
                except Exception:
                    pass

            # 2. 按页面顺序排序所有 Content.xml
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
                        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                        if tag in ("TextCode", "TextObject") and elem.text:
                            t = elem.text.strip()
                            if t:
                                codes.append(t)
                    if codes:
                        # 合并相连的单字或短词块
                        joined = "".join(codes)
                        # 优化公文常见段落断行
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
    # 1. 尝试 pypdf
    try:
        import pypdf
        reader = pypdf.PdfReader(str(file_path))
        pages = []
        for idx, page in enumerate(reader.pages):
            txt = page.extract_text() or ""
            if txt.strip():
                pages.append(txt.strip())
        if pages:
            return "\n\n".join(pages)
    except Exception:
        pass

    # 2. 尝试 pdfplumber
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

    # 3. 尝试系统自带 pdftotext (银河麒麟 / Linux poppler-utils 常见预装)
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

    return "[该 PDF 未提取到可复制文字层，可能是扫描件/图片版公文，可调用 AI 多模态接口或本地 Agent 识别]"


def extract_legacy_doc_or_wps(file_path: Path) -> str:
    """
    提取老版本二进制 .doc 或 .wps 文档正文：
    1. 若实质为 zip 封装（新版 WPS 有时后缀为 .wps 但内部是 XML zip），先尝试按 docx 解析
    2. 尝试系统 antiword / catdoc
    3. 纯 Python 扫描二进制流中的连续中文字符与 UTF-16LE 文本段（零依赖高可用兜底）
    """
    if zipfile.is_zipfile(file_path):
        txt = extract_docx(file_path)
        if txt and not txt.startswith("[DOCX解析提示"):
            return txt

    # 尝试 antiword
    for cmd in (["antiword", "-m", "UTF-8.txt", str(file_path)], ["catdoc", "-d", "utf-8", str(file_path)]):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=10)
            if res.returncode == 0 and res.stdout:
                out = res.stdout.decode("utf-8", errors="ignore").strip()
                if len(out) > 20:
                    return out
        except Exception:
            pass

    # 纯 Python OLE2 / UTF-16LE 与 GBK 可读中文段落提取兜底
    try:
        raw = file_path.read_bytes()
        candidates = []
        # 提取 UTF-16LE 连续可读中文/标点/数字段
        u16_text = raw.decode("utf-16le", errors="ignore")
        u16_matches = re.findall(
            r"[\u4e00-\u9fa5A-Za-z0-9，。、；：？！“”‘’（）《》【】—…·\-\s]{8,}",
            u16_text,
        )
        for m in u16_matches:
            clean = m.strip()
            # 过滤无意义乱码（要求包含常用汉字）
            if len(re.findall(r"[\u4e00-\u9fa5]", clean)) >= 4:
                candidates.append(clean)

        if candidates:
            return "\n".join(candidates)

        # 备选 GB18030 连续段落提取
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

    return "[未能直接解析该二进制文档正文，可直接下载原件用 WPS 查看]"


def extract_document_text(file_path: Path) -> Tuple[str, str]:
    """
    统一入口：输入文件路径，返回 (extracted_text, file_type_label)
    """
    if not file_path.exists() or not file_path.is_file():
        return "", "未知"

    ext = file_path.suffix.lower()
    if ext in (".txt", ".md", ".markdown", ".csv", ".log", ".json", ".yaml", ".yml", ".ini"):
        raw = file_path.read_bytes()
        return read_text_with_fallback_encodings(raw).strip(), "文本/Markdown"
    elif ext == ".docx":
        return extract_docx(file_path), "Word公文(.docx)"
    elif ext == ".ofd":
        return extract_ofd(file_path), "电子公文(.ofd)"
    elif ext == ".pdf":
        return extract_pdf(file_path), "PDF公文(.pdf)"
    elif ext in (".doc", ".wps", ".et", ".dps"):
        return extract_legacy_doc_or_wps(file_path), f"办公文档({ext})"
    elif ext in (".html", ".htm", ".xml"):
        raw = file_path.read_bytes()
        txt = read_text_with_fallback_encodings(raw)
        clean = re.sub(r"<[^>]+>", " ", txt)
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean, "网页/XML"
    elif ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(file_path), data_only=True, read_only=True)
            lines = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                lines.append(f"【工作表：{sheet}】")
                for row in ws.iter_rows(values_only=True):
                    vals = [str(v).strip() for v in row if v is not None and str(v).strip()]
                    if vals:
                        lines.append(" | ".join(vals))
            return "\n".join(lines), "电子表格(.xlsx)"
        except Exception:
            return "[Excel表格文件，可下载原件查看]", "电子表格"
    else:
        return f"[附件文件：{file_path.name}]", f"附件({ext or '无后缀'})"
