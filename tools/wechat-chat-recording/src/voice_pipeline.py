#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地语音处理：音轨预检、降噪提取、本地 Whisper 转写和语音气泡联合定位。

设计原则：
- 只处理本地文件，不调用网络转写服务；
- 画面中没有可对应语音气泡的声音不写入正式文字稿；
- 画面、时间或时长存在冲突时标记“待核”，不强行确定。
- 录制人补充说明是显式开启的独立通道，绝不伪装为微信消息。
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import cv2
import numpy as np

from config import APP_SUPPORT_DIR

MIC_WORDS = ("microphone", "mic", "external", "麦克风", "话筒", "外置")
SYSTEM_WORDS = ("system", "screen", "internal", "loopback", "系统", "屏幕", "内部")
NON_SPEECH_TEXTS = {
    "[music]", "[musical instrument]", "[silence]", "(音乐)", "[音乐]", "音乐", "（音乐）",
    "[applause]", "(掌声)", "[掌声]", "（掌声）",
}
LOCAL_MACWHISPER_PREFIXES = ("whisperkit:", "whisper-cpp:")


def _stream_label(stream: dict) -> str:
    if stream.get("label"):
        return str(stream["label"])
    tags = stream.get("tags") or {}
    values = [tags.get("title"), tags.get("name"), tags.get("handler_name"), tags.get("language"), stream.get("codec_name")]
    return " ".join(str(v) for v in values if v).strip()


def _contains_any(text: str, words) -> bool:
    low = text.lower()
    return any(word in low for word in words)


def select_audio_stream(streams: list[dict], requested: str = "auto") -> tuple[dict | None, bool, str]:
    """返回（选中音轨，是否存在歧义，选择说明）。index 使用 ffprobe 全局索引。"""
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not audio:
        return None, False, "视频不含音轨"
    requested = str(requested or "auto").strip().lower()
    if requested != "auto":
        try:
            wanted = int(requested)
        except ValueError:
            return None, True, f"音轨编号无效：{requested}"
        for stream in audio:
            if int(stream.get("index", -1)) == wanted:
                return stream, False, f"已指定音轨 {wanted}"
        return None, True, f"找不到音轨 {wanted}"

    scored = []
    for order, stream in enumerate(audio):
        label = _stream_label(stream)
        mic = _contains_any(label, MIC_WORDS)
        system = _contains_any(label, SYSTEM_WORDS)
        disposition = stream.get("disposition") or {}
        score = (30 if system else 0) - (100 if mic else 0) + (5 if disposition.get("default") else 0) - order
        scored.append((score, stream, mic, system, label))
    scored.sort(key=lambda x: x[0], reverse=True)
    selected = scored[0]
    non_mic = [x for x in scored if not x[2]]
    if non_mic:
        selected = non_mic[0]
    ambiguous = len(non_mic) > 1 and abs(non_mic[0][0] - non_mic[1][0]) <= 1
    if len(audio) == 1:
        reason = "仅有一条音轨"
    elif selected[3]:
        reason = "已优先选择系统/屏幕音轨，排除麦克风音轨"
    elif selected[2]:
        reason = "只能选到麦克风或混合音轨"
    else:
        reason = "已按默认音轨顺序选择"
    return selected[1], ambiguous, reason


def probe_audio(video: Path, requested: str = "auto", ffprobe: str | None = None) -> dict:
    ffprobe = ffprobe or shutil.which("ffprobe")
    result = {
        "ffprobe": ffprobe or "",
        "available": False,
        "audio_streams": [],
        "selected_stream": None,
        "ambiguous": False,
        "has_microphone_track": False,
        "reason": "",
    }
    if not ffprobe:
        result["reason"] = "未找到 ffprobe，无法检查音轨"
        return result
    cmd = [
        ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video),
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        result["reason"] = f"音轨检查失败：{(run.stderr or run.stdout).strip()[:300]}"
        return result
    try:
        data = json.loads(run.stdout)
    except json.JSONDecodeError:
        result["reason"] = "ffprobe 返回了无法解析的结果"
        return result
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    compact = []
    for stream in audio:
        label = _stream_label(stream)
        compact.append(
            {
                "index": int(stream.get("index", -1)),
                "codec_type": "audio",
                "codec_name": stream.get("codec_name", ""),
                "channels": int(stream.get("channels") or 0),
                "sample_rate": int(stream.get("sample_rate") or 0),
                "label": label,
                "is_microphone": _contains_any(label, MIC_WORDS),
                "is_system": _contains_any(label, SYSTEM_WORDS),
                "disposition": stream.get("disposition") or {},
                "tags": stream.get("tags") or {},
            }
        )
    selected, ambiguous, reason = select_audio_stream(compact, requested)
    result.update(
        {
            "available": bool(audio),
            "audio_streams": compact,
            "selected_stream": selected,
            "ambiguous": ambiguous,
            "has_microphone_track": any(s["is_microphone"] for s in compact),
            "reason": reason,
        }
    )
    return result


def resolve_whisper_cli() -> str | None:
    candidates = [
        os.environ.get("WHISPER_CPP_BIN", ""),
        APP_SUPPORT_DIR / "bin" / "whisper-cli",
        shutil.which("whisper-cli") or "",
        shutil.which("main") or "",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def resolve_macwhisper_cli() -> str | None:
    candidates = [
        os.environ.get("MACWHISPER_CLI", ""),
        shutil.which("mw") or "",
        Path("/Applications/MacWhisper.app/Contents/MacOS/mw"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def backend_cli_compatible(cli: str, backend: str) -> tuple[bool, str]:
    """不启动模型，只检查当前版本是否认识本工具必需的参数。"""
    try:
        cmd = [cli, "transcribe", "--help"] if backend == "macwhisper" else [cli, "--help"]
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"命令检查失败：{exc}"
    help_text = (run.stdout or "") + "\n" + (run.stderr or "")
    required = ("--format", "--no-speakers", "--overwrite") if backend == "macwhisper" else ("-ojf", "-sns", "-np")
    if run.returncode != 0 or any(flag not in help_text for flag in required):
        return False, "当前本地语音命令版本不支持所需参数"
    return True, "参数兼容"


def macwhisper_model_assets_present() -> bool:
    root = Path.home() / "Library" / "Application Support" / "MacWhisper" / "models" / "whisperkit" / "models"
    if not root.is_dir():
        return False
    try:
        return any(root.glob("**/TextDecoder.mlmodelc"))
    except OSError:
        return False


def validate_macwhisper_model_id(value: str) -> str:
    model_id = str(value or "").strip()
    if not model_id.startswith(LOCAL_MACWHISPER_PREFIXES) or not re.fullmatch(
        r"[A-Za-z0-9._-]+:[A-Za-z0-9._-]+", model_id
    ):
        raise ValueError("MacWhisper 只允许使用已安装的 whisperkit: 或 whisper-cpp: 本地模型")
    return model_id


def parse_macwhisper_models(output: str) -> list[str]:
    """只接受 MacWhisper 列表中的本地模型，并优先返回当前选中项。"""
    cleaned = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(output or ""))
    selected: list[str] = []
    available: list[str] = []
    pattern = r"(?:whisperkit|whisper-cpp):[A-Za-z0-9._-]+"
    for line in cleaned.splitlines():
        match = re.search(pattern, line)
        if not match:
            continue
        model_id = validate_macwhisper_model_id(match.group(0))
        if model_id not in available:
            available.append(model_id)
        if "▸" in line and model_id not in selected:
            selected.append(model_id)
    return selected + [model_id for model_id in available if model_id not in selected]


def probe_macwhisper(timeout: int = 30) -> dict:
    """确认 CLI、模型资产和实际调用能力；不转写、不下载、不保存历史。"""
    cli = resolve_macwhisper_cli()
    result = {
        "cli": cli or "",
        "assets_present": macwhisper_model_assets_present(),
        "callable": False,
        "models": [],
        "model": "",
        "reason": "",
    }
    if not cli:
        result["reason"] = "未找到 MacWhisper 本地命令"
        return result
    configured = os.environ.get("CHAT_RECORDING_MACWHISPER_MODEL_ID", "").strip()
    try:
        completed = subprocess.run(
            [cli, "models", "list"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"MacWhisper 模型检查失败：{exc}"
        return result
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip().splitlines()
        result["reason"] = "MacWhisper 当前无法调用" + (f"：{message[-1][:160]}" if message else "")
        return result
    models = parse_macwhisper_models(completed.stdout)
    result["models"] = models
    if configured:
        try:
            configured = validate_macwhisper_model_id(configured)
        except ValueError as exc:
            result["reason"] = str(exc)
            return result
        if configured not in models:
            result["reason"] = "指定的 MacWhisper 本地模型尚未下载"
            return result
        models = [configured] + [item for item in models if item != configured]
        result["models"] = models
    if not models:
        result["reason"] = "MacWhisper 未列出可用的本地模型"
        return result
    result["model"] = models[0]
    result["callable"] = True
    result["reason"] = "MacWhisper 本地模型可用"
    return result


def resolve_model(model_name: str) -> Path | None:
    explicit = os.environ.get("WHISPER_MODEL", "").strip()
    names = [f"ggml-{model_name}.bin", f"ggml-{model_name}-q5_1.bin"]
    candidates = [Path(explicit)] if explicit else []
    candidates += [APP_SUPPORT_DIR / "models" / name for name in names]
    for root in (Path("/opt/homebrew/share/whisper-cpp"), Path("/usr/local/share/whisper-cpp")):
        candidates += [root / name for name in names]
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_size > 1_000_000:
            return candidate
    return None


def voice_preflight(video: Path, mode: str, audio_track: str, model_name: str) -> dict:
    probe = probe_audio(video, audio_track)
    probe["mode"] = mode
    probe["ffmpeg"] = shutil.which("ffmpeg") or ""
    probe["backend"] = ""
    probe["backend_cli"] = ""
    probe["whisper_cli"] = resolve_whisper_cli() or ""
    probe["model"] = ""
    probe["model_label"] = ""
    probe["macwhisper"] = {
        "cli": resolve_macwhisper_cli() or "",
        "assets_present": macwhisper_model_assets_present(),
        "callable": False,
        "models": [],
        "model": "",
        "reason": "未执行调用检查",
    }
    if mode == "off":
        probe["ready"] = False
        probe["status"] = "语音识别已关闭"
    elif not probe["available"]:
        probe["ready"] = False
        probe["status"] = probe["reason"] or "无音轨"
    elif probe["ambiguous"] and str(audio_track) == "auto":
        probe["ready"] = False
        probe["status"] = "存在多条无法自动区分的音轨，请用 --audio-track 指定"
    elif not probe["ffmpeg"]:
        probe["ready"] = False
        probe["status"] = "未安装 ffmpeg"
    else:
        macwhisper = probe_macwhisper()
        probe["macwhisper"] = macwhisper
        mac_ok, mac_reason = backend_cli_compatible(macwhisper["cli"], "macwhisper") if macwhisper["callable"] else (False, "")
        if macwhisper["callable"] and mac_ok:
            probe["backend"] = "macwhisper"
            probe["backend_cli"] = macwhisper["cli"]
            probe["model"] = macwhisper["model"]
            probe["model_label"] = macwhisper["model"]
            probe["ready"] = True
            probe["status"] = f"本地语音识别已就绪（MacWhisper：{macwhisper['model']}）"
        else:
            model = resolve_model(model_name)
            cpp_ok, cpp_reason = backend_cli_compatible(probe["whisper_cli"], "whisper-cpp") if probe["whisper_cli"] and model else (False, "")
            if probe["whisper_cli"] and model and cpp_ok:
                probe["backend"] = "whisper-cpp"
                probe["backend_cli"] = probe["whisper_cli"]
                probe["model"] = str(model)
                probe["model_label"] = model.name
                probe["ready"] = True
                probe["status"] = f"本地语音识别已就绪（whisper.cpp：{model.name}）"
            else:
                probe["ready"] = False
                detail = mac_reason or macwhisper.get("reason") or cpp_reason or "MacWhisper 不可用"
                probe["status"] = f"未找到可调用的现有本地语音模型（{detail}）；本工具不会自动下载"
    return probe


def _clean_text(text: str) -> str:
    text = " ".join(str(text or "").strip().split())
    if text.lower() in NON_SPEECH_TEXTS:
        return ""
    return text.strip(" \t\r\n")


def parse_whisper_json(data: dict, time_offset: float = 0.0) -> list[dict]:
    segments = []
    for item in data.get("transcription") or []:
        offsets = item.get("offsets") or {}
        try:
            start = float(offsets.get("from", 0)) / 1000.0 + time_offset
            end = float(offsets.get("to", 0)) / 1000.0 + time_offset
        except (TypeError, ValueError):
            continue
        text = _clean_text(item.get("text", ""))
        if not text or end <= start:
            continue
        probs = []
        for token in item.get("tokens") or []:
            try:
                probs.append(float(token.get("p")))
            except (TypeError, ValueError):
                continue
        confidence = sum(probs) / len(probs) if probs else None
        segments.append(
            {
                "start": start,
                "end": end,
                "duration": end - start,
                "text": text,
                "confidence": confidence,
            }
        )
    return segments


def _parse_srt_time(value: str) -> float:
    match = re.fullmatch(r"\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*", value)
    if not match:
        raise ValueError(f"无法解析 SRT 时间：{value}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds + millis / 1000.0


def parse_srt(text: str, time_offset: float = 0.0) -> list[dict]:
    segments = []
    for block in re.split(r"\r?\n\s*\r?\n", str(text or "").strip()):
        lines = [line.strip("\ufeff\r") for line in block.splitlines() if line.strip()]
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        try:
            start_raw, end_raw = lines[timing_index].split("-->", 1)
            start = _parse_srt_time(start_raw) + time_offset
            end = _parse_srt_time(end_raw) + time_offset
        except (TypeError, ValueError):
            continue
        transcript = _clean_text(" ".join(lines[timing_index + 1 :]))
        if not transcript or end <= start:
            continue
        segments.append(
            {
                "start": start,
                "end": end,
                "duration": end - start,
                "text": transcript,
                "confidence": None,
            }
        )
    return segments


def _extract_audio(video: Path, wav_path: Path, stream_index: int, start: float, end: float, ffmpeg: str) -> None:
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if end > 0:
        cmd += ["-t", f"{max(0.01, end - start):.3f}"]
    cmd += [
        "-map", f"0:{stream_index}", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        "-af", "highpass=f=80,lowpass=f=8000,afftdn=nf=-25", str(wav_path),
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0:
        # 某些较旧 ffmpeg 没有 afftdn，降级为不降噪提取，但不影响原视频。
        fallback = [x for x in cmd]
        af_index = fallback.index("-af")
        del fallback[af_index : af_index + 2]
        run = subprocess.run(fallback, capture_output=True, text=True)
    if run.returncode != 0 or not wav_path.exists():
        raise RuntimeError(f"音轨提取失败：{(run.stderr or run.stdout).strip()[:500]}")


def _run_whisper(wav_path: Path, cli: str, model: Path, language: str, time_offset: float) -> list[dict]:
    prefix = wav_path.with_suffix("")
    out_json = Path(str(prefix) + ".json")
    cmd = [
        cli, "-m", str(model), "-f", str(wav_path), "-l", language or "zh", "-ojf", "-of", str(prefix),
        "-np", "-sns", "-t", str(max(1, min(8, os.cpu_count() or 4))),
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    if run.returncode != 0 or not out_json.exists():
        raise RuntimeError(f"本地语音转写失败：{(run.stderr or run.stdout).strip()[:500]}")
    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"无法解析本地语音转写结果：{exc}") from exc
    return parse_whisper_json(data, time_offset)


def _run_macwhisper(wav_path: Path, cli: str, model_id: str, language: str, time_offset: float) -> list[dict]:
    model_id = validate_macwhisper_model_id(model_id)
    out_srt = wav_path.with_name("transcript.srt")
    cmd = [
        cli,
        "transcribe",
        "--model",
        model_id,
        "--language",
        language or "zh",
        "--format",
        "srt",
        "--style",
        "subtitles",
        "--no-speakers",
        "-o",
        out_srt.name,
        "--overwrite",
        wav_path.name,
    ]
    # 不传 --persist；只传递匿名临时文件名，避免原路径进入 MacWhisper 日志。
    run = subprocess.run(cmd, capture_output=True, text=True, cwd=wav_path.parent, timeout=3600, check=False)
    if run.returncode != 0 or not out_srt.exists():
        raise RuntimeError(f"MacWhisper 本地转写失败：{(run.stderr or run.stdout).strip()[:500]}")
    return parse_srt(out_srt.read_text(encoding="utf-8-sig"), time_offset)


def _nearest_occurrence(candidate: dict, timestamp: float) -> tuple[float, dict | None]:
    occurrences = candidate.get("occurrences") or []
    if not occurrences:
        return math.inf, None
    best = min(occurrences, key=lambda x: abs(float(x.get("time_seconds", 0.0)) - timestamp))
    return abs(float(best.get("time_seconds", 0.0)) - timestamp), best


def _anchor_from_candidate(candidate: dict, frames_dir: Path):
    occurrences = candidate.get("occurrences") or []
    source = next((o for o in occurrences if (frames_dir / str(o.get("img", ""))).exists()), None)
    if source is None:
        return None
    image = cv2.imread(str(frames_dir / source["img"]), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    h, w = image.shape
    x0 = int(candidate.get("x0", candidate.get("x_center", 0.5) * w))
    x1 = int(candidate.get("x1", x0 + 40))
    yc = int(source.get("y_screen", candidate.get("y_screen", h / 2)))
    pad_x = max(8, (28 - (x1 - x0)) // 2)
    left = max(0, x0 - pad_x)
    right = min(w, x1 + pad_x)
    top = max(int(h * 0.10), yc - 18)
    bottom = min(int(h * 0.92), yc + 18)
    anchor = image[top:bottom, left:right]
    if anchor.shape[0] < 10 or anchor.shape[1] < 12:
        return None
    return anchor, w, h


def visual_playback_score(video: Path, candidate: dict, segment: dict, frames_dir: Path) -> dict:
    """在语音片段内检查候选气泡是否可见且存在局部播放动画。"""
    prepared = _anchor_from_candidate(candidate, frames_dir)
    if prepared is None:
        return {"visibility": 0.0, "activity": 0.0, "samples": 0, "visible_intervals": []}
    anchor, expected_w, expected_h = prepared
    duration = max(0.3, float(segment["end"]) - float(segment["start"]))
    sample_count = max(3, min(16, int(math.ceil(duration * 4))))
    times = np.linspace(float(segment["start"]), float(segment["end"]), sample_count)
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return {"visibility": 0.0, "activity": 0.0, "samples": 0, "visible_intervals": []}
    patches = []
    matches = []
    visible_times = []
    side = candidate.get("side", "left")
    try:
        for ts in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(ts)) * 1000.0)
            ok, frame = cap.read()
            if not ok:
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if gray.shape[1] != expected_w or gray.shape[0] != expected_h:
                anchor_scaled = cv2.resize(
                    anchor,
                    (max(12, round(anchor.shape[1] * gray.shape[1] / expected_w)), max(10, round(anchor.shape[0] * gray.shape[0] / expected_h))),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                anchor_scaled = anchor
            h, w = gray.shape
            sx0, sx1 = (int(0.02 * w), int(0.58 * w)) if side == "left" else (int(0.42 * w), int(0.98 * w))
            sy0, sy1 = int(0.10 * h), int(0.92 * h)
            search = gray[sy0:sy1, sx0:sx1]
            if search.shape[0] < anchor_scaled.shape[0] or search.shape[1] < anchor_scaled.shape[1]:
                continue
            response = cv2.matchTemplate(search, anchor_scaled, cv2.TM_CCOEFF_NORMED)
            _min_val, max_val, _min_loc, max_loc = cv2.minMaxLoc(response)
            matches.append(float(max_val))
            if max_val < 0.55:
                continue
            visible_times.append(round(float(ts), 3))
            y_center = sy0 + max_loc[1] + anchor_scaled.shape[0] // 2
            bx0, bx1 = (int(0.04 * w), int(0.54 * w)) if side == "left" else (int(0.46 * w), int(0.96 * w))
            by0, by1 = max(sy0, y_center - 34), min(sy1, y_center + 34)
            patch = gray[by0:by1, bx0:bx1]
            if patch.size:
                patches.append(cv2.resize(patch, (220, 56), interpolation=cv2.INTER_AREA))
    finally:
        cap.release()
    diffs = []
    for a, b in zip(patches, patches[1:]):
        diff = cv2.absdiff(a, b)
        diffs.append(float(np.mean(diff)))
    visibility = max(matches, default=0.0)
    # 静态压缩噪声通常低于 0.7；微信扬声器/波纹动画会产生连续局部变化。
    active_pairs = [d for d in diffs if d >= 0.8]
    activity = min(1.0, (sum(active_pairs) / max(1, len(diffs))) / 3.0) if diffs else 0.0
    intervals = []
    if visible_times:
        intervals.append({"start": min(visible_times), "end": max(visible_times)})
    return {"visibility": visibility, "activity": activity, "samples": len(patches), "visible_intervals": intervals}


def _duration_similarity(expected: float, actual: float) -> float:
    if expected <= 0 or actual <= 0:
        return 0.5
    return max(0.0, 1.0 - abs(expected - actual) / max(expected, actual, 1.0))


def associate_segments(
    segments: list[dict],
    candidates: list[dict],
    visual_scorer=None,
) -> tuple[list[dict], list[dict]]:
    """保守地将 ASR 片段关联到语音气泡；无画面可见证据的声音不会进入文字稿。"""
    associated = []
    unlinked = []
    for segment in segments:
        midpoint = (float(segment["start"]) + float(segment["end"])) / 2
        rankings = []
        for candidate in candidates:
            delta, occurrence = _nearest_occurrence(candidate, midpoint)
            expected = float(candidate.get("duration_s") or 0)
            # 截图时间只用于缩小搜索范围；最终必须由原视频中的可见证据确认。
            if delta > max(90.0, expected + 20.0):
                continue
            visual = visual_scorer(candidate, segment) if visual_scorer else {"visibility": 0.0, "activity": 0.0, "samples": 0, "visible_intervals": []}
            if float(visual.get("visibility", 0)) < 0.58:
                continue
            time_score = max(0.0, 1.0 - delta / 90.0)
            duration_score = _duration_similarity(expected, float(segment.get("duration") or 0))
            score = 0.50 * float(visual.get("activity", 0)) + 0.20 * float(visual.get("visibility", 0)) + 0.20 * time_score + 0.10 * duration_score
            rankings.append(
                {
                    "candidate": candidate,
                    "score": score,
                    "delta": delta,
                    "occurrence": occurrence,
                    "visual": visual,
                    "duration_score": duration_score,
                    "visible_intervals": visual.get("visible_intervals", []),
                }
            )
        rankings.sort(key=lambda x: x["score"], reverse=True)
        if not rankings:
            unlinked.append({**segment, "reason": "声音发生时未发现可见的语音气泡"})
            continue
        best = rankings[0]
        margin = best["score"] - (rankings[1]["score"] if len(rankings) > 1 else 0.0)
        visible_and_active = best["visual"].get("activity", 0) >= 0.18
        unique = margin >= 0.08 or len(rankings) == 1
        duration_conflict = best["duration_score"] < 0.25
        if visible_and_active and unique and not duration_conflict:
            status, reason = "transcribed", "画面播放动画、时间和时长相互印证"
        elif unique and not duration_conflict:
            status, reason = "pending", "已定位到唯一候选气泡，但播放动画或时长证据不足"
        else:
            unlinked.append({**segment, "reason": "存在多个候选气泡或定位证据冲突"})
            continue
        associated.append(
            {
                **segment,
                "voice_id": best["candidate"]["voice_id"],
                "status": status,
                "reason": reason,
                "association_score": round(best["score"], 4),
                "time_delta": round(best["delta"], 3),
                "visual": best["visual"],
                "visible_intervals": best["visible_intervals"],
            }
        )
    return associated, unlinked


def _audio_transient_flag(wav_path: Path, start: float, end: float) -> bool:
    """检测候选片段中非常突出的短时峰值，作为通知声重叠的待核信号。"""
    try:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            width = wf.getsampwidth()
            if width != 2:
                return False
            wf.setpos(max(0, min(wf.getnframes(), int(start * rate))))
            raw = wf.readframes(max(1, int((end - start) * rate)))
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        if len(samples) < rate // 4:
            return False
        window = max(80, rate // 50)
        rms = []
        for pos in range(0, len(samples) - window + 1, window):
            chunk = samples[pos : pos + window]
            rms.append(float(np.sqrt(np.mean(chunk * chunk) + 1.0)))
        if len(rms) < 4:
            return False
        median = float(np.median([x for x in rms if x > 1] or [1]))
        return max(rms) > max(6000.0, median * 7.0) and float(np.max(np.abs(samples))) > 30000
    except Exception:  # noqa: BLE001
        return False


def _cluster_associated(events: list[dict], expected_duration: float) -> list[list[dict]]:
    clusters = []
    for event in sorted(events, key=lambda x: x["start"]):
        if clusters and event["start"] - clusters[-1][-1]["end"] <= 1.5 and event["end"] - clusters[-1][0]["start"] <= max(8.0, expected_duration + 5.0):
            clusters[-1].append(event)
        else:
            clusters.append([event])
    return clusters


def _transcribe_wav(wav_path: Path, preflight: dict, language: str, time_offset: float) -> list[dict]:
    """用已经预检通过的本地后端转写临时音频，不下载模型也不保留音频。"""
    if preflight.get("backend") == "macwhisper":
        return _run_macwhisper(wav_path, preflight["backend_cli"], preflight["model"], language, time_offset)
    if preflight.get("backend") == "whisper-cpp":
        return _run_whisper(wav_path, preflight["backend_cli"], Path(preflight["model"]), language, time_offset)
    raise RuntimeError("语音预检未选定本地转写引擎")


def _interval_overlap(start: float, end: float, intervals: list[tuple[float, float]]) -> bool:
    return any(start < right and end > left for left, right in intervals)


def _merge_narrator_segments(segments: list[dict]) -> list[dict]:
    """相邻的 ASR 小片段合为一段说明，避免文字稿出现一串碎句。"""
    grouped: list[list[dict]] = []
    for segment in sorted(segments, key=lambda x: (x["start"], x["end"])):
        if grouped and segment["start"] - grouped[-1][-1]["end"] <= 1.2:
            grouped[-1].append(segment)
        else:
            grouped.append([segment])
    events = []
    for group in grouped:
        text = "".join(str(item.get("text") or "").strip() for item in group).strip()
        if not text:
            continue
        confidences = [float(item["confidence"]) for item in group if item.get("confidence") is not None]
        events.append(
            {
                "start": round(float(group[0]["start"]), 3),
                "end": round(float(group[-1]["end"]), 3),
                "anchor_time": round(float(group[-1]["end"]), 3),
                "text": text,
                # 未经画面气泡确认的环境声音不能标为确定事实；这里只确认其发生在非微信播放区间。
                "status": "pending" if not confidences or min(confidences) < 0.35 else "review",
                "reason": "未与已定位的微信语音播放区间重合，作为录制人补充说明待核",
            }
        )
    return events


def run_narrator_pipeline(
    video: Path,
    log_dir: Path,
    preflight: dict,
    voice_results: dict | None = None,
    asr_segments: list[dict] | None = None,
    *,
    mode: str = "off",
    start: float = 0.0,
    end: float = 0.0,
    language: str = "zh",
) -> tuple[list[dict], dict]:
    """提取非微信语音播放区间的环境人声，供文字稿作为“录制人补充说明”插入。

    该通道默认关闭；即使开启也只写入待核说明，不猜测录制人姓名、不把内容写成聊天消息。
    """
    log_path = Path(log_dir) / "narrator_events.jsonl"
    summary = {"enabled": mode == "auto", "segments": 0, "status": "录制人补充说明已关闭", "failed": False}
    engine = {
        "kind": "narrator_engine",
        "enabled": mode == "auto",
        "backend": preflight.get("backend", ""),
        "model": preflight.get("model_label", ""),
        "status": preflight.get("status", ""),
    }
    if mode != "auto":
        _write_jsonl(log_path, [engine])
        return [], summary
    if not preflight.get("ready"):
        summary["status"] = preflight.get("status") or "本地语音识别未就绪"
        _write_jsonl(log_path, [engine])
        return [], summary

    excluded: list[tuple[float, float]] = []
    for item in (voice_results or {}).values():
        try:
            left, right = float(item.get("playback_start")), float(item.get("playback_end"))
        except (TypeError, ValueError):
            continue
        if right > left:
            excluded.append((left, right))

    temp_ctx = tempfile.TemporaryDirectory(prefix="chat_narrator_")
    try:
        if asr_segments is None:
            wav_path = Path(temp_ctx.name) / "narrator.wav"
            stream = preflight.get("selected_stream") or {}
            _extract_audio(video, wav_path, int(stream["index"]), start, end, preflight["ffmpeg"])
            segments = _transcribe_wav(wav_path, preflight, language, start)
        else:
            # 微信语音识别已完成时复用同一轮本地 ASR 结果，避免重复读取和转写整段音轨。
            segments = list(asr_segments)
        remaining = [
            segment for segment in segments
            if not _interval_overlap(float(segment["start"]), float(segment["end"]), excluded)
        ]
        events = _merge_narrator_segments(remaining)
        summary.update({"segments": len(events), "status": "录制人补充说明已生成，均需人工核对"})
        _write_jsonl(log_path, [engine] + [{"kind": "narrator_segment", **event} for event in events])
        return events, summary
    except Exception as exc:  # noqa: BLE001
        summary.update({"status": str(exc), "failed": True})
        _write_jsonl(log_path, [engine, {"kind": "narrator_error", "reason": str(exc)}])
        return [], summary
    finally:
        temp_ctx.cleanup()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_voice_pipeline(
    video: Path,
    candidates: list[dict],
    frames_dir: Path,
    log_dir: Path,
    preflight: dict,
    *,
    start: float = 0.0,
    end: float = 0.0,
    language: str = "zh",
    keep_audio: bool = False,
) -> tuple[dict, dict]:
    """返回（voice_id -> 渲染结果，汇总统计），并写入 voice_events.jsonl。"""
    log_path = Path(log_dir) / "voice_events.jsonl"
    results = {
        c["voice_id"]: {"status": "untranscribed", "text": "", "reason": "录屏中未找到可确认的播放内容"}
        for c in candidates
    }
    base_summary = {
        "bubbles": len(candidates),
        "transcribed": 0,
        "pending": 0,
        "untranscribed": len(candidates),
        "unlinked_audio": 0,
        "status": preflight.get("status", ""),
        "failed": False,
        "backend": preflight.get("backend", ""),
        "model": preflight.get("model_label", ""),
    }
    engine_record = {
        "kind": "voice_engine",
        "backend": preflight.get("backend", ""),
        "model": preflight.get("model_label", ""),
        "ready": bool(preflight.get("ready")),
        "status": preflight.get("status", ""),
        "macwhisper": {
            "cli_present": bool((preflight.get("macwhisper") or {}).get("cli")),
            "assets_present": bool((preflight.get("macwhisper") or {}).get("assets_present")),
            "callable": bool((preflight.get("macwhisper") or {}).get("callable")),
            "reason": (preflight.get("macwhisper") or {}).get("reason", ""),
        },
    }
    if not candidates:
        _write_jsonl(log_path, [engine_record])
        return results, base_summary
    if not preflight.get("ready"):
        for value in results.values():
            value["reason"] = preflight.get("status") or value["reason"]
        _write_jsonl(
            log_path,
            [engine_record]
            + [{"kind": "voice_bubble", **c, "result": results[c["voice_id"]]} for c in candidates],
        )
        return results, base_summary

    stream = preflight["selected_stream"]
    temp_ctx = tempfile.TemporaryDirectory(prefix="chat_voice_")
    temp_dir = Path(temp_ctx.name)
    wav_path = temp_dir / "voice.wav"
    records = [engine_record]
    try:
        _extract_audio(video, wav_path, int(stream["index"]), start, end, preflight["ffmpeg"])
        if keep_audio:
            audit_dir = Path(log_dir) / "语音核对音频"
            audit_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(wav_path, audit_dir / "选定音轨_已轻度降噪.wav")
        segments = _transcribe_wav(wav_path, preflight, language, start)
        base_summary["_asr_segments"] = segments

        def scorer(candidate, segment):
            return visual_playback_score(video, candidate, segment, frames_dir)

        associated, unlinked = associate_segments(segments, candidates, scorer)
        for event in associated:
            rel_start, rel_end = max(0.0, event["start"] - start), max(0.0, event["end"] - start)
            event["notification_overlap"] = _audio_transient_flag(wav_path, rel_start, rel_end)
            if event["notification_overlap"]:
                event["status"] = "pending"
                event["reason"] = "语音片段中存在可疑突发通知声，需要人工核对"
            if event.get("confidence") is not None and event["confidence"] < 0.35:
                event["status"] = "pending"
                event["reason"] = "本地语音识别置信度偏低，需要人工核对"

        by_voice = {}
        for event in associated:
            by_voice.setdefault(event["voice_id"], []).append(event)
        for candidate in candidates:
            voice_id = candidate["voice_id"]
            events = by_voice.get(voice_id, [])
            if not events:
                continue
            clusters = _cluster_associated(events, float(candidate.get("duration_s") or 0))
            best = max(
                clusters,
                key=lambda group: (
                    1 if all(e["status"] == "transcribed" for e in group) else 0,
                    sum(float(e.get("association_score", 0)) for e in group) / len(group),
                    len("".join(e["text"] for e in group)),
                ),
            )
            status = "transcribed" if all(e["status"] == "transcribed" for e in best) else "pending"
            text = "".join(e["text"] for e in best).strip()
            results[voice_id] = {
                "status": status,
                "text": text,
                "reason": "；".join(dict.fromkeys(e["reason"] for e in best)),
                "playback_start": round(best[0]["start"], 3),
                "playback_end": round(best[-1]["end"], 3),
                "association_score": round(sum(e["association_score"] for e in best) / len(best), 4),
                "visible_intervals": [interval for event in best for interval in event.get("visible_intervals", [])],
                "playback_cluster_segments": len(best),
            }
        records.extend({"kind": "voice_bubble", **c, "result": results[c["voice_id"]]} for c in candidates)
        records.extend({"kind": "unlinked_audio", **event} for event in unlinked)
        base_summary["unlinked_audio"] = len(unlinked)
        base_summary["status"] = "语音处理完成"
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        base_summary["status"] = message
        base_summary["failed"] = True
        for value in results.values():
            value["reason"] = message
        records = [engine_record] + [
            {"kind": "voice_bubble", **c, "result": results[c["voice_id"]]} for c in candidates
        ]
    finally:
        temp_ctx.cleanup()

    _write_jsonl(log_path, records)
    base_summary["transcribed"] = sum(1 for x in results.values() if x["status"] == "transcribed")
    base_summary["pending"] = sum(1 for x in results.values() if x["status"] == "pending")
    base_summary["untranscribed"] = sum(1 for x in results.values() if x["status"] == "untranscribed")
    return results, base_summary


def load_voice_results(path: Path) -> dict:
    results = {}
    path = Path(path)
    if not path.exists():
        return results
    with open(path, encoding="utf-8") as f:
        for raw in f:
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if item.get("kind") == "voice_bubble" and item.get("voice_id") and isinstance(item.get("result"), dict):
                results[item["voice_id"]] = item["result"]
    return results


def load_narrator_events(path: Path) -> list[dict]:
    """读取已缓存的录制人说明，以便改名后只重排文字稿而不重转写。"""
    events = []
    path = Path(path)
    if not path.exists():
        return events
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if item.get("kind") == "narrator_segment" and str(item.get("text") or "").strip():
            events.append(item)
    return events
