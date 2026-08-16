#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 归属后处理：把“消息文本 + 上方昵称候选”交给可配置的文本模型，修正群聊说话人归属。

模型接口：OpenAI 兼容的 /chat/completions（本地 Ollama、DeepSeek API、各家云 API 均可）。
配置优先级：命令行参数 > 环境变量 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY。
未配置或调用失败时自动回退到规则结果，不影响出稿。
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

BATCH_SIZE = 200


def build_config(base_url: str = "", model: str = "", api_key: str = "", timeout: int = 90):
    cfg = {
        "base_url": base_url.strip() or os.environ.get("LLM_BASE_URL", "").strip(),
        "model": model.strip() or os.environ.get("LLM_MODEL", "").strip(),
        "api_key": (api_key.strip() if api_key else os.environ.get("LLM_API_KEY", "").strip()),
        "timeout": timeout,
    }
    if not cfg["base_url"] or not cfg["model"]:
        return None
    if not cfg["base_url"].endswith("/chat/completions"):
        cfg["base_url"] = cfg["base_url"].rstrip("/") + "/chat/completions"
    return cfg


def _call(cfg: dict, messages: list) -> str:
    body = {"model": cfg["model"], "messages": messages, "temperature": 0}
    req = urllib.request.Request(
        cfg["base_url"],
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    if cfg.get("api_key"):
        req.add_header("Authorization", f"Bearer {cfg['api_key']}")
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 90)) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def _extract_json(content: str):
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
    return None


def postprocess_left_speakers(messages: list, allowed_names: list, cfg: dict) -> dict:
    """messages: [{idx, text, cand:[上方昵称候选]}]; 返回 {idx: speaker}，失败返回 {}。

    批量调用模型，模型只允许从名单中选择；返回名单外的结果会被丢弃。
    """
    if not messages or cfg is None:
        return {}
    allowed = sorted({str(n) for n in allowed_names if n})
    result = {}
    for start in range(0, len(messages), BATCH_SIZE):
        batch = messages[start : start + BATCH_SIZE]
        lines = "\n".join(
            f"{m['idx']} | 文本：{m['text'][:120]} | 上方昵称候选：{'/'.join(m.get('cand', []) or ['无'])}"
            for m in batch
        )
        sys_prompt = (
            "你是聊天记录整理助手。给定左侧消息的文本和它上方的昵称候选，判断每条消息的说话人。"
            "只能从提供的名单中选择；确实无法判断时选“对方”。只输出 JSON，不要解释。"
        )
        user_prompt = (
            f"可用的左侧成员名单：{allowed}\n"
            f"消息列表（编号 | 文本 | 上方昵称候选）：\n{lines}\n"
            f'请输出：{{"attributions": [{{"idx": 编号, "speaker": "姓名"}}]}}'
        )
        try:
            content = _call(cfg, [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}])
            data = _extract_json(content)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(data, dict):
            continue
        for item in data.get("attributions") or []:
            idx = item.get("idx")
            speaker = str(item.get("speaker", "")).strip()
            if isinstance(idx, int) and speaker in allowed:
                result[idx] = speaker
    return result
