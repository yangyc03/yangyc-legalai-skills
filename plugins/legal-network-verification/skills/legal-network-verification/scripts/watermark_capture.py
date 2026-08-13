#!/usr/bin/env python3
"""Create the only retained, timestamp-watermarked page screenshot."""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


BEIJING = ZoneInfo("Asia/Shanghai")
CHINA_ID_PATTERN = re.compile(
    r"(?<!\d)(?:"
    r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]"
    r"|\d{6}\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}"
    r")(?!\d)"
)
MIN_SOURCE_WIDTH = 320
MIN_SOURCE_HEIGHT = 180
MAX_ANALYSIS_DIMENSION = 1024
VISIBLE_ALPHA_THRESHOLD = 16
MIN_VISIBLE_RATIO = 0.01
NEAR_SOLID_CHANNEL_RANGE = 8
NEAR_SOLID_CHANNEL_STDDEV = 4.0
COLOR_BUCKET_SIZE = 16
MIN_VARIANT_RATIO = 0.001
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
    return parsed.astimezone(BEIJING)


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


def verify_source_content(original: Image.Image) -> None:
    """Reject empty-looking source captures before any watermark pixels are added.

    This is a deliberately conservative heuristic gate, not an authenticity or
    cryptographic verification. Fully transparent pixels are excluded from the
    colour analysis so hidden RGB data cannot make an empty capture look valid.
    """

    width, height = original.size
    if width < MIN_SOURCE_WIDTH or height < MIN_SOURCE_HEIGHT:
        raise ValueError(
            "Source screenshot is too small for evidence: "
            f"minimum is {MIN_SOURCE_WIDTH}x{MIN_SOURCE_HEIGHT} pixels"
        )

    sample = original.convert("RGBA")
    sample.thumbnail(
        (MAX_ANALYSIS_DIMENSION, MAX_ANALYSIS_DIMENSION),
        Image.Resampling.BILINEAR,
    )
    if hasattr(sample, "get_flattened_data"):
        sampled_pixels = list(sample.get_flattened_data())
    else:  # Pillow < 12 compatibility.
        sampled_pixels = list(sample.getdata())
    visible_pixels = [
        (red, green, blue)
        for red, green, blue, alpha in sampled_pixels
        if alpha > VISIBLE_ALPHA_THRESHOLD
    ]
    if not visible_pixels:
        raise ValueError("Source screenshot is fully transparent")

    sampled_count = len(sampled_pixels)
    minimum_visible = max(256, math.ceil(sampled_count * MIN_VISIBLE_RATIO))
    if len(visible_pixels) < minimum_visible:
        raise ValueError("Source screenshot has too little visible content")

    channel_mins = [min(pixel[channel] for pixel in visible_pixels) for channel in range(3)]
    channel_maxes = [max(pixel[channel] for pixel in visible_pixels) for channel in range(3)]
    channel_ranges = [
        maximum - minimum for minimum, maximum in zip(channel_mins, channel_maxes)
    ]
    if max(channel_ranges) <= NEAR_SOLID_CHANNEL_RANGE:
        raise ValueError("Source screenshot is solid or near-solid colour")

    pixel_count = len(visible_pixels)
    channel_means = [
        sum(pixel[channel] for pixel in visible_pixels) / pixel_count for channel in range(3)
    ]
    channel_stddevs = [
        math.sqrt(
            sum(
                (pixel[channel] - channel_means[channel]) ** 2
                for pixel in visible_pixels
            )
            / pixel_count
        )
        for channel in range(3)
    ]
    if max(channel_stddevs) < NEAR_SOLID_CHANNEL_STDDEV:
        raise ValueError("Source screenshot is near-solid or near-blank")

    colour_buckets = Counter(
        tuple(channel // COLOR_BUCKET_SIZE for channel in pixel)
        for pixel in visible_pixels
    )
    dominant_count = colour_buckets.most_common(1)[0][1]
    minimum_variant = max(64, math.ceil(pixel_count * MIN_VARIANT_RATIO))
    if pixel_count - dominant_count < minimum_variant:
        raise ValueError("Source screenshot has insufficient non-background content")


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
        verify_source_content(original)
        canvas = original.convert("RGBA")
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    lines = [
        f"北京时间：{queried_at:%Y-%m-%d %H:%M:%S}｜编号：{evidence_id}",
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
    metadata.add_text("SourceContentVerified", "heuristic_pre_watermark_gate_passed")
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
