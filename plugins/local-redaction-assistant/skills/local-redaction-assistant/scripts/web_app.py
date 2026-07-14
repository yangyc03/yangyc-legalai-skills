#!/usr/bin/env python3
"""Local-only browser application for the redaction assistant.

The web UI is deliberately a thin orchestration layer.  It stores the
selected copy in a private temporary workspace, keeps candidate values and
regions in process memory or owner-only temporary files, and delegates the
actual copy creation to the existing deterministic redaction tools.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from docx_clean_copy import (
    add_manual_occurrences,
    create_template_copy,
    inspect_docx_features,
    public_candidate_payload,
    scan_docx_occurrences,
)
from docx_redaction_profile import (
    TERM_CATEGORIES,
    TERM_CATEGORY_LABELS,
    normalise_redaction_dictionary,
)
from local_redactor import (
    AI_SHARE_PROFILE,
    IMAGE_EXTENSIONS,
    OUTPUT_MARKER,
    VERSION,
)
from local_region_selector import RegionSelectorError, SelectorState, _inspect_source
from local_term_selector import (
    TermSelectorError,
    TermSelectorState,
    _normalise_associations,
    _normalise_candidate,
    _normalise_tables,
)


MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_JSON_BYTES = 512 * 1024
SUPPORTED_UPLOAD_SUFFIXES = {".docx", ".pdf", ".png", ".jpg", ".jpeg"}
WEB_REPORT_FILENAME = "web_workflow_report.json"
WEB_UI_ROOT = Path(__file__).resolve().parents[1] / "assets" / "web-ui"


class WebAppError(RuntimeError):
    """Safe, user-actionable web application error."""


class LocalWebServer(ThreadingHTTPServer):
    """HTTP server bound only to loopback without reverse DNS lookup."""

    allow_reuse_address = True

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def _safe_json_bytes(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_safe_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _safe_chmod(temporary)
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise WebAppError("web_local_file_write_failed") from exc


def _normalise_upload_filename(filename: str | None) -> tuple[str, str]:
    if not filename:
        raise WebAppError("web_upload_filename_missing")
    basename = Path(filename.replace("\\", "/")).name
    suffix = Path(basename).suffix.lower()
    if suffix not in SUPPORTED_UPLOAD_SUFFIXES:
        raise WebAppError("web_upload_extension_unsupported")
    return basename, suffix


def _error_codes_from_report(path: Path) -> list[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    counts = payload.get("error_code_counts", {})
    if not isinstance(counts, dict):
        return []
    return sorted(str(key) for key in counts)


def _find_safe_output(output_path: Path, suffix: str, preferred: str) -> Path | None:
    candidate = output_path / "redacted_files" / preferred
    if candidate.is_file() and candidate.suffix.lower() == suffix:
        return candidate
    matches = sorted(
        path for path in (output_path / "redacted_files").glob(f"F000001_*.{suffix.lstrip('.')}") if path.is_file()
    )
    return matches[0] if len(matches) == 1 else None


def _safe_source_type(suffix: str) -> str:
    if suffix == ".docx":
        return "docx"
    if suffix == ".pdf":
        return "pdf"
    return "image"


@dataclass
class WebSession:
    workspace: Path
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    file_id: str = "F000001"
    source_path: Path | None = None
    active_source_path: Path | None = None
    display_name: str = ""
    suffix: str = ""
    source_type: str = ""
    profile: str = AI_SHARE_PROFILE
    phase: str = "idle"
    message: str = "请选择一份本地副本。"
    preflight: dict[str, int | bool] = field(default_factory=dict)
    candidate_terms: dict[str, list[Any]] = field(default_factory=dict)
    candidate_associations: list[dict[str, str]] = field(default_factory=list)
    table_candidates: list[dict] = field(default_factory=list)
    normalised_candidates: dict[str, list[dict]] = field(default_factory=dict)
    legacy_candidates: bool = True
    occurrences: list[Any] = field(default_factory=list)
    tables: list[Any] = field(default_factory=list)
    dictionary: dict[str, list[str]] = field(default_factory=dict)
    auto_occurrence_ids: set[str] = field(default_factory=set)
    page_count: int = 0
    preview_dpi: int = 160
    region_selector: SelectorState | None = None
    regions_path: Path | None = None
    regions_saved: int = 0
    result_path: Path | None = None
    download_name: str = ""
    error_codes: list[str] = field(default_factory=list)
    job_thread: threading.Thread | None = None
    server: LocalWebServer | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def output_path(self) -> Path:
        return self.workspace / "output"

    def reset_workspace(self) -> None:
        for child in self.workspace.iterdir():
            if child.name == "output":
                shutil.rmtree(child, ignore_errors=True)
            elif child.is_file() or child.is_symlink():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        self.output_path.mkdir(parents=True, exist_ok=True)
        (self.output_path / "redacted_files").mkdir(parents=True, exist_ok=True)
        (self.output_path / "reports").mkdir(parents=True, exist_ok=True)
        (self.output_path / "logs").mkdir(parents=True, exist_ok=True)
        (self.output_path / OUTPUT_MARKER).write_text("local-redaction-assistant\n", encoding="utf-8")
        _safe_chmod(self.output_path / OUTPUT_MARKER)

    def public_payload(self) -> dict[str, Any]:
        with self.lock:
            result = {
                "version": VERSION,
                "phase": self.phase,
                "message": self.message,
                "file_id": self.file_id,
                "display_name": self.display_name,
                "source_type": self.source_type,
                "profile": self.profile,
                "preflight": dict(self.preflight),
                "candidate_terms": {
                    category: list(values) for category, values in self.candidate_terms.items()
                },
                "candidate_associations": list(self.candidate_associations),
                "category_labels": dict(TERM_CATEGORY_LABELS),
                "table_candidates": list(self.table_candidates),
                "dictionary_loaded": bool(self.dictionary),
                "high_confidence_count": len(self.auto_occurrence_ids),
                "page_count": self.page_count,
                "regions_saved": self.regions_saved,
                "manual_review_required": True,
                "raw_values_recorded": False,
                "source_names_recorded_in_reports": False,
                "source_paths_recorded_in_reports": False,
                "coordinates_recorded_in_reports": False,
                "error_codes": list(self.error_codes),
                "download_available": bool(self.result_path and self.result_path.is_file()),
                "download_name": self.download_name,
            }
            return result

    def write_report(self, status: str, *, selected_counts: dict[str, int] | None = None) -> None:
        payload = {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "mode": "local-web-app",
            "source_type": self.source_type,
            "status": status,
            "file_id": self.file_id,
            "profile": self.profile,
            "raw_values_recorded": False,
            "source_names_recorded": False,
            "source_paths_recorded": False,
            "coordinates_recorded": False,
            "manual_review_required": True,
            "selected_term_counts": selected_counts or {},
            "regions_saved": self.regions_saved,
            "error_code_counts": {code: self.error_codes.count(code) for code in sorted(set(self.error_codes))},
            "notes": [
                "The web application is loopback-only and token-protected.",
                "Raw candidate values, source names, source paths and regions are not written to this report.",
                "All generated copies require lawyer review before sharing.",
            ],
        }
        _write_safe_json(self.output_path / "reports" / WEB_REPORT_FILENAME, payload)


def _prepare_docx_candidates(state: WebSession) -> None:
    if state.active_source_path is None:
        raise WebAppError("web_docx_source_missing")
    occurrences, tables, preflight = scan_docx_occurrences(
        state.active_source_path,
        profile=state.profile,
        dictionary=state.dictionary or None,
        main_body_only=state.profile == AI_SHARE_PROFILE,
    )
    state.occurrences = occurrences
    state.tables = tables
    state.preflight = preflight
    if not preflight.get("docx_scan_complete"):
        raise WebAppError("web_docx_scan_incomplete")
    state.candidate_terms = public_candidate_payload(occurrences)
    state.table_candidates = []
    for table in tables:
        public_table = table.public()
        if state.profile == AI_SHARE_PROFILE:
            public_table["actions"] = ["term-only"]
        state.table_candidates.append(public_table)
    state.candidate_associations = []
    state.auto_occurrence_ids = {
        occurrence.occurrence_id for occurrence in occurrences if occurrence.auto_eligible
    }

    normalised: dict[str, list[dict]] = {}
    legacy_flags: list[bool] = []
    for category in TERM_CATEGORIES:
        normalised[category] = []
        for value in state.candidate_terms.get(category, []):
            item, legacy = _normalise_candidate(category, value)
            normalised[category].append(item)
            legacy_flags.append(legacy)
    state.normalised_candidates = normalised
    state.legacy_candidates = all(legacy_flags) if legacy_flags else True
    state.phase = "docx_review"
    state.message = "请在本地页面确认候选词；高置信格式项默认不自动替换。"


def _prepare_visual_session(state: WebSession) -> None:
    if state.source_path is None:
        raise WebAppError("web_visual_source_missing")
    source_kind = "pdf" if state.suffix == ".pdf" else "image"
    try:
        page_count = _inspect_source(state.source_path, source_kind)
    except RegionSelectorError as exc:
        raise WebAppError(str(exc) or "web_visual_preview_unavailable") from exc
    state.page_count = page_count
    state.regions_path = state.output_path / (
        "pdf_redaction_regions.local.json" if source_kind == "pdf" else "image_redaction_regions.local.json"
    )
    state.region_selector = SelectorState(
        source_path=state.source_path,
        source_kind=source_kind,
        file_id=state.file_id,
        page_count=page_count,
        preview_dpi=state.preview_dpi,
        regions_path=state.regions_path,
        token=state.token,
        generation_confirmation_required=True,
    )
    state.phase = "visual_review"
    state.message = "请在页面中框选敏感区域，先保存区域，再确认生成。"


def _prepare_after_upload(state: WebSession) -> None:
    if state.source_path is None:
        raise WebAppError("web_upload_source_missing")
    if state.source_type == "docx":
        if not zipfile.is_zipfile(state.source_path):
            raise WebAppError("web_docx_package_invalid")
        state.active_source_path = state.source_path
        state.preflight = inspect_docx_features(state.source_path)
        if state.profile == "legal-template" and (
            int(state.preflight.get("revision_nodes", 0))
            or bool(state.preflight.get("track_revisions_enabled"))
            or int(state.preflight.get("comments", 0))
        ):
            state.phase = "docx_cleaning_required"
            state.message = "该 DOCX 含修订或批注，需要先确认清洁安全编号临时副本。"
        else:
            _prepare_docx_candidates(state)
    else:
        _prepare_visual_session(state)


def _validate_origin(handler: BaseHTTPRequestHandler, origin: str) -> bool:
    return handler.headers.get("Origin") in {None, "", origin}


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise WebAppError("web_request_invalid") from exc
    if length <= 0 or length > MAX_JSON_BYTES:
        raise WebAppError("web_request_too_large")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WebAppError("web_request_invalid") from exc
    if not isinstance(payload, dict):
        raise WebAppError("web_request_invalid")
    return payload


def _multipart_parameter(header: str, parameter: str) -> str:
    quoted = re.search(rf'(?:^|;)\s*{re.escape(parameter)}="([^"]*)"', header, re.IGNORECASE)
    if quoted:
        return quoted.group(1)
    unquoted = re.search(rf'(?:^|;)\s*{re.escape(parameter)}=([^;\s]+)', header, re.IGNORECASE)
    return unquoted.group(1) if unquoted else ""


def _decode_multipart_filename(filename: str) -> str:
    try:
        return filename.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return filename


def _parse_upload(
    handler: BaseHTTPRequestHandler,
    max_upload_bytes: int,
) -> tuple[str, str, Path, dict[str, list[str]]]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise WebAppError("web_upload_request_invalid") from exc
    if length <= 0 or length > max_upload_bytes:
        raise WebAppError("web_upload_too_large")
    content_type = handler.headers.get("Content-Type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise WebAppError("web_upload_multipart_required")
    boundary_match = re.search(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", content_type, re.IGNORECASE)
    if not boundary_match:
        raise WebAppError("web_upload_boundary_missing")
    boundary = b"--" + (boundary_match.group(1) or boundary_match.group(2)).encode("latin-1")
    profile = AI_SHARE_PROFILE
    display_name = ""
    suffix = ""
    target: Path | None = None
    file_seen = False
    dictionary_payload: object | None = None
    dictionary_seen = False
    try:
        with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as body:
            remaining = length
            while remaining:
                chunk = handler.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise WebAppError("web_upload_multipart_invalid")
                body.write(chunk)
                remaining -= len(chunk)
            body.seek(0)
            raw = body.read()
            if not raw.startswith(boundary):
                raise WebAppError("web_upload_multipart_invalid")
            chunks = raw.split(boundary)
            if len(chunks) < 3 or chunks[0] != b"" or not chunks[-1].lstrip(b"\r\n").startswith(b"--"):
                raise WebAppError("web_upload_multipart_invalid")
            for chunk in chunks[1:-1]:
                if not chunk.strip():
                    continue
                chunk = chunk.lstrip(b"\r\n")
                if b"\r\n\r\n" not in chunk:
                    raise WebAppError("web_upload_multipart_headers_invalid")
                header_bytes, content = chunk.split(b"\r\n\r\n", 1)
                headers: dict[str, str] = {}
                for header_line in header_bytes.split(b"\r\n"):
                    if b":" not in header_line:
                        raise WebAppError("web_upload_multipart_headers_invalid")
                    name, value = header_line.decode("latin-1").split(":", 1)
                    headers[name.strip().lower()] = value.strip()
                disposition = headers.get("content-disposition", "")
                field_name = _multipart_parameter(disposition, "name")
                filename = _decode_multipart_filename(_multipart_parameter(disposition, "filename"))
                if content.endswith(b"\r\n"):
                    content = content[:-2]
                if field_name == "file":
                    if file_seen or not filename:
                        raise WebAppError("web_upload_multiple_files_not_allowed")
                    display_name, suffix = _normalise_upload_filename(filename)
                    target = Path(tempfile.mkstemp(prefix="web-upload-", suffix=suffix)[1])
                    file_seen = True
                    target.write_bytes(content)
                elif field_name == "dictionary":
                    if dictionary_seen or not filename:
                        raise WebAppError("web_upload_multiple_dictionaries_not_allowed")
                    if Path(filename).suffix.lower() != ".json":
                        raise WebAppError("web_dictionary_extension_unsupported")
                    if len(content) > 512 * 1024:
                        raise WebAppError("web_dictionary_too_large")
                    try:
                        dictionary_payload = json.loads(content.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        raise WebAppError("redaction_dictionary_json_invalid") from exc
                    dictionary_seen = True
                else:
                    value = content.decode("utf-8", "replace").strip()
                    if field_name == "profile":
                        profile = value or AI_SHARE_PROFILE
        if not file_seen or target is None:
            raise WebAppError("web_upload_file_missing")
        if profile not in {AI_SHARE_PROFILE, "legal-template"}:
            raise WebAppError("web_profile_unsupported")
        if target.stat().st_size <= 0 or target.stat().st_size > max_upload_bytes:
            raise WebAppError("web_upload_too_large")
        _safe_chmod(target)
        dictionary = normalise_redaction_dictionary(dictionary_payload) if dictionary_payload is not None else {}
    except BaseException:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    return profile, display_name, target, dictionary


def _run_subprocess(command: list[str], timeout_seconds: int = 600) -> int:
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 124
    return completed.returncode


def _run_docx_job(state: WebSession, selection: dict[str, Any]) -> None:
    selected_counts = dict(selection.get("selected_term_counts", {}))
    try:
        if state.active_source_path is None:
            raise WebAppError("web_docx_source_missing")
        output_path = state.output_path
        if state.profile == "legal-template":
            occurrences = state.occurrences
            addition_ids: list[str] = []
            manual_additions = selection.get("manual_additions", {})
            if manual_additions:
                occurrences, addition_ids = add_manual_occurrences(
                    state.active_source_path,
                    occurrences,
                    dict(manual_additions),
                )
            selected_ids = list(dict.fromkeys(list(selection.get("selected_occurrence_ids", [])) + addition_ids))
            final_path = output_path / "redacted_files" / f"{state.file_id}_脱敏版.docx"
            result = create_template_copy(
                state.active_source_path,
                final_path,
                occurrences,
                state.tables,
                selected_ids,
                dict(selection.get("table_actions", {})),
                word_validation="required",
            )
            if result.get("status") != "redaction_succeeded":
                state.error_codes = list(result.get("error_codes", [])) or ["web_docx_redaction_failed"]
            else:
                state.result_path = final_path
                state.download_name = final_path.name
        else:
            occurrences = state.occurrences
            addition_ids: list[str] = []
            manual_additions = selection.get("manual_additions", {})
            if manual_additions:
                occurrences, addition_ids = add_manual_occurrences(
                    state.active_source_path,
                    occurrences,
                    dict(manual_additions),
                    main_body_only=True,
                )
            selected_ids = list(dict.fromkeys(list(selection.get("selected_occurrence_ids", [])) + addition_ids))
            final_path = output_path / "redacted_files" / f"{state.file_id}_脱敏版.docx"
            result = create_template_copy(
                state.active_source_path,
                final_path,
                occurrences,
                [],
                selected_ids,
                {},
                word_validation="off",
                audit_parts={"word/document.xml"},
            )
            if result.get("status") != "redaction_succeeded":
                state.error_codes = list(result.get("error_codes", [])) or ["web_docx_redaction_failed"]
            else:
                state.result_path = final_path
                state.download_name = final_path.name
        success = state.result_path is not None and not state.error_codes
        state.phase = "completed" if success else "failed"
        state.message = "脱敏文件已生成，请下载后逐页人工复核。" if success else "脱敏未完成，请查看错误状态并重新处理。"
        state.write_report("web_docx_completed" if success else "web_docx_failed", selected_counts=selected_counts)
    except (OSError, ValueError, WebAppError, TermSelectorError) as exc:
        state.error_codes = [str(exc) or "web_docx_redaction_failed"]
        state.phase = "failed"
        state.message = "DOCX 脱敏失败，未提供下载文件。"
        state.write_report("web_docx_failed", selected_counts=selected_counts)


def _run_visual_job(state: WebSession) -> None:
    try:
        if state.source_path is None or state.regions_path is None:
            raise WebAppError("web_visual_regions_missing")
        if state.suffix == ".pdf":
            mode = "visual-redact-pdf"
            region_option = "--pdf-regions"
            preferred = f"{state.file_id}_视觉脱敏版.pdf"
        else:
            mode = "visual-redact-image"
            region_option = "--image-regions"
            preferred = f"{state.file_id}_视觉脱敏版{state.suffix}"
        command = [
            sys.executable,
            str(Path(__file__).with_name("local_redactor.py")),
            "--input",
            str(state.source_path),
            "--output",
            str(state.output_path),
            "--mode",
            mode,
            region_option,
            str(state.regions_path),
            "--redaction-padding-ratio",
            "0.004",
            "--allow-existing-output",
        ]
        if state.suffix == ".pdf":
            command.extend(["--render-dpi", str(state.preview_dpi)])
        return_code = _run_subprocess(command)
        report_name = "visual_redaction_report.json" if state.suffix == ".pdf" else "image_visual_redaction_report.json"
        report_path = state.output_path / "reports" / report_name
        if return_code != 0:
            state.error_codes = _error_codes_from_report(report_path) or ["web_visual_redaction_failed"]
        else:
            final_path = _find_safe_output(state.output_path, state.suffix, preferred)
            if final_path is None:
                state.error_codes = ["web_visual_output_missing"]
            else:
                state.result_path = final_path
                state.download_name = final_path.name
        success = state.result_path is not None and not state.error_codes
        state.phase = "completed" if success else "failed"
        state.message = "视觉脱敏文件已生成，请下载后人工复核。" if success else "视觉脱敏未完成，请查看错误状态。"
        state.write_report("web_visual_completed" if success else "web_visual_failed")
    except (OSError, ValueError, WebAppError, RegionSelectorError) as exc:
        state.error_codes = [str(exc) or "web_visual_redaction_failed"]
        state.phase = "failed"
        state.message = "视觉脱敏失败，未提供下载文件。"
        state.write_report("web_visual_failed")


def _start_job(state: WebSession, target, *args: Any) -> None:
    with state.lock:
        if state.job_thread and state.job_thread.is_alive():
            raise WebAppError("web_job_already_running")
        state.phase = "processing"
        state.message = "正在本机处理，请保持页面打开。"
        state.error_codes = []
        state.result_path = None
        state.download_name = ""
        thread = threading.Thread(target=target, args=(state, *args), name="local-redaction-web-job", daemon=True)
        state.job_thread = thread
        thread.start()


def _start_visual_job(state: WebSession) -> None:
    with state.lock:
        if state.region_selector is None or not state.region_selector.regions_are_saved:
            raise WebAppError("web_visual_generate_requires_saved_regions")
        state.region_selector.confirm_generation()
    _start_job(state, lambda current: _run_visual_job(current))


def _handler_factory(state: WebSession, origin: str, max_upload_bytes: int):
    try:
        html = (WEB_UI_ROOT / "index.html").read_text(encoding="utf-8")
        css = (WEB_UI_ROOT / "app.css").read_text(encoding="utf-8")
        javascript = (WEB_UI_ROOT / "app.js").read_text(encoding="utf-8")
    except OSError as exc:
        raise WebAppError("web_ui_assets_missing") from exc

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _authorised(self) -> bool:
            supplied = parse_qs(urlparse(self.path).query).get("token", [""])[0]
            return secrets.compare_digest(supplied, state.token)

        def _send(self, status: HTTPStatus, content_type: str, payload: bytes, *, disposition: str | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'",
            )
            if disposition:
                self.send_header("Content-Disposition", disposition)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: HTTPStatus, payload: object) -> None:
            self._send(status, "application/json; charset=utf-8", _safe_json_bytes(payload))

        def _error(self, status: HTTPStatus, code: str) -> None:
            self._json(status, {"error": code, "raw_values_recorded": False})

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorised():
                self._error(HTTPStatus.FORBIDDEN, "web_access_denied")
                return
            path = urlparse(self.path).path
            if path == "/":
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", html.replace("__TOKEN__", state.token).encode("utf-8"))
                return
            if path == "/assets/app.css":
                self._send(HTTPStatus.OK, "text/css; charset=utf-8", css.encode("utf-8"))
                return
            if path == "/assets/app.js":
                self._send(HTTPStatus.OK, "application/javascript; charset=utf-8", javascript.encode("utf-8"))
                return
            if path == "/api/session":
                self._json(HTTPStatus.OK, state.public_payload())
                return
            if path.startswith("/api/preview/") and path.endswith(".png"):
                try:
                    page = int(path.removeprefix("/api/preview/").removesuffix(".png"))
                    if state.region_selector is None:
                        raise RegionSelectorError("web_visual_preview_unavailable")
                    self._send(HTTPStatus.OK, "image/png", state.region_selector.page_png(page))
                except (ValueError, RegionSelectorError) as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc) or "web_visual_preview_unavailable")
                return
            if path == "/api/download":
                if state.phase != "completed" or state.result_path is None or not state.result_path.is_file():
                    self._error(HTTPStatus.NOT_FOUND, "web_download_not_available")
                    return
                try:
                    payload = state.result_path.read_bytes()
                except OSError:
                    self._error(HTTPStatus.NOT_FOUND, "web_download_not_available")
                    return
                download_name = state.download_name or f"{state.file_id}_脱敏文件{state.suffix}"
                self._send(
                    HTTPStatus.OK,
                    "application/octet-stream",
                    payload,
                    disposition=f"attachment; filename*=UTF-8''{quote(download_name)}",
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "web_route_missing")

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorised() or not _validate_origin(self, origin):
                self._error(HTTPStatus.FORBIDDEN, "web_access_denied")
                return
            path = urlparse(self.path).path
            try:
                if path == "/api/upload":
                    with state.lock:
                        if state.job_thread and state.job_thread.is_alive():
                            raise WebAppError("web_job_already_running")
                        profile, display_name, uploaded_path, dictionary = _parse_upload(self, max_upload_bytes)
                        state.reset_workspace()
                        target = state.workspace / f"{state.file_id}{uploaded_path.suffix.lower()}"
                        uploaded_path.replace(target)
                        state.source_path = target
                        state.active_source_path = target
                        state.display_name = display_name
                        state.suffix = target.suffix.lower()
                        state.source_type = _safe_source_type(state.suffix)
                        state.profile = profile
                        state.dictionary = dictionary
                        state.preflight = {}
                        state.candidate_terms = {}
                        state.candidate_associations = []
                        state.table_candidates = []
                        state.normalised_candidates = {}
                        state.legacy_candidates = True
                        state.occurrences = []
                        state.tables = []
                        state.auto_occurrence_ids = set()
                        state.page_count = 0
                        state.regions_saved = 0
                        state.region_selector = None
                        state.regions_path = None
                        state.error_codes = []
                        state.result_path = None
                        state.download_name = ""
                        _prepare_after_upload(state)
                    self._json(HTTPStatus.OK, state.public_payload())
                    return
                if path == "/api/docx/clean":
                    payload = _read_json_body(self)
                    with state.lock:
                        if state.phase != "docx_cleaning_required" or state.source_path is None:
                            raise WebAppError("web_docx_cleaning_not_required")
                        accept_revisions = bool(payload.get("accept_revisions"))
                        remove_comments = bool(payload.get("remove_comments"))
                        if int(state.preflight.get("revision_nodes", 0)) and not accept_revisions:
                            raise WebAppError("web_docx_revision_confirmation_required")
                        if int(state.preflight.get("comments", 0)) and not remove_comments:
                            raise WebAppError("web_docx_comment_confirmation_required")
                        cleaned_path = state.workspace / f"{state.file_id}-clean.docx"

                        def clean_job(current: WebSession) -> None:
                            try:
                                from word_native_validator import create_clean_copy_with_word

                                ok, errors = create_clean_copy_with_word(
                                    current.source_path,
                                    cleaned_path,
                                    accept_revisions=accept_revisions,
                                    remove_comments=remove_comments,
                                )
                                if not ok:
                                    raise WebAppError(str(errors[0]) if errors else "web_docx_word_cleaning_failed")
                                cleaned_preflight = inspect_docx_features(cleaned_path)
                                if (
                                    int(cleaned_preflight.get("revision_nodes", 0))
                                    or bool(cleaned_preflight.get("track_revisions_enabled"))
                                    or int(cleaned_preflight.get("comments", 0))
                                ):
                                    raise WebAppError("web_docx_cleaning_audit_failed")
                                current.active_source_path = cleaned_path
                                _prepare_docx_candidates(current)
                            except (OSError, ValueError, WebAppError, TermSelectorError, ImportError) as exc:
                                current.error_codes = [str(exc) or "web_docx_cleaning_failed"]
                                current.phase = "failed"
                                current.message = "Word 清洁失败，未生成下载文件。"
                                current.write_report("web_docx_cleaning_failed")

                        _start_job(state, clean_job)
                    self._json(HTTPStatus.ACCEPTED, state.public_payload())
                    return
                if path == "/api/docx/confirm":
                    payload = _read_json_body(self)
                    with state.lock:
                        if state.phase != "docx_review":
                            raise WebAppError("web_docx_confirmation_not_available")
                        selector = TermSelectorState(
                            file_id=state.file_id,
                            candidates=state.normalised_candidates,
                            token=state.token,
                            candidate_associations=_normalise_associations(state.candidate_associations),
                            redaction_profile=state.profile,
                            tables=_normalise_tables(state.table_candidates) if state.profile == "legal-template" else [],
                            legacy_candidates=state.legacy_candidates,
                            auto_occurrence_ids=set(state.auto_occurrence_ids),
                        )
                        selector.confirm(payload)
                        if selector.result is None:
                            raise WebAppError("web_docx_confirmation_invalid")
                        _start_job(state, _run_docx_job, selector.result)
                    self._json(HTTPStatus.ACCEPTED, state.public_payload())
                    return
                if path == "/api/visual/save-regions":
                    payload = _read_json_body(self)
                    with state.lock:
                        if state.phase != "visual_review" or state.region_selector is None:
                            raise WebAppError("web_visual_selection_not_available")
                        state.region_selector.save_regions(payload.get("regions"))
                        state.regions_saved = state.region_selector.regions_saved
                        state.message = "区域已保存。请检查后点击确认生成。"
                    self._json(HTTPStatus.OK, state.public_payload())
                    return
                if path == "/api/visual/generate":
                    with state.lock:
                        if state.phase != "visual_review" or state.region_selector is None:
                            raise WebAppError("web_visual_generation_not_available")
                        if not state.region_selector.regions_are_saved:
                            raise WebAppError("web_visual_generate_requires_saved_regions")
                        state.region_selector.confirm_generation()
                        _start_job(state, _run_visual_job)
                    self._json(HTTPStatus.ACCEPTED, state.public_payload())
                    return
                if path == "/api/reset":
                    with state.lock:
                        if state.job_thread and state.job_thread.is_alive():
                            raise WebAppError("web_job_already_running")
                        state.reset_workspace()
                        state.source_path = None
                        state.active_source_path = None
                        state.display_name = ""
                        state.suffix = ""
                        state.source_type = ""
                        state.profile = AI_SHARE_PROFILE
                        state.dictionary = {}
                        state.phase = "idle"
                        state.message = "请选择一份本地副本。"
                        state.preflight = {}
                        state.candidate_terms = {}
                        state.candidate_associations = []
                        state.table_candidates = []
                        state.normalised_candidates = {}
                        state.legacy_candidates = True
                        state.occurrences = []
                        state.tables = []
                        state.auto_occurrence_ids = set()
                        state.page_count = 0
                        state.region_selector = None
                        state.regions_path = None
                        state.regions_saved = 0
                        state.result_path = None
                        state.download_name = ""
                        state.error_codes = []
                    self._json(HTTPStatus.OK, state.public_payload())
                    return
                if path == "/api/shutdown":
                    self._json(HTTPStatus.OK, {"status": "shutdown_requested"})
                    if state.server is not None:
                        threading.Thread(target=state.server.shutdown, daemon=True).start()
                    return
                self._error(HTTPStatus.NOT_FOUND, "web_route_missing")
            except WebAppError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc) or "web_request_invalid")
            except (OSError, ValueError, TermSelectorError, RegionSelectorError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc) or "web_request_invalid")

    return Handler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the local-only browser redaction application.")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 chooses a random available port.")
    parser.add_argument("--timeout-seconds", type=int, default=1800, help="Idle server lifetime. Default: 1800.")
    parser.add_argument("--max-upload-mb", type=int, default=200, help="Maximum selected file size. Default: 200.")
    parser.add_argument("--no-open-browser", action="store_true", help="Do not open the browser automatically.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.port < 0 or args.port > 65535:
        raise SystemExit("--port must be between 0 and 65535")
    if args.timeout_seconds < 60 or args.timeout_seconds > 24 * 60 * 60:
        raise SystemExit("--timeout-seconds must be between 60 and 86400")
    if args.max_upload_mb < 1 or args.max_upload_mb > 500:
        raise SystemExit("--max-upload-mb must be between 1 and 500")
    max_upload_bytes = args.max_upload_mb * 1024 * 1024
    temporary_workspace = tempfile.TemporaryDirectory(prefix="local-redaction-web-")
    workspace = Path(temporary_workspace.name)
    state = WebSession(workspace=workspace)
    state.reset_workspace()
    try:
        server = LocalWebServer(("127.0.0.1", args.port), _handler_factory(state, "", max_upload_bytes))
    except OSError as exc:
        temporary_workspace.cleanup()
        raise SystemExit(f"web server could not start on 127.0.0.1:{args.port}: {exc}") from exc
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    server.RequestHandlerClass = _handler_factory(state, origin, max_upload_bytes)
    state.server = server
    url = f"{origin}/?token={quote(state.token)}"
    print(f"local redaction web app ready: {url}")
    print("bound to 127.0.0.1 only; keep this terminal open until the browser workflow is complete")
    timer = threading.Timer(args.timeout_seconds, server.shutdown)
    timer.daemon = True
    timer.start()
    if not args.no_open_browser:
        webbrowser.open(url, new=1, autoraise=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("local redaction web app stopped")
    finally:
        timer.cancel()
        server.server_close()
        temporary_workspace.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
