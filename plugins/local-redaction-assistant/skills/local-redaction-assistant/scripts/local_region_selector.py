#!/usr/bin/env python3
"""Loopback-only manual region selector for local-redaction-assistant."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


MAX_REQUEST_BYTES = 64 * 1024
MAX_REGIONS = 100
MAX_IMAGE_PIXELS = 80_000_000
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class LoopbackHTTPServer(ThreadingHTTPServer):
    """HTTP server that never performs reverse DNS lookup for a loopback bind."""

    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class RegionSelectorError(RuntimeError):
    """Raised for safe, user-actionable selector failures."""


def _asset_text(name: str) -> str:
    path = Path(__file__).resolve().parents[1] / "assets" / "region-selector" / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RegionSelectorError("region_selector_assets_missing") from exc


def _load_pymupdf():
    try:
        import pymupdf  # type: ignore
    except ImportError as exc:
        raise RegionSelectorError("region_selector_pymupdf_missing") from exc
    return pymupdf


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
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
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise RegionSelectorError("region_selector_regions_write_error") from exc


def _as_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RegionSelectorError("region_selector_region_invalid")
    return float(value)


def _as_page(value: object, page_count: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > page_count:
        raise RegionSelectorError("region_selector_page_invalid")
    return value


def _validate_regions(raw_regions: object, page_count: int) -> list[dict[str, float | int | str]]:
    if not isinstance(raw_regions, list) or not raw_regions or len(raw_regions) > MAX_REGIONS:
        raise RegionSelectorError("region_selector_regions_invalid")

    regions: list[dict[str, float | int | str]] = []
    for index, item in enumerate(raw_regions, start=1):
        if not isinstance(item, dict):
            raise RegionSelectorError("region_selector_region_invalid")
        page = _as_page(item.get("page"), page_count)
        x0 = _as_number(item.get("x0"))
        y0 = _as_number(item.get("y0"))
        x1 = _as_number(item.get("x1"))
        y1 = _as_number(item.get("y1"))
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise RegionSelectorError("region_selector_region_bounds_invalid")
        if x1 - x0 < 0.001 or y1 - y0 < 0.001:
            raise RegionSelectorError("region_selector_region_too_small")
        regions.append(
            {
                "page": page,
                "x0": round(x0, 6),
                "y0": round(y0, 6),
                "x1": round(x1, 6),
                "y1": round(y1, 6),
                "label": f"manual_area_{index}",
            }
        )
    return regions


@dataclass
class SelectorState:
    source_path: Path
    source_kind: str
    file_id: str
    page_count: int
    preview_dpi: int
    regions_path: Path
    token: str
    generation_confirmation_required: bool = False
    page_cache: dict[int, bytes] = field(default_factory=dict)
    pages_rendered: set[int] = field(default_factory=set)
    regions_saved: int = 0
    regions_are_saved: bool = False
    result: dict[str, Any] | None = None
    result_ready: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def session_payload(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "source_kind": self.source_kind,
            "page_count": self.page_count,
            "generation_confirmation_required": self.generation_confirmation_required,
            "manual_review_required": True,
        }

    def page_png(self, page_number: int) -> bytes:
        if page_number < 1 or page_number > self.page_count:
            raise RegionSelectorError("region_selector_page_invalid")
        with self.lock:
            cached = self.page_cache.get(page_number)
        if cached is not None:
            return cached

        pymupdf = _load_pymupdf()
        try:
            if self.source_kind == "pdf":
                document = pymupdf.open(self.source_path)
                try:
                    page = document.load_page(page_number - 1)
                    matrix = pymupdf.Matrix(self.preview_dpi / 72.0, self.preview_dpi / 72.0)
                    pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                finally:
                    document.close()
            else:
                pixmap = pymupdf.Pixmap(str(self.source_path))
            if pixmap.width <= 0 or pixmap.height <= 0 or pixmap.width * pixmap.height > MAX_IMAGE_PIXELS:
                raise RegionSelectorError("region_selector_preview_dimensions_invalid")
            rendered = pixmap.tobytes("png")
        except RegionSelectorError:
            raise
        except Exception as exc:
            raise RegionSelectorError("region_selector_preview_render_error") from exc

        with self.lock:
            self.page_cache[page_number] = rendered
            self.pages_rendered.add(page_number)
        return rendered

    def save_regions(self, raw_regions: object) -> None:
        regions = _validate_regions(raw_regions, self.page_count)
        payload: dict[str, Any]
        if self.source_kind == "pdf":
            payload = {
                "schema_version": "0.5",
                "coordinate_system": "page_ratio",
                "source": "local_region_selector",
                "local_sensitive_file": True,
                "manual_selection_required": True,
                "files": [{"file_id": self.file_id, "regions": regions}],
            }
        else:
            payload = {
                "schema_version": "0.7",
                "coordinate_system": "image_ratio",
                "source": "local_region_selector",
                "local_sensitive_file": True,
                "manual_selection_required": True,
                "files": [{"file_id": self.file_id, "regions": regions}],
            }
        _atomic_write_json(self.regions_path, payload)
        with self.lock:
            self.regions_saved = len(regions)
            self.regions_are_saved = True
            if self.generation_confirmation_required:
                return
            self.result = {
                "status": "region_selector_regions_saved",
                "regions_saved": len(regions),
                "pages_rendered": len(self.pages_rendered),
            }
            self.result_ready.set()

    def confirm_generation(self) -> None:
        with self.lock:
            if not self.regions_are_saved:
                raise RegionSelectorError("region_selector_generate_requires_saved_regions")
            self.result = {
                "status": "region_selector_generation_confirmed",
                "regions_saved": self.regions_saved,
                "pages_rendered": len(self.pages_rendered),
            }
            self.result_ready.set()

    def cancel(self) -> None:
        with self.lock:
            self.result = {
                "status": "region_selector_cancelled",
                "regions_saved": 0,
                "pages_rendered": len(self.pages_rendered),
            }
            self.result_ready.set()


def _safe_handler(state: SelectorState, server_origin: str):
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
            origin = self.headers.get("Origin")
            return origin in {None, "", server_origin}

        def _send(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: HTTPStatus, code: str) -> None:
            self._send(status, "application/json; charset=utf-8", json.dumps({"error": code}).encode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if not self._token_valid():
                self._error(HTTPStatus.FORBIDDEN, "region_selector_access_denied")
                return
            path = urlparse(self.path).path
            if path == "/":
                body = html.replace("__SELECTOR_TOKEN__", state.token).encode("utf-8")
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
            if path.startswith("/page/") and path.endswith(".png"):
                try:
                    page_number = int(path.removeprefix("/page/").removesuffix(".png"))
                    self._send(HTTPStatus.OK, "image/png", state.page_png(page_number))
                except (ValueError, RegionSelectorError):
                    self._error(HTTPStatus.BAD_REQUEST, "region_selector_preview_unavailable")
                return
            self._error(HTTPStatus.NOT_FOUND, "region_selector_route_missing")

        def do_POST(self) -> None:  # noqa: N802
            if not self._token_valid() or not self._origin_valid():
                self._error(HTTPStatus.FORBIDDEN, "region_selector_access_denied")
                return
            path = urlparse(self.path).path
            if path not in {"/save", "/generate", "/cancel"}:
                self._error(HTTPStatus.NOT_FOUND, "region_selector_route_missing")
                return
            if path == "/cancel":
                state.cancel()
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", b'{"status":"cancelled"}')
                return
            if path == "/generate":
                try:
                    state.confirm_generation()
                except RegionSelectorError as exc:
                    self._error(HTTPStatus.BAD_REQUEST, str(exc) or "region_selector_generation_invalid")
                    return
                self._send(HTTPStatus.OK, "application/json; charset=utf-8", b'{"status":"generation_confirmed"}')
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = MAX_REQUEST_BYTES + 1
            if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
                self._error(HTTPStatus.BAD_REQUEST, "region_selector_request_invalid")
                return
            try:
                payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise RegionSelectorError("region_selector_request_invalid")
                state.save_regions(payload.get("regions"))
            except (UnicodeDecodeError, json.JSONDecodeError, RegionSelectorError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc) or "region_selector_request_invalid")
                return
            self._send(HTTPStatus.OK, "application/json; charset=utf-8", b'{"status":"saved"}')

    return Handler


def _inspect_source(source_path: Path, source_kind: str) -> int:
    pymupdf = _load_pymupdf()
    try:
        if source_kind == "pdf":
            document = pymupdf.open(source_path)
            try:
                if getattr(document, "needs_pass", False) or document.page_count < 1:
                    raise RegionSelectorError("region_selector_pdf_unavailable")
                return document.page_count
            finally:
                document.close()
        pixmap = pymupdf.Pixmap(str(source_path))
        if pixmap.width <= 0 or pixmap.height <= 0 or pixmap.width * pixmap.height > MAX_IMAGE_PIXELS:
            raise RegionSelectorError("region_selector_image_unavailable")
        return 1
    except RegionSelectorError:
        raise
    except Exception as exc:
        raise RegionSelectorError("region_selector_source_unavailable") from exc


def run_region_selector(
    source_path: Path,
    file_id: str,
    regions_path: Path,
    preview_dpi: int,
    selector_port: int,
    timeout_seconds: int,
    open_browser: bool,
    overwrite_regions: bool,
    generation_confirmation_required: bool = False,
) -> dict[str, Any]:
    suffix = source_path.suffix.lower()
    if suffix == ".pdf":
        source_kind = "pdf"
    elif suffix in IMAGE_SUFFIXES:
        source_kind = "image"
    else:
        raise RegionSelectorError("region_selector_input_type_unsupported")
    if regions_path.exists() and not overwrite_regions:
        raise RegionSelectorError("region_selector_regions_output_exists")
    page_count = _inspect_source(source_path, source_kind)
    state = SelectorState(
        source_path=source_path,
        source_kind=source_kind,
        file_id=file_id,
        page_count=page_count,
        preview_dpi=preview_dpi,
        regions_path=regions_path,
        token=secrets.token_urlsafe(32),
        generation_confirmation_required=generation_confirmation_required,
    )
    try:
        server = LoopbackHTTPServer(("127.0.0.1", selector_port), _safe_handler(state, ""))
    except OSError as exc:
        raise RegionSelectorError("region_selector_server_start_failed") from exc
    host, port = server.server_address[:2]
    origin = f"http://{host}:{port}"
    server.RequestHandlerClass = _safe_handler(state, origin)
    server.timeout = 0.25
    url = f"{origin}/?token={state.token}"
    if open_browser:
        webbrowser.open(url, new=1, autoraise=True)
    print(f"local region selector ready for {file_id}; browser bound to 127.0.0.1:{port}")
    try:
        deadline = time.monotonic() + timeout_seconds
        while not state.result_ready.is_set() and time.monotonic() < deadline:
            server.handle_request()
        if state.result is None:
            return {
                "status": "region_selector_timed_out",
                "regions_saved": 0,
                "pages_rendered": len(state.pages_rendered),
                "page_count": page_count,
                "source_kind": source_kind,
            }
        return {**state.result, "page_count": page_count, "source_kind": source_kind}
    finally:
        server.server_close()
