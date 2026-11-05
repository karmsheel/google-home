"""A tiny LAN file server for the audio the speaker is asked to fetch.

A Cast ``LOAD`` does not push bytes: the device fetches ``contentId`` itself.
So every utterance needs a URL the speaker can reach. That means:

* bind on all interfaces of the port we advertise,
* advertise the address the routing table picks for that specific speaker,
* answer unauthenticated GET/HEAD with range support (receivers probe with
  range requests and with HEAD),
* and count requests, because "the speaker never fetched the file" is the
  signature of a Windows Firewall block and we want to say so.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}


def content_type_for(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in CONTENT_TYPES:
        return CONTENT_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


class _Handler(BaseHTTPRequestHandler):
    server_version = "hermes-googlehome/0.1"
    protocol_version = "HTTP/1.1"

    # injected by MediaServer via the server instance
    @property
    def media_root(self) -> Path:
        return self.server.media_root  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - stdlib signature
        logger.debug("media server: " + fmt, *args)

    def _note(self, path: str, status: int) -> None:
        server = self.server
        server.request_count += 1  # type: ignore[attr-defined]
        server.last_request_at = time.time()  # type: ignore[attr-defined]
        server.last_request_path = path  # type: ignore[attr-defined]
        server.last_request_status = status  # type: ignore[attr-defined]
        logger.info("media fetch %s -> %s", path, status)

    def _resolve(self) -> Optional[Path]:
        parsed = urlparse(self.path)
        requested = unquote(parsed.path).lstrip("/")
        if not requested or "/" in requested or "\\" in requested or requested.startswith("."):
            return None
        candidate = (self.media_root / requested).resolve()
        try:
            candidate.relative_to(self.media_root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _send_body(self, path: Path) -> None:
        size = path.stat().st_size
        content_type = content_type_for(str(path))
        range_header = self.headers.get("Range")

        start, end = 0, size - 1
        partial = False
        if range_header:
            match = _RANGE_RE.match(range_header.strip())
            if match:
                raw_start, raw_end = match.groups()
                if raw_start:
                    start = int(raw_start)
                if raw_end:
                    end = min(int(raw_end), size - 1)
                if not raw_start and raw_end:
                    start = max(0, size - int(raw_end))
                end = max(start, min(end, size - 1))
                partial = start > 0 or end < size - 1

        length = max(0, end - start + 1)
        self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            self._note(path.name, 206 if partial else 200)
            return
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        self._note(path.name, 206 if partial else 200)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        path = self._resolve()
        if path is None:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            self._note(self.path, 404)
            return
        try:
            self._send_body(path)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("receiver hung up mid-stream for %s", path.name)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()


class MediaServer:
    """Serves generated audio files to Cast devices on the LAN."""

    def __init__(self, media_root: Path, port: int = 0, advertise_host: str = "") -> None:
        self.media_root = Path(media_root)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self.requested_port = port
        self.advertise_host = advertise_host
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> int:
        httpd = ThreadingHTTPServer(("0.0.0.0", self.requested_port), _Handler)
        httpd.daemon_threads = True
        httpd.media_root = self.media_root  # type: ignore[attr-defined]
        httpd.request_count = 0  # type: ignore[attr-defined]
        httpd.last_request_at = 0.0  # type: ignore[attr-defined]
        httpd.last_request_path = ""  # type: ignore[attr-defined]
        httpd.last_request_status = 0  # type: ignore[attr-defined]
        self._server = httpd
        self._thread = threading.Thread(
            target=lambda: httpd.serve_forever(poll_interval=0.1),
            name="gh-media-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("media server listening on 0.0.0.0:%s (root %s)", httpd.server_port, self.media_root)
        return httpd.server_port

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.shutdown()
            server.server_close()

    @property
    def port(self) -> int:
        return self._server.server_port if self._server else 0

    # ── stats (used to diagnose firewall blocks) ─────────────────────────────
    @property
    def stats(self) -> dict:
        server = self._server
        if server is None:
            return {"serving": False}
        return {
            "serving": True,
            "port": server.server_port,
            "advertise_host": self.advertise_host,
            "requests": getattr(server, "request_count", 0),
            "last_request_at": getattr(server, "last_request_at", 0.0),
            "last_request_path": getattr(server, "last_request_path", ""),
            "last_request_status": getattr(server, "last_request_status", 0),
        }

    # ── publishing ───────────────────────────────────────────────────────────
    def url_for(self, filename: str, host: str = "") -> str:
        host = host or self.advertise_host
        return f"http://{host}:{self.port}/{filename}"

    def publish(self, source_path: str, *, name: Optional[str] = None) -> str:
        """Copy (or hard-link) a file into the media root; return its filename."""
        source = Path(source_path)
        filename = name or source.name
        destination = self.media_root / filename
        if not destination.exists():
            try:
                os.link(source, destination)
            except OSError:
                data = source.read_bytes()
                destination.write_bytes(data)
        return filename

    def prune(self, max_age_seconds: float = 3600.0) -> int:
        """Delete published files older than ``max_age_seconds``."""
        cutoff = time.time() - max_age_seconds
        removed = 0
        for entry in self.media_root.iterdir():
            try:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
