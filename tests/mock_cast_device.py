"""A fake Cast device for tests.

Implements just enough of the Cast v2 receiver + media namespaces to exercise
``googlehome.castdevice.CastClient`` without real hardware: TLS on an ephemeral
port, CONNECT/heartbeat, GET_STATUS, LAUNCH, LOAD, SET_VOLUME, STOP.

The certificate is the repo's self-signed test fixture (localhost only).
"""
from __future__ import annotations

import contextlib
import json
import socket
import ssl
import threading
import time
from pathlib import Path

from googlehome import castproto as p

FIXTURES = Path(__file__).with_name("fixtures")
CERT = FIXTURES / "mock-cert.pem"
KEY = FIXTURES / "mock-key.pem"

APP_TRANSPORT = "web-1"


class MockCastDevice:
    """Runs a TLS Cast endpoint in a background thread."""

    def __init__(self, finish_after: float = 0.15, port: int = 0) -> None:
        self.finish_after = finish_after
        self.volume = 0.5
        self.muted = False
        self.app_running = False
        self.loaded_urls: list[str] = []
        self.loaded_content_types: list[str] = []
        self.session_counter = 0
        self.current_state = "IDLE"
        self.current_session = 0
        self._state_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._context.load_cert_chain(str(CERT), str(KEY))
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", port))
        self._listener.listen(4)
        self._listener.settimeout(0.2)
        self.port = self._listener.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> "MockCastDevice":
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):
            self._listener.close()
        for thread in self._threads:
            thread.join(timeout=2)

    def __enter__(self) -> "MockCastDevice":
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                raw, _addr = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            thread = threading.Thread(target=self._serve, args=(raw,), daemon=True)
            thread.start()
            self._threads.append(thread)

    # ── per-connection ───────────────────────────────────────────────────────
    def _serve(self, raw: socket.socket) -> None:
        try:
            conn = self._context.wrap_socket(raw, server_side=True)
        except (ssl.SSLError, OSError):
            raw.close()
            return
        reader = conn.makefile("rb")
        sender = "sender-0"
        connected: set[str] = set()
        try:
            while not self._stop.is_set():
                frame = p.read_frame(reader)
                # Reply to whoever asked: sender ids are per client, not a constant.
                sender = frame.get("source_id") or sender
                namespace = frame["namespace"]
                payload = frame["payload"]
                kind = payload.get("type")
                if kind == p.MSG_PING:
                    self._send(conn, p.NS_HEARTBEAT, p.RECEIVER_ID, sender, {"type": p.MSG_PONG})
                elif kind == p.MSG_CONNECT:
                    connected.add(str(frame.get("destination_id") or ""))
                    continue
                elif namespace == p.NS_RECEIVER:
                    self._handle_receiver(conn, sender, payload)
                elif namespace == p.NS_MEDIA:
                    if str(frame.get("destination_id") or "") not in connected:
                        # Real receivers boot a sender that talks to a transport it never opened.
                        self._send(conn, p.NS_CONNECTION, p.RECEIVER_ID, sender, {"type": p.MSG_CLOSE})
                        return
                    self._handle_media(conn, sender, payload)
        except (p.CastProtocolError, OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(OSError):
                reader.close()
            with contextlib.suppress(OSError):
                conn.close()

    def _send(self, conn, namespace, source, destination, payload) -> None:
        message = p.encode_message(source, destination, namespace, payload)
        with self._send_lock:
            conn.sendall(p.frame(message))

    def _receiver_status(self, request_id=None) -> dict:
        volumes = {"level": self.volume, "muted": self.muted}
        apps = []
        if self.app_running:
            apps.append(
                {
                    "appId": p.DEFAULT_MEDIA_RECEIVER,
                    "displayName": "Default Media Receiver",
                    "sessionId": "session-1",
                    "transportId": APP_TRANSPORT,
                    "statusText": "Ready To Cast",
                    "namespaces": [
                        {"name": p.NS_CONNECTION},
                        {"name": p.NS_MEDIA},
                    ],
                }
            )
        status = {"type": "RECEIVER_STATUS", "status": {"volume": volumes, "applications": apps}}
        if request_id is not None:
            status["requestId"] = request_id
        return status

    def _handle_receiver(self, conn, sender, payload) -> None:
        kind = payload.get("type")
        request_id = payload.get("requestId")
        if kind == "GET_STATUS":
            self._send(conn, p.NS_RECEIVER, p.RECEIVER_ID, sender, self._receiver_status(request_id))
        elif kind == "LAUNCH":
            self.app_running = True
            self._send(conn, p.NS_RECEIVER, p.RECEIVER_ID, sender, self._receiver_status(request_id))
        elif kind == "SET_VOLUME":
            volume = payload.get("volume") or {}
            if "level" in volume:
                self.volume = float(volume["level"])
            if "muted" in volume:
                self.muted = bool(volume["muted"])
            self._send(conn, p.NS_RECEIVER, p.RECEIVER_ID, sender, self._receiver_status(request_id))
        elif kind == "STOP":
            self.app_running = False
            self._send(
                conn, p.NS_RECEIVER, p.RECEIVER_ID, sender,
                {"type": "RECEIVER_STATUS", "requestId": request_id,
                 "status": {"volume": {"level": self.volume, "muted": self.muted}, "applications": []}},
            )

    def _media_status(self, request_id, state=None, session_id=None, duration=1.0) -> dict:
        state = self.current_state if state is None else state
        session_id = self.current_session if session_id is None else session_id
        return {
            "type": "MEDIA_STATUS",
            "requestId": request_id,
            "status": [
                {
                    "mediaSessionId": session_id,
                    "playerState": state,
                    "idleReason": "FINISHED" if state == "IDLE" else None,
                    "media": {"contentId": self.loaded_urls[-1] if self.loaded_urls else "",
                              "duration": duration},
                }
            ],
        }

    def _handle_media(self, conn, sender, payload) -> None:
        kind = payload.get("type")
        request_id = payload.get("requestId")
        if kind == "LOAD":
            media = payload.get("media") or {}
            self.loaded_urls.append(str(media.get("contentId") or ""))
            self.loaded_content_types.append(str(media.get("contentType") or ""))
            with self._state_lock:
                self.session_counter += 1
                self.current_session = self.session_counter
                self.current_state = "PLAYING"
            session_id = self.current_session
            self._send(conn, p.NS_MEDIA, APP_TRANSPORT, sender,
                       self._media_status(request_id, "PLAYING", session_id))
            if self.finish_after is not None:
                def finish() -> None:
                    time.sleep(self.finish_after)
                    with self._state_lock:
                        if self.current_session == session_id:
                            self.current_state = "IDLE"
                    with contextlib.suppress(OSError):
                        self._send(conn, p.NS_MEDIA, APP_TRANSPORT, sender,
                                   self._media_status(None, "IDLE", session_id))
                threading.Thread(target=finish, daemon=True).start()
        elif kind == "GET_STATUS":
            self._send(conn, p.NS_MEDIA, APP_TRANSPORT, sender, self._media_status(request_id))
        elif kind == "STOP":
            with self._state_lock:
                self.current_state = "IDLE"
            self._send(conn, p.NS_MEDIA, APP_TRANSPORT, sender, self._media_status(request_id, "IDLE"))
        else:
            self._send(conn, p.NS_MEDIA, APP_TRANSPORT, sender,
                       {"type": "INVALID_REQUEST", "requestId": request_id, "reason": kind})
