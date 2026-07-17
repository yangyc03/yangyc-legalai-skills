#!/usr/bin/env python3
"""Prepare, validate, build and audit portable legal network workpapers.

This helper is deliberately offline. An authorized host Agent or the user
performs browser interaction; this script only handles local structured data,
privacy checks and document generation.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "1.0"
DEFAULT_TIMEZONE = "Asia/Shanghai"
ALLOWED_PROFILES = {"general-person", "general-company", "private-fund", "custom"}
ALLOWED_SUBJECT_TYPES = {"natural_person", "company"}
ALLOWED_STATUSES = {
    "identity_match",
    "no_match_displayed",
    "same_name_candidates",
    "not_applicable",
    "access_limited",
    "failed",
    "pending_review",
}
STATUS_LABELS = {
    "identity_match": "明确匹配",
    "no_match_displayed": "未显示可确认匹配",
    "same_name_candidates": "同名待核",
    "not_applicable": "查询对象不适用",
    "access_limited": "查询受限",
    "failed": "查询失败",
    "pending_review": "待复核",
}
SCREENSHOT_REQUIRED = {
    "identity_match",
    "no_match_displayed",
    "same_name_candidates",
    "not_applicable",
    "pending_review",
}
FORBIDDEN_KEYS = {
    "id_number",
    "identity_number",
    "identity_card_number",
    "birth_date",
    "birthday",
    "mobile",
    "phone",
    "email",
    "cookie",
    "token",
    "password",
    "authorization",
    "raw_response",
    "request_headers",
}
FORBIDDEN_ABSOLUTE_PHRASES = {
    "确定不存在",
    "不存在任何",
    "无任何诉讼",
    "无任何执行",
    "无任何处罚",
    "无任何失信",
    "所有同名结果均与本人无关",
}
CHINA_ID_PATTERN = re.compile(
    r"(?<!\d)(?:\d{17}[0-9Xx]|\d{8}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3})(?!\d)"
)
BIRTHDATE_LABEL_PATTERN = re.compile(
    r"(?:出生日期|出生年月|生日)\s*[:：]?\s*(?:19|20)\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?"
)
ZERO_WIDTH_SPACE = "\u200b"
MASKED_ID_PATTERN = re.compile(r"^\d{10}\*{8}$")
SENSITIVE_NO_CAPTURE_KIND = "not_retained_sensitive_page"
WATERMARKED_CAPTURE_KIND = "watermarked_page_only"
OUTPUT_DOCX_FONT = "Noto Serif CJK SC"
BUNDLED_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "assets" / "generic-network-query-record.docx"
)
TEXT_ARTIFACT_SUFFIXES = {
    ".csv",
    ".json",
    ".log",
    ".md",
    ".rels",
    ".rst",
    ".svg",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
RASTER_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
LOCAL_PATH_PATTERNS = (
    re.compile(r"/Users/[^/\s]+/"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"),
    re.compile(r"(?:^|[/\\])\.codex(?:[/\\]|$)"),
    re.compile(r"(?:^|[/\\])\.workbuddy(?:[/\\]|$)"),
    re.compile(r"OneDrive", re.IGNORECASE),
)
SENSITIVE_LABEL_PATTERN = re.compile(
    r"(?:cookie|password|token|authorization|raw_response|request_headers)\s*[:=]",
    re.IGNORECASE,
)


class ValidationError(ValueError):
    """Raised when a run file violates the Skill contract."""


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"JSON file not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValidationError("JSON root must be an object.")
    return value


def write_json(path: Path, value: dict[str, Any], *, overwrite: bool = False) -> None:
    if path.is_symlink():
        raise ValidationError(f"Output path must not be a symbolic link: {path}")
    if path.exists() and not overwrite:
        raise FileExistsError(f"Output file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
    )


def require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object.")
    return value


def require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError(f"{label} must be a list.")
    return value


def require_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValidationError(f"{label} is required.")
    return text


def require_timestamp(value: Any, label: str) -> datetime:
    text = require_text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO 8601 timestamp.") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{label} must include a timezone offset.")
    return parsed


def scan_sensitive_values(value: Any, location: str = "run") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in FORBIDDEN_KEYS:
                raise ValidationError(f"Sensitive field is not allowed: {location}.{key}")
            scan_sensitive_values(item, f"{location}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            scan_sensitive_values(item, f"{location}[{index}]")
        return
    if isinstance(value, str):
        if CHINA_ID_PATTERN.search(value):
            raise ValidationError(f"Full identity number is not allowed: {location}")
        if BIRTHDATE_LABEL_PATTERN.search(value):
            raise ValidationError(f"Exact birth date must not be persisted: {location}")


def validate_safe_relative_path(value: Any, label: str) -> Path:
    text = require_text(value, label)
    path = Path(text)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError(f"{label} must be a safe relative path without '..'.")
    return path


def check_wording(text: str, label: str) -> None:
    for phrase in FORBIDDEN_ABSOLUTE_PHRASES:
        if phrase in text:
            raise ValidationError(f"Absolute nonexistence wording is not allowed in {label}: {phrase}")


def text_privacy_issues(text: str, label: str) -> list[str]:
    issues: list[str] = []
    if CHINA_ID_PATTERN.search(text):
        issues.append(f"full identity number: {label}")
    if SENSITIVE_LABEL_PATTERN.search(text):
        issues.append(f"credential or authorization field: {label}")
    for pattern in LOCAL_PATH_PATTERNS:
        if pattern.search(text):
            issues.append(f"machine-specific path: {label}")
            break
    return issues


def metadata_value_text(value: Any) -> str:
    if isinstance(value, bytes):
        decoded: list[str] = []
        for encoding in ("utf-8", "utf-16-le", "utf-16-be", "latin-1"):
            try:
                text = value.decode(encoding)
            except UnicodeDecodeError:
                continue
            if text not in decoded:
                decoded.append(text)
        return "\n".join(decoded)
    if isinstance(value, dict):
        return "\n".join(
            f"{metadata_value_text(key)}={metadata_value_text(item)}"
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return "\n".join(metadata_value_text(item) for item in value)
    return str(value)


def image_metadata_privacy_issues(payload: bytes, label: str) -> list[str]:
    try:
        from PIL import Image

        with Image.open(BytesIO(payload)) as image:
            metadata_items: list[tuple[Any, Any]] = list(image.info.items())
            try:
                metadata_items.extend(image.getexif().items())
            except (AttributeError, OSError, ValueError):
                pass
            metadata_text = "\n".join(
                f"{metadata_value_text(key)}={metadata_value_text(value)}"
                for key, value in metadata_items
            )
    except Exception as exc:
        return [f"unreadable image metadata: {label}: {exc}"]
    return text_privacy_issues(metadata_text, label)


def atomic_write_text(path: Path, text: str) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def safe_output_destination(path: Path, root: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValidationError(f"{label} must not be a symbolic link: {path}")
    resolved_parent = path.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ValidationError(f"{label} escapes workpaper_root: {path}") from exc
    destination = resolved_parent / path.name
    if destination.is_symlink():
        raise ValidationError(f"{label} must not be a symbolic link: {path}")
    return destination


def atomic_copy_file(source: Path, destination: Path) -> None:
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary_path)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def template_check(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"DOCX template not found: {path}")
    if path.suffix.lower() != ".docx":
        raise ValidationError("Template must use .docx extension.")

    issues: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                issues.append("missing word/document.xml")
            combined_xml: list[str] = []
            for name in sorted(names):
                lower_name = name.lower()
                suffix = Path(lower_name).suffix
                if lower_name.endswith((".xml", ".rels", ".txt")):
                    text = archive.read(name).decode("utf-8", errors="ignore")
                    combined_xml.append(text)
                    issues.extend(text_privacy_issues(text, name))
                elif lower_name.startswith("word/media/") and suffix in RASTER_IMAGE_SUFFIXES:
                    issues.extend(
                        image_metadata_privacy_issues(
                            archive.read(name),
                            f"{name}!image-metadata",
                        )
                    )
                elif lower_name.startswith("word/media/") and suffix == ".svg":
                    text = archive.read(name).decode("utf-8", errors="ignore")
                    issues.extend(text_privacy_issues(text, name))
            package_text = "\n".join(combined_xml)
            if not re.search(r"\bPAGE\b", package_text):
                issues.append("missing Word PAGE field")
    except zipfile.BadZipFile as exc:
        raise ValidationError(f"Template is not a valid DOCX package: {path}") from exc

    try:
        from docx import Document

        document = Document(str(path))
    except Exception as exc:
        raise ValidationError(f"Template cannot be opened as DOCX: {path}") from exc

    subject_marker_rows = []
    for table in document.tables:
        for row in table.rows:
            if "【SUBJECT_ROWS】" in "".join(cell.text for cell in row.cells):
                subject_marker_rows.append(row)
                if len(row.cells) < 3:
                    issues.append("subject marker table must contain at least three columns")
    if len(subject_marker_rows) != 1:
        issues.append("template must contain exactly one 【SUBJECT_ROWS】 marker row")

    query_markers = [
        paragraph for paragraph in document.paragraphs if "【QUERY_RESULT_ITEMS】" in paragraph.text
    ]
    if len(query_markers) != 1:
        issues.append("template must contain exactly one 【QUERY_RESULT_ITEMS】 marker paragraph")

    if issues:
        raise ValidationError("DOCX template check failed: " + "; ".join(dict.fromkeys(issues)))
    return {
        "path": str(path),
        "subject_marker_rows": 1,
        "query_marker_paragraphs": 1,
        "page_field": True,
        "privacy_safe": True,
    }


def artifact_audit(root: Path) -> dict[str, Any]:
    if not root.exists():
        raise FileNotFoundError(f"Artifact path not found: {root}")
    files = [root] if root.is_file() else sorted(path for path in root.rglob("*") if path.is_file())
    issues: list[str] = []
    scanned = 0

    for path in files:
        scanned += 1
        label = path.name if root.is_file() else path.relative_to(root).as_posix()
        issues.extend(text_privacy_issues(label, f"filename:{label}"))
        suffix = path.suffix.lower()
        if suffix in TEXT_ARTIFACT_SUFFIXES:
            issues.extend(
                text_privacy_issues(path.read_text(encoding="utf-8", errors="ignore"), label)
            )
        elif suffix == ".docx":
            try:
                with zipfile.ZipFile(path) as archive:
                    for member in archive.namelist():
                        lower_member = member.lower()
                        member_suffix = Path(lower_member).suffix
                        if lower_member.endswith((".xml", ".rels", ".txt", ".json")):
                            text = archive.read(member).decode("utf-8", errors="ignore")
                            issues.extend(text_privacy_issues(text, f"{label}!{member}"))
                        elif (
                            lower_member.startswith("word/media/")
                            and member_suffix in RASTER_IMAGE_SUFFIXES
                        ):
                            issues.extend(
                                image_metadata_privacy_issues(
                                    archive.read(member),
                                    f"{label}!{member}!image-metadata",
                                )
                            )
                        elif lower_member.startswith("word/media/") and member_suffix == ".svg":
                            text = archive.read(member).decode("utf-8", errors="ignore")
                            issues.extend(text_privacy_issues(text, f"{label}!{member}"))
            except zipfile.BadZipFile:
                issues.append(f"invalid DOCX package: {label}")
        elif suffix in RASTER_IMAGE_SUFFIXES:
            issues.extend(
                image_metadata_privacy_issues(
                    path.read_bytes(),
                    f"{label}!image-metadata",
                )
            )

    return {
        "root": str(root),
        "scanned_files": scanned,
        "issues": sorted(dict.fromkeys(issues)),
        "ok": not issues,
    }


def doctor_report(browser: str) -> dict[str, Any]:
    dependency_modules = {
        "python-docx": "docx",
        "Pillow": "PIL",
        "lxml": "lxml",
    }
    dependencies = {
        name: importlib.util.find_spec(module_name) is not None
        for name, module_name in dependency_modules.items()
    }
    template_ok = False
    template_message = "bundled template is missing"
    if BUNDLED_TEMPLATE.is_file() and all(dependencies.values()):
        try:
            template_check(BUNDLED_TEMPLATE)
            template_ok = True
            template_message = "bundled template passed structural and privacy checks"
        except (FileNotFoundError, OSError, ValidationError) as exc:
            template_message = str(exc)

    local_ready = sys.version_info >= (3, 10) and all(dependencies.values())
    if browser == "available" and local_ready:
        mode = "full-workflow"
    elif local_ready:
        mode = "local-processing-only"
    elif browser == "available":
        mode = "browser-only"
    else:
        mode = "planning-only"

    return {
        "python": {
            "version": platform.python_version(),
            "minimum": "3.10",
            "ok": sys.version_info >= (3, 10),
        },
        "dependencies": dependencies,
        "browser": browser,
        "libreoffice": shutil.which("soffice") or shutil.which("libreoffice") or "",
        "bundled_template": {
            "available": BUNDLED_TEMPLATE.is_file(),
            "ok": template_ok,
            "message": template_message,
        },
        "mode": mode,
    }


def validate_run(
    data: dict[str, Any],
    *,
    workpaper_root: Path | None = None,
    formal_mode: str = "draft",
) -> None:
    scan_sensitive_values(data)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(f"Unsupported schema_version: {data.get('schema_version')}")

    run = require_object(data.get("run"), "run")
    require_text(run.get("run_id"), "run.run_id")
    timezone_name = require_text(run.get("timezone"), "run.timezone")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:  # pragma: no cover - platform timezone database edge
        raise ValidationError(f"Unknown timezone: {timezone_name}") from exc
    profile = require_text(run.get("profile"), "run.profile")
    if profile not in ALLOWED_PROFILES:
        raise ValidationError(f"run.profile must be one of: {', '.join(sorted(ALLOWED_PROFILES))}")

    project = require_object(run.get("project"), "run.project")
    require_text(project.get("name"), "run.project.name")
    require_text(project.get("short_name"), "run.project.short_name")
    require_text(project.get("matter"), "run.project.matter")
    require_text(project.get("period"), "run.project.period")

    subjects = require_list(data.get("subjects"), "subjects")
    if not subjects:
        raise ValidationError("subjects must not be empty.")
    subject_map: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(subjects):
        subject = require_object(raw, f"subjects[{index}]")
        subject_id = require_text(subject.get("subject_id"), f"subjects[{index}].subject_id")
        if subject_id in subject_map:
            raise ValidationError(f"Duplicate subject_id: {subject_id}")
        subject_type = require_text(subject.get("type"), f"subjects[{index}].type")
        if subject_type not in ALLOWED_SUBJECT_TYPES:
            raise ValidationError(f"Invalid subject type for {subject_id}: {subject_type}")
        require_text(subject.get("name"), f"subjects[{index}].name")
        require_text(subject.get("role"), f"subjects[{index}].role")
        if subject_type == "natural_person" and str(subject.get("credit_code") or "").strip():
            raise ValidationError(f"Natural person must not use credit_code field: {subject_id}")
        masked_id = str(subject.get("masked_id_number") or "").strip()
        if subject_type == "company" and masked_id:
            raise ValidationError(f"Company must not use masked_id_number field: {subject_id}")
        if subject_type == "natural_person" and masked_id and not MASKED_ID_PATTERN.fullmatch(masked_id):
            raise ValidationError(
                "masked_id_number must keep 10 digits and mask the final 8 characters: "
                + subject_id
            )
        subject_map[subject_id] = subject

    queries = require_list(data.get("queries"), "queries")
    evidence_ids: set[str] = set()
    for index, raw in enumerate(queries):
        query = require_object(raw, f"queries[{index}]")
        evidence_id = require_text(query.get("evidence_id"), f"queries[{index}].evidence_id")
        if evidence_id in evidence_ids:
            raise ValidationError(f"Duplicate evidence_id: {evidence_id}")
        evidence_ids.add(evidence_id)
        subject_id = require_text(query.get("subject_id"), f"queries[{index}].subject_id")
        if subject_id not in subject_map:
            raise ValidationError(f"Query references unknown subject: {subject_id}")
        require_text(query.get("site_id"), f"queries[{index}].site_id")
        require_text(query.get("site_name"), f"queries[{index}].site_name")
        url = require_text(query.get("url"), f"queries[{index}].url")
        if not re.match(r"^https?://", url, re.IGNORECASE):
            raise ValidationError(f"Query URL must use HTTP(S): {evidence_id}")
        require_timestamp(query.get("query_time"), f"queries[{index}].query_time")
        require_text(query.get("query_terms"), f"queries[{index}].query_terms")
        require_text(query.get("filters"), f"queries[{index}].filters")
        status = require_text(query.get("status"), f"queries[{index}].status")
        if status not in ALLOWED_STATUSES:
            raise ValidationError(f"Invalid status for {evidence_id}: {status}")
        summary = require_text(query.get("result_summary"), f"queries[{index}].result_summary")
        assessment = require_text(
            query.get("identity_assessment"), f"queries[{index}].identity_assessment"
        )
        check_wording(summary, f"queries[{index}].result_summary")
        check_wording(assessment, f"queries[{index}].identity_assessment")

        combined = summary + assessment
        formal_negative_wording = (
            "未发现与" in combined and "相符" in combined and "负面记录" in combined
        )
        if status == "no_match_displayed" and not (
            any(token in combined for token in ("未显示", "未发现可确认", "未检出匹配"))
            or formal_negative_wording
        ):
            raise ValidationError(
                "no_match_displayed must use limited no-match wording: " + evidence_id
            )
        if status in {"access_limited", "failed"} and any(
            token in combined for token in ("未发现记录", "未显示匹配", "没有记录")
        ):
            raise ValidationError(f"Restricted or failed query cannot claim no record: {evidence_id}")

        screenshot_text = str(query.get("screenshot_path") or "").strip()
        capture_kind = str(query.get("capture_kind") or "").strip()
        sensitive_no_capture = capture_kind == SENSITIVE_NO_CAPTURE_KIND
        if status in SCREENSHOT_REQUIRED and not screenshot_text and not sensitive_no_capture:
            raise ValidationError(
                "Screenshot is required unless a sensitive page was not retained: " + evidence_id
            )
        if screenshot_text:
            screenshot_path = validate_safe_relative_path(
                screenshot_text, f"queries[{index}].screenshot_path"
            )
            if capture_kind != WATERMARKED_CAPTURE_KIND:
                raise ValidationError(f"Screenshot must be watermarked_page_only: {evidence_id}")
            if workpaper_root is not None:
                root = workpaper_root.resolve()
                candidate = (root / screenshot_path).resolve()
                try:
                    candidate.relative_to(root)
                except ValueError as exc:
                    raise ValidationError(f"Screenshot escapes workpaper root: {evidence_id}") from exc
                if not candidate.is_file():
                    raise FileNotFoundError(f"Screenshot not found: {candidate}")
        elif sensitive_no_capture:
            if "敏感信息保护" not in (summary + str(query.get("follow_up") or "")):
                raise ValidationError(
                    "Sensitive no-capture record must explain sensitive information protection: "
                    + evidence_id
                )
        elif capture_kind:
            raise ValidationError(f"capture_kind is invalid without a screenshot: {evidence_id}")

    if formal_mode not in {"none", "draft", "final"}:
        raise ValidationError("formal_mode must be none, draft, or final.")
    if formal_mode == "final":
        formal = require_object(run.get("formal_record"), "run.formal_record")
        require_text(formal.get("query_date"), "run.formal_record.query_date")
        require_text(formal.get("query_location"), "run.formal_record.query_location")
        people = require_list(formal.get("query_people"), "run.formal_record.query_people")
        if not people or any(not str(item).strip() for item in people):
            raise ValidationError("run.formal_record.query_people must not be empty in final mode.")
        for subject in subjects:
            subject_id = subject["subject_id"]
            if subject["type"] == "company":
                require_text(subject.get("credit_code"), f"subjects[{subject_id}].credit_code")
            else:
                require_text(
                    subject.get("masked_id_number"),
                    f"subjects[{subject_id}].masked_id_number",
                )


def prepare_run(input_data: dict[str, Any]) -> dict[str, Any]:
    scan_sensitive_values(input_data, "input")
    project = require_object(input_data.get("project"), "project")
    subjects = copy.deepcopy(require_list(input_data.get("subjects"), "subjects"))
    formal_input = require_object(input_data.get("formal_record", {}), "formal_record")
    now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))
    prepared = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": str(input_data.get("run_id") or f"NQ-{now:%Y%m%d-%H%M%S}"),
            "timezone": str(input_data.get("timezone") or DEFAULT_TIMEZONE),
            "profile": str(input_data.get("profile") or "custom"),
            "project": {
                "name": require_text(project.get("name"), "project.name"),
                "short_name": require_text(project.get("short_name"), "project.short_name"),
                "matter": require_text(project.get("matter"), "project.matter"),
                "period": require_text(project.get("period"), "project.period"),
            },
            "formal_record": {
                "query_date": str(formal_input.get("query_date") or ""),
                "query_location": str(formal_input.get("query_location") or ""),
                "query_people": copy.deepcopy(formal_input.get("query_people", [])),
            },
        },
        "subjects": subjects,
        "queries": [],
        "opinion_wording_requested": bool(input_data.get("opinion_wording_requested", False)),
    }
    validate_run(prepared, formal_mode="draft")
    return prepared


def safe_filename(value: str) -> str:
    text = re.sub(r"[\\/:*?\"<>|]", "-", value.strip())
    text = re.sub(r"\s+", "", text)
    text = text.strip(".-")
    return text or "未命名"


def markdown_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def markdown_screenshot_link(
    screenshot_path: str,
    *,
    workpaper_root: Path | None = None,
    markdown_dir: Path | None = None,
) -> str:
    if workpaper_root is None or markdown_dir is None:
        return Path(screenshot_path).as_posix()
    source = (workpaper_root.resolve() / Path(screenshot_path)).resolve()
    return Path(os.path.relpath(source, markdown_dir.resolve())).as_posix()


def query_date_range(queries: list[dict[str, Any]], timezone_name: str) -> str:
    if not queries:
        return "尚未查询"
    timezone = ZoneInfo(timezone_name)
    dates = sorted(
        require_timestamp(item["query_time"], "query.query_time").astimezone(timezone).date()
        for item in queries
    )
    timezone_label = "北京时间" if timezone_name == DEFAULT_TIMEZONE else timezone_name
    if dates[0] == dates[-1]:
        return f"{dates[0].year}年{dates[0].month}月{dates[0].day}日（{timezone_label}）"
    return (
        f"{dates[0].year}年{dates[0].month}月{dates[0].day}日至"
        f"{dates[-1].year}年{dates[-1].month}月{dates[-1].day}日（{timezone_label}）"
    )


def build_opinion_wording(subject: dict[str, Any], queries: list[dict[str, Any]]) -> str:
    source_names = "、".join(dict.fromkeys(item["site_name"] for item in queries))
    matched = [item for item in queries if item["status"] == "identity_match"]
    same_name = [item for item in queries if item["status"] == "same_name_candidates"]
    limited = [item for item in queries if item["status"] in {"access_limited", "failed"}]
    parts = [f"本所通过{source_names or '相关公开网站'}对{subject['name']}进行了公开信息核查。"]
    if matched:
        parts.append("公开页面显示与姓名及关联机构等身份要素相匹配的信息。")
    if same_name:
        parts.append("部分网站存在同名候选结果，现有公开信息不足以确认归属于本案核查对象。")
    if limited:
        parts.append("部分查询因网站功能或访问条件受限，相关事项仍需补充核验。")
    parts.append(
        "前述结果仅反映本次查询条件及网站公开可查询范围，不等同于对不存在任何诉讼、执行、处罚、失信或其他负面事项的绝对确认。"
    )
    return "".join(parts)


def sentence_text(value: Any) -> str:
    text = str(value or "").strip().rstrip("。；; ")
    return f"{text}。" if text else ""


def internal_conclusion_text(query: dict[str, Any], subject_name: str) -> str:
    summary = sentence_text(query.get("result_summary"))
    status = query["status"]

    if status == "identity_match":
        return summary + "经本次公开页面所示身份要素核对，相关结果与本案核查对象相符。"
    if status == "no_match_displayed":
        return (
            summary
            + f"仅就本次查询条件及该网站公开可查询范围，未显示与{subject_name}相符的公开负面记录。"
        )
    if status == "same_name_candidates":
        return summary + "相关同名记录尚需进一步核对，暂不归属于本案核查对象。"
    if status == "not_applicable":
        return summary + "该公开入口不适用于本次核查对象，本次未据此作出否定性结论。"
    if status == "access_limited":
        return summary + "该项查询未完整完成，不作为“未发现负面记录”的依据。"
    if status == "failed":
        return summary + "该项查询未完成，不作为“未发现负面记录”的依据。"
    return summary + "相关结果尚待进一步核对，暂不作结论。"


def build_internal_markdown(
    data: dict[str, Any],
    subject: dict[str, Any],
    queries: list[dict[str, Any]],
    *,
    workpaper_root: Path | None = None,
    markdown_dir: Path | None = None,
) -> str:
    run = data["run"]
    project = run["project"]
    lines: list[str] = [
        f"# {project['name']}",
        "",
        f"## {subject['name']}网络核查底稿",
        "",
        f"**核查日期：** {query_date_range(queries, run['timezone'])}  ",
        f"**核查对象：** {subject['name']}  ",
        f"**主体角色：** {subject['role']}  ",
    ]
    associated = str(subject.get("associated_entity") or "").strip()
    if associated:
        lines.append(f"**关联机构：** {associated}  ")
    lines.extend(
        [
            f"**核查目的：** {project['matter']}  ",
            "**底稿性质：** 公开信息核查底稿，不替代身份要素、主管机关文件、当事人声明及其他书面核验，也不单独构成不存在负面事项的最终结论。",
            "",
            "## 一、核查口径",
            "",
            f"1. 本次核查期间为{project['period']}，实际查询日期见各网站记录。",
            "2. 仅名称或姓名相同的结果列为同名候选，不直接归属于本案核查对象。",
            "3. 使用身份交叉条件时只记录‘本案材料所载身份要素’等非敏感描述，不留存完整身份证号码、出生日期等敏感值。",
            "4. 查询受限、失败或网站不适用的，不作为未发现记录的依据。",
            "5. 网络公开结果可能存在滞后、匿名化、未公开、下架或未收录等限制，应结合其他书面材料综合判断。",
            "",
            "## 二、核查结果总表",
            "",
            "| 序号 | 网站 | 当前查询入口 | 查询结果 | 身份判断 | 结论级别 | 截图编号 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for index, query in enumerate(queries, start=1):
        lines.append(
            "| {index} | {site} | <{url}> | {summary} | {assessment} | {status} | {evidence} |".format(
                index=index,
                site=markdown_cell(query["site_name"]),
                url=query["url"],
                summary=markdown_cell(query["result_summary"]),
                assessment=markdown_cell(query["identity_assessment"]),
                status=STATUS_LABELS[query["status"]],
                evidence=query["evidence_id"],
            )
        )

    lines.extend(["", "## 三、逐站核查记录及截图", ""])
    for index, query in enumerate(queries, start=1):
        lines.extend(
            [
                f"### {index}. {query['site_name']}",
                "",
                f"- 查询入口：<{query['url']}>",
                f"- 查询条件：{query['query_terms']}",
                f"- 筛选条件：{query['filters']}",
                f"- 查询结果：{query['result_summary']}",
                f"- 身份判断：{query['identity_assessment']}",
                f"- 结论级别：{STATUS_LABELS[query['status']]}",
            ]
        )
        screenshot = str(query.get("screenshot_path") or "").strip()
        if screenshot:
            link = markdown_screenshot_link(
                screenshot,
                workpaper_root=workpaper_root,
                markdown_dir=markdown_dir,
            )
            lines.append(f"- [查询截图（{query['evidence_id']}）]({link})")
        else:
            lines.append("- 截图：未取得；原因见查询结果和身份判断。")
        follow_up = str(query.get("follow_up") or "").strip()
        if follow_up:
            lines.append(f"- 后续核验：{follow_up}")
        lines.append("")

    lines.extend(["## 四、初步结论", ""])
    if not queries:
        lines.append("尚未完成公开网站查询。")
    else:
        for index, query in enumerate(queries, start=1):
            lines.append(
                f"{index}. **{query['site_name']}：** "
                f"{internal_conclusion_text(query, subject['name'])}"
            )

    followups = list(
        dict.fromkeys(str(item.get("follow_up") or "").strip() for item in queries if str(item.get("follow_up") or "").strip())
    )
    lines.extend(["", "## 五、建议补核事项", ""])
    if followups:
        for index, item in enumerate(followups, start=1):
            lines.append(f"{index}. {item}")
    else:
        lines.append("无。")

    if data.get("opinion_wording_requested"):
        lines.extend(
            [
                "",
                "## 六、可供成果文件使用的核查表述（待律师复核）",
                "",
                f"> {build_opinion_wording(subject, queries)}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def iter_paragraphs(document: Any):
    for paragraph in document.paragraphs:
        yield paragraph
    for table in document.tables:
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs:
                    yield paragraph


def replace_in_paragraph(paragraph: Any, replacements: dict[str, str]) -> None:
    for run in paragraph.runs:
        for old, new in replacements.items():
            if old in run.text:
                run.text = run.text.replace(old, new)
    combined = paragraph.text
    if not any(old in combined for old in replacements):
        return
    for old, new in replacements.items():
        combined = combined.replace(old, new)
    if paragraph.runs:
        paragraph.runs[0].text = combined
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(combined)


def set_run_font(run: Any, size_pt: float = 12, bold: bool | None = None) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = OUTPUT_DOCX_FONT
    run.font.size = Pt(size_pt)
    if bold is not None:
        run.bold = bold
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), OUTPUT_DOCX_FONT)
    rfonts.set(qn("w:ascii"), OUTPUT_DOCX_FONT)
    rfonts.set(qn("w:hAnsi"), OUTPUT_DOCX_FONT)
    rfonts.set(qn("w:cs"), OUTPUT_DOCX_FONT)


def set_cell_text(cell: Any, value: str, *, bold: bool = False) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT

    cell.text = value
    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for paragraph in cell.paragraphs:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.first_line_indent = None
        paragraph.paragraph_format.left_indent = None
        paragraph.paragraph_format.right_indent = None
        for run in paragraph.runs:
            set_run_font(run, 10.5, bold=bold)


def clear_paragraph_numbering(paragraph: Any) -> None:
    from docx.oxml.ns import qn

    ppr = paragraph._p.get_or_add_pPr()
    numpr = ppr.find(qn("w:numPr"))
    if numpr is not None:
        ppr.remove(numpr)


def concise_formal_query_parts(
    index: int,
    query: dict[str, Any],
    subject_name: str,
) -> tuple[str, str, str]:
    summary = str(query["result_summary"]).rstrip("。；; ")
    assessment = str(query["identity_assessment"]).rstrip("。；; ")
    status = query["status"]

    if status == "identity_match":
        result_text = f"{summary}。经核对，{assessment}。"
    elif status == "no_match_displayed":
        result_text = (
            f"截至查询日，经查询{query['site_name']}，{summary}。"
            f"前述结果仅反映本次查询条件及该网站公开可查询范围，"
            f"未显示与{subject_name}相符的公开负面记录。"
        )
    elif status == "same_name_candidates":
        result_text = (
            f"{summary}。检索结果中存在同名记录，相关记录尚需进一步核对，"
            f"暂不作是否与{subject_name}相关的结论。"
        )
    elif status == "not_applicable":
        result_text = f"{summary}。本项未据此作出是否存在负面记录的结论。"
    elif status in {"access_limited", "failed"}:
        result_text = f"{summary}。本项查询未完整完成，暂不作是否存在负面记录的结论。"
    else:
        result_text = f"{summary}。相关结果尚待进一步核对，暂不作结论。"

    return (
        f"{index}. 就{subject_name}查询{query['site_name']}（",
        str(query["url"]),
        f"），本次以{query['query_terms']}为主要条件，并结合{query['filters']}进行核查。{result_text}",
    )


def concise_formal_query_text(index: int, query: dict[str, Any], subject_name: str) -> str:
    prefix, url, suffix = concise_formal_query_parts(index, query, subject_name)
    return prefix + url + suffix


def breakable_url_display(url: str) -> str:
    display = re.sub(
        r"%[0-9A-Fa-f]{2}",
        lambda match: match.group(0) + ZERO_WIDTH_SPACE,
        url,
    )
    return re.sub(
        r"[/\.?&=]",
        lambda match: match.group(0) + ZERO_WIDTH_SPACE,
        display,
    )


def enable_latin_word_wrap(paragraph: Any) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = paragraph._p.get_or_add_pPr()
    existing = ppr.find(qn("w:wordWrap"))
    if existing is not None:
        existing.set(qn("w:val"), "1")
        return

    word_wrap = OxmlElement("w:wordWrap")
    word_wrap.set(qn("w:val"), "1")
    successors = {
        qn("w:overflowPunct"),
        qn("w:topLinePunct"),
        qn("w:autoSpaceDE"),
        qn("w:autoSpaceDN"),
        qn("w:bidi"),
        qn("w:adjustRightInd"),
        qn("w:snapToGrid"),
        qn("w:spacing"),
        qn("w:ind"),
        qn("w:contextualSpacing"),
        qn("w:mirrorIndents"),
        qn("w:suppressOverlap"),
        qn("w:jc"),
        qn("w:textDirection"),
        qn("w:textAlignment"),
        qn("w:textboxTightWrap"),
        qn("w:outlineLvl"),
        qn("w:divId"),
        qn("w:cnfStyle"),
        qn("w:rPr"),
        qn("w:sectPr"),
        qn("w:pPrChange"),
    }
    for position, child in enumerate(ppr):
        if child.tag in successors:
            ppr.insert(position, word_wrap)
            break
    else:
        ppr.append(word_wrap)


def clear_paragraph_content(paragraph: Any) -> None:
    from docx.oxml.ns import qn

    for child in list(paragraph._p):
        if child.tag != qn("w:pPr"):
            paragraph._p.remove(child)


def append_external_hyperlink(paragraph: Any, url: str, display_text: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    relationship_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relationship_id)

    run = OxmlElement("w:r")
    run_properties = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    fonts.set(qn("w:ascii"), OUTPUT_DOCX_FONT)
    fonts.set(qn("w:hAnsi"), OUTPUT_DOCX_FONT)
    fonts.set(qn("w:eastAsia"), OUTPUT_DOCX_FONT)
    fonts.set(qn("w:cs"), OUTPUT_DOCX_FONT)
    run_properties.append(fonts)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "000000")
    run_properties.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "none")
    run_properties.append(underline)
    size = OxmlElement("w:sz")
    size.set(qn("w:val"), "24")
    run_properties.append(size)
    size_cs = OxmlElement("w:szCs")
    size_cs.set(qn("w:val"), "24")
    run_properties.append(size_cs)
    run.append(run_properties)

    text = OxmlElement("w:t")
    text.text = display_text
    run.append(text)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def build_formal_docx(data: dict[str, Any], template_path: Path, output_path: Path, formal_mode: str) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.text.paragraph import Paragraph

    document = Document(str(template_path))
    run = data["run"]
    project = run["project"]
    formal = require_object(run.get("formal_record", {}), "run.formal_record")

    def formal_value(key: str, placeholder: str) -> str:
        value = formal.get(key)
        if isinstance(value, list):
            text = "、".join(str(item).strip() for item in value if str(item).strip())
        else:
            text = str(value or "").strip()
        return text or (placeholder if formal_mode == "draft" else "")

    replacements = {
        "【项目名称】": project["name"],
        "【项目简称】": project["short_name"],
        "【查验事项】": project["matter"],
        "【核查期间】": project["period"],
        "【查询时间】": formal_value("query_date", "【待填写查询时间】"),
        "【查询地点】": formal_value("query_location", "【待填写查询地点】"),
        "【查询人】": formal_value("query_people", "【待填写查询人】"),
    }
    for paragraph in iter_paragraphs(document):
        replace_in_paragraph(paragraph, replacements)

    marker_row_found = False
    for table in document.tables:
        for row in list(table.rows):
            if "【SUBJECT_ROWS】" not in "".join(cell.text for cell in row.cells):
                continue
            marker_row_found = True
            for subject in data["subjects"]:
                new_row = table.add_row()
                row._tr.addprevious(new_row._tr)
                if len(new_row.cells) < 3:
                    raise ValidationError("DOCX template subject table must contain at least three columns.")
                identifier = str(
                    subject.get("credit_code")
                    or subject.get("masked_id_number")
                    or (
                        "【由用户填写统一社会信用代码】"
                        if subject["type"] == "company"
                        else "【由用户填写脱敏身份证号码】"
                    )
                )
                set_cell_text(new_row.cells[0], subject["role"])
                set_cell_text(new_row.cells[1], subject["name"])
                set_cell_text(new_row.cells[2], identifier)
            table._tbl.remove(row._tr)
    if not marker_row_found:
        raise ValidationError("DOCX template is missing 【SUBJECT_ROWS】 marker row.")

    query_marker = None
    for paragraph in document.paragraphs:
        if "【QUERY_RESULT_ITEMS】" in paragraph.text:
            query_marker = paragraph
            break
    if query_marker is None:
        raise ValidationError("DOCX template is missing 【QUERY_RESULT_ITEMS】 marker paragraph.")

    subject_by_id = {item["subject_id"]: item for item in data["subjects"]}
    for index, query in enumerate(data["queries"], start=1):
        new_xml = copy.deepcopy(query_marker._p)
        query_marker._p.addprevious(new_xml)
        paragraph = Paragraph(new_xml, query_marker._parent)
        clear_paragraph_numbering(paragraph)
        prefix, url, suffix = concise_formal_query_parts(
            index,
            query,
            subject_by_id[query["subject_id"]]["name"],
        )
        clear_paragraph_content(paragraph)
        prefix_run = paragraph.add_run(prefix)
        set_run_font(prefix_run, 12)
        append_external_hyperlink(paragraph, url, breakable_url_display(url))
        suffix_run = paragraph.add_run(suffix)
        set_run_font(suffix_run, 12)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph.paragraph_format.first_line_indent = None
        paragraph.paragraph_format.left_indent = None
        paragraph.paragraph_format.right_indent = None
        paragraph.paragraph_format.line_spacing = 1.5
        enable_latin_word_wrap(paragraph)
    query_marker._element.getparent().remove(query_marker._element)

    document.core_properties.author = ""
    document.core_properties.last_modified_by = ""
    document.core_properties.title = f"网络查询记录-{project['short_name']}"
    document.core_properties.subject = ""
    document.core_properties.keywords = ""
    document.core_properties.comments = ""
    document.core_properties.category = ""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output_path))


def resolve_template_path(requested: Path | None, formal_mode: str) -> Path | None:
    if formal_mode == "none":
        return None
    if requested is not None:
        if not requested.is_file():
            raise FileNotFoundError(f"Requested DOCX template not found: {requested}")
        template_check(requested)
        return requested
    if BUNDLED_TEMPLATE.is_file():
        template_check(BUNDLED_TEMPLATE)
        return BUNDLED_TEMPLATE
    if formal_mode == "final":
        raise FileNotFoundError(
            "No readable DOCX template is available; final mode cannot continue."
        )
    print(
        "Warning: no readable DOCX template is available; generated Markdown only.",
        file=sys.stderr,
    )
    return None


def build_outputs(
    data: dict[str, Any],
    *,
    workpaper_root: Path,
    output_dir: Path,
    template_docx: Path | None,
    formal_mode: str,
    layout: str,
    overwrite: bool,
) -> list[Path]:
    root = workpaper_root.resolve()
    resolved_output_dir = output_dir.resolve()
    try:
        resolved_output_dir.relative_to(root)
    except ValueError as exc:
        raise ValidationError("output_dir must be inside workpaper_root.") from exc
    if layout not in {"two-layer", "flat"}:
        raise ValidationError("layout must be two-layer or flat.")
    validate_run(data, workpaper_root=workpaper_root, formal_mode=formal_mode)
    resolved_template = resolve_template_path(template_docx, formal_mode)
    formal_enabled = formal_mode != "none" and resolved_template is not None

    queries = data["queries"]
    date_token = "未查询"
    if queries:
        latest = max(require_timestamp(item["query_time"], "query.query_time") for item in queries)
        date_token = latest.astimezone(ZoneInfo(data["run"]["timezone"])).strftime("%Y%m%d")
    name_counts: dict[str, int] = {}
    for subject in data["subjects"]:
        name_counts[subject["name"]] = name_counts.get(subject["name"], 0) + 1

    internal_output_dir = output_dir / "01-内部底稿" if layout == "two-layer" else output_dir
    formal_output_dir = output_dir / "02-正式记录" if layout == "two-layer" else output_dir
    final_paths: list[Path] = []
    subject_queries: dict[str, list[dict[str, Any]]] = {item["subject_id"]: [] for item in data["subjects"]}
    for query in queries:
        subject_queries[query["subject_id"]].append(query)
    for subject in data["subjects"]:
        suffix = f"-{subject['subject_id']}" if name_counts[subject["name"]] > 1 else ""
        filename = f"{safe_filename(subject['name'])}网络核查底稿-{date_token}{suffix}.md"
        final_paths.append(internal_output_dir / filename)
    if formal_enabled:
        project_name = safe_filename(data["run"]["project"]["short_name"])
        draft_suffix = "_草稿" if formal_mode == "draft" else ""
        final_paths.append(
            formal_output_dir / f"网络查询记录-{project_name}-{date_token}{draft_suffix}.docx"
        )

    internal_output_dir.mkdir(parents=True, exist_ok=True)
    if formal_enabled:
        formal_output_dir.mkdir(parents=True, exist_ok=True)
    safe_destinations = [
        safe_output_destination(path, root, "output file") for path in final_paths
    ]
    existing = [path for path in safe_destinations if path.exists()]
    if existing and not overwrite:
        raise FileExistsError(
            "Output file(s) already exist: " + ", ".join(str(path) for path in existing)
        )
    with tempfile.TemporaryDirectory(prefix="network-workpaper-build-") as temp_text:
        temp_dir = Path(temp_text)
        staged: list[tuple[Path, Path]] = []
        path_index = 0
        for subject in data["subjects"]:
            destination = final_paths[path_index]
            path_index += 1
            staged_path = temp_dir / destination.name
            staged_path.write_text(
                build_internal_markdown(
                    data,
                    subject,
                    subject_queries[subject["subject_id"]],
                    workpaper_root=workpaper_root,
                    markdown_dir=internal_output_dir,
                ),
                encoding="utf-8",
            )
            staged.append((staged_path, destination))
        if formal_enabled:
            destination = final_paths[path_index]
            staged_path = temp_dir / destination.name
            assert resolved_template is not None
            build_formal_docx(data, resolved_template, staged_path, formal_mode)
            staged.append((staged_path, destination))
        for (staged_path, _), destination in zip(staged, safe_destinations):
            atomic_copy_file(staged_path, destination)
    return final_paths


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare, validate, and build legal network workpapers.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Check portable runtime capabilities.")
    doctor.add_argument(
        "--browser",
        choices=("available", "unavailable", "unknown"),
        default="unknown",
        help="Browser capability confirmed by the host Agent or user.",
    )

    prepare = subparsers.add_parser("prepare", help="Create a run skeleton without network access.")
    prepare.add_argument("input_file", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--overwrite", action="store_true")

    validate = subparsers.add_parser("validate", help="Validate a completed run file.")
    validate.add_argument("run_file", type=Path)
    validate.add_argument("--workpaper-root", type=Path, default=None)
    validate.add_argument("--formal-mode", choices=("none", "draft", "final"), default="draft")

    template = subparsers.add_parser(
        "template-check", help="Check DOCX markers, page field and privacy safety."
    )
    template.add_argument("template_docx", type=Path)

    audit = subparsers.add_parser(
        "artifact-audit", help="Scan generated artifacts for sensitive values and local paths."
    )
    audit.add_argument("artifact_path", type=Path)

    build = subparsers.add_parser("build", help="Generate internal Markdown and optional formal DOCX.")
    build.add_argument("run_file", type=Path)
    build.add_argument("--workpaper-root", type=Path, required=True)
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--template-docx", type=Path, default=None)
    build.add_argument("--formal-mode", choices=("none", "draft", "final"), default="draft")
    build.add_argument("--layout", choices=("two-layer", "flat"), default="two-layer")
    build.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "doctor":
            report = doctor_report(args.browser)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["python"]["ok"] or not all(report["dependencies"].values()):
                return 2
        elif args.command == "prepare":
            prepared = prepare_run(load_json(args.input_file))
            write_json(args.output, prepared, overwrite=args.overwrite)
            print(args.output)
        elif args.command == "validate":
            validate_run(
                load_json(args.run_file),
                workpaper_root=args.workpaper_root,
                formal_mode=args.formal_mode,
            )
            print("Network verification run is valid.")
        elif args.command == "template-check":
            print(json.dumps(template_check(args.template_docx), ensure_ascii=False, indent=2))
        elif args.command == "artifact-audit":
            report = artifact_audit(args.artifact_path)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not report["ok"]:
                return 2
        else:
            paths = build_outputs(
                load_json(args.run_file),
                workpaper_root=args.workpaper_root,
                output_dir=args.output_dir,
                template_docx=args.template_docx,
                formal_mode=args.formal_mode,
                layout=args.layout,
                overwrite=args.overwrite,
            )
            for path in paths:
                print(path)
    except (FileExistsError, FileNotFoundError, OSError, ValidationError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
