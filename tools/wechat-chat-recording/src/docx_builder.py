#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把内部 Markdown 文字稿导出为可打印的 Word 文稿。

Markdown 仅作为可再生成的内部中间稿；对用户展示的文字稿统一为 DOCX。
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt


def _set_fonts(obj, east_asia: str = "Noto Sans CJK SC", western: str = "Noto Sans CJK SC") -> None:
    """同时写入中文与西文字体槽。

    聊天转写属于工具生成的检索稿，采用本机可稳定渲染的 Noto 字体；避免没有宋体的
    Word/LibreOffice 环境把中文显示成方框。正式法律文书仍应使用其专门模板。
    """
    obj.font.name = western
    rpr = obj._element.get_or_add_rPr()
    fonts = rpr.rFonts
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    fonts.set(qn("w:eastAsia"), east_asia)
    fonts.set(qn("w:ascii"), western)
    fonts.set(qn("w:hAnsi"), western)
    fonts.set(qn("w:cs"), western)
    fonts.set(qn("w:hint"), "eastAsia")


def _add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("第 ")
    _set_fonts(run)
    # 用完整域结构，并给未立即刷新域的预览程序提供“1”作为显示兜底。
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    fallback = OxmlElement("w:t")
    fallback.text = "1"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    field_run = OxmlElement("w:r")
    field_run.append(begin)
    field_run.append(instr)
    field_run.append(separate)
    field_run.append(fallback)
    field_run.append(end)
    paragraph._p.append(field_run)
    run = paragraph.add_run(" 页")
    _set_fonts(run)


def _set_style(style, *, size: float, bold: bool = False, first_indent: bool = False, before: float = 0, after: float = 0) -> None:
    _set_fonts(style)
    style.font.size = Pt(size)
    style.font.bold = bold
    pf = style.paragraph_format
    pf.space_before = Pt(before)
    pf.space_after = Pt(after)
    pf.line_spacing = 1.5
    pf.first_line_indent = Pt(24) if first_indent else Pt(0)


def _add_message(paragraph, body: str) -> None:
    """保留“说话人 + 时间 + 内容”的检索结构，同时不让补充说明伪装成聊天消息。"""
    match = re.match(r"\*\*([^*]+)\*\*\s+(\[[^]]+\])\s*(.*)$", body)
    if match:
        speaker, timestamp, content = match.groups()
        run = paragraph.add_run(speaker)
        run.bold = True
        _set_fonts(run)
        run = paragraph.add_run(f" {timestamp} ")
        _set_fonts(run)
        run = paragraph.add_run(content)
        _set_fonts(run)
        return
    tag_match = re.match(r"(【[^】]+】)(.*)$", body)
    if tag_match:
        tag, content = tag_match.groups()
        run = paragraph.add_run(tag)
        run.bold = True
        _set_fonts(run)
        run = paragraph.add_run(content)
        _set_fonts(run)
        return
    run = paragraph.add_run(body)
    _set_fonts(run)


def build_docx_from_markdown(md_path: Path, docx_path: Path) -> Path:
    """导出 A4、宋体正文、可打印的聊天记录 Word 文稿。"""
    md_path, docx_path = Path(md_path), Path(docx_path)
    source = md_path.read_text(encoding="utf-8")
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.top_margin = section.bottom_margin = Cm(2.54)
    section.left_margin = section.right_margin = Cm(2.54)
    section.header_distance, section.footer_distance = Cm(1.5), Cm(1.75)

    normal = doc.styles["Normal"]
    _set_style(normal, size=12, first_indent=False, before=0, after=7.8)
    for name, size, before, after in (("TranscriptTitle", 18, 0, 12), ("TranscriptHeading", 14, 12, 6)):
        style = doc.styles.add_style(name, WD_STYLE_TYPE.PARAGRAPH)
        _set_style(style, size=size, bold=True, before=before, after=after)

    footer = section.footer.paragraphs[0]
    _add_page_number(footer)

    for raw in source.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("# "):
            p = doc.add_paragraph(style="TranscriptTitle")
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(line[2:].strip())
            run.bold = True
            _set_fonts(run)
            continue
        if line.startswith("## "):
            p = doc.add_paragraph(style="TranscriptHeading")
            run = p.add_run(line[3:].strip())
            run.bold = True
            _set_fonts(run)
            continue
        if line.startswith("- "):
            # 内置 List Bullet 使用真实 Word 编号定义，换行自动对齐到文本而非圆点。
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.line_spacing = 1.5
            _add_message(p, line[2:].strip())
            continue
        p = doc.add_paragraph()
        _add_message(p, line)

    docx_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(docx_path)
    return docx_path
