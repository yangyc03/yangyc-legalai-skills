#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按文件夹批量处理聊天录屏，并汇总每段视频的 PDF 与 Word 文稿。

所有视频仍逐段生成独立成果，避免跨视频滚动位置、日期或说话人被混在一起；
案件级成员表只共享用户确认的姓名和昵称映射，未知头像成员不自动跨视频合并。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

from input_form import template

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv"}


def _videos(folder: Path) -> list[Path]:
    return sorted(
        item for item in folder.iterdir()
        if item.is_file() and item.suffix.lower() in VIDEO_EXTENSIONS and not item.name.startswith(".")
    )


def _ensure_case_members(path: Path) -> None:
    if path.exists():
        return
    data = template()
    data["_说明"] = "本表适用于本次批量中的全部视频：填写已确认的右侧/左侧姓名、群名和成员昵称映射。未知头像成员仍在每段视频成果中单独核对，避免错认。"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _copy_summary(result_root: Path, summary_root: Path, stem: str) -> tuple[bool, str]:
    pdf = result_root / "02_可打印PDF" / "聊天记录（可打印）.pdf"
    docx = result_root / "03_文字稿" / "聊天记录.docx"
    if not pdf.exists() or not docx.exists():
        missing = "、".join(name for path, name in ((pdf, "PDF"), (docx, "Word")) if not path.exists())
        return False, f"缺少{missing}"
    pdf_dir = summary_root / "01_可打印PDF"
    docx_dir = summary_root / "02_文字稿（Word）"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    docx_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf, pdf_dir / f"{stem}_聊天记录（可打印）.pdf")
    shutil.copy2(docx, docx_dir / f"{stem}_聊天记录.docx")
    return True, "已汇总"


def main() -> int:
    parser = argparse.ArgumentParser(description="批量处理一个文件夹中的聊天录屏，并生成 PDF/Word 汇总")
    parser.add_argument("folder", help="只读取该文件夹第一层内的视频文件")
    parser.add_argument("--out-dir", default="", help="输出目录；默认在所选文件夹下建立“聊天录屏工具批量成果”")
    parser.add_argument("--narrator", choices=["auto", "off"], default="off", help="可选录制人补充说明（默认关闭）")
    parser.add_argument("--narrator-name", default="", help="可选录制人姓名；留空不显示具体姓名")
    parser.add_argument("--voice", choices=["auto", "off"], default="auto", help="微信语音识别开关")
    args = parser.parse_args()

    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(f"错误：不是文件夹：{folder}")
        return 1
    videos = _videos(folder)
    if not videos:
        print("未找到可处理的视频（支持 mp4、mov、m4v、avi、mkv）。")
        return 1

    out_root = Path(args.out_dir).expanduser() if args.out_dir else folder / "聊天录屏工具批量成果"
    out_root.mkdir(parents=True, exist_ok=True)
    case_members = out_root / "成员名单.json"
    _ensure_case_members(case_members)
    print(f"共找到 {len(videos)} 个视频。案件级成员表：{case_members}")
    print("提示：首次可先停止并填写该成员表，再重新运行；未填写时工具仍会生成待核标记。")

    records = []
    for index, video in enumerate(videos, 1):
        result = out_root / f"{video.stem}_成果"
        command = [
            sys.executable,
            str(Path(__file__).with_name("video2screens.py")),
            str(video),
            "--out-dir",
            str(result),
            "--info-file",
            str(case_members),
            "--voice",
            args.voice,
            "--narrator",
            args.narrator,
        ]
        if args.narrator_name.strip():
            command += ["--narrator-name", args.narrator_name.strip()]
        print(f"[{index}/{len(videos)}] 正在处理：{video.name}")
        completed = subprocess.run(command, cwd=str(Path(__file__).parent), check=False)
        record = {"video": video.name, "result": result.name, "returncode": completed.returncode}
        if completed.returncode == 0:
            ok, detail = _copy_summary(result, out_root / "汇总", video.stem)
            record.update({"summary": detail, "ok": ok})
        else:
            record.update({"summary": "处理失败", "ok": False})
        records.append(record)

    (out_root / ".内部数据").mkdir(exist_ok=True)
    (out_root / ".内部数据" / "批处理结果.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    complete = sum(1 for item in records if item.get("ok"))
    print(f"批量完成：{complete}/{len(records)} 段已汇总。汇总目录：{out_root / '汇总'}")
    return 0 if complete == len(records) else 1


if __name__ == "__main__":
    raise SystemExit(main())
