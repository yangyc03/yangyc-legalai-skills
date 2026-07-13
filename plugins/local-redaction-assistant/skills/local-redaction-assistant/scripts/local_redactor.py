#!/usr/bin/env python3
"""Local redaction assistant v1.0 deterministic redaction engine.

This tool scans file metadata, runs local regex detectors on synthetic .txt
test files, DOCX main-body text, and XLSX visible-cell text, then writes safe
reports. In redact mode it only creates DOCX redacted copies for ordinary
word/document.xml w:t nodes. In visual-redact-pdf mode it creates image-based
PDF copies from user-supplied local region coordinates. In pdf-preview-grid
mode it renders local grid PNG previews to help users manually choose regions.
In grid-regions-to-json mode it converts user-supplied grid cell ranges into a
local pdf_redaction_regions.local.json file without reading PDF or preview
contents. In ocr-precheck mode it uses the local Tesseract installation to
inspect user-selected PDF or image copies and emits masked findings plus local
candidate regions for manual confirmation.
It does not use cloud OCR, automatically create redacted files from OCR
findings, use PDF true redaction, or redact XLSX.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from docx_redaction_profile import (
    AI_SHARE_PROFILE,
    LEGAL_TEMPLATE_PROFILE,
    REDACTION_PROFILES,
    apply_byte_patches,
    extract_profile_candidates,
    extract_safe_docx_text_slices,
    sanitise_docx_metadata_part,
    validate_docx_ooxml_package,
    xml_escape_text,
)
from local_region_selector import RegionSelectorError, run_region_selector
from word_native_validator import WORD_VALIDATION_MODES, validate_docx_with_word

VERSION = "1.1.1"
SUPPORTED_EXTENSIONS = {".docx", ".xlsx", ".pdf", ".png", ".jpg", ".jpeg", ".txt"}
TEXT_DETECTOR_EXTENSIONS = {".txt"}
DOCX_DETECTOR_EXTENSIONS = {".docx"}
XLSX_DETECTOR_EXTENSIONS = {".xlsx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
OCR_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
LOCAL_TERMS_FILENAME = "redaction_terms.local.json"
OCR_CANDIDATES_FILENAME = "ocr_candidate_regions.local.json"
PDF_REGIONS_FILENAME = "pdf_redaction_regions.local.json"
IMAGE_REGIONS_FILENAME = "image_redaction_regions.local.json"
DOCX_CANDIDATES_FILENAME = "docx_candidate_terms.local.json"
OUTPUT_DIR_NAMES = {"_redaction_output", "redacted_files", "reports", "logs", "previews"}
OUTPUT_MARKER = ".local_redaction_assistant_output"
OUTPUT_STRUCTURE_DIRS = {"redacted_files", "reports", "logs"}
DETECTOR_NAMES = (
    "phone",
    "id_card",
    "email",
    "unified_social_credit_code",
    "bank_account",
    "license_plate",
    "url",
    "contract_number",
    "case_number",
    "custom_terms",
)
CUSTOM_TERM_KEYS = (
    "companies",
    "company_aliases",
    "persons",
    "projects",
    "clients",
    "counterparties",
    "institutions",
    "meeting_locations",
    "other_locations",
    "template_years",
    "template_dates",
    "headcounts",
    "share_counts",
    "vote_counts",
    "ownership_ratios",
    "service_fees",
    "other_amounts",
    "service_terms",
    "fund_manager_registration_numbers",
    "registration_numbers",
    "lawyer_phones",
    "phones",
    "emails",
    "identity_numbers",
    "addresses",
    "other_terms",
)
CUSTOM_TERM_LABELS = {
    "companies": "公司",
    "company_aliases": "公司",
    "persons": "姓名",
    "projects": "项目",
    "clients": "公司",
    "counterparties": "公司",
    "institutions": "公司",
    "meeting_locations": "会议地点",
    "other_locations": "地点",
    "template_years": "年份",
    "template_dates": "日期",
    "headcounts": "人数",
    "share_counts": "股份数",
    "vote_counts": "票数",
    "ownership_ratios": "比例",
    "service_fees": "服务费",
    "other_amounts": "金额",
    "service_terms": "期限",
    "fund_manager_registration_numbers": "登记编号",
    "registration_numbers": "登记编号",
    "lawyer_phones": "手机号",
    "addresses": "地址",
    "other_terms": "自定义",
}
REGEX_PATTERNS = {
    "phone": re.compile(r"(?<!\d)(?:\+?86[- ]?)?(1[3-9]\d{9})(?!\d)"),
    "id_card": re.compile(
        r"(?<![0-9Xx])"
        r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])"
        r"(?:0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]"
        r"(?![0-9Xx])"
    ),
    "email": re.compile(
        r"(?<![A-Za-z0-9._%+-])"
        r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        r"(?![A-Za-z0-9._%+-])"
    ),
    "unified_social_credit_code": re.compile(
        r"(?<![0-9A-Z])(?=[0-9A-Z]{18}(?![0-9A-Z]))"
        r"(?=[0-9A-Z]*[A-Z])[0-9A-Z]{18}(?![0-9A-Z])"
    ),
    "bank_account": re.compile(r"(?<!\d)\d{16,30}(?!\d)"),
    "license_plate": re.compile(
        r"(?<![\u4e00-\u9fa5A-Z0-9])"
        r"[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-Z][A-Z0-9]{5,6}"
        r"(?![A-Z0-9])"
    ),
    "url": re.compile(r"(?:https?://|www\.)[^\s<>'\"，。；、（）()]+", re.IGNORECASE),
    "contract_number": re.compile(
        r"\b(?:HT|CONTRACT|AGREEMENT)[-_./][A-Z0-9][A-Z0-9._/-]{2,}\b",
        re.IGNORECASE,
    ),
    "case_number": re.compile(r"[（(]\d{4}[）)][\u4e00-\u9fa5A-Za-z0-9]{2,40}号"),
}
GRID_CELL_PATTERN = re.compile(r"^([A-Za-z]{1,2})([1-9][0-9]*)$")
GRID_REGION_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
OCR_LANGUAGE_PATTERN = re.compile(r"^[A-Za-z0-9_+.-]+$")
DETECTOR_PRIORITY = (
    "email",
    "id_card",
    "unified_social_credit_code",
    "bank_account",
    "phone",
    "license_plate",
    "url",
    "case_number",
    "contract_number",
)
DOCX_COMPLEX_PART_MARKERS = (
    ("word/header", "docx_headers_not_checked"),
    ("word/footer", "docx_footers_not_checked"),
    ("word/comments.xml", "docx_comments_not_checked"),
    ("word/footnotes.xml", "docx_footnotes_not_checked"),
    ("word/endnotes.xml", "docx_endnotes_not_checked"),
    ("word/media/", "docx_embedded_images_may_exist"),
)
DOCX_TEXT_EXCLUDED_ANCESTORS = {
    "txbxContent",
    "del",
    "ins",
    "moveFrom",
    "moveTo",
    "fldSimple",
    "instrText",
    "drawing",
    "object",
    "pict",
    "hyperlink",
}
DOCX_REVIEW_REASON_BY_TAG = {
    "txbxContent": "docx_text_boxes_may_exist",
    "del": "docx_revisions_may_exist",
    "ins": "docx_revisions_may_exist",
    "moveFrom": "docx_revisions_may_exist",
    "moveTo": "docx_revisions_may_exist",
    "fldSimple": "docx_field_codes_may_exist",
    "instrText": "docx_field_codes_may_exist",
    "drawing": "docx_drawings_or_objects_may_exist",
    "object": "docx_drawings_or_objects_may_exist",
    "pict": "docx_drawings_or_objects_may_exist",
    "hyperlink": "docx_external_links_may_exist",
}
XLSX_OFFICE_REL_NS_URI = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_PACKAGE_REL_NS_URI = "http://schemas.openxmlformats.org/package/2006/relationships"
XLSX_MAIN_NS = {
    "x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": XLSX_OFFICE_REL_NS_URI,
    "rel": XLSX_PACKAGE_REL_NS_URI,
}
XLSX_COMPLEX_PART_MARKERS = (
    ("xl/comments", "xlsx_comments_not_checked"),
    ("xl/threadedComments", "xlsx_comments_not_checked"),
    ("xl/drawings/", "xlsx_drawings_or_images_may_exist"),
    ("xl/media/", "xlsx_drawings_or_images_may_exist"),
    ("xl/externalLinks/", "xlsx_external_links_may_exist"),
    ("xl/vbaProject.bin", "xlsx_macros_may_exist"),
)
XLSX_REVIEW_REASON_BY_TAG = {
    "f": "xlsx_formulas_not_read",
    "hyperlink": "xlsx_hyperlinks_not_read",
    "dataValidation": "xlsx_data_validations_may_exist",
    "dataValidations": "xlsx_data_validations_may_exist",
    "drawing": "xlsx_drawings_or_images_may_exist",
    "picture": "xlsx_drawings_or_images_may_exist",
    "oleObject": "xlsx_drawings_or_images_may_exist",
    "oleObjects": "xlsx_drawings_or_images_may_exist",
    "legacyDrawing": "xlsx_drawings_or_images_may_exist",
    "externalReference": "xlsx_external_links_may_exist",
    "externalReferences": "xlsx_external_links_may_exist",
}
PLACEHOLDER_LABELS = {
    "phone": "手机号",
    "id_card": "身份证号",
    "email": "邮箱",
    "unified_social_credit_code": "统一社会信用代码",
    "bank_account": "银行账号",
    "license_plate": "车牌号",
    "url": "URL",
    "contract_number": "合同编号",
    "case_number": "案件编号",
    "custom_terms.companies": "公司",
    "custom_terms.company_aliases": "公司",
    "custom_terms.persons": "姓名",
    "custom_terms.projects": "项目",
    "custom_terms.clients": "公司",
    "custom_terms.counterparties": "公司",
    "custom_terms.institutions": "机构",
    "custom_terms.meeting_locations": "会议地点",
    "custom_terms.template_years": "年份",
    "custom_terms.headcounts": "人数",
    "custom_terms.share_counts": "股份数",
    "custom_terms.ownership_ratios": "比例",
    "custom_terms.service_fees": "服务费",
    "custom_terms.fund_manager_registration_numbers": "登记编号",
    "custom_terms.lawyer_phones": "手机号",
    "custom_terms.phones": "电话",
    "custom_terms.emails": "邮箱",
    "custom_terms.identity_numbers": "证件号码",
    "custom_terms.addresses": "地址",
    "custom_terms.other_terms": "自定义",
}


def find_repo_root(start: Path) -> Path | None:
    for path in (start, *start.parents):
        if (path / ".git").exists():
            return path
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Local-only redaction assistant. Generates safe reports and, only in "
            "redact mode, DOCX redacted copies without writing raw sensitive "
            "values to reports."
        )
    )
    parser.add_argument("--input", required=True, help="Input file or directory.")
    parser.add_argument("--output", required=True, help="Output directory.")
    parser.add_argument("--config", help="Optional redaction config path.")
    parser.add_argument(
        "--redaction-profile",
        choices=REDACTION_PROFILES,
        default=AI_SHARE_PROFILE,
        help=(
            "DOCX redaction profile. ai-share keeps the ordinary AI-sharing detectors; "
            "legal-template generates legal-template candidates and only applies terms "
            "confirmed by the lawyer. Default: ai-share."
        ),
    )
    parser.add_argument(
        "--word-validation",
        choices=WORD_VALIDATION_MODES,
        default=None,
        help=(
            "Microsoft Word native validation. Defaults to required for legal-template "
            "and off for ai-share. LibreOffice never satisfies a required gate."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=(
            "dry-run",
            "redact",
            "docx-candidate-precheck",
            "visual-redact-pdf",
            "visual-redact-image",
            "region-selector",
            "pdf-preview-grid",
            "grid-regions-to-json",
            "ocr-precheck",
        ),
        default="dry-run",
        help=(
            "dry-run only reports; redact creates DOCX redacted copies only; "
            "docx-candidate-precheck writes local-only candidate dictionary terms from "
            "ordinary DOCX body context without auto-redacting; "
            "visual-redact-pdf creates image-based PDF visual redaction copies "
            "from local region config; pdf-preview-grid creates local grid PNG "
            "previews only; grid-regions-to-json converts manual grid ranges "
            "to local PDF regions JSON; ocr-precheck runs local Tesseract on "
            "selected PDF/image copies and writes masked findings plus candidate regions; "
            "region-selector opens a loopback-only browser selector and saves manual regions; "
            "visual-redact-image creates PNG visual redaction copies from local image regions."
        ),
    )
    parser.add_argument(
        "--terms-file",
        help=(
            "Optional local-sensitive JSON file containing custom_terms for names, "
            "companies, addresses, and other explicit dictionary values."
        ),
    )
    parser.add_argument(
        "--docx-candidates-output",
        help=(
            "Local-sensitive candidate dictionary output for docx-candidate-precheck. "
            "Default: OUTPUT/docx_candidate_terms.local.json."
        ),
    )
    parser.add_argument(
        "--overwrite-docx-candidates",
        action="store_true",
        help="Allow overwriting an existing docx_candidate_terms.local.json.",
    )
    parser.add_argument(
        "--ocr-langs",
        default="chi_sim+eng",
        help="Local Tesseract language set for ocr-precheck. Default: chi_sim+eng.",
    )
    parser.add_argument(
        "--ocr-dpi",
        type=int,
        default=200,
        help="PDF page render DPI for ocr-precheck. Default: 200.",
    )
    parser.add_argument(
        "--ocr-min-confidence",
        type=float,
        default=60.0,
        help="Minimum Tesseract word confidence used for detector input. Default: 60.",
    )
    parser.add_argument(
        "--ocr-psm",
        type=int,
        default=3,
        help="Tesseract page segmentation mode for ocr-precheck. Default: 3.",
    )
    parser.add_argument(
        "--ocr-max-pages",
        type=int,
        default=50,
        help="Maximum PDF pages processed per file in ocr-precheck. Default: 50.",
    )
    parser.add_argument(
        "--ocr-timeout-seconds",
        type=int,
        default=120,
        help="Per-page local Tesseract timeout in seconds. Default: 120.",
    )
    parser.add_argument(
        "--ocr-candidates-output",
        help=(
            "Local-sensitive OCR candidate regions output. Default: "
            "OUTPUT/ocr_candidate_regions.local.json."
        ),
    )
    parser.add_argument(
        "--pdf-regions",
        help="Local pdf_redaction_regions.local.json path for visual-redact-pdf mode.",
    )
    parser.add_argument(
        "--image-regions",
        help="Local image_redaction_regions.local.json path for visual-redact-image mode.",
    )
    parser.add_argument(
        "--selector-port",
        type=int,
        default=0,
        help="Loopback-only region selector port. Default: a random available port.",
    )
    parser.add_argument(
        "--selector-timeout-seconds",
        type=int,
        default=1800,
        help="Local region selector timeout in seconds. Default: 1800.",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not open the default browser for region-selector mode.",
    )
    parser.add_argument(
        "--confirm-ocr-candidates",
        action="store_true",
        help=(
            "Confirm that OCR candidate regions were manually reviewed before "
            "visual-redact-pdf uses them."
        ),
    )
    parser.add_argument(
        "--render-dpi",
        type=int,
        help=(
            "PDF render DPI for visual-redact-pdf mode. Default: 200. In "
            "pdf-preview-grid mode, this is used only when --preview-dpi is not set."
        ),
    )
    parser.add_argument(
        "--preview-dpi",
        type=int,
        help="PDF preview-grid render DPI. Default: 120.",
    )
    parser.add_argument(
        "--grid-cols",
        "--grid-columns",
        dest="grid_cols",
        type=int,
        default=12,
        help="PDF preview grid columns. Default: 12.",
    )
    parser.add_argument(
        "--grid-rows",
        type=int,
        default=18,
        help="PDF preview grid rows. Default: 18.",
    )
    parser.add_argument(
        "--grid-region",
        action="append",
        default=[],
        help=(
            "Manual grid region in FILE_ID:PAGE:START_CELL:END_CELL:LABEL format. "
            "May be provided multiple times in grid-regions-to-json mode."
        ),
    )
    parser.add_argument(
        "--regions-template",
        help=(
            "Local pdf_redaction_regions.local.template.json path for "
            "grid-regions-to-json mode. Default: OUTPUT/pdf_redaction_regions.local.template.json."
        ),
    )
    parser.add_argument(
        "--regions-output",
        help=(
            "Local pdf_redaction_regions.local.json output path for "
            "grid-regions-to-json mode. Default: OUTPUT/pdf_redaction_regions.local.json."
        ),
    )
    parser.add_argument(
        "--overwrite-regions",
        action="store_true",
        help="Allow overwriting an existing pdf_redaction_regions.local.json in grid-regions-to-json mode.",
    )
    parser.add_argument(
        "--redaction-padding-ratio",
        type=float,
        default=0.004,
        help="Padding ratio for PDF visual redaction rectangles. Default: 0.004.",
    )
    parser.add_argument(
        "--save-map",
        action="store_true",
        help="Generate sensitive_mapping.local.json in the output root.",
    )
    parser.add_argument("--jobs", type=int, default=1, help="Worker count. Default: 1.")
    parser.add_argument(
        "--max-file-size-mb",
        type=int,
        default=200,
        help="Skip files larger than this size and mark for review.",
    )
    parser.add_argument(
        "--allow-existing-output",
        action="store_true",
        help="Allow reusing an existing local-redaction-assistant output directory.",
    )
    return parser.parse_args()


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def has_tool_output_structure(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    if (path / OUTPUT_MARKER).exists():
        return True
    return all((path / dirname).is_dir() for dirname in OUTPUT_STRUCTURE_DIRS)


def is_empty_dir(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    return False


def strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = None
            elif quote is None:
                quote = char
            continue
        if char == "#" and quote is None:
            return line[:index]
    return line


def parse_scalar(value: str) -> str | bool | list[str]:
    value = value.strip()
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value == "[]":
        return []
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_text_value(part.strip()) for part in inner.split(",") if part.strip()]
    return parse_text_value(value)


def parse_text_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_detector_config(config_path: Path | None) -> dict:
    enabled = {name: True for name in DETECTOR_NAMES}
    custom_terms = {key: [] for key in CUSTOM_TERM_KEYS}
    result = {
        "provided": bool(config_path),
        "parsed": False,
        "parser": "built_in_minimal_yaml_subset",
        "enabled_detectors": enabled,
        "custom_terms": custom_terms,
        "parse_warnings": [],
    }
    if config_path is None:
        result["parsed"] = True
        return result

    section: str | None = None
    custom_key: str | None = None
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        result["parse_warnings"].append(f"config_read_failed:{type(exc).__name__}")
        return result

    for raw_line in lines:
        line = strip_yaml_comment(raw_line).rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        if indent == 0:
            custom_key = None
            if stripped.endswith(":"):
                section = stripped[:-1].strip()
                continue
            if ":" in stripped:
                section = None
                continue

        if section == "enabled_detectors" and indent >= 2 and ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            parsed = parse_scalar(value)
            if key in enabled and isinstance(parsed, bool):
                enabled[key] = parsed
            continue

        if section == "custom_terms":
            if indent >= 2 and not stripped.startswith("-") and ":" in stripped:
                key, value = stripped.split(":", 1)
                key = key.strip()
                custom_key = key if key in custom_terms else None
                if custom_key is None:
                    continue
                parsed = parse_scalar(value)
                if isinstance(parsed, list):
                    custom_terms[custom_key] = normalize_terms(parsed)
                elif isinstance(parsed, str) and parsed:
                    custom_terms[custom_key] = normalize_terms([parsed])
                continue
            if indent >= 4 and stripped.startswith("-") and custom_key:
                term = parse_text_value(stripped[1:].strip())
                if term:
                    custom_terms[custom_key] = normalize_terms(custom_terms[custom_key] + [term])

    result["parsed"] = True
    return result


def normalize_terms(terms: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for term in terms:
        value = str(term).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def merge_terms_file(detector_config: dict, terms_path: Path | None) -> dict:
    """Merge an explicit local-sensitive JSON dictionary without reporting values."""
    detector_config["terms_file_provided"] = bool(terms_path)
    detector_config["terms_file_parsed"] = False
    if terms_path is None:
        detector_config["terms_file_parsed"] = True
        return detector_config
    if terms_path.name != LOCAL_TERMS_FILENAME:
        raise SystemExit(f"--terms-file must be named {LOCAL_TERMS_FILENAME}")
    if not terms_path.exists() or not terms_path.is_file():
        raise SystemExit("--terms-file does not exist or is not a file")
    try:
        payload = json.loads(terms_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("--terms-file could not be parsed as UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--terms-file must contain a JSON object")
    supplied = payload.get("custom_terms", payload)
    if not isinstance(supplied, dict):
        raise SystemExit("--terms-file custom_terms must be a JSON object")

    for key, values in supplied.items():
        if key not in CUSTOM_TERM_KEYS:
            continue
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise SystemExit(f"--terms-file field {key} must be a list of strings")
        detector_config["custom_terms"][key] = normalize_terms(
            detector_config["custom_terms"][key] + values
        )
    detector_config["terms_file_parsed"] = True
    return detector_config


def apply_redaction_profile(detector_config: dict, redaction_profile: str) -> dict:
    """Keep legal-template replacement limited to locally confirmed custom terms."""
    detector_config["redaction_profile"] = redaction_profile
    if redaction_profile == LEGAL_TEMPLATE_PROFILE:
        for detector_name in DETECTOR_PRIORITY:
            detector_config["enabled_detectors"][detector_name] = False
        detector_config["enabled_detectors"]["custom_terms"] = True
    return detector_config


def validate_custom_terms(detector_config: dict) -> None:
    for values in detector_config["custom_terms"].values():
        if any(len(value.strip()) < 2 for value in values):
            raise SystemExit("custom terms must contain at least 2 characters")


def validate_output_path(input_path: Path, output_path: Path, allow_existing: bool) -> None:
    input_resolved = input_path.resolve()
    output_resolved = output_path.resolve(strict=False)

    if output_resolved == input_resolved:
        raise SystemExit("--output must not equal --input")

    if input_path.is_dir() and is_within(output_resolved, input_resolved):
        if output_resolved.name != "_redaction_output":
            raise SystemExit('--output inside --input must be named "_redaction_output"')

    repo_root = find_repo_root(Path(__file__).resolve())
    if repo_root is not None:
        forbidden_dirs = [
            (".git/", repo_root / ".git"),
            ("skills/", repo_root / "skills"),
            ("skills/local-redaction-assistant/", repo_root / "skills" / "local-redaction-assistant"),
            (
                "skills/local-redaction-assistant/scripts/",
                repo_root / "skills" / "local-redaction-assistant" / "scripts",
            ),
            (
                "skills/local-redaction-assistant/configs/",
                repo_root / "skills" / "local-redaction-assistant" / "configs",
            ),
            (
                "skills/local-redaction-assistant/tests/",
                repo_root / "skills" / "local-redaction-assistant" / "tests",
            ),
        ]
        for label, forbidden in forbidden_dirs:
            forbidden_resolved = forbidden.resolve(strict=False)
            if output_resolved == forbidden_resolved or is_within(output_resolved, forbidden_resolved):
                raise SystemExit(f"--output must not be inside repository asset directory: {label}")

    if output_path.exists():
        if not output_path.is_dir():
            raise SystemExit("--output exists and is not a directory")
        if is_empty_dir(output_path):
            return
        if not has_tool_output_structure(output_path):
            raise SystemExit(
                "--output already exists and is not recognized as local-redaction-assistant output; "
                "choose a new directory"
            )
        if not allow_existing:
            raise SystemExit(
                "--output already contains local-redaction-assistant output; "
                "pass --allow-existing-output only if you intend to reuse it"
            )


def validate_ocr_candidates_path(output_path: Path, candidates_path: Path) -> None:
    if candidates_path.name != OCR_CANDIDATES_FILENAME:
        raise SystemExit(f"--ocr-candidates-output must be named {OCR_CANDIDATES_FILENAME}")
    output_resolved = output_path.resolve(strict=False)
    candidates_resolved = candidates_path.resolve(strict=False)
    if not is_within(candidates_resolved, output_resolved):
        raise SystemExit("--ocr-candidates-output must be inside --output")
    reports_resolved = (output_resolved / "reports").resolve(strict=False)
    if candidates_resolved == reports_resolved or is_within(candidates_resolved, reports_resolved):
        raise SystemExit("--ocr-candidates-output must not be inside reports/")


def validate_region_selector_input(input_path: Path) -> None:
    if not input_path.is_file() or input_path.is_symlink():
        raise SystemExit("region-selector requires one regular PDF or image input file")
    if input_path.suffix.lower() not in {".pdf", *IMAGE_EXTENSIONS}:
        raise SystemExit("region-selector supports only one PDF, PNG, JPG, or JPEG input file")


def validate_selector_regions_path(output_path: Path, regions_path: Path, expected_filename: str) -> None:
    if regions_path.name != expected_filename:
        raise SystemExit(f"region-selector output must be named {expected_filename}")
    output_resolved = output_path.resolve(strict=False)
    regions_resolved = regions_path.resolve(strict=False)
    if not is_within(regions_resolved, output_resolved):
        raise SystemExit("region-selector regions output must be inside --output")
    reports_resolved = (output_resolved / "reports").resolve(strict=False)
    if regions_resolved == reports_resolved or is_within(regions_resolved, reports_resolved):
        raise SystemExit("region-selector regions output must not be inside reports/")


def validate_visual_image_input(input_path: Path) -> None:
    if not input_path.is_file() or input_path.is_symlink() or input_path.suffix.lower() not in IMAGE_EXTENSIONS:
        raise SystemExit("visual-redact-image requires one regular PNG, JPG, or JPEG input file")


def validate_visual_pdf_input(input_path: Path, mode: str) -> None:
    if not input_path.is_file() or input_path.is_symlink() or input_path.suffix.lower() != ".pdf":
        raise SystemExit(f"{mode} requires one regular PDF input file")


def validate_ocr_precheck_input(input_path: Path) -> None:
    if not input_path.is_file() or input_path.is_symlink() or input_path.suffix.lower() not in OCR_EXTENSIONS:
        raise SystemExit("ocr-precheck requires one regular PDF, PNG, JPG, or JPEG input file")


def validate_docx_candidate_input(input_path: Path) -> None:
    if not input_path.is_file() or input_path.is_symlink() or input_path.suffix.lower() != ".docx":
        raise SystemExit("docx-candidate-precheck requires one regular DOCX input file")


def validate_docx_candidates_path(output_path: Path, candidates_path: Path) -> None:
    if candidates_path.name != DOCX_CANDIDATES_FILENAME:
        raise SystemExit(f"--docx-candidates-output must be named {DOCX_CANDIDATES_FILENAME}")
    output_resolved = output_path.resolve(strict=False)
    candidates_resolved = candidates_path.resolve(strict=False)
    if not is_within(candidates_resolved, output_resolved):
        raise SystemExit("--docx-candidates-output must be inside --output")
    reports_resolved = (output_resolved / "reports").resolve(strict=False)
    if candidates_resolved == reports_resolved or is_within(candidates_resolved, reports_resolved):
        raise SystemExit("--docx-candidates-output must not be inside reports/")


def has_hidden_relative_part(path: Path, base: Path) -> bool:
    try:
        relative = path.relative_to(base)
    except ValueError:
        relative = Path(path.name)
    return any(part.startswith(".") for part in relative.parts)


def scan_input_files(input_path: Path, output_path: Path) -> tuple[list[Path], dict[str, int]]:
    output_resolved = output_path.resolve()
    stats = {
        "symlink_files_skipped": 0,
        "symlink_dirs_skipped": 0,
    }
    files: list[Path] = []

    if input_path.is_symlink():
        if input_path.is_dir():
            stats["symlink_dirs_skipped"] += 1
        else:
            stats["symlink_files_skipped"] += 1
        return files, stats

    if input_path.is_file():
        if input_path.name.startswith("."):
            return files, stats
        files.append(input_path)
        return files, stats

    for root, dirs, filenames in os.walk(input_path):
        root_path = Path(root)
        kept_dirs = []
        for dirname in dirs:
            dir_path = root_path / dirname
            if dir_path.is_symlink():
                stats["symlink_dirs_skipped"] += 1
                continue
            if dirname.startswith(".") or dirname in OUTPUT_DIR_NAMES:
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs

        if has_hidden_relative_part(root_path, input_path):
            continue

        for filename in filenames:
            if filename.startswith("."):
                continue
            candidate = root_path / filename
            if candidate.is_symlink():
                stats["symlink_files_skipped"] += 1
                continue
            try:
                if is_within(candidate.resolve(), output_resolved):
                    continue
            except FileNotFoundError:
                continue
            files.append(candidate)

    return files, stats


def scan_pdf_preview_input_files(input_path: Path, output_path: Path) -> tuple[list[Path], dict[str, int]]:
    output_resolved = output_path.resolve(strict=False)
    stats = {
        "symlink_files_skipped": 0,
        "symlink_dirs_skipped": 0,
    }
    files: list[Path] = []

    if input_path.is_symlink():
        if input_path.is_dir():
            stats["symlink_dirs_skipped"] += 1
        else:
            stats["symlink_files_skipped"] += 1
        return files, stats

    if input_path.is_file():
        if not input_path.name.startswith("."):
            files.append(input_path)
        return files, stats

    for child in sorted(input_path.iterdir()):
        if child.name.startswith(".") or child.name in OUTPUT_DIR_NAMES:
            continue
        if child.is_symlink():
            if child.is_dir():
                stats["symlink_dirs_skipped"] += 1
            else:
                stats["symlink_files_skipped"] += 1
            continue
        if child.is_dir():
            continue
        try:
            if is_within(child.resolve(), output_resolved):
                continue
        except FileNotFoundError:
            continue
        files.append(child)
    return files, stats


def safe_path_hash(path: Path, base: Path) -> str:
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = Path(path.name)
    digest = hashlib.sha256(str(rel).encode("utf-8")).hexdigest()
    return digest[:16]


def safe_relative_path(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return path.name


def size_bucket(size: int | None) -> str:
    if size is None:
        return "unknown"
    if size == 0:
        return "0B"
    thresholds = [
        (10 * 1024, "1B-10KB"),
        (100 * 1024, "10KB-100KB"),
        (1 * 1024 * 1024, "100KB-1MB"),
        (10 * 1024 * 1024, "1MB-10MB"),
        (50 * 1024 * 1024, "10MB-50MB"),
        (200 * 1024 * 1024, "50MB-200MB"),
    ]
    for limit, label in thresholds:
        if size <= limit:
            return label
    return ">200MB"


def mask_middle(value: str, keep_start: int, keep_end: int) -> str:
    if len(value) <= keep_start + keep_end:
        return "*" * len(value)
    return value[:keep_start] + ("*" * (len(value) - keep_start - keep_end)) + value[-keep_end:]


def mask_email(value: str) -> str:
    if "@" not in value:
        return "[email]"
    user, _domain = value.rsplit("@", 1)
    prefix = user[:3] if len(user) >= 3 else user[:1]
    return f"{prefix}***@[email-domain]"


def mask_url(value: str) -> str:
    return "[URL]"


def mask_value(detector_name: str, value: str) -> str:
    if detector_name == "phone":
        return mask_middle(value, 3, 4)
    if detector_name == "id_card":
        return mask_middle(value, 6, 4)
    if detector_name == "email":
        return mask_email(value)
    if detector_name == "unified_social_credit_code":
        return mask_middle(value, 4, 4)
    if detector_name == "bank_account":
        return mask_middle(value, 4, 4)
    if detector_name == "license_plate":
        return mask_middle(value, 2, 1)
    if detector_name == "url":
        return mask_url(value)
    if detector_name == "contract_number":
        return "[合同编号]"
    if detector_name == "case_number":
        return "[案件编号]"
    return "[已掩码]"


def add_masked_sample(samples: dict[str, list[str]], detector_name: str, masked_value: str) -> None:
    current = samples.setdefault(detector_name, [])
    if masked_value in current or len(current) >= 3:
        return
    current.append(masked_value)


def spans_overlap(span: tuple[int, int], claimed_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < claimed_end and end > claimed_start for claimed_start, claimed_end in claimed_spans)


def empty_detection_result() -> dict:
    return {
        "detected_sensitive_items": 0,
        "detected_by_type": {},
        "masked_samples": {},
        "content_read": False,
        "read_error": False,
        "office_document_body_read": False,
        "docx_main_body_read": False,
        "docx_review_reasons": [],
        "xlsx_visible_cells_read": False,
        "xlsx_review_reasons": [],
    }


def find_sensitive_matches(text: str, detector_config: dict) -> list[dict]:
    enabled = detector_config["enabled_detectors"]
    custom_terms = detector_config["custom_terms"]
    matches: list[dict] = []
    claimed_spans: list[tuple[int, int]] = []

    for detector_name in DETECTOR_PRIORITY:
        if not enabled.get(detector_name, True):
            continue
        pattern = REGEX_PATTERNS[detector_name]
        for match in pattern.finditer(text):
            value = match.group(1) if match.lastindex else match.group(0)
            span = match.span(1) if match.lastindex else match.span(0)
            if span[0] == span[1] or spans_overlap(span, claimed_spans):
                continue
            if detector_name == "bank_account" and REGEX_PATTERNS["id_card"].fullmatch(value):
                continue
            claimed_spans.append(span)
            matches.append(
                {
                    "detector_name": detector_name,
                    "value": value,
                    "start": span[0],
                    "end": span[1],
                }
            )

    if enabled.get("custom_terms", True):
        for key, terms in custom_terms.items():
            for term in terms:
                if not term:
                    continue
                for match in re.finditer(re.escape(term), text):
                    span = match.span(0)
                    if span[0] == span[1] or spans_overlap(span, claimed_spans):
                        continue
                    claimed_spans.append(span)
                    matches.append(
                        {
                            "detector_name": f"custom_terms.{key}",
                            "value": term,
                            "start": span[0],
                            "end": span[1],
                        }
                    )

    return sorted(matches, key=lambda item: (item["start"], item["end"], item["detector_name"]))


def custom_mask_for_sample(detector_name: str, value: str, placeholders: dict[str, dict[str, str]]) -> str:
    custom_key = detector_name.split(".", 1)[1]
    label = CUSTOM_TERM_LABELS[custom_key]
    placeholders.setdefault(label, {})
    if value not in placeholders[label]:
        placeholders[label][value] = f"[{label}{len(placeholders[label]) + 1}]"
    return placeholders[label][value]


def detect_sensitive_text(text: str, detector_config: dict) -> dict:
    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    custom_sample_placeholders: dict[str, dict[str, str]] = {}

    for match in find_sensitive_matches(text, detector_config):
        detector_name = match["detector_name"]
        value = match["value"]
        counts[detector_name] += 1
        if detector_name.startswith("custom_terms."):
            masked = custom_mask_for_sample(detector_name, value, custom_sample_placeholders)
        else:
            masked = mask_value(detector_name, value)
        add_masked_sample(samples, detector_name, masked)

    return {
        "detected_sensitive_items": sum(counts.values()),
        "detected_by_type": dict(sorted(counts.items())),
        "masked_samples": {key: samples[key] for key in sorted(samples)},
        "content_read": True,
        "read_error": False,
        "office_document_body_read": False,
        "docx_main_body_read": False,
        "docx_review_reasons": [],
        "xlsx_visible_cells_read": False,
        "xlsx_review_reasons": [],
    }


def local_xml_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def inspect_docx_document_root(root: ElementTree.Element) -> list[str]:
    reasons: set[str] = set()
    for element in root.iter():
        name = local_xml_name(element.tag)
        reason = DOCX_REVIEW_REASON_BY_TAG.get(name)
        if reason:
            reasons.add(reason)
    return sorted(reasons)


def extract_safe_docx_text_nodes(
    element: ElementTree.Element,
    ancestor_names: tuple[str, ...] = (),
) -> list[str]:
    name = local_xml_name(element.tag)
    current_ancestors = ancestor_names + (name,)
    if (
        name == "t"
        and element.text
        and "body" in ancestor_names
        and "p" in ancestor_names
        and "r" in ancestor_names
        and not DOCX_TEXT_EXCLUDED_ANCESTORS.intersection(ancestor_names)
    ):
        return [element.text]

    text_nodes: list[str] = []
    for child in element:
        text_nodes.extend(extract_safe_docx_text_nodes(child, current_ancestors))
    return text_nodes


def extract_safe_docx_text_elements(
    element: ElementTree.Element,
    ancestor_names: tuple[str, ...] = (),
) -> list[ElementTree.Element]:
    name = local_xml_name(element.tag)
    current_ancestors = ancestor_names + (name,)
    if (
        name == "t"
        and element.text
        and "body" in ancestor_names
        and "p" in ancestor_names
        and "r" in ancestor_names
        and not DOCX_TEXT_EXCLUDED_ANCESTORS.intersection(ancestor_names)
    ):
        return [element]

    text_elements: list[ElementTree.Element] = []
    for child in element:
        text_elements.extend(extract_safe_docx_text_elements(child, current_ancestors))
    return text_elements


def extract_safe_docx_paragraph_text_elements(root: ElementTree.Element) -> list[list[ElementTree.Element]]:
    """Return ordinary main-body paragraphs as ordered w:t element groups.

    Paragraphs in any excluded OOXML structure are deliberately omitted. Keeping
    the groups preserves the logical paragraph text needed for cross-run matches
    while retaining the original run elements for minimally scoped replacement.
    """
    paragraphs: list[list[ElementTree.Element]] = []

    def walk(element: ElementTree.Element, ancestor_names: tuple[str, ...] = ()) -> None:
        name = local_xml_name(element.tag)
        current_ancestors = ancestor_names + (name,)
        if (
            name == "p"
            and "body" in ancestor_names
            and not DOCX_TEXT_EXCLUDED_ANCESTORS.intersection(ancestor_names)
        ):
            text_elements = extract_safe_docx_text_elements(element, ancestor_names)
            if text_elements:
                paragraphs.append(text_elements)
            return
        for child in element:
            walk(child, current_ancestors)

    walk(root)
    return paragraphs


def paragraph_text_from_elements(elements: list[ElementTree.Element]) -> str:
    return "".join(element.text or "" for element in elements)


def extract_docx_main_body_text(path: Path) -> tuple[str, list[str], bool]:
    reasons: set[str] = set()
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for prefix, reason in DOCX_COMPLEX_PART_MARKERS:
                if any(name.startswith(prefix) for name in names):
                    reasons.add(reason)

            if "word/document.xml" not in names:
                reasons.add("docx_document_xml_missing")
                return "", sorted(reasons), True

            with archive.open("word/document.xml") as document_xml:
                root = ElementTree.parse(document_xml).getroot()
    except (ElementTree.ParseError, OSError, zipfile.BadZipFile):
        return "", sorted(reasons | {"docx_main_body_read_error"}), True

    reasons.update(inspect_docx_document_root(root))
    paragraphs = extract_safe_docx_paragraph_text_elements(root)
    return "\n".join(paragraph_text_from_elements(elements) for elements in paragraphs), sorted(reasons), False


def extract_docx_context_candidates(
    text: str,
    limit_per_type: int = 50,
    redaction_profile: str = AI_SHARE_PROFILE,
) -> dict[str, list[str]]:
    """Find profile-scoped candidates for local lawyer confirmation only."""
    return extract_profile_candidates(text, redaction_profile, limit_per_type)["candidate_terms"]


def apply_docx_candidate_precheck_mode(
    input_path: Path,
    output_path: Path,
    candidates_path: Path,
    overwrite_candidates: bool,
    detector_config: dict,
    redaction_profile: str,
) -> int:
    """Write local-only DOCX contextual candidates without automatic redaction."""
    validate_docx_candidate_input(input_path)
    validate_docx_candidates_path(output_path, candidates_path)
    if candidates_path.exists() and not overwrite_candidates:
        raise SystemExit("docx candidate output already exists; pass --overwrite-docx-candidates to replace it")

    reports_dir = output_path / "reports"
    redacted_dir = output_path / "redacted_files"
    logs_dir = output_path / "logs"
    for directory in (reports_dir, redacted_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (output_path / OUTPUT_MARKER).write_text("local-redaction-assistant\n", encoding="utf-8")
    generated_at = datetime.now(timezone.utc).isoformat()
    record = build_file_record(
        1,
        input_path,
        input_path.parent,
        "docx-candidate-precheck",
        500 * 1024 * 1024,
        detector_config,
    )
    text, docx_reasons, read_error = extract_docx_main_body_text(input_path)
    profile_result = extract_profile_candidates("", redaction_profile)
    candidates = profile_result["candidate_terms"]
    associations: list[dict[str, str]] = []
    error_codes: list[str] = []
    if read_error:
        error_codes.append("docx_candidate_precheck_read_error")
        status = "docx_candidate_precheck_failed"
    else:
        profile_result = extract_profile_candidates(text, redaction_profile)
        candidates = profile_result["candidate_terms"]
        associations = profile_result["candidate_associations"]
        status = "docx_candidate_precheck_completed"
        write_local_sensitive_json(
            candidates_path,
            {
                "schema_version": "1.0",
                "source": "docx_context_candidate_precheck",
                "redaction_profile": redaction_profile,
                "local_sensitive_file": True,
                "auto_apply_redaction": False,
                "manual_lawyer_confirmation_required": True,
                "candidate_terms": candidates,
                "candidate_associations": associations,
                "note": (
                    "Local candidate terms only. Review each candidate and manually copy only approved "
                    "terms into redaction_terms.local.json before DOCX redaction. Do not commit, upload, or share this file."
                ),
            },
        )
    record.update(
        {
            "status": status,
            "planned_action": "manually_confirm_candidate_terms_before_redaction",
            "detector_scope": "docx_context_candidate_precheck",
            "review_reasons": sorted(
                set(
                    record["review_reasons"]
                    + docx_reasons
                    + ["docx_context_candidates_require_manual_confirmation"]
                    + error_codes
                )
            ),
        }
    )
    candidate_counts = {key: len(values) for key, values in candidates.items()}
    candidate_report = {
        "tool": "local-redaction-assistant",
        "version": VERSION,
        "generated_at_utc": generated_at,
        "mode": "docx-candidate-precheck",
        "redaction_profile": redaction_profile,
        "files_attempted": 1,
        "files_completed": 0 if read_error else 1,
        "candidate_counts": candidate_counts,
        "candidate_association_count": len(associations),
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "candidate_terms_recorded_in_report": False,
        "manual_review_required": True,
        "error_code_counts": dict(sorted(Counter(error_codes).items())),
        "files": [
            {
                "file_id": record["file_id"],
                "file_type": "docx",
                "status": status,
                "candidate_counts": candidate_counts,
                "review_required": True,
                "review_reasons": record["review_reasons"],
                "error_codes": error_codes,
            }
        ],
    }
    summary = {
        "tool": "local-redaction-assistant",
        "version": VERSION,
        "generated_at_utc": generated_at,
        "mode": "docx-candidate-precheck",
        "redaction_profile": redaction_profile,
        "safety": {
            "originals_modified": False,
            "network_used": False,
            "external_api_used": False,
            "docx_main_body_read_only": not read_error,
            "docx_complex_structures_read": False,
            "raw_sensitive_values_recorded": False,
            "source_names_recorded": False,
            "source_paths_recorded": False,
            "stable_file_identifier_recorded": False,
            "precise_size_recorded": False,
            "local_file_index_saved": True,
            "human_review_required": True,
        },
        "totals": {
            "files_seen": 1,
            "supported_files": 1,
            "unsupported_files": 0,
            "symlink_files_skipped": 0,
            "symlink_dirs_skipped": 0,
            "detected_sensitive_items": 0,
            "files_with_findings": 0,
            "redacted_files_created": 0,
            "manual_review_files": 1,
        },
        "detected_by_type": {},
        "masked_samples_by_type": {},
        "status_counts": {status: 1},
        "review_reason_counts": dict(sorted(Counter(record["review_reasons"]).items())),
        "files": [record],
        "notes": [
            "docx-candidate-precheck uses local deterministic legal-context patterns on ordinary main-body DOCX text only.",
            "legal-template candidates are suggestions only; strong-semantic names, aliases, places, people and project figures require lawyer confirmation.",
            "Candidate terms are not auto-approved and are never used for automatic replacement in this mode.",
            "Candidate values are stored only in docx_candidate_terms.local.json, which is local-sensitive and excluded from reports.",
            "The lawyer must manually review candidates and explicitly add approved terms to redaction_terms.local.json before redaction.",
        ],
    }
    write_json(reports_dir / "summary.json", summary)
    write_json(reports_dir / "precheck_report.json", build_precheck_report(summary))
    write_manual_review_csv(reports_dir / "manual_review_list.csv", [record])
    write_run_summary_md(reports_dir / "run_summary.md", summary)
    write_json(reports_dir / "docx_candidate_report.json", candidate_report)
    write_local_sensitive_json(
        output_path / "file_index.local.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "local_sensitive_file": True,
            "original_absolute_paths_included": False,
            "raw_sensitive_values_recorded": False,
            "entries": [build_file_index_record(1, input_path, input_path.parent)],
            "note": "Local index for lawyer-side file lookup only. Do not commit or share.",
        },
    )
    print(
        "DOCX candidate precheck finished: "
        f"status={status}, raw_values_recorded=false, manual_confirmation_required=true"
    )
    print("docx_candidate_terms.local.json is local-sensitive; do not commit, upload, or share it.")
    return 0 if not read_error else 2


def placeholder_for_match(match: dict, placeholder_state: dict) -> str:
    detector_name = match["detector_name"]
    value = match["value"]
    label = PLACEHOLDER_LABELS.get(detector_name, "敏感信息")
    by_label = placeholder_state.setdefault("by_label", {})
    entries = placeholder_state.setdefault("entries", [])
    label_values = by_label.setdefault(label, {})
    if value not in label_values:
        placeholder = f"【{label}{len(label_values) + 1}】"
        label_values[value] = placeholder
        entries.append(
            {
                "detector_type": detector_name,
                "placeholder": placeholder,
                "raw_value": value,
            }
        )
    return label_values[value]


def redact_text_with_placeholders(
    text: str,
    detector_config: dict,
    placeholder_state: dict,
) -> tuple[str, Counter[str], int]:
    matches = find_sensitive_matches(text, detector_config)
    if not matches:
        return text, Counter(), 0

    redacted = text
    by_type: Counter[str] = Counter()
    for match in reversed(matches):
        placeholder = placeholder_for_match(match, placeholder_state)
        start = match["start"]
        end = match["end"]
        redacted = redacted[:start] + placeholder + redacted[end:]
        by_type[match["detector_name"]] += 1
    return redacted, by_type, len(matches)


def set_docx_text_value(element: ElementTree.Element, value: str) -> None:
    element.text = value
    xml_space = "{http://www.w3.org/XML/1998/namespace}space"
    if value.startswith(" ") or value.endswith(" "):
        element.attrib[xml_space] = "preserve"
    else:
        element.attrib.pop(xml_space, None)


def redact_docx_paragraph_elements(
    elements: list[ElementTree.Element],
    detector_config: dict,
    placeholder_state: dict,
) -> tuple[Counter[str], int]:
    """Replace non-overlapping detector matches, including clean cross-run matches."""
    logical_text = paragraph_text_from_elements(elements)
    matches = find_sensitive_matches(logical_text, detector_config)
    if not matches:
        return Counter(), 0

    spans: list[tuple[int, int, ElementTree.Element]] = []
    cursor = 0
    for element in elements:
        value = element.text or ""
        end = cursor + len(value)
        spans.append((cursor, end, element))
        cursor = end

    by_type: Counter[str] = Counter()
    replacements = 0
    for match in reversed(matches):
        overlap_indices = [
            index
            for index, (start, end, _element) in enumerate(spans)
            if match["start"] < end and match["end"] > start
        ]
        if not overlap_indices:
            continue
        first_index = overlap_indices[0]
        last_index = overlap_indices[-1]
        first_start, _first_end, first_element = spans[first_index]
        last_start, _last_end, last_element = spans[last_index]
        placeholder = placeholder_for_match(match, placeholder_state)
        start_offset = match["start"] - first_start
        end_offset = match["end"] - last_start

        if first_index == last_index:
            value = first_element.text or ""
            set_docx_text_value(first_element, value[:start_offset] + placeholder + value[end_offset:])
        else:
            first_value = first_element.text or ""
            last_value = last_element.text or ""
            set_docx_text_value(first_element, first_value[:start_offset] + placeholder)
            for index in overlap_indices[1:-1]:
                set_docx_text_value(spans[index][2], "")
            set_docx_text_value(last_element, last_value[end_offset:])
        by_type[match["detector_name"]] += 1
        replacements += 1
    return by_type, replacements


def redact_docx_document_xml(
    document_xml: bytes,
    detector_config: dict,
    placeholder_state: dict,
) -> tuple[bytes, Counter[str], int, list[str], bool]:
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError:
        return document_xml, Counter(), 0, ["docx_document_xml_parse_error"], True

    reasons = set(inspect_docx_document_root(root))
    try:
        paragraphs, slice_errors = extract_safe_docx_text_slices(
            document_xml,
            DOCX_TEXT_EXCLUDED_ANCESTORS,
        )
    except ElementTree.ParseError:
        return document_xml, Counter(), 0, ["docx_document_xml_parse_error"], True
    reasons.update(slice_errors)
    if slice_errors:
        return document_xml, Counter(), 0, sorted(reasons), True

    replacements_by_type: Counter[str] = Counter()
    replacements_total = 0
    patches: list[tuple[int, int, bytes]] = []
    for slices in paragraphs:
        logical_text = "".join(item.text for item in slices)
        matches = find_sensitive_matches(logical_text, detector_config)
        if not matches:
            continue
        spans: list[tuple[int, int]] = []
        values = [item.text for item in slices]
        cursor = 0
        for value in values:
            spans.append((cursor, cursor + len(value)))
            cursor += len(value)
        for match in reversed(matches):
            overlap_indices = [
                index
                for index, (start, end) in enumerate(spans)
                if match["start"] < end and match["end"] > start
            ]
            if not overlap_indices:
                continue
            first_index = overlap_indices[0]
            last_index = overlap_indices[-1]
            first_start, _first_end = spans[first_index]
            last_start, _last_end = spans[last_index]
            placeholder = placeholder_for_match(match, placeholder_state)
            start_offset = match["start"] - first_start
            end_offset = match["end"] - last_start
            if first_index == last_index:
                value = values[first_index]
                values[first_index] = value[:start_offset] + placeholder + value[end_offset:]
            else:
                values[first_index] = values[first_index][:start_offset] + placeholder
                for index in overlap_indices[1:-1]:
                    values[index] = ""
                values[last_index] = values[last_index][end_offset:]
            replacements_by_type[match["detector_name"]] += 1
            replacements_total += 1
        for item, value in zip(slices, values):
            if value != item.text:
                patches.append((item.start, item.end, xml_escape_text(value)))

    try:
        redacted_xml = apply_byte_patches(document_xml, patches)
        ElementTree.fromstring(redacted_xml)
    except (ElementTree.ParseError, ValueError):
        return document_xml, Counter(), 0, sorted(reasons | {"docx_minimal_patch_validation_failed"}), True
    return redacted_xml, replacements_by_type, replacements_total, sorted(reasons), False


def validate_redacted_docx(
    output_path: Path,
    redacted_types: set[str],
    detector_config: dict,
) -> tuple[bool, list[str]]:
    if not output_path.exists():
        return False, ["redacted_docx_missing"]
    package_ok, package_errors = validate_docx_ooxml_package(output_path)
    if not package_ok:
        return False, package_errors
    try:
        with zipfile.ZipFile(output_path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names:
                return False, ["redacted_docx_document_xml_missing"]
            with archive.open("word/document.xml") as document_xml:
                ElementTree.parse(document_xml).getroot()
    except (ElementTree.ParseError, OSError, zipfile.BadZipFile, KeyError):
        return False, ["redacted_docx_reopen_failed"]

    detection = detect_file(output_path, detector_config)
    if detection["read_error"]:
        return False, ["redacted_docx_validation_failed"]

    remaining_types = {
        detector_name
        for detector_name in redacted_types
        if detection["detected_by_type"].get(detector_name, 0) > 0
    }
    if remaining_types:
        return False, ["redacted_copy_contains_remaining_supported_findings"]
    return True, []


def create_docx_redacted_copy(
    source_path: Path,
    redacted_dir: Path,
    file_id: str,
    detector_config: dict,
    placeholder_state: dict,
    word_validation: str = "off",
) -> dict:
    temp_path = redacted_dir / f"{file_id}.tmp.docx"
    final_path = redacted_dir / f"{file_id}_脱敏版.docx"
    result = {
        "status": "redaction_failed",
        "redacted_copy_created": False,
        "validation_succeeded": False,
        "error_codes": [],
        "redaction_by_type": {},
        "replacements": 0,
        "metadata_fields_sanitized": 0,
        "document_xml_minimal_patch_used": False,
        "word_native_validation_required": word_validation == "required",
        "word_native_validation_succeeded": False,
    }

    try:
        if temp_path.exists():
            temp_path.unlink()
        with zipfile.ZipFile(source_path, "r") as source_zip:
            names = set(source_zip.namelist())
            if "word/document.xml" not in names:
                result["error_codes"].append("docx_document_xml_missing")
                return result

            document_xml = source_zip.read("word/document.xml")
            redacted_xml, by_type, replacements, docx_reasons, parse_error = redact_docx_document_xml(
                document_xml,
                detector_config,
                placeholder_state,
            )
            result["docx_review_reasons"] = docx_reasons
            if parse_error:
                result["error_codes"].append("docx_document_xml_parse_error")
                return result
            if replacements == 0:
                result["status"] = "redaction_skipped_no_supported_findings"
                result["redaction_by_type"] = {}
                return result

            metadata_fields_sanitized = 0
            try:
                with zipfile.ZipFile(temp_path, "w") as output_zip:
                    for item in source_zip.infolist():
                        if item.filename == "word/document.xml":
                            data = redacted_xml
                        else:
                            data = source_zip.read(item.filename)
                            data, metadata_changes = sanitise_docx_metadata_part(item.filename, data)
                            metadata_fields_sanitized += metadata_changes
                        output_zip.writestr(item, data)
            except ElementTree.ParseError:
                result["error_codes"].append("docx_metadata_sanitization_failed")
                temp_path.unlink(missing_ok=True)
                return result
            result["metadata_fields_sanitized"] = metadata_fields_sanitized
            result["document_xml_minimal_patch_used"] = True

        validation_ok, validation_errors = validate_redacted_docx(
            temp_path,
            set(by_type),
            detector_config,
        )
        if not validation_ok:
            result["status"] = "redaction_validation_failed"
            result["error_codes"].extend(validation_errors)
            temp_path.unlink(missing_ok=True)
            return result

        word_ok, word_errors = validate_docx_with_word(temp_path, word_validation)
        if not word_ok:
            result["status"] = "redaction_validation_failed"
            result["error_codes"].extend(word_errors)
            temp_path.unlink(missing_ok=True)
            return result
        result["word_native_validation_succeeded"] = word_validation == "required"

        if final_path.exists():
            final_path.unlink()
        temp_path.replace(final_path)
        result.update(
            {
                "status": "redaction_succeeded",
                "redacted_copy_created": True,
                "validation_succeeded": True,
                "redaction_by_type": dict(sorted(by_type.items())),
                "replacements": replacements,
                "metadata_fields_sanitized": metadata_fields_sanitized,
                "document_xml_minimal_patch_used": True,
            }
        )
        return result
    except (OSError, zipfile.BadZipFile, KeyError):
        result["error_codes"].append("docx_redaction_copy_failed")
        temp_path.unlink(missing_ok=True)
        return result


def inspect_xlsx_package_parts(names: set[str]) -> list[str]:
    reasons: set[str] = set()
    for prefix, reason in XLSX_COMPLEX_PART_MARKERS:
        if any(name.startswith(prefix) for name in names):
            reasons.add(reason)
    return sorted(reasons)


def inspect_xlsx_sheet_root(root: ElementTree.Element) -> list[str]:
    reasons: set[str] = set()
    for element in root.iter():
        reason = XLSX_REVIEW_REASON_BY_TAG.get(local_xml_name(element.tag))
        if reason:
            reasons.add(reason)
    return sorted(reasons)


def resolve_xlsx_relationship_target(target: str) -> str | None:
    target = target.strip()
    if not target or "://" in target:
        return None
    if target.startswith("/"):
        candidate = PurePosixPath(target.lstrip("/"))
    else:
        candidate = PurePosixPath("xl") / target
    normalized_parts: list[str] = []
    for part in candidate.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            return None
        normalized_parts.append(part)
    normalized = PurePosixPath(*normalized_parts)
    if not str(normalized).startswith("xl/worksheets/"):
        return None
    if normalized.suffix.lower() != ".xml":
        return None
    return normalized.as_posix()


def parse_xlsx_workbook_relationships(archive: zipfile.ZipFile, names: set[str]) -> tuple[dict[str, str], list[str], bool]:
    reasons: set[str] = set()
    rels_path = "xl/_rels/workbook.xml.rels"
    if rels_path not in names:
        return {}, ["xlsx_workbook_rels_missing"], True

    try:
        with archive.open(rels_path) as rels_xml:
            root = ElementTree.parse(rels_xml).getroot()
    except (ElementTree.ParseError, OSError, KeyError):
        return {}, ["xlsx_workbook_rels_read_error"], True

    relationships: dict[str, str] = {}
    for rel in root.findall("rel:Relationship", XLSX_MAIN_NS):
        rel_id = rel.attrib.get("Id", "").strip()
        rel_type = rel.attrib.get("Type", "")
        target = rel.attrib.get("Target", "")
        if not rel_id:
            reasons.add("xlsx_sheet_relationship_missing")
            continue
        if not rel_type.endswith("/worksheet"):
            continue
        resolved = resolve_xlsx_relationship_target(target)
        if resolved is None:
            reasons.add("xlsx_sheet_path_resolve_error")
            continue
        relationships[rel_id] = resolved

    return relationships, sorted(reasons), False


def parse_xlsx_visible_sheet_paths(archive: zipfile.ZipFile, names: set[str]) -> tuple[list[str], list[str], bool]:
    reasons: set[str] = set()
    if "xl/workbook.xml" not in names:
        return [], ["xlsx_workbook_xml_missing"], True

    try:
        with archive.open("xl/workbook.xml") as workbook_xml:
            root = ElementTree.parse(workbook_xml).getroot()
    except (ElementTree.ParseError, OSError, KeyError):
        return [], ["xlsx_workbook_read_error"], True

    relationships, rels_reasons, rels_error = parse_xlsx_workbook_relationships(archive, names)
    reasons.update(rels_reasons)
    if rels_error:
        return [], sorted(reasons), True

    visible_paths: list[str] = []
    sheets = root.findall(".//x:sheet", XLSX_MAIN_NS)
    visible_sheet_count = 0
    for sheet in sheets:
        state = sheet.attrib.get("state", "visible")
        if state in {"hidden", "veryHidden"}:
            reasons.add("xlsx_hidden_sheets_may_exist")
            continue
        visible_sheet_count += 1
        rel_id = sheet.attrib.get(f"{{{XLSX_OFFICE_REL_NS_URI}}}id", "").strip()
        if not rel_id:
            reasons.add("xlsx_sheet_relationship_missing")
            continue
        sheet_path = relationships.get(rel_id)
        if not sheet_path:
            reasons.add("xlsx_sheet_relationship_missing")
            continue
        if sheet_path not in names:
            reasons.add("xlsx_sheet_path_resolve_error")
            continue
        visible_paths.append(sheet_path)

    if not sheets:
        return [], sorted(reasons | {"xlsx_no_workbook_sheets_found"}), True
    if visible_sheet_count and not visible_paths:
        return [], sorted(reasons), True
    return visible_paths, sorted(reasons), False


def find_direct_child_text(element: ElementTree.Element, child_name: str) -> str | None:
    for child in element:
        if local_xml_name(child.tag) == child_name:
            return child.text
    return None


def extract_inline_string(cell: ElementTree.Element) -> str:
    text_parts: list[str] = []
    for element in cell.iter():
        if local_xml_name(element.tag) == "t" and element.text:
            text_parts.append(element.text)
    return "".join(text_parts)


def collect_xlsx_visible_cell_values(
    root: ElementTree.Element,
) -> tuple[list[str], list[int], list[str]]:
    reasons: set[str] = set(inspect_xlsx_sheet_root(root))
    values: list[str] = []
    shared_string_indices: list[int] = []

    for cell in root.iter():
        if local_xml_name(cell.tag) != "c":
            continue
        if any(local_xml_name(child.tag) == "f" for child in cell):
            reasons.add("xlsx_formulas_not_read")
            continue

        cell_type = cell.attrib.get("t", "")
        if cell_type == "s":
            raw_index = find_direct_child_text(cell, "v")
            if raw_index is None:
                continue
            try:
                shared_string_indices.append(int(raw_index.strip()))
            except ValueError:
                reasons.add("xlsx_shared_string_index_invalid")
            continue
        if cell_type == "inlineStr":
            value = extract_inline_string(cell).strip()
            if value:
                values.append(value)
            continue

        raw_value = find_direct_child_text(cell, "v")
        if raw_value is not None and raw_value.strip():
            values.append(raw_value.strip())

    return values, shared_string_indices, sorted(reasons)


def extract_needed_shared_strings(
    archive: zipfile.ZipFile,
    names: set[str],
    needed_indices: set[int],
) -> tuple[dict[int, str], list[str]]:
    if not needed_indices:
        return {}, []
    if "xl/sharedStrings.xml" not in names:
        return {}, ["xlsx_shared_strings_missing"]

    shared_values: dict[int, str] = {}
    try:
        with archive.open("xl/sharedStrings.xml") as shared_strings_xml:
            index = -1
            capture = False
            text_parts: list[str] = []
            for event, element in ElementTree.iterparse(shared_strings_xml, events=("start", "end")):
                name = local_xml_name(element.tag)
                if event == "start" and name == "si":
                    index += 1
                    capture = index in needed_indices
                    text_parts = []
                elif event == "end" and capture and name == "t" and element.text:
                    text_parts.append(element.text)
                elif event == "end" and name == "si":
                    if capture:
                        value = "".join(text_parts).strip()
                        if value:
                            shared_values[index] = value
                    element.clear()
    except (ElementTree.ParseError, OSError, KeyError):
        return {}, ["xlsx_shared_strings_read_error"]

    return shared_values, []


def extract_xlsx_visible_cell_text(path: Path) -> tuple[str, list[str], bool]:
    reasons: set[str] = set()
    values: list[str] = []
    shared_string_indices: list[int] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            reasons.update(inspect_xlsx_package_parts(names))
            visible_sheet_paths, workbook_reasons, workbook_error = parse_xlsx_visible_sheet_paths(archive, names)
            reasons.update(workbook_reasons)
            if workbook_error:
                return "", sorted(reasons), True

            for sheet_path in visible_sheet_paths:
                try:
                    with archive.open(sheet_path) as sheet_xml:
                        sheet_root = ElementTree.parse(sheet_xml).getroot()
                except (ElementTree.ParseError, OSError, KeyError):
                    reasons.add("xlsx_visible_sheet_read_error")
                    return "", sorted(reasons), True
                sheet_values, sheet_shared_indices, sheet_reasons = collect_xlsx_visible_cell_values(sheet_root)
                values.extend(sheet_values)
                shared_string_indices.extend(sheet_shared_indices)
                reasons.update(sheet_reasons)

            needed_indices = set(shared_string_indices)
            shared_values, shared_reasons = extract_needed_shared_strings(archive, names, needed_indices)
            reasons.update(shared_reasons)
            values.extend(shared_values[index] for index in shared_string_indices if index in shared_values)
    except (OSError, zipfile.BadZipFile):
        return "", sorted(reasons | {"xlsx_visible_cells_read_error"}), True

    return "\n".join(values), sorted(reasons), False


def detect_file(path: Path, detector_config: dict) -> dict:
    suffix = path.suffix.lower()
    if suffix in TEXT_DETECTOR_EXTENSIONS:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            result = empty_detection_result()
            result["content_read"] = False
            result["read_error"] = True
            return result
        return detect_sensitive_text(text, detector_config)

    if suffix in DOCX_DETECTOR_EXTENSIONS:
        text, docx_reasons, read_error = extract_docx_main_body_text(path)
        result = detect_sensitive_text(text, detector_config)
        result["content_read"] = not read_error
        result["read_error"] = read_error
        result["office_document_body_read"] = not read_error
        result["docx_main_body_read"] = not read_error
        result["docx_review_reasons"] = docx_reasons
        return result

    if suffix in XLSX_DETECTOR_EXTENSIONS:
        text, xlsx_reasons, read_error = extract_xlsx_visible_cell_text(path)
        result = detect_sensitive_text(text, detector_config)
        result["content_read"] = not read_error
        result["read_error"] = read_error
        result["office_document_body_read"] = not read_error
        result["xlsx_visible_cells_read"] = not read_error
        result["xlsx_review_reasons"] = xlsx_reasons
        return result

    return empty_detection_result()


def load_tesseract() -> str:
    executable = shutil.which("tesseract")
    if not executable:
        raise SystemExit(
            "Tesseract is required for --mode ocr-precheck. "
            "Install or configure the local OCR runtime before retrying."
        )
    return executable


def run_tesseract_tsv(
    executable: str,
    image_bytes: bytes,
    languages: str,
    psm: int,
    min_confidence: float,
    timeout_seconds: int,
) -> tuple[list[dict], int, int, int, list[str]]:
    command = [
        executable,
        "stdin",
        "stdout",
        "-l",
        languages,
        "--psm",
        str(psm),
        "tsv",
    ]
    try:
        completed = subprocess.run(
            command,
            input=image_bytes,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return [], 0, 0, 0, ["ocr_engine_timeout"]
    except OSError:
        return [], 0, 0, 0, ["ocr_engine_execution_failed"]
    if completed.returncode != 0:
        return [], 0, 0, 0, ["ocr_engine_failed"]
    try:
        tsv_text = completed.stdout.decode("utf-8", errors="replace")
        rows = csv.DictReader(io.StringIO(tsv_text), delimiter="\t")
        tokens: list[dict] = []
        image_width = 0
        image_height = 0
        low_confidence_tokens = 0
        for row in rows:
            try:
                level = int(row.get("level", "0"))
                left = int(row.get("left", "0"))
                top = int(row.get("top", "0"))
                width = int(row.get("width", "0"))
                height = int(row.get("height", "0"))
            except (TypeError, ValueError):
                continue
            if level == 1:
                image_width = max(image_width, width)
                image_height = max(image_height, height)
                continue
            if level != 5:
                continue
            text = (row.get("text") or "").strip()
            if not text or width <= 0 or height <= 0:
                continue
            try:
                confidence = float(row.get("conf", "-1"))
            except (TypeError, ValueError):
                confidence = -1.0
            if confidence < min_confidence:
                low_confidence_tokens += 1
                continue
            try:
                block_num = int(row.get("block_num", "0"))
                par_num = int(row.get("par_num", "0"))
                line_num = int(row.get("line_num", "0"))
                word_num = int(row.get("word_num", "0"))
            except (TypeError, ValueError):
                block_num = par_num = line_num = word_num = 0
            tokens.append(
                {
                    "text": text,
                    "confidence": confidence,
                    "left": left,
                    "top": top,
                    "width": width,
                    "height": height,
                    "line_key": (block_num, par_num, line_num),
                    "word_num": word_num,
                }
            )
    except (csv.Error, UnicodeError):
        return [], 0, 0, 0, ["ocr_tsv_parse_error"]
    if image_width <= 0 or image_height <= 0:
        return [], 0, 0, low_confidence_tokens, ["ocr_image_dimensions_missing"]
    return tokens, image_width, image_height, low_confidence_tokens, []


def ocr_confidence_bucket(confidence: float) -> str:
    if confidence >= 85:
        return "high"
    if confidence >= 70:
        return "medium"
    return "review"


def separator_between_ocr_tokens(previous: dict | None, current: dict) -> str:
    if previous is None:
        return ""
    previous_right = previous["left"] + previous["width"]
    horizontal_gap = current["left"] - previous_right
    previous_char_width = previous["width"] / max(len(previous["text"]), 1)
    current_char_width = current["width"] / max(len(current["text"]), 1)
    merge_gap_limit = max(2.0, min(previous_char_width, current_char_width) * 0.75)
    return " " if horizontal_gap > merge_gap_limit else ""


def detect_ocr_tokens(
    tokens: list[dict],
    image_width: int,
    image_height: int,
    page_number: int,
    detector_config: dict,
) -> tuple[Counter[str], dict[str, list[str]], list[dict]]:
    grouped: dict[tuple[int, int, int], list[dict]] = defaultdict(list)
    for token in tokens:
        grouped[token["line_key"]].append(token)

    counts: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    custom_placeholders: dict[str, dict[str, str]] = {}
    regions: list[dict] = []
    seen_regions: set[tuple] = set()
    for line_tokens in grouped.values():
        line_tokens.sort(key=lambda item: item["word_num"])
        line_text_parts: list[str] = []
        token_spans: list[tuple[int, int, dict]] = []
        cursor = 0
        previous_token: dict | None = None
        for token in line_tokens:
            separator = separator_between_ocr_tokens(previous_token, token)
            if separator:
                line_text_parts.append(separator)
                cursor += len(separator)
            value = token["text"]
            start = cursor
            cursor += len(value)
            token_spans.append((start, cursor, token))
            line_text_parts.append(value)
            previous_token = token
        line_text = "".join(line_text_parts)
        for match in find_sensitive_matches(line_text, detector_config):
            matched_tokens = [
                token
                for start, end, token in token_spans
                if match["start"] < end and match["end"] > start
            ]
            if not matched_tokens:
                continue
            detector_name = match["detector_name"]
            raw_value = match["value"]
            counts[detector_name] += 1
            if detector_name.startswith("custom_terms."):
                masked = custom_mask_for_sample(detector_name, raw_value, custom_placeholders)
            else:
                masked = mask_value(detector_name, raw_value)
            add_masked_sample(samples, detector_name, masked)

            left = min(token["left"] for token in matched_tokens)
            top = min(token["top"] for token in matched_tokens)
            right = max(token["left"] + token["width"] for token in matched_tokens)
            bottom = max(token["top"] + token["height"] for token in matched_tokens)
            x0 = round(max(0.0, left / image_width), 6)
            y0 = round(max(0.0, top / image_height), 6)
            x1 = round(min(1.0, right / image_width), 6)
            y1 = round(min(1.0, bottom / image_height), 6)
            region_key = (page_number, x0, y0, x1, y1, detector_name)
            if region_key in seen_regions:
                continue
            seen_regions.add(region_key)
            mean_confidence = sum(token["confidence"] for token in matched_tokens) / len(matched_tokens)
            regions.append(
                {
                    "page": page_number,
                    "x0": x0,
                    "y0": y0,
                    "x1": x1,
                    "y1": y1,
                    "label": re.sub(r"[^A-Za-z0-9_.-]", "_", detector_name) + "_candidate",
                    "detector_type": detector_name,
                    "confidence_bucket": ocr_confidence_bucket(mean_confidence),
                }
            )
    return counts, {key: samples[key] for key in sorted(samples)}, regions


def precheck_ocr_file(
    path: Path,
    detector_config: dict,
    tesseract: str,
    languages: str,
    dpi: int,
    min_confidence: float,
    psm: int,
    max_pages: int,
    timeout_seconds: int,
    pymupdf,
) -> dict:
    result = {
        "status": "ocr_precheck_failed",
        "pages_seen": 0,
        "pages_processed": 0,
        "detected_sensitive_items": 0,
        "detected_by_type": {},
        "masked_samples": {},
        "candidate_regions": [],
        "low_confidence_tokens": 0,
        "error_codes": [],
        "review_reasons": [
            "local_ocr_precheck_only",
            "ocr_candidates_require_manual_review",
            "ocr_text_not_persisted",
        ],
    }
    total_counts: Counter[str] = Counter()
    total_samples: dict[str, list[str]] = {}
    suffix = path.suffix.lower()

    def process_image_bytes(image_bytes: bytes, page_number: int) -> None:
        tokens, width, height, low_count, errors = run_tesseract_tsv(
            tesseract,
            image_bytes,
            languages,
            psm,
            min_confidence,
            timeout_seconds,
        )
        result["low_confidence_tokens"] += low_count
        result["error_codes"].extend(errors)
        if errors:
            return
        counts, samples, regions = detect_ocr_tokens(
            tokens,
            width,
            height,
            page_number,
            detector_config,
        )
        total_counts.update(counts)
        for detector_name, masked_values in samples.items():
            for masked_value in masked_values:
                add_masked_sample(total_samples, detector_name, masked_value)
        result["candidate_regions"].extend(regions)
        result["pages_processed"] += 1

    if suffix == ".pdf":
        try:
            document = pymupdf.open(path)
        except Exception:
            result["error_codes"].append("ocr_pdf_open_failed")
            return result
        try:
            result["pages_seen"] = document.page_count
            if getattr(document, "needs_pass", False):
                result["error_codes"].append("ocr_pdf_encrypted")
                return result
            pages_to_process = min(document.page_count, max_pages)
            if document.page_count > max_pages:
                result["review_reasons"].append("ocr_page_limit_reached")
            matrix = pymupdf.Matrix(dpi / 72.0, dpi / 72.0)
            for page_index in range(pages_to_process):
                try:
                    page = document.load_page(page_index)
                    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                    process_image_bytes(pixmap.tobytes("png"), page_index + 1)
                except Exception:
                    result["error_codes"].append("ocr_pdf_render_failed")
        finally:
            document.close()
    else:
        result["pages_seen"] = 1
        try:
            process_image_bytes(path.read_bytes(), 1)
        except OSError:
            result["error_codes"].append("ocr_image_read_failed")

    if result["low_confidence_tokens"]:
        result["review_reasons"].append("ocr_low_confidence_tokens_skipped")
    result["detected_by_type"] = dict(sorted(total_counts.items()))
    result["detected_sensitive_items"] = sum(total_counts.values())
    result["masked_samples"] = {key: total_samples[key] for key in sorted(total_samples)}
    result["error_codes"] = sorted(set(result["error_codes"]))
    result["review_reasons"] = sorted(set(result["review_reasons"]))
    if result["pages_processed"] == 0:
        result["status"] = "ocr_precheck_failed"
    elif result["error_codes"] or result["pages_processed"] < result["pages_seen"]:
        result["status"] = "ocr_precheck_partial_review_required"
    else:
        result["status"] = "ocr_precheck_completed_review_required"
    return result


def empty_ocr_precheck_report(
    dpi: int,
    languages: str,
    min_confidence: float,
    psm: int,
    max_pages: int,
    timeout_seconds: int,
) -> dict:
    return {
        "mode": "ocr-precheck",
        "ocr_engine": "tesseract_local",
        "ocr_files_attempted": 0,
        "ocr_files_processed": 0,
        "ocr_files_failed": 0,
        "ocr_files_skipped": 0,
        "pages_seen": 0,
        "pages_processed": 0,
        "detected_sensitive_items": 0,
        "detected_by_type": {},
        "candidate_regions_created": 0,
        "low_confidence_tokens_skipped": 0,
        "ocr_dpi": dpi,
        "ocr_languages": languages,
        "ocr_min_confidence": min_confidence,
        "ocr_psm": psm,
        "ocr_max_pages": max_pages,
        "ocr_timeout_seconds": timeout_seconds,
        "raw_values_recorded": False,
        "ocr_text_persisted": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "coordinates_recorded_in_report": False,
        "manual_review_required": True,
        "error_code_counts": {},
        "files": [],
    }


def apply_ocr_precheck(
    scanned_paths: list[Path],
    files: list[dict],
    candidates_path: Path,
    detector_config: dict,
    languages: str,
    dpi: int,
    min_confidence: float,
    psm: int,
    max_pages: int,
    timeout_seconds: int,
) -> dict:
    report = empty_ocr_precheck_report(
        dpi,
        languages,
        min_confidence,
        psm,
        max_pages,
        timeout_seconds,
    )
    tesseract = load_tesseract()
    pymupdf = load_pymupdf() if any(path.suffix.lower() == ".pdf" for path in scanned_paths) else None
    error_counts: Counter[str] = Counter()
    detected_by_type: Counter[str] = Counter()
    candidate_files: list[dict] = []

    if len(scanned_paths) != len(files):
        raise SystemExit("internal OCR file record count mismatch")
    for path, record in zip(scanned_paths, files):
        file_entry = {
            "file_id": record["file_id"],
            "file_type": record["file_type"],
            "status": record["status"],
            "pages_seen": 0,
            "pages_processed": 0,
            "detected_sensitive_items": 0,
            "detected_by_type": {},
            "masked_samples": {},
            "candidate_regions_created": 0,
            "low_confidence_tokens_skipped": 0,
            "review_required": True,
            "review_reasons": list(record["review_reasons"]),
            "error_codes": [],
        }
        if record["status"] in {"skipped_too_large", "skipped_unsupported_type", "ocr_precheck_skipped_non_ocr"}:
            report["ocr_files_skipped"] += 1
            report["files"].append(file_entry)
            continue

        report["ocr_files_attempted"] += 1
        result = precheck_ocr_file(
            path,
            detector_config,
            tesseract,
            languages,
            dpi,
            min_confidence,
            psm,
            max_pages,
            timeout_seconds,
            pymupdf,
        )
        record.update(
            {
                "status": result["status"],
                "planned_action": "manual_review_ocr_candidates",
                "detector_scope": "local_tesseract_ocr_precheck",
                "detected_sensitive_items": result["detected_sensitive_items"],
                "detected_by_type": result["detected_by_type"],
                "findings_count": result["detected_sensitive_items"],
                "finding_types": sorted(result["detected_by_type"]),
                "masked_samples": result["masked_samples"],
                "ocr_pages_processed": result["pages_processed"],
                "ocr_low_confidence_tokens_skipped": result["low_confidence_tokens"],
                "review_reasons": sorted(set(record["review_reasons"] + result["review_reasons"] + result["error_codes"])),
            }
        )
        file_entry.update(
            {
                "status": result["status"],
                "pages_seen": result["pages_seen"],
                "pages_processed": result["pages_processed"],
                "detected_sensitive_items": result["detected_sensitive_items"],
                "detected_by_type": result["detected_by_type"],
                "masked_samples": result["masked_samples"],
                "candidate_regions_created": len(result["candidate_regions"]),
                "low_confidence_tokens_skipped": result["low_confidence_tokens"],
                "review_reasons": record["review_reasons"],
                "error_codes": result["error_codes"],
            }
        )
        report["pages_seen"] += result["pages_seen"]
        report["pages_processed"] += result["pages_processed"]
        report["detected_sensitive_items"] += result["detected_sensitive_items"]
        report["candidate_regions_created"] += len(result["candidate_regions"])
        report["low_confidence_tokens_skipped"] += result["low_confidence_tokens"]
        detected_by_type.update(result["detected_by_type"])
        error_counts.update(result["error_codes"])
        if result["status"] == "ocr_precheck_failed":
            report["ocr_files_failed"] += 1
        else:
            report["ocr_files_processed"] += 1
        candidate_files.append(
            {
                "file_id": record["file_id"],
                "regions": result["candidate_regions"],
            }
        )
        report["files"].append(file_entry)

    report["detected_by_type"] = dict(sorted(detected_by_type.items()))
    report["error_code_counts"] = dict(sorted(error_counts.items()))
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    write_local_sensitive_json(
        candidates_path,
        {
            "schema_version": "0.5",
            "coordinate_system": "page_ratio",
            "source": "local_tesseract_ocr_precheck_candidates",
            "local_sensitive_file": True,
            "raw_values_recorded": False,
            "manual_confirmation_required": True,
            "candidate_coordinates_include_padding": False,
            "files": candidate_files,
            "note": (
                "Local OCR candidate regions only. Review every region before explicitly "
                "using visual-redact-pdf. Do not commit, upload, or share this file."
            ),
        },
    )
    return report


def load_pymupdf():
    try:
        import pymupdf  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "PyMuPDF is required for PDF visual redaction, preview grid, and OCR precheck modes. "
            "Use the dedicated local-redaction-assistant PDF venv."
        ) from exc
    return pymupdf


def empty_visual_redaction_report(render_dpi: int) -> dict:
    return {
        "mode": "visual-redact-pdf",
        "visual_redaction_files_attempted": 0,
        "visual_redaction_files_created": 0,
        "visual_redaction_failed_files": 0,
        "visual_redaction_skipped_files": 0,
        "validation_failed_files": 0,
        "manual_review_files": 0,
        "total_pages_rendered": 0,
        "total_regions_applied": 0,
        "render_dpi": render_dpi,
        "output_type": "image_based_pdf",
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "pdf_text_layer_preserved": False,
        "ocr_candidate_regions_source": False,
        "ocr_candidate_regions_confirmed": False,
        "manual_review_required": True,
        "error_code_counts": {},
        "files": [],
    }


def load_pdf_region_map(
    regions_path: Path | None,
    confirm_ocr_candidates: bool = False,
) -> tuple[dict[str, list[dict]], list[str], bool]:
    if regions_path is None:
        return {}, ["pdf_regions_config_missing"], False
    if not regions_path.exists():
        return {}, ["pdf_regions_config_missing"], False
    try:
        data = json.loads(regions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, ["pdf_regions_config_read_error"], False

    if not isinstance(data, dict):
        return {}, ["pdf_regions_config_schema_error"], False
    if data.get("schema_version") != "0.5" or data.get("coordinate_system") != "page_ratio":
        return {}, ["pdf_regions_config_schema_error"], False
    is_ocr_candidate_source = data.get("source") == "local_tesseract_ocr_precheck_candidates"
    if is_ocr_candidate_source and not confirm_ocr_candidates:
        return {}, ["pdf_ocr_candidates_not_confirmed"], True
    files = data.get("files")
    if not isinstance(files, list):
        return {}, ["pdf_regions_config_schema_error"], is_ocr_candidate_source

    region_map: dict[str, list[dict]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return {}, ["pdf_regions_config_schema_error"], is_ocr_candidate_source
        file_id = entry.get("file_id")
        regions = entry.get("regions", [])
        if not isinstance(file_id, str) or not isinstance(regions, list):
            return {}, ["pdf_regions_config_schema_error"], is_ocr_candidate_source
        region_map[file_id] = [region for region in regions if isinstance(region, dict)]
    return region_map, [], is_ocr_candidate_source


def load_image_region_map(regions_path: Path | None) -> tuple[dict[str, list[dict]], list[str]]:
    if regions_path is None or not regions_path.exists():
        return {}, ["image_regions_config_missing"]
    if regions_path.name != IMAGE_REGIONS_FILENAME:
        return {}, ["image_regions_config_schema_error"]
    try:
        data = json.loads(regions_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, ["image_regions_config_read_error"]
    if not isinstance(data, dict):
        return {}, ["image_regions_config_schema_error"]
    if data.get("schema_version") != "0.7" or data.get("coordinate_system") != "image_ratio":
        return {}, ["image_regions_config_schema_error"]
    files = data.get("files")
    if not isinstance(files, list):
        return {}, ["image_regions_config_schema_error"]
    region_map: dict[str, list[dict]] = {}
    for entry in files:
        if not isinstance(entry, dict):
            return {}, ["image_regions_config_schema_error"]
        file_id = entry.get("file_id")
        regions = entry.get("regions", [])
        if not isinstance(file_id, str) or not isinstance(regions, list):
            return {}, ["image_regions_config_schema_error"]
        region_map[file_id] = [region for region in regions if isinstance(region, dict)]
    return region_map, []


def validate_pdf_regions(regions: list[dict], page_count: int) -> list[str]:
    errors: set[str] = set()
    for region in regions:
        page = region.get("page")
        if not isinstance(page, int) or page < 1 or page > page_count:
            errors.add("pdf_region_page_invalid")
        for key in ("x0", "y0", "x1", "y1"):
            value = region.get(key)
            if not isinstance(value, (int, float)) or value < 0 or value > 1:
                errors.add("pdf_region_coordinate_invalid")
        x0 = region.get("x0")
        y0 = region.get("y0")
        x1 = region.get("x1")
        y1 = region.get("y1")
        if all(isinstance(value, (int, float)) for value in (x0, y0, x1, y1)):
            if x1 <= x0:
                errors.add("pdf_region_x_order_invalid")
            if y1 <= y0:
                errors.add("pdf_region_y_order_invalid")
    return sorted(errors)


def validate_image_regions(regions: list[dict]) -> list[str]:
    errors: set[str] = set()
    for region in regions:
        page = region.get("page", 1)
        if page != 1:
            errors.add("image_region_page_invalid")
        for key in ("x0", "y0", "x1", "y1"):
            value = region.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value > 1:
                errors.add("image_region_coordinate_invalid")
        x0 = region.get("x0")
        y0 = region.get("y0")
        x1 = region.get("x1")
        y1 = region.get("y1")
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (x0, y0, x1, y1)):
            if x1 <= x0:
                errors.add("image_region_x_order_invalid")
            if y1 <= y0:
                errors.add("image_region_y_order_invalid")
    return sorted(errors)


def pixel_rect_for_region(region: dict, width: int, height: int, padding_ratio: float, pymupdf) -> object:
    pad_x = max(0, round(width * padding_ratio))
    pad_y = max(0, round(height * padding_ratio))
    x0 = max(0, round(float(region["x0"]) * width) - pad_x)
    y0 = max(0, round(float(region["y0"]) * height) - pad_y)
    x1 = min(width, round(float(region["x1"]) * width) + pad_x)
    y1 = min(height, round(float(region["y1"]) * height) + pad_y)
    return pymupdf.IRect(x0, y0, x1, y1)


def empty_image_visual_redaction_report() -> dict:
    return {
        "mode": "visual-redact-image",
        "image_visual_redaction_files_attempted": 0,
        "image_visual_redaction_files_created": 0,
        "image_visual_redaction_files_failed": 0,
        "image_visual_redaction_files_skipped": 0,
        "validation_failed_files": 0,
        "total_regions_applied": 0,
        "output_type": "metadata_free_png",
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "image_metadata_preserved": False,
        "manual_review_required": True,
        "error_code_counts": {},
        "files": [],
    }


def validate_visual_image_output(
    output_path: Path,
    expected_width: int,
    expected_height: int,
    pymupdf,
) -> tuple[bool, list[str]]:
    if not output_path.exists():
        return False, ["image_visual_redaction_output_missing"]
    try:
        pixmap = pymupdf.Pixmap(str(output_path))
    except Exception:
        return False, ["image_visual_redaction_output_reopen_failed"]
    if pixmap.width != expected_width or pixmap.height != expected_height:
        return False, ["image_visual_redaction_dimensions_mismatch"]
    return True, []


def create_image_visual_redacted_copy(
    source_path: Path,
    redacted_dir: Path,
    file_id: str,
    regions: list[dict],
    padding_ratio: float,
    pymupdf,
) -> dict:
    temp_path = redacted_dir / f"{file_id}.tmp.png"
    final_path = redacted_dir / f"{file_id}_视觉脱敏版.png"
    result = {
        "status": "image_visual_redaction_failed",
        "output_file_created": False,
        "validation_succeeded": False,
        "error_codes": [],
        "regions_applied": 0,
    }
    region_errors = validate_image_regions(regions)
    if not regions:
        result["status"] = "image_visual_redaction_skipped_no_regions"
        return result
    if region_errors:
        result["status"] = "image_visual_redaction_skipped_invalid_regions"
        result["error_codes"].extend(region_errors)
        return result
    try:
        pixmap = pymupdf.Pixmap(str(source_path))
    except Exception:
        result["status"] = "image_visual_redaction_failed_image_unreadable"
        result["error_codes"].append("image_visual_redaction_failed_image_unreadable")
        return result
    if pixmap.width <= 0 or pixmap.height <= 0:
        result["status"] = "image_visual_redaction_failed_image_unreadable"
        result["error_codes"].append("image_visual_redaction_failed_image_unreadable")
        return result
    try:
        for region in regions:
            pixmap.set_rect(
                pixel_rect_for_region(region, pixmap.width, pixmap.height, padding_ratio, pymupdf),
                (0, 0, 0),
            )
            result["regions_applied"] += 1
        temp_path.unlink(missing_ok=True)
        pixmap.save(str(temp_path))
        validation_ok, validation_errors = validate_visual_image_output(
            temp_path,
            pixmap.width,
            pixmap.height,
            pymupdf,
        )
        if not validation_ok:
            result["status"] = "image_visual_redaction_failed_validation_error"
            result["error_codes"].extend(validation_errors)
            temp_path.unlink(missing_ok=True)
            return result
        final_path.unlink(missing_ok=True)
        temp_path.replace(final_path)
        result.update(
            {
                "status": "image_visual_redaction_copy_created",
                "output_file_created": True,
                "validation_succeeded": True,
            }
        )
        return result
    except Exception:
        result["status"] = "image_visual_redaction_failed_output_write_error"
        result["error_codes"].append("image_visual_redaction_failed_output_write_error")
        temp_path.unlink(missing_ok=True)
        return result


def validate_visual_pdf_output(output_path: Path, expected_pages: int, pymupdf) -> tuple[bool, list[str]]:
    if not output_path.exists():
        return False, ["visual_redaction_output_missing"]
    try:
        doc = pymupdf.open(output_path)
        try:
            if doc.page_count != expected_pages:
                return False, ["visual_redaction_page_count_mismatch"]
            extracted_parts: list[str] = []
            for page in doc:
                extracted_parts.append(page.get_text("text").strip())
            if "".join(extracted_parts).strip():
                return False, ["visual_redaction_output_text_layer_present"]
        finally:
            doc.close()
    except Exception:
        return False, ["visual_redaction_output_reopen_failed"]
    return True, []


def empty_preview_grid_report(preview_dpi: int, grid_cols: int, grid_rows: int) -> dict:
    return {
        "mode": "pdf-preview-grid",
        "preview_pdf_files_attempted": 0,
        "preview_pdf_files_processed": 0,
        "preview_pdf_files_failed": 0,
        "preview_pdf_files_skipped": 0,
        "validation_failed_files": 0,
        "preview_images_created": 0,
        "files_processed": 0,
        "total_pages_rendered": 0,
        "grid_columns": grid_cols,
        "grid_rows": grid_rows,
        "render_dpi": preview_dpi,
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "manual_review_required": True,
        "previews_are_local_sensitive_artifacts": True,
        "error_code_counts": {},
        "files": [],
    }


def grid_column_label(index: int) -> str:
    label = ""
    current = index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        label = chr(ord("A") + remainder) + label
    return label


def grid_column_index(label: str) -> int:
    value = 0
    for char in label.upper():
        if char < "A" or char > "Z":
            return 0
        value = value * 26 + (ord(char) - ord("A") + 1)
    return value


def parse_grid_cell(cell: str, grid_cols: int, grid_rows: int) -> tuple[int, int, str | None]:
    match = GRID_CELL_PATTERN.fullmatch(cell.strip())
    if not match:
        return 0, 0, "grid_cell_format_error"
    column_label, row_text = match.groups()
    column = grid_column_index(column_label)
    row = int(row_text)
    if column < 1 or column > grid_cols or row < 1 or row > grid_rows:
        return column, row, "grid_cell_out_of_bounds"
    return column, row, None


def is_safe_grid_region_label(label: str) -> bool:
    if not label or not GRID_REGION_LABEL_PATTERN.fullmatch(label):
        return False
    if not any(char.isalpha() for char in label):
        return False
    for detector_name in (
        "phone",
        "id_card",
        "email",
        "unified_social_credit_code",
        "bank_account",
        "license_plate",
        "url",
        "contract_number",
    ):
        if REGEX_PATTERNS[detector_name].search(label):
            return False
    return True


def empty_grid_regions_report() -> dict:
    return {
        "mode": "grid-regions-to-json",
        "files_seen_in_template": 0,
        "regions_requested": 0,
        "regions_written": 0,
        "grid_columns": None,
        "grid_rows": None,
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "coordinates_recorded_in_report": False,
        "grid_region_args_recorded": False,
        "labels_recorded_in_report": False,
        "manual_review_required": True,
        "error_code_counts": {},
        "files": [],
    }


def write_grid_regions_report(reports_dir: Path, report: dict, generated_at_utc: str | None = None) -> None:
    write_json(
        reports_dir / "grid_regions_report.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "generated_at_utc": generated_at_utc or datetime.now(timezone.utc).isoformat(),
            **report,
        },
    )


def load_grid_regions_template(template_path: Path) -> tuple[dict | None, list[str]]:
    if not template_path.exists():
        return None, ["grid_regions_template_missing"]
    try:
        data = json.loads(template_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["grid_regions_template_schema_error"]
    if data.get("schema_version") != "0.5" or data.get("coordinate_system") != "page_ratio":
        return None, ["grid_regions_template_schema_error"]
    files = data.get("files")
    if not isinstance(files, list):
        return None, ["grid_regions_template_schema_error"]
    grid_reference = data.get("grid_reference", {})
    if not isinstance(grid_reference, dict):
        return None, ["grid_regions_template_schema_error"]
    grid_columns = grid_reference.get("grid_columns")
    grid_rows = grid_reference.get("grid_rows")
    if not isinstance(grid_columns, int) or not isinstance(grid_rows, int):
        return None, ["grid_regions_template_schema_error"]
    if grid_columns < 1 or grid_columns > 52 or grid_rows < 1 or grid_rows > 100:
        return None, ["grid_regions_template_schema_error"]
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("file_id"), str):
            return None, ["grid_regions_template_schema_error"]
    return data, []


def parse_grid_region_arg(
    value: str,
    known_file_ids: set[str],
    grid_cols: int,
    grid_rows: int,
) -> tuple[dict | None, str | None]:
    parts = value.split(":")
    if len(parts) != 5:
        return None, "grid_region_arg_format_error"
    file_id, page_text, start_cell_text, end_cell_text, label = [part.strip() for part in parts]
    if not file_id or not page_text or not start_cell_text or not end_cell_text:
        return None, "grid_region_arg_format_error"
    if file_id not in known_file_ids:
        return None, "grid_region_file_id_unknown"
    try:
        page = int(page_text)
    except ValueError:
        return None, "grid_region_page_invalid"
    if page < 1:
        return None, "grid_region_page_invalid"
    if not is_safe_grid_region_label(label):
        return None, "grid_region_label_invalid"
    start_col, start_row, start_error = parse_grid_cell(start_cell_text, grid_cols, grid_rows)
    if start_error:
        return None, start_error
    end_col, end_row, end_error = parse_grid_cell(end_cell_text, grid_cols, grid_rows)
    if end_error:
        return None, end_error
    if end_col < start_col or end_row < start_row:
        return None, "grid_region_order_invalid"
    start_cell = f"{grid_column_label(start_col)}{start_row}"
    end_cell = f"{grid_column_label(end_col)}{end_row}"
    return (
        {
            "file_id": file_id,
            "page": page,
            "x0": round((start_col - 1) / grid_cols, 6),
            "y0": round((start_row - 1) / grid_rows, 6),
            "x1": round(end_col / grid_cols, 6),
            "y1": round(end_row / grid_rows, 6),
            "label": label,
            "grid_hint": f"{start_cell}:{end_cell}",
        },
        None,
    )


def apply_grid_regions_to_json(
    output_path: Path,
    template_path: Path,
    regions_output_path: Path,
    grid_region_args: list[str],
    overwrite_regions: bool,
    cli_grid_cols: int,
    cli_grid_rows: int,
) -> int:
    reports_dir = output_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path.mkdir(parents=True, exist_ok=True)
    (output_path / OUTPUT_MARKER).write_text("local-redaction-assistant\n", encoding="utf-8")

    report = empty_grid_regions_report()
    error_counts: Counter[str] = Counter()
    generated_at = datetime.now(timezone.utc).isoformat()

    template, template_errors = load_grid_regions_template(template_path)
    if template_errors:
        for error_code in template_errors:
            error_counts[error_code] += 1
        report["error_code_counts"] = dict(sorted(error_counts.items()))
        write_grid_regions_report(reports_dir, report, generated_at)
        raise SystemExit(1)

    assert template is not None
    template_grid = template["grid_reference"]
    template_grid_cols = int(template_grid["grid_columns"])
    template_grid_rows = int(template_grid["grid_rows"])
    grid_cols = cli_grid_cols if cli_grid_cols != 12 else template_grid_cols
    grid_rows = cli_grid_rows if cli_grid_rows != 18 else template_grid_rows
    report["grid_columns"] = grid_cols
    report["grid_rows"] = grid_rows

    template_files = template.get("files", [])
    known_file_ids = {
        entry["file_id"]
        for entry in template_files
        if isinstance(entry, dict) and isinstance(entry.get("file_id"), str)
    }
    report["files_seen_in_template"] = len(known_file_ids)
    report["files"] = [
        {
            "file_id": entry["file_id"],
            "regions_written": 0,
            "status": "regions_json_pending",
            "review_required": True,
            "review_reasons": [
                "manual_grid_regions_converted",
                "visual_redaction_requires_manual_review",
            ],
        }
        for entry in template_files
        if isinstance(entry, dict) and isinstance(entry.get("file_id"), str)
    ]
    file_entries = {entry["file_id"]: entry for entry in report["files"]}

    if not grid_region_args:
        error_counts["grid_region_arg_missing"] += 1
    if regions_output_path.exists() and not overwrite_regions:
        error_counts["grid_regions_output_exists"] += 1

    parsed_regions: list[dict] = []
    for grid_region_arg in grid_region_args:
        region, error_code = parse_grid_region_arg(
            grid_region_arg,
            known_file_ids,
            grid_cols,
            grid_rows,
        )
        report["regions_requested"] += 1
        if error_code:
            error_counts[error_code] += 1
            continue
        assert region is not None
        parsed_regions.append(region)

    if error_counts:
        report["error_code_counts"] = dict(sorted(error_counts.items()))
        for entry in report["files"]:
            entry["status"] = "regions_json_not_created"
        write_grid_regions_report(reports_dir, report, generated_at)
        raise SystemExit(1)

    regions_by_file: dict[str, list[dict]] = {file_id: [] for file_id in known_file_ids}
    for region in parsed_regions:
        file_id = region.pop("file_id")
        regions_by_file[file_id].append(region)

    output_data = {
        "schema_version": "0.5",
        "coordinate_system": "page_ratio",
        "grid_reference": {
            "grid_columns": grid_cols,
            "grid_rows": grid_rows,
            "source": "pdf-preview-grid",
        },
        "local_sensitive_file": True,
        "source": "grid-regions-to-json",
        "files": [
            {
                "file_id": file_id,
                "regions": regions_by_file.get(file_id, []),
            }
            for file_id in sorted(known_file_ids)
        ],
    }
    try:
        regions_output_path.parent.mkdir(parents=True, exist_ok=True)
        write_local_sensitive_json(regions_output_path, output_data)
    except OSError:
        error_counts["grid_regions_output_write_error"] += 1
        report["error_code_counts"] = dict(sorted(error_counts.items()))
        for entry in report["files"]:
            entry["status"] = "regions_json_not_created"
        write_grid_regions_report(reports_dir, report, generated_at)
        raise SystemExit(1)

    for file_id, regions in regions_by_file.items():
        if file_id in file_entries:
            file_entries[file_id]["regions_written"] = len(regions)
            file_entries[file_id]["status"] = "regions_json_created" if regions else "regions_json_created_no_regions"
    report["regions_written"] = len(parsed_regions)
    report["error_code_counts"] = {}
    write_grid_regions_report(reports_dir, report, generated_at)
    print(
        "grid regions written: pdf_redaction_regions.local.json "
        f"(regions_written={len(parsed_regions)}, raw_values_recorded=false)"
    )
    print("pdf_redaction_regions.local.json is local-sensitive; do not commit, upload, or share it.")
    return 0


def validate_preview_png(path: Path, pymupdf) -> tuple[bool, list[str]]:
    if not path.exists():
        return False, ["preview_grid_png_missing"]
    try:
        pixmap = pymupdf.Pixmap(str(path))
        if pixmap.width <= 0 or pixmap.height <= 0:
            return False, ["preview_grid_png_invalid"]
    except Exception:
        return False, ["preview_grid_png_open_failed"]
    return True, []


def render_pdf_grid_preview_page(
    page,
    output_path: Path,
    file_id: str,
    page_number: int,
    grid_cols: int,
    grid_rows: int,
    preview_dpi: int,
    pymupdf,
) -> tuple[bool, list[str]]:
    matrix = pymupdf.Matrix(preview_dpi / 72, preview_dpi / 72)
    try:
        base_pixmap = page.get_pixmap(matrix=matrix, alpha=False)
    except Exception:
        return False, ["preview_grid_render_error"]

    preview_doc = pymupdf.open()
    try:
        preview_page = preview_doc.new_page(width=base_pixmap.width, height=base_pixmap.height)
        preview_page.insert_image(preview_page.rect, stream=base_pixmap.tobytes("png"))
        width = float(base_pixmap.width)
        height = float(base_pixmap.height)
        cell_width = width / grid_cols
        cell_height = height / grid_rows
        line_color = (1, 0, 0)
        label_color = (1, 0, 0)
        line_width = max(0.75, min(width, height) / 1200)
        label_size = max(7, min(18, min(cell_width, cell_height) * 0.22))

        for col in range(grid_cols + 1):
            x = col * cell_width
            preview_page.draw_line((x, 0), (x, height), color=line_color, width=line_width)
            if col < grid_cols:
                label = grid_column_label(col + 1)
                preview_page.insert_text(
                    (x + 3, min(height - 3, label_size + 4)),
                    label,
                    fontsize=label_size,
                    color=label_color,
                )

        for row in range(grid_rows + 1):
            y = row * cell_height
            preview_page.draw_line((0, y), (width, y), color=line_color, width=line_width)
            if row < grid_rows:
                label = str(row + 1)
                preview_page.insert_text(
                    (3, min(height - 3, y + label_size + 4)),
                    label,
                    fontsize=label_size,
                    color=label_color,
                )

        footer_size = max(7, min(14, min(cell_width, cell_height) * 0.18))
        preview_page.insert_text(
            (3, max(footer_size + 4, height - footer_size - 3)),
            f"{file_id} page {page_number:03d} grid {grid_cols}x{grid_rows}",
            fontsize=footer_size,
            color=label_color,
        )
        grid_pixmap = preview_page.get_pixmap(alpha=False)
        grid_pixmap.save(str(output_path))
    except Exception:
        return False, ["preview_grid_png_write_error"]
    finally:
        preview_doc.close()

    return validate_preview_png(output_path, pymupdf)


def create_pdf_preview_grid_outputs(
    source_path: Path,
    previews_dir: Path,
    file_id: str,
    grid_cols: int,
    grid_rows: int,
    preview_dpi: int,
    pymupdf,
) -> dict:
    result = {
        "status": "preview_grid_failed",
        "pages_seen": 0,
        "preview_pages_created": 0,
        "validation_succeeded": False,
        "preview_relative_paths": [],
        "error_codes": [],
    }
    try:
        source_doc = pymupdf.open(source_path)
    except Exception:
        result["status"] = "preview_grid_failed_pdf_unreadable"
        result["error_codes"].append("preview_grid_failed_pdf_unreadable")
        return result

    try:
        page_count = source_doc.page_count
        result["pages_seen"] = page_count
        if page_count <= 0:
            result["status"] = "preview_grid_failed_no_pages"
            result["error_codes"].append("preview_grid_failed_no_pages")
            return result

        preview_paths: list[str] = []
        for page_number in range(1, page_count + 1):
            page = source_doc.load_page(page_number - 1)
            output_name = f"{file_id}_page_{page_number:03d}_grid.png"
            output_path = previews_dir / output_name
            ok, errors = render_pdf_grid_preview_page(
                page,
                output_path,
                file_id,
                page_number,
                grid_cols,
                grid_rows,
                preview_dpi,
                pymupdf,
            )
            if not ok:
                result["status"] = "preview_grid_failed_render_error"
                result["error_codes"].extend(errors)
                for relative_path in preview_paths:
                    (previews_dir.parent / relative_path).unlink(missing_ok=True)
                return result
            preview_paths.append(f"previews/{output_name}")
            result["preview_pages_created"] += 1

        result.update(
            {
                "status": "preview_grid_created",
                "validation_succeeded": True,
                "preview_relative_paths": preview_paths,
            }
        )
        return result
    except Exception:
        result["status"] = "preview_grid_failed_render_error"
        result["error_codes"].append("preview_grid_failed_render_error")
        return result
    finally:
        try:
            source_doc.close()
        except Exception:
            pass


def create_pdf_visual_redacted_copy(
    source_path: Path,
    redacted_dir: Path,
    file_id: str,
    regions: list[dict],
    render_dpi: int,
    padding_ratio: float,
    pymupdf,
) -> dict:
    temp_path = redacted_dir / f"{file_id}.tmp.pdf"
    final_path = redacted_dir / f"{file_id}_视觉脱敏版.pdf"
    result = {
        "status": "visual_redaction_failed",
        "output_file_created": False,
        "validation_succeeded": False,
        "error_codes": [],
        "pages_seen": 0,
        "pages_rendered": 0,
        "regions_applied": 0,
    }

    try:
        source_doc = pymupdf.open(source_path)
    except Exception:
        result["status"] = "visual_redaction_failed_pdf_unreadable"
        result["error_codes"].append("visual_redaction_failed_pdf_unreadable")
        return result

    output_doc = pymupdf.open()
    try:
        page_count = source_doc.page_count
        result["pages_seen"] = page_count
        region_errors = validate_pdf_regions(regions, page_count)
        if not regions:
            result["status"] = "visual_redaction_skipped_no_regions"
            return result
        if region_errors:
            result["status"] = "visual_redaction_skipped_invalid_regions"
            result["error_codes"].extend(region_errors)
            return result

        regions_by_page: dict[int, list[dict]] = {}
        for region in regions:
            regions_by_page.setdefault(int(region["page"]), []).append(region)

        matrix = pymupdf.Matrix(render_dpi / 72, render_dpi / 72)
        for page_number in range(1, page_count + 1):
            page = source_doc.load_page(page_number - 1)
            try:
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            except Exception:
                result["status"] = "visual_redaction_failed_render_error"
                result["error_codes"].append("visual_redaction_failed_render_error")
                return result

            for region in regions_by_page.get(page_number, []):
                rect = pixel_rect_for_region(
                    region,
                    pixmap.width,
                    pixmap.height,
                    padding_ratio,
                    pymupdf,
                )
                pixmap.set_rect(rect, (0, 0, 0))
                result["regions_applied"] += 1

            image_stream = pixmap.tobytes("png")
            output_page = output_doc.new_page(width=page.rect.width, height=page.rect.height)
            output_page.insert_image(output_page.rect, stream=image_stream)
            result["pages_rendered"] += 1

        if temp_path.exists():
            temp_path.unlink()
        try:
            output_doc.save(temp_path, garbage=4, deflate=True)
        except Exception:
            result["status"] = "visual_redaction_failed_output_write_error"
            result["error_codes"].append("visual_redaction_failed_output_write_error")
            temp_path.unlink(missing_ok=True)
            return result

        validation_ok, validation_errors = validate_visual_pdf_output(temp_path, page_count, pymupdf)
        if not validation_ok:
            result["status"] = "visual_redaction_failed_validation_error"
            result["error_codes"].extend(validation_errors)
            temp_path.unlink(missing_ok=True)
            return result

        if final_path.exists():
            final_path.unlink()
        temp_path.replace(final_path)
        result.update(
            {
                "status": "visual_redaction_copy_created",
                "output_file_created": True,
                "validation_succeeded": True,
            }
        )
        return result
    except Exception:
        result["status"] = "visual_redaction_failed_output_write_error"
        result["error_codes"].append("visual_redaction_failed_output_write_error")
        temp_path.unlink(missing_ok=True)
        return result
    finally:
        output_doc.close()
        try:
            source_doc.close()
        except Exception:
            pass


def review_reasons_for(path: Path, status: str, detection: dict, mode: str) -> list[str]:
    ext = path.suffix.lower()
    reasons: list[str] = []
    if status == "skipped_unsupported_type":
        reasons.append("unsupported_file_type")
    if status == "skipped_too_large":
        reasons.append("file_exceeds_size_limit")
    if mode == "pdf-preview-grid":
        if ext == ".pdf":
            reasons.extend(
                [
                    "preview_grid_contains_rendered_pdf_pages",
                    "local_sensitive_preview_output",
                    "manual_region_selection_required",
                ]
            )
        else:
            reasons.append("preview_grid_skipped_non_pdf")
        if detection["read_error"]:
            reasons.append("detector_read_error")
        return reasons
    if mode == "docx-candidate-precheck":
        if ext == ".docx":
            reasons.extend(
                [
                    "docx_context_candidates_require_manual_confirmation",
                    "docx_candidate_terms_local_sensitive",
                ]
            )
        else:
            reasons.append("docx_candidate_precheck_skipped_non_docx")
        return reasons
    if mode == "visual-redact-pdf":
        if ext == ".pdf":
            reasons.extend(
                [
                    "destructive_visual_redaction_copy",
                    "not_true_pdf_redaction",
                    "manual_review_required_before_sharing",
                    "pdf_regions_required",
                ]
            )
        else:
            reasons.append("visual_redaction_skipped_non_pdf")
        if detection["read_error"]:
            reasons.append("detector_read_error")
        return reasons
    if mode == "visual-redact-image":
        if ext in IMAGE_EXTENSIONS:
            reasons.extend(
                [
                    "destructive_visual_image_redaction_copy",
                    "manual_review_required_before_sharing",
                    "image_regions_required",
                ]
            )
        else:
            reasons.append("visual_image_redaction_skipped_non_image")
        return reasons
    if mode == "region-selector":
        if ext in {".pdf", *IMAGE_EXTENSIONS}:
            reasons.extend(
                [
                    "loopback_manual_region_selection_only",
                    "manual_region_selection_required",
                    "local_sensitive_regions_output",
                ]
            )
        else:
            reasons.append("region_selector_skipped_non_supported_media")
        return reasons
    if mode == "ocr-precheck":
        if ext in OCR_EXTENSIONS:
            reasons.extend(
                [
                    "local_ocr_precheck_only",
                    "ocr_candidates_require_manual_review",
                    "ocr_text_not_persisted",
                ]
            )
        else:
            reasons.append("ocr_precheck_skipped_non_ocr")
        return reasons
    if ext == ".pdf":
        reasons.append("pdf_true_redaction_not_implemented_v0_3_precheck")
        reasons.append("scan_or_text_pdf_type_not_verified_v0_3_precheck")
    elif ext in IMAGE_EXTENSIONS:
        reasons.append("image_requires_explicit_ocr_precheck")
        reasons.append("low_ocr_confidence_review_required")
    elif ext == ".xlsx":
        reasons.append("xlsx_visible_cells_read_only_detector_stage")
        if mode == "redact":
            reasons.append("xlsx_redaction_not_implemented_v0_3_mvp_a")
        else:
            reasons.append("xlsx_no_redaction_generated")
        reasons.extend(detection["xlsx_review_reasons"])
    elif ext == ".docx":
        reasons.append("docx_main_body_read_only_detector_stage")
        if mode == "redact":
            reasons.append("docx_redaction_copy_clean_main_body_only")
        else:
            reasons.append("docx_no_redaction_generated")
        reasons.extend(detection["docx_review_reasons"])
    elif ext == ".txt":
        reasons.append("txt_detector_stage_not_formal_project_support")
    if detection["read_error"]:
        reasons.append("detector_read_error")
    if detection["detected_sensitive_items"] > 0:
        reasons.append("masked_findings_require_manual_review")
    if not reasons:
        reasons.append("content_detection_not_implemented_v0_3_detector")
    return reasons


def detector_scope_for(ext: str) -> str:
    if ext == ".txt":
        return "txt_detector_stage_not_formal_project_support"
    if ext == ".docx":
        return "docx_main_body_read_only_detector_stage"
    if ext == ".xlsx":
        return "xlsx_visible_cells_read_only_detector_stage"
    return "metadata_only"


def planned_action_for(mode: str, ext: str) -> str:
    if mode == "docx-candidate-precheck" and ext == ".docx":
        return "generate_local_docx_candidate_terms_for_manual_confirmation"
    if mode == "pdf-preview-grid" and ext == ".pdf":
        return "generate_pdf_preview_grid"
    if mode == "region-selector" and (ext == ".pdf" or ext in IMAGE_EXTENSIONS):
        return "open_loopback_manual_region_selector"
    if mode == "visual-redact-pdf" and ext == ".pdf":
        return "create_image_based_pdf_visual_redaction_copy"
    if mode == "visual-redact-image" and ext in IMAGE_EXTENSIONS:
        return "create_image_visual_redaction_copy"
    if mode == "ocr-precheck" and ext in OCR_EXTENSIONS:
        return "run_local_ocr_precheck"
    if mode in {
        "visual-redact-pdf",
        "visual-redact-image",
        "pdf-preview-grid",
        "region-selector",
        "ocr-precheck",
    }:
        return "manual_review_unsupported_in_selected_mode"
    if mode == "dry-run":
        return "report_only"
    return "create_docx_redacted_copy" if ext == ".docx" else "manual_review_before_redaction"


def detector_scope_for_mode(mode: str, ext: str) -> str:
    if mode == "docx-candidate-precheck" and ext == ".docx":
        return "docx_context_candidate_precheck"
    if mode == "pdf-preview-grid" and ext == ".pdf":
        return "pdf_preview_grid_no_detector"
    if mode == "region-selector" and (ext == ".pdf" or ext in IMAGE_EXTENSIONS):
        return "loopback_manual_region_selector"
    if mode == "visual-redact-pdf" and ext == ".pdf":
        return "pdf_visual_redaction_manual_regions"
    if mode == "visual-redact-image" and ext in IMAGE_EXTENSIONS:
        return "image_visual_redaction_manual_regions"
    if mode == "ocr-precheck" and ext in OCR_EXTENSIONS:
        return "local_tesseract_ocr_precheck"
    return detector_scope_for(ext)


def build_file_record(
    index: int,
    path: Path,
    input_base: Path,
    mode: str,
    max_bytes: int,
    detector_config: dict,
) -> dict:
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        size = None

    supported = ext in SUPPORTED_EXTENSIONS
    if mode == "pdf-preview-grid":
        supported = ext == ".pdf"
    elif mode == "docx-candidate-precheck":
        supported = ext == ".docx"
    elif mode == "region-selector":
        supported = ext == ".pdf" or ext in IMAGE_EXTENSIONS
    elif mode == "ocr-precheck":
        supported = ext in OCR_EXTENSIONS
    if not supported:
        status = "skipped_unsupported_type"
    elif size is not None and size > max_bytes:
        status = "skipped_too_large"
    elif mode == "pdf-preview-grid":
        status = "preview_grid_pending" if ext == ".pdf" else "preview_grid_skipped_non_pdf"
    elif mode == "docx-candidate-precheck":
        status = "docx_candidate_precheck_pending" if ext == ".docx" else "docx_candidate_precheck_skipped_non_docx"
    elif mode == "visual-redact-pdf":
        status = "visual_redaction_pending" if ext == ".pdf" else "visual_redaction_skipped_non_pdf"
    elif mode == "visual-redact-image":
        status = "image_visual_redaction_pending" if ext in IMAGE_EXTENSIONS else "image_visual_redaction_skipped_non_image"
    elif mode == "region-selector":
        status = "region_selector_pending" if ext in {".pdf", *IMAGE_EXTENSIONS} else "region_selector_skipped_non_supported_media"
    elif mode == "ocr-precheck":
        status = "ocr_precheck_pending" if ext in OCR_EXTENSIONS else "ocr_precheck_skipped_non_ocr"
    else:
        if mode == "dry-run":
            status = "precheck_only"
        elif ext == ".docx":
            status = "redaction_pending"
        else:
            status = "redaction_not_implemented_v0_3_mvp_a"

    if mode in {
        "visual-redact-pdf",
        "visual-redact-image",
        "pdf-preview-grid",
        "region-selector",
        "ocr-precheck",
        "docx-candidate-precheck",
    }:
        detection = empty_detection_result()
    elif supported and status not in {"skipped_too_large", "skipped_unsupported_type"}:
        detection = detect_file(path, detector_config)
    else:
        detection = empty_detection_result()

    reasons = review_reasons_for(path, status, detection, mode)
    return {
        "file_id": f"F{index:06d}",
        "file_type": ext.lstrip(".") or "unknown",
        "size_bucket": size_bucket(size),
        "supported_type": supported,
        "status": status,
        "planned_action": planned_action_for(mode, ext),
        "detector_scope": detector_scope_for_mode(mode, ext),
        "detected_sensitive_items": detection["detected_sensitive_items"],
        "detected_by_type": detection["detected_by_type"],
        "findings_count": detection["detected_sensitive_items"],
        "finding_types": sorted(detection["detected_by_type"]),
        "masked_samples": detection["masked_samples"],
        "txt_detector_test_content_read": ext == ".txt" and detection["content_read"],
        "office_document_body_read": ext in {".docx", ".xlsx"} and detection["office_document_body_read"],
        "docx_main_body_read_only": ext == ".docx" and detection["docx_main_body_read"],
        "docx_non_main_parts_read": False,
        "xlsx_visible_cells_read_only": ext == ".xlsx" and detection["xlsx_visible_cells_read"],
        "xlsx_hidden_sheets_read": False,
        "xlsx_formulas_read": False,
        "xlsx_comments_read": False,
        "xlsx_hyperlink_targets_read": False,
        "xlsx_images_or_objects_read": False,
        "ocr_pages_processed": 0,
        "ocr_low_confidence_tokens_skipped": 0,
        "raw_values_recorded": False,
        "review_required": True,
        "review_reasons": reasons,
    }


def build_file_index_record(index: int, path: Path, input_base: Path) -> dict:
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    return {
        "file_id": f"F{index:06d}",
        "original_filename": path.name,
        "original_relative_path": safe_relative_path(path, input_base),
        "original_absolute_path_included": False,
        "path_hash": safe_path_hash(path, input_base),
        "size_bytes": size,
    }


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_local_sensitive_json(path: Path, data: dict) -> None:
    """Atomically write a local-only JSON artifact with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError:
        temporary_path.unlink(missing_ok=True)
        raise


def apply_region_selector_mode(
    input_path: Path,
    output_path: Path,
    pdf_regions_path: Path | None,
    image_regions_path: Path | None,
    preview_dpi: int,
    selector_port: int,
    selector_timeout_seconds: int,
    open_browser: bool,
    overwrite_regions: bool,
    detector_config: dict,
) -> int:
    """Run the one-file loopback selector and write safe, content-free reports."""
    validate_region_selector_input(input_path)
    source_ext = input_path.suffix.lower()
    is_pdf = source_ext == ".pdf"
    expected_filename = PDF_REGIONS_FILENAME if is_pdf else IMAGE_REGIONS_FILENAME
    regions_path = (
        (pdf_regions_path if is_pdf else image_regions_path)
        or output_path / expected_filename
    )
    validate_selector_regions_path(output_path, regions_path, expected_filename)

    reports_dir = output_path / "reports"
    redacted_dir = output_path / "redacted_files"
    logs_dir = output_path / "logs"
    for directory in (reports_dir, redacted_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    (output_path / OUTPUT_MARKER).write_text("local-redaction-assistant\n", encoding="utf-8")

    generated_at = datetime.now(timezone.utc).isoformat()
    record = build_file_record(
        1,
        input_path,
        input_path.parent,
        "region-selector",
        500 * 1024 * 1024,
        detector_config,
    )
    selector_result: dict = {
        "status": "region_selector_failed",
        "regions_saved": 0,
        "pages_rendered": 0,
        "page_count": 0,
        "source_kind": "pdf" if is_pdf else "image",
    }
    error_codes: list[str] = []
    try:
        selector_result = run_region_selector(
            input_path,
            record["file_id"],
            regions_path,
            preview_dpi,
            selector_port,
            selector_timeout_seconds,
            open_browser,
            overwrite_regions,
        )
    except RegionSelectorError as exc:
        error_codes.append(str(exc) or "region_selector_failed")

    status = str(selector_result["status"])
    record["status"] = status
    record["planned_action"] = (
        "explicitly_confirm_visual_redaction_after_manual_selection"
        if status == "region_selector_regions_saved"
        else "manual_review_region_selection_not_completed"
    )
    record["review_reasons"] = list(
        dict.fromkeys(
            record["review_reasons"]
            + ["manual_review_required_before_redaction"]
            + (error_codes if error_codes else [])
        )
    )

    selector_report = {
        "tool": "local-redaction-assistant",
        "version": VERSION,
        "generated_at_utc": generated_at,
        "mode": "region-selector",
        "selector_bound_to_loopback_only": True,
        "browser_open_requested": open_browser,
        "files_attempted": 1,
        "regions_saved": int(selector_result.get("regions_saved", 0)),
        "pages_rendered_for_selection": int(selector_result.get("pages_rendered", 0)),
        "source_type": "pdf" if is_pdf else "image",
        "status_counts": {status: 1},
        "raw_values_recorded": False,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "coordinates_recorded_in_report": False,
        "labels_recorded_in_report": False,
        "manual_review_required": True,
        "error_code_counts": dict(sorted(Counter(error_codes).items())),
        "files": [
            {
                "file_id": record["file_id"],
                "file_type": record["file_type"],
                "status": status,
                "regions_saved": int(selector_result.get("regions_saved", 0)),
                "pages_rendered_for_selection": int(selector_result.get("pages_rendered", 0)),
                "review_required": True,
                "review_reasons": record["review_reasons"],
                "error_codes": error_codes,
            }
        ],
    }
    summary = {
        "tool": "local-redaction-assistant",
        "version": VERSION,
        "generated_at_utc": generated_at,
        "mode": "region-selector",
        "safety": {
            "originals_modified": False,
            "network_used": False,
            "external_api_used": False,
            "loopback_browser_used": True,
            "pdf_or_image_content_rendered_for_manual_selection": True,
            "raw_sensitive_values_recorded": False,
            "source_names_recorded": False,
            "source_paths_recorded": False,
            "stable_file_identifier_recorded": False,
            "precise_size_recorded": False,
            "local_file_index_saved": True,
            "human_review_required": True,
        },
        "totals": {
            "files_seen": 1,
            "supported_files": 1,
            "unsupported_files": 0,
            "symlink_files_skipped": 0,
            "symlink_dirs_skipped": 0,
            "detected_sensitive_items": 0,
            "files_with_findings": 0,
            "redacted_files_created": 0,
            "manual_review_files": 1,
        },
        "detected_by_type": {},
        "masked_samples_by_type": {},
        "status_counts": {status: 1},
        "review_reason_counts": dict(sorted(Counter(record["review_reasons"]).items())),
        "files": [record],
        "notes": [
            "region-selector renders only the user-selected local PDF or image to a loopback browser session.",
            "The selector does not OCR, auto-detect sensitive information, generate a redacted copy, or inspect saved regions after selection.",
            "The regions JSON is local-sensitive and must not be committed, uploaded, or shared with Codex for real projects.",
            "A separate explicit visual-redact-pdf or visual-redact-image command is required to create a copy.",
        ],
    }
    write_json(reports_dir / "summary.json", summary)
    write_json(reports_dir / "precheck_report.json", build_precheck_report(summary))
    write_manual_review_csv(reports_dir / "manual_review_list.csv", [record])
    write_run_summary_md(reports_dir / "run_summary.md", summary)
    write_json(reports_dir / "region_selector_report.json", selector_report)
    write_local_sensitive_json(
        output_path / "file_index.local.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "local_sensitive_file": True,
            "original_absolute_paths_included": False,
            "raw_sensitive_values_recorded": False,
            "entries": [build_file_index_record(1, input_path, input_path.parent)],
            "note": "Local index for lawyer-side file lookup only. Do not commit or share.",
        },
    )
    print(
        "region selector finished: "
        f"status={status}, regions_saved={selector_report['regions_saved']}, raw_values_recorded=false"
    )
    print("file_index.local.json and selected regions are local-sensitive; do not commit, upload, or share them.")
    return 0 if status in {"region_selector_regions_saved", "region_selector_cancelled", "region_selector_timed_out"} else 2


def build_precheck_report(summary: dict) -> dict:
    allowed_keys = (
        "tool",
        "version",
        "generated_at_utc",
        "mode",
        "redaction_profile",
        "totals",
        "detected_by_type",
        "masked_samples_by_type",
        "status_counts",
        "review_reason_counts",
        "files",
        "safety",
        "notes",
    )
    return {key: summary[key] for key in allowed_keys if key in summary}


def encode_csv_cell(value: object) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_manual_review_csv(path: Path, files: list[dict]) -> None:
    fields = [
        "file_id",
        "file_type",
        "detector_scope",
        "size_bucket",
        "findings_count",
        "finding_types",
        "masked_samples",
        "review_required",
        "review_reasons",
        "planned_action",
        "status",
    ]
    with path.open("w", encoding="utf-8", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fields)
        writer.writeheader()
        for record in files:
            writer.writerow({field: encode_csv_cell(record.get(field, "")) for field in fields})


def format_count_lines(counts: dict[str, int]) -> list[str]:
    if not counts:
        return ["- none"]
    return [f"- {key}: {counts[key]}" for key in sorted(counts)]


def write_run_summary_md(path: Path, summary: dict) -> None:
    totals = summary["totals"]
    safety = summary["safety"]
    if summary["mode"] == "redact":
        mode_reminders = [
            "- v0.3 MVP-A creates DOCX redacted copies only for supported ordinary w:t matches.",
            "- XLSX, PDF, OCR, and image redaction are not implemented.",
            "- Generated DOCX copies still require lawyer review before external sharing.",
        ]
    elif summary["mode"] == "docx-candidate-precheck":
        mode_reminders = [
            "- docx-candidate-precheck writes local-only contextual candidate terms for lawyer confirmation.",
            "- It does not auto-approve candidate terms or create a redacted copy.",
            "- Candidate terms are local-sensitive and must not be committed, uploaded, or shared.",
        ]
    elif summary["mode"] == "visual-redact-pdf":
        mode_reminders = [
            "- visual-redact-pdf creates image-based PDF visual redaction copies from local region config only.",
            "- This is not true PDF redaction and does not use OCR or automatic sensitive information detection.",
            "- Generated PDF copies still require lawyer review before external sharing.",
        ]
    elif summary["mode"] == "visual-redact-image":
        mode_reminders = [
            "- visual-redact-image creates a metadata-free PNG copy only from explicit local manual regions.",
            "- It does not OCR, auto-detect sensitive information, or create a PDF.",
            "- Generated image copies still require lawyer review before external sharing.",
        ]
    elif summary["mode"] == "pdf-preview-grid":
        mode_reminders = [
            "- pdf-preview-grid creates local-sensitive grid PNG previews only.",
            "- This mode does not OCR, auto-detect sensitive information, or create redacted PDFs.",
            "- Preview images must be reviewed locally and must not be committed, uploaded, or shared.",
        ]
    elif summary["mode"] == "region-selector":
        mode_reminders = [
            "- region-selector opens a token-protected loopback browser session for one PDF or image at a time.",
            "- It does not OCR, auto-detect sensitive information, or create a redacted copy.",
            "- Selected regions are local-sensitive; after saving them, separately confirm visual redaction before any copy is created.",
        ]
    elif summary["mode"] == "ocr-precheck":
        mode_reminders = [
            "- ocr-precheck uses the configured local Tesseract engine and does not call cloud OCR.",
            "- OCR text is processed in memory and is not persisted in reports or logs.",
            "- OCR candidate regions are local-sensitive suggestions and require manual confirmation.",
            "- This mode does not create a redacted PDF; invoke visual-redact-pdf explicitly after review.",
        ]
    else:
        mode_reminders = [
            "- dry-run mode does not create redacted copies.",
            "- DOCX and XLSX read-only parsing remains detector-stage precheck.",
            "- PDF redaction, OCR, and image handling are not implemented.",
        ]
    lines = [
        "# local-redaction-assistant Run Summary",
        "",
        f"- mode: {summary['mode']}",
        f"- files_seen: {totals['files_seen']}",
        f"- supported_files: {totals['supported_files']}",
        f"- files_with_findings: {totals['files_with_findings']}",
        f"- detected_sensitive_items: {totals['detected_sensitive_items']}",
        f"- manual_review_files: {totals['manual_review_files']}",
        f"- symlink_files_skipped: {totals['symlink_files_skipped']}",
        f"- symlink_dirs_skipped: {totals['symlink_dirs_skipped']}",
        "",
        "## Detected By Type",
        "",
        *format_count_lines(summary["detected_by_type"]),
        "",
        "## Safety Boundary Summary",
        "",
        f"- originals_modified: {str(safety['originals_modified']).lower()}",
        f"- redacted_files_created: {totals['redacted_files_created']}",
        f"- network_used: {str(safety['network_used']).lower()}",
        f"- external_api_used: {str(safety['external_api_used']).lower()}",
        f"- raw_sensitive_values_recorded: {str(safety['raw_sensitive_values_recorded']).lower()}",
        f"- source_names_recorded: {str(safety['source_names_recorded']).lower()}",
        f"- source_paths_recorded: {str(safety['source_paths_recorded']).lower()}",
        f"- stable_file_identifier_recorded: {str(safety['stable_file_identifier_recorded']).lower()}",
        f"- precise_size_recorded: {str(safety['precise_size_recorded']).lower()}",
        f"- human_review_required: {str(safety['human_review_required']).lower()}",
        "",
        "## Manual Review Reminders",
        "",
        *mode_reminders,
        "- Review all files before external sharing; this report does not replace lawyer review.",
        "- Use file_index.local.json only on the local machine for file lookup; do not commit or share it.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def make_empty_redaction_report() -> dict:
    return {
        "redacted_files_created": 0,
        "redaction_attempted_files": 0,
        "redaction_succeeded_files": 0,
        "redaction_failed_files": 0,
        "redaction_skipped_files": 0,
        "validation_failed_files": 0,
        "manual_review_files": 0,
        "redaction_by_type": {},
        "error_code_counts": {},
        "files": [],
    }


def apply_docx_redactions(
    paths: list[Path],
    files: list[dict],
    redacted_dir: Path,
    detector_config: dict,
    placeholder_state: dict,
    word_validation: str = "off",
) -> dict:
    report = make_empty_redaction_report()
    error_counts: Counter[str] = Counter()
    redaction_by_type: Counter[str] = Counter()

    for path, record in zip(paths, files):
        file_entry = {
            "file_id": record["file_id"],
            "file_type": record["file_type"],
            "attempted": False,
            "status": "redaction_skipped",
            "redacted_copy_created": False,
            "validation_succeeded": False,
            "document_xml_minimal_patch_used": False,
            "metadata_fields_sanitized": 0,
            "word_native_validation_required": word_validation == "required",
            "word_native_validation_succeeded": False,
            "redaction_by_type": {},
            "error_codes": [],
        }

        if record["status"] in {"skipped_too_large", "skipped_unsupported_type"}:
            file_entry["status"] = record["status"]
            report["redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        if path.suffix.lower() != ".docx":
            file_entry["status"] = "redaction_not_implemented_v0_3_mvp_a"
            report["redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        if record["findings_count"] == 0:
            record["status"] = "redaction_skipped_no_supported_findings"
            record["planned_action"] = "manual_review_no_supported_findings"
            record["review_reasons"].append("redaction_skipped_no_supported_findings")
            file_entry["status"] = record["status"]
            report["redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        report["redaction_attempted_files"] += 1
        file_entry["attempted"] = True
        result = create_docx_redacted_copy(
            path,
            redacted_dir,
            record["file_id"],
            detector_config,
            placeholder_state,
            word_validation,
        )
        file_entry.update(
            {
                "status": result["status"],
                "redacted_copy_created": result["redacted_copy_created"],
                "validation_succeeded": result["validation_succeeded"],
                "redaction_by_type": result["redaction_by_type"],
                "error_codes": result["error_codes"],
                "document_xml_minimal_patch_used": result["document_xml_minimal_patch_used"],
                "metadata_fields_sanitized": result["metadata_fields_sanitized"],
                "word_native_validation_required": result["word_native_validation_required"],
                "word_native_validation_succeeded": result["word_native_validation_succeeded"],
            }
        )
        record["status"] = result["status"]
        record["planned_action"] = (
            "manual_review_redacted_copy"
            if result["redacted_copy_created"]
            else "manual_review_redaction_failed"
        )
        record["redacted_copy_created"] = result["redacted_copy_created"]
        record["redaction_validation_succeeded"] = result["validation_succeeded"]
        record["redaction_by_type"] = result["redaction_by_type"]
        for error_code in result["error_codes"]:
            record["review_reasons"].append(error_code)
            error_counts[error_code] += 1
        if result["status"] == "redaction_succeeded":
            record["review_reasons"].append("redacted_copy_generated_review_required")
            report["redaction_succeeded_files"] += 1
            report["redacted_files_created"] += 1
            redaction_by_type.update(result["redaction_by_type"])
        else:
            report["redaction_failed_files"] += 1
            if result["status"] == "redaction_validation_failed":
                report["validation_failed_files"] += 1
        report["files"].append(file_entry)

    report["manual_review_files"] = len(files)
    report["redaction_by_type"] = dict(sorted(redaction_by_type.items()))
    report["error_code_counts"] = dict(sorted(error_counts.items()))
    return report


def apply_pdf_visual_redactions(
    paths: list[Path],
    files: list[dict],
    redacted_dir: Path,
    region_map: dict[str, list[dict]],
    region_config_errors: list[str],
    render_dpi: int,
    padding_ratio: float,
) -> dict:
    report = empty_visual_redaction_report(render_dpi)
    error_counts: Counter[str] = Counter()
    pymupdf = None

    for path, record in zip(paths, files):
        file_entry = {
            "file_id": record["file_id"],
            "file_type": record["file_type"],
            "status": "visual_redaction_skipped",
            "pages_seen": 0,
            "pages_rendered": 0,
            "regions_applied": 0,
            "output_file_created": False,
            "output_file_type": "image_based_pdf",
            "validation_succeeded": False,
            "review_required": True,
            "review_reasons": list(record["review_reasons"]),
            "error_codes": [],
        }

        if record["status"] in {"skipped_too_large", "skipped_unsupported_type"}:
            file_entry["status"] = record["status"]
            report["visual_redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        if path.suffix.lower() != ".pdf":
            file_entry["status"] = "visual_redaction_skipped_non_pdf"
            report["visual_redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        if region_config_errors:
            status = (
                "visual_redaction_skipped_no_regions"
                if "pdf_regions_config_missing" in region_config_errors
                else "visual_redaction_skipped_invalid_regions"
            )
            record["status"] = status
            record["planned_action"] = "manual_review_pdf_regions_config"
            record["review_reasons"].extend(region_config_errors)
            file_entry["status"] = status
            file_entry["error_codes"].extend(region_config_errors)
            file_entry["review_reasons"] = list(record["review_reasons"])
            for error_code in region_config_errors:
                error_counts[error_code] += 1
            report["visual_redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        regions = region_map.get(record["file_id"], [])
        if not regions:
            status = "visual_redaction_skipped_no_regions"
            record["status"] = status
            record["planned_action"] = "manual_review_pdf_no_regions"
            record["review_reasons"].append(status)
            file_entry["status"] = status
            file_entry["review_reasons"] = list(record["review_reasons"])
            report["visual_redaction_skipped_files"] += 1
            report["files"].append(file_entry)
            continue

        if pymupdf is None:
            pymupdf = load_pymupdf()

        report["visual_redaction_files_attempted"] += 1
        result = create_pdf_visual_redacted_copy(
            path,
            redacted_dir,
            record["file_id"],
            regions,
            render_dpi,
            padding_ratio,
            pymupdf,
        )
        file_entry.update(
            {
                "status": result["status"],
                "pages_seen": result["pages_seen"],
                "pages_rendered": result["pages_rendered"],
                "regions_applied": result["regions_applied"],
                "output_file_created": result["output_file_created"],
                "validation_succeeded": result["validation_succeeded"],
                "error_codes": result["error_codes"],
            }
        )
        record["status"] = result["status"]
        record["planned_action"] = (
            "review_visual_redaction_copy_before_sharing"
            if result["output_file_created"]
            else "manual_review_pdf_visual_redaction_failed"
        )
        record["visual_redaction_copy_created"] = result["output_file_created"]
        record["visual_redaction_validation_succeeded"] = result["validation_succeeded"]
        for reason in (
            "destructive_visual_redaction_copy",
            "not_true_pdf_redaction",
            "manual_review_required_before_sharing",
        ):
            if reason not in record["review_reasons"]:
                record["review_reasons"].append(reason)
        for error_code in result["error_codes"]:
            record["review_reasons"].append(error_code)
            error_counts[error_code] += 1
        file_entry["review_reasons"] = list(record["review_reasons"])

        report["total_pages_rendered"] += result["pages_rendered"]
        report["total_regions_applied"] += result["regions_applied"]
        if result["status"] == "visual_redaction_copy_created":
            report["visual_redaction_files_created"] += 1
            record["review_reasons"].append("visual_redaction_copy_generated_review_required")
            file_entry["review_reasons"] = list(record["review_reasons"])
        else:
            report["visual_redaction_failed_files"] += 1
            if result["status"] == "visual_redaction_failed_validation_error":
                report["validation_failed_files"] += 1
        report["files"].append(file_entry)

    report["manual_review_files"] = len(files)
    report["error_code_counts"] = dict(sorted(error_counts.items()))
    return report


def apply_image_visual_redactions(
    paths: list[Path],
    files: list[dict],
    redacted_dir: Path,
    region_map: dict[str, list[dict]],
    region_config_errors: list[str],
    padding_ratio: float,
) -> dict:
    report = empty_image_visual_redaction_report()
    error_counts: Counter[str] = Counter()
    pymupdf = None

    for path, record in zip(paths, files):
        file_entry = {
            "file_id": record["file_id"],
            "file_type": record["file_type"],
            "status": "image_visual_redaction_skipped",
            "regions_applied": 0,
            "output_file_created": False,
            "output_file_type": "metadata_free_png",
            "validation_succeeded": False,
            "review_required": True,
            "review_reasons": list(record["review_reasons"]),
            "error_codes": [],
        }
        if record["status"] in {"skipped_too_large", "skipped_unsupported_type"}:
            file_entry["status"] = record["status"]
            report["image_visual_redaction_files_skipped"] += 1
            report["files"].append(file_entry)
            continue
        if path.suffix.lower() not in IMAGE_EXTENSIONS:
            file_entry["status"] = "image_visual_redaction_skipped_non_image"
            report["image_visual_redaction_files_skipped"] += 1
            report["files"].append(file_entry)
            continue
        if region_config_errors:
            status = (
                "image_visual_redaction_skipped_no_regions"
                if "image_regions_config_missing" in region_config_errors
                else "image_visual_redaction_skipped_invalid_regions"
            )
            record["status"] = status
            record["planned_action"] = "manual_review_image_regions_config"
            record["review_reasons"] = list(dict.fromkeys(record["review_reasons"] + region_config_errors))
            file_entry["status"] = status
            file_entry["error_codes"] = list(region_config_errors)
            file_entry["review_reasons"] = list(record["review_reasons"])
            error_counts.update(region_config_errors)
            report["image_visual_redaction_files_skipped"] += 1
            report["files"].append(file_entry)
            continue
        regions = region_map.get(record["file_id"], [])
        if not regions:
            status = "image_visual_redaction_skipped_no_regions"
            record["status"] = status
            record["planned_action"] = "manual_review_image_no_regions"
            record["review_reasons"] = list(dict.fromkeys(record["review_reasons"] + [status]))
            file_entry["status"] = status
            file_entry["review_reasons"] = list(record["review_reasons"])
            report["image_visual_redaction_files_skipped"] += 1
            report["files"].append(file_entry)
            continue
        if pymupdf is None:
            pymupdf = load_pymupdf()
        report["image_visual_redaction_files_attempted"] += 1
        result = create_image_visual_redacted_copy(
            path,
            redacted_dir,
            record["file_id"],
            regions,
            padding_ratio,
            pymupdf,
        )
        file_entry.update(
            {
                "status": result["status"],
                "regions_applied": result["regions_applied"],
                "output_file_created": result["output_file_created"],
                "validation_succeeded": result["validation_succeeded"],
                "error_codes": result["error_codes"],
            }
        )
        record["status"] = result["status"]
        record["planned_action"] = (
            "review_image_visual_redaction_copy_before_sharing"
            if result["output_file_created"]
            else "manual_review_image_visual_redaction_failed"
        )
        record["image_visual_redaction_copy_created"] = result["output_file_created"]
        record["image_visual_redaction_validation_succeeded"] = result["validation_succeeded"]
        for reason in (
            "destructive_visual_image_redaction_copy",
            "manual_review_required_before_sharing",
        ):
            if reason not in record["review_reasons"]:
                record["review_reasons"].append(reason)
        for error_code in result["error_codes"]:
            record["review_reasons"].append(error_code)
            error_counts[error_code] += 1
        file_entry["review_reasons"] = list(record["review_reasons"])
        report["total_regions_applied"] += result["regions_applied"]
        if result["status"] == "image_visual_redaction_copy_created":
            report["image_visual_redaction_files_created"] += 1
            record["review_reasons"].append("image_visual_redaction_copy_generated_review_required")
            file_entry["review_reasons"] = list(record["review_reasons"])
        else:
            report["image_visual_redaction_files_failed"] += 1
            if result["status"] == "image_visual_redaction_failed_validation_error":
                report["validation_failed_files"] += 1
        report["files"].append(file_entry)

    report["error_code_counts"] = dict(sorted(error_counts.items()))
    return report


def build_pdf_regions_template(files: list[dict], grid_cols: int, grid_rows: int) -> dict:
    return {
        "schema_version": "0.5",
        "coordinate_system": "page_ratio",
        "grid_reference": {
            "grid_columns": grid_cols,
            "grid_rows": grid_rows,
            "source": "pdf-preview-grid",
        },
        "local_sensitive_file": True,
        "source_names_recorded": False,
        "source_paths_recorded": False,
        "files": [
            {
                "file_id": record["file_id"],
                "regions": [],
            }
            for record in files
            if record["file_type"] == "pdf"
        ],
    }


def write_preview_grid_guide(path: Path, grid_cols: int, grid_rows: int) -> None:
    example_col_start = 7 if grid_cols >= 10 else 1
    example_col_end = 10 if grid_cols >= 10 else min(grid_cols, 2)
    example_row_start = 2 if grid_rows >= 4 else 1
    example_row_end = 4 if grid_rows >= 4 else min(grid_rows, 2)
    example_hint = (
        f"{grid_column_label(example_col_start)}{example_row_start}:"
        f"{grid_column_label(example_col_end)}{example_row_end}"
    )
    x0 = (example_col_start - 1) / grid_cols
    y0 = (example_row_start - 1) / grid_rows
    x1 = example_col_end / grid_cols
    y1 = example_row_end / grid_rows
    lines = [
        "# PDF Preview Grid Coordinate Guide",
        "",
        "This guide is for local manual review only. Preview PNG files are local-sensitive artifacts.",
        "",
        "## Boundary",
        "",
        "- This mode generates preview PNG files only.",
        "- It does not OCR, auto-detect sensitive information, or create redacted PDFs.",
        "- Do not upload, commit, or share preview PNG files.",
        "- Do not ask GPT to inspect real-project preview images.",
        "",
        "## Grid",
        "",
        f"- columns: {grid_cols}",
        f"- rows: {grid_rows}",
        "- columns use letters from left to right, such as A, B, C.",
        "- rows use numbers from top to bottom, such as 1, 2, 3.",
        "",
        "## Convert A Grid Range To page_ratio",
        "",
        "For a range from start cell to end cell:",
        "",
        "```text",
        "x0 = (col_start - 1) / grid_columns",
        "y0 = (row_start - 1) / grid_rows",
        "x1 = col_end / grid_columns",
        "y1 = row_end / grid_rows",
        "```",
        "",
        "Example:",
        "",
        "```json",
        json.dumps(
            {
                "page": 1,
                "grid_hint": example_hint,
                "x0": round(x0, 4),
                "y0": round(y0, 4),
                "x1": round(x1, 4),
                "y1": round(y1, 4),
                "label": "manual_review_area_1",
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "Use the generated pdf_redaction_regions.local.template.json as the starting point for the later visual-redact-pdf step.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def apply_pdf_preview_grid(
    paths: list[Path],
    files: list[dict],
    previews_dir: Path,
    output_path: Path,
    preview_dpi: int,
    grid_cols: int,
    grid_rows: int,
) -> dict:
    report = empty_preview_grid_report(preview_dpi, grid_cols, grid_rows)
    error_counts: Counter[str] = Counter()
    pymupdf = None

    for path, record in zip(paths, files):
        file_entry = {
            "file_id": record["file_id"],
            "file_type": record["file_type"],
            "status": "preview_grid_skipped",
            "pages_seen": 0,
            "preview_pages_created": 0,
            "preview_relative_paths": [],
            "validation_succeeded": False,
            "review_required": True,
            "review_reasons": list(record["review_reasons"]),
            "error_codes": [],
        }

        if record["status"] in {"skipped_too_large", "skipped_unsupported_type"}:
            file_entry["status"] = record["status"]
            report["preview_pdf_files_skipped"] += 1
            report["files"].append(file_entry)
            continue

        if path.suffix.lower() != ".pdf":
            file_entry["status"] = "preview_grid_skipped_non_pdf"
            report["preview_pdf_files_skipped"] += 1
            report["files"].append(file_entry)
            continue

        if pymupdf is None:
            pymupdf = load_pymupdf()

        report["preview_pdf_files_attempted"] += 1
        result = create_pdf_preview_grid_outputs(
            path,
            previews_dir,
            record["file_id"],
            grid_cols,
            grid_rows,
            preview_dpi,
            pymupdf,
        )
        file_entry.update(
            {
                "status": result["status"],
                "pages_seen": result["pages_seen"],
                "preview_pages_created": result["preview_pages_created"],
                "preview_relative_paths": result["preview_relative_paths"],
                "validation_succeeded": result["validation_succeeded"],
                "error_codes": result["error_codes"],
            }
        )
        record["status"] = result["status"]
        record["planned_action"] = (
            "manual_region_selection_from_preview_grid"
            if result["validation_succeeded"]
            else "manual_review_preview_grid_failed"
        )
        record["preview_grid_pages_created"] = result["preview_pages_created"]
        record["preview_grid_validation_succeeded"] = result["validation_succeeded"]
        for reason in (
            "preview_grid_contains_rendered_pdf_pages",
            "local_sensitive_preview_output",
            "manual_region_selection_required",
        ):
            if reason not in record["review_reasons"]:
                record["review_reasons"].append(reason)
        for error_code in result["error_codes"]:
            record["review_reasons"].append(error_code)
            error_counts[error_code] += 1
        file_entry["review_reasons"] = list(record["review_reasons"])

        report["total_pages_rendered"] += result["preview_pages_created"]
        report["preview_images_created"] += result["preview_pages_created"]
        if result["status"] == "preview_grid_created":
            report["preview_pdf_files_processed"] += 1
            report["files_processed"] += 1
            record["review_reasons"].append("preview_grid_generated_review_required")
            file_entry["review_reasons"] = list(record["review_reasons"])
        else:
            report["preview_pdf_files_failed"] += 1
            if not result["validation_succeeded"]:
                report["validation_failed_files"] += 1
        report["files"].append(file_entry)

    write_local_sensitive_json(
        output_path / "pdf_redaction_regions.local.template.json",
        build_pdf_regions_template(files, grid_cols, grid_rows),
    )
    write_preview_grid_guide(previews_dir / "preview_grid_coordinate_guide.md", grid_cols, grid_rows)
    report["error_code_counts"] = dict(sorted(error_counts.items()))
    return report


def mapping_entries_for_write(placeholder_state: dict) -> list[dict]:
    entries = placeholder_state.get("entries", [])
    return sorted(entries, key=lambda item: (item["detector_type"], item["placeholder"]))


def main() -> int:
    args = parse_args()
    input_path = Path(args.input).expanduser()
    output_path = Path(args.output).expanduser()
    config_path = Path(args.config).expanduser() if args.config else None
    terms_path = Path(args.terms_file).expanduser() if args.terms_file else None
    pdf_regions_path = Path(args.pdf_regions).expanduser() if args.pdf_regions else None
    image_regions_path = Path(args.image_regions).expanduser() if args.image_regions else None
    regions_template_path = (
        Path(args.regions_template).expanduser()
        if args.regions_template
        else output_path / "pdf_redaction_regions.local.template.json"
    )
    regions_output_path = (
        Path(args.regions_output).expanduser()
        if args.regions_output
        else output_path / "pdf_redaction_regions.local.json"
    )
    ocr_candidates_path = (
        Path(args.ocr_candidates_output).expanduser()
        if args.ocr_candidates_output
        else output_path / "ocr_candidate_regions.local.json"
    )
    docx_candidates_path = (
        Path(args.docx_candidates_output).expanduser()
        if args.docx_candidates_output
        else output_path / DOCX_CANDIDATES_FILENAME
    )

    if args.jobs < 1:
        raise SystemExit("--jobs must be >= 1")
    if args.jobs > 4:
        raise SystemExit("--jobs must not exceed 4")
    if args.max_file_size_mb < 1:
        raise SystemExit("--max-file-size-mb must be >= 1")
    if args.max_file_size_mb > 500:
        raise SystemExit("--max-file-size-mb must not exceed 500")
    render_dpi = args.render_dpi if args.render_dpi is not None else 200
    preview_dpi = args.preview_dpi if args.preview_dpi is not None else (args.render_dpi if args.render_dpi is not None else 120)
    if render_dpi < 72 or render_dpi > 400:
        raise SystemExit("--render-dpi must be between 72 and 400")
    if preview_dpi < 72 or preview_dpi > 300:
        raise SystemExit("--preview-dpi must be between 72 and 300")
    if args.grid_cols < 1 or args.grid_cols > 52:
        raise SystemExit("--grid-cols must be between 1 and 52")
    if args.grid_rows < 1 or args.grid_rows > 100:
        raise SystemExit("--grid-rows must be between 1 and 100")
    if args.redaction_padding_ratio < 0 or args.redaction_padding_ratio > 0.05:
        raise SystemExit("--redaction-padding-ratio must be between 0 and 0.05")
    if not OCR_LANGUAGE_PATTERN.fullmatch(args.ocr_langs):
        raise SystemExit("--ocr-langs contains unsupported characters")
    if args.ocr_dpi < 100 or args.ocr_dpi > 400:
        raise SystemExit("--ocr-dpi must be between 100 and 400")
    if args.ocr_min_confidence < 0 or args.ocr_min_confidence > 100:
        raise SystemExit("--ocr-min-confidence must be between 0 and 100")
    if args.ocr_psm < 3 or args.ocr_psm > 13:
        raise SystemExit("--ocr-psm must be between 3 and 13")
    if args.ocr_max_pages < 1 or args.ocr_max_pages > 200:
        raise SystemExit("--ocr-max-pages must be between 1 and 200")
    if args.ocr_timeout_seconds < 5 or args.ocr_timeout_seconds > 600:
        raise SystemExit("--ocr-timeout-seconds must be between 5 and 600")
    if args.selector_port < 0 or args.selector_port > 65535:
        raise SystemExit("--selector-port must be between 0 and 65535")
    if args.selector_timeout_seconds < 30 or args.selector_timeout_seconds > 7200:
        raise SystemExit("--selector-timeout-seconds must be between 30 and 7200")
    if not input_path.exists():
        raise SystemExit("--input does not exist")
    if config_path and not config_path.exists():
        raise SystemExit("--config does not exist")

    detector_config = merge_terms_file(load_detector_config(config_path), terms_path)
    detector_config = apply_redaction_profile(detector_config, args.redaction_profile)
    validate_custom_terms(detector_config)
    validate_output_path(
        input_path,
        output_path,
        args.allow_existing_output or args.mode == "grid-regions-to-json",
    )
    if args.mode == "ocr-precheck":
        validate_ocr_candidates_path(output_path, ocr_candidates_path)
        validate_ocr_precheck_input(input_path)
    if args.mode == "visual-redact-image":
        validate_visual_image_input(input_path)
        if image_regions_path is not None:
            validate_selector_regions_path(output_path, image_regions_path, IMAGE_REGIONS_FILENAME)
    if args.mode in {"visual-redact-pdf", "pdf-preview-grid"}:
        validate_visual_pdf_input(input_path, args.mode)
    if args.mode == "docx-candidate-precheck":
        validate_docx_candidate_input(input_path)
        validate_docx_candidates_path(output_path, docx_candidates_path)

    if args.mode == "grid-regions-to-json":
        return apply_grid_regions_to_json(
            output_path,
            regions_template_path,
            regions_output_path,
            args.grid_region,
            args.overwrite_regions,
            args.grid_cols,
            args.grid_rows,
        )
    if args.mode == "region-selector":
        return apply_region_selector_mode(
            input_path,
            output_path,
            pdf_regions_path,
            image_regions_path,
            preview_dpi,
            args.selector_port,
            args.selector_timeout_seconds,
            not args.no_open_browser,
            args.overwrite_regions,
            detector_config,
        )
    if args.mode == "docx-candidate-precheck":
        return apply_docx_candidate_precheck_mode(
            input_path,
            output_path,
            docx_candidates_path,
            args.overwrite_docx_candidates,
            detector_config,
            args.redaction_profile,
        )

    reports_dir = output_path / "reports"
    redacted_dir = output_path / "redacted_files"
    previews_dir = output_path / "previews"
    logs_dir = output_path / "logs"
    for directory in (reports_dir, redacted_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)
    if args.mode == "pdf-preview-grid":
        previews_dir.mkdir(parents=True, exist_ok=True)
    (output_path / OUTPUT_MARKER).write_text("local-redaction-assistant\n", encoding="utf-8")

    input_base = input_path if input_path.is_dir() else input_path.parent
    max_bytes = args.max_file_size_mb * 1024 * 1024
    if args.mode in {"pdf-preview-grid", "ocr-precheck"}:
        scanned_paths, scan_stats = scan_pdf_preview_input_files(input_path, output_path)
    else:
        scanned_paths, scan_stats = scan_input_files(input_path, output_path)
    scanned_paths = list(sorted(scanned_paths))
    files = [
        build_file_record(i, path, input_base, args.mode, max_bytes, detector_config)
        for i, path in enumerate(scanned_paths, start=1)
    ]
    file_index = [
        build_file_index_record(i, path, input_base)
        for i, path in enumerate(scanned_paths, start=1)
    ]
    placeholder_state: dict = {"by_label": {}, "entries": []}
    ocr_precheck_report = empty_ocr_precheck_report(
        args.ocr_dpi,
        args.ocr_langs,
        args.ocr_min_confidence,
        args.ocr_psm,
        args.ocr_max_pages,
        args.ocr_timeout_seconds,
    )
    if args.mode == "redact":
        word_validation = args.word_validation or (
            "required" if args.redaction_profile == LEGAL_TEMPLATE_PROFILE else "off"
        )
        redaction_report = apply_docx_redactions(
            scanned_paths,
            files,
            redacted_dir,
            detector_config,
            placeholder_state,
            word_validation,
        )
        visual_redaction_report = empty_visual_redaction_report(render_dpi)
        image_visual_redaction_report = empty_image_visual_redaction_report()
        preview_grid_report = empty_preview_grid_report(preview_dpi, args.grid_cols, args.grid_rows)
    elif args.mode == "visual-redact-pdf":
        redaction_report = make_empty_redaction_report()
        redaction_report["manual_review_files"] = len(files)
        region_map, region_config_errors, ocr_candidate_regions_source = load_pdf_region_map(
            pdf_regions_path,
            args.confirm_ocr_candidates,
        )
        visual_redaction_report = apply_pdf_visual_redactions(
            scanned_paths,
            files,
            redacted_dir,
            region_map,
            region_config_errors,
            render_dpi,
            args.redaction_padding_ratio,
        )
        visual_redaction_report["ocr_candidate_regions_source"] = ocr_candidate_regions_source
        visual_redaction_report["ocr_candidate_regions_confirmed"] = bool(
            ocr_candidate_regions_source and args.confirm_ocr_candidates
        )
        image_visual_redaction_report = empty_image_visual_redaction_report()
        preview_grid_report = empty_preview_grid_report(preview_dpi, args.grid_cols, args.grid_rows)
    elif args.mode == "visual-redact-image":
        redaction_report = make_empty_redaction_report()
        redaction_report["manual_review_files"] = len(files)
        visual_redaction_report = empty_visual_redaction_report(render_dpi)
        image_region_map, image_region_config_errors = load_image_region_map(image_regions_path)
        image_visual_redaction_report = apply_image_visual_redactions(
            scanned_paths,
            files,
            redacted_dir,
            image_region_map,
            image_region_config_errors,
            args.redaction_padding_ratio,
        )
        preview_grid_report = empty_preview_grid_report(preview_dpi, args.grid_cols, args.grid_rows)
    elif args.mode == "pdf-preview-grid":
        redaction_report = make_empty_redaction_report()
        redaction_report["manual_review_files"] = len(files)
        visual_redaction_report = empty_visual_redaction_report(render_dpi)
        image_visual_redaction_report = empty_image_visual_redaction_report()
        preview_grid_report = apply_pdf_preview_grid(
            scanned_paths,
            files,
            previews_dir,
            output_path,
            preview_dpi,
            args.grid_cols,
            args.grid_rows,
        )
    elif args.mode == "ocr-precheck":
        redaction_report = make_empty_redaction_report()
        redaction_report["manual_review_files"] = len(files)
        visual_redaction_report = empty_visual_redaction_report(render_dpi)
        image_visual_redaction_report = empty_image_visual_redaction_report()
        preview_grid_report = empty_preview_grid_report(preview_dpi, args.grid_cols, args.grid_rows)
        ocr_precheck_report = apply_ocr_precheck(
            scanned_paths,
            files,
            ocr_candidates_path,
            detector_config,
            args.ocr_langs,
            args.ocr_dpi,
            args.ocr_min_confidence,
            args.ocr_psm,
            args.ocr_max_pages,
            args.ocr_timeout_seconds,
        )
    else:
        redaction_report = make_empty_redaction_report()
        redaction_report["manual_review_files"] = len(files)
        visual_redaction_report = empty_visual_redaction_report(render_dpi)
        image_visual_redaction_report = empty_image_visual_redaction_report()
        preview_grid_report = empty_preview_grid_report(preview_dpi, args.grid_cols, args.grid_rows)

    status_counts = Counter(record["status"] for record in files)
    reason_counts = Counter(reason for record in files for reason in record["review_reasons"])
    detected_by_type = Counter()
    masked_samples_by_type: dict[str, list[str]] = {}
    for record in files:
        detected_by_type.update(record["detected_by_type"])
        for finding_type, samples in record["masked_samples"].items():
            for sample in samples:
                add_masked_sample(masked_samples_by_type, finding_type, sample)
    files_with_findings = sum(1 for record in files if record["findings_count"] > 0)
    txt_detector_test_files_read = sum(1 for record in files if record["txt_detector_test_content_read"])
    docx_main_body_detector_files_read = sum(1 for record in files if record["docx_main_body_read_only"])
    xlsx_visible_cells_detector_files_read = sum(1 for record in files if record["xlsx_visible_cells_read_only"])
    office_document_bodies_read = sum(1 for record in files if record["office_document_body_read"])
    ocr_pdf_files_processed = sum(
        1 for record in files if record["file_type"] == "pdf" and record.get("ocr_pages_processed", 0) > 0
    )
    ocr_image_files_processed = sum(
        1
        for record in files
        if record["file_type"] in {"png", "jpg", "jpeg"} and record.get("ocr_pages_processed", 0) > 0
    )
    custom_term_counts = {
        key: len(detector_config["custom_terms"][key])
        for key in CUSTOM_TERM_KEYS
        if detector_config["custom_terms"][key]
    }
    summary = {
        "tool": "local-redaction-assistant",
        "version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": args.mode,
        "redaction_profile": args.redaction_profile,
        "safety": {
            "originals_modified": False,
            "network_used": False,
            "external_api_used": False,
            "office_document_bodies_read": office_document_bodies_read > 0,
            "docx_main_body_read_only": docx_main_body_detector_files_read > 0,
            "docx_headers_footers_comments_read": False,
            "docx_footnotes_endnotes_read": False,
            "docx_text_boxes_read": False,
            "docx_document_xml_minimal_patch_only": args.mode == "redact",
            "docx_metadata_changes_limited_to_docprops": args.mode == "redact",
            "xlsx_visible_cells_read_only": xlsx_visible_cells_detector_files_read > 0,
            "xlsx_hidden_sheets_read": False,
            "xlsx_formulas_read": False,
            "xlsx_comments_read": False,
            "xlsx_hyperlink_targets_read": False,
            "xlsx_images_or_objects_read": False,
            "pdf_bodies_read": ocr_pdf_files_processed > 0,
            "pdf_pages_rendered_for_visual_redaction": visual_redaction_report["total_pages_rendered"] > 0,
            "pdf_pages_rendered_for_preview_grid": preview_grid_report["total_pages_rendered"] > 0,
            "pdf_pages_rendered_for_ocr": ocr_precheck_report["pages_processed"] > 0 and ocr_pdf_files_processed > 0,
            "pdf_preview_grid_generated": preview_grid_report["preview_images_created"] > 0,
            "pdf_text_layer_preserved": False if args.mode == "visual-redact-pdf" else None,
            "image_bodies_read": ocr_image_files_processed > 0 or args.mode == "visual-redact-image",
            "image_metadata_preserved": False if args.mode == "visual-redact-image" else None,
            "local_ocr_used": args.mode == "ocr-precheck" and ocr_precheck_report["pages_processed"] > 0,
            "cloud_ocr_used": False,
            "ocr_text_persisted": False,
            "txt_detector_test_content_read": txt_detector_test_files_read > 0,
            "raw_sensitive_values_recorded": False,
            "source_names_recorded": False,
            "source_paths_recorded": False,
            "stable_file_identifier_recorded": False,
            "precise_size_recorded": False,
            "mapping_saved": bool(args.save_map),
            "local_mapping_file_contains_raw_values": bool(args.save_map and placeholder_state.get("entries")),
            "local_file_index_saved": True,
            "human_review_required": True,
        },
        "input": {
            "kind": "directory" if input_path.is_dir() else "file",
            "raw_path_omitted": True,
        },
        "config": {
            "provided": bool(config_path),
            "parsed": bool(detector_config["parsed"]),
            "parser": detector_config["parser"],
            "enabled_detectors": {
                key: detector_config["enabled_detectors"][key]
                for key in DETECTOR_NAMES
            },
            "custom_term_counts": custom_term_counts,
            "parse_warnings": detector_config["parse_warnings"],
            "terms_file_provided": bool(detector_config["terms_file_provided"]),
            "terms_file_parsed": bool(detector_config["terms_file_parsed"]),
        },
        "options": {
            "jobs": args.jobs,
            "max_file_size_mb": args.max_file_size_mb,
            "save_map": bool(args.save_map),
            "render_dpi": render_dpi if args.mode == "visual-redact-pdf" else None,
            "preview_dpi": preview_dpi if args.mode == "pdf-preview-grid" else None,
            "grid_columns": args.grid_cols if args.mode == "pdf-preview-grid" else None,
            "grid_rows": args.grid_rows if args.mode == "pdf-preview-grid" else None,
            "redaction_padding_ratio": args.redaction_padding_ratio if args.mode == "visual-redact-pdf" else None,
            "image_redaction_padding_ratio": args.redaction_padding_ratio if args.mode == "visual-redact-image" else None,
            "ocr_languages": args.ocr_langs if args.mode == "ocr-precheck" else None,
            "ocr_dpi": args.ocr_dpi if args.mode == "ocr-precheck" else None,
            "ocr_min_confidence": args.ocr_min_confidence if args.mode == "ocr-precheck" else None,
            "ocr_psm": args.ocr_psm if args.mode == "ocr-precheck" else None,
            "ocr_max_pages": args.ocr_max_pages if args.mode == "ocr-precheck" else None,
            "ocr_timeout_seconds": args.ocr_timeout_seconds if args.mode == "ocr-precheck" else None,
        },
        "totals": {
            "files_seen": len(files),
            "supported_files": sum(1 for record in files if record["supported_type"]),
            "unsupported_files": sum(1 for record in files if not record["supported_type"]),
            "symlink_files_skipped": scan_stats["symlink_files_skipped"],
            "symlink_dirs_skipped": scan_stats["symlink_dirs_skipped"],
            "txt_detector_test_files_read": txt_detector_test_files_read,
            "docx_main_body_detector_files_read": docx_main_body_detector_files_read,
            "xlsx_visible_cells_detector_files_read": xlsx_visible_cells_detector_files_read,
            "detected_sensitive_items": sum(detected_by_type.values()),
            "files_with_findings": files_with_findings,
            "redacted_files_created": redaction_report["redacted_files_created"]
            + visual_redaction_report["visual_redaction_files_created"]
            + image_visual_redaction_report["image_visual_redaction_files_created"],
            "visual_redaction_files_created": visual_redaction_report["visual_redaction_files_created"],
            "visual_redaction_failed_files": visual_redaction_report["visual_redaction_failed_files"],
            "visual_redaction_validation_failed_files": visual_redaction_report["validation_failed_files"],
            "visual_redaction_total_pages_rendered": visual_redaction_report["total_pages_rendered"],
            "visual_redaction_total_regions_applied": visual_redaction_report["total_regions_applied"],
            "image_visual_redaction_files_created": image_visual_redaction_report["image_visual_redaction_files_created"],
            "image_visual_redaction_failed_files": image_visual_redaction_report["image_visual_redaction_files_failed"],
            "image_visual_redaction_validation_failed_files": image_visual_redaction_report["validation_failed_files"],
            "image_visual_redaction_total_regions_applied": image_visual_redaction_report["total_regions_applied"],
            "preview_grid_pdf_files_processed": preview_grid_report["preview_pdf_files_processed"],
            "preview_grid_images_created": preview_grid_report["preview_images_created"],
            "preview_grid_total_pages_rendered": preview_grid_report["total_pages_rendered"],
            "ocr_files_processed": ocr_precheck_report["ocr_files_processed"],
            "ocr_files_failed": ocr_precheck_report["ocr_files_failed"],
            "ocr_pages_processed": ocr_precheck_report["pages_processed"],
            "ocr_candidate_regions_created": ocr_precheck_report["candidate_regions_created"],
            "ocr_low_confidence_tokens_skipped": ocr_precheck_report["low_confidence_tokens_skipped"],
            "manual_review_files": len(files),
        },
        "detected_by_type": dict(sorted(detected_by_type.items())),
        "masked_samples_by_type": {key: masked_samples_by_type[key] for key in sorted(masked_samples_by_type)},
        "status_counts": dict(sorted(status_counts.items())),
        "review_reason_counts": dict(sorted(reason_counts.items())),
        "files": files,
        "notes": [
            "v0.3 MVP-A reads .txt files only as detector validation samples.",
            "v0.3 MVP-A reads DOCX word/document.xml only for ordinary main-body w:t detector validation and DOCX copy redaction.",
            "v0.3 MVP-A does not read DOCX headers, footers, comments, footnotes, endnotes, text boxes, or embedded media.",
            "v0.3 MVP-A reads XLSX visible sheet ordinary cell text only for detector-stage precheck.",
            "v0.3 MVP-A does not read XLSX hidden sheets, formulas, comments, hyperlink targets, drawings, images, objects, macros, or external link bodies.",
            "v0.5 MVP-B does not read PDF text bodies for detector purposes; visual-redact-pdf renders pages to images only from local region config.",
            "v0.5 MVP-C pdf-preview-grid renders pages to local-sensitive grid PNG previews only and does not create redacted PDFs.",
            "v0.3 MVP-A creates redacted copies only for supported DOCX ordinary w:t matches in explicit redact mode.",
            "v0.3 MVP-A does not create XLSX, PDF, or image redacted copies.",
            "summary.json omits stable file identifiers, precise file size, source names, and source paths.",
            "summary.json records masked samples only, never raw matched values.",
            "redaction_report.json records safe statistics and error codes only.",
            "visual_redaction_report.json records safe statistics and error codes only.",
            "file_index.local.json is local-sensitive and must not be committed or read by Codex for real projects.",
            "sensitive_mapping.local.json is generated only with --save-map and must not be committed or read by Codex for real projects.",
            "All files require manual review before external sharing.",
            "visual-redact-pdf creates image-based PDF visual redaction copies only from local region config.",
            "visual-redact-pdf does not OCR, auto-detect sensitive information, use apply_redactions(), or perform true PDF redaction.",
            "visual-redact-image creates metadata-free PNG copies only from explicit local manual regions.",
            "visual-redact-image does not OCR, auto-detect sensitive information, or preserve source image metadata.",
            "pdf-preview-grid does not OCR, auto-detect sensitive information, create redacted PDFs, or inspect preview image content.",
            "ocr-precheck uses local Tesseract only; OCR text is processed in memory and is not persisted.",
            "ocr-precheck writes local-sensitive candidate regions for manual confirmation and does not create redacted files.",
            "OCR candidate regions must be visually reviewed before explicit visual-redact-pdf use.",
        ],
    }

    write_json(reports_dir / "summary.json", summary)
    write_json(reports_dir / "precheck_report.json", build_precheck_report(summary))
    write_manual_review_csv(reports_dir / "manual_review_list.csv", files)
    write_run_summary_md(reports_dir / "run_summary.md", summary)
    write_json(
        reports_dir / "redaction_report.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "generated_at_utc": summary["generated_at_utc"],
            "mode": args.mode,
            "redaction_profile": args.redaction_profile,
            "raw_values_recorded": False,
            "source_names_recorded": False,
            "source_paths_recorded": False,
            **redaction_report,
        },
    )
    write_json(
        reports_dir / "visual_redaction_report.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "generated_at_utc": summary["generated_at_utc"],
            **visual_redaction_report,
        },
    )
    write_json(
        reports_dir / "image_visual_redaction_report.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "generated_at_utc": summary["generated_at_utc"],
            **image_visual_redaction_report,
        },
    )
    write_json(
        reports_dir / "preview_grid_report.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "generated_at_utc": summary["generated_at_utc"],
            **preview_grid_report,
        },
    )
    write_json(
        reports_dir / "ocr_precheck_report.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "generated_at_utc": summary["generated_at_utc"],
            **ocr_precheck_report,
        },
    )
    write_local_sensitive_json(
        output_path / "file_index.local.json",
        {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "local_sensitive_file": True,
            "original_absolute_paths_included": False,
            "raw_sensitive_values_recorded": False,
            "entries": file_index,
            "note": "Local index for lawyer-side file lookup only. Do not commit or share.",
        },
    )

    if args.save_map:
        mapping_entries = mapping_entries_for_write(placeholder_state)
        mapping = {
            "tool": "local-redaction-assistant",
            "version": VERSION,
            "local_sensitive_file": True,
            "entries": mapping_entries,
            "raw_values_recorded": bool(mapping_entries),
            "note": "Local sensitive mapping for lawyer-side lookup only. Do not commit or share.",
        }
        write_local_sensitive_json(output_path / "sensitive_mapping.local.json", mapping)

    print(
        "summary written: reports/summary.json "
        f"(files_seen={len(files)}, mode={args.mode}, raw_values_recorded=false)"
    )
    print("file_index.local.json is local-sensitive; do not commit, upload, or share it.")
    if terms_path is not None:
        print("terms file is local-sensitive; do not commit, upload, or share it.")
    if args.mode == "ocr-precheck":
        print("ocr_candidate_regions.local.json requires manual confirmation before visual redaction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
