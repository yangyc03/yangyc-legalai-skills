#!/usr/bin/env python3
"""Sequential, manifest-only legal-template batch orchestration (maximum 30 DOCX files)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path

from review_book import ReviewBookError, build_review_book
from word_native_validator import export_pdf_with_word


MAX_BATCH_FILES = 30
SAFE_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]{2,31}$")


class BatchError(RuntimeError):
    pass


def load_manifest(path: Path) -> list[tuple[str, Path]]:
    if not path.is_file() or path.is_symlink():
        raise BatchError("batch_manifest_invalid")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BatchError("batch_manifest_permissions_not_owner_only")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BatchError("batch_manifest_invalid") from exc
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_BATCH_FILES:
        raise BatchError("batch_manifest_file_count_invalid")
    result: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            raise BatchError("batch_manifest_entry_invalid")
        safe_id = item.get("safe_id")
        raw_path = item.get("path")
        if not isinstance(safe_id, str) or not SAFE_ID_PATTERN.fullmatch(safe_id) or safe_id in seen:
            raise BatchError("batch_manifest_safe_id_invalid")
        if not isinstance(raw_path, str):
            raise BatchError("batch_manifest_entry_invalid")
        input_path = Path(raw_path).expanduser()
        if not input_path.is_absolute() or not input_path.is_file() or input_path.is_symlink() or input_path.suffix.lower() != ".docx":
            raise BatchError("batch_manifest_docx_invalid")
        seen.add(safe_id)
        result.append((safe_id, input_path))
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _guided_errors(report_path: Path) -> list[str]:
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        return sorted(payload.get("error_code_counts", {}).keys())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ["batch_guided_report_missing"]


def run_batch(args, output_path: Path, run_docx_flow, prepare_output) -> int:
    manifest_path = Path(args.input_manifest).expanduser()
    try:
        items = load_manifest(manifest_path)
    except BatchError as exc:
        raise SystemExit(str(exc)) from exc
    prepare_output(output_path)
    for directory in (output_path / "review_pdfs", output_path / "review_book"):
        directory.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    review_items: list[tuple[str, Path]] = []
    for safe_id, source in items:
        if source.stat().st_size > args.max_file_size_mb * 1024 * 1024:
            records.append({"safe_id": safe_id, "status": "redaction_failed", "error_codes": ["guided_input_too_large"]})
            continue
        before_hash = _sha256(source)
        before_mtime = source.stat().st_mtime_ns
        status_code = run_docx_flow(args, source, output_path, safe_id)
        guided_report = output_path / "reports" / "guided_workflow_report.json"
        safe_report = output_path / "reports" / f"{safe_id}_guided_workflow_report.json"
        if guided_report.exists():
            guided_report.replace(safe_report)
        record = {"safe_id": safe_id, "status": "failed", "error_codes": []}
        if _sha256(source) != before_hash or source.stat().st_mtime_ns != before_mtime:
            record["error_codes"] = ["batch_original_changed"]
            records.append(record)
            continue
        redacted = output_path / "redacted_files" / f"{safe_id}_脱敏版.docx"
        if status_code != 0 or not redacted.is_file():
            record["status"] = "cancelled" if status_code == 0 else "redaction_failed"
            record["error_codes"] = _guided_errors(safe_report)
            records.append(record)
            continue
        pdf_path = output_path / "review_pdfs" / f"{safe_id}.pdf"
        pdf_ok, pdf_errors = export_pdf_with_word(redacted, pdf_path)
        if not pdf_ok:
            record["status"] = "word_pdf_failed"
            record["error_codes"] = pdf_errors
            records.append(record)
            continue
        record["status"] = "succeeded"
        records.append(record)
        review_items.append((safe_id, pdf_path))
        for directory in (output_path / "redacted_files", output_path / "review_pdfs"):
            for lock in directory.glob(f"~${safe_id}*"):
                lock.unlink(missing_ok=True)

    mapping: list[dict] = []
    review_error: str | None = None
    if review_items:
        try:
            mapping = build_review_book(review_items, output_path / "review_book" / "batch_review_book.pdf")
        except ReviewBookError as exc:
            review_error = str(exc)
    page_map = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": mapping,
        "raw_values_recorded": False,
        "source_paths_recorded": False,
    }
    (output_path / "review_book" / "page_map.json").write_text(
        json.dumps(page_map, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    summary = {
        "tool": "local-redaction-assistant",
        "version": "1.2.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "legal-template-controlled-batch",
        "files_total": len(records),
        "files_succeeded": sum(item["status"] == "succeeded" for item in records),
        "files_failed": sum(item["status"] not in {"succeeded", "cancelled"} for item in records),
        "files_cancelled": sum(item["status"] == "cancelled" for item in records),
        "review_book_created": bool(mapping) and review_error is None,
        "review_book_error_code": review_error,
        "files": records,
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "candidate_terms_recorded": False,
        "coordinates_recorded": False,
        "manual_review_required": True,
    }
    summary_path = output_path / "reports" / "batch_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(summary_path, 0o600)
    return 0 if all(item["status"] in {"succeeded", "cancelled"} for item in records) and review_error is None else 2
