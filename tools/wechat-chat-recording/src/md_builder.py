#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把每屏 OCR 结果合并成一份 Markdown 聊天文字稿。

- 按“累计滚动位移 + 行位置”把同一消息在不同截图里的重复识别合并掉；
- 日期分隔条识别为章节标题，视频时间为每条消息打时间戳；
- 说话人按消息气泡位置/颜色判断（右侧绿泡默认“我”，左侧默认“对方”；
  可传入双方姓名 speakers，或从标题栏自动识别左侧姓名）。
"""

from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

TOP_CROP = 0.10  # 顶部状态栏/标题栏占比
BOT_CROP = 0.08  # 底部输入栏占比
DUPE_JITTER = 16  # 同一行跨屏识别的纵坐标抖动容差（像素）

HARD_DATE_RE = re.compile(r"(\d{1,2}月\d{1,2}日)|(\d{4}年\d{1,2}月)")
SOFT_DATE_RE = re.compile(r"(今天|昨天|前天|星期[一二三四五六日天]|上午|下午)")
TIME_RE = re.compile(r"\d{1,2}:\d{2}")
CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
VOICE_DURATION_RE = re.compile(r'^\s*(\d{1,3})\s*(?:[\"\u201d\u2033]|秒)\s*[（(]?\s*$')
BARE_DURATION_RE = re.compile(r'^\s*(\d{1,3})\s*$')
SKIP_TOP_WORDS = {"返回", "···", "更多", "微信", "WeChat"}
NAME_STOP = {
    "目录", "正文", "标题", "条款", "附件", "合同", "日期", "编号", "金额", "合计", "备注",
    "姓名", "身份证", "电话", "地址", "单位", "职务", "说明", "页码", "微信网页版", "群公告",
    "文件传输助手", "聊天记录", "朋友圈", "项目", "大厦", "租赁", "报告", "周报", "月报",
    "pdf", "docx", "doc", "xlsx", "date", "from", "subject", "rarb", "rar", "sheet",
    "to", "cc", "fw", "re", "do", "关于", "中国", "关于中国", "保险",
}
NAME_STOP_LOWER = {w.lower() for w in NAME_STOP}
SUFFIX_STOP = (
    "方案", "平方米", "方米", "公司", "情况", "部分", "内容", "要求", "建议", "设计",
    "图纸", "周报", "月报", "汇报", "报告", "清单", "合同", "协议", "大厦", "项目",
    "食堂", "踏勘", "预算", "招商", "租赁", "北分", "使用", "标准", "面积", "中心",
    "文件", "版本", "楼层", "车位", "费用", "需求", "信息", "材料", "流程",
    "方米", "方案", "责任险", "工程险", "支付令", "条款", "董事", "基金公", "金路中", "公", "司", "部经", "工程部",
)
FILE_RE = re.compile(r"\.(docx?|pdf|xlsx?|pptx?|zip|rar|txt|jpg|png)$", re.I)
NUM_RE = re.compile(r"\d")
EDGE_PUNCT = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff_]+|[^0-9A-Za-z\u4e00-\u9fff_]+$")
SURNAMES = set(
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
ASCII_NAME_RE = re.compile(r"^[A-Za-z_]{3,8}$")
CHINESE_NAME_RE = re.compile(r"^[\u4e00-\u9fff]{2,8}$")
MIXED_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z_\u4e00-\u9fff]{1,7}$")


def _is_date_like(text: str) -> bool:
    """判断是否为聊天记录里的日期/时间分隔条（如“2025年5月15日 下午4:46”）。"""
    if re.match(r"^M[O0]?\d{2,3}", text):
        return False
    if HARD_DATE_RE.search(text):
        return True
    # “今天/昨天/上午/下午”这类软日期，需要带时间或本身很短，避免误伤消息正文
    return bool(SOFT_DATE_RE.search(text) and (TIME_RE.search(text) or len(text) <= 6))


def _is_wechat_system_message(text: str) -> bool:
    """群成员变更等系统提示不是聊天成员发言，不能进入成员归并。"""
    normalized = re.sub(r"\s+", "", str(text or ""))
    return bool(
        re.search(
            r"(?:邀请.*加入了群聊|加入了群聊|退出了群聊|已被移出群聊|撤回了一条消息|修改群名称|拍了拍)",
            normalized,
        )
    )


def _name_noise(text: str, allow_long_nickname: bool = False) -> bool:
    """判断一段文本是否不像“群聊昵称”（数字、文件名、文档片段、长句等）。"""
    if not text:
        return True
    if NUM_RE.search(text):
        return True
    if any(ch in text for ch in "：:℃°%㎡（）()【】[]/\\•·。，、；;\"'"):
        return True
    if FILE_RE.search(text):
        return True
    if "http" in text.lower() or "www." in text.lower():
        return True
    max_len = 30 if allow_long_nickname else 8
    if not (2 <= len(text) <= max_len):
        return True
    if text.lower() in NAME_STOP_LOWER:
        return True
    if text.strip("_").lower() in NAME_STOP_LOWER:
        return True
    if ASCII_NAME_RE.fullmatch(text):
        return False
    if MIXED_NAME_RE.fullmatch(text) and "_" in text:
        ascii_letters = sum(1 for ch in text if ch.isascii() and ch.isalpha())
        if ascii_letters >= 2:
            return False
    # 群昵称可能含部门/职务，例如“某租赁部门张三”。仅在它同时满足
    # “左侧、窄行、紧贴消息气泡”的几何条件时才启用这一宽松判断。
    compact = re.sub(r"\s+", "", text)
    if allow_long_nickname and re.fullmatch(r"[A-Za-z_\u4e00-\u9fff]{2,30}", compact):
        if len(compact) >= 2 and compact[-2] in SURNAMES:
            return False
        if len(compact) >= 3 and compact[-3] in SURNAMES:
            return False
    if CHINESE_NAME_RE.fullmatch(text):
        if text[0] in SURNAMES and len(text) <= 4 and not text.endswith(SUFFIX_STOP):
            return False
        # 以“姓氏+名”结尾的显示名（如“某租赁部门张三”“某机构李四”“机构项目负责人”）
        if len(text) >= 2 and text[-2] in SURNAMES and not text.endswith(SUFFIX_STOP):
            return False
        if len(text) >= 3 and text[-3] in SURNAMES and not text.endswith(SUFFIX_STOP):
            return False
    return True


def _clean_name(text: str) -> str:
    return EDGE_PUNCT.sub("", text)


def _canon_name(text: str, confirmed: set) -> str:
    """返回包含该文本的最长确认昵称（用于合并 OCR 变体）。"""
    if text in confirmed:
        return text
    best = None
    for c in confirmed:
        if text and (text in c or c in text):
            if best is None or len(c) > len(best):
                best = c
    return best or text


def collect_group_names(lines_map: dict, frames_dir: Path):
    """识别群聊昵称，返回 (自动确认的昵称集合, 每屏全部昵称候选位置表)。

    昵称特征：左侧、窄行、紧贴在一条左侧消息上方、全片反复出现、
    不像数字/文件名/文档片段，且不作为独立消息出现。候选位置表保留所有候选，
    供成员名单和 LLM 归属后处理使用。
    """
    frames_dir = Path(frames_dir)
    if not lines_map or not frames_dir.exists():
        return set(), {}
    with Image.open(sorted(frames_dir.glob("*.png"))[0]) as first_image:
        w_im, h_im = first_image.size
    cand = {}
    standalone = {}
    labels_raw = {}
    for key, lines in lines_map.items():
        screen_name = Path(key).name
        scr_lines = []
        for ln in lines:
            x, y, w, h = ln.x * w_im, ln.y * h_im, ln.w * w_im, ln.h * h_im
            if y < TOP_CROP * h_im or y + h > (1 - BOT_CROP) * h_im:
                continue
            scr_lines.append({"text": _clean_name(ln.text.strip()), "x": x, "y": y, "w": w, "h": h})
        scr_lines.sort(key=lambda l: l["y"])
        screen_labels = []
        for i, ln in enumerate(scr_lines):
            xc = (ln["x"] + ln["w"] / 2) / w_im if "w" in ln else (ln["x"] + 0) / w_im
            # 昵称通常比正文行窄且较矮；这允许带部门/职务的长昵称，
            # 同时避免把左侧长正文误识别为昵称。
            if (
                not ln["text"]
                or xc >= 0.45
                or ln["w"] > 0.45 * w_im
                or ln["h"] > 30
                or _name_noise(ln["text"], allow_long_nickname=True)
            ):
                continue
            is_label = False
            for j in range(i + 1, min(i + 3, len(scr_lines))):
                nxt = scr_lines[j]
                # 左侧长气泡的文字可延伸到屏幕中部，不能只看文字中心；
                # 右侧气泡的文字起点通常明显靠右。
                if nxt["x"] > 0.28 * w_im:
                    continue
                gap = nxt["y"] - (ln["y"] + ln["h"])
                if 0 <= gap <= 30:
                    is_label = True
                    break
            if is_label:
                cand[ln["text"]] = cand.get(ln["text"], 0) + 1
                screen_labels.append((ln["text"], ln["y"], ln["y"] + ln["h"]))
            else:
                standalone[ln["text"]] = standalone.get(ln["text"], 0) + 1
        labels_raw[screen_name] = screen_labels
    # 合并 OCR 变体：短名是长名子串时并入长名
    merged = {}
    for t, c in cand.items():
        anchor = t
        for other in cand:
            if other != t and t in other and len(other) > len(anchor):
                anchor = other
        merged[anchor] = merged.get(anchor, 0) + c
    confirmed = {
        t for t, c in merged.items()
        if c >= 2 and standalone.get(t, 0) <= c // 2 and not _name_noise(t, allow_long_nickname=True)
    }
    if len(confirmed) < 2:
        # 仍保留逐屏的昵称位置；明确是群聊时可把与气泡紧邻、但只出现一次的昵称标为待核。
        return set(), labels_raw
    return confirmed, labels_raw


def _label_display(raw: str, nick_to_name: dict, auto_names: set, allow_pending: bool = False):
    """昵称候选文本 → 最终显示名；群聊中可保留紧邻气泡的待核昵称。"""
    if raw in nick_to_name:
        return nick_to_name[raw]
    canon = _canon_name(raw, auto_names)
    if canon in auto_names:
        return nick_to_name.get(canon, canon)
    if allow_pending and raw and not _name_noise(raw, allow_long_nickname=True):
        return raw
    return None


def _is_label_line(ln, labels_raw: dict, nick_to_name: dict, auto_names: set, allow_pending: bool = False) -> bool:
    for img, y_center in ln.get("occ", [(ln["img"], ln["y_center"])]):
        for raw, y_top, y_bot in labels_raw.get(img, []):
            if _label_display(raw, nick_to_name, auto_names, allow_pending) is None:
                continue
            if raw == ln["text"] and y_top - 8 <= y_center <= y_bot + 8:
                return True
    return False


def _is_orphan_confirmed_label(ln, nick_to_name: dict, auto_names: set, screen_width: int) -> bool:
    """图片等无 OCR 正文时，已确认的昵称也不应作为独立聊天消息输出。"""
    if _label_display(ln.get("text", ""), nick_to_name, auto_names) is None:
        return False
    return (
        float(ln.get("x_center", 1)) < 0.45
        and float(ln.get("x1", 0)) - float(ln.get("x0", 0)) <= 0.45 * screen_width
        and float(ln.get("h", 99)) <= 30
    )


def _nearest_label(img: str, y_center: float, labels_raw: dict, nick_to_name: dict, auto_names: set, allow_pending: bool = False):
    best, best_gap = None, None
    for raw, _y_top, y_bot in labels_raw.get(img, []):
        disp = _label_display(raw, nick_to_name, auto_names, allow_pending)
        if disp is None or y_bot > y_center - 5 or y_center - y_bot > 150:
            continue
        gap = y_center - y_bot
        if best_gap is None or gap < best_gap:
            best, best_gap = disp, gap
    return best


def _fmt_time(ts: str) -> str:
    return ts.split(".")[0] if ts else ""


def _time_seconds(ts: str) -> float:
    """把 report.csv 中的 HH:MM:SS(.mmm) 转为秒。"""
    try:
        parts = str(ts or "").split(":")
        if len(parts) != 3:
            return 0.0
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except (TypeError, ValueError):
        return 0.0


def _message_id(item: dict) -> str:
    """为人工归属保留稳定键；不把技术编号展示在文字稿中。"""
    value = f"{round(float(item.get('y', 0)), 1)}|{item.get('time', '')}|{item.get('text', '')}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def _avatar_feature(image, y_center: float):
    """提取左侧头像区域的低精度颜色特征；空白或系统提示不参与成员聚类。"""
    if image is None:
        return None
    try:
        width, height = image.size
        x0, x1 = max(0, int(width * 0.015)), min(int(width * 0.20), 118)
        y0, y1 = max(0, int(y_center - 50)), min(height, int(y_center + 50))
        if x1 - x0 < 30 or y1 - y0 < 40:
            return None
        arr = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB").resize((24, 24)))
        # 纯白/纯灰区域通常没有头像；不强行给系统提示或裁切失败的画面编号。
        if float(arr.std()) < 18:
            return None
        feature = []
        for channel in range(3):
            hist, _ = np.histogram(arr[:, :, channel], bins=8, range=(0, 256), density=True)
            feature.extend(hist.tolist())
        vector = np.asarray(feature, dtype=np.float32)
        norm = float(np.linalg.norm(vector))
        return vector / norm if norm else None
    except Exception:  # noqa: BLE001
        return None


def _cluster_unknown_group_members(items, img_cache: dict, left_name: str):
    """将无昵称依据的左侧消息按头像画面特征聚为稳定待核成员组。"""
    clusters = []
    for item in items:
        if item.get("kind") != "msg" or item.get("speaker") != left_name:
            continue
        feature = _avatar_feature(img_cache.get(item.get("img", "")), float(item.get("y_screen", 0)))
        if feature is None:
            continue
        best, best_distance = None, float("inf")
        for cluster in clusters:
            distance = float(np.mean(np.abs(feature - cluster["feature"])))
            if distance < best_distance:
                best, best_distance = cluster, distance
        # 同一头像的低精度直方图在同一录屏中通常极为接近；阈值刻意收紧，
        # 宁可把同一人拆成多个待核组，也不能把不同群成员擅自合并。
        if best is None or best_distance > 0.018:
            best = {"id": f"avatar_{len(clusters) + 1:03d}", "feature": feature, "items": []}
            clusters.append(best)
        best["items"].append(item)
    return clusters


def _merge_message_lines(items: list[dict], screen_width: int) -> list[dict]:
    """合并同一气泡内连续 OCR 行，避免换行被当成多条聊天消息。"""
    merged = []
    for item in items:
        previous = merged[-1] if merged else None
        previous_width = float(previous.get("x1", 0)) - float(previous.get("x0", 0)) if previous else 0
        line_gap = (
            float(item.get("y", 0)) - float(previous.get("_last_line_y", previous.get("y", 0)))
            if previous
            else float("inf")
        )
        same_left_edge = previous is not None and abs(float(item.get("x0", 0)) - float(previous.get("x0", 0))) <= 24
        can_merge = (
            previous is not None
            and previous.get("kind") == item.get("kind") == "msg"
            and not previous.get("voice_id")
            and not item.get("voice_id")
            and previous.get("speaker") == item.get("speaker")
            and same_left_edge
            and 0 < line_gap <= 48
            # 前一行接近气泡常见的整行宽度，下一行才可能是它的换行；
            # 这会避免把两个相邻的短消息误拼接。
            and previous_width >= 0.45 * screen_width
            and previous.get("text") != "[图片]"
            and item.get("text") != "[图片]"
        )
        if not can_merge:
            copied = dict(item)
            copied["_last_line_y"] = float(item.get("y", 0))
            merged.append(copied)
            continue
        previous["text"] = f"{previous.get('text', '')}{item.get('text', '')}"
        previous["x0"] = min(float(previous.get("x0", 0)), float(item.get("x0", 0)))
        previous["x1"] = max(float(previous.get("x1", 0)), float(item.get("x1", 0)))
        previous["occ"] = list(previous.get("occ") or []) + list(item.get("occ") or [])
        previous["_last_line_y"] = float(item.get("y", 0))
    return merged


def _is_green(pixel) -> bool:
    r, g, b = pixel[:3]
    return g > r + 25 and g > b + 25


def _looks_like_voice_bubble(image, ln: dict) -> bool:
    """裸时长数字只有同时具备气泡色块和近邻图标特征时才接受。"""
    if image is None:
        return False
    try:
        width, height = image.size
        x0 = max(0, int(ln["x0"]) - 48)
        x1 = min(width, int(ln["x1"]) + 56)
        y0 = max(int(height * TOP_CROP), int(ln["y_center"]) - 28)
        y1 = min(int(height * (1 - BOT_CROP)), int(ln["y_center"]) + 28)
        if x1 - x0 < 32 or y1 - y0 < 20:
            return False
        arr = np.asarray(image.crop((x0, y0, x1, y1)).convert("RGB"))
        green = ((arr[:, :, 1] > arr[:, :, 0] + 18) & (arr[:, :, 1] > arr[:, :, 2] + 18)).mean()
        near_white = (arr.min(axis=2) > 235).mean()
        dark = (arr.max(axis=2) < 110).sum()
        # 绿色或白色气泡底色 + 附近存在一组深色扬声器/波纹像素，页码不满足此组合。
        return (green > 0.12 or near_white > 0.35) and dark >= 12
    except Exception:  # noqa: BLE001
        return False


def detect_other_name(lines_map: dict, frames_dir: Path) -> str | None:
    """尝试从标题栏识别左侧对话人姓名（尽力而为，识别不到返回 None）。"""
    frames_dir = Path(frames_dir)
    if not lines_map or not frames_dir.exists():
        return None
    first = sorted(frames_dir.glob("*.png"))[0]
    with Image.open(first) as first_image:
        h_im = first_image.size[1]
    counts = {}
    for key, lines in lines_map.items():
        for ln in lines:
            y = ln.y * h_im
            if y >= 0.10 * h_im:
                continue
            text = ln.text.strip()
            if not text or CLOCK_RE.match(text) or len(text) > 14:
                continue
            if any(w in text for w in SKIP_TOP_WORDS):
                continue
            counts[text] = counts.get(text, 0) + 1
    if not counts:
        return None
    best = max(counts.items(), key=lambda kv: kv[1])
    if best[1] < 3:
        return None
    name = best[0].replace("微信", "").strip(" ｜|·—")
    # 标题栏 OCR 偶尔会把返回箭头等符号识别为“<”；自动姓名至少应含两个可读字符。
    if len(name) < 2 or not re.fullmatch(r"[\w\u4e00-\u9fff·・ -]+", name):
        return None
    return name


def detect_chat_title(lines_map: dict, frames_dir: Path) -> str | None:
    """从聊天页标题栏提取重复出现的名称；仅作为待核提示，不作身份认定。"""
    frames_dir = Path(frames_dir)
    files = sorted(frames_dir.glob("*.png"))
    if not lines_map or not files:
        return None
    with Image.open(files[0]) as image:
        w_im, h_im = image.size
    counts = {}
    for lines in lines_map.values():
        for ln in lines:
            text = ln.text.strip().replace("微信", "").strip(" ｜|·—")
            xc = ln.x + ln.w / 2
            if not text or len(text) > 24 or ln.y * h_im >= 0.10 * h_im or not (0.15 <= xc <= 0.85):
                continue
            if CLOCK_RE.fullmatch(text) or text in SKIP_TOP_WORDS or text in {"返回", "更多"}:
                continue
            counts[text] = counts.get(text, 0) + 1
    if not counts:
        return None
    title, count = max(counts.items(), key=lambda item: item[1])
    return title if count >= 2 else None


def build_markdown(
    video_name: str,
    rows,
    lines_map: dict,
    frames_dir: Path,
    md_path: Path,
    raw_path: Path,
    speakers: dict | None = None,
    names_file: Path | None = None,
    llm_cfg: dict | None = None,
    auto_left: str | None = None,
    voice_results: dict | None = None,
    narrator_events: list[dict] | None = None,
):
    frames_dir = Path(frames_dir)
    md_path = Path(md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path = Path(raw_path)

    # 成员名单文件：{"right": 右侧姓名, "left": 左侧默认姓名, "nick_to_name": {识别昵称: 真实姓名}}
    data = {}
    nick_to_name = {}
    file_right = file_left = file_group_name = file_narrator_name = ""
    file_chat_type = "unknown"
    if names_file is not None and Path(names_file).exists():
        try:
            data = json.loads(Path(names_file).read_text(encoding="utf-8"))
            file_right = str(data.get("right") or "").strip()
            file_left = str(data.get("left") or "").strip()
            file_chat_type = str(data.get("chat_type") or "unknown").strip().lower()
            file_group_name = str(data.get("group_name") or "").strip()
            file_narrator_name = str(data.get("narrator_name") or "").strip()
            nick_to_name = {str(k).strip(): str(v).strip() for k, v in (data.get("nick_to_name") or {}).items() if str(k).strip()}
        except Exception:  # noqa: BLE001
            print(f"警告：成员名单文件解析失败，已忽略：{names_file}")

    metas = []
    for r in rows:
        metas.append(
            {
                "name": r["文件名"],
                "time": r["视频时间"],
                "offset": float(r["累计位移(像素)"]),
                "page_type": str(r.get("页面类型") or "chat"),
            }
        )

    img_path = frames_dir / metas[0]["name"] if metas else None
    if img_path is not None and img_path.exists():
        with Image.open(img_path) as first_image:
            w_im, h_im = first_image.size
    else:
        w_im, h_im = 540, 960

    raw_out = []
    all_lines = []
    for m in metas:
        key = str(frames_dir / m["name"])
        ocr_lines = lines_map.get(key, [])
        kept = []
        for ln in ocr_lines:
            text = ln.text.strip()
            if not text:
                continue
            x, y, w, h = ln.x * w_im, ln.y * h_im, ln.w * w_im, ln.h * h_im
            if y < TOP_CROP * h_im or y + h > (1 - BOT_CROP) * h_im:
                continue
            kept.append({"text": text, "x": round(x), "y": round(y), "w": round(w), "h": round(h)})
            if m["page_type"] == "chat":
                all_lines.append(
                    {
                        "content_y": y + m["offset"],
                        "text": text,
                        "x_center": (x + w / 2) / w_im,
                        "x0": x,
                        "x1": x + w,
                        "h": h,
                        "y_center": y + h / 2,
                        "img": m["name"],
                        "time": _fmt_time(m["time"]),
                    }
                )
        raw_out.append({"file": m["name"], "time": m["time"], "offset": m["offset"], "lines": kept})
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with open(raw_path, "w", encoding="utf-8") as f:
        for item in raw_out:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    # 跨屏去重：同一行内容在滚动中的坐标是固定的。按内容坐标排序后，
    # 把纵坐标相差很小（同一行的识别抖动）的条目合并，保留识别更完整的文本；
    # 不同消息行至少相距 50+ 像素，不会被误并。
    # 跨屏去重：同一行内容在滚动中的坐标是固定的。按内容坐标排序后，
    # 把纵坐标相差很小（同一行的识别抖动）的条目合并，保留识别更完整的文本；
    # 并记录该行在哪些屏出现过（用于跨屏找昵称标签）。
    groups = []
    for ln in sorted(all_lines, key=lambda x: x["content_y"]):
        if groups and ln["content_y"] - groups[-1][0]["content_y"] <= DUPE_JITTER:
            groups[-1].append(ln)
        else:
            groups.append([ln])
    unique = []
    for g in groups:
        rep = dict(max(g, key=lambda x: len(x["text"])))
        rep["occ"] = [(x["img"], x["y_center"]) for x in g]
        unique.append(rep)

    chat_lines_map = {
        str(frames_dir / meta["name"]): lines_map.get(str(frames_dir / meta["name"]), [])
        for meta in metas
        if meta["page_type"] == "chat"
    }
    auto_names, labels_raw = collect_group_names(chat_lines_map, frames_dir) if chat_lines_map else (set(), {})
    if file_right and file_left:
        # 名单已明确指定双方姓名：视为 1 对 1 聊天，关闭群聊昵称识别，
        # 避免把文档/文件内容误当成昵称。
        auto_names, labels_raw = set(), {}
    confirmed = auto_names | set(nick_to_name)
    group_mode = file_chat_type in {"group", "群聊"} or any(meta["page_type"] == "group_info" for meta in metas)

    IMAGE_WORDS = {"图片", "囡片", "照片"}

    items = []
    voice_index = 0
    seen_dates = set()
    img_cache = {}
    for name in {ln["img"] for ln in unique}:
        try:
            img_cache[name] = Image.open(frames_dir / name).convert("RGB")
        except OSError:
            pass  # 截图可能已移入备份，仅影响极少数的颜色判断，按位置判断即可

    right_name = (speakers or {}).get("right") or file_right or "我"
    left_name = (speakers or {}).get("left") or file_left or auto_left or ("未识别成员⚠" if group_mode else "对方")
    auto_left_detected = bool(auto_left and not (speakers or {}).get("left") and not file_left)
    # 用户明确提供的姓名（名单/信息表/命令行）视为已确认，自动识别的昵称标 ⚠ 待确认
    confirmed_displays = set(nick_to_name.values())
    if (speakers or {}).get("right") or file_right:
        confirmed_displays.add(right_name)
    if (speakers or {}).get("left") or file_left:
        confirmed_displays.add(left_name)

    def _mark(speaker: str) -> str:
        if speaker in confirmed_displays or speaker in ("我", "对方"):
            return speaker
        return speaker + "⚠"

    def speaker_of(ln):
        sample = img_cache.get(ln["img"])
        green = sample is not None and _is_green(
            sample.getpixel((max(0, int(ln["x0"]) - 6), int(ln["y_center"])))
        )
        # 右侧绿色气泡中的末行往往很短，文字中心可能落在屏幕左半部；
        # 此时气泡颜色比文字位置更可靠。
        if green:
            return right_name
        if ln["x_center"] > 0.55:
            return right_name
        if ln["x_center"] < 0.45:
            return left_name
        return left_name

    for ln in unique:
        text = ln["text"].strip()
        if len(text) < 2:
            continue
        if text in IMAGE_WORDS:
            items.append(
                {
                    "kind": "msg",
                    "speaker": speaker_of(ln),
                    "text": "[图片]",
                    "y": ln["content_y"],
                    "time": ln["time"],
                    "img": ln["img"],
                    "y_screen": ln["y_center"],
                    "x0": ln["x0"],
                    "x1": ln["x1"],
                    "occ": ln.get("occ", []),
                }
            )
            continue
        if _is_label_line(ln, labels_raw, nick_to_name, auto_names, group_mode) or (
            group_mode and _is_orphan_confirmed_label(ln, nick_to_name, auto_names, w_im)
        ):
            continue  # 群聊昵称标签：消费掉，不当作消息输出
        voice_match = VOICE_DURATION_RE.fullmatch(text)
        bare_match = BARE_DURATION_RE.fullmatch(text)
        voice_duration = int((voice_match or bare_match).group(1)) if (voice_match or bare_match) else 0
        visual_voice_hint = _looks_like_voice_bubble(img_cache.get(ln["img"]), ln) if bare_match and not voice_match else False
        if (
            (voice_match or visual_voice_hint)
            and 1 <= voice_duration <= 60
            and (ln["x_center"] <= 0.43 or ln["x_center"] >= 0.57)
        ):
            voice_index += 1
            # 语音消息时长（如“4"（”）
            items.append(
                {
                    "kind": "msg",
                    "speaker": speaker_of(ln),
                    "text": "",
                    "voice_id": f"voice_{voice_index:04d}",
                    "duration_s": voice_duration,
                    "side": "right" if ln["x_center"] >= 0.5 else "left",
                    "y": ln["content_y"],
                    "time": ln["time"],
                    "img": ln["img"],
                    "y_screen": ln["y_center"],
                    "x_center": ln["x_center"],
                    "x0": ln["x0"],
                    "x1": ln["x1"],
                    "occ": ln.get("occ", []),
                }
            )
            continue
        is_msg_like = bool(re.match(r"^M[O0]?\d{2,3}", text))
        ln_width = ln["x1"] - ln["x0"]
        if (
            _is_date_like(text)
            and not is_msg_like
            and len(text) <= 26
            and 0.30 <= ln["x_center"] <= 0.70
            and ln_width <= 0.65 * w_im
        ):
            if text not in seen_dates:
                seen_dates.add(text)
                items.append({"kind": "date", "text": text, "y": ln["content_y"], "time": ln["time"]})
            continue
        if group_mode and _is_wechat_system_message(text):
            items.append({"kind": "system", "text": text, "y": ln["content_y"], "time": ln["time"]})
            continue
        items.append(
            {
                "kind": "msg",
                "speaker": speaker_of(ln),
                "text": text,
                "y": ln["content_y"],
                "time": ln["time"],
                "img": ln["img"],
                "y_screen": ln["y_center"],
                "x0": ln["x0"],
                "x1": ln["x1"],
                "occ": ln.get("occ", []),
            }
        )

    if confirmed or group_mode:
        for it in items:
            if it["kind"] == "msg" and it.get("speaker") == left_name:
                for img, y_screen in it.get("occ", [(it.get("img", ""), it.get("y_screen", -1))]):
                    nm = _nearest_label(img, y_screen, labels_raw, nick_to_name, auto_names, group_mode)
                    if nm:
                        it["speaker"] = _mark(nm)
                        break

    if auto_left_detected:
        for it in items:
            if it["kind"] == "msg" and it.get("speaker") == left_name:
                it["speaker"] = _mark(left_name)

    # 先合并同一气泡的换行，再做待核成员归并；成员组数量按实际消息而非 OCR 行计。
    items = _merge_message_lines(items, w_im)

    unresolved_groups = []
    group_assignments = {
        str(k): str(v).strip()
        for k, v in (data.get("unresolved_speaker_groups") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    if group_mode:
        unresolved_groups = _cluster_unknown_group_members(items, img_cache, left_name)
        for index, group in enumerate(unresolved_groups, 1):
            label = group_assignments.get(group["id"]) or f"未识别成员{index}⚠"
            for item in group["items"]:
                item["unresolved_group_id"] = group["id"]
                item["speaker"] = label

    manual_speakers = {
        str(k): str(v).strip()
        for k, v in (data.get("manual_message_speakers") or {}).items()
        if str(k).strip() and str(v).strip()
    }
    if group_mode:
        for it in items:
            if it["kind"] != "msg":
                continue
            message_id = _message_id(it)
            it["message_id"] = message_id
            if it.get("speaker") == left_name and message_id in manual_speakers:
                it["speaker"] = manual_speakers[message_id]

    # LLM 归属后处理（可选）：对仍未归到具体成员的左侧消息做语义修正
    if llm_cfg and confirmed:
        try:
            from llm_attrib import postprocess_left_speakers

            left_msgs = []
            for i, it in enumerate(items):
                if it["kind"] == "msg" and it.get("speaker") == left_name:
                    cands = []
                    for raw, _y0, y1 in labels_raw.get(it.get("img", ""), []):
                        if y1 <= it.get("y_screen", -1) - 5 and it.get("y_screen", -1) - y1 <= 150:
                            cands.append(raw)
                    left_msgs.append({"idx": i, "text": it["text"], "cand": cands[:5]})
            allowed = sorted(
                {_label_display(n, nick_to_name, auto_names) for n in confirmed if _label_display(n, nick_to_name, auto_names)}
                | {left_name}
            )
            attrib = postprocess_left_speakers(left_msgs, allowed, llm_cfg)
            for i, sp in attrib.items():
                if 0 <= i < len(items) and items[i]["kind"] == "msg":
                    items[i]["speaker"] = _mark(sp)
        except Exception as e:  # noqa: BLE001
            print(f"提示：LLM 归属后处理失败，已使用规则结果：{e}")

    merged = items

    # 为音频管线准备语音气泡位置。播放时间只用于日志，主文字稿仍按画面对话顺序排列。
    meta_by_name = {m["name"]: m for m in metas}
    voice_bubbles = []
    voice_results = voice_results or {}
    for it in merged:
        if not it.get("voice_id"):
            continue
        occurrences = []
        for img, y_screen in it.get("occ") or [(it.get("img", ""), it.get("y_screen", 0))]:
            meta = meta_by_name.get(img, {})
            raw_time = meta.get("time", it.get("time", ""))
            occurrences.append(
                {
                    "img": img,
                    "y_screen": round(float(y_screen), 2),
                    "time": raw_time,
                    "time_seconds": round(_time_seconds(raw_time), 3),
                }
            )
        candidate = {
            "voice_id": it["voice_id"],
            "duration_s": it["duration_s"],
            "side": it["side"],
            "speaker": it["speaker"],
            "message_time": it["time"],
            "content_y": round(float(it["y"]), 2),
            "x_center": round(float(it["x_center"]), 4),
            "x0": round(float(it["x0"]), 2),
            "x1": round(float(it["x1"]), 2),
            "y_screen": round(float(it["y_screen"]), 2),
            "occurrences": occurrences,
        }
        voice_bubbles.append(candidate)
        result = voice_results.get(it["voice_id"], {})
        status = result.get("status")
        transcript = str(result.get("text") or "").strip()
        if status == "transcribed" and transcript:
            it["text"] = f"【语音转写】{transcript}"
        elif status == "pending" and transcript:
            it["text"] = f"【语音转写·待核】{transcript}"
        else:
            it["text"] = "【语音·未能转写】"

    if group_mode:
        group_review = [
            {
                "id": group["id"],
                "number": index,
                "count": len(group["items"]),
                "samples": [
                    {"time": item.get("time", ""), "text": item.get("text", "")}
                    for item in group["items"][:3]
                ],
            }
            for index, group in enumerate(unresolved_groups, 1)
            if group["id"] not in group_assignments
        ]
        (raw_path.parent / "待核成员分组.json").write_text(
            json.dumps(group_review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        review = [
            {
                "id": it.get("message_id") or _message_id(it),
                "time": it.get("time", ""),
                "text": it.get("text", ""),
            }
            for it in merged
            if it.get("kind") == "msg" and it.get("speaker") == left_name
        ]
        (raw_path.parent / "待核成员消息.json").write_text(
            json.dumps(review, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    speaker_note = f"右侧={right_name}，左侧={left_name}"
    if confirmed:
        members = []
        for n in sorted(confirmed):
            d = _label_display(n, nick_to_name, auto_names)
            if d:
                members.append(_mark(d))
        speaker_note += f"，群聊成员按气泡上方昵称标注（{'、'.join(members)}）"
        if any("⚠" in m for m in members):
            speaker_note += "；带 ⚠ 的成员为自动识别的微信显示昵称，核对后可在预检的 视频信息.json 填写实名对应关系"

    context_labels = {
        "group_info": "打开微信群信息页",
        "chat_info": "打开聊天信息页",
        "contact_profile": "打开联系人资料页",
        "context_pending": "打开资料页面（待核）",
    }
    context_pages = [meta for meta in metas if meta["page_type"] in context_labels]
    auto_title = detect_chat_title(chat_lines_map, frames_dir) if chat_lines_map else None
    inferred_group = bool(auto_names or group_mode)
    if file_chat_type in {"group", "群聊"}:
        chat_type = "微信群"
    elif file_chat_type in {"1v1", "one_to_one", "一对一"}:
        chat_type = "一对一聊天"
    elif inferred_group:
        chat_type = "微信群"
    elif file_right or file_left:
        chat_type = "一对一聊天"
    else:
        chat_type = "待确认"

    # 生成 Markdown
    lines = [
        "# 聊天记录（自动识别稿）",
        "",
        f"- 来源视频：{video_name}",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"- 聊天类型：{chat_type}",
    ]
    if chat_type == "微信群":
        if file_group_name:
            lines.append(f"- 微信群名称：{file_group_name}")
        elif auto_title:
            lines.append(f"- 微信群名称：{auto_title}（自动识别·待核）")
        else:
            lines.append("- 微信群名称：未填写（可在预检的 视频信息.json 填写）")
    else:
        right_display = _mark(right_name) if right_name not in confirmed_displays and right_name != "我" else right_name
        left_display = _mark(left_name) if auto_left_detected else left_name
        lines.append(f"- 对话双方：右侧={right_display}；左侧={left_display}")
    lines += [
        f"- 说明：文字由本地 OCR 自动识别，可能有少量错字；说话人按消息气泡位置判断（{speaker_note}），仅供检索参考。",
        "",
    ]
    if context_pages:
        lines += ["## 页面记录", ""]
        for meta in context_pages:
            lines.append(f"- [{_fmt_time(meta['time'])}] {context_labels[meta['page_type']]}（截图：{meta['name']}）")
        lines.append("")

    # 录制人补充说明不是微信消息：它只来自用户明确开启的环境音转写，
    # 并根据发生时间插在相邻聊天段落之后。未设置姓名时绝不擅自填入某个人名。
    pending_narrator = sorted(
        [event for event in (narrator_events or []) if str(event.get("text") or "").strip()],
        key=lambda event: float(event.get("anchor_time", event.get("end", 0)) or 0),
    )
    narrator_index = 0

    def _append_narrator_until(time_seconds: float) -> None:
        nonlocal narrator_index
        while narrator_index < len(pending_narrator):
            event = pending_narrator[narrator_index]
            try:
                anchor = float(event.get("anchor_time", event.get("end", 0)) or 0)
            except (TypeError, ValueError):
                anchor = 0.0
            if anchor > time_seconds:
                break
            label = f"{file_narrator_name or '录制人'}补充说明"
            if str(event.get("status") or "") == "pending":
                label += "·待核"
            lines.append(f"- 【{label}】{str(event.get('text') or '').strip()}")
            narrator_index += 1

    current_date = None
    for it in merged:
        _append_narrator_until(_time_seconds(it.get("time", "")))
        if it["kind"] == "date":
            if it["text"] != current_date:
                current_date = it["text"]
                lines.append(f"## {it['text']}")
                lines.append("")
            continue
        if it["kind"] == "system":
            if current_date is None:
                current_date = "聊天记录"
                lines.append("## 聊天记录")
                lines.append("")
            lines.append(f"- [{it['time']}] 【系统消息】{it['text']}")
            continue
        if current_date is None:
            current_date = "聊天记录"
            lines.append("## 聊天记录")
            lines.append("")
        lines.append(f"- **{it['speaker']}** [{it['time']}] {it['text']}")
    _append_narrator_until(float("inf"))
    if len(lines) > 7:
        md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return {"voice_bubbles": voice_bubbles}
