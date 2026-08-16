#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印精简版：从逐屏截图中按“新增内容量”挑选打印页。

规则：
- 第一屏必留；场景跳变（画面整体替换）时保留该场景第一屏；
- 同一场景内按“累计滚动位移”分页：相对上一保留页滚动前进满 threshold×屏高 才另起一页；
- 若某两页之间间距超过一整个屏高（理论上不应发生，遇边界情况），自动补一页保证连续覆盖。

原截图与完整 PDF 不受影响，本模块只额外生成精简版 PDF 与选中清单。
"""

from __future__ import annotations

import csv
import io
import os
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]

TOP_CROP = 0.10  # 顶部状态栏/标题栏
BOT_CROP = 0.08  # 底部输入栏
SCENE_OVERLAP = 0.30  # 相邻两屏画面重叠低于该值才视为真实场景跳变（内容整体替换）

_coarse_cache = {}


def load_font(size: int):
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _coarse(path: Path):
    key = str(path)
    if key not in _coarse_cache:
        img = cv2.imread(key, cv2.IMREAD_GRAYSCALE)
        _coarse_cache[key] = cv2.resize(img, (48, 104), interpolation=cv2.INTER_AREA)
    return _coarse_cache[key]


def _img_overlap(a, b) -> float:
    diff = np.abs(a.astype(np.int16) - b.astype(np.int16))
    return 1.0 - np.count_nonzero(diff > 25) / diff.size


def select_print_screens(frames_dir: Path, rows, threshold: float, max_overlap: float = 0.65):
    """按顺序挑选打印页，返回选中行（含帧号、时间、位移）。

    返回的相邻两页滚动间距始终小于一屏高，保证内容连续覆盖。
    """
    frames_dir = Path(frames_dir)
    with Image.open(frames_dir / rows[0]["文件名"]) as first_image:
        h_im = first_image.size[1]
    min_advance = threshold * h_im

    mandatory_names = {r["文件名"] for r in rows if str(r.get("强制保留") or "") == "是"}
    selected = []
    prev_img = None
    last_kept_offset = None

    for r in rows:
        name = frames_dir / r["文件名"]
        offset = float(r["累计位移(像素)"])

        scene = False
        if prev_img is not None:
            scene = _img_overlap(prev_img, _coarse(name)) < SCENE_OVERLAP

        prev_img = _coarse(name)
        if r["文件名"] in mandatory_names or last_kept_offset is None or scene or offset - last_kept_offset >= min_advance:
            selected.append(r)
            last_kept_offset = offset

    # 结尾兜底：始终保留完整集的最后一张截图，避免聊天尾部内容缺失
    if selected and selected[-1]["文件名"] != rows[-1]["文件名"]:
        selected.append(rows[-1])

    # 去重（画面非单调前进时补页可能重复选中）
    seen = set()
    dedup = []
    for r in selected:
        if r["文件名"] in seen:
            continue
        seen.add(r["文件名"])
        dedup.append(r)
    selected = dedup
    sel_names = {r["文件名"] for r in selected}

    by_name = {r["文件名"]: r for r in rows}
    off = {r["文件名"]: float(r["累计位移(像素)"]) for r in rows}
    # 交替执行“补页(间距≥0.9屏时插入)”与“内容去重(重合超上限删后页)”，直到稳定，
    # 保证既有连续覆盖，又让每页都有足够新内容。
    for _ in range(20):
        changed = False
        # 补页
        i = 0
        while i < len(selected) - 1:
            o1 = off[selected[i]["文件名"]]
            o2 = off[selected[i + 1]["文件名"]]
            if o2 - o1 >= 0.9 * h_im:
                best, best_d = None, float("inf")
                for r in rows:
                    o = off[r["文件名"]]
                    if o1 < o < o2 and r["文件名"] not in sel_names:
                        d = abs(o - (o1 + 0.75 * h_im))
                        if d < best_d:
                            best, best_d = r, d
                if best is not None:
                    selected.insert(i + 1, best)
                    sel_names.add(best["文件名"])
                    changed = True
                else:
                    i += 1
            else:
                i += 1
        # 全局内容去重：与“任意一张已保留页”重合过高即删除（处理反复回看同一内容）；
        # 位移前进很小但重合较高时按 max_overlap 删，重合极高（>90%）无条件删。
        # 删除后不得让相邻保留页间距超过 0.9 屏（保证消息不跨页丢失）。
        i = 0
        while i < len(selected) - 1:
            nxt_name = selected[i + 1]["文件名"]
            adv = off[nxt_name] - off[selected[i]["文件名"]]
            worst = 0.0
            for k in range(i + 1):
                o = _img_overlap(
                    _coarse(frames_dir / selected[k]["文件名"]),
                    _coarse(frames_dir / nxt_name),
                )
                if o > worst:
                    worst = o
            would_gap = False
            if i + 2 < len(selected):
                would_gap = off[selected[i + 2]["文件名"]] - off[selected[i]["文件名"]] >= 0.9 * h_im
            if nxt_name not in mandatory_names and ((worst > max_overlap and adv < 0.5 * h_im) or worst > 0.90) and not would_gap:
                sel_names.discard(nxt_name)
                del selected[i + 1]
                changed = True
            else:
                i += 1
        if not changed:
            break
    # 强制资料页可能在补页/去重循环中不在相邻位置，最后按原始顺序补回。
    selected_names = {r["文件名"] for r in selected}
    selected.extend(r for r in rows if r["文件名"] in mandatory_names and r["文件名"] not in selected_names)
    order = {r["文件名"]: i for i, r in enumerate(rows)}
    return sorted(selected, key=lambda r: order[r["文件名"]])


def _build_pdf(selected, frames_dir: Path, out_pdf: Path, page_size: str):
    """逐页写入 PDF，避免长录屏把全部打印页同时留在内存中。"""
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

    total = len(selected)
    if not total:
        return

    # Pillow 的多页 PDF 接口会保留 append_images 中的全部 PIL 图像。长录屏可能有上千张
    # 截图，内存会随页数线性增长并使进程被系统终止。以下为最小 PDF 写入器：每次仅保留
    # 当前页的 JPEG 字节，写入后立即释放图像对象。
    page_ids = []
    offsets: dict[int, int] = {}
    next_obj = 3  # 1=Catalog, 2=Pages；其余对象按页写入

    def write_obj(handle, number: int, body: bytes):
        offsets[number] = handle.tell()
        handle.write(f"{number} 0 obj\n".encode("ascii"))
        handle.write(body)
        handle.write(b"\nendobj\n")

    page_w_pt, page_h_pt = page_pt
    with open(out_pdf, "wb") as pdf:
        pdf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        write_obj(pdf, 1, b"<< /Type /Catalog /Pages 2 0 R >>")

        for i, r in enumerate(selected, 1):
            with Image.open(frames_dir / r["文件名"]) as source:
                img = source.convert("RGB")
            try:
                s = min(usable_w / img.width, usable_h / img.height)
                nw = max(1, int(round(img.width * s)))
                nh = max(1, int(round(img.height * s)))
                resized = img.resize((nw, nh), Image.LANCZOS)
                try:
                    page = Image.new("RGB", page_px, "white")
                    page.paste(resized, ((page_px[0] - nw) // 2, margin))
                    draw = ImageDraw.Draw(page)
                    draw.text(
                        (page_px[0] // 2, page_px[1] - footer_h // 2),
                        f"第 {i} 页 / 共 {total} 页",
                        font=font_num,
                        fill=(70, 70, 70),
                        anchor="mm",
                    )
                    encoded = io.BytesIO()
                    page.save(encoded, "JPEG", quality=90, optimize=True)
                    jpeg = encoded.getvalue()
                finally:
                    resized.close()
                    page.close()
            finally:
                img.close()

            image_obj, content_obj, page_obj = next_obj, next_obj + 1, next_obj + 2
            next_obj += 3
            image_body = (
                f"<< /Type /XObject /Subtype /Image /Width {page_px[0]} /Height {page_px[1]} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>\nstream\n"
            ).encode("ascii") + jpeg + b"\nendstream"
            write_obj(pdf, image_obj, image_body)
            content = f"q\n{page_w_pt:.2f} 0 0 {page_h_pt:.2f} 0 0 cm\n/Im{i} Do\nQ\n".encode("ascii")
            content_body = f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"endstream"
            write_obj(pdf, content_obj, content_body)
            page_body = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w_pt:.2f} {page_h_pt:.2f}] "
                f"/Resources << /XObject << /Im{i} {image_obj} 0 R >> >> /Contents {content_obj} 0 R >>"
            ).encode("ascii")
            write_obj(pdf, page_obj, page_body)
            page_ids.append(page_obj)

        kids = " ".join(f"{number} 0 R" for number in page_ids)
        write_obj(pdf, 2, f"<< /Type /Pages /Kids [{kids}] /Count {total} >>".encode("ascii"))
        xref_at = pdf.tell()
        pdf.write(f"xref\n0 {next_obj}\n".encode("ascii"))
        pdf.write(b"0000000000 65535 f \n")
        for number in range(1, next_obj):
            pdf.write(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
        pdf.write(
            f"trailer\n<< /Size {next_obj} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
        )


def build_print_version(
    frames_dir: Path,
    rows,
    out_dir: Path,
    page_size: str,
    threshold: float,
    max_overlap: float = 0.65,
    write_manifest: bool = True,
    pdf_name: str = "打印精简版.pdf",
):
    """生成可打印 PDF；需要技术核对时才额外写入截图清单。"""
    frames_dir = Path(frames_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = select_print_screens(frames_dir, rows, threshold, max_overlap)
    pdf_path = out_dir / pdf_name
    csv_path = out_dir / "打印版截图清单.csv" if write_manifest else None

    if csv_path is not None:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["序号", "原文件名", "帧号", "视频时间", "累计位移(像素)"])
            for i, r in enumerate(selected, 1):
                writer.writerow([i, r["文件名"], r["帧号"], r["视频时间"], r["累计位移(像素)"]])

    _build_pdf(selected, frames_dir, pdf_path, page_size)
    return pdf_path, csv_path
