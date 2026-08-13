from __future__ import annotations

import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw, PngImagePlugin


SKILL_ROOT = Path(__file__).resolve().parents[1] / "skills" / "legal-network-verification"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


network_workpaper = load_module(
    "network_workpaper_v20_security",
    SKILL_ROOT / "scripts" / "network_workpaper.py",
)
watermark_capture = load_module(
    "watermark_capture_v20_security",
    SKILL_ROOT / "scripts" / "watermark_capture.py",
)
fixtures = load_module(
    "network_workpaper_v20_fixtures",
    Path(__file__).with_name("test_v20_schema_execution.py"),
)


def write_capture(
    root: Path,
    run: dict,
    *,
    queried_at: str = "2026-08-12T10:00:04+08:00",
) -> Path:
    query = run["queries"][0]
    output = root / query["screenshot_path"]
    source = root / "anonymous-page-source.png"
    image = Image.new("RGB", (480, 280), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, 456, 256), outline="black", width=4)
    draw.text((60, 120), "QUERY CONDITIONS / 0 RESULTS", fill="black")
    image.save(source)
    watermark_capture.add_watermark(
        source,
        output,
        evidence_id=query["evidence_id"],
        subject=run["subjects"][0]["name"],
        source=query["site_name"],
        queried_at=watermark_capture.parse_timestamp(queried_at),
        overwrite=True,
    )
    return output


def base_run(root: Path) -> dict:
    run = fixtures.base_run(root)
    write_capture(root, run)
    return run


def rewrite_png_metadata(path: Path, **overrides: str) -> None:
    with Image.open(path) as opened:
        pixels = opened.convert("RGB")
        values = dict(opened.info)
    values.update(overrides)
    metadata = PngImagePlugin.PngInfo()
    for key, value in values.items():
        if isinstance(value, str):
            metadata.add_text(key, value)
    pixels.save(path, format="PNG", pnginfo=metadata)


def recovered_run(root: Path) -> dict:
    run = base_run(root)
    execution = run["query_executions"][0]
    execution["attempts"] = [
        {"sequence": 1, "at": "2026-08-12T10:00:00+08:00", "event": "query_submitted"},
        {"sequence": 2, "at": "2026-08-12T10:00:01+08:00", "event": "session_expired"},
        {"sequence": 3, "at": "2026-08-12T10:00:02+08:00", "event": "user_resume_confirmed"},
        {
            "sequence": 4,
            "at": "2026-08-12T10:00:02+08:00",
            "event": "resume_safety_check_passed",
        },
        {
            "sequence": 5,
            "at": "2026-08-12T10:00:03+08:00",
            "event": "explicit_resubmit_confirmed",
        },
        {"sequence": 6, "at": "2026-08-12T10:00:04+08:00", "event": "query_submitted"},
        {"sequence": 7, "at": "2026-08-12T10:00:05+08:00", "event": "result_region_loaded"},
        {"sequence": 8, "at": "2026-08-12T10:00:06+08:00", "event": "screenshot"},
    ]
    execution["completion_evidence"]["observed_at"] = "2026-08-12T10:00:06+08:00"
    run["queries"][0]["query_time"] = "2026-08-12T10:00:06+08:00"
    write_capture(root, run, queried_at="2026-08-12T10:00:06+08:00")
    return run


def sensitive_no_capture(run: dict) -> None:
    query = run["queries"][0]
    query["screenshot_path"] = ""
    query["capture_kind"] = network_workpaper.SENSITIVE_NO_CAPTURE_KIND
    query["follow_up"] = network_workpaper.SENSITIVE_NO_CAPTURE_EXPLANATION
    run["query_executions"][0]["completion_evidence"][
        "capture_review"
    ] = "sensitive_no_capture"


class V20SecurityGateTests(unittest.TestCase):
    def test_login_resume_requires_dedicated_safety_check_before_query(self) -> None:
        attempts = [
            {"sequence": 1, "at": "2026-08-12T10:00:00+08:00", "event": "login_required"},
            {"sequence": 2, "at": "2026-08-12T10:00:01+08:00", "event": "user_resume_confirmed"},
            {"sequence": 3, "at": "2026-08-12T10:00:02+08:00", "event": "query_submitted"},
        ]
        with self.assertRaises(network_workpaper.ValidationError):
            network_workpaper.validate_attempt_trace(
                attempts,
                label="attempts",
                browser_selection="user_explicit",
                execution_state="running",
                submit_state="submitted",
            )

    def test_post_submit_technical_error_requires_unknown_submit_state(self) -> None:
        for event in ("network_error", "tool_error"):
            attempts = [
                {"sequence": 1, "at": "2026-08-12T10:00:00+08:00", "event": "query_submitted"},
                {"sequence": 2, "at": "2026-08-12T10:00:01+08:00", "event": event},
            ]
            with self.subTest(event=event):
                trace = network_workpaper.validate_attempt_trace(
                    attempts,
                    label="attempts",
                    browser_selection="user_explicit",
                    execution_state="failed",
                    submit_state="unknown",
                )
                self.assertTrue(trace["query_submitted"])

    def test_post_submit_technical_error_cannot_resubmit_without_confirmation(self) -> None:
        for event in ("network_error", "tool_error"):
            unconfirmed = [
                {"sequence": 1, "at": "2026-08-12T10:00:00+08:00", "event": "query_submitted"},
                {"sequence": 2, "at": "2026-08-12T10:00:01+08:00", "event": event},
                {"sequence": 3, "at": "2026-08-12T10:00:02+08:00", "event": "query_submitted"},
            ]
            with self.subTest(event=event, confirmation="missing"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_attempt_trace(
                        unconfirmed,
                        label="attempts",
                        browser_selection="user_explicit",
                        execution_state="failed",
                        submit_state="submitted",
                    )

            confirmed = copy.deepcopy(unconfirmed)
            confirmed.insert(
                2,
                {
                    "sequence": 3,
                    "at": "2026-08-12T10:00:02+08:00",
                    "event": "explicit_resubmit_confirmed",
                },
            )
            confirmed[-1]["sequence"] = 4
            confirmed[-1]["at"] = "2026-08-12T10:00:03+08:00"
            with self.subTest(event=event, confirmation="explicit"):
                network_workpaper.validate_attempt_trace(
                    confirmed,
                    label="attempts",
                    browser_selection="user_explicit",
                    execution_state="failed",
                    submit_state="submitted",
                )

    def test_browser_fallback_after_first_submission_is_rejected(self) -> None:
        attempts = [
            {"sequence": 1, "at": "2026-08-12T10:00:00+08:00", "event": "query_submitted"},
            {"sequence": 2, "at": "2026-08-12T10:00:01+08:00", "event": "browser_fallback"},
        ]
        with self.assertRaises(network_workpaper.ValidationError):
            network_workpaper.validate_attempt_trace(
                attempts,
                label="attempts",
                browser_selection="automatic_fallback",
                execution_state="running",
                submit_state="submitted",
            )

    def test_resubmission_rejects_stale_capture_query_and_observation_times(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            baseline = recovered_run(root)
            network_workpaper.validate_run(baseline, workpaper_root=root, formal_mode="final")

            stale_capture = copy.deepcopy(baseline)
            write_capture(root, stale_capture, queried_at="2026-08-12T10:00:01+08:00")
            with self.subTest(stale="screenshot"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        stale_capture, workpaper_root=root, formal_mode="final"
                    )

            write_capture(root, baseline, queried_at="2026-08-12T10:00:06+08:00")
            stale_query_time = copy.deepcopy(baseline)
            stale_query_time["queries"][0]["query_time"] = "2026-08-12T10:00:01+08:00"
            with self.subTest(stale="query_time"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        stale_query_time, workpaper_root=root, formal_mode="final"
                    )

            stale_observation = copy.deepcopy(baseline)
            stale_observation["query_executions"][0]["completion_evidence"][
                "observed_at"
            ] = "2026-08-12T10:00:01+08:00"
            with self.subTest(stale="completion_observed_at"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        stale_observation, workpaper_root=root, formal_mode="final"
                    )

    def test_watermark_rejects_single_colour_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            source = root / "blank.png"
            output = root / "watermarked.png"
            Image.new("RGB", (480, 280), "white").save(source)
            with self.assertRaises(ValueError):
                watermark_capture.add_watermark(
                    source,
                    output,
                    evidence_id="NQ-ANON-001",
                    subject="匿名主体",
                    source="示例官方平台",
                    queried_at=watermark_capture.parse_timestamp(
                        "2026-08-12T10:00:00+08:00"
                    ),
                )
            self.assertFalse(output.exists())

    def test_validator_requires_matching_png_evidence_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = fixtures.base_run(root)
            path = root / run["queries"][0]["screenshot_path"]
            with Image.open(path) as opened:
                opened.convert("RGB").save(path)
            with self.subTest(metadata="missing"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(run, workpaper_root=root, formal_mode="final")

            run = base_run(root)
            path = root / run["queries"][0]["screenshot_path"]
            mismatches = {
                "EvidenceId": "NQ-WRONG",
                "QueriedAt": "2026-08-12T09:00:00+08:00",
                "Subject": "其他匿名主体",
                "Source": "其他示例平台",
                "CaptureKind": "full_app_screenshot",
            }
            for key, value in mismatches.items():
                write_capture(root, run)
                rewrite_png_metadata(path, **{key: value})
                with self.subTest(metadata=key):
                    with self.assertRaises(network_workpaper.ValidationError):
                        network_workpaper.validate_run(
                            run, workpaper_root=root, formal_mode="final"
                        )

    def test_sensitive_no_capture_only_for_person_user_manual_full_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            company = base_run(root)
            sensitive_no_capture(company)
            with self.subTest(subject="company", mode="exact_subject_name"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(company, workpaper_root=root)

            person = base_run(root)
            person["subjects"][0]["type"] = "natural_person"
            person["subjects"][0]["name"] = "示例自然人"
            person["query_scope"]["items"][0]["query_term_mode"] = "exact_subject_name"
            person["queries"][0]["query_term_mode"] = "exact_subject_name"
            person["queries"][0]["query_terms"] = network_workpaper.canonical_query_terms(
                person["subjects"][0], "exact_subject_name"
            )
            sensitive_no_capture(person)
            with self.subTest(subject="natural_person", mode="exact_subject_name"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(person, workpaper_root=root)

            blind = copy.deepcopy(person)
            blind["query_scope"]["items"][0]["query_term_mode"] = "user_manual_full_id"
            blind["queries"][0]["query_term_mode"] = "user_manual_full_id"
            blind["queries"][0]["query_terms"] = network_workpaper.canonical_query_terms(
                blind["subjects"][0], "user_manual_full_id"
            )
            blind["query_executions"][0]["attempts"][-1] = {
                "sequence": 5,
                "at": "2026-08-12T10:00:04+08:00",
                "event": "sensitive_result_review_confirmed",
            }
            network_workpaper.validate_run(blind, workpaper_root=root)

    def test_schema12_final_requires_workpaper_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            run = base_run(Path(temp_text))
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(run, formal_mode="final")

    def test_completion_preview_not_ready_without_final_evidence_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = base_run(root)
            with self.subTest(missing="workpaper_root"):
                self.assertFalse(network_workpaper.completion_preview(run)["final_ready"])

            missing_formal = copy.deepcopy(run)
            del missing_formal["run"]["formal_record"]["query_location"]
            with self.subTest(missing="formal_record_field"):
                self.assertFalse(
                    network_workpaper.completion_preview(
                        missing_formal, workpaper_root=root
                    )["final_ready"]
                )

            missing_screenshot = copy.deepcopy(run)
            (root / missing_screenshot["queries"][0]["screenshot_path"]).unlink()
            with self.subTest(missing="screenshot"):
                self.assertFalse(
                    network_workpaper.completion_preview(
                        missing_screenshot, workpaper_root=root
                    )["final_ready"]
                )

    def test_schema12_rejects_unknown_nested_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            targets = (
                ("run", ("run",)),
                ("project", ("run", "project")),
                ("formal_record", ("run", "formal_record")),
                ("subject", ("subjects", 0)),
                ("scope_condition", ("query_scope", "items", 0, "conditions", 0)),
                ("completion_evidence", ("query_executions", 0, "completion_evidence")),
            )
            for label, path in targets:
                run = base_run(root)
                target = run
                for part in path:
                    target = target[part]
                target["unexpected_field"] = "anonymous"
                with self.subTest(object=label):
                    with self.assertRaises(network_workpaper.ValidationError):
                        network_workpaper.validate_run(run, workpaper_root=root)

    def test_schema12_rejects_local_paths_and_sensitive_url_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            local_path = base_run(root)
            local_path["queries"][0]["follow_up"] = "/Users/example/private/source.pdf"
            with self.subTest(value="local_absolute_path"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(local_path, workpaper_root=root)

            embedded_path = base_run(root)
            embedded_path["queries"][0]["follow_up"] = (
                "材料另存于 /Users/example/private/source.pdf，请人工核对。"
            )
            with self.subTest(value="embedded_local_absolute_path"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(embedded_path, workpaper_root=root)

            adjacent_path = base_run(root)
            adjacent_path["queries"][0]["follow_up"] = (
                "材料另存于/Users/example/private/source.pdf，请人工核对。"
            )
            with self.subTest(value="adjacent_local_absolute_path"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(adjacent_path, workpaper_root=root)

            for parameter in ("token", "session", "session_id"):
                run = base_run(root)
                run["queries"][0]["url"] = (
                    f"https://sub.example.gov.cn/search?{parameter}=opaque-value"
                )
                with self.subTest(value=f"url_{parameter}"):
                    with self.assertRaises(network_workpaper.ValidationError):
                        network_workpaper.validate_run(run, workpaper_root=root)

            for path in ("/%73ession/opaque-value", "/%2573ession/opaque-value", "/session_id/opaque"):
                run = base_run(root)
                run["queries"][0]["url"] = "https://sub.example.gov.cn" + path
                with self.subTest(value=f"encoded_path_{path}"):
                    with self.assertRaises(network_workpaper.ValidationError):
                        network_workpaper.validate_run(run, workpaper_root=root)

            for path in (
                "/search;session=opaque-value",
                "/a;session=opaque-value/b",
                "/search%3Bsession=opaque-value",
                "/search%253Bsession=opaque-value",
                "/search%3Bjsessionid=opaque-value",
                "/session=opaque-value",
                "/session_id=opaque-value",
                "/x/session%3Dopaque-value",
                "/x/%73ession%3Dopaque-value",
                "/x/%2573ession%253Dopaque-value",
                "/session%3Aopaque-value",
                "/x%5Csession%3Dopaque-value",
            ):
                semicolon = base_run(root)
                semicolon["queries"][0]["url"] = (
                    "https://sub.example.gov.cn" + path
                )
                with self.subTest(value=f"semicolon_path_parameter_{path}"):
                    with self.assertRaises(network_workpaper.ValidationError):
                        network_workpaper.validate_run(semicolon, workpaper_root=root)

            for path in (
                "/access_token/opaque-value",
                "/refresh-token/opaque-value",
                "/jsessionid/opaque-value",
                "/authorization/opaque-value",
                "/code/opaque-value",
                "/api-key/opaque-value",
                "/password/opaque-value",
            ):
                sensitive_path = base_run(root)
                sensitive_path["queries"][0]["url"] = (
                    "https://sub.example.gov.cn" + path
                )
                with self.subTest(value=f"sensitive_path_segment_{path}"):
                    with self.assertRaises(network_workpaper.ValidationError):
                        network_workpaper.validate_run(
                            sensitive_path, workpaper_root=root
                        )

    def test_scope_confirmation_must_precede_attempts_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = base_run(root)
            run["query_scope"]["confirmed_at"] = "2026-08-12T10:00:10+08:00"
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(run, workpaper_root=root, formal_mode="final")

    def test_terminal_block_reason_must_match_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            blocked_technical = base_run(root)
            fixtures.set_blocked(blocked_technical, "network_error", "network_error")
            with self.subTest(state="blocked", reason="network_error"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        blocked_technical, workpaper_root=root, formal_mode="final"
                    )

            failed_access = base_run(root)
            fixtures.set_blocked(failed_access, "rate_limited", "rate_limited")
            failed_access["query_executions"][0]["execution_state"] = "failed"
            failed_access["queries"][0]["status"] = "failed"
            failed_access["queries"][0]["result_summary"] = (
                network_workpaper.CANONICAL_FAILED_RESULT
            )
            with self.subTest(state="failed", reason="rate_limited"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        failed_access, workpaper_root=root, formal_mode="final"
                    )

    def test_login_cannot_be_closed_as_access_limited_without_user_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = base_run(root)
            fixtures.set_blocked(run, "login_required", "login_required")
            run["query_executions"][0]["access_state"] = "login_required"
            run["query_executions"][0]["attempts"].append(
                {
                    "sequence": 3,
                    "at": "2026-08-12T10:00:02+08:00",
                    "event": "user_resume_confirmed",
                }
            )
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(run, workpaper_root=root, formal_mode="final")

    def test_restricted_result_cannot_smuggle_zero_result_wording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = base_run(root)
            fixtures.set_blocked(run, "rate_limited", "rate_limited")
            run["queries"][0]["result_summary"] = "页面明确显示零条匹配记录。"
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(run, workpaper_root=root, formal_mode="final")

            synonym = base_run(root)
            fixtures.set_blocked(synonym, "rate_limited", "rate_limited")
            synonym["queries"][0]["result_summary"] = "本次页面未检出匹配记录。"
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(
                    synonym, workpaper_root=root, formal_mode="final"
                )

            follow_up = base_run(root)
            fixtures.set_blocked(follow_up, "rate_limited", "rate_limited")
            follow_up["queries"][0]["follow_up"] = "本次页面未检出匹配记录。"
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(
                    follow_up, workpaper_root=root, formal_mode="final"
                )

    def test_new_result_load_invalidates_prior_capture_or_sensitive_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = base_run(root)
            run["query_executions"][0]["attempts"].append(
                {
                    "sequence": 6,
                    "at": "2026-08-12T10:00:04+08:00",
                    "event": "result_region_loaded",
                }
            )
            with self.subTest(kind="page_capture"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        run, workpaper_root=root, formal_mode="final"
                    )

            sensitive = base_run(root)
            sensitive["subjects"][0]["type"] = "natural_person"
            sensitive["subjects"][0]["name"] = "示例自然人"
            sensitive["query_scope"]["items"][0]["query_term_mode"] = "user_manual_full_id"
            query = sensitive["queries"][0]
            query["query_term_mode"] = "user_manual_full_id"
            query["query_terms"] = network_workpaper.canonical_query_terms(
                sensitive["subjects"][0], "user_manual_full_id"
            )
            query["screenshot_path"] = ""
            query["capture_kind"] = network_workpaper.SENSITIVE_NO_CAPTURE_KIND
            query["follow_up"] = network_workpaper.SENSITIVE_NO_CAPTURE_EXPLANATION
            execution = sensitive["query_executions"][0]
            execution["completion_evidence"]["capture_review"] = "sensitive_no_capture"
            execution["attempts"][-1] = {
                "sequence": 5,
                "at": "2026-08-12T10:00:04+08:00",
                "event": "sensitive_result_review_confirmed",
            }
            execution["attempts"].append(
                {
                    "sequence": 6,
                    "at": "2026-08-12T10:00:04+08:00",
                    "event": "result_region_loaded",
                }
            )
            with self.subTest(kind="sensitive_review"):
                with self.assertRaises(network_workpaper.ValidationError):
                    network_workpaper.validate_run(
                        sensitive, workpaper_root=root, formal_mode="final"
                    )

    def test_sensitive_no_capture_review_must_follow_observation_for_any_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_text:
            root = Path(temp_text)
            run = base_run(root)
            run["subjects"][0]["type"] = "natural_person"
            run["subjects"][0]["name"] = "示例自然人"
            run["query_scope"]["items"][0]["query_term_mode"] = "user_manual_full_id"
            query = run["queries"][0]
            query["query_term_mode"] = "user_manual_full_id"
            query["query_terms"] = network_workpaper.canonical_query_terms(
                run["subjects"][0], "user_manual_full_id"
            )
            query["status"] = "identity_match"
            query["screenshot_path"] = ""
            query["capture_kind"] = network_workpaper.SENSITIVE_NO_CAPTURE_KIND
            query["follow_up"] = network_workpaper.SENSITIVE_NO_CAPTURE_EXPLANATION
            query["query_time"] = "2026-08-12T10:30:00+08:00"
            execution = run["query_executions"][0]
            execution["completion_evidence"].update(
                {
                    "result_signal": "matching_results_displayed",
                    "capture_review": "sensitive_no_capture",
                    "observed_at": "2026-08-12T10:30:00+08:00",
                }
            )
            execution["attempts"][-1] = {
                "sequence": 5,
                "at": "2026-08-12T10:00:04+08:00",
                "event": "sensitive_result_review_confirmed",
            }
            with self.assertRaises(network_workpaper.ValidationError):
                network_workpaper.validate_run(run, workpaper_root=root, formal_mode="final")


if __name__ == "__main__":
    unittest.main()
