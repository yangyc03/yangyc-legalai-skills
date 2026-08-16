#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端验证：生成模拟聊天滚动视频 → 运行 video2screens → 校验覆盖/去重/PDF。"""

from __future__ import annotations

import atexit
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from print_builder import select_print_screens  # noqa: E402
W, H = 540, 960
FPS = 30
DURATION = 100
HEADER_H = 78
FOOTER_H = 70
OVERLAP = 0.75
CONTENT_TOP = 100

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]

PHRASES = [
    "好的，收到",
    "我看看文件再回复你",
    "这个方案我觉得可以",
    "稍等，我在路上",
    "麻烦发我一下链接",
    "明天上午十点开会",
    "费用明细我整理好了",
    "收到，谢谢",
    "那块内容需要再确认",
    "下午我回电话给你",
    "版本号记得更新",
    "截图已经发你邮箱了",
    "这个风险点要标注出来",
    "晚点给你反馈",
]


def load_font(size: int):
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def gen_items(n: int = 150):
    """生成内容块：消息（1~3 行）、图片、日期分隔条，行高各不相同。"""
    rng = random.Random(42)
    items = []
    y = CONTENT_TOP
    for i in range(1, n + 1):
        if i % 17 == 0:
            h = 34
            items.append({"id": f"D{i:03d}", "kind": "divider", "y0": y, "y1": y + h, "text": f"8月{i % 28 + 1}日"})
        elif i % 13 == 0:
            h = 150
            items.append({"id": f"I{i:03d}", "kind": "img", "y0": y, "y1": y + h, "who": "me" if i % 3 == 0 else "other"})
        else:
            nlines = rng.choices([1, 1, 2, 3], weights=[6, 2, 1, 1])[0]
            lines = [f"M{i:03d} {rng.choice(PHRASES)}"]
            for _ in range(nlines - 1):
                lines.append(rng.choice(PHRASES))
            h = 28 + 22 * nlines
            items.append({"id": f"M{i:03d}", "kind": "msg", "who": "me" if i % 3 == 0 else "other", "lines": lines, "y0": y, "y1": y + h})
        y += (items[-1]["y1"] - items[-1]["y0"]) + rng.randint(8, 22)
    return items


def gen_offsets(total_target: int):
    rng = random.Random(42)
    offsets = [0]
    t = 0
    total_frames = DURATION * FPS
    while offsets[-1] < total_target and t < total_frames:
        pause = int(rng.uniform(0.3, 1.0) * FPS)
        for _ in range(pause):
            offsets.append(offsets[-1])
            t += 1
            if t >= total_frames:
                break
        drag = int(rng.uniform(0.5, 1.5) * FPS)
        speed = rng.uniform(4.0, 10.0)
        for _ in range(drag):
            offsets.append(min(offsets[-1] + speed, total_target))
            t += 1
            if t >= total_frames:
                break
    while t < total_frames:
        offsets.append(offsets[-1])
        t += 1
    return offsets[: total_frames + 1]


def draw_frame(items, offset: float, frame_idx: int):
    font = load_font(16)
    font_h = load_font(20)
    font_s = load_font(12)
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)

    d.rectangle([0, 0, W, HEADER_H], fill=(237, 237, 237))
    d.text((W // 2, 20), "微信 · 项目讨论组", font=font_h, fill=(20, 20, 20), anchor="mm")
    clock = f"14:{frame_idx // FPS % 60:02d}:{frame_idx % FPS:02d}"
    d.text((W - 14, 46), clock, font=font_s, fill=(120, 120, 120), anchor="rm")

    for it in items:
        y0 = it["y0"] - offset
        y1 = it["y1"] - offset
        if y1 < HEADER_H or y0 > H - FOOTER_H:
            continue
        if it["kind"] == "divider":
            d.text((W // 2, (y0 + y1) / 2), it["text"], font=font_s, fill=(150, 150, 150), anchor="mm")
            continue
        if it["kind"] == "img":
            if it["who"] == "me":
                ax = W - 34 - 22
                bx0, bx1 = W - 36 - 190, W - 36
            else:
                ax = 22
                bx0, bx1 = 36, 36 + 190
            d.ellipse([ax, y0 + 4, ax + 26, y0 + 30], fill=(150, 180, 220))
            d.rounded_rectangle([bx0, y0, bx1, y1], radius=8, fill=(255, 255, 255), outline=(210, 210, 210), width=1)
            d.rounded_rectangle([bx0 + 10, y0 + 10, bx1 - 10, y1 - 34], radius=6, fill=(120, 150, 190))
            d.text(((bx0 + bx1) / 2, y1 - 22), "图片", font=font_s, fill=(90, 90, 90), anchor="mm")
            continue
        # 消息气泡
        who = it["who"]
        lines = it["lines"]
        max_tw = max(d.textlength(t, font=font) for t in lines)
        bw = min(max_tw + 30, W - 150)
        if who == "me":
            ax = W - 34 - 22
            x0, x1 = W - 40 - bw, W - 40
            d.ellipse([ax, y0 + 4, ax + 26, y0 + 30], fill=(220, 170, 120))
            d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=(149, 236, 105))
            tx = x1 - 14
            anchor = "rm"
        else:
            ax = 22
            x0, x1 = 40, 40 + bw
            d.ellipse([ax, y0 + 4, ax + 26, y0 + 30], fill=(120, 170, 220))
            d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=(255, 255, 255), outline=(210, 210, 210), width=1)
            tx = x0 + 14
            anchor = "lm"
        y_text = y0 + 13
        for t in lines:
            d.text((tx, y_text), t, font=font, fill=(0, 0, 0), anchor=anchor)
            y_text += 22

    d.rectangle([0, H - FOOTER_H, W, H], fill=(244, 244, 244))
    d.rounded_rectangle([12, H - FOOTER_H + 12, W - 12, H - 12], radius=8, outline=(200, 200, 200), width=2)
    return np.array(img)


def make_video(video_path: Path, offsets_csv: Path, items_csv: Path):
    items = gen_items()
    content_h = items[-1]["y1"] + 200
    total_target = content_h - H + 80
    offsets = gen_offsets(total_target)

    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    vw = cv2.VideoWriter(str(video_path), fourcc, FPS, (W, H))
    if not vw.isOpened():
        print("无法创建测试视频（缺少编码支持）")
        sys.exit(1)
    with open(offsets_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["frame_idx", "offset_px"])
        for i, off in enumerate(offsets):
            bgr = cv2.cvtColor(draw_frame(items, off, i), cv2.COLOR_RGB2BGR)
            vw.write(bgr)
            w.writerow([i, round(off, 2)])
    vw.release()
    with open(items_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "kind", "y0", "y1"])
        for it in items:
            w.writerow([it["id"], it["kind"], it["y0"], it["y1"]])
    return items, content_h


def pdf_page_count(path: Path) -> int:
    data = path.read_bytes()
    return len(re.findall(rb"/Type\s*/Page(?![s])", data))


def main():
    test_root = Path(tempfile.mkdtemp(prefix="chat_pipeline_test_"))
    atexit.register(shutil.rmtree, test_root, ignore_errors=True)
    video_path = test_root / "test_chat.mp4"
    offsets_csv = test_root / "test_offsets.csv"
    items_csv = test_root / "test_items.csv"
    out_dir = test_root / "测试输出"

    print("[1/3] 生成模拟聊天滚动视频…")
    items, content_h = make_video(video_path, offsets_csv, items_csv)
    total_target = content_h - H + 80

    print("[2/3] 运行 video2screens…")
    py = Path(sys.executable)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "成员名单.json").write_text(
        json.dumps({"right": "我这边", "left": "对方那边"}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    cmd = [
        str(py),
        str(ROOT / "video2screens.py"),
        str(video_path),
        "--out-dir",
        str(out_dir),
        "--overlap",
        str(OVERLAP),
        "--ocr",
        "tesseract",
        "--me",
        "我这边",
        "--other",
        "对方那边",
    ]
    env = dict(os.environ)
    env["CHAT_RECORDING_HOME"] = str(out_dir / ".local-runtime")
    env["CLANG_MODULE_CACHE_PATH"] = "/private/tmp/chat-recording-swift-module-cache"
    env["SWIFT_MODULECACHE_PATH"] = "/private/tmp/chat-recording-swift-module-cache"
    r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr)
        print("运行失败")
        return 1

    print("[3/3] 校验输出…")
    offsets = {}
    with open(offsets_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            offsets[int(row["frame_idx"])] = float(row["offset_px"])

    report = out_dir / ".内部数据" / "report.csv"
    rows = []
    with open(report, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    saved_frames = [int(r["帧号"]) for r in rows]
    saved_offsets = [offsets[i] for i in saved_frames]

    problems = []

    # 1) 测试视频本身必须滚到底
    if max(offsets.values()) < total_target - 100:
        problems.append(f"测试视频没滚到底（最大位移 {max(offsets.values()):.0f} < {total_target:.0f}），校验无效")

    # 2) 覆盖：每条消息/每张图片都必须至少在一张保存帧里完整可见
    with open(items_csv, newline="", encoding="utf-8") as f:
        items_data = list(csv.DictReader(f))
    missing = []
    for it in items_data:
        if it["kind"] == "divider":
            continue
        y0, y1 = int(it["y0"]), int(it["y1"])
        if not any(o + 4 <= y0 and y1 <= o + H - 4 for o in saved_offsets):
            missing.append(it["id"])
    if missing:
        problems.append(f"有 {len(missing)} 个内容块未被任何截图完整覆盖，如 {missing[:8]}")

    # 3) 去重：相邻截图既不能几乎重复，也不能位移过大
    too_close = sum(1 for a, b in zip(saved_offsets, saved_offsets[1:]) if b - a < 2)
    max_gap = (1 - OVERLAP) * H + 80
    too_far = sum(1 for a, b in zip(saved_offsets, saved_offsets[1:]) if b - a > max_gap)
    if too_close:
        problems.append(f"存在 {too_close} 对几乎重复的相邻截图")
    if too_far:
        problems.append(f"存在 {too_far} 对位移过大的相邻截图（可能漏内容）")

    # 4) 可打印 PDF 页数与内部筛选结果一致
    pdf = out_dir / "02_可打印PDF" / "聊天记录（可打印）.pdf"
    selected_rows = select_print_screens(out_dir / "01_截图", rows, 0.80, 0.65)
    sel = [int(row["帧号"]) for row in selected_rows]
    if not pdf.exists():
        problems.append("可打印 PDF 未生成")
    else:
        n_pdf = pdf_page_count(pdf)
        if n_pdf != len(sel):
            problems.append(f"PDF 页数 {n_pdf} 与筛选页数 {len(sel)} 不一致")

    # 5) Word 文字稿：对外 Word 存在；内部 Markdown 仍供不重跑再生成使用。
    md = out_dir / ".内部数据" / "聊天记录.md"
    docx = out_dir / "03_文字稿" / "聊天记录.docx"
    if not docx.exists():
        problems.append("Word 文字稿未生成")
    if not md.exists():
        problems.append("内部文字稿未生成")
    else:
        md_text = md.read_text(encoding="utf-8")
        # Tesseract 偶尔会省略测试编号中的前导 0，但消息正文仍已识别，因此允许 M12/M012 两种形式。
        ids = set(re.findall(r"M[O0]?\d{2,3}", md_text))
        msg_count = sum(1 for x in items_data if x["kind"] == "msg")
        if len(ids) < msg_count * 0.5:
            problems.append(f"MD 中识别到 {len(ids)}/{msg_count} 条消息标记，低于 50%")
        if "- 对话双方：右侧=我这边；左侧=对方那边" not in md_text:
            problems.append("MD 缺少已确认的双方身份标注")
        if not re.search(r"^## ", md_text, re.M):
            problems.append("MD 缺少日期/章节标题")

    # 6) 可打印 PDF：覆盖连续且已起到精简作用
    if pdf.exists():
        print_offsets = [offsets[i] for i in sel]
        missing_print = []
        for it in items_data:
            if it["kind"] in ("divider", "img"):
                # 打印版保证文字消息完整可读；超高图片以完整版（01_截图/汇总PDF）为准
                continue
            y0, y1 = int(it["y0"]), int(it["y1"])
            if not any(o + 4 <= y0 and y1 <= o + H - 4 for o in print_offsets):
                missing_print.append(it["id"])
        if missing_print:
            problems.append(f"打印版有 {len(missing_print)} 个内容块未被覆盖，如 {missing_print[:8]}")
        gaps_print = [b - a for a, b in zip(print_offsets, print_offsets[1:])]
        if gaps_print and max(gaps_print) > 0.9 * H + 10:
            problems.append(f"打印版相邻页间距过大（最大 {max(gaps_print):.0f}px），消息可能跨页截断")
        if len(sel) > len(rows) * 0.9:
            problems.append(f"打印版页数偏多（{len(sel)}/{len(rows)}），未起到精简作用")

    if len(rows) < max(4, len(items_data) // 18):
        problems.append(f"截图数偏少（{len(rows)} 张），测试可能没滚完内容")

    # 7) 截图保存完整稳定页；相邻打印页重合度受限
    kept_png = len(list((out_dir / "01_截图").glob("*.png")))
    if kept_png != len(rows):
        problems.append(f"01_截图数量 {kept_png} 与有效稳定页 {len(rows)} 不一致")

    def _coarse(p):
        return cv2.resize(cv2.imread(str(p), cv2.IMREAD_GRAYSCALE), (48, 104), interpolation=cv2.INTER_AREA)

    sel_names = [row["文件名"] for row in selected_rows]
    max_ov = 0.0
    for a, b in zip(sel_names, sel_names[1:]):
        x, y = _coarse(out_dir / "01_截图" / a), _coarse(out_dir / "01_截图" / b)
        d = np.abs(x.astype(np.int16) - y.astype(np.int16))
        max_ov = max(max_ov, 1.0 - np.count_nonzero(d > 25) / d.size)
    if max_ov > 0.92:
        problems.append(f"打印版相邻页最大重合度 {max_ov:.0%} 超过上限")

    gaps = [b - a for a, b in zip(saved_offsets, saved_offsets[1:])]
    print(f"  截图 {len(rows)} 张，消息/图片块 {sum(1 for x in items_data if x['kind'] != 'divider')} 个")
    print(f"  相邻截图位移：中位 {sorted(gaps)[len(gaps) // 2]:.0f} px，范围 {min(gaps, default=0):.0f} ~ {max(gaps, default=0):.0f} px")
    if problems:
        print("校验结果：FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print("校验结果：PASS（不漏、不重复、PDF 页数正确）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
