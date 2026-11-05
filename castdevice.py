"""A small, dependency-free Google Cast v2 client.

Speaks the same protocol as pychromecast / node-castv2, but only the subset a
smart speaker needs: connect, receiver status, launch the Default Media
Receiver, LOAD an audio URL, device volume, and stop.

Everything here is blocking, so the asyncio gateway calls it through
``asyncio.to_thread``. A background thread sends heartbeat PINGs: without them
the device closes the socket after ~10 s and the next ``play_url`` would stall.
"""
from __future__ import annotations

import json
import logging
import socket
import ssl
import threading
import time
from typing import Any, Callable, Optional

from . import castproto as p

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 8.0
HEARTBEAT_INTERVAL = 5.0


class CastError(Exception):
    """Base error for Cast failures."""


class CastConnectionError(CastError):
    """Transport-level failure: the device was unreachable or dropped us."""


class CastCommandError(CastError):
    """The device answered, but with an error payload."""


class CastTimeout(CastError):
    """A read timed out. The connection is still usable; callers just retry."""


class CastClient:
    """One TLS connection to one Cast device."""

    def __init__(
        self,
        host: str,
        port: int = 8009,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        source_id: str = "sender-0",
        heartbeat: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.source_id = source_id
        self._heartbeat_enabled = heartbeat

        self._sock: Optional[ssl.SSLSocket] = None
        self._reader = None
        self._send_lock = threading.Lock()
        self._request_id = 0
        self._request_lock = threading.Lock()
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._transport_id: Optional[str] = None
        self._closed = True
        self.last_status: dict[str, Any] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._sock is not None:
            return
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Cast devices present a self-signed certificate; senders never verify it.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            raw = socket.create_connection((self.host, self.port), timeout=self.timeout)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock = context.wrap_socket(raw, server_hostname=None)
        except (OSError, ssl.SSLError) as exc:
            raise CastConnectionError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc

        sock.settimeout(self.timeout)
        self._sock = sock
        self._reader = sock.makefile("rb")
        self._closed = False

        # Open the virtual channel to the platform (receiver-0).
        self._send(p.NS_CONNECTION, p.RECEIVER_ID, {"type": p.MSG_CONNECT})

        if self._heartbeat_enabled:
            self._heartbeat_stop.clear()
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop, name=f"cast-heartbeat-{self.host}", daemon=True
            )
            self._heartbeat_thread.start()
        logger.debug("connected to Cast device %s:%s", self.host, self.port)

    def close(self) -> None:
        self._heartbeat_stop.set()
        self._closed = True
        sock, reader = self._sock, self._reader
        self._sock, self._reader = None, None
        self._transport_id = None
        for closable in (reader, sock):
            try:
                if closable is not None:
                    closable.close()
            except OSError:
                pass

    def __enter__(self) -> "CastClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _next_request_id(self) -> int:
        with self._request_lock:
            self._request_id += 1
            return self._request_id

    def _send(self, namespace: str, destination: str, payload: dict[str, Any]) -> None:
        sock = self._sock
        if sock is None:
            raise CastConnectionError("not connected")
        message = p.encode_message(self.source_id, destination, namespace, payload)
        try:
            with self._send_lock:
                sock.sendall(p.frame(message))
        except OSError as exc:
            self._closed = True
            raise CastConnectionError(f"send failed: {exc}") from exc

    def _read_frame(self, timeout: Optional[float] = None) -> dict[str, Any]:
        if self._reader is None:
            raise CastConnectionError("not connected")
        old_timeout = self._sock.gettimeout() if self._sock else None
        try:
            if self._sock is not None and timeout is not None:
                self._sock.settimeout(timeout)
            return p.read_frame(self._reader)
        except socket.timeout as exc:
            # A quiet device is normal: the media namespace only pushes on state
            # changes. Keep the socket; let the caller decide whether to retry.
            raise CastTimeout(f"read timed out after {timeout}s") from exc
        except (OSError, p.CastProtocolError) as exc:
            self._closed = True
            raise CastConnectionError(f"receive failed: {exc}") from exc
        finally:
            if self._sock is not None and old_timeout is not None:
                try:
                    self._sock.settimeout(old_timeout)
                except OSError:
                    pass

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(HEARTBEAT_INTERVAL):
            try:
                self._send(p.NS_HEARTBEAT, p.RECEIVER_ID, {"type": p.MSG_PING})
            except CastConnectionError:
                return

    def _await(
        self,
        accept: Callable[[dict[str, Any]], bool],
        *,
        timeout: Optional[float] = None,
        description: str = "reply",
    ) -> dict[str, Any]:
        """Read frames until ``accept(payload)`` is true, answering heartbeats."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CastCommandError(f"timed out waiting for {description}")
            try:
                frame = self._read_frame(timeout=max(0.5, remaining))
            except CastTimeout:
                continue  # quiet device: re-check the deadline
            namespace = frame.get("namespace", "")
            payload = frame.get("payload") or {}

            if namespace == p.NS_HEARTBEAT:
                if payload.get("type") == p.MSG_PING:
                    try:
                        self._send(p.NS_HEARTBEAT, p.RECEIVER_ID, {"type": p.MSG_PONG})
                    except CastConnectionError:
                        pass
                continue
            if namespace == p.NS_CONNECTION:
                if payload.get("type") == p.MSG_CLOSE:
                    self._closed = True
                    raise CastConnectionError("device closed the connection")
                continue
            if accept(payload):
                return payload
            if payload.get("type") in {"INVALID_REQUEST", "LOAD_FAILED", "LOAD_CANCELLED"}:
                raise CastCommandError(str(payload.get("reason") or payload.get("type")))
            logger.debug("ignoring %s frame while waiting for %s", payload.get("type"), description)

    # ── receiver / volume ────────────────────────────────────────────────────
    def get_status(self, timeout: Optional[float] = None) -> dict[str, Any]:
        """GET_STATUS on the receiver namespace: apps, volume, and device name.

        Returns the inner ``status`` object (``{volume, applications}``), which is
        what every caller wants; the raw envelope is kept on ``last_status``.
        """
        request_id = self._next_request_id()
        self._send(p.NS_RECEIVER, p.RECEIVER_ID, {"type": "GET_STATUS", "requestId": request_id})
        payload = self._await(
            lambda reply: reply.get("type") == "RECEIVER_STATUS", timeout=timeout,
            description="RECEIVER_STATUS",
        )
        self.last_status = payload
        inner = payload.get("status")
        return inner if isinstance(inner, dict) else {}

    @staticmethod
    def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
        volume = status.get("volume") or {}
        apps = status.get("applications") or []
        return {
            "volume": volume.get("level"),
            "muted": volume.get("muted"),
            "app": apps[0].get("displayName") if apps else None,
            "app_id": apps[0].get("appId") if apps else None,
            "transport_id": apps[0].get("transportId") if apps else None,
            "status_text": apps[0].get("statusText") if apps else None,
            "session_id": apps[0].get("sessionId") if apps else None,
            "namespaces": apps[0].get("namespaces") if apps else None,
        }

    def get_volume(self) -> float:
        info = self.summarize_status(self.get_status())
        level = info.get("volume")
        return float(level) if isinstance(level, (int, float)) else 1.0

    def set_volume(self, level: float) -> float:
        """Set device volume, waiting briefly for the receiver's acknowledgement."""
        level = max(0.0, min(1.0, float(level)))
        request_id = self._next_request_id()
        self._send(
            p.NS_RECEIVER,
            p.RECEIVER_ID,
            {"type": "SET_VOLUME", "volume": {"level": level}, "requestId": request_id},
        )
        try:
            self._await(
                lambda reply: reply.get("type") == "RECEIVER_STATUS", timeout=3.0,
                description="SET_VOLUME ack",
            )
        except CastCommandError:
            # Fire-and-forget is acceptable: the level is applied regardless.
            logger.debug("no SET_VOLUME ack for level %.2f", level)
        return level

    def ensure_media_receiver(self, timeout: Optional[float] = None) -> str:
        """Return the transport id of the Default Media Receiver, launching it if needed."""
        if self._transport_id:
            return self._transport_id
        info = self.summarize_status(self.get_status(timeout=timeout))
        if info.get("app_id") == p.DEFAULT_MEDIA_RECEIVER and info.get("transport_id"):
            self._transport_id = str(info["transport_id"])
            return self._transport_id

        request_id = self._next_request_id()
        self._send(
            p.NS_RECEIVER,
            p.RECEIVER_ID,
            {"type": "LAUNCH", "appId": p.DEFAULT_MEDIA_RECEIVER, "requestId": request_id},
        )
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            status = self.get_status(timeout=max(0.5, remaining))
            info = self.summarize_status(status)
            if info.get("app_id") == p.DEFAULT_MEDIA_RECEIVER and info.get("transport_id"):
                transport_id = str(info["transport_id"])
                self._transport_id = transport_id
                # Open a virtual channel to the app itself.
                self._send(p.NS_CONNECTION, transport_id, {"type": p.MSG_CONNECT})
                return transport_id
            if time.monotonic() > deadline:
                raise CastCommandError("Default Media Receiver did not start")
            time.sleep(0.25)

    # ── media ────────────────────────────────────────────────────────────────
    def play_url(
        self,
        url: str,
        *,
        content_type: str = "audio/mpeg",
        title: str = "Hermes",
        timeout: float = 12.0,
        autoplay: bool = True,
    ) -> dict[str, Any]:
        """LOAD an audio URL and return once playback has started."""
        transport_id = self.ensure_media_receiver(timeout=timeout)
        request_id = self._next_request_id()
        payload = {
            "type": "LOAD",
            "requestId": request_id,
            "autoplay": autoplay,
            "media": {
                "contentId": url,
                "streamType": "BUFFERED",
                "contentType": content_type,
                "metadata": {
                    "type": 0,
                    "metadataType": 0,
                    "title": title,
                    "artist": "Hermes",
                },
            },
        }
        self._send(p.NS_MEDIA, transport_id, payload)

        status = self._await(
            lambda reply: reply.get("type") in {"MEDIA_STATUS", "LOAD_FAILED"}
            and (reply.get("requestId") in (None, request_id) or reply.get("type") == "LOAD_FAILED"),
            timeout=timeout,
            description="MEDIA_STATUS",
        )
        if status.get("type") == "LOAD_FAILED":
            raise CastCommandError(str(status.get("reason") or "LOAD_FAILED"))
        items = status.get("status") or []
        first = items[0] if items else {}
        return {
            "media_session_id": first.get("mediaSessionId"),
            "player_state": first.get("playerState"),
            "idle_reason": first.get("idleReason"),
            "duration": first.get("media", {}).get("duration") if first.get("media") else None,
        }

    def get_media_status(self, timeout: float = 3.0) -> dict[str, Any]:
        """Explicit MEDIA GET_STATUS: the device only pushes on change, so poll."""
        transport_id = self._transport_id
        if not transport_id:
            return {}
        request_id = self._next_request_id()
        self._send(p.NS_MEDIA, transport_id, {"type": "GET_STATUS", "requestId": request_id})
        reply = self._await(
            lambda payload: payload.get("type") == "MEDIA_STATUS"
            and payload.get("requestId") in (None, request_id),
            timeout=timeout,
            description="MEDIA_STATUS",
        )
        items = reply.get("status") or []
        return items[0] if items else {}

    def stop_media(self, media_session_id: int | str, timeout: float = 5.0) -> None:
        transport_id = self._transport_id
        if not transport_id:
            return
        try:
            self._send(
                p.NS_MEDIA,
                transport_id,
                {"type": "STOP", "mediaSessionId": media_session_id,
                 "requestId": self._next_request_id()},
            )
        except CastConnectionError:
            raise
        except CastError:
            return

    def play_and_wait(
        self,
        url: str,
        *,
        content_type: str = "audio/mpeg",
        title: str = "Hermes",
        timeout: float = 12.0,
        max_wait: float = 180.0,
    ) -> dict[str, Any]:
        """Play a URL and block until the clip finishes (or ``max_wait`` elapses)."""
        started = self.play_url(url, content_type=content_type, title=title, timeout=timeout)
        if started.get("media_session_id") is None:
            return started
        deadline = time.monotonic() + max_wait
        saw_playing = started.get("player_state") == "PLAYING"
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                status = self.get_media_status(timeout=3.0)
            except CastError:
                break
            state = status.get("playerState")
            if state == "PLAYING":
                saw_playing = True
            elif state == "IDLE" and (saw_playing or status.get("idleReason") in {"FINISHED", "ERROR"}):
                return {**started, "finished": True, "idle_reason": status.get("idleReason")}
        return {**started, "finished": False}


def probe(host: str, port: int = 8009, timeout: float = 5.0) -> dict[str, Any]:
    """Connect, read the receiver status, disconnect. Raises CastError on failure."""
    client = CastClient(host, port, timeout=timeout, heartbeat=False)
    try:
        client.connect()
        status = client.get_status(timeout=timeout)
        info = CastClient.summarize_status(status)
        info["host"] = host
        info["port"] = port
        return info
    finally:
        client.close()
