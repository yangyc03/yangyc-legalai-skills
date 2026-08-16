#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微信录屏页面分类与横向切换后的稳定页筛选。"""

from __future__ import annotations

import json
from pathlib import Path


GROUP_WORDS = (
    "群聊信息", "群公告", "群管理", "群二维码", "我在群里的昵称", "显示群成员昵称", "群成员",
)
CHAT_INFO_WORDS = (
    "聊天信息", "查找聊天内容", "消息免打扰", "置顶聊天", "清空聊天记录", "聊天背景",
)
CONTACT_WORDS = (
    "微信号", "更多信息", "朋友权限", "朋友圈", "地区", "个人信息", "来源",
)
CONTEXT_TYPES = {"group_info", "chat_info", "contact_profile", "context_pending"}
TYPE_LABELS = {
    "group_info": "微信群信息页",
    "chat_info": "聊天信息页",
    "contact_profile": "联系人资料页",
    "context_pending": "资料页面（待核）",
    "other": "其他页面",
    "chat": "聊天页",
    "transition": "过渡帧",
}


def _texts(lines) -> list[str]:
    return [str(getattr(line, "text", "") or "").strip() for line in lines if str(getattr(line, "text", "") or "").strip()]


def classify_page(lines) -> tuple[str, str, list[str]]:
    """以微信界面文字为主要依据，返回（页面类型、置信状态、依据）。"""
    text = "\n".join(_texts(lines))
    if not text:
        return "other", "待核", []
    groups = [word for word in GROUP_WORDS if word in text]
    chats = [word for word in CHAT_INFO_WORDS if word in text]
    contacts = [word for word in CONTACT_WORDS if word in text]
    ranked = [("group_info", groups), ("chat_info", chats), ("contact_profile", contacts)]
    kind, evidence = max(ranked, key=lambda item: len(item[1]))
    if len(evidence) >= 2 or (kind == "contact_profile" and "微信号" in evidence):
        return kind, "已确认", evidence
    if evidence:
        return "context_pending", "待核", evidence
    return "chat", "已确认", []


def _layout_score(lines, evidence: list[str]) -> float:
    """资料页越接近全屏，关键菜单文字通常越靠左；用于从滑动动画中选终点帧。"""
    positions = [float(getattr(line, "x", 1.0)) for line in lines if str(getattr(line, "text", "") or "").strip() in evidence]
    return 1.0 - (sum(positions) / len(positions)) if positions else 0.0


def _float(row: dict, key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _time_seconds(value: str) -> float:
    try:
        h, m, s = str(value).split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (TypeError, ValueError):
        return 0.0


def classify_rows(rows: list[dict], lines_map: dict) -> tuple[list[dict], list[dict], dict, list[dict]]:
    """标记有效稳定页。

    抽帧阶段为保守起见会保留横向切换中的多个画面；这里把同一低位移、短时间的
    资料页片段压缩为最清晰的稳定终点，且不让关闭资料页的动画混入聊天页。
    """
    annotated = []
    for row in rows:
        item = dict(row)
        lines = lines_map.get(str(Path(row["_frames_dir"]) / row["文件名"]), []) if row.get("_frames_dir") else []
        kind, confidence, evidence = classify_page(lines)
        item.update(
            {
                "页面类型": kind,
                "页面置信状态": confidence,
                "强制保留": "是" if kind in CONTEXT_TYPES else "否",
                "保留原因": "；".join(evidence) if evidence else "聊天内容" if kind == "chat" else "未识别到明确页面特征",
                "_drop": False,
                "_evidence": evidence,
                "_layout_score": _layout_score(lines, evidence),
            }
        )
        annotated.append(item)

    events = []
    context_kept: dict[tuple[str, int], tuple[dict, dict]] = {}
    i = 0
    while i < len(annotated):
        if annotated[i]["页面类型"] not in CONTEXT_TYPES:
            i += 1
            continue
        start = i
        base_y = _float(annotated[i], "累计位移(像素)")
        j = i + 1
        while j < len(annotated):
            candidate = annotated[j]
            # OCR 在全屏资料页偶尔会只识别到左侧聊天内容；在同一横向切换片段内，
            # 也把这一帧作为“待选稳定终点”，但由相邻资料页的证据决定最终类型。
            if candidate["页面类型"] not in CONTEXT_TYPES and candidate["页面类型"] != "chat":
                break
            if abs(_float(candidate, "累计位移(像素)") - base_y) > 80:
                break
            gap = _time_seconds(candidate["视频时间"]) - _time_seconds(annotated[j - 1]["视频时间"])
            if gap > 1.2:
                break
            # OCR 漏掉全屏资料页菜单时，只接纳动画停稳后出现的聊天文本帧；
            # 紧随前帧的聊天文本仍属于侧滑动画。
            if candidate["页面类型"] == "chat" and gap < 0.35:
                break
            j += 1
        segment = annotated[start:j]
        # 横向打开完成后最后出现的稳定帧最完整；OCR漏掉菜单时仍可由相邻资料页的证据保留下来。
        kept = max(segment, key=lambda row: _time_seconds(row["视频时间"]))
        strongest = max(segment, key=lambda row: (len(row["_evidence"]), row["_layout_score"], _float(row, "清晰度")))
        kept["页面类型"] = strongest["页面类型"] if strongest["页面类型"] != "context_pending" else kept["页面类型"]
        kept["页面置信状态"] = strongest["页面置信状态"]
        kept["强制保留"] = "是"
        kept["保留原因"] = "；".join(strongest["_evidence"]) or "资料页面待核"
        for row in segment:
            if row is not kept:
                row.update({"页面类型": "transition", "页面置信状态": "已排除", "强制保留": "否", "保留原因": "横向切换或同页动画", "_drop": True})
        event = {
            "kind": "context_page",
            "page_type": kept["页面类型"],
            "confidence": kept["页面置信状态"],
            "file": kept["文件名"],
            "time": kept["视频时间"],
            "evidence": strongest["_evidence"],
            "transition_files": [row["文件名"] for row in segment if row is not kept],
        }
        context_key = (kept["页面类型"], round(base_y / 80.0))
        previous = context_kept.get(context_key)
        if previous is None:
            events.append(event)
            context_kept[context_key] = (kept, event)
        else:
            # 同一横向页面在关闭动画中会再次命中菜单关键词；首个稳定终点更接近实际页面，保留它。
            kept.update({"页面类型": "transition", "页面置信状态": "已排除", "强制保留": "否", "保留原因": "同一资料页的关闭动画", "_drop": True})

        # 资料页关闭后、尚未发生真实纵向滚动的连续聊天动画不应作为消息截图。
        k = j
        while k < len(annotated):
            row = annotated[k]
            if row["页面类型"] != "chat":
                break
            if abs(_float(row, "累计位移(像素)") - base_y) > 80:
                break
            if _time_seconds(row["视频时间"]) - _time_seconds(annotated[k - 1]["视频时间"]) > 1.2:
                break
            row.update({"页面类型": "transition", "页面置信状态": "已排除", "强制保留": "否", "保留原因": "资料页关闭动画", "_drop": True})
            k += 1
        i = max(k, j)

    for row in annotated:
        row.pop("_frames_dir", None)
    kept_rows = [row for row in annotated if not row["_drop"]]
    summary = {
        "context_pages": sum(1 for row in kept_rows if row["页面类型"] in CONTEXT_TYPES),
        "transitions": sum(1 for row in annotated if row["_drop"]),
        "mandatory_pages": sum(1 for row in kept_rows if row["强制保留"] == "是"),
    }
    return kept_rows, events, summary, annotated


def write_page_events(path: Path, events: list[dict], rows: list[dict]) -> None:
    records = list(events)
    records.extend(
        {
            "kind": "transition",
            "file": row["文件名"],
            "time": row["视频时间"],
            "reason": row["保留原因"],
        }
        for row in rows
        if row.get("_drop")
    )
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
