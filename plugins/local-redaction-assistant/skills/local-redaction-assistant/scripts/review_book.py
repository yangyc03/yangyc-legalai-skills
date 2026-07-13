#!/usr/bin/env python3
"""Build a safe-ID-only batch review PDF book from Word-native PDFs."""

from __future__ import annotations

import tempfile
from pathlib import Path


class ReviewBookError(RuntimeError):
    pass


def _dependencies():
    try:
        from pypdf import PdfReader, PdfWriter
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas
    except ImportError as exc:
        raise ReviewBookError("review_book_dependencies_unavailable") from exc
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    except Exception:
        pass
    return PdfReader, PdfWriter, canvas


def ensure_review_book_dependencies() -> None:
    _dependencies()


def _one_page_pdf(path: Path, title: str, lines: list[str]) -> None:
    _PdfReader, _PdfWriter, canvas = _dependencies()
    page = canvas.Canvas(str(path), pagesize=(595.28, 841.89))
    page.setFont("STSong-Light", 22)
    page.drawString(72, 730, title)
    page.setFont("STSong-Light", 12)
    y = 680
    for line in lines:
        page.drawString(72, y, line)
        y -= 26
    page.showPage()
    page.save()


def build_review_book(items: list[tuple[str, Path]], output_path: Path) -> list[dict]:
    PdfReader, PdfWriter, _canvas = _dependencies()
    if not items:
        raise ReviewBookError("review_book_no_word_pdfs")
    for safe_id, path in items:
        if not path.is_file() or not safe_id:
            raise ReviewBookError("review_book_input_invalid")
    page_counts = {safe_id: len(PdfReader(str(path)).pages) for safe_id, path in items}
    total_body_pages = sum(page_counts.values())
    mapping: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="local-redaction-review-book-") as tmp:
        root = Path(tmp)
        cover = root / "cover.pdf"
        _one_page_pdf(
            cover,
            "法律模板脱敏副本集中复核册",
            [f"文件数：{len(items)}", f"正文总页数：{total_body_pages}", "所有文件仍须律师逐页人工复核。"],
        )
        writer = PdfWriter()
        writer.append(str(cover))
        current_page = 2
        for index, (safe_id, pdf_path) in enumerate(items, start=1):
            separator = root / f"separator-{index:03d}.pdf"
            _one_page_pdf(separator, safe_id, [f"第 {index} 份", f"正文页数：{page_counts[safe_id]}"])
            writer.append(str(separator))
            document_start = current_page + 1
            document_end = document_start + page_counts[safe_id] - 1
            mapping.append(
                {
                    "safe_id": safe_id,
                    "separator_page": current_page,
                    "document_start_page": document_start,
                    "document_end_page": document_end,
                    "document_page_count": page_counts[safe_id],
                }
            )
            writer.append(str(pdf_path))
            current_page = document_end + 1
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as handle:
            writer.write(handle)
    return mapping
