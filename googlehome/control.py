"""Loopback control API: the channel Hermes Desktop talks to.

The adapter lives inside the gateway process, which is where a turn must be
injected and where the Cast session lives. The Desktop extension therefore
needs a door into *that* process; this is it. It binds 127.0.0.1 only and
requires the per-install token that ``config.ensure_token()`` writes to
``control-token`` (and mirrors into ``control.json`` so the Desktop plugin can
find both the port and the token).

Endpoints
---------
GET  /status     adapter + device state
GET  /devices    mDNS discovery results
POST /ask        {"text": "..."}            run a turn as the bound profile
POST /speak      {"text": "..."}            speak text, no agent turn
POST /listen     {"data_url": "data:audio/...;base64,..."}  transcribe + turn
POST /device     {"device": "ip or name"}   re-target the speaker
POST /profile    {"profile": "name"}        re-bind the profile
POST /volume     {"level": 0-100}
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_BODY = 30 * 1024 * 1024  # 30 MiB: matches the core transcription upload cap


class ControlServer:
    """Threaded HTTP server bridged onto the adapter's asyncio loop."""

    def __init__(self, adapter: Any, *, port: int = 8765, token: str = "") -> None:
        self.adapter = adapter
        self.requested_port = port
        self.token = token or ""
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> int:
        handler = _make_handler(self)
        last_error: Optional[Exception] = None
        for offset in range(0, 11):
            port = self.requested_port + offset
            if port > 65535:
                break
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError as exc:
                last_error = exc
                continue
            httpd.daemon_threads = True
            httpd.control = self  # type: ignore[attr-defined]
            self._server = httpd
            self._thread = threading.Thread(target=httpd.serve_forever,
                                            name="gh-control", daemon=True)
            self._thread.start()
            logger.info("[googlehome] control API on http://127.0.0.1:%s", port)
            return port
        raise last_error or OSError("could not bind the control port")

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def port(self) -> int:
        return self._server.server_port if self._server else 0

    # ── bridge to asyncio ────────────────────────────────────────────────────
    def run(self, coro: Any, timeout: float = 180.0) -> Any:
        loop = getattr(self.adapter, "_loop", None)
        if loop is None or not loop.is_running():
            raise RuntimeError("the Hermes gateway loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    def authorized(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self.token:
            return True
        supplied = handler.headers.get("X-Hermes-Token", "")
        if not supplied:
            query = handler.path.split("?", 1)[1] if "?" in handler.path else ""
            for pair in query.split("&"):
                if pair.startswith("token="):
                    supplied = pair[len("token="):]
                    break
        return supplied == self.token


def _make_handler(server_ref: Any) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "hermes-googlehome/0.1"
        protocol_version = "HTTP/1.1"

        # ── helpers ─────────────────────────────────────────────────────────
        @property
        def control(self) -> ControlServer:
            return self.server.control  # type: ignore[attr-defined]

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            logger.debug("control: " + fmt, *args)

        def _cors(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Hermes-Token")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

        def _reply(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _error(self, status: int, message: str) -> None:
            self._reply(status, {"ok": False, "error": message})

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            if length > _MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type.startswith("audio/") or content_type == "video/webm":
                encoded = base64.b64encode(raw).decode("ascii")
                return {"data_url": f"data:{content_type};base64,{encoded}",
                        "mime_type": content_type}
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"invalid JSON body: {exc}") from exc

        def _guard(self) -> bool:
            if not self.control.authorized(self):
                self._error(HTTPStatus.UNAUTHORIZED, "missing or invalid X-Hermes-Token")
                return False
            return True

        # ── verbs ───────────────────────────────────────────────────────────
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            if path in ("/", "/health"):
                self._reply(200, {"ok": True, "service": "hermes-googlehome"})
                return
            if path == "/status":
                try:
                    self._reply(200, self.control.run(self.control.adapter.status(), timeout=20))
                except Exception as exc:  # noqa: BLE001
                    self._error(500, str(exc))
                return
            if path == "/devices":
                try:
                    from . import discovery

                    devices = discovery.discover(timeout=3.0)
                    self._reply(200, {"ok": True, "devices": devices})
                except Exception as exc:  # noqa: BLE001
                    self._error(500, str(exc))
                return
            if path == "/info":
                self._reply(200, {"ok": True, **self.control.adapter.control_info()})
                return
            self._error(404, f"unknown path {path}")

        def do_POST(self) -> None:  # noqa: N802
            if not self._guard():
                return
            path = self.path.split("?", 1)[0].rstrip("/")
            try:
                body = self._body()
            except ValueError as exc:
                self._error(400, str(exc))
                return
            adapter = self.control.adapter
            try:
                if path == "/ask":
                    text = str(body.get("text") or "").strip()
                    if not text:
                        self._error(400, "text is required")
                        return
                    submitted = self.control.run(
                        adapter.submit_utterance(text, origin="desktop")
                    )
                    self._reply(200, {"ok": True, "submitted": bool(submitted), "text": text})
                    return
                if path == "/speak":
                    text = str(body.get("text") or "").strip()
                    if not text:
                        self._error(400, "text is required")
                        return
                    result = self.control.run(adapter.speak_text(text))
                    self._reply(200 if result.get("ok") else 500, result)
                    return
                if path == "/listen":
                    data_url = str(body.get("data_url") or "")
                    if not data_url.startswith("data:") or "," not in data_url:
                        self._error(400, "data_url must be a base64 data URL")
                        return
                    header, encoded = data_url.split(",", 1)
                    mime = str(body.get("mime_type") or header[5:].split(";", 1)[0] or "audio/webm")
                    try:
                        audio = base64.b64decode(encoded, validate=True)
                    except (binascii.Error, ValueError):
                        self._error(400, "data_url is not valid base64")
                        return
                    result = self.control.run(adapter.submit_audio(audio, mime))
                    self._reply(200 if result.get("ok") else 400, result)
                    return
                if path == "/device":
                    spec = str(body.get("device") or "")
                    result = self.control.run(adapter.set_device(spec))
                    self._reply(200, {"ok": True, "status": result})
                    return
                if path == "/profile":
                    name = str(body.get("profile") or "")
                    result = self.control.run(adapter.set_profile(name))
                    self._reply(200, {"ok": True, "status": result})
                    return
                if path == "/volume":
                    try:
                        level = int(body.get("level"))
                    except (TypeError, ValueError):
                        self._error(400, "level must be an integer 0-100")
                        return
                    result = self.control.run(adapter.set_volume(level))
                    self._reply(200 if result.get("ok") else 500, result)
                    return
                if path == "/refresh":
                    result = self.control.run(adapter.refresh_device(), timeout=30)
                    self._reply(200, {"ok": True, "status": result})
                    return
            except Exception as exc:  # noqa: BLE001 - report, never crash the thread
                logger.exception("[googlehome] control %s failed", path)
                self._error(500, str(exc))
                return
            self._error(404, f"unknown path {path}")

    return Handler
