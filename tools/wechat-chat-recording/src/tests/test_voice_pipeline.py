#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from PIL import Image
from docx import Document

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import apply_to_args, merged_config  # noqa: E402
from docx_builder import build_docx_from_markdown  # noqa: E402
from md_builder import build_markdown, detect_other_name  # noqa: E402
from ocr import Line, ocr_quality, recognize_all, recognize_with_quality_fallback  # noqa: E402
from page_classifier import classify_rows  # noqa: E402
from print_builder import _build_pdf, select_print_screens  # noqa: E402
from video2screens import open_video  # noqa: E402
import video2screens as v2s  # noqa: E402
import voice_pipeline as vp  # noqa: E402
import edit_names as name_editor  # noqa: E402
from voice_pipeline import (  # noqa: E402
    associate_segments,
    _audio_transient_flag,
    load_voice_results,
    parse_macwhisper_models,
    parse_srt,
    parse_whisper_json,
    select_audio_stream,
    visual_playback_score,
)


class ConfigTests(unittest.TestCase):
    def test_cli_value_wins_and_missing_values_use_config(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "overlap": 0.61,
                        "page_size": "letter",
                        "voice": {"mode": "off", "asr_model": "medium"},
                        "llm": {"api_key": "must-not-load"},
                    }
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(overlap=0.88)
            apply_to_args(args, path)
            self.assertEqual(args.overlap, 0.88)
            self.assertEqual(args.page_size, "letter")
            self.assertEqual(args.voice, "off")
            self.assertEqual(args.asr_model, "medium")
            self.assertEqual(args.attrib_key, "")
            self.assertNotIn("api_key", merged_config(path)["llm"])
            self.assertEqual(args.narrator, "off")
            self.assertEqual(args.narrator_name, "")


class WordAndNarratorTests(unittest.TestCase):
    def test_docx_is_exported_from_internal_markdown(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            md = root / "聊天记录.md"
            out = root / "聊天记录.docx"
            md.write_text(
                "# 聊天记录（自动识别稿）\n\n## 2025年6月16日 上午11:24\n\n- **测试用户** [00:00:00] 测试消息\n- 【录制人补充说明】背景说明\n",
                encoding="utf-8",
            )
            build_docx_from_markdown(md, out)
            self.assertTrue(out.exists())
            text = "\n".join(p.text for p in Document(out).paragraphs)
            self.assertIn("测试用户 [00:00:00] 测试消息", text)
            self.assertIn("录制人补充说明", text)

    def test_narrator_description_uses_generic_label_and_time_position(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            frames = root / "01_截图"
            frames.mkdir()
            for name in ("0001.png", "0002.png"):
                Image.new("RGB", (540, 960), "white").save(frames / name)
            rows = [
                {"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "chat"},
                {"文件名": "0002.png", "视频时间": "00:00:06.000", "累计位移(像素)": "500", "页面类型": "chat"},
            ]
            lines_map = {
                str(frames / "0001.png"): [Line("第一条消息", 0.12, 0.36, 0.30, 0.04)],
                str(frames / "0002.png"): [Line("第二条消息", 0.12, 0.36, 0.30, 0.04)],
            }
            names = root / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group"}), encoding="utf-8")
            md = root / "聊天记录.md"
            build_markdown(
                "sample.mp4", rows, lines_map, frames, md, root / "raw.ndjson", names_file=names,
                narrator_events=[{"anchor_time": 4.0, "text": "这是背景说明", "status": "review"}],
            )
            text = md.read_text(encoding="utf-8")
            self.assertIn("【录制人补充说明】这是背景说明", text)
            self.assertLess(text.index("第一条消息"), text.index("录制人补充说明"))
            self.assertLess(text.index("录制人补充说明"), text.index("第二条消息"))
            self.assertNotIn("测试用户补充说明", text)

    def test_narrator_segments_merge_and_exclude_playback_ranges(self):
        merged = vp._merge_narrator_segments(
            [
                {"start": 1.0, "end": 2.0, "text": "第一句", "confidence": 0.8},
                {"start": 2.3, "end": 3.0, "text": "第二句", "confidence": 0.8},
                {"start": 5.0, "end": 6.0, "text": "第三句", "confidence": 0.8},
            ]
        )
        self.assertEqual([item["text"] for item in merged], ["第一句第二句", "第三句"])
        self.assertTrue(vp._interval_overlap(1.5, 2.5, [(2.0, 4.0)]))
        self.assertFalse(vp._interval_overlap(1.0, 1.5, [(2.0, 4.0)]))


class AudioSelectionTests(unittest.TestCase):
    def test_system_track_beats_microphone_track(self):
        streams = [
            {"index": 1, "codec_type": "audio", "label": "External Microphone", "tags": {}, "disposition": {}},
            {"index": 2, "codec_type": "audio", "label": "System Audio", "tags": {}, "disposition": {"default": 1}},
        ]
        selected, ambiguous, reason = select_audio_stream(streams)
        self.assertEqual(selected["index"], 2)
        self.assertFalse(ambiguous)
        self.assertIn("系统", reason)

    def test_explicit_track_is_respected(self):
        streams = [
            {"index": 3, "codec_type": "audio", "label": "A", "tags": {}, "disposition": {}},
            {"index": 5, "codec_type": "audio", "label": "B", "tags": {}, "disposition": {}},
        ]
        selected, ambiguous, _reason = select_audio_stream(streams, "5")
        self.assertEqual(selected["index"], 5)
        self.assertFalse(ambiguous)

    def test_ffprobe_name_tag_is_used_for_track_role(self):
        streams = [
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"name": "System Audio"}, "disposition": {"default": 1}},
            {"index": 2, "codec_type": "audio", "codec_name": "aac", "tags": {"name": "External Microphone"}, "disposition": {}},
        ]
        selected, ambiguous, _reason = select_audio_stream(streams)
        self.assertEqual(selected["index"], 1)
        self.assertFalse(ambiguous)


class WhisperParsingTests(unittest.TestCase):
    def test_offsets_are_milliseconds_and_receive_video_offset(self):
        data = {
            "transcription": [
                {"offsets": {"from": 1200, "to": 4600}, "text": " 付款节点需要确认 ", "tokens": [{"p": 0.8}, {"p": 0.6}]},
                {"offsets": {"from": 5000, "to": 5500}, "text": "[music]"},
            ]
        }
        parsed = parse_whisper_json(data, 10.0)
        self.assertEqual(len(parsed), 1)
        self.assertAlmostEqual(parsed[0]["start"], 11.2)
        self.assertAlmostEqual(parsed[0]["end"], 14.6)
        self.assertAlmostEqual(parsed[0]["confidence"], 0.7)

    def test_loud_short_transient_is_flagged_for_review(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "notification.wav"
            rate = 16000
            samples = np.full(rate, 500, dtype=np.int16)
            samples[8000:8320] = 32767
            with wave.open(str(path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(rate)
                wf.writeframes(samples.tobytes())
            self.assertTrue(_audio_transient_flag(path, 0.0, 1.0))

    def test_macwhisper_srt_receives_video_offset(self):
        parsed = parse_srt(
            "1\n00:00:00,100 --> 00:00:02,700\n这个付款节点需要确认\n",
            10.0,
        )
        self.assertEqual(len(parsed), 1)
        self.assertAlmostEqual(parsed[0]["start"], 10.1)
        self.assertAlmostEqual(parsed[0]["end"], 12.7)
        self.assertEqual(parsed[0]["text"], "这个付款节点需要确认")


class LocalVoiceBackendTests(unittest.TestCase):
    def test_macwhisper_model_list_prefers_selected_and_rejects_cloud(self):
        output = """
          whisperkit:openai_whisper-small  Small
        ▸ whisperkit:openai_whisper-large-v3-v20240930  Large v3 Turbo
          openai:gpt-4o-transcribe  Cloud
        """
        self.assertEqual(
            parse_macwhisper_models(output),
            ["whisperkit:openai_whisper-large-v3-v20240930", "whisperkit:openai_whisper-small"],
        )

    def test_preflight_prefers_callable_macwhisper_model(self):
        audio = {
            "available": True,
            "ambiguous": False,
            "selected_stream": {"index": 1},
            "audio_streams": [{"index": 1}],
            "has_microphone_track": False,
            "reason": "仅有一条音轨",
        }
        local = {
            "cli": "/Applications/MacWhisper.app/Contents/MacOS/mw",
            "assets_present": True,
            "callable": True,
            "models": ["whisperkit:openai_whisper-large-v3-v20240930"],
            "model": "whisperkit:openai_whisper-large-v3-v20240930",
            "reason": "MacWhisper 本地模型可用",
        }
        with mock.patch.object(vp, "probe_audio", return_value=audio), mock.patch.object(
            vp.shutil, "which", side_effect=lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else None
        ), mock.patch.object(vp, "resolve_whisper_cli", return_value="/opt/homebrew/bin/whisper-cli"), mock.patch.object(
            vp, "resolve_macwhisper_cli", return_value=local["cli"]
        ), mock.patch.object(vp, "macwhisper_model_assets_present", return_value=True), mock.patch.object(
            vp, "probe_macwhisper", return_value=local
        ), mock.patch.object(vp, "resolve_model", side_effect=AssertionError("whisper.cpp fallback should not run")):
            result = vp.voice_preflight(Path("sample.mp4"), "auto", "auto", "small")
        self.assertTrue(result["ready"])
        self.assertEqual(result["backend"], "macwhisper")
        self.assertEqual(result["model"], local["model"])

    def test_preflight_falls_back_to_existing_whisper_cpp_model(self):
        audio = {
            "available": True,
            "ambiguous": False,
            "selected_stream": {"index": 1},
            "audio_streams": [{"index": 1}],
            "has_microphone_track": False,
            "reason": "仅有一条音轨",
        }
        unavailable = {
            "cli": "",
            "assets_present": False,
            "callable": False,
            "models": [],
            "model": "",
            "reason": "未找到 MacWhisper 本地命令",
        }
        with mock.patch.object(vp, "probe_audio", return_value=audio), mock.patch.object(
            vp.shutil, "which", side_effect=lambda name: "/opt/homebrew/bin/ffmpeg" if name == "ffmpeg" else None
        ), mock.patch.object(vp, "resolve_whisper_cli", return_value="/opt/homebrew/bin/whisper-cli"), mock.patch.object(
            vp, "resolve_macwhisper_cli", return_value=None
        ), mock.patch.object(vp, "macwhisper_model_assets_present", return_value=False), mock.patch.object(
            vp, "probe_macwhisper", return_value=unavailable
        ), mock.patch.object(vp, "resolve_model", return_value=Path("/local/models/ggml-small.bin")):
            result = vp.voice_preflight(Path("sample.mp4"), "auto", "auto", "small")
        self.assertTrue(result["ready"])
        self.assertEqual(result["backend"], "whisper-cpp")
        self.assertEqual(result["model_label"], "ggml-small.bin")

    def test_default_environment_check_does_not_request_model_download(self):
        script = (ROOT / "安装或修复运行环境.command").read_text(encoding="utf-8")
        self.assertIn('MODEL_NAME="${1:-}"', script)
        guard = script.index('if [ -n "$MODEL_NAME" ]; then')
        download = script.index("curl -fL --retry 3")
        self.assertLess(guard, download)

    def test_macwhisper_transcription_is_anonymous_and_not_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            wav = Path(td) / "voice.wav"
            wav.write_bytes(b"anonymous")
            commands = []

            def fake_run(command, **kwargs):
                commands.append((command, kwargs))
                (Path(kwargs["cwd"]) / "transcript.srt").write_text(
                    "1\n00:00:00,001 --> 00:00:02,700\n本地转写结果\n",
                    encoding="utf-8",
                )
                return vp.subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.object(vp.subprocess, "run", side_effect=fake_run):
                segments = vp._run_macwhisper(
                    wav,
                    "/opt/homebrew/bin/mw",
                    "whisperkit:openai_whisper-large-v3-v20240930",
                    "zh",
                    5.0,
                )
        command, kwargs = commands[0]
        self.assertNotIn("--persist", command)
        self.assertEqual(command[-1], "voice.wav")
        self.assertFalse(any(str(td) in str(part) for part in command))
        self.assertEqual(kwargs["cwd"], wav.parent)
        self.assertEqual(segments[0]["text"], "本地转写结果")
        self.assertAlmostEqual(segments[0]["start"], 5.001)


class OCRFallbackTests(unittest.TestCase):
    class FakeBackend:
        def __init__(self, name, output):
            self.name = name
            self.output = output
            self.calls = []

        def recognize(self, images, _langs):
            self.calls.append([str(path) for path in images])
            return {str(path): list(self.output.get(str(path), [])) for path in images}

    class FailingBackend:
        name = "vision"

        def recognize(self, _images, _langs):
            raise RuntimeError("Vision unavailable")

    def test_low_quality_vision_result_uses_better_tesseract_result(self):
        image = Path("frame.png")
        vision = self.FakeBackend("vision", {str(image): [Line("�", 0.1, 0.1, 0.1, 0.1)]})
        tesseract = self.FakeBackend(
            "tesseract",
            {str(image): [Line("这个付款节点需要再确认一下", 0.1, 0.1, 0.5, 0.1)]},
        )
        result, events = recognize_with_quality_fallback([image], vision, tesseract, "zh-Hans", quiet=True)
        self.assertEqual(result[str(image)][0].text, "这个付款节点需要再确认一下")
        self.assertEqual(events[0]["selected"], "tesseract")
        self.assertTrue(events[0]["fallback_attempted"])

    def test_good_vision_result_does_not_run_fallback(self):
        image = Path("frame.png")
        text = "项目合同付款节点需要再确认一下下午回复"
        vision = self.FakeBackend("vision", {str(image): [Line(text, 0.1, 0.1, 0.6, 0.1)]})
        tesseract = self.FakeBackend("tesseract", {})
        result, events = recognize_with_quality_fallback([image], vision, tesseract, "zh-Hans", quiet=True)
        self.assertEqual(result[str(image)][0].text, text)
        self.assertEqual(events[0]["selected"], "vision")
        self.assertFalse(events[0]["fallback_attempted"])
        self.assertFalse(tesseract.calls)
        self.assertFalse(ocr_quality(result[str(image)])["needs_fallback"])

    def test_primary_engine_failure_uses_local_fallback(self):
        image = Path("frame.png")
        tesseract = self.FakeBackend(
            "tesseract",
            {str(image): [Line("本机备用识别结果", 0.1, 0.1, 0.5, 0.1)]},
        )
        result, events = recognize_with_quality_fallback(
            [image], self.FailingBackend(), tesseract, "zh-Hans", quiet=True
        )
        self.assertEqual(result[str(image)][0].text, "本机备用识别结果")
        self.assertEqual(events[0]["selected"], "tesseract")
        self.assertIsNone(events[0]["primary_quality"])

    def test_vision_ocr_is_split_into_small_batches_for_long_recordings(self):
        images = [Path(f"frame-{i:04d}.png") for i in range(50)]
        vision = self.FakeBackend("vision", {})
        recognize_all(images, vision, "zh-Hans", quiet=True)
        self.assertEqual([len(batch) for batch in vision.calls], [24, 24, 2])


class AssociationTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [
            {"voice_id": "voice_0001", "duration_s": 4, "occurrences": [{"time_seconds": 10.0}]},
            {"voice_id": "voice_0002", "duration_s": 6, "occurrences": [{"time_seconds": 32.0}]},
        ]

    @staticmethod
    def visual(candidate, _segment):
        if candidate["voice_id"] == "voice_0001":
            return {"visibility": 0.91, "activity": 0.75, "samples": 8}
        return {"visibility": 0.2, "activity": 0.0, "samples": 2}

    def test_clear_visual_match_is_automatic(self):
        linked, unlinked = associate_segments(
            [{"start": 10.0, "end": 14.0, "duration": 4.0, "text": "需要确认", "confidence": 0.8}],
            self.candidates,
            self.visual,
        )
        self.assertEqual(len(linked), 1)
        self.assertFalse(unlinked)
        self.assertEqual(linked[0]["voice_id"], "voice_0001")
        self.assertEqual(linked[0]["status"], "transcribed")

    def test_ambient_speech_without_nearby_bubble_is_not_linked(self):
        linked, unlinked = associate_segments(
            [{"start": 100.0, "end": 103.0, "duration": 3.0, "text": "环境说话", "confidence": 0.8}],
            self.candidates,
            self.visual,
        )
        self.assertFalse(linked)
        self.assertEqual(len(unlinked), 1)

    def test_unique_but_static_bubble_is_pending(self):
        linked, _unlinked = associate_segments(
            [{"start": 31.0, "end": 37.0, "duration": 6.0, "text": "待核文字", "confidence": 0.8}],
            self.candidates,
            lambda _candidate, _segment: {"visibility": 0.8, "activity": 0.0, "samples": 8},
        )
        self.assertEqual(linked[0]["status"], "pending")

    def test_sound_without_visible_bubble_is_not_pending(self):
        linked, unlinked = associate_segments(
            [{"start": 17.0, "end": 20.0, "duration": 3.0, "text": "环境声音", "confidence": 0.8}],
            [{"voice_id": "voice_0001", "duration_s": 3, "occurrences": [{"time_seconds": 0.0}]}],
            lambda _candidate, _segment: {"visibility": 0.0, "activity": 0.0, "samples": 0},
        )
        self.assertFalse(linked)
        self.assertEqual(len(unlinked), 1)


class MarkdownVoiceTests(unittest.TestCase):
    def test_top_bar_symbol_is_not_accepted_as_a_person_name(self):
        with tempfile.TemporaryDirectory() as td:
            frames = Path(td)
            Image.new("RGB", (540, 960), "white").save(frames / "0001.png")
            lines = {str(frames / "0001.png"): [Line("<", 0.45, 0.03, 0.02, 0.02)]}
            self.assertIsNone(detect_other_name(lines, frames))

    def test_voice_transcript_is_inline_and_simple(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = frames / "0001.png"
            Image.new("RGB", (540, 960), "white").save(image)
            rows = [
                {
                    "文件名": "0001.png",
                    "视频时间": "00:00:12.000",
                    "累计位移(像素)": "0",
                }
            ]
            lines_map = {str(image): [Line('4"', 0.70, 0.40, 0.08, 0.035)]}
            names = work / "成员名单.json"
            names.write_text(json.dumps({"right": "张三", "left": "李四", "nick_to_name": {}}), encoding="utf-8")
            md = work / "04_文字稿" / "聊天记录.md"
            raw = work / "03_日志" / "ocr_raw.ndjson"
            first = build_markdown("sample.mp4", rows, lines_map, frames, md, raw, names_file=names)
            self.assertEqual(first["voice_bubbles"][0]["voice_id"], "voice_0001")
            build_markdown(
                "sample.mp4",
                rows,
                lines_map,
                frames,
                md,
                raw,
                names_file=names,
                voice_results={"voice_0001": {"status": "transcribed", "text": "这个付款节点需要确认。"}},
            )
            text = md.read_text(encoding="utf-8")
            self.assertIn("- **张三** [00:00:12] 【语音转写】这个付款节点需要确认。", text)
            self.assertNotIn("voice_0001", text)

    def test_cached_voice_result_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "voice_events.jsonl"
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"kind": "voice_bubble", "voice_id": "voice_0001", "result": {"status": "pending", "text": "待核"}}, ensure_ascii=False) + "\n")
                f.write(json.dumps({"kind": "unlinked_audio", "text": "环境音"}, ensure_ascii=False) + "\n")
            loaded = load_voice_results(path)
            self.assertEqual(loaded["voice_0001"]["status"], "pending")
            self.assertEqual(len(loaded), 1)

    def test_bare_page_numbers_are_not_voice_bubbles(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = frames / "0001.png"
            Image.new("RGB", (540, 960), "white").save(image)
            rows = [{"文件名": "0001.png", "视频时间": "00:00:12.000", "累计位移(像素)": "0", "页面类型": "chat"}]
            for number in ("1", "21", "60"):
                lines_map = {str(image): [Line(number, 0.70, 0.40, 0.08, 0.035)]}
                result = build_markdown(
                    "sample.mp4", rows, lines_map, frames, work / f"chat-{number}.md", work / f"raw-{number}.ndjson"
                )
                self.assertEqual(result["voice_bubbles"], [], number)

    def test_context_page_is_header_record_not_chat_message(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            for name in ("0001.png", "0002.png"):
                Image.new("RGB", (540, 960), "white").save(frames / name)
            rows = [
                {"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "group_info"},
                {"文件名": "0002.png", "视频时间": "00:00:04.000", "累计位移(像素)": "20", "页面类型": "chat"},
            ]
            lines_map = {
                str(frames / "0001.png"): [Line("群管理", 0.2, 0.4, 0.2, 0.04)],
                str(frames / "0002.png"): [Line("正常聊天内容", 0.62, 0.4, 0.2, 0.04)],
            }
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group", "group_name": "项目群", "right": "张三", "left": "李四"}), encoding="utf-8")
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, work / "raw.ndjson", names_file=names)
            text = md.read_text(encoding="utf-8")
            self.assertIn("- 微信群名称：项目群", text)
            self.assertIn("- [00:00:02] 打开微信群信息页（截图：0001.png）", text)
            self.assertIn("正常聊天内容", text)
            self.assertNotIn("**李四** [00:00:02] 群管理", text)

    def test_group_messages_keep_distinct_nearby_nicknames(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            for name in ("0001.png", "0002.png"):
                Image.new("RGB", (540, 960), "white").save(frames / name)
            rows = [
                {"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "chat"},
                {"文件名": "0002.png", "视频时间": "00:00:04.000", "累计位移(像素)": "500", "页面类型": "chat"},
            ]
            lines_map = {
                str(frames / "0001.png"): [Line("王五", 0.10, 0.30, 0.10, 0.03), Line("第一条消息", 0.10, 0.36, 0.24, 0.04)],
                str(frames / "0002.png"): [Line("赵六", 0.10, 0.30, 0.10, 0.03), Line("第二条消息", 0.10, 0.36, 0.24, 0.04)],
            }
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group", "group_name": "项目群"}), encoding="utf-8")
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, work / "raw.ndjson", names_file=names)
            text = md.read_text(encoding="utf-8")
            self.assertIn("**王五⚠** [00:00:02] 第一条消息", text)
            self.assertIn("**赵六⚠** [00:00:04] 第二条消息", text)
            self.assertNotIn("**左侧成员**", text)

    def test_long_group_nickname_is_used_as_speaker_not_message(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = frames / "0001.png"
            Image.new("RGB", (540, 960), "white").save(image)
            rows = [{"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "chat"}]
            nickname = "某机构项目负责人张三"
            lines_map = {
                str(image): [
                    Line(nickname, 0.18, 0.30, 0.37, 0.021),
                    Line("今天我们确认后就要启动挂牌", 0.20, 0.345, 0.69, 0.035),
                ]
            }
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group"}), encoding="utf-8")
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, work / "raw.ndjson", names_file=names)
            text = md.read_text(encoding="utf-8")
            self.assertIn(f"**{nickname}⚠** [00:00:02] 今天我们确认后就要启动挂牌", text)
            self.assertNotIn(f"] {nickname}", text)

    def test_confirmed_orphan_nickname_is_not_emitted_as_message(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = frames / "0001.png"
            Image.new("RGB", (540, 960), "white").save(image)
            nickname = "某机构李四"
            rows = [{"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "chat"}]
            lines_map = {str(image): [Line(nickname, 0.18, 0.30, 0.22, 0.021)]}
            names = work / "成员名单.json"
            names.write_text(
                json.dumps({"chat_type": "group", "nick_to_name": {nickname: "李四"}}), encoding="utf-8"
            )
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, work / "raw.ndjson", names_file=names)
            self.assertNotIn("**李四**", md.read_text(encoding="utf-8"))

    def test_multiline_green_bubble_is_merged_and_keeps_right_speaker(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = Image.new("RGB", (540, 960), "white")
            image.paste((149, 236, 105), (90, 300, 510, 450))
            path = frames / "0001.png"
            image.save(path)
            rows = [{"文件名": "0001.png", "视频时间": "00:00:23.000", "累计位移(像素)": "0", "页面类型": "chat"}]
            lines_map = {
                str(path): [
                    Line("第一段测试文本", 0.20, 0.34, 0.69, 0.034),
                    Line("第二段测试文本", 0.20, 0.375, 0.69, 0.034),
                    Line("第三段测试文本。", 0.20, 0.41, 0.31, 0.034),
                ]
            }
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group", "right": "测试用户"}), encoding="utf-8")
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, work / "raw.ndjson", names_file=names)
            text = md.read_text(encoding="utf-8")
            self.assertIn(
                "**测试用户** [00:00:23] 第一段测试文本第二段测试文本第三段测试文本。",
                text,
            )
            self.assertEqual(text.count("[00:00:23]"), 1)

    def test_group_membership_system_message_is_not_an_unknown_member(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = frames / "0001.png"
            Image.new("RGB", (540, 960), "white").save(image)
            rows = [{"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "chat"}]
            lines_map = {str(image): [Line("张三邀请李四加入了群聊", 0.50, 0.36, 0.30, 0.04)]}
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group"}), encoding="utf-8")
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, work / "raw.ndjson", names_file=names)
            text = md.read_text(encoding="utf-8")
            self.assertIn("[00:00:02] 【系统消息】张三邀请李四加入了群聊", text)
            self.assertNotIn("**未识别成员", text)
            self.assertEqual(json.loads((work / "待核成员分组.json").read_text(encoding="utf-8")), [])

    def test_manual_group_speaker_assignment_replaces_only_selected_message(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            image = frames / "0001.png"
            Image.new("RGB", (540, 960), "white").save(image)
            rows = [{"文件名": "0001.png", "视频时间": "00:00:02.000", "累计位移(像素)": "0", "页面类型": "chat"}]
            lines_map = {str(image): [Line("需要人工归属的消息", 0.10, 0.36, 0.30, 0.04)]}
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group"}), encoding="utf-8")
            raw = work / "raw.ndjson"
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, raw, names_file=names)
            review = json.loads((work / "待核成员消息.json").read_text(encoding="utf-8"))
            self.assertEqual(len(review), 1)
            names.write_text(
                json.dumps({"chat_type": "group", "manual_message_speakers": {review[0]["id"]: "王五"}}),
                encoding="utf-8",
            )
            build_markdown("sample.mp4", rows, lines_map, frames, md, raw, names_file=names)
            self.assertIn("**王五** [00:00:02] 需要人工归属的消息", md.read_text(encoding="utf-8"))

    def test_unknown_group_members_are_clustered_by_avatar_and_renamed_together(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "01_截图"
            frames.mkdir()
            images = []
            for name in ("0001.png", "0002.png", "0003.png"):
                image = Image.new("RGB", (540, 960), "white")
                color = (220, 30, 30) if name != "0003.png" else (30, 60, 220)
                image.paste(color, (8, 315, 108, 415))
                path = frames / name
                image.save(path)
                images.append(path)
            rows = [
                {"文件名": name, "视频时间": f"00:00:0{i + 2}.000", "累计位移(像素)": str(i * 500), "页面类型": "chat"}
                for i, name in enumerate(("0001.png", "0002.png", "0003.png"))
            ]
            lines_map = {
                str(images[0]): [Line("第一条消息", 0.10, 0.36, 0.24, 0.04)],
                str(images[1]): [Line("第二条消息", 0.10, 0.36, 0.24, 0.04)],
                str(images[2]): [Line("第三条消息", 0.10, 0.36, 0.24, 0.04)],
            }
            names = work / "成员名单.json"
            names.write_text(json.dumps({"chat_type": "group"}), encoding="utf-8")
            raw = work / "raw.ndjson"
            md = work / "chat.md"
            build_markdown("sample.mp4", rows, lines_map, frames, md, raw, names_file=names)
            groups = json.loads((work / "待核成员分组.json").read_text(encoding="utf-8"))
            self.assertEqual([group["count"] for group in groups], [2, 1])
            self.assertIn("**未识别成员1⚠** [00:00:02] 第一条消息", md.read_text(encoding="utf-8"))
            names.write_text(
                json.dumps({"chat_type": "group", "unresolved_speaker_groups": {groups[0]["id"]: "王五"}}),
                encoding="utf-8",
            )
            build_markdown("sample.mp4", rows, lines_map, frames, md, raw, names_file=names)
            text = md.read_text(encoding="utf-8")
            self.assertIn("**王五** [00:00:02] 第一条消息", text)
            self.assertIn("**王五** [00:00:03] 第二条消息", text)
            self.assertIn("**未识别成员2⚠** [00:00:04] 第三条消息", text)

    def test_group_mapping_parser_targets_the_stable_group_id(self):
        groups = [{"id": "avatar_001", "number": 1}, {"id": "avatar_002", "number": 2}]
        self.assertEqual(
            name_editor._parse_group_mappings("未识别成员1=张三；2=李四", groups),
            {"avatar_001": "张三", "avatar_002": "李四"},
        )


class PageClassifierTests(unittest.TestCase):
    def test_pdf_builder_writes_multiple_pages_without_collecting_page_images(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            rows = []
            for index, color in enumerate(("red", "green", "blue"), 1):
                name = f"{index:04d}.png"
                Image.new("RGB", (540, 960), color).save(work / name)
                rows.append({"文件名": name})
            pdf = work / "print.pdf"
            _build_pdf(rows, work, pdf, "a4")
            data = pdf.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertIn(b"/Count 3", data)
            self.assertTrue(data.rstrip().endswith(b"%%EOF"))

    def test_group_info_transition_keeps_only_stable_context_page(self):
        rows = [
            {"文件名": "0001.png", "视频时间": "00:00:01.000", "累计位移(像素)": "0", "清晰度": "20", "_frames_dir": "/tmp/frames"},
            {"文件名": "0002.png", "视频时间": "00:00:01.300", "累计位移(像素)": "0", "清晰度": "50", "_frames_dir": "/tmp/frames"},
            {"文件名": "0003.png", "视频时间": "00:00:01.500", "累计位移(像素)": "0", "清晰度": "30", "_frames_dir": "/tmp/frames"},
            {"文件名": "0004.png", "视频时间": "00:00:03.000", "累计位移(像素)": "150", "清晰度": "40", "_frames_dir": "/tmp/frames"},
        ]
        lines = {
            "/tmp/frames/0001.png": [Line("群公告", 0.1, 0.2, 0.2, 0.04), Line("群管理", 0.1, 0.3, 0.2, 0.04)],
            "/tmp/frames/0002.png": [Line("群公告", 0.1, 0.2, 0.2, 0.04), Line("群管理", 0.1, 0.3, 0.2, 0.04)],
            "/tmp/frames/0003.png": [Line("正常页面", 0.1, 0.2, 0.2, 0.04)],
            "/tmp/frames/0004.png": [Line("新的聊天内容", 0.1, 0.2, 0.2, 0.04)],
        }
        kept, events, summary, all_rows = classify_rows(rows, lines)
        self.assertEqual([row["文件名"] for row in kept], ["0002.png", "0004.png"])
        self.assertEqual(kept[0]["页面类型"], "group_info")
        self.assertEqual(kept[0]["强制保留"], "是")
        self.assertEqual(summary["transitions"], 2)
        self.assertEqual(events[0]["file"], "0002.png")
        self.assertTrue(any(row["页面类型"] == "transition" for row in all_rows))

    def test_full_screen_terminal_without_menu_ocr_is_kept_after_stabilizing(self):
        rows = [
            {"文件名": "0001.png", "视频时间": "00:00:01.000", "累计位移(像素)": "0", "清晰度": "20", "_frames_dir": "/tmp/frames"},
            {"文件名": "0002.png", "视频时间": "00:00:01.200", "累计位移(像素)": "0", "清晰度": "40", "_frames_dir": "/tmp/frames"},
            {"文件名": "0003.png", "视频时间": "00:00:01.650", "累计位移(像素)": "0", "清晰度": "80", "_frames_dir": "/tmp/frames"},
        ]
        lines = {
            "/tmp/frames/0001.png": [Line("群公告", 0.1, 0.2, 0.2, 0.04), Line("群管理", 0.1, 0.3, 0.2, 0.04)],
            "/tmp/frames/0002.png": [Line("群公告", 0.1, 0.2, 0.2, 0.04), Line("群管理", 0.1, 0.3, 0.2, 0.04)],
            "/tmp/frames/0003.png": [Line("普通聊天文字", 0.1, 0.2, 0.2, 0.04)],
        }
        kept, events, _summary, _all_rows = classify_rows(rows, lines)
        self.assertEqual([row["文件名"] for row in kept], ["0003.png"])
        self.assertEqual(kept[0]["页面类型"], "group_info")
        self.assertEqual(events[0]["file"], "0003.png")

    def test_mandatory_context_page_survives_print_overlap_pruning(self):
        with tempfile.TemporaryDirectory() as td:
            frames = Path(td)
            for name in ("0001.png", "0002.png"):
                Image.new("RGB", (540, 960), "white").save(frames / name)
            rows = [
                {"文件名": "0001.png", "累计位移(像素)": "0", "强制保留": "否"},
                {"文件名": "0002.png", "累计位移(像素)": "0", "强制保留": "是"},
            ]
            selected = select_print_screens(frames, rows, 0.8, 0.65)
            self.assertIn("0002.png", {row["文件名"] for row in selected})


class OutputSafetyTests(unittest.TestCase):
    def test_preview_is_seeded_and_temporary_stage_is_cleaned(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            previous = root / "成果"
            preview = previous / "00_预检"
            preview.mkdir(parents=True)
            (preview / "预检报告.md").write_text("预检", encoding="utf-8")
            (previous / "视频信息.json").write_text("{}", encoding="utf-8")
            stage = root / ".成果.处理中-test"
            stage.mkdir()
            v2s._seed_user_inputs(previous, stage)
            self.assertTrue((stage / ".内部数据" / "00_预检" / "预检报告.md").exists())
            self.assertTrue((stage / ".内部数据" / "视频信息.json").exists())
            v2s._ACTIVE_STAGE_ROOT = stage
            v2s._cleanup_temporary_paths()
            self.assertFalse(stage.exists())

    def test_final_screens_are_renumbered_without_gaps(self):
        with tempfile.TemporaryDirectory() as td:
            frames = Path(td)
            for name in ("0001.png", "0007.png", "0016.png"):
                Image.new("RGB", (64, 64), "white").save(frames / name)
            rows = [
                {"序号": "1", "文件名": "0001.png"},
                {"序号": "7", "文件名": "0007.png"},
                {"序号": "16", "文件名": "0016.png"},
            ]
            lines_map = {str(frames / row["文件名"]): [] for row in rows}
            rows, remapped, mapping = v2s.renumber_final_frames(frames, rows, lines_map)
            self.assertEqual([row["文件名"] for row in rows], ["0001.png", "0002.png", "0003.png"])
            self.assertEqual(mapping["0016.png"], "0003.png")
            self.assertEqual(sorted(path.name for path in frames.glob("*.png")), ["0001.png", "0002.png", "0003.png"])
            self.assertEqual(set(remapped), {str(frames / "0001.png"), str(frames / "0002.png"), str(frames / "0003.png")})


class NameEditorTests(unittest.TestCase):
    def test_group_mapping_input_is_parsed_without_guessing_names(self):
        self.assertEqual(
            name_editor._parse_mappings("Lily=张三；小王=王五；无效项；=空值"),
            {"Lily": "张三", "小王": "王五"},
        )

    def test_message_index_mapping_targets_only_listed_pending_message(self):
        pending = [{"id": "a"}, {"id": "b"}]
        self.assertEqual(name_editor._parse_index_mappings("1=张三；3=无效；2=李四", pending), {"a": "张三", "b": "李四"})


class VideoSeekTests(unittest.TestCase):
    def test_start_really_seeks_source_video(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "seek.mp4"
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
            self.assertTrue(writer.isOpened())
            for idx in range(30):
                writer.write(np.full((64, 64, 3), idx * 7, dtype=np.uint8))
            writer.release()
            cap, first, _used, frame_index = open_video(path, 1.0)
            try:
                self.assertIsNotNone(first)
                self.assertGreaterEqual(frame_index, 8)
                self.assertGreater(float(np.mean(first)), 45.0)
            finally:
                cap.release()


class VisualJointLocationTests(unittest.TestCase):
    def test_visible_animated_voice_bubble_has_activity(self):
        with tempfile.TemporaryDirectory() as td:
            work = Path(td)
            frames = work / "frames"
            frames.mkdir()
            video = work / "animated.mp4"
            width, height, fps = 540, 960, 10
            writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (width, height))
            self.assertTrue(writer.isOpened())
            first_frame = None
            for idx in range(20):
                frame = np.full((height, width, 3), 245, dtype=np.uint8)
                cv2.rectangle(frame, (35, 350), (280, 430), (255, 255, 255), -1)
                cv2.putText(frame, '4"', (80, 402), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 0), 2, cv2.LINE_AA)
                cv2.circle(frame, (205, 390), 5 + idx % 4 * 4, (20, 20, 20), 2)
                writer.write(frame)
                if idx == 0:
                    first_frame = frame.copy()
            writer.release()
            cv2.imwrite(str(frames / "anchor.png"), first_frame)
            candidate = {
                "voice_id": "voice_0001",
                "side": "left",
                "x0": 75,
                "x1": 125,
                "y_screen": 392,
                "occurrences": [{"img": "anchor.png", "y_screen": 392, "time_seconds": 0.0}],
            }
            score = visual_playback_score(video, candidate, {"start": 0.0, "end": 1.9}, frames)
            self.assertGreaterEqual(score["visibility"], 0.58)
            self.assertGreaterEqual(score["activity"], 0.18)


if __name__ == "__main__":
    unittest.main()
