#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重新生成 Word 文字稿（不重跑视频、不重新识别文字）。

用法：
  python3 regenerate_md.py <成果目录> [--me 姓名] [--other 姓名]
  python3 regenerate_md.py <父目录> [--me 姓名] [--other 姓名]   # 批量处理全部 xxx_成果

姓名来源优先级：--me/--other 命令行 > 成果目录隐藏的内部名单 > 自动识别 > 默认（我/对方）。
名单可在预检的 视频信息.json 填写；重新运行本脚本时无需重跑视频。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from PIL import Image

from llm_attrib import build_config
from md_builder import build_markdown, detect_other_name
from ocr import Line
from voice_pipeline import load_narrator_events, load_voice_results


def _internal_dir(work: Path) -> Path:
    """2.1.1 起内部识别数据隐藏；兼容读取旧版公开日志目录。"""
    hidden = work / ".内部数据"
    return hidden if hidden.exists() else work / "03_日志"


def _load(work: Path):
    frames = work / "01_截图"
    internal = _internal_dir(work)
    report = internal / "report.csv"
    raw = internal / "ocr_raw.ndjson"
    md = internal / "聊天记录.md"
    if not md.exists():  # 兼容 2.1 及更早版本的公开 Markdown 文字稿。
        md = work / "03_文字稿" / "聊天记录.md"
    if not md.exists():
        md = work / "04_文字稿" / "聊天记录.md"
    if not (frames.exists() and report.exists() and raw.exists()):
        return None
    rows = []
    with open(report, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    first_frame = next(
        (frames / row["文件名"] for row in rows if (frames / row["文件名"]).exists()),
        None,
    )
    if first_frame is None:
        first_frame = next((work / "06_完整截图备份" / row["文件名"] for row in rows if (work / "06_完整截图备份" / row["文件名"]).exists()), None)
    if first_frame is None:
        return None
    w_im, h_im = Image.open(first_frame).size
    lines_map = {}
    with open(raw, encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            key = str(frames / obj["file"])
            lines_map[key] = [
                Line(it["text"], it["x"] / w_im, it["y"] / h_im, it["w"] / w_im, it["h"] / h_im)
                for it in obj["lines"]
            ]
    return rows, lines_map, md


def main():
    from config import apply_to_args, ensure_template

    ensure_template()
    p = argparse.ArgumentParser(description="重新生成 Word 文字稿（使用已有识别结果，不重跑视频）")
    p.add_argument("target", help="成果目录（xxx_成果）或其父目录")
    p.add_argument("--me", default=argparse.SUPPRESS, help="右侧对话人姓名（绿色气泡一侧）")
    p.add_argument("--other", default=argparse.SUPPRESS, help="左侧对话人姓名（白色气泡一侧）")
    p.add_argument("--attrib-llm", action="store_true", default=argparse.SUPPRESS, help="启用 LLM 归属后处理")
    p.add_argument("--attrib-base-url", default=argparse.SUPPRESS, help="LLM 接口地址（OpenAI 兼容）")
    p.add_argument("--attrib-model", default=argparse.SUPPRESS, help="LLM 模型名")
    p.add_argument("--attrib-key", default="", help="LLM API 密钥")
    args = p.parse_args()
    args = apply_to_args(args)

    target = Path(args.target)
    if (target / "01_截图").exists():
        works = [target]
    else:
        works = sorted(target.glob("*_成果"))
    if not works:
        print("未找到成果目录：", target)
        return 1

    speakers = {k: v for k, v in {"right": args.me, "left": args.other}.items() if v}
    llm_cfg = None
    if args.attrib_llm:
        llm_cfg = build_config(args.attrib_base_url, args.attrib_model, args.attrib_key)
        if llm_cfg is None:
            print("提示：未配置 LLM（需要 LLM_BASE_URL 与 LLM_MODEL），已跳过归属后处理")
    for w in works:
        loaded = _load(w)
        if loaded is None:
            print("跳过（缺少报告或识别数据）：", w.name)
            continue
        rows, lines_map, md = loaded
        internal = _internal_dir(w)
        names_file = internal / "成员名单.json"
        if not names_file.exists():
            names_file.write_text(
                json.dumps({"right": "", "left": "", "chat_type": "unknown", "group_name": "", "narrator_name": "", "nick_to_name": {}}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        info_file = internal / "视频信息.json"
        if info_file.exists():
            try:
                from input_form import merge_into_names

                merge_into_names(names_file, info_file)
            except Exception:  # noqa: BLE001
                pass
        names_left = ""
        names_right = ""
        data = {}
        try:
            data = json.loads(names_file.read_text(encoding="utf-8"))
            names_left = str(data.get("left") or "").strip()
            names_right = str(data.get("right") or "").strip()
        except Exception:  # noqa: BLE001
            pass
        detected = None
        is_group_chat = str(data.get("chat_type") or "").lower() in {"group", "群聊"} or any(
            str(row.get("页面类型") or "") == "group_info" for row in rows
        )
        if not args.other and not names_left and not is_group_chat:
            detected = detect_other_name(lines_map, w / "01_截图")
            if detected:
                print(f"[{w.name}] 已识别左侧对话人：{detected}")
        voice_results = load_voice_results(internal / "voice_events.jsonl")
        narrator_events = load_narrator_events(internal / "narrator_events.jsonl")
        build_markdown(
            w.name.replace("_成果", ".mp4"),
            rows,
            lines_map,
            w / "01_截图",
            md,
            internal / "ocr_raw.ndjson",
            speakers,
            names_file,
            llm_cfg,
            detected,
            voice_results,
            narrator_events,
        )
        try:
            from docx_builder import build_docx_from_markdown

            build_docx_from_markdown(md, w / "03_文字稿" / "聊天记录.docx")
        except Exception as exc:  # noqa: BLE001
            print(f"[{w.name}] Word 文字稿生成失败：{exc}")
            continue
        right_disp = speakers.get("right") or names_right or "我"
        left_disp = speakers.get("left") or names_left or detected or "对方"
        print(f"[{w.name}] Word 文字稿已重新生成（右侧={right_disp}，左侧={left_disp}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
