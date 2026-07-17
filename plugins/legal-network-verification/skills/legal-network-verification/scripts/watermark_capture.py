#!/usr/bin/env python3
"""Create the only retained, timestamp-watermarked page screenshot."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


CHINA_ID_PATTERN = re.compile(
    r"(?<!\d)(?:\d{17}[0-9Xx]|\d{8}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("queried-at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("queried-at must include a timezone offset")
    return parsed


def reject_sensitive_value(value: str, label: str) -> None:
    if CHINA_ID_PATTERN.search(str(value)):
        raise ValueError(f"{label} must not contain a full identity number")


def choose_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default(size=size)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def fit_font(draw: ImageDraw.ImageDraw, lines: list[str], width: int) -> ImageFont.ImageFont:
    size = max(16, min(30, width // 48))
    max_width = int(width * 0.92)
    while size > 12:
        font = choose_font(size)
        if max(text_width(draw, line, font) for line in lines) <= max_width:
            return font
        size -= 1
    return choose_font(12)


def add_watermark(
    input_path: Path,
    output_path: Path,
    *,
    evidence_id: str,
    subject: str,
    source: str,
    queried_at: datetime,
    overwrite: bool = False,
) -> None:
    if not input_path.is_file():
        raise FileNotFoundError(f"Input screenshot not found: {input_path}")
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Output must not overwrite the source screenshot")
    if output_path.suffix.lower() != ".png":
        raise ValueError("Output screenshot must use .png extension")
    if output_path.is_symlink():
        raise ValueError("Output screenshot must not be a symbolic link")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"Output screenshot already exists: {output_path}")
    for label, value in (("evidence-id", evidence_id), ("subject", subject), ("source", source)):
        if not str(value).strip():
            raise ValueError(f"{label} is required")
        reject_sensitive_value(str(value), label)

    with Image.open(input_path) as original:
        canvas = original.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    lines = [
        f"查询时间：{queried_at.isoformat(timespec='seconds')}｜编号：{evidence_id}",
        f"查询对象：{subject}｜来源：{source}",
    ]
    font = fit_font(draw, lines, canvas.width)
    line_boxes = [draw.textbbox((0, 0), line, font=font) for line in lines]
    line_heights = [box[3] - box[1] for box in line_boxes]
    line_widths = [box[2] - box[0] for box in line_boxes]
    padding = max(8, canvas.width // 160)
    spacing = max(4, padding // 2)
    panel_width = max(line_widths) + padding * 2
    panel_height = sum(line_heights) + spacing + padding * 2
    left = max(0, canvas.width - panel_width - padding)
    top = max(0, canvas.height - panel_height - padding)
    right = canvas.width - padding
    bottom = canvas.height - padding
    draw.rounded_rectangle(
        (left, top, right, bottom),
        radius=max(6, padding),
        fill=(255, 255, 255, 210),
        outline=(90, 90, 90, 150),
        width=max(1, padding // 4),
    )
    y = top + padding
    for line, height in zip(lines, line_heights):
        x = right - padding - text_width(draw, line, font)
        draw.text((x, y), line, font=font, fill=(45, 45, 45, 235))
        y += height + spacing

    result = Image.alpha_composite(canvas, overlay).convert("RGB")
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("EvidenceId", evidence_id)
    metadata.add_text("QueriedAt", queried_at.isoformat())
    metadata.add_text("Subject", subject)
    metadata.add_text("Source", source)
    metadata.add_text("CaptureKind", "watermarked_page_only")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        result.save(temporary_path, format="PNG", pnginfo=metadata, optimize=True)
        os.replace(temporary_path, output_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Add a legal-workpaper timestamp watermark to a screenshot.")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--queried-at", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        add_watermark(
            args.input,
            args.output,
            evidence_id=args.evidence_id,
            subject=args.subject,
            source=args.source,
            queried_at=parse_timestamp(args.queried_at),
            overwrite=args.overwrite,
        )
        print(args.output)
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
