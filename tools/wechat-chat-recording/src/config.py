#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工具统一配置。

实际配置保存在本机 Application Support，不跟随项目、OneDrive 或 Git 同步。
配置优先级：命令行显式参数 > 本机配置 > 环境变量（仅 LLM）> 内置默认值。
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path

APP_NAME = "聊天录屏工具"
APP_SUPPORT_DIR = Path(
    os.environ.get("CHAT_RECORDING_HOME", Path.home() / "Library" / "Application Support" / APP_NAME)
).expanduser()
CONFIG_FILE = APP_SUPPORT_DIR / "工具配置.json"

DEFAULT_CONFIG = {
    "overlap": 0.75,
    "page_size": "a4",
    "print_threshold": 0.80,
    "print_max_overlap": 0.65,
    "ocr": "auto",
    "ocr_langs": "zh-Hans,en-US",
    "me": "",
    "other": "",
    "voice": {
        "mode": "auto",
        "asr_model": "small",
        "language": "zh",
        "audio_track": "auto",
        "keep_audio": False,
    },
    "narrator": {
        "mode": "off",
        "name": "",
    },
    "llm": {
        "enabled": False,
        "base_url": "",
        "model": "",
    },
}


def ensure_template(path=CONFIG_FILE) -> Path:
    path = Path(path)
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            # 受限测试环境或只读运行时仍可使用内置默认值，不因无法写配置而中断处理。
            pass
    return path


def load(path=CONFIG_FILE) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def merged_config(path=CONFIG_FILE) -> dict:
    """用户配置只覆盖已提供字段，未写字段保留内置默认值。"""
    result = deepcopy(DEFAULT_CONFIG)
    user = load(path)
    for key, value in user.items():
        if key in ("voice", "narrator", "llm") and isinstance(value, dict):
            result[key].update(value)
        elif key in result:
            result[key] = value
    # 历史配置如曾写入 api_key，读取时也不使用；密钥只允许来自环境变量。
    result["llm"].pop("api_key", None)
    return result


def apply_to_args(args, path=CONFIG_FILE):
    """补全 argparse Namespace。

    需配置的命令行参数使用 argparse.SUPPRESS 作默认值，因此已存在的
    属性就代表用户显式传入，不会被配置文件覆盖。
    """
    cfg = merged_config(path)

    def _set_missing(name, value):
        if not hasattr(args, name):
            setattr(args, name, value)

    for key in ("overlap", "page_size", "print_threshold", "print_max_overlap", "ocr", "ocr_langs", "me", "other"):
        _set_missing(key, cfg[key])

    voice = cfg["voice"]
    _set_missing("voice", voice["mode"])
    _set_missing("asr_model", voice["asr_model"])
    _set_missing("asr_language", voice["language"])
    _set_missing("audio_track", str(voice["audio_track"]))
    _set_missing("keep_voice_audio", bool(voice["keep_audio"]))

    narrator = cfg["narrator"]
    _set_missing("narrator", narrator["mode"])
    _set_missing("narrator_name", str(narrator["name"]))

    llm = cfg["llm"]
    _set_missing("attrib_llm", bool(llm["enabled"]))
    _set_missing("attrib_base_url", str(llm["base_url"]))
    _set_missing("attrib_model", str(llm["model"]))
    _set_missing("attrib_key", "")
    return args
