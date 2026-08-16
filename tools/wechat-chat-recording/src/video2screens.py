#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
video2screens —— 把“滚动播放的聊天录屏”抽成逐屏清晰截图，并汇总成带页码的 PDF。

核心思路：
  逐帧测量画面滚动的位移（相位相关，亚像素精度）并累加：
  - 累计滚动不足一屏（默认滚走约 25% 屏幕）→ 不保存；
  - 滚够一屏 → 把这一小段里“推进最远且足够清晰”的一帧保存下来。
  相邻两张截图始终有重叠，内容不会漏；同一屏停多久也只留一张，不会刷屏；
  静止时如果底部/顶部原地冒出新内容（新消息、加载更早记录），也会单独存一张。

用法：
  python3 video2screens.py 输入视频.mp4
  python3 video2screens.py 输入视频.mp4 --overlap 0.80 --out-dir 我的输出
"""

from __future__ import annotations

import argparse
import atexit
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from voice_pipeline import run_narrator_pipeline, run_voice_pipeline, voice_preflight
from page_classifier import classify_rows, write_page_events

VERSION = "2.2.0"
_TEMP_DIRS: list[Path] = []
_ACTIVE_STAGE_ROOT: Path | None = None


def _cleanup_temporary_paths() -> None:
    """进程结束或异常时清理仅供本次解码/发布使用的临时文件。"""
    global _ACTIVE_STAGE_ROOT
    if _ACTIVE_STAGE_ROOT is not None and _ACTIVE_STAGE_ROOT.exists():
        shutil.rmtree(_ACTIVE_STAGE_ROOT, ignore_errors=True)
    _ACTIVE_STAGE_ROOT = None
    while _TEMP_DIRS:
        shutil.rmtree(_TEMP_DIRS.pop(), ignore_errors=True)


atexit.register(_cleanup_temporary_paths)

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, seconds)
    ms = int(round((seconds - int(seconds)) * 1000))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def load_font(size: int):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def sharpness_score(gray):
    """清晰度：拉普拉斯方差，越大越清晰。"""
    small = cv2.resize(gray, (0, 0), fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(small, cv2.CV_64F).var())


def overlap_ratio(anchor_small, cur_small, pixel_thresh: int) -> float:
    """两张缩略图的重合度：1.0 完全相同，0.0 完全不同。"""
    diff = cv2.absdiff(anchor_small, cur_small)
    changed = int(np.count_nonzero(diff > pixel_thresh))
    return 1.0 - changed / float(anchor_small.size)


def make_hann(size):
    """汉宁窗：抑制画面边缘对相位相关的影响。"""
    return cv2.createHanningWindow(size, cv2.CV_64F)


def frame_shift(prev_float, cur_float, hann):
    """测量相邻两帧的滚动位移（小图坐标，像素，亚像素精度）。

    返回 (dx, dy, 匹配可信度)。dx/dy 为累计移动量，可信度接近 1 表示两帧确为同一画面
    平移关系；明显偏低表示画面发生了跳变（换聊天、切屏等）。
    """
    a = prev_float * hann
    b = cur_float * hann
    (dx, dy), resp = cv2.phaseCorrelate(a, b)
    return -float(dx), -float(dy), float(resp)


def band_overlap(anchor_small, cur_small, y0_frac: float, y1_frac: float, pixel_thresh: int) -> float:
    """比较两张小图同一横向区域（如顶部/底部边缘）的重合度。"""
    h = anchor_small.shape[0]
    a = anchor_small[int(h * y0_frac) : int(h * y1_frac), :]
    b = cur_small[int(h * y0_frac) : int(h * y1_frac), :]
    return overlap_ratio(a, b, pixel_thresh)


def cluster_add(cluster, idx: int, ts: float, frame, small, sc: float, accum_y: float, dist_y: float, accum_x: float, dist_x: float):
    if cluster is None:
        return {
            "max_sc": sc,
            "latest": (idx, ts, frame, small, sc, accum_y, dist_y, accum_x, dist_x),
            "progress": (idx, ts, frame, small, sc, accum_y, dist_y, accum_x, dist_x),
        }
    c = cluster
    if sc > c["max_sc"]:
        c["max_sc"] = sc
    if sc >= 0.5 * c["max_sc"]:
        c["latest"] = (idx, ts, frame, small, sc, accum_y, dist_y, accum_x, dist_x)
        if dist_y >= c["progress"][6]:
            c["progress"] = (idx, ts, frame, small, sc, accum_y, dist_y, accum_x, dist_x)
    return c


def cluster_pick(cluster):
    return cluster["progress"] or cluster["latest"]


def parse_args():
    p = argparse.ArgumentParser(
        prog="video2screens",
        description="把滚动的聊天录屏整理为截图、可打印 PDF 和文字稿。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python3 video2screens.py 聊天录屏.mp4\n"
            "  python3 video2screens.py 聊天录屏.mp4 --overlap 0.80\n"
            "  python3 video2screens.py 聊天录屏.mp4 --start 10 --end 300 --out-dir 输出\n"
        ),
    )
    p.add_argument("video", help="输入视频文件路径（.mp4/.mov/.m4v 等）")
    p.add_argument("-o", "--out-dir", default=None, help="输出目录（默认：视频同目录下“<视频名>_截图”）")
    p.add_argument(
        "--overlap",
        type=float,
        default=argparse.SUPPRESS,
        help="与上一张截图保持的画面重合度阈值（0.50~0.95，默认 0.75）。"
        "越大相邻截图越密、越保险；越小张数越少。",
    )
    p.add_argument("--diff-size", type=int, default=192, help="快速比对用的缩略图宽度（默认 192），一般无需修改")
    p.add_argument("--pixel-thresh", type=int, default=14, help="判定像素发生变化的灰度差阈值（默认 14），一般无需修改")
    p.add_argument("--start", type=float, default=0.0, help="只处理从第 N 秒开始（默认 0）")
    p.add_argument("--end", type=float, default=0.0, help="只处理到第 N 秒（默认 0=处理到结尾）")
    p.add_argument("--page-size", choices=["a4", "letter"], default=argparse.SUPPRESS, help="PDF 纸张大小（默认 A4）")
    p.add_argument("--no-contact-sheet", action="store_true", help="兼容旧参数；默认不再生成总览拼图")
    p.add_argument(
        "--ocr",
        choices=["auto", "vision", "tesseract", "none"],
        default=argparse.SUPPRESS,
        help="文字识别引擎：auto=自动探测本机可用引擎（默认），vision=macOS 自带 Vision，"
        "tesseract=已安装的 Tesseract，none=不识别文字。也可用环境变量 OCR_BACKEND 指定。",
    )
    p.add_argument("--ocr-langs", default=argparse.SUPPRESS, help="识别语言（默认 zh-Hans,en-US）")
    p.add_argument("--me", default=argparse.SUPPRESS, help="右侧对话人姓名（默认“我”；如右侧不是本人请指定）")
    p.add_argument("--other", default=argparse.SUPPRESS, help="左侧对话人姓名（默认“对方”；可自动识别标题栏，识别不到可手动指定）")
    p.add_argument("--attrib-llm", action="store_true", default=argparse.SUPPRESS, help="启用 LLM 归属后处理（需配置 LLM_BASE_URL/LLM_MODEL，或 --attrib-* 参数）")
    p.add_argument("--attrib-base-url", default=argparse.SUPPRESS, help="LLM 接口地址（OpenAI 兼容，如 http://127.0.0.1:11434/v1）")
    p.add_argument("--attrib-model", default=argparse.SUPPRESS, help="LLM 模型名")
    p.add_argument("--attrib-key", default=argparse.SUPPRESS, help="LLM API 密钥（只在本次运行内使用，不写入配置）")
    p.add_argument("--voice", choices=["auto", "off"], default=argparse.SUPPRESS, help="本地语音转写：auto=检测到音轨时尝试（默认），off=关闭")
    p.add_argument(
        "--asr-model",
        choices=["small", "medium"],
        default=argparse.SUPPRESS,
        help="MacWhisper 不可用时的本地 whisper.cpp 备用模型（默认 small）",
    )
    p.add_argument("--asr-language", default=argparse.SUPPRESS, help="语音主要语言（默认 zh）")
    p.add_argument("--audio-track", default=argparse.SUPPRESS, help="音轨：auto 或 ffprobe 显示的音轨编号")
    p.add_argument("--keep-voice-audio", action="store_true", default=argparse.SUPPRESS, help="仅排查问题时保留已轻度降噪的核对音频")
    p.add_argument(
        "--narrator",
        choices=["auto", "off"],
        default=argparse.SUPPRESS,
        help="录制人补充说明：auto=转写非微信语音播放时段的环境人声（默认关闭），off=关闭",
    )
    p.add_argument(
        "--narrator-name",
        default=argparse.SUPPRESS,
        help="录制人补充说明显示的姓名；留空时固定显示“录制人补充说明”，不会猜测姓名",
    )
    p.add_argument(
        "--print-threshold",
        type=float,
        default=argparse.SUPPRESS,
        help="可打印 PDF 分页阈值：相对上一页新增满多少屏高才另起一页（0.10~0.95，默认 0.80）。"
        "越大打印页越少；全部有效稳定页面仍保留在截图目录。",
    )
    p.add_argument(
        "--print-max-overlap",
        type=float,
        default=argparse.SUPPRESS,
        help="打印版相邻两页的画面重合度上限（0.50~0.95，默认 0.65）：超过则删除后一页，保证每页有足够新内容",
    )
    p.add_argument("--no-print", action="store_true", help="不生成可打印 PDF")
    p.add_argument(
        "--preview",
        nargs="?",
        const=60,
        type=int,
        default=None,
        metavar="秒数",
        help="预检模式：只分析前 N 秒（默认 60），生成样张、候选昵称和视频信息表，不生成最终成果",
    )
    p.add_argument("--info-file", default="", help="视频信息表路径（默认：读取预检中填写的信息）")
    p.add_argument("--quiet", action="store_true", help="减少中间提示")
    p.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    return p.parse_args()


def auto_convert(video: Path):
    """用 macOS 自带工具把无法解码的视频重编码为兼容格式。"""
    avc = shutil.which("avconvert")
    if not avc:
        return None
    tmpdir = Path(tempfile.mkdtemp(prefix="v2s_"))
    _TEMP_DIRS.append(tmpdir)
    out = tmpdir / (video.stem + "_conv.mov")
    print("视频编码无法直接解码，正在用系统工具转换为兼容格式（可能需要一点时间）…")
    try:
        r = subprocess.run(
            [avc, "--preset", "PresetHighestQuality", "--source", str(video), "--output", str(out)],
            capture_output=True,
            timeout=3600,
        )
    except Exception as e:  # noqa: BLE001
        print(f"自动转换失败：{e}")
        return None
    if r.returncode == 0 and out.exists():
        return out
    return None


def _read_first_at(cap, start: float):
    """真正跳转到 start 后读取首帧，返回（是否成功，帧，实际帧号）。"""
    if start > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000.0)
    ok, first = cap.read()
    frame_index = max(0, int(round(cap.get(cv2.CAP_PROP_POS_FRAMES))) - 1) if ok else 0
    return ok, first, frame_index


def open_video(video: Path, start: float = 0.0):
    """打开视频；打不开时依次尝试 ASCII 临时路径、系统工具重编码。

    返回 (cap, first_frame, used_path, first_frame_index)。
    """
    cap = cv2.VideoCapture(str(video))
    if cap.isOpened():
        ok, first, frame_index = _read_first_at(cap, start)
        if ok:
            return cap, first, str(video), frame_index
        cap.release()

    if any(ord(ch) > 127 for ch in str(video)):
        tmpdir = Path(tempfile.mkdtemp(prefix="v2s_"))
        _TEMP_DIRS.append(tmpdir)
        tmp = tmpdir / (video.name.encode("ascii", "ignore").decode() or "video.mp4")
        try:
            shutil.copyfile(video, tmp)
        except OSError:
            tmp = None
        if tmp is not None:
            cap = cv2.VideoCapture(str(tmp))
            if cap.isOpened():
                ok, first, frame_index = _read_first_at(cap, start)
                if ok:
                    return cap, first, str(tmp), frame_index
                cap.release()

    conv = auto_convert(video)
    if conv is not None:
        cap = cv2.VideoCapture(str(conv))
        if cap.isOpened():
            ok, first, frame_index = _read_first_at(cap, start)
            if ok:
                return cap, first, str(conv), frame_index
            cap.release()
    return None, None, None, 0


def save_one(out_frames: Path, frame_bgr, idx: int, ts: float, sc: float, disp_px: float, abs_px: float, h_disp_px: float, h_abs_px: float, writer, n: int):
    fname = f"{n:04d}.png"
    im = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    im.save(out_frames / fname, "PNG")
    writer.writerow([n, fname, idx, fmt_ts(ts), f"{sc:.1f}", f"{disp_px:.1f}", f"{abs_px:.1f}", f"{h_disp_px:.1f}", f"{h_abs_px:.1f}"])


def extract_frames(cap, first_frame, fps: float, args, out_frames: Path, writer, total: int, first_frame_index: int = 0):
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1
    diff_h = max(1, round(args.diff_size * h / w))
    hann = make_hann((args.diff_size, diff_h))

    anchor_small = None
    cluster = None
    saved = 0
    idx = 0
    sc_vals = []
    frame = first_frame
    prev_small = None
    prev_float = None
    still_count = 0
    scale_real = w / float(args.diff_size)  # 小图坐标 → 原始像素
    shift_limit = (1.0 - args.overlap) * diff_h  # 小图坐标
    paused_eps = 5.0
    min_response = 0.80
    accum = 0.0
    anchor_accum = 0.0
    accum_x = 0.0
    anchor_accum_x = 0.0

    while frame is not None:
        source_idx = first_frame_index + idx
        ts = source_idx / fps
        if args.end > 0 and ts > args.end:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (args.diff_size, diff_h), interpolation=cv2.INTER_AREA)
        sc = sharpness_score(gray)

        if anchor_small is None:
            saved += 1
            save_one(out_frames, frame, source_idx, ts, sc, 0.0, 0.0, 0.0, 0.0, writer, saved)
            anchor_small = small
            prev_small = small
            prev_float = small.astype(np.float64)
            accum = 0.0
            anchor_accum = 0.0
            sc_vals.append(sc)
        else:
            # 判断“是否连续几帧完全静止”：帧与帧之间几乎无变化才算静止。
            # 这个判断必须在每个分支前更新，滚动/跳变时也要让静止计数归零。
            frame_ov = overlap_ratio(prev_small, small, args.pixel_thresh) if prev_small is not None else 1.0
            dx, dy, resp = frame_shift(prev_float, small.astype(np.float64), hann)
            accum += dy
            accum_x += dx
            prev_small = small
            prev_float = small.astype(np.float64)
            if frame_ov >= 0.998:
                still_count += 1
            else:
                still_count = 0

            scene_changed = resp < min_response
            dist = abs(accum - anchor_accum)
            dist_x = abs(accum_x - anchor_accum_x)

            if scene_changed:
                # 画面跳变（换了聊天/切屏等）：先存这一段的最后内容，再存当前
                if cluster is not None and cluster["progress"][6] >= paused_eps:
                    pick = cluster_pick(cluster)
                    saved += 1
                    disp = pick[6] * scale_real
                    abs_px = pick[5] * scale_real
                    save_one(out_frames, pick[2], pick[0], pick[1], pick[4], disp, abs_px, pick[8] * scale_real, pick[7] * scale_real, writer, saved)
                    sc_vals.append(pick[4])
                cluster = None
                saved += 1
                save_one(out_frames, frame, source_idx, ts, sc, 0.0, accum * scale_real, dist_x * scale_real, accum_x * scale_real, writer, saved)
                anchor_small = small
                anchor_accum = accum
                anchor_accum_x = accum_x
                sc_vals.append(sc)
            elif dist >= shift_limit:
                # 滚够了：保存这一段里“推进最远且足够清晰”的一帧
                if cluster is not None:
                    pick = cluster_pick(cluster)
                else:
                    pick = (source_idx, ts, frame, small, sc, accum, dist, accum_x, dist_x)
                cluster = None
                saved += 1
                disp = pick[6] * scale_real
                abs_px = pick[5] * scale_real
                save_one(out_frames, pick[2], pick[0], pick[1], pick[4], disp, abs_px, pick[8] * scale_real, pick[7] * scale_real, writer, saved)
                anchor_small = pick[3]
                anchor_accum = pick[5]
                anchor_accum_x = pick[7]
                sc_vals.append(pick[4])
            else:
                # 只有在“连续几帧完全静止”时，才检查上下边缘是否有原地新增的内容
                # （新消息、加载更早记录）。滚动过程中永远不进入这个分支，避免误存。
                if still_count >= 3 and dist < paused_eps:
                    top_ov = band_overlap(anchor_small, small, 0.08, 0.25, args.pixel_thresh)
                    bot_ov = band_overlap(anchor_small, small, 0.72, 0.95, args.pixel_thresh)
                    if top_ov < 0.93 or bot_ov < 0.93:
                        cluster = None
                        saved += 1
                        save_one(out_frames, frame, source_idx, ts, sc, 0.0, accum * scale_real, dist_x * scale_real, accum_x * scale_real, writer, saved)
                        anchor_small = small
                        anchor_accum = accum
                        anchor_accum_x = accum_x
                        sc_vals.append(sc)
                    else:
                        cluster = cluster_add(cluster, source_idx, ts, frame, small, sc, accum, dist, accum_x, dist_x)
                else:
                    cluster = cluster_add(cluster, source_idx, ts, frame, small, sc, accum, dist, accum_x, dist_x)

        idx += 1
        if not args.quiet and idx % 500 == 0:
            print(f"  已分析 {idx} 帧，已保存 {saved} 屏（视频时间 {fmt_ts(ts)}）")
        ok, frame = cap.read()
        if not ok:
            frame = None

    if cluster is not None:
        pick = cluster_pick(cluster)
        saved += 1
        disp = pick[6] * scale_real
        abs_px = pick[5] * scale_real
        save_one(out_frames, pick[2], pick[0], pick[1], pick[4], disp, abs_px, pick[8] * scale_real, pick[7] * scale_real, writer, saved)
        sc_vals.append(pick[4])

    stats = {
        "processed": idx,
        "saved": saved,
        "sc_vals": sc_vals,
        "width": w,
        "height": h,
        "fps": fps,
    }
    return stats


def read_report(path: Path):
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)
    return rows


def write_report(path: Path, rows: list[dict]) -> None:
    """写回附带页面分类信息的报告，保持旧列并只追加新列。"""
    fields = [
        "序号", "文件名", "帧号", "视频时间", "清晰度", "距上一屏位移(像素)", "累计位移(像素)",
        "距上一屏横向位移(像素)", "累计横向位移(像素)", "页面类型", "页面置信状态", "强制保留", "保留原因",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def renumber_final_frames(frames_dir: Path, rows: list[dict], lines_map: dict | None = None):
    """正式成果只使用连续截图编号，并同步 OCR 索引。

    横向切换动画帧被剔除后，原始抽帧编号会留下空档；该编号不是证据序号，
    因此在发布前重新编号为 0001.png、0002.png……，避免造成“漏页”误解。
    """
    frames_dir = Path(frames_dir)
    mapping = {str(row["文件名"]): f"{i:04d}.png" for i, row in enumerate(rows, 1)}
    pending = []
    for old, new in mapping.items():
        if old == new:
            continue
        source = frames_dir / old
        if source.exists():
            temporary = frames_dir / f".__renumbering_{new}"
            source.rename(temporary)
            pending.append((temporary, frames_dir / new))
    for temporary, target in pending:
        temporary.rename(target)

    remapped_lines = {} if lines_map is not None else None
    for i, row in enumerate(rows, 1):
        old = str(row["文件名"])
        new = mapping[old]
        row["序号"] = str(i)
        row["文件名"] = new
        if remapped_lines is not None:
            remapped_lines[str(frames_dir / new)] = lines_map.get(str(frames_dir / old), [])
    return rows, remapped_lines, mapping


def build_pdf(frames_dir: Path, out_pdf: Path, page_size: str):
    dpi = 150
    if page_size == "a4":
        page_pt = (595.28, 841.89)
    else:
        page_pt = (612.0, 792.0)
    scale = dpi / 72.0
    page_px = (int(round(page_pt[0] * scale)), int(round(page_pt[1] * scale)))
    margin = int(round(36 * scale))
    footer_h = int(round(52 * scale))
    usable_w = page_px[0] - 2 * margin
    usable_h = page_px[1] - 2 * margin - footer_h

    font_num = load_font(int(round(12 * scale)))
    files = sorted(frames_dir.glob("*.png"))
    total = len(files)
    pages = []
    for i, fp in enumerate(files, 1):
        img = Image.open(fp).convert("RGB")
        s = min(usable_w / img.width, usable_h / img.height)
        nw = max(1, int(round(img.width * s)))
        nh = max(1, int(round(img.height * s)))
        img = img.resize((nw, nh), Image.LANCZOS)
        page = Image.new("RGB", page_px, "white")
        page.paste(img, ((page_px[0] - nw) // 2, margin))
        draw = ImageDraw.Draw(page)
        line = f"第 {i} 页 / 共 {total} 页"
        draw.text(
            (page_px[0] // 2, page_px[1] - footer_h // 2),
            line,
            font=font_num,
            fill=(70, 70, 70),
            anchor="mm",
        )
        pages.append(page)
    if pages:
        pages[0].save(out_pdf, "PDF", save_all=True, append_images=pages[1:], resolution=dpi)


def build_contact_sheet(frames_dir: Path, rows, out_png: Path):
    files = sorted(frames_dir.glob("*.png"))
    if not files:
        return
    tw, th = 260, 340
    gap = 12
    label_h = 28
    cols = 4
    sheet_w = cols * tw + (cols + 1) * gap
    n_rows = (len(files) + cols - 1) // cols
    sheet_h = n_rows * (th + label_h) + (n_rows + 1) * gap
    sheet = Image.new("RGB", (sheet_w, sheet_h), "white")
    draw = ImageDraw.Draw(sheet)
    font = load_font(15)
    for i, fp in enumerate(files):
        img = Image.open(fp).convert("RGB")
        img.thumbnail((tw, th), Image.LANCZOS)
        r, c = divmod(i, cols)
        x = gap + c * (tw + gap)
        y = gap + r * (th + label_h + gap)
        sheet.paste(img, (x + (tw - img.width) // 2, y + (th - img.height) // 2))
        ts = rows[i]["视频时间"] if i < len(rows) else ""
        draw.text((x + tw // 2, y + th + 5), f"{i + 1:04d} · {ts}", font=font, fill=(80, 80, 80), anchor="mm")
    sheet.save(out_png, "PNG")


def write_summary(out_log: Path, video: Path, used_path: str, stats: dict, elapsed: float, args, voice_summary=None, narrator_summary=None, page_summary=None, run_status="完整"):
    sc = sorted(stats["sc_vals"]) if stats["sc_vals"] else [0.0]
    low = [(fmt_ts(t), v) for t, v in []]
    lines = [
        "聊天录屏截图汇总",
        "=" * 40,
        f"输入视频：{video}",
        f"实际读取：{used_path}",
        f"视频尺寸：{stats['width']} x {stats['height']}",
        f"帧率：{stats['fps']:.2f}",
        f"分析帧数：{stats['processed']}",
        f"输出截图：{stats['saved']} 张",
        f"重合度阈值：{args.overlap:.2f}",
        f"清晰度范围：{sc[0]:.1f} ~ {sc[-1]:.1f}（平均 {sum(sc) / len(sc):.1f}）",
        f"处理耗时：{elapsed:.1f} 秒",
        f"本次成果状态：{run_status}",
    ]
    if voice_summary:
        lines += [
            f"语音转写引擎：{voice_summary.get('backend', '') or '未选定'}",
            f"语音转写模型：{voice_summary.get('model', '') or '未选定'}",
            f"语音气泡：{voice_summary.get('bubbles', 0)} 条",
            f"语音已转写：{voice_summary.get('transcribed', 0)} 条",
            f"语音待核：{voice_summary.get('pending', 0)} 条",
            f"语音未能转写：{voice_summary.get('untranscribed', 0)} 条",
            f"未关联音频片段：{voice_summary.get('unlinked_audio', 0)} 条",
            f"语音处理说明：{voice_summary.get('status', '')}",
        ]
    if narrator_summary:
        lines += [
            f"录制人补充说明：{'已开启' if narrator_summary.get('enabled') else '未开启'}",
            f"录制人补充说明段数：{narrator_summary.get('segments', 0)} 段",
            f"录制人补充说明状态：{narrator_summary.get('status', '')}",
        ]
    if page_summary:
        lines += [
            f"资料页面：{page_summary.get('context_pages', 0)} 张",
            f"横向过渡帧排除：{page_summary.get('transitions', 0)} 张",
            f"强制保留页面：{page_summary.get('mandatory_pages', 0)} 张",
        ]
    (out_log / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_preview(args, video: Path, out_root: Path) -> int:
    """预检模式：分析前 N 秒，生成样张、候选昵称和视频信息表。"""
    import shutil

    from input_form import write_template as write_info_template
    from md_builder import collect_group_names

    preview_dir = out_root / "00_预检"
    frames_dir = preview_dir / "01_截图"
    log_dir = preview_dir / "03_日志"
    frames_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    cap, first, used_path, first_frame_index = open_video(video, args.start)
    if cap is None:
        print("无法打开或解码该视频。")
        return 1
    fps = float(cap.get(cv2.CAP_PROP_FPS)) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    preview_seconds = args.preview or 60
    orig_end = args.end
    preview_limit = args.start + preview_seconds
    args.end = preview_limit if args.end <= 0 else min(args.end, preview_limit)
    actual_preview_end = args.end
    report_csv = log_dir / "report.csv"
    with open(report_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["序号", "文件名", "帧号", "视频时间", "清晰度", "距上一屏位移(像素)", "累计位移(像素)", "距上一屏横向位移(像素)", "累计横向位移(像素)"])
        stats = extract_frames(cap, first, fps, args, frames_dir, writer, total, first_frame_index)
    cap.release()
    args.end = orig_end
    rows = read_report(report_csv)
    print(f"预检：分析 {args.start:.0f}~{actual_preview_end:.0f} 秒（{stats['processed']} 帧），提取 {stats['saved']} 屏")

    auto_names = []
    if args.ocr != "none":
        from ocr import recognize_with_quality_fallback, resolve_backend, write_engine_events

        backend = resolve_backend(args.ocr)
        if backend is None:
            print("提示：未找到可用的本地 OCR 引擎，跳过昵称识别")
        else:
            fallback = resolve_backend("tesseract") if args.ocr == "auto" and backend.name == "vision" else None
            lines_map, ocr_events = recognize_with_quality_fallback(
                [frames_dir / r["文件名"] for r in rows],
                backend,
                fallback,
                args.ocr_langs,
                quiet=args.quiet,
            )
            write_engine_events(log_dir / "ocr_engine_events.jsonl", ocr_events)
            auto_names, _labels = collect_group_names(lines_map, frames_dir)
            auto_names = sorted(auto_names)
    print("候选群聊昵称：", auto_names if auto_names else "未识别到（可能是 1 对 1 聊天或文档滚动）")

    files = sorted(frames_dir.glob("*.png"))
    step = max(1, len(files) // 10)
    for i, fp in enumerate(files[::step][:10]):
        shutil.copyfile(fp, preview_dir / f"样张_{i + 1:02d}.png")

    info_file = out_root / "视频信息.json"
    if not info_file.exists():
        write_info_template(info_file, auto_names)
        print(f"已生成视频信息表：{info_file}")

    preview_voice = voice_preflight(video, args.voice, args.audio_track, args.asr_model)
    selected_audio = preview_voice.get("selected_stream") or {}
    report = [
        "# 预检报告",
        "",
        f"- 视频：{video.name}",
        f"- 分辨率：{w}x{h}，帧率 {fps:.1f}，总时长约 {total / fps / 60:.1f} 分钟",
        f"- 预检范围：{args.start:.0f}~{actual_preview_end:.0f} 秒，提取 {stats['saved']} 屏",
        f"- 音轨数量：{len(preview_voice.get('audio_streams', []))}",
        f"- 独立麦克风音轨：{'是' if preview_voice.get('has_microphone_track') else '未检测到'}",
        f"- 已选音轨：{selected_audio.get('index', '无')} {selected_audio.get('label', '')}".rstrip(),
        f"- 本地语音状态：{preview_voice['status']}",
        f"- 本地语音引擎：{preview_voice.get('backend') or '未选定'}",
        f"- 本地语音模型：{preview_voice.get('model_label') or '未选定'}",
        "",
        "## 自动识别到的群聊候选昵称",
        "",
    ]
    report.append("、".join(auto_names) if auto_names else "未识别到（可能是 1 对 1 聊天，或本段没有群聊昵称标签）")
    report += [
        "",
        "## 请确认以下信息",
        "",
        "1. 这是 1 对 1 聊天还是群聊？",
        "2. 群聊时请填写群名称；1 对 1 时填写右侧（绿色）和左侧（白色）是谁。",
        "3. 群聊成员：识别昵称 → 真实姓名",
        "4. 内容说明（可选）",
        "",
        "把答案填进 视频信息.json（与 00_预检 同目录），然后重新运行本工具（不加 --preview）即可全量处理。",
        "",
    ]
    (preview_dir / "预检报告.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"预检完成：样张与报告在 {preview_dir}")
    print("请查看样张并填写 视频信息.json 后，重新运行本工具（不加 --preview）进行全量处理。")
    return 0


def _seed_user_inputs(final_root: Path, stage_root: Path) -> None:
    """继承用户维护信息及预检依据到隐藏内部数据，不复制上一次的生成成果。"""
    if not final_root.exists():
        return
    stage_internal = stage_root / ".内部数据"
    stage_internal.mkdir(parents=True, exist_ok=True)
    for name in ("成员名单.json", "视频信息.json"):
        for source in (final_root / name, final_root / ".内部数据" / name):
            if source.is_file():
                shutil.copyfile(source, stage_internal / name)
                break
    for preview in (final_root / "00_预检", final_root / ".内部数据" / "00_预检"):
        if preview.is_dir():
            shutil.copytree(preview, stage_internal / "00_预检", dirs_exist_ok=True)
            break


def _publish_stage(stage_root: Path, final_root: Path) -> Path | None:
    """全部生成完成后原子性发布；旧成果改名保留，发布失败时自动恢复。"""
    previous = None
    if final_root.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        previous = final_root.with_name(f"{final_root.name}_上次结果_{stamp}")
        suffix = 1
        while previous.exists():
            previous = final_root.with_name(f"{final_root.name}_上次结果_{stamp}_{suffix}")
            suffix += 1
        final_root.rename(previous)
    try:
        stage_root.rename(final_root)
    except Exception:
        if previous is not None and previous.exists() and not final_root.exists():
            previous.rename(final_root)
        raise
    return previous


def main():
    args = parse_args()
    from config import apply_to_args, ensure_template

    ensure_template()
    args = apply_to_args(args)
    video = Path(args.video)
    if not video.exists():
        print(f"错误：找不到视频文件 {video}")
        return 1
    if not (0.5 <= args.overlap <= 0.95):
        print("错误：--overlap 应在 0.50 ~ 0.95 之间")
        return 1
    if not (0.10 <= args.print_threshold <= 0.95):
        print("错误：--print-threshold 应在 0.10 ~ 0.95 之间")
        return 1
    if not (0.50 <= args.print_max_overlap <= 0.95):
        print("错误：--print-max-overlap 应在 0.50 ~ 0.95 之间")
        return 1

    if args.end > 0 and args.end <= args.start:
        print("错误：--end 必须大于 --start")
        return 1

    final_root = Path(args.out_dir) if args.out_dir else video.with_name(video.stem + "_截图")
    if args.preview is not None:
        return run_preview(args, video, final_root)

    # 先完成解码和语音环境预检，再创建本次临时成果；此前不触碰旧成果。
    cap, first, used_path, first_frame_index = open_video(video, args.start)
    if cap is None:
        print("无法打开或解码该视频。")
        print("如果是 iPhone 录屏（HEVC/H.265 编码）且自动转换也失败，请先用 QuickTime Player 另存为 1080p。")
        return 1
    voice_check = voice_preflight(video, args.voice, args.audio_track, args.asr_model)
    if voice_check.get("ambiguous") and args.audio_track == "auto" and sys.stdin.isatty() and not args.quiet:
        print("检测到多条无法自动区分的音轨：")
        for stream in voice_check.get("audio_streams", []):
            print(f"  {stream['index']}：{stream.get('label') or stream.get('codec_name') or '未命名音轨'}")
        chosen = input("请输入要使用的音轨编号（直接回车则本次不转写语音）：").strip()
        if chosen:
            args.audio_track = chosen
            voice_check = voice_preflight(video, args.voice, args.audio_track, args.asr_model)
    narrator_check = voice_check
    if args.narrator == "auto" and not voice_check.get("ready"):
        # 即使用户关闭微信语音识别，录制人说明仍可独立使用同一套本地模型。
        narrator_check = voice_preflight(video, "auto", args.audio_track, args.asr_model)
    if not args.quiet:
        print(f"  语音预检：{voice_check['status']}")

    final_root.parent.mkdir(parents=True, exist_ok=True)
    global _ACTIVE_STAGE_ROOT
    stage_root = Path(tempfile.mkdtemp(prefix=f".{final_root.name}.处理中-", dir=final_root.parent))
    _ACTIVE_STAGE_ROOT = stage_root
    _seed_user_inputs(final_root, stage_root)
    out_root = stage_root
    out_frames = out_root / "01_截图"
    out_pdf_dir = out_root / "02_可打印PDF"
    out_text_dir = out_root / "03_文字稿"
    out_log = out_root / ".内部数据"
    for d in (out_frames, out_pdf_dir, out_text_dir, out_log):
        d.mkdir(parents=True, exist_ok=True)

    t0 = time.time()

    fps = float(cap.get(cv2.CAP_PROP_FPS))
    if not fps or fps <= 0 or fps > 240:
        fps = 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if not args.quiet:
        print(f"开始处理：{video.name}")
        print(f"  尺寸 {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}，帧率 {fps:.1f}，总帧数 {total if total > 0 else '未知'}")

    report_csv = out_log / "report.csv"
    with open(report_csv, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["序号", "文件名", "帧号", "视频时间", "清晰度", "距上一屏位移(像素)", "累计位移(像素)", "距上一屏横向位移(像素)", "累计横向位移(像素)"])
        stats = extract_frames(cap, first, fps, args, out_frames, writer, total, first_frame_index)
    cap.release()

    rows = read_report(report_csv)
    pdf_path = out_pdf_dir / "聊天记录（可打印）.pdf"
    page_summary = {"context_pages": 0, "transitions": 0, "mandatory_pages": 0}

    md_path = out_log / "聊天记录.md"
    docx_path = out_text_dir / "聊天记录.docx"
    raw_path = out_log / "ocr_raw.ndjson"
    lines_map = None
    voice_summary = {
        "bubbles": 0,
        "transcribed": 0,
        "pending": 0,
        "untranscribed": 0,
        "unlinked_audio": 0,
        "status": voice_check["status"],
        "backend": voice_check.get("backend", ""),
        "model": voice_check.get("model_label", ""),
    }
    narrator_summary = {"enabled": args.narrator == "auto", "segments": 0, "status": "录制人补充说明已关闭", "failed": False}
    run_issues = []
    speakers = {k: v for k, v in {"right": args.me, "left": args.other}.items() if v}
    names_file = out_log / "成员名单.json"
    if not names_file.exists():
        names_file.write_text(
            json.dumps({"right": "", "left": "", "chat_type": "unknown", "group_name": "", "narrator_name": "", "nick_to_name": {}}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    info_file = Path(args.info_file) if args.info_file else out_log / "视频信息.json"
    if info_file.exists():
        try:
            from input_form import merge_into_names

            merge_into_names(names_file, info_file)
        except Exception as e:  # noqa: BLE001
            print(f"警告：视频信息表合并失败：{e}")
    if args.narrator_name:
        try:
            name_data = json.loads(names_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            name_data = {}
        name_data["narrator_name"] = str(args.narrator_name).strip()
        names_file.write_text(json.dumps(name_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    names_left = ""
    try:
        names_left = str(json.loads(names_file.read_text(encoding="utf-8")).get("left") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    llm_cfg = None
    if args.attrib_llm:
        from llm_attrib import build_config

        llm_cfg = build_config(args.attrib_base_url, args.attrib_model, args.attrib_key)
        if llm_cfg is None:
            print("提示：未配置 LLM 归属后处理（需要 LLM_BASE_URL 与 LLM_MODEL，或 --attrib-* 参数），已跳过")
    if args.ocr != "none":
        try:
            from ocr import recognize_with_quality_fallback, resolve_backend, write_engine_events
            from md_builder import build_markdown, detect_other_name

            backend = resolve_backend(args.ocr)
            if backend is None:
                print("提示：未找到可用的本地 OCR 引擎，跳过文字识别（可用 --ocr tesseract 指定已安装的引擎）")
                run_issues.append("未找到可用的本地 OCR 引擎")
            else:
                if not args.quiet:
                    print(f"正在识别文字（引擎：{backend.name}，共 {len(rows)} 屏）…")
                fallback_backend = (
                    resolve_backend("tesseract") if args.ocr == "auto" and backend.name == "vision" else None
                )
                lines_map, ocr_events = recognize_with_quality_fallback(
                    [out_frames / r["文件名"] for r in rows],
                    backend,
                    fallback_backend,
                    args.ocr_langs,
                    quiet=args.quiet,
                )
                write_engine_events(out_log / "ocr_engine_events.jsonl", ocr_events)
                fallback_attempts = sum(1 for event in ocr_events if event["fallback_attempted"])
                fallback_selected = sum(1 for event in ocr_events if event["selected"] == "tesseract")
                if not args.quiet and fallback_attempts:
                    print(
                        f"  OCR 质量复核：{fallback_attempts} 屏调用 Tesseract 备用识别，"
                        f"{fallback_selected} 屏采用了更完整的结果。"
                    )
                if not any(lines_map.values()):
                    raise RuntimeError("本地 OCR 未识别到任何文字")
                classified_input = []
                for row in rows:
                    tagged = dict(row)
                    tagged["_frames_dir"] = str(out_frames)
                    classified_input.append(tagged)
                rows, page_events, page_summary, classified_all = classify_rows(classified_input, lines_map)
                stats["saved"] = len(rows)
                for row in classified_all:
                    if row.get("_drop"):
                        frame = out_frames / row["文件名"]
                        if frame.exists():
                            frame.unlink()
                rows, lines_map, filename_map = renumber_final_frames(out_frames, rows, lines_map)
                for event in page_events:
                    if event.get("file") in filename_map:
                        event["file"] = filename_map[event["file"]]
                write_report(report_csv, rows)
                write_page_events(out_log / "page_events.jsonl", page_events, classified_all)
                if not args.quiet and page_summary["transitions"]:
                    print(
                        f"  页面筛选：保留 {page_summary['context_pages']} 张资料页，"
                        f"排除 {page_summary['transitions']} 张横向切换动画帧。"
                    )
                auto_left = None
                is_group_chat = any(str(row.get("页面类型") or "") == "group_info" for row in rows)
                if not args.other and not names_left and not is_group_chat:
                    detected = detect_other_name(lines_map, out_frames)
                    if detected:
                        auto_left = detected
                        if not args.quiet:
                            print(f"已从标题栏识别左侧对话人：{detected}（如不对可用 --other 或名单指定）")
                first_md = build_markdown(
                    video.name, rows, lines_map, out_frames, md_path, raw_path, speakers, names_file, llm_cfg, auto_left
                )
                voice_results, voice_summary = run_voice_pipeline(
                    video,
                    first_md.get("voice_bubbles", []),
                    out_frames,
                    out_log,
                    voice_check,
                    start=args.start,
                    end=args.end,
                    language=args.asr_language,
                    keep_audio=args.keep_voice_audio,
                )
                narrator_events, narrator_summary = run_narrator_pipeline(
                    video,
                    out_log,
                    narrator_check,
                    voice_results,
                    voice_summary.get("_asr_segments"),
                    mode=args.narrator,
                    start=args.start,
                    end=args.end,
                    language=args.asr_language,
                )
                if (
                    first_md.get("voice_bubbles")
                    and args.voice == "auto"
                    and not voice_check.get("ready")
                    and voice_check.get("status") != "视频不含音轨"
                ):
                    run_issues.append(f"语音转写未完成：{voice_check['status']}")
                if voice_summary.get("failed"):
                    run_issues.append(voice_summary["status"])
                # 使用已缓存的语音结果覆写占位稿；后续改姓名时不需重跑语音识别。
                build_markdown(
                    video.name,
                    rows,
                    lines_map,
                    out_frames,
                    md_path,
                    raw_path,
                    speakers,
                    names_file,
                    llm_cfg,
                    auto_left,
                    voice_results,
                    narrator_events,
                )
                if not md_path.exists():
                    raise RuntimeError("本地 OCR 未生成有效文字稿")
                try:
                    from docx_builder import build_docx_from_markdown

                    build_docx_from_markdown(md_path, docx_path)
                except Exception as exc:  # noqa: BLE001
                    print(f"生成 Word 文字稿失败：{exc}")
                    run_issues.append(f"生成 Word 文字稿失败：{exc}")
                if not args.quiet and not (args.other or names_left or auto_left):
                    print("提示：未识别到对话人姓名，可用 --me/--other 或名单指定后重新生成文字稿")
        except Exception as e:  # noqa: BLE001
            print(f"文字识别/生成 Markdown 失败：{e}")
            run_issues.append(f"文字识别/生成 Markdown 失败：{e}")
    else:
        voice_summary["status"] = "已按参数跳过 OCR 和语音气泡定位"

    if lines_map is None:
        for row in rows:
            row.setdefault("页面类型", "chat")
            row.setdefault("页面置信状态", "待核")
            row.setdefault("强制保留", "否")
            row.setdefault("保留原因", "未运行页面文字识别")
        rows, _unused_lines, _unused_map = renumber_final_frames(out_frames, rows)
        write_report(report_csv, rows)
        write_page_events(out_log / "page_events.jsonl", [], [])
    stats["saved"] = len(rows)

    # 对外只生成一个可打印 PDF；所有有效稳定截图均保留在 01_截图。
    print_dir = out_pdf_dir
    print_pdf = None
    print_csv = None
    if not args.no_print:
        try:
            from print_builder import build_print_version

            print_pdf, print_csv = build_print_version(
                out_frames,
                rows,
                print_dir,
                args.page_size,
                args.print_threshold,
                args.print_max_overlap,
                write_manifest=False,
                pdf_name=pdf_path.name,
            )
        except Exception as e:  # noqa: BLE001
            print(f"生成打印精简版失败：{e}")
            run_issues.append(f"生成打印精简版失败：{e}")

    elapsed = time.time() - t0
    run_status = "完整" if not run_issues else "不完整：" + "；".join(run_issues)
    write_summary(out_log, video, used_path, stats, elapsed, args, voice_summary, narrator_summary, page_summary, run_status)

    previous = _publish_stage(stage_root, final_root)
    _ACTIVE_STAGE_ROOT = None
    _cleanup_temporary_paths()
    out_root = final_root
    out_frames = out_root / "01_截图"
    out_pdf_dir = out_root / "02_可打印PDF"
    out_log = out_root / ".内部数据"
    report_csv = out_log / "report.csv"
    pdf_path = out_pdf_dir / "聊天记录（可打印）.pdf"
    md_path = out_root / ".内部数据" / "聊天记录.md"
    docx_path = out_root / "03_文字稿" / "聊天记录.docx"
    if print_pdf is not None:
        print_pdf = out_pdf_dir / print_pdf.name

    print("\n完成！")
    print(f"  共分析 {stats['processed']} 帧，输出 {stats['saved']} 张截图")
    print(f"  截图目录：{out_frames}")
    print(f"  可打印 PDF：{pdf_path}")
    if docx_path.exists():
        print(f"  Word 文字稿：{docx_path}")
    if previous is not None:
        print(f"  上一次成果已保留：{previous}")
    if run_issues:
        print("提示：本次成果已标记为不完整；内部状态已保留，旧成果未受影响。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
