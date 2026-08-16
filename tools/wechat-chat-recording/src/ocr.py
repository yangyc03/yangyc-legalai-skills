#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地 OCR 引擎封装：不写死具体识别模型。

支持：
- vision    macOS 自带 Vision 框架（离线、支持中文），通过 ocr_vision.swift 调用；
- tesseract 本机已安装的 Tesseract（需带对应语言包）；
- auto      自动探测（macOS 优先 Vision，其次 Tesseract）；
- none      关闭。

可用命令行参数 --ocr 指定，也可用环境变量 OCR_BACKEND 指定。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

SCRIPT_DIR = Path(__file__).resolve().parent

LANG_MAP = {
    "zh-Hans": "chi_sim",
    "zh-Hant": "chi_tra",
    "en-US": "eng",
    "en": "eng",
}


@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float


class VisionBackend:
    name = "vision"

    def __init__(self, swift: str):
        self.swift = swift

    def recognize(self, images, langs: str) -> dict:
        script = SCRIPT_DIR / "ocr_vision.swift"
        cmd = [self.swift, str(script), "--langs", langs] + [str(p) for p in images]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"Vision OCR 失败：{(r.stderr or r.stdout)[:500]}")
        result = {}
        for raw in r.stdout.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            key = str(Path(obj.get("file", "")))
            result[key] = [
                Line(it["text"], float(it["x"]), float(it["y"]), float(it["w"]), float(it["h"]))
                for it in obj.get("lines", [])
            ]
        return result


class TesseractBackend:
    name = "tesseract"

    def __init__(self, binary: str):
        self.binary = binary

    def recognize(self, images, langs: str) -> dict:
        tess_langs = "+".join(LANG_MAP.get(x.strip(), x.strip()) for x in langs.split(","))
        result = {}
        for img in images:
            r = subprocess.run(
                [self.binary, str(img), "stdout", "-l", tess_langs, "--psm", "6", "tsv"],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise RuntimeError(f"Tesseract OCR 失败：{(r.stderr or r.stdout)[:500]}")
            lines = _parse_tesseract_tsv(r.stdout)
            # 统一后端数据约定：Line 坐标必须是 0~1 归一化值。
            with Image.open(img) as source:
                width, height = source.size
            result[str(img)] = [
                Line(line.text, line.x / width, line.y / height, line.w / width, line.h / height)
                for line in lines
            ]
        return result


def _parse_tesseract_tsv(tsv: str):
    words = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        try:
            level, conf = int(parts[0]), float(parts[10])
        except ValueError:
            continue
        text = parts[11].strip()
        if level != 5 or conf < 40 or not text:
            continue
        words.append((int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]), text))
    words.sort(key=lambda w: (w[1], w[0]))
    lines = []
    for left, top, w, h, text in words:
        if lines and abs(top - lines[-1].y) <= 8:
            lines[-1].w = max(lines[-1].w, left + w - lines[-1].x)
            lines[-1].h = max(lines[-1].h, top + h - lines[-1].y)
            lines[-1].text += " " + text
        else:
            lines.append(Line(text, float(left), float(top), float(w), float(h)))
    return lines


def resolve_backend(choice: str):
    """按指定或环境变量选择可用引擎；返回 None 表示没有可用引擎。"""
    if choice == "auto":
        choice = os.environ.get("OCR_BACKEND", "auto")
    if choice == "vision":
        swift = shutil.which("swift")
        return VisionBackend(swift) if swift else None
    if choice == "tesseract":
        tess = shutil.which("tesseract")
        return TesseractBackend(tess) if tess else None
    if choice == "none":
        return None
    if sys.platform == "darwin":
        swift = shutil.which("swift")
        if swift:
            return VisionBackend(swift)
    tess = shutil.which("tesseract")
    if tess:
        return TesseractBackend(tess)
    return None


def recognize_all(images, backend, langs: str, quiet: bool = False) -> dict:
    images = [Path(p) for p in images]
    if backend.name == "tesseract":
        result = {}
        for i, img in enumerate(images, 1):
            if not quiet:
                print(f"  OCR {i}/{len(images)} {img.name}")
            result[str(img)] = backend.recognize([img], langs).get(str(img), [])
        return result
    # 长录屏可能先提取数百张候选画面。一次把所有路径交给 Swift/Vision，
    # 会长时间没有进度反馈，且容易占满图像识别资源；分批保持同等识别质量。
    batch_size = 24
    result = {}
    for start in range(0, len(images), batch_size):
        batch = images[start:start + batch_size]
        if not quiet:
            end = start + len(batch)
            print(f"  Vision OCR {start + 1}-{end}/{len(images)}")
        result.update(backend.recognize(batch, langs))
    return result


def ocr_quality(lines: list[Line]) -> dict:
    """用与语言无关的特征评估 OCR 结果，只用于决定是否调用本机备用引擎。"""
    text = "".join(str(line.text or "") for line in lines)
    compact = "".join(ch for ch in text if not ch.isspace())
    useful = sum(1 for ch in compact if unicodedata.category(ch)[:1] in {"L", "N"})
    suspicious = sum(1 for ch in compact if ch in "�□■◯☐" or unicodedata.category(ch) == "Cc")
    useful_ratio = useful / max(1, len(compact))
    score = (
        0.55 * min(1.0, len(compact) / 30.0)
        + 0.35 * useful_ratio
        + 0.10 * min(1.0, len(lines) / 3.0)
        - 0.15 * min(1.0, suspicious / max(1, len(compact)))
    )
    reason = ""
    if not compact:
        reason = "未识别到文字"
    elif len(compact) < 8 and len(lines) <= 1:
        reason = "识别内容过少"
    elif useful_ratio < 0.55 or suspicious:
        reason = "识别结果疑似乱码"
    return {
        "score": round(max(0.0, score), 4),
        "characters": len(compact),
        "lines": len(lines),
        "useful_ratio": round(useful_ratio, 4),
        "needs_fallback": bool(reason),
        "reason": reason,
    }


def recognize_with_quality_fallback(images, primary, fallback, langs: str, quiet: bool = False) -> tuple[dict, list[dict]]:
    """
    先运行主引擎，只对结果可疑的画面运行备用引擎，并逐帧择优。

    返回（最终识别结果，逐帧引擎选择日志）。
    """
    images = [Path(path) for path in images]
    try:
        primary_map = recognize_all(images, primary, langs, quiet=quiet)
    except Exception as exc:
        if fallback is None:
            raise
        fallback_map = recognize_all(images, fallback, langs, quiet=quiet)
        events = []
        for path in images:
            key = str(path)
            quality = ocr_quality(fallback_map.get(key, []))
            events.append(
                {
                    "file": path.name,
                    "primary": primary.name,
                    "primary_quality": None,
                    "fallback_attempted": True,
                    "fallback": fallback.name,
                    "fallback_quality": quality,
                    "selected": fallback.name,
                    "reason": f"主引擎运行失败，已使用本机备用引擎：{str(exc)[:180]}",
                }
            )
        return {str(path): fallback_map.get(str(path), []) for path in images}, events
    final_map = {str(path): primary_map.get(str(path), []) for path in images}
    qualities = {str(path): ocr_quality(final_map[str(path)]) for path in images}
    retry = [path for path in images if qualities[str(path)]["needs_fallback"]] if fallback is not None else []
    fallback_map = recognize_all(retry, fallback, langs, quiet=quiet) if retry else {}
    events = []
    for path in images:
        key = str(path)
        primary_quality = qualities[key]
        fallback_lines = fallback_map.get(key, [])
        fallback_quality = ocr_quality(fallback_lines) if key in fallback_map else None
        selected = primary.name
        decision = primary_quality["reason"] or "主引擎识别质量正常"
        if fallback_quality is not None:
            improvement = fallback_quality["score"] - primary_quality["score"]
            if (primary_quality["characters"] == 0 and fallback_quality["characters"] > 0) or improvement >= 0.05:
                final_map[key] = fallback_lines
                selected = fallback.name
                decision = f"{primary_quality['reason']}，备用引擎结果更完整"
            else:
                decision = f"{primary_quality['reason']}，备用引擎未明显改善"
        events.append(
            {
                "file": path.name,
                "primary": primary.name,
                "primary_quality": primary_quality,
                "fallback_attempted": fallback_quality is not None,
                "fallback": fallback.name if fallback_quality is not None else "",
                "fallback_quality": fallback_quality,
                "selected": selected,
                "reason": decision,
            }
        )
    return final_map, events


def write_engine_events(path: Path, events: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
