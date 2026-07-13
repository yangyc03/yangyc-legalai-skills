#!/usr/bin/env python3
"""Deterministic legal-template DOCX occurrence mapping and clean-copy editing."""

from __future__ import annotations

import re
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

from docx_redaction_profile import (
    LEGAL_TEMPLATE_PROFILE,
    TERM_CATEGORY_LABELS,
    XmlTextSlice,
    _decode_text,
    _markup_end,
    _tag_details,
    apply_byte_patches,
    extract_profile_candidates,
    extract_safe_docx_text_slices,
    sanitise_docx_metadata_part,
    validate_docx_ooxml_package,
    xml_escape_text,
)
from word_native_validator import validate_docx_with_word


SUPPORTED_PART_PATTERN = re.compile(
    r"^word/(?:document|header\d+|footer\d+|footnotes|endnotes|comments)\.xml$"
)
EXCLUDED_TEXT_ANCESTORS = {
    "del",
    "ins",
    "moveFrom",
    "moveTo",
    "instrText",
    "fldChar",
    "drawing",
    "object",
    "pict",
    "txbxContent",
}
REVISION_TAGS = {
    "ins",
    "del",
    "moveFrom",
    "moveTo",
    "rPrChange",
    "pPrChange",
    "tblPrChange",
    "trPrChange",
    "tcPrChange",
    "sectPrChange",
    "numberingChange",
}
HIGH_RISK_PATTERNS = {
    "phone": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    "identity_number": re.compile(r"(?:身份证(?:号码|号)?|证件(?:号码|号)?)[：:]?\s*[0-9A-Za-z*]{15,20}"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
}
TABLE_ACTION_TERM_ONLY = "term-only"
TABLE_ACTION_CLEAR_DETAILS = "clear-detail-rows"
TABLE_ACTIONS = (TABLE_ACTION_TERM_ONLY, TABLE_ACTION_CLEAR_DETAILS)


@dataclass(frozen=True)
class Occurrence:
    occurrence_id: str
    category: str
    display: str
    raw: str
    part_name: str
    paragraph_index: int
    start: int
    end: int
    replacement_supported: bool = True

    def public(self) -> dict:
        return {
            "id": self.occurrence_id,
            "category": self.category,
            "display": self.display,
            "part_scope": _part_scope(self.part_name),
            "replacement_supported": self.replacement_supported,
        }


@dataclass(frozen=True)
class RowRef:
    start: int
    end: int
    text: str
    slices: tuple[XmlTextSlice, ...]


@dataclass
class TableRef:
    table_id: str
    part_name: str
    rows: list[RowRef] = field(default_factory=list)

    @property
    def total_rows(self) -> list[int]:
        return [index for index, row in enumerate(self.rows) if any(marker in row.text for marker in ("合计", "总计", "总数"))]

    def public(self) -> dict:
        return {
            "table_id": self.table_id,
            "part_scope": _part_scope(self.part_name),
            "row_count": len(self.rows),
            "header_rows_preserved": [1] if self.rows else [],
            "total_rows_detected": [index + 1 for index in self.total_rows],
            "actions": list(TABLE_ACTIONS),
        }


def _part_scope(part_name: str) -> str:
    if part_name == "word/document.xml":
        return "正文/表格"
    if "/header" in part_name:
        return "页眉"
    if "/footer" in part_name:
        return "页脚"
    return {
        "word/footnotes.xml": "脚注",
        "word/endnotes.xml": "尾注",
        "word/comments.xml": "批注",
    }.get(part_name, "其他")


def supported_text_parts(names: set[str]) -> list[str]:
    return sorted(name for name in names if SUPPORTED_PART_PATTERN.match(name))


def inspect_docx_features(path: Path) -> dict[str, int | bool]:
    """Return counts only; never persist text, names, paths, or coordinates."""
    result: dict[str, int | bool] = {
        "revision_nodes": 0,
        "track_revisions_enabled": False,
        "comments": 0,
        "text_boxes": 0,
        "fields": 0,
        "external_relationships": 0,
        "macros": False,
        "embedded_objects": 0,
        "images": 0,
    }
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        content_types = archive.read("[Content_Types].xml") if "[Content_Types].xml" in names else b""
        result["macros"] = "word/vbaProject.bin" in names or b"macroEnabled" in content_types
        result["embedded_objects"] = sum(name.startswith("word/embeddings/") for name in names)
        result["images"] = sum(name.startswith("word/media/") for name in names)
        for name in sorted(names):
            if not (name.endswith(".xml") or name.endswith(".rels")):
                continue
            try:
                root = ElementTree.fromstring(archive.read(name))
            except ElementTree.ParseError:
                continue
            for element in root.iter():
                local = element.tag.rsplit("}", 1)[-1]
                if local in REVISION_TAGS:
                    result["revision_nodes"] = int(result["revision_nodes"]) + 1
                if local == "trackRevisions":
                    result["track_revisions_enabled"] = True
                if name == "word/comments.xml" and local == "comment":
                    result["comments"] = int(result["comments"]) + 1
                if local == "txbxContent":
                    result["text_boxes"] = int(result["text_boxes"]) + 1
                if local in {"instrText", "fldChar"}:
                    result["fields"] = int(result["fields"]) + 1
                if name.endswith(".rels") and local == "Relationship" and element.attrib.get("TargetMode", "").lower() == "external":
                    result["external_relationships"] = int(result["external_relationships"]) + 1
    return result


def _candidate_spans(text: str, category: str, display: str) -> list[tuple[int, int, str]]:
    if not display:
        return []
    pattern = "".join(r"\s+" if char.isspace() else re.escape(char) for char in display)
    return [(match.start(), match.end(), match.group(0)) for match in re.finditer(pattern, text)]


def _scan_part_occurrences(
    part_name: str,
    payload: bytes,
    start_number: int,
    *,
    text_box_only: bool = False,
) -> tuple[list[Occurrence], int]:
    excluded = set(EXCLUDED_TEXT_ANCESTORS)
    required_ancestor = None
    replacement_supported = True
    if text_box_only:
        excluded.difference_update({"txbxContent", "pict", "drawing", "object"})
        required_ancestor = "txbxContent"
        replacement_supported = False
    paragraphs, errors = extract_safe_docx_text_slices(payload, excluded, required_ancestor)
    if errors:
        raise ValueError(errors[0])
    occurrences: list[Occurrence] = []
    number = start_number
    for paragraph_index, slices in enumerate(paragraphs):
        logical = "".join(item.text for item in slices)
        candidates = extract_profile_candidates(logical, LEGAL_TEMPLATE_PROFILE)["candidate_terms"]
        proposals: list[tuple[int, int, str, str, str]] = []
        for category, displays in candidates.items():
            for display in displays:
                for start, end, raw in _candidate_spans(logical, category, display):
                    proposals.append((start, end, category, display, raw))
        chosen: list[tuple[int, int]] = []
        for start, end, category, display, raw in sorted(
            proposals, key=lambda item: (item[0], -(item[1] - item[0]), item[2])
        ):
            if any(start < chosen_end and end > chosen_start for chosen_start, chosen_end in chosen):
                continue
            chosen.append((start, end))
            occurrences.append(
                Occurrence(
                    occurrence_id=f"O{number:06d}",
                    category=category,
                    display=display,
                    raw=raw,
                    part_name=part_name,
                    paragraph_index=paragraph_index,
                    start=start,
                    end=end,
                    replacement_supported=replacement_supported,
                )
            )
            number += 1
    return occurrences, number


def _scan_tables(part_name: str, payload: bytes, start_number: int) -> tuple[list[TableRef], int]:
    tables: list[TableRef] = []
    table_stack: list[TableRef] = []
    row_stack: list[dict] = []
    text_stack: list[int] = []
    index = 0
    number = start_number
    while index < len(payload):
        start = payload.find(b"<", index)
        if start < 0:
            break
        end = _markup_end(payload, start)
        kind, name, self_closing = _tag_details(payload[start:end])
        if kind == "start" and not self_closing:
            if name == "tbl":
                table = TableRef(f"T{number:04d}", part_name)
                number += 1
                table_stack.append(table)
            elif name == "tr" and table_stack:
                row_stack.append({"start": start, "texts": [], "slices": []})
            elif name == "t" and row_stack:
                text_stack.append(end)
        elif kind == "end":
            if name == "t" and row_stack and text_stack:
                content_start = text_stack.pop()
                try:
                    value = _decode_text(payload[content_start:start])
                except ElementTree.ParseError:
                    value = ""
                row_stack[-1]["texts"].append(value)
                row_stack[-1]["slices"].append(XmlTextSlice(content_start, start, value))
            elif name == "tr" and table_stack and row_stack:
                row = row_stack.pop()
                table_stack[-1].rows.append(
                    RowRef(row["start"], end, "".join(row["texts"]), tuple(row["slices"]))
                )
            elif name == "tbl" and table_stack:
                tables.append(table_stack.pop())
        index = end
    return tables, number


def scan_docx_occurrences(path: Path) -> tuple[list[Occurrence], list[TableRef], dict[str, int | bool]]:
    preflight = inspect_docx_features(path)
    occurrences: list[Occurrence] = []
    tables: list[TableRef] = []
    occurrence_number = 1
    table_number = 1
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for part_name in supported_text_parts(names):
            payload = archive.read(part_name)
            found, occurrence_number = _scan_part_occurrences(part_name, payload, occurrence_number)
            occurrences.extend(found)
            if b"txbxContent" in payload:
                found, occurrence_number = _scan_part_occurrences(
                    part_name, payload, occurrence_number, text_box_only=True
                )
                occurrences.extend(found)
            found_tables, table_number = _scan_tables(part_name, payload, table_number)
            tables.extend(found_tables)
    return occurrences, tables, preflight


def public_candidate_payload(occurrences: list[Occurrence]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {category: [] for category in TERM_CATEGORY_LABELS}
    for occurrence in occurrences:
        result.setdefault(occurrence.category, []).append(occurrence.public())
    return result


def add_manual_occurrences(
    path: Path,
    existing: list[Occurrence],
    additions: dict[str, list[str]],
) -> tuple[list[Occurrence], list[str]]:
    """Resolve browser-entered terms to exact in-memory occurrences without persisting raw mappings."""
    result = list(existing)
    selected_ids: list[str] = []
    number = max((int(item.occurrence_id[1:]) for item in existing), default=0) + 1
    occupied = {
        (item.part_name, item.paragraph_index, item.start, item.end)
        for item in existing
    }
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for part_name in supported_text_parts(names):
            paragraphs, errors = extract_safe_docx_text_slices(
                archive.read(part_name), EXCLUDED_TEXT_ANCESTORS, None
            )
            if errors:
                raise ValueError(errors[0])
            for paragraph_index, slices in enumerate(paragraphs):
                logical = "".join(item.text for item in slices)
                for category, terms in additions.items():
                    for term in terms:
                        for start, end, raw in _candidate_spans(logical, category, term):
                            key = (part_name, paragraph_index, start, end)
                            if key in occupied:
                                match = next(
                                    item for item in result
                                    if (item.part_name, item.paragraph_index, item.start, item.end) == key
                                )
                                selected_ids.append(match.occurrence_id)
                                continue
                            occurrence = Occurrence(
                                f"O{number:06d}", category, term, raw, part_name,
                                paragraph_index, start, end, True,
                            )
                            number += 1
                            occupied.add(key)
                            result.append(occurrence)
                            selected_ids.append(occurrence.occurrence_id)
    for category, terms in additions.items():
        for term in terms:
            if not any(item.category == category and item.display == term for item in result):
                raise ValueError("manual_addition_not_found")
    return result, list(dict.fromkeys(selected_ids))


def _placeholder_for(occurrence: Occurrence, state: dict) -> str:
    key = (occurrence.category, occurrence.display)
    if key not in state["values"]:
        state["counts"][occurrence.category] += 1
        label = TERM_CATEGORY_LABELS.get(occurrence.category, "变量").split("/")[0]
        state["values"][key] = f"【{label}{state['counts'][occurrence.category]}】"
    return state["values"][key]


def _clear_ranges(tables: list[TableRef], table_actions: dict[str, str]) -> dict[str, list[tuple[int, int]]]:
    result: dict[str, list[tuple[int, int]]] = defaultdict(list)
    by_id = {table.table_id: table for table in tables}
    for table_id, action in table_actions.items():
        if table_id not in by_id or action not in TABLE_ACTIONS:
            raise ValueError("table_action_invalid")
        if action != TABLE_ACTION_CLEAR_DETAILS:
            continue
        table = by_id[table_id]
        preserved = {0, *table.total_rows}
        for index, row in enumerate(table.rows):
            if index not in preserved:
                result[table.part_name].append((row.start, row.end))
    return result


def _patch_supported_part(
    part_name: str,
    payload: bytes,
    selected: list[Occurrence],
    tables: list[TableRef],
    table_actions: dict[str, str],
    placeholder_state: dict,
) -> tuple[bytes, Counter[str], int]:
    paragraphs, errors = extract_safe_docx_text_slices(payload, EXCLUDED_TEXT_ANCESTORS, None)
    if errors:
        raise ValueError(errors[0])
    clear_ranges = _clear_ranges(tables, table_actions).get(part_name, [])
    patches: list[tuple[int, int, bytes]] = []
    counts: Counter[str] = Counter()
    replacements = 0
    grouped: dict[int, list[Occurrence]] = defaultdict(list)
    for occurrence in selected:
        if occurrence.part_name == part_name:
            grouped[occurrence.paragraph_index].append(occurrence)
    for paragraph_index, occurrences in grouped.items():
        if paragraph_index >= len(paragraphs):
            raise ValueError("occurrence_paragraph_missing")
        slices = paragraphs[paragraph_index]
        if slices and any(start <= slices[0].start < end for start, end in clear_ranges):
            continue
        logical = "".join(item.text for item in slices)
        spans: list[tuple[int, int]] = []
        values = [item.text for item in slices]
        cursor = 0
        for value in values:
            spans.append((cursor, cursor + len(value)))
            cursor += len(value)
        occupied: list[tuple[int, int]] = []
        for occurrence in sorted(occurrences, key=lambda item: item.start, reverse=True):
            if logical[occurrence.start:occurrence.end] != occurrence.raw:
                raise ValueError("occurrence_exact_value_changed")
            if any(occurrence.start < end and occurrence.end > start for start, end in occupied):
                raise ValueError("occurrence_selection_overlap")
            occupied.append((occurrence.start, occurrence.end))
            overlap = [
                index
                for index, (start, end) in enumerate(spans)
                if occurrence.start < end and occurrence.end > start
            ]
            if not overlap:
                raise ValueError("occurrence_run_mapping_missing")
            first, last = overlap[0], overlap[-1]
            placeholder = _placeholder_for(occurrence, placeholder_state)
            start_offset = occurrence.start - spans[first][0]
            end_offset = occurrence.end - spans[last][0]
            if first == last:
                values[first] = values[first][:start_offset] + placeholder + values[first][end_offset:]
            else:
                values[first] = values[first][:start_offset] + placeholder
                for index in overlap[1:-1]:
                    values[index] = ""
                values[last] = values[last][end_offset:]
            counts[occurrence.category] += 1
            replacements += 1
        for item, value in zip(slices, values):
            if value != item.text:
                patches.append((item.start, item.end, xml_escape_text(value)))
    for table in tables:
        if table.part_name != part_name or table_actions.get(table.table_id) != TABLE_ACTION_CLEAR_DETAILS:
            continue
        preserved = {0, *table.total_rows}
        for index, row in enumerate(table.rows):
            if index in preserved:
                continue
            for item in row.slices:
                if item.text:
                    patches.append((item.start, item.end, b""))
    output = apply_byte_patches(payload, patches)
    ElementTree.fromstring(output)
    return output, counts, replacements


def residual_audit(path: Path, selected: list[Occurrence]) -> list[str]:
    errors: set[str] = set()
    selected_values = {item.raw for item in selected}
    preflight = inspect_docx_features(path)
    if int(preflight["revision_nodes"]) or bool(preflight["track_revisions_enabled"]):
        errors.add("docx_revision_nodes_remaining")
    if int(preflight["comments"]):
        errors.add("docx_comments_remaining")
    if int(preflight["text_boxes"]):
        errors.add("docx_text_boxes_require_manual_redaction")
    if bool(preflight["macros"]):
        errors.add("docx_macros_blocked")
    if int(preflight["embedded_objects"]):
        errors.add("docx_embedded_objects_blocked")
    if int(preflight["external_relationships"]):
        errors.add("docx_external_relationships_require_review")
    with zipfile.ZipFile(path) as archive:
        text = ""
        field_text = ""
        for part_name in supported_text_parts(set(archive.namelist())):
            try:
                root = ElementTree.fromstring(archive.read(part_name))
            except ElementTree.ParseError:
                errors.add("docx_residual_audit_parse_error")
                continue
            text += "\n" + "".join(element.text or "" for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"t", "delText"})
            field_text += "\n" + "".join(element.text or "" for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "instrText")
        metadata = b"".join(
            archive.read(name)
            for name in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
            if name in archive.namelist()
        ).decode("utf-8", "replace")
    for value in selected_values:
        if value and (value in text or value in metadata):
            errors.add("confirmed_term_remaining")
        if value and value in field_text:
            errors.add("confirmed_term_in_field_code")
    for label, pattern in HIGH_RISK_PATTERNS.items():
        if pattern.search(text):
            errors.add(f"supported_{label}_remaining")
    return sorted(errors)


def create_template_copy(
    source_path: Path,
    output_path: Path,
    occurrences: list[Occurrence],
    tables: list[TableRef],
    selected_ids: list[str],
    table_actions: dict[str, str],
    *,
    word_validation: str = "required",
) -> dict:
    """Create one clean legal-template copy; raw occurrence mapping stays in memory."""
    result = {
        "status": "redaction_failed",
        "redacted_copy_created": False,
        "validation_succeeded": False,
        "error_codes": [],
        "replacement_counts": {},
        "replacements": 0,
        "tables_cleared": sum(action == TABLE_ACTION_CLEAR_DETAILS for action in table_actions.values()),
        "metadata_fields_sanitized": 0,
        "word_native_validation_succeeded": False,
    }
    by_id = {item.occurrence_id: item for item in occurrences}
    if len(set(selected_ids)) != len(selected_ids) or any(item not in by_id for item in selected_ids):
        result["error_codes"] = ["occurrence_selection_invalid"]
        return result
    selected = [by_id[item] for item in selected_ids]
    if not selected and not any(action == TABLE_ACTION_CLEAR_DETAILS for action in table_actions.values()):
        result["status"] = "redaction_skipped_no_confirmed_actions"
        return result
    if any(not item.replacement_supported for item in selected):
        result["error_codes"] = ["text_box_occurrence_requires_manual_redaction"]
        return result
    preflight = inspect_docx_features(source_path)
    blockers = []
    if bool(preflight["macros"]):
        blockers.append("docx_macros_blocked")
    if int(preflight["embedded_objects"]):
        blockers.append("docx_embedded_objects_blocked")
    if int(preflight["revision_nodes"]) or bool(preflight["track_revisions_enabled"]):
        blockers.append("docx_revision_cleanup_required")
    if int(preflight["comments"]):
        blockers.append("docx_comment_cleanup_required")
    if blockers:
        result["error_codes"] = blockers
        return result
    temp_path = output_path.with_name(f".{output_path.stem}.tmp.docx")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.unlink(missing_ok=True)
    placeholder_state = {"counts": Counter(), "values": {}}
    total_counts: Counter[str] = Counter()
    replacements = 0
    metadata_changes = 0
    try:
        with zipfile.ZipFile(source_path) as source_zip, zipfile.ZipFile(temp_path, "w") as target_zip:
            names = set(source_zip.namelist())
            for item in source_zip.infolist():
                payload = source_zip.read(item.filename)
                if item.filename in supported_text_parts(names):
                    payload, counts, changed = _patch_supported_part(
                        item.filename,
                        payload,
                        selected,
                        tables,
                        table_actions,
                        placeholder_state,
                    )
                    total_counts.update(counts)
                    replacements += changed
                payload, changed = sanitise_docx_metadata_part(item.filename, payload)
                metadata_changes += changed
                target_zip.writestr(item, payload)
        valid, errors = validate_docx_ooxml_package(temp_path)
        if not valid:
            result["error_codes"] = errors
            temp_path.unlink(missing_ok=True)
            return result
        residual_errors = residual_audit(temp_path, selected)
        if residual_errors:
            result["error_codes"] = residual_errors
            temp_path.unlink(missing_ok=True)
            return result
        word_ok, word_errors = validate_docx_with_word(temp_path, word_validation)
        if not word_ok:
            result["error_codes"] = word_errors
            temp_path.unlink(missing_ok=True)
            return result
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        temp_path.replace(output_path)
        result.update(
            {
                "status": "redaction_succeeded",
                "redacted_copy_created": True,
                "validation_succeeded": True,
                "replacement_counts": dict(sorted(total_counts.items())),
                "replacements": replacements,
                "metadata_fields_sanitized": metadata_changes,
                "word_native_validation_succeeded": word_validation == "required",
            }
        )
        return result
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, ValueError) as exc:
        temp_path.unlink(missing_ok=True)
        code = str(exc) if str(exc).startswith(("docx_", "occurrence_", "table_")) else "docx_clean_copy_failed"
        result["error_codes"] = [code]
        return result
