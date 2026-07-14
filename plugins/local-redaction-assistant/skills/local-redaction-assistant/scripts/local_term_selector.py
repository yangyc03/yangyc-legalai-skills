#!/usr/bin/env python3
"""Loopback-only DOCX term confirmation for local-redaction-assistant."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from docx_redaction_profile import AI_SHARE_PROFILE, LEGAL_TEMPLATE_PROFILE, TERM_CATEGORIES, TERM_CATEGORY_LABELS

MAX_REQUEST_BYTES = 256 * 1024
MAX_TERMS_PER_CATEGORY = 100
OCCURRENCE_ID_PATTERN = re.compile(r"^O\d{6}$")
TABLE_ID_PATTERN = re.compile(r"^T\d{4}$")
TABLE_ACTIONS = {"term-only", "clear-detail-rows"}


class TermSelectorError(RuntimeError):
    """Raised when the local-only confirmation flow cannot continue safely."""


class LoopbackHTTPServer(ThreadingHTTPServer):
    """HTTP server that never performs reverse DNS lookup for a loopback bind."""

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def _asset_text(name: str) -> str:
    path = Path(__file__).resolve().parents[1] / "assets" / "term-selector" / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TermSelectorError("term_selector_assets_missing") from exc


def _normalise_term(value: object) -> str:
    if not isinstance(value, str):
        raise TermSelectorError("term_selector_term_invalid")
    term = value.strip()
    if len(term) < 2 or len(term) > 80 or any(ord(char) < 32 for char in term):
        raise TermSelectorError("term_selector_term_invalid")
    return term


def _normalise_values(values: object, allowed: set[str]) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_TERMS_PER_CATEGORY:
        raise TermSelectorError("term_selector_selection_invalid")
    selected: list[str] = []
    for value in values:
        term = _normalise_term(value)
        if term not in allowed or term in selected:
            raise TermSelectorError("term_selector_selection_invalid")
        selected.append(term)
    return selected


def _normalise_additions(values: object) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_TERMS_PER_CATEGORY:
        raise TermSelectorError("term_selector_additions_invalid")
    additions: list[str] = []
    for value in values:
        term = _normalise_term(value)
        if term not in additions:
            additions.append(term)
    return additions


def _validate_selection(raw: object, candidates: dict[str, list[dict]]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    if not isinstance(raw, dict):
        raise TermSelectorError("term_selector_request_invalid")
    selected_raw = raw.get("selected", {})
    additions_raw = raw.get("additions", {})
    if not isinstance(selected_raw, dict) or not isinstance(additions_raw, dict):
        raise TermSelectorError("term_selector_request_invalid")

    selected_result: dict[str, list[str]] = {}
    additions_result: dict[str, list[str]] = {}
    for category in TERM_CATEGORIES:
        selected = _normalise_values(
            selected_raw.get(category, []),
            {item["id"] if isinstance(item, dict) else item for item in candidates.get(category, [])},
        )
        additions = _normalise_additions(additions_raw.get(category, []))
        selected_result[category] = selected
        additions_result[category] = additions
    if (
        not any(selected_result.values())
        and not any(additions_result.values())
        and not raw.get("table_actions")
        and not raw.get("auto_high_confidence")
    ):
        raise TermSelectorError("term_selector_no_terms_confirmed")
    return selected_result, additions_result


def _normalise_candidate(category: str, value: object) -> tuple[dict, bool]:
    if isinstance(value, str):
        term = _normalise_term(value)
        return {"id": term, "display": term, "category": category, "part_scope": "正文", "replacement_supported": True}, True
    if not isinstance(value, dict):
        raise TermSelectorError("term_selector_candidate_invalid")
    occurrence_id = value.get("id")
    display = _normalise_term(value.get("display"))
    if not isinstance(occurrence_id, str) or not OCCURRENCE_ID_PATTERN.fullmatch(occurrence_id):
        raise TermSelectorError("term_selector_candidate_invalid")
    part_scope = value.get("part_scope", "其他")
    if not isinstance(part_scope, str) or len(part_scope) > 20:
        raise TermSelectorError("term_selector_candidate_invalid")
    return {
        "id": occurrence_id,
        "display": display,
        "category": category,
        "part_scope": part_scope,
        "replacement_supported": bool(value.get("replacement_supported", True)),
    }, False


def _normalise_tables(values: object) -> list[dict]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > 100:
        raise TermSelectorError("term_selector_tables_invalid")
    result = []
    for value in values:
        if not isinstance(value, dict) or not TABLE_ID_PATTERN.fullmatch(str(value.get("table_id", ""))):
            raise TermSelectorError("term_selector_tables_invalid")
        result.append({
            "table_id": value["table_id"],
            "part_scope": str(value.get("part_scope", "其他"))[:20],
            "row_count": int(value.get("row_count", 0)),
            "header_rows_preserved": list(value.get("header_rows_preserved", [])),
            "total_rows_detected": list(value.get("total_rows_detected", [])),
            "actions": sorted(TABLE_ACTIONS),
        })
    return result


def _normalise_table_actions(raw: object, tables: list[dict]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TermSelectorError("term_selector_table_actions_invalid")
    allowed_ids = {item["table_id"] for item in tables}
    result: dict[str, str] = {}
    for table_id, action in raw.items():
        if table_id not in allowed_ids or action not in TABLE_ACTIONS:
            raise TermSelectorError("term_selector_table_actions_invalid")
        result[table_id] = action
    return result


def _normalise_associations(values: object) -> list[dict[str, str]]:
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_TERMS_PER_CATEGORY:
        raise TermSelectorError("term_selector_associations_invalid")
    result: list[dict[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            raise TermSelectorError("term_selector_associations_invalid")
        full_name = _normalise_term(value.get("full_name"))
        alias = _normalise_term(value.get("alias"))
        item = {"full_name": full_name, "alias": alias}
        if item not in result:
            result.append(item)
    return result


@dataclass
class TermSelectorState:
    file_id: str
    candidates: dict[str, list[dict]]
    token: str
    candidate_associations: list[dict[str, str]] = field(default_factory=list)
    redaction_profile: str = AI_SHARE_PROFILE
    tables: list[dict] = field(default_factory=list)
    auto_occurrence_ids: set[str] = field(default_factory=set)
    legacy_candidates: bool = True
    result: dict[str, Any] | None = None
    result_ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def session_payload(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "candidate_terms": {category: list(self.candidates.get(category, [])) for category in TERM_CATEGORIES},
            "candidate_associations": list(self.candidate_associations),
            "category_labels": dict(TERM_CATEGORY_LABELS),
            "redaction_profile": self.redaction_profile,
            "manual_review_required": True,
            "table_candidates": list(self.tables),
            "high_confidence_count": len(self.auto_occurrence_ids),
        }

    def confirm(self, raw: object) -> None:
        selected, additions = _validate_selection(raw, self.candidates)
        if raw.get("table_actions") and self.redaction_profile != LEGAL_TEMPLATE_PROFILE:
            raise TermSelectorError("term_selector_table_action_profile_forbidden")
        table_actions = _normalise_table_actions(raw.get("table_actions"), self.tables) if isinstance(raw, dict) else {}
        selected_ids = [item for category in TERM_CATEGORIES for item in selected[category]]
        auto_added_ids: set[str] = set()
        if raw.get("auto_high_confidence") and self.redaction_profile != AI_SHARE_PROFILE:
            raise TermSelectorError("term_selector_auto_high_confidence_profile_forbidden")
        auto_high_confidence = bool(raw.get("auto_high_confidence"))
        if auto_high_confidence:
            auto_added_ids = self.auto_occurrence_ids.difference(selected_ids)
            selected_ids.extend(sorted(self.auto_occurrence_ids))
            selected_ids = list(dict.fromkeys(selected_ids))
        auto_counts = {category: 0 for category in TERM_CATEGORIES}
        if auto_added_ids:
            for category in TERM_CATEGORIES:
                auto_counts[category] = sum(
                    1
                    for item in self.candidates.get(category, [])
                    if isinstance(item, dict) and item.get("id") in auto_added_ids
                )
        terms: dict[str, list[str]] = {}
        for category in TERM_CATEGORIES:
            displays = {
                item["id"] if isinstance(item, dict) else item: item["display"] if isinstance(item, dict) else item
                for item in self.candidates.get(category, [])
            }
            terms[category] = [displays[item] for item in selected[category]] + additions[category]
        if not selected_ids and not any(additions.values()) and not table_actions:
            raise TermSelectorError("term_selector_no_terms_confirmed")
        with self.lock:
            self.result = {
                "status": "term_selector_terms_confirmed",
                "selected_term_counts": {
                    category: len(selected[category]) + len(additions[category]) + auto_counts[category]
                    for category in TERM_CATEGORIES
                },
                "terms": terms,
                "selected_occurrence_ids": [] if self.legacy_candidates else selected_ids,
                "manual_additions": additions,
                "table_actions": table_actions,
                "auto_high_confidence": auto_high_confidence,
            }
            self.result_ready.set()

    def cancel(self) -> None:
        with self.lock:
            self.result = {
                "status": "term_selector_cancelled",
                "selected_term_counts": {category: 0 for category in TERM_CATEGORIES},
                "terms": {category: [] for category in TERM_CATEGORIES},
            }
            self.result_ready.set()


def _safe_handler(state: TermSelectorState, server_origin: str):
    html = _asset_text("selector.html")
    css = _asset_text("selector.css")
    javascript = _asset_text("selector.js")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _token_valid(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            supplied = query.get("token", [""])[0]
            return secrets.compare_digest(supplied, state.token)

        def _origin_valid(self) -> bool:
            return self.headers.get("Origin") in {None, "", server_origin}

        def _send(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: HTTPStatus, code: str) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps({"error": code}).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if not self._token_valid():
                self._error(HTTPStatus.FORBIDDEN, "term_selector_access_denied")
                return
            path = urlparse(self.path).path
            if path == "/":
                body = html.replace("__TERM_SELECTOR_TOKEN__", state.token).encode("utf-8")
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", body)
                return
            if path == "/assets/selector.css":
                self._send(HTTPStatus.OK, "text/css; charset=utf-8", css.encode("utf-8"))
                return
            if path == "/assets/selector.js":
                self._send(HTTPStatus.OK, "application/javascript; charset=utf-8", javascript.encode("utf-8"))
                return
            if path == "/session":
                self._send(
                    HTTPStatus.OK,
                    "application/json; charset=utf-8",
                    json.dumps(state.session_payload(), ensure_ascii=False).encode("utf-8"),
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "term_selector_route_missing")

        def do_POST(self) -> None:  # noqa: N802
            if not self._token_valid() or not self._origin_valid():
                self._error(HTTPStatus.FORBIDDEN, "term_selector_access_denied")
                return
            path = urlparse(self.path).path
            if path not in {"/confirm", "/cancel"}:
                self._error(HTTPStatus.NOT_FOUND, "term_selector_route_missing")
                return
            if path == "/cancel":
                state.cancel()
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", b'{"status":"cancelled"}')
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = MAX_REQUEST_BYTES + 1
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "term_selector_request_invalid")
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                state.confirm(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, TermSelectorError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc) or "term_selector_request_invalid")
                return
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", b'{"status":"confirmed"}')

    return Handler


def run_term_selector(
    file_id: str,
    candidates: dict[str, list],
    selector_port: int,
    timeout_seconds: int,
    open_browser: bool,
    *,
    candidate_associations: list[dict[str, str]] | None = None,
    redaction_profile: str = AI_SHARE_PROFILE,
    table_candidates: list[dict] | None = None,
) -> dict[str, Any]:
    normalised_candidates: dict[str, list[dict]] = {}
    legacy_flags: list[bool] = []
    seen_ids: set[str] = set()
    for category in TERM_CATEGORIES:
        normalised_candidates[category] = []
        for value in candidates.get(category, []):
            item, legacy = _normalise_candidate(category, value)
            legacy_flags.append(legacy)
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            normalised_candidates[category].append(item)
    state = TermSelectorState(
        file_id=file_id,
        candidates=normalised_candidates,
        token=secrets.token_urlsafe(32),
        candidate_associations=_normalise_associations(candidate_associations),
        redaction_profile=redaction_profile,
        tables=_normalise_tables(table_candidates),
        legacy_candidates=all(legacy_flags) if legacy_flags else True,
    )
    try:
        server = LoopbackHTTPServer(("127.0.0.1", selector_port), _safe_handler(state, ""))
    except OSError as exc:
        raise TermSelectorError("term_selector_server_start_failed") from exc
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    server.RequestHandlerClass = _safe_handler(state, origin)
    server.timeout = 0.25
    if open_browser:
        webbrowser.open(f"{origin}/?token={state.token}", new=1, autoraise=True)
    print(f"local DOCX term selector ready for {file_id}; browser bound to 127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + timeout_seconds
        while not state.result_ready.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if state.result is None:
            return {
                "status": "term_selector_timed_out",
                "selected_term_counts": {category: 0 for category in TERM_CATEGORIES},
                "terms": {category: [] for category in TERM_CATEGORIES},
            }
        return state.result
    finally:
        server.server_close()
