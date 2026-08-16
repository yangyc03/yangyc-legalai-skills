#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通过双击入口修改姓名并重新生成已有 Word 文字稿，不重跑视频。"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


def _clean_path(value: str) -> Path:
    value = value.strip().strip("'\"").replace("\\ ", " ")
    return Path(value).expanduser()


def _internal_dir(result: Path) -> Path:
    hidden = result / ".内部数据"
    return hidden if hidden.exists() else result / "03_日志"


def _load_names(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    data.setdefault("right", "")
    data.setdefault("left", "")
    data.setdefault("chat_type", "unknown")
    data.setdefault("group_name", "")
    data.setdefault("narrator_name", "")
    data.setdefault("nick_to_name", {})
    data.setdefault("unresolved_speaker_groups", {})
    data.setdefault("manual_message_speakers", {})
    return data


def _is_group(result: Path, data: dict) -> bool:
    if str(data.get("chat_type") or "").lower() in {"group", "群聊"}:
        return True
    transcript = _internal_dir(result) / "聊天记录.md"
    if not transcript.exists():
        transcript = result / "03_文字稿" / "聊天记录.md"
    try:
        return "- 聊天类型：微信群" in transcript.read_text(encoding="utf-8")
    except OSError:
        return False


def _detected_nicknames(result: Path) -> list[str]:
    transcript = _internal_dir(result) / "聊天记录.md"
    if not transcript.exists():
        transcript = result / "03_文字稿" / "聊天记录.md"
    try:
        text = transcript.read_text(encoding="utf-8")
    except OSError:
        return []
    return sorted(set(re.findall(r"\*\*([^*⚠]{1,30})⚠\*\*", text)))


def _ask(prompt: str, current: str = "") -> str:
    suffix = f"（当前：{current}）" if current else ""
    value = input(f"{prompt}{suffix}：").strip()
    return value or current


def _parse_mappings(value: str) -> dict[str, str]:
    mappings = {}
    for part in re.split(r"[；;\n]", value):
        if "=" not in part:
            continue
        nick, name = (item.strip() for item in part.split("=", 1))
        if nick and name:
            mappings[nick] = name
    return mappings


def _load_pending_messages(internal: Path) -> list[dict]:
    try:
        data = json.loads((internal / "待核成员消息.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [item for item in data if str(item.get("id") or "").strip()]


def _load_pending_groups(internal: Path) -> list[dict]:
    try:
        data = json.loads((internal / "待核成员分组.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    return [
        item
        for item in data
        if str(item.get("id") or "").strip() and str(item.get("number") or "").strip()
    ]


def _parse_index_mappings(value: str, pending: list[dict]) -> dict[str, str]:
    result = {}
    for index, name in _parse_mappings(value).items():
        try:
            item = pending[int(index) - 1]
        except (ValueError, IndexError):
            continue
        result[str(item["id"])] = name
    return result


def _parse_group_mappings(value: str, pending: list[dict]) -> dict[str, str]:
    """Convert user-facing group numbers to stable internal avatar group ids."""
    by_number = {str(item["number"]): str(item["id"]) for item in pending}
    result = {}
    for number, name in _parse_mappings(value).items():
        match = re.search(r"(\d+)$", number)
        group_id = by_number.get(match.group(1)) if match else None
        if group_id:
            result[group_id] = name
    return result


def main() -> int:
    print("把成果文件夹拖到这里，然后按回车：")
    result = _clean_path(input())
    if not (result / "01_截图").is_dir():
        print("未找到成果文件夹（其中应有 01_截图）。未做任何修改。")
        return 1
    internal = _internal_dir(result)
    internal.mkdir(parents=True, exist_ok=True)
    names_file = internal / "成员名单.json"
    data = _load_names(names_file)
    data["narrator_name"] = _ask("录制人姓名（可直接回车保留；留空时文字稿显示“录制人补充说明”）", str(data.get("narrator_name") or ""))

    if _is_group(result, data):
        data["chat_type"] = "group"
        data["group_name"] = _ask("微信群名称（可直接回车保留）", str(data.get("group_name") or ""))
        data["right"] = _ask("右侧“我”的姓名（可直接回车保留）", str(data.get("right") or ""))
        known = _detected_nicknames(result)
        if known:
            print("本稿中待核的微信昵称：" + "、".join(known))
        print("填写昵称与实名对应关系（例如：Lily=张三；小王=王五；直接回车则不改）：")
        mappings = _parse_mappings(input())
        data["nick_to_name"].update(mappings)

        pending_groups = _load_pending_groups(internal)
        if pending_groups:
            print("以下待核成员已按头像和画面位置合并；填写一次即可更新该成员的全部消息：")
            for group in pending_groups:
                samples = []
                for sample in group.get("samples", [])[:2]:
                    text = " ".join(str(sample.get("text") or "").split())
                    if text:
                        samples.append(f"[{sample.get('time') or ''}] {text[:40]}")
                detail = "；".join(samples) or "无可读文字样本"
                print(f"  {group['number']}. 未识别成员{group['number']}（共 {group.get('count', 0)} 条）：{detail}")
            print("例如：1=张三；2=李四；直接回车则保持“未识别成员1⚠”等标记：")
            grouped = _parse_group_mappings(input(), pending_groups)
            data["unresolved_speaker_groups"].update(grouped)

        pending = _load_pending_messages(internal)
        if pending:
            print("以下极少数消息无法归入头像组，可按序号分别指定发送人：")
            for i, item in enumerate(pending, 1):
                text = " ".join(str(item.get("text") or "").split())
                print(f"  {i}. [{item.get('time') or ''}] {text[:60]}")
            print("例如：1=张三；3=李四；直接回车则保持“未识别成员⚠”：")
            manual = _parse_index_mappings(input(), pending)
            data["manual_message_speakers"].update(manual)
    else:
        data["chat_type"] = "1v1"
        data["right"] = _ask("右侧（绿色气泡）姓名", str(data.get("right") or ""))
        data["left"] = _ask("左侧（白色气泡）姓名", str(data.get("left") or ""))

    names_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    command = [sys.executable, str(Path(__file__).with_name("regenerate_md.py")), str(result)]
    completed = subprocess.run(command, check=False)
    if completed.returncode == 0:
        print("姓名已更新，Word 文字稿已重新生成；截图和 PDF 未重新处理。")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
