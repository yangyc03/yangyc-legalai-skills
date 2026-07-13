#!/usr/bin/env python3
"""Loopback-only confirmation before Word accepts revisions or removes comments."""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse


class CleaningSelectorError(RuntimeError):
    pass


HTML = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Word 清洁副本确认</title><link rel="stylesheet" href="/style.css?token=__TOKEN__"></head><body><main><p class="eyebrow">LOCAL-ONLY</p><h1>Word 清洁副本确认</h1><p id="file"></p><section id="summary"></section><label id="revision-row"><input id="accept-revisions" type="checkbox"> 接受全部修订，并关闭继续跟踪</label><label id="comment-row"><input id="remove-comments" type="checkbox"> 移除全部批注</label><p class="warning">操作只作用于安全编号临时副本。未经确认不会执行；清洁后若仍有修订节点将停止。</p><div class="actions"><button id="cancel">取消</button><button id="confirm">确认清洁临时副本</button></div><p id="status"></p></main><script src="/app.js?token=__TOKEN__"></script></body></html>'''
CSS = ''':root{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;color:#172033;background:#f4f6fa}main{max-width:720px;margin:40px auto;padding:28px;background:#fff;border:1px solid #d8deea;border-radius:8px}.eyebrow{color:#526781;font-size:12px;letter-spacing:.08em}label{display:block;margin:14px 0;padding:12px;background:#f7f9fc}.warning{padding:14px;background:#fff4dd;color:#6a4808}.actions{display:flex;justify-content:flex-end;gap:10px}button{padding:9px 14px}#confirm{background:#1267d6;color:#fff;border:1px solid #0f5cc0}'''
JS = '''(() => {const token=new URLSearchParams(location.search).get("token")||"";const ep=p=>`${p}?token=${encodeURIComponent(token)}`;let session;const status=document.getElementById("status");fetch(ep("/session"),{cache:"no-store"}).then(r=>r.json()).then(data=>{session=data;document.getElementById("file").textContent=`${data.file_id} · DOCX`;document.getElementById("summary").textContent=`修订节点：${data.revision_nodes}；批注：${data.comments}`;const ar=document.getElementById("accept-revisions");const rc=document.getElementById("remove-comments");ar.checked=data.revision_nodes>0;rc.checked=data.comments>0;document.getElementById("revision-row").hidden=data.revision_nodes===0;document.getElementById("comment-row").hidden=data.comments===0;}).catch(()=>status.textContent="无法连接本地确认页");async function post(path,payload){const r=await fetch(ep(path),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload),cache:"no-store"});if(!r.ok)throw new Error("failed");}document.getElementById("confirm").onclick=async()=>{try{await post("/confirm",{accept_revisions:document.getElementById("accept-revisions").checked,remove_comments:document.getElementById("remove-comments").checked});status.textContent="已确认，正在生成清洁临时副本。";}catch(_){status.textContent="确认不完整，未执行。";}};document.getElementById("cancel").onclick=async()=>{try{await post("/cancel",{});}catch(_){}status.textContent="已取消。";};})();'''


class Server(ThreadingHTTPServer):
    def server_bind(self) -> None:
        self.socket.bind(self.server_address)
        self.server_address = self.socket.getsockname()
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class State:
    def __init__(self, file_id: str, preflight: dict[str, Any]):
        self.file_id = file_id
        self.preflight = preflight
        self.token = secrets.token_urlsafe(32)
        self.result: dict | None = None
        self.ready = threading.Event()


def _handler(state: State, origin: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *args: object) -> None:
            return

        def valid(self) -> bool:
            return secrets.compare_digest(parse_qs(urlparse(self.path).query).get("token", [""])[0], state.token)

        def send_payload(self, status: HTTPStatus, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802
            if not self.valid():
                self.send_payload(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"access_denied"}')
                return
            path = urlparse(self.path).path
            if path == "/":
                self.send_payload(HTTPStatus.OK, "text/html; charset=utf-8", HTML.replace("__TOKEN__", state.token).encode())
            elif path == "/style.css":
                self.send_payload(HTTPStatus.OK, "text/css; charset=utf-8", CSS.encode())
            elif path == "/app.js":
                self.send_payload(HTTPStatus.OK, "application/javascript; charset=utf-8", JS.encode())
            elif path == "/session":
                payload = {
                    "file_id": state.file_id,
                    "revision_nodes": int(state.preflight.get("revision_nodes", 0)),
                    "comments": int(state.preflight.get("comments", 0)),
                }
                self.send_payload(HTTPStatus.OK, "application/json; charset=utf-8", json.dumps(payload).encode())
            else:
                self.send_payload(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"route_missing"}')

        def do_POST(self) -> None:  # noqa: N802
            if not self.valid() or self.headers.get("Origin") not in {None, "", origin}:
                self.send_payload(HTTPStatus.FORBIDDEN, "application/json", b'{"error":"access_denied"}')
                return
            path = urlparse(self.path).path
            if path == "/cancel":
                state.result = {"status": "cleaning_cancelled"}
                state.ready.set()
                self.send_payload(HTTPStatus.OK, "application/json", b'{"status":"cancelled"}')
                return
            if path != "/confirm":
                self.send_payload(HTTPStatus.NOT_FOUND, "application/json", b'{"error":"route_missing"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    raise ValueError
                raw = json.loads(self.rfile.read(length).decode())
            except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
                self.send_payload(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"request_invalid"}')
                return
            accept = raw.get("accept_revisions") is True
            remove = raw.get("remove_comments") is True
            if int(state.preflight.get("revision_nodes", 0)) and not accept:
                self.send_payload(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"revision_confirmation_required"}')
                return
            if int(state.preflight.get("comments", 0)) and not remove:
                self.send_payload(HTTPStatus.BAD_REQUEST, "application/json", b'{"error":"comment_confirmation_required"}')
                return
            state.result = {"status": "cleaning_confirmed", "accept_revisions": accept, "remove_comments": remove}
            state.ready.set()
            self.send_payload(HTTPStatus.OK, "application/json", b'{"status":"confirmed"}')

    return Handler


def run_cleaning_selector(file_id: str, preflight: dict, port: int, timeout_seconds: int, open_browser: bool) -> dict:
    state = State(file_id, preflight)
    try:
        server = Server(("127.0.0.1", port), _handler(state, ""))
    except OSError as exc:
        raise CleaningSelectorError("cleaning_selector_server_start_failed") from exc
    host, actual_port = server.server_address[:2]
    origin = f"http://{host}:{actual_port}"
    server.RequestHandlerClass = _handler(state, origin)
    server.timeout = 0.25
    if open_browser:
        webbrowser.open(f"{origin}/?token={state.token}", new=1, autoraise=True)
    try:
        deadline = time.monotonic() + timeout_seconds
        while not state.ready.is_set() and time.monotonic() < deadline:
            server.handle_request()
        return state.result or {"status": "cleaning_timed_out"}
    finally:
        server.server_close()
