#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频信息表：处理前由用户填写的基础事实（谁是谁、群聊名单、内容说明）。

预检流程会生成模板；全量处理时读取本表并合并进隐藏的内部名单。
字段优先级：命令行 --me/--other > 视频信息表 > 内部名单 > 自动识别 > 默认。
"""

from __future__ import annotations

import json
from pathlib import Path


def template() -> dict:
    return {
        "right": "",       # 右侧对话人姓名（绿色气泡一侧，1 对 1）
        "left": "",        # 左侧对话人姓名（白色气泡一侧，1 对 1）
        "chat_type": "unknown",  # 1v1 / group / unknown
        "group_name": "",  # 群聊名称（群聊时优先使用用户确认的名称）
        "content_note": "",      # 内容说明（如：滚动历史聊天记录）
        "narrator_name": "",     # 可选：录制人补充说明的显示姓名；留空则显示“录制人”
        "members": {},           # 群聊：识别到的昵称 -> 真实姓名
    }


def write_template(path: Path, detected_nicks=None):
    data = template()
    if detected_nicks:
        data["members"] = {nick: "" for nick in detected_nicks}
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_info(path) -> dict:
    """读取视频信息表，返回基础身份、群聊及可选录制人信息。"""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    members = {}
    for k, v in (data.get("members") or {}).items():
        if str(k).strip():
            members[str(k).strip()] = str(v).strip()
    return {
        "right": str(data.get("right") or "").strip(),
        "left": str(data.get("left") or "").strip(),
        "chat_type": str(data.get("chat_type") or "unknown").strip(),
        "group_name": str(data.get("group_name") or "").strip(),
        "content_note": str(data.get("content_note") or "").strip(),
        "narrator_name": str(data.get("narrator_name") or "").strip(),
        "members": members,
    }


def merge_into_names(names_file: Path, info_path: Path) -> dict:
    """把视频信息表合并进成员名单（信息表非空字段优先），返回合并后的名单 dict。"""
    names_file = Path(names_file)
    try:
        data = json.loads(names_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {"right": "", "left": "", "chat_type": "unknown", "group_name": "", "nick_to_name": {}}
    info = read_info(info_path)
    if info.get("right"):
        data["right"] = info["right"]
    if info.get("left"):
        data["left"] = info["left"]
    if info.get("chat_type") and info["chat_type"] != "unknown":
        data["chat_type"] = info["chat_type"]
    if info.get("group_name"):
        data["group_name"] = info["group_name"]
    if info.get("narrator_name"):
        data["narrator_name"] = info["narrator_name"]
    merged = dict(data.get("nick_to_name") or {})
    for k, v in info.get("members", {}).items():
        if v:
            merged[k] = v
    data["nick_to_name"] = merged
    names_file.parent.mkdir(parents=True, exist_ok=True)
    names_file.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return data
