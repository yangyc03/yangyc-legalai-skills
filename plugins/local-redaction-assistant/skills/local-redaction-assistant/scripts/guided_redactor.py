#!/usr/bin/env python3
"""Single-file, browser-guided local redaction launcher.

This wrapper intentionally delegates copy creation to ``local_redactor.py``.
It keeps user-confirmed DOCX terms in a temporary owner-only file and never
writes them to reports. PDF and image workflows retain a separate browser
confirmation after regions are saved.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_redactor import (
    AI_SHARE_PROFILE,
    IMAGE_EXTENSIONS,
    IMAGE_REGIONS_FILENAME,
    OUTPUT_MARKER,
    PDF_REGIONS_FILENAME,
    VERSION,
    extract_docx_main_body_text,
    extract_profile_candidates,
    validate_output_path,
)
from local_region_selector import RegionSelectorError, run_region_selector
from local_term_selector import TermSelectorError, run_term_selector
from local_cleaning_selector import CleaningSelectorError, run_cleaning_selector
from docx_clean_copy import (
    add_manual_occurrences,
    create_template_copy,
    inspect_docx_features,
    public_candidate_payload,
    scan_docx_occurrences,
)
from word_native_validator import create_clean_copy_with_word


GUIDED_REPORT_FILENAME = "guided_workflow_report.json"
SYNC_DIRECTORY_MARKERS = ("onedrive", "icloud", "dropbox", "cloudstorage")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Local guided redaction for one explicit file or an owner-only DOCX manifest. "
            "Opens loopback browser pages for required lawyer confirmation."
        )
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input", help="One DOCX, PDF, PNG, JPG, or JPEG copy.")
    input_group.add_argument(
        "--input-manifest",
        help="Owner-only redaction_batch.local.json whitelist; maximum 30 DOCX files and no directory recursion.",
    )
    parser.add_argument(
        "--output",
        help="Output directory. Default: sibling _redaction_output beside the selected copy or manifest.",
    )
    parser.add_argument("--config", help="Optional local redaction configuration.")
    parser.add_argument(
        "--redaction-profile",
        choices=("ai-share", "legal-template"),
        default=AI_SHARE_PROFILE,
        help=(
            "ai-share for ordinary AI-sharing redaction; legal-template for template-extraction "
            "candidates that require local lawyer confirmation. Default: ai-share."
        ),
    )
    parser.add_argument(
        "--word-validation",
        choices=("required", "off"),
        default=None,
        help="Defaults to required for legal-template and off for ai-share.",
    )
    parser.add_argument(
        "--revision-policy",
        choices=("confirm-accept",),
        default="confirm-accept",
        help="Revisions require local confirmation before Word accepts them in a temporary safe-ID copy.",
    )
    parser.add_argument("--selector-port", type=int, default=0, help="Local loopback port. Default: random.")
    parser.add_argument(
        "--selector-timeout-seconds",
        type=int,
        default=1800,
        help="Maximum local browser confirmation time. Default: 1800.",
    )
    parser.add_argument("--render-dpi", type=int, default=200, help="PDF visual-copy render DPI. Default: 200.")
    parser.add_argument("--redaction-padding-ratio", type=float, default=0.004)
    parser.add_argument(
        "--max-file-size-mb",
        type=int,
        default=200,
        help="Reject a selected copy larger than this size before browser preview or DOCX candidate read. Default: 200.",
    )
    parser.add_argument("--no-open-browser", action="store_true", help="Do not open the local browser page.")
    parser.add_argument(
        "--allow-existing-output",
        action="store_true",
        help="Reuse an existing local-redaction-assistant output directory after explicit confirmation.",
    )
    return parser.parse_args()


def _safe_exit(message: str) -> None:
    raise SystemExit(message)


def validate_args(args: argparse.Namespace, input_path: Path, output_path: Path) -> None:
    if not input_path.is_file() or input_path.is_symlink():
        _safe_exit("guided-redact requires one regular DOCX, PDF, PNG, JPG, or JPEG copy")
    if input_path.suffix.lower() not in {".docx", ".pdf", *IMAGE_EXTENSIONS}:
        _safe_exit("guided-redact supports only DOCX, PDF, PNG, JPG, or JPEG copies")
    if args.selector_port < 0 or args.selector_port > 65535:
        _safe_exit("--selector-port must be between 0 and 65535")
    if args.selector_timeout_seconds < 30 or args.selector_timeout_seconds > 7200:
        _safe_exit("--selector-timeout-seconds must be between 30 and 7200")
    if args.render_dpi < 72 or args.render_dpi > 400:
        _safe_exit("--render-dpi must be between 72 and 400")
    if args.redaction_padding_ratio < 0 or args.redaction_padding_ratio > 0.05:
        _safe_exit("--redaction-padding-ratio must be between 0 and 0.05")
    if args.max_file_size_mb < 1 or args.max_file_size_mb > 500:
        _safe_exit("--max-file-size-mb must be between 1 and 500")
    if args.config and not Path(args.config).expanduser().is_file():
        _safe_exit("--config does not exist")
    validate_output_path(input_path, output_path, args.allow_existing_output)


def prepare_output(output_path: Path) -> None:
    for directory in (output_path / "reports", output_path / "redacted_files", output_path / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
    (output_path / OUTPUT_MARKER).write_text("local-redaction-assistant\n", encoding="utf-8")


def output_may_be_synchronized(output_path: Path) -> bool:
    return any(marker in part.lower() for part in output_path.parts for marker in SYNC_DIRECTORY_MARKERS)


def write_guided_report(
    output_path: Path,
    source_type: str,
    status: str,
    error_codes: list[str],
    selected_term_counts: dict[str, int] | None = None,
    regions_saved: int = 0,
    redaction_command_completed: bool = False,
    redacted_copy_created: bool = False,
    redaction_profile: str = AI_SHARE_PROFILE,
) -> None:
    payload: dict[str, Any] = {
        "tool": "local-redaction-assistant",
        "version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "guided-redact",
        "redaction_profile": redaction_profile,
        "source_type": source_type,
        "status": status,
        "loopback_browser_used": True,
        "docx_terms_confirmed_in_browser": source_type == "docx" and redaction_command_completed,
        "regions_saved": regions_saved,
        "redaction_command_completed": redaction_command_completed,
        "redacted_copy_created": redacted_copy_created,
        "selected_term_counts": selected_term_counts or {"persons": 0, "companies": 0, "addresses": 0},
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "coordinates_recorded_in_report": False,
        "labels_recorded_in_report": False,
        "output_may_be_synchronized": output_may_be_synchronized(output_path),
        "manual_review_required": True,
        "error_code_counts": {code: error_codes.count(code) for code in sorted(set(error_codes))},
        "notes": [
            "The guided browser page is loopback-only and token-protected.",
            "DOCX candidate values, full-name/alias associations, and manually entered terms are never written to reports.",
            "PDF/image regions are saved locally, then require a separate browser confirmation before copy creation.",
            "All output copies require manual review before sharing.",
            "When the output location may be synchronized, keep local-sensitive artifacts out of shared storage.",
        ],
    }
    (output_path / "reports").mkdir(parents=True, exist_ok=True)
    (output_path / "reports" / GUIDED_REPORT_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def write_temporary_terms(terms: dict[str, list[str]]) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    workspace = tempfile.TemporaryDirectory(prefix="local-redaction-guided-terms-")
    path = Path(workspace.name) / "redaction_terms.local.json"
    payload = {"custom_terms": terms}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return workspace, path


def run_redactor(arguments: list[str]) -> int:
    command = [sys.executable, str(Path(__file__).with_name("local_redactor.py")), *arguments]
    completed = subprocess.run(command, check=False)
    return completed.returncode


def downstream_copy_created(output_path: Path, source_type: str) -> bool:
    filename = {
        "docx": "redaction_report.json",
        "pdf": "visual_redaction_report.json",
        "image": "image_visual_redaction_report.json",
    }[source_type]
    try:
        payload = json.loads((output_path / "reports" / filename).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if source_type == "docx":
        return int(payload.get("redacted_files_created", 0)) > 0
    if source_type == "pdf":
        return int(payload.get("visual_redaction_files_created", 0)) > 0
    return int(payload.get("image_visual_redaction_files_created", 0)) > 0


def common_redactor_arguments(input_path: Path, output_path: Path, config_path: str | None) -> list[str]:
    arguments = ["--input", str(input_path), "--output", str(output_path), "--allow-existing-output"]
    if config_path:
        arguments.extend(["--config", str(Path(config_path).expanduser())])
    return arguments


def _cleanup_safe_working_files(output_path: Path, file_id: str) -> None:
    for directory in (output_path / "logs", output_path / "redacted_files"):
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            is_log_working = directory.name == "logs" and path.name.startswith(file_id)
            is_safe_lock_or_temp = path.name.startswith(f"~${file_id}") or path.name.startswith(f".{file_id}")
            if path.is_file() and (is_log_working or is_safe_lock_or_temp):
                path.unlink(missing_ok=True)


def run_legal_template_docx_flow(
    args: argparse.Namespace,
    input_path: Path,
    output_path: Path,
    file_id: str = "F000001",
) -> int:
    preflight = inspect_docx_features(input_path)
    blockers = []
    if preflight.get("macros"):
        blockers.append("docx_macros_blocked")
    if int(preflight.get("embedded_objects", 0)):
        blockers.append("docx_embedded_objects_blocked")
    if blockers:
        write_guided_report(output_path, "docx", "guided_docx_preflight_blocked", blockers, redaction_profile="legal-template")
        return 2

    logs_dir = output_path / "logs"
    working_source = logs_dir / f"{file_id}-source-copy.docx"
    cleaned_source = logs_dir / f"{file_id}-clean.docx"
    _cleanup_safe_working_files(output_path, file_id)
    shutil.copy2(input_path, working_source)
    os.chmod(working_source, 0o600)
    active_source = working_source
    try:
        if int(preflight.get("revision_nodes", 0)) or bool(preflight.get("track_revisions_enabled")) or int(preflight.get("comments", 0)):
            try:
                confirmation = run_cleaning_selector(
                    file_id,
                    preflight,
                    args.selector_port,
                    args.selector_timeout_seconds,
                    not args.no_open_browser,
                )
            except CleaningSelectorError as exc:
                write_guided_report(output_path, "docx", "guided_docx_cleaning_selector_failed", [str(exc)], redaction_profile="legal-template")
                return 2
            if confirmation.get("status") != "cleaning_confirmed":
                write_guided_report(output_path, "docx", str(confirmation.get("status", "cleaning_cancelled")), [], redaction_profile="legal-template")
                return 0
            cleaned_ok, cleaned_errors = create_clean_copy_with_word(
                working_source,
                cleaned_source,
                accept_revisions=bool(confirmation.get("accept_revisions")),
                remove_comments=bool(confirmation.get("remove_comments")),
            )
            if not cleaned_ok:
                write_guided_report(output_path, "docx", "guided_docx_word_cleaning_failed", cleaned_errors, redaction_profile="legal-template")
                return 2
            cleaned_preflight = inspect_docx_features(cleaned_source)
            if int(cleaned_preflight.get("revision_nodes", 0)) or bool(cleaned_preflight.get("track_revisions_enabled")) or int(cleaned_preflight.get("comments", 0)):
                write_guided_report(
                    output_path,
                    "docx",
                    "guided_docx_cleaning_audit_failed",
                    ["docx_revision_or_comment_nodes_remaining"],
                    redaction_profile="legal-template",
                )
                return 2
            active_source = cleaned_source

        occurrences, tables, _clean_preflight = scan_docx_occurrences(active_source)
        candidate_payload = public_candidate_payload(occurrences)
        try:
            selection = run_term_selector(
                file_id,
                candidate_payload,
                args.selector_port,
                args.selector_timeout_seconds,
                not args.no_open_browser,
                redaction_profile="legal-template",
                table_candidates=[table.public() for table in tables],
            )
        except TermSelectorError as exc:
            write_guided_report(output_path, "docx", "guided_docx_selector_failed", [str(exc)], redaction_profile="legal-template")
            return 2
        if selection.get("status") != "term_selector_terms_confirmed":
            write_guided_report(output_path, "docx", str(selection.get("status", "term_selector_cancelled")), [], redaction_profile="legal-template")
            return 0
        try:
            occurrences, addition_ids = add_manual_occurrences(
                active_source,
                occurrences,
                dict(selection.get("manual_additions", {})),
            )
        except ValueError as exc:
            write_guided_report(output_path, "docx", "guided_docx_manual_addition_failed", [str(exc)], redaction_profile="legal-template")
            return 2
        selected_ids = list(dict.fromkeys(list(selection.get("selected_occurrence_ids", [])) + addition_ids))
        final_path = output_path / "redacted_files" / f"{file_id}_脱敏版.docx"
        result = create_template_copy(
            active_source,
            final_path,
            occurrences,
            tables,
            selected_ids,
            dict(selection.get("table_actions", {})),
            word_validation=getattr(args, "word_validation", None) or "required",
        )
        success = result.get("status") == "redaction_succeeded"
        write_guided_report(
            output_path,
            "docx",
            "guided_docx_redaction_completed" if success else "guided_docx_redaction_failed",
            list(result.get("error_codes", [])),
            dict(selection.get("selected_term_counts", {})),
            redaction_command_completed=success,
            redacted_copy_created=success,
            redaction_profile="legal-template",
        )
        return 0 if success else 2
    finally:
        working_source.unlink(missing_ok=True)
        cleaned_source.unlink(missing_ok=True)
        for lock in logs_dir.glob(f"~${file_id}*"):
            lock.unlink(missing_ok=True)


def run_docx_flow(args: argparse.Namespace, input_path: Path, output_path: Path, file_id: str = "F000001") -> int:
    redaction_profile = getattr(args, "redaction_profile", AI_SHARE_PROFILE)
    if redaction_profile == "legal-template":
        return run_legal_template_docx_flow(args, input_path, output_path, file_id)
    try:
        text, _reasons, read_error = extract_docx_main_body_text(input_path)
    except Exception:
        text, read_error = "", True
    if read_error:
        write_guided_report(
            output_path,
            "docx",
            "guided_docx_candidate_read_failed",
            ["guided_docx_candidate_read_failed"],
            redaction_profile=redaction_profile,
        )
        return 2

    profile_result = extract_profile_candidates(text, redaction_profile)
    candidates = profile_result["candidate_terms"]
    try:
        selection = run_term_selector(
            file_id,
            candidates,
            args.selector_port,
            args.selector_timeout_seconds,
            not args.no_open_browser,
            candidate_associations=profile_result["candidate_associations"],
            redaction_profile=redaction_profile,
        )
    except TermSelectorError as exc:
        write_guided_report(
            output_path,
            "docx",
            "guided_docx_selector_failed",
            [str(exc) or "guided_docx_selector_failed"],
            redaction_profile=redaction_profile,
        )
        return 2

    status = str(selection.get("status", "guided_docx_selector_failed"))
    selected_counts = dict(selection.get("selected_term_counts", {}))
    if status != "term_selector_terms_confirmed":
        write_guided_report(
            output_path,
            "docx",
            f"guided_docx_{status}",
            [],
            selected_counts,
            redaction_profile=redaction_profile,
        )
        return 0

    workspace, terms_path = write_temporary_terms(dict(selection.get("terms", {})))
    try:
        command = common_redactor_arguments(input_path, output_path, args.config)
        command.extend(
            [
                "--mode",
                "redact",
                "--redaction-profile",
                redaction_profile,
                "--terms-file",
                str(terms_path),
            ]
        )
        word_validation = getattr(args, "word_validation", None) or (
            "required" if redaction_profile == "legal-template" else "off"
        )
        command.extend(["--word-validation", word_validation])
        return_code = run_redactor(command)
    finally:
        workspace.cleanup()
    copy_created = return_code == 0 and downstream_copy_created(output_path, "docx")
    write_guided_report(
        output_path,
        "docx",
        (
            "guided_docx_redaction_completed"
            if copy_created
            else "guided_docx_redaction_completed_no_copy"
            if return_code == 0
            else "guided_docx_redaction_failed"
        ),
        [] if return_code == 0 else ["guided_docx_redaction_command_failed"],
        selected_counts,
        redaction_command_completed=return_code == 0,
        redacted_copy_created=copy_created,
        redaction_profile=redaction_profile,
    )
    return return_code


def run_visual_flow(args: argparse.Namespace, input_path: Path, output_path: Path) -> int:
    is_pdf = input_path.suffix.lower() == ".pdf"
    source_type = "pdf" if is_pdf else "image"
    regions_path = output_path / (PDF_REGIONS_FILENAME if is_pdf else IMAGE_REGIONS_FILENAME)
    try:
        selection = run_region_selector(
            input_path,
            "F000001",
            regions_path,
            args.render_dpi if is_pdf else 120,
            args.selector_port,
            args.selector_timeout_seconds,
            not args.no_open_browser,
            args.allow_existing_output,
            generation_confirmation_required=True,
        )
    except RegionSelectorError as exc:
        write_guided_report(output_path, source_type, "guided_visual_selector_failed", [str(exc) or "guided_visual_selector_failed"])
        return 2

    status = str(selection.get("status", "guided_visual_selector_failed"))
    regions_saved = int(selection.get("regions_saved", 0))
    if status != "region_selector_generation_confirmed":
        write_guided_report(output_path, source_type, f"guided_visual_{status}", [], regions_saved=regions_saved)
        return 0

    command = common_redactor_arguments(input_path, output_path, args.config)
    if is_pdf:
        command.extend(
            [
                "--mode",
                "visual-redact-pdf",
                "--pdf-regions",
                str(regions_path),
                "--render-dpi",
                str(args.render_dpi),
                "--redaction-padding-ratio",
                str(args.redaction_padding_ratio),
            ]
        )
    else:
        command.extend(
            [
                "--mode",
                "visual-redact-image",
                "--image-regions",
                str(regions_path),
                "--redaction-padding-ratio",
                str(args.redaction_padding_ratio),
            ]
        )
    return_code = run_redactor(command)
    copy_created = return_code == 0 and downstream_copy_created(output_path, source_type)
    write_guided_report(
        output_path,
        source_type,
        (
            "guided_visual_redaction_completed"
            if copy_created
            else "guided_visual_redaction_completed_no_copy"
            if return_code == 0
            else "guided_visual_redaction_failed"
        ),
        [] if return_code == 0 else ["guided_visual_redaction_command_failed"],
        regions_saved=regions_saved,
        redaction_command_completed=return_code == 0,
        redacted_copy_created=copy_created,
    )
    return return_code


def main() -> int:
    args = parse_args()
    if getattr(args, "input_manifest", None):
        manifest_path = Path(args.input_manifest).expanduser()
        output_path = Path(args.output).expanduser() if args.output else manifest_path.parent / "_redaction_output"
        if args.redaction_profile != "legal-template":
            _safe_exit("batch mode currently requires --redaction-profile legal-template")
        if args.word_validation not in {None, "required"}:
            _safe_exit("batch legal-template mode requires --word-validation required")
        if output_path.exists() and any(output_path.iterdir()):
            marker = output_path / OUTPUT_MARKER
            if not args.allow_existing_output or not marker.is_file():
                _safe_exit("batch output exists; use a marked output directory and explicit --allow-existing-output")
        from batch_redactor import run_batch
        from review_book import ReviewBookError, ensure_review_book_dependencies
        try:
            ensure_review_book_dependencies()
        except ReviewBookError as exc:
            _safe_exit(str(exc))
        return run_batch(args, output_path, run_docx_flow, prepare_output)
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser() if args.output else input_path.parent / "_redaction_output"
    validate_args(args, input_path, output_path)
    prepare_output(output_path)
    if output_may_be_synchronized(output_path):
        print("guided workflow warning: output may be synchronized; keep local-sensitive artifacts local and do not share them.")
    if input_path.stat().st_size > args.max_file_size_mb * 1024 * 1024:
        write_guided_report(
            output_path,
            input_path.suffix.lower().lstrip(".") or "unknown",
            "guided_input_too_large",
            ["guided_input_too_large"],
        )
        print("guided workflow stopped: status=guided_input_too_large, raw_values_recorded=false")
        return 2

    suffix = input_path.suffix.lower()
    if suffix == ".docx":
        return run_docx_flow(args, input_path, output_path)
    return run_visual_flow(args, input_path, output_path)


if __name__ == "__main__":
    raise SystemExit(main())
