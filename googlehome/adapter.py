"""The Hermes gateway platform adapter for a Google Home speaker.

Responsibilities
----------------
* **Outbound** — every reply for a turn on this platform is synthesized with
  Hermes's own TTS stack and cast to the speaker. ``play_tts`` is overridden so
  auto-TTS speaks instead of pretending to send a voice message.
* **Inbound** — ``submit_utterance`` converts a transcript into a normal
  ``MessageEvent`` so the turn appears in Hermes exactly like any other chat,
  with tools, approvals, cron and memory intact.
* **Profile binding** — the configured profile is stamped on the inbound
  ``SessionSource`` so a multiplexed gateway routes the turn to that profile's
  ``HERMES_HOME``.
* **Local control** — a loopback HTTP server (``control.py``) gives Hermes
  Desktop a push-to-talk and device-management channel, and writes a small
  ``control.json`` discovery file for the Desktop plugin.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType

from . import PLATFORM_NAME, __version__
from . import config as ghconfig
from . import castdevice, discovery, playback, speech
from .audioserver import MediaServer, content_type_for

logger = logging.getLogger(__name__)

#: The live adapter, so tools and the Desktop channel can find the running
#: Cast session. Tools run inside the gateway process, which is the same
#: process the adapter connects in.
_ACTIVE: Optional["GoogleHomeAdapter"] = None


def register_instance(adapter: "GoogleHomeAdapter") -> None:
    global _ACTIVE
    _ACTIVE = adapter


def unregister_instance(adapter: "GoogleHomeAdapter") -> None:
    global _ACTIVE
    if _ACTIVE is adapter:
        _ACTIVE = None


def active_adapter() -> Optional["GoogleHomeAdapter"]:
    return _ACTIVE


_AUDIO_EXTENSIONS = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
}


def check_requirements() -> bool:
    """Passive dependency probe: the plugin is stdlib-only, so always ready."""
    return True


def validate_config(config: PlatformConfig) -> bool:
    """No credential is required; an unreachable device is a runtime problem."""
    return True


class GoogleHomeAdapter(BasePlatformAdapter):
    """Speaks through a Cast device and listens through the host microphone."""

    supports_code_blocks = False
    supports_status_text = False
    splits_long_messages = False
    MAX_MESSAGE_LENGTH = 0

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform(PLATFORM_NAME))
        self.settings = ghconfig.resolve_settings(config)
        self._media = MediaServer(ghconfig.media_dir(), port=self.settings.media_port)
        self._control = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._client: Optional[castdevice.CastClient] = None
        self._speak_lock = threading.Lock()
        self._device: dict[str, Any] = {}
        self._started = False
        self.started_at = 0.0
        self.last_error = ""
        self.last_spoken = ""
        self.last_transcript = ""
        self.speaking = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def connect(self, *, is_reconnect: bool = False) -> bool:
        self._loop = asyncio.get_running_loop()
        if self.settings.proxy:
            # A second multiplex profile bound to the same speaker. Do not open
            # another media server, control server or Cast connection: forward
            # every utterance to the primary bridge's loopback API instead.
            self._started = True
            self.started_at = time.time()
            self._ensure_home_channel()
            self._mark_connected()
            logger.info("[%s] proxy mode: forwarding speech to the primary bridge", PLATFORM_NAME)
            return True
        try:
            self._media.start()
        except OSError as exc:
            self.last_error = f"cannot start the media server: {exc}"
            logger.error("[%s] %s", PLATFORM_NAME, self.last_error)
            return False

        self._started = True
        self.started_at = time.time()
        self._write_control_file()

        reachable = await self._refresh_device()
        if not reachable:
            logger.warning("[%s] speaker not reachable yet: %s", PLATFORM_NAME, self.last_error)

        try:
            from .control import ControlServer

            token = ghconfig.ensure_token() if self.settings.token_required else ""
            self._control = ControlServer(self, port=self.settings.control_port, token=token)
            self._control.start()
            self._write_control_file()
        except OSError as exc:
            logger.warning("[%s] control server unavailable on %s: %s",
                           PLATFORM_NAME, self.settings.control_port, exc)
        except Exception as exc:  # noqa: BLE001 - never fail connect for the UI channel
            logger.warning("[%s] control server failed: %s", PLATFORM_NAME, exc)

        self._mark_connected()
        register_instance(self)
        self._ensure_home_channel()
        logger.info(
            "[%s] ready: device=%s profile=%s media_port=%s",
            PLATFORM_NAME,
            self._device.get("address") or self.settings.device or "(none yet)",
            self.settings.profile or "(gateway default)",
            self._media.port,
        )
        return True

    def _ensure_home_channel(self) -> None:
        """Persist a home channel so bare ``deliver: googlehome`` resolves.

        Cron's bare-platform token resolves to the platform's home channel. The
        bridge has exactly one conversation, so this is the chat id the adapter
        already uses; setting it once is what makes the obvious cron target
        work without hand-written ``googlehome:speaker-...`` tokens.
        """
        try:
            from gateway.config import (
                HomeChannel, Platform, load_gateway_config, persist_home_channel,
            )

            platform = Platform(PLATFORM_NAME)
            existing = load_gateway_config().get_home_channel(platform)
            if existing is not None and existing.chat_id:
                return
            chat_id = self.settings.chat_id
            if self.settings.proxy:
                from .cronlink import live_chat_id

                chat_id = live_chat_id() or chat_id
            persist_home_channel(HomeChannel(
                platform=platform,
                chat_id=chat_id,
                name=str(self._device.get("name") or "Google Home"),
            ))
            logger.info("[%s] home channel set to %s", PLATFORM_NAME, chat_id)
        except Exception as exc:  # noqa: BLE001 - a home channel is best effort
            logger.debug("[%s] could not set a home channel: %s", PLATFORM_NAME, exc)

    async def disconnect(self) -> None:
        self._started = False
        unregister_instance(self)
        # Do not leave a reply talking to an empty room after the bridge is
        # gone; the stop hook sends one last STOP before the socket closes.
        playback.stop()
        playback.reset()
        if self._control is not None:
            try:
                self._control.stop()
            except Exception:  # noqa: BLE001
                pass
            self._control = None
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._media.stop()
        self._clear_control_file()
        self._mark_disconnected()

    # ── device ───────────────────────────────────────────────────────────────
    def _resolve_blocking(self) -> dict[str, Any]:
        try:
            resolved = discovery.resolve_device(self.settings.device, timeout=3.0, enrich=True)
        except Exception as exc:  # noqa: BLE001 - discovery is best effort
            self.last_error = f"device discovery failed: {exc}"
            return {}
        if not resolved:
            self.last_error = (
                "no Cast device answered. Check the speaker is powered on and on the "
                "same network, or set its IP in the plugin settings."
            )
            return {}
        host = discovery.dial_host(resolved)
        port = int(resolved.get("port") or 8009)
        resolved["host"] = host
        resolved["port"] = port
        try:
            probed = castdevice.probe(host, port, timeout=4.0)
            resolved["reachable"] = True
            resolved["volume"] = probed.get("volume")
            resolved["active_app"] = probed.get("app")
            self.last_error = ""
        except castdevice.CastError as exc:
            resolved["reachable"] = False
            self.last_error = f"device found at {host} but not answering: {exc}"
        return resolved

    async def _refresh_device(self) -> bool:
        info = await asyncio.to_thread(self._resolve_blocking)
        if info:
            self._device = {**self._device, **info}
        return bool(self._device.get("reachable"))

    async def refresh_device(self) -> dict[str, Any]:
        await self._refresh_device()
        self._write_control_file()
        return await self.status()

    def _client_blocking(self) -> castdevice.CastClient:
        if self._client is not None and self._client.connected:
            return self._client
        if not self._device.get("host"):
            info = self._resolve_blocking()
            if not info.get("host"):
                raise castdevice.CastConnectionError(self.last_error or "speaker not found")
            self._device = {**self._device, **info}
        host = str(self._device["host"])
        port = int(self._device.get("port") or 8009)
        client = castdevice.CastClient(host, port, timeout=8.0)
        client.connect()
        logger.debug("[%s] cast connection open to %s:%s", PLATFORM_NAME, host, port)
        self._client = client
        return client

    def _drop_client(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    @staticmethod
    def _max_wait_for(paths: list[str]) -> float:
        """Generous playback cap derived from file size (assumes >= 64 kbps)."""
        total = 0
        for path in paths:
            try:
                total += Path(path).stat().st_size
            except OSError:
                continue
        return max(20.0, min(600.0, total / 8000.0))

    def _play_blocking(self, paths: list[str]) -> None:
        if not paths:
            return
        with self._speak_lock:
            self.speaking = True
            try:
                self._play_once(paths)
            finally:
                self.speaking = False

    def _play_once(self, paths: list[str]) -> None:
        client = self._client_blocking()
        host = discovery.local_ip_for(str(self._device.get("host", ""))) or self._media.advertise_host
        if not host:
            raise castdevice.CastError(
                "cannot determine a LAN address for this machine to serve audio from"
            )
        self._media.advertise_host = host

        queued: list[tuple[str, str]] = []
        for path in paths:
            filename = self._media.publish(path)
            queued.append((self._media.url_for(filename, host), content_type_for(path)))

        interrupted = threading.Event()
        device_name = str(self._device.get("name") or self._device.get("host") or "")

        def request_stop() -> bool:
            interrupted.set()
            return client.stop_current()

        volume_before: Optional[float] = None
        if self.settings.volume:
            try:
                volume_before = client.get_volume()
                client.set_volume(self.settings.volume / 100.0)
            except castdevice.CastError:
                volume_before = None
        try:
            max_wait = self._max_wait_for(paths)
            for url, mime in queued:
                if interrupted.is_set():
                    break
                started = client.play_url(
                    url,
                    content_type=mime,
                    title="Hermes",
                    timeout=15.0,
                )
                handle = playback.begin(request_stop, source="reply", device=device_name)
                try:
                    result = client.wait_until_done(started, max_wait=max_wait)
                finally:
                    playback.end(handle)
                if interrupted.is_set():
                    logger.info("[%s] playback stopped on request", PLATFORM_NAME)
                    break
                if not result.get("finished"):
                    logger.warning("[%s] playback did not report an idle state for %s",
                                   PLATFORM_NAME, url)
        finally:
            if volume_before is not None and self.settings.restore_volume:
                try:
                    client.set_volume(volume_before)
                except castdevice.CastError:
                    pass
            self._media.prune()

    async def _play(self, paths: list[str]) -> None:
        """Cast files, reconnecting once if the session had gone stale."""
        try:
            await asyncio.to_thread(self._play_blocking, paths)
        except castdevice.CastConnectionError:
            logger.info("[%s] cast session dropped; reconnecting once", PLATFORM_NAME)
            self._drop_client()
            await self._refresh_device()
            await asyncio.to_thread(self._play_blocking, paths)

    # ── profile secret scope ─────────────────────────────────────────────────
    def _profile_home(self) -> Path:
        """The HERMES_HOME whose credentials this adapter's speech calls must use."""
        name = (self.settings.profile or "").strip()
        if name:
            try:
                from hermes_cli.profiles import get_profile_dir, profile_exists

                if profile_exists(name):
                    return Path(get_profile_dir(name))
                logger.warning("[%s] profile %r does not exist; using the active home",
                               PLATFORM_NAME, name)
            except Exception as exc:  # noqa: BLE001 - profile helpers are best effort
                logger.debug("[%s] profile lookup failed: %s", PLATFORM_NAME, exc)
        try:
            from hermes_constants import get_hermes_home

            return Path(get_hermes_home())
        except Exception:  # noqa: BLE001
            return ghconfig.hermes_home()

    @contextlib.contextmanager
    def _profile_scope(self):
        """Install this adapter's profile scope around a credential-using call.

        A multiplexing gateway refuses secret reads from unscoped tasks
        (``agent.secret_scope.UnscopedSecretError``: "Hermes could not read this
        profile's XAI_BASE_URL …") rather than borrowing another profile's
        credentials. Turns run inside ``_profile_runtime_scope``, but the control
        API, cron links and ambient speech call in from outside any turn, so the
        adapter installs its own scope — the per-adapter scope that error names.
        """
        home = self._profile_home()
        tokens: list[tuple[str, Any]] = []
        try:
            try:
                from hermes_constants import set_hermes_home_override

                tokens.append(("home", set_hermes_home_override(str(home))))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] no HERMES_HOME override: %s", PLATFORM_NAME, exc)
            try:
                from agent.secret_scope import build_profile_secret_scope, set_secret_scope

                tokens.append(("secret", set_secret_scope(
                    build_profile_secret_scope(home), profile_home=str(home))))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[%s] no secret scope: %s", PLATFORM_NAME, exc)
            yield
        finally:
            while tokens:
                kind, token = tokens.pop()
                try:
                    if kind == "secret":
                        from agent.secret_scope import reset_secret_scope

                        reset_secret_scope(token)
                    else:
                        from hermes_constants import reset_hermes_home_override

                        reset_hermes_home_override(token)
                except Exception:  # noqa: BLE001 - unwinding must never raise
                    pass

    def _synthesize(self, text: str) -> list[str]:
        """Synthesize on a worker thread, inside the profile scope."""
        with self._profile_scope():
            return speech.synthesize(
                text,
                provider=self.settings.tts_provider,
                backend=self.settings.speech_backend,
                voice=self.settings.speech_voice,
            )

    # ── outbound ─────────────────────────────────────────────────────────────
    def _spoken_text(self, content: str, *, cron: bool = False) -> str:
        text = content or ""
        if cron:
            # A cron delivery is a wrapped report. Speak the job name plus a
            # short digest (or its explicit SPEAK: line), never the scaffolding.
            return speech.cron_digest(text, self.settings.cron_max_chars).strip()
        prepare = getattr(self, "prepare_tts_text", None)
        if callable(prepare):
            try:
                text = prepare(text)
            except Exception:  # noqa: BLE001 - fall back to our own flattener
                text = speech.flatten_for_speech(text, self.settings.max_chars)
        else:
            text = speech.flatten_for_speech(text, self.settings.max_chars)
        return (text or "").strip()[: self.settings.max_chars]

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        # Cron passes ``job_id`` in the metadata; interactive replies do not.
        is_cron = bool((metadata or {}).get("job_id"))
        if is_cron:
            if not self.settings.speak_cron:
                return SendResult(success=True, message_id=f"gh-cron-muted-{uuid.uuid4().hex[:8]}")
        elif not self.settings.speak_replies:
            return SendResult(success=True, message_id=f"gh-muted-{uuid.uuid4().hex[:8]}")

        text = self._spoken_text(content, cron=is_cron)
        if not text:
            return SendResult(success=True, message_id=f"gh-empty-{uuid.uuid4().hex[:8]}")
        if self.settings.proxy:
            return await self._proxy_send(text)

        try:
            files = await asyncio.to_thread(self._synthesize, text)
        except speech.TtsUnavailable as exc:
            self.last_error = str(exc)
            logger.warning("[%s] %s", PLATFORM_NAME, exc)
            return SendResult(success=False, error=str(exc), error_kind="tts_unavailable")
        except RuntimeError as exc:
            self.last_error = str(exc)
            logger.warning("[%s] %s", PLATFORM_NAME, exc)
            return SendResult(success=False, error=str(exc), retryable=True)

        self.last_spoken = text
        try:
            await self._play(files)
        except castdevice.CastError as exc:
            self.last_error = str(exc)
            logger.warning("[%s] could not play reply: %s", PLATFORM_NAME, exc)
            return SendResult(success=False, error=str(exc), retryable=True)

        self.last_error = ""
        return SendResult(success=True, message_id=f"gh-{uuid.uuid4().hex[:10]}")

    async def _proxy_send(self, text: str) -> SendResult:
        """Speak through the primary bridge's loopback API."""
        from .standalone import _speak_via_bridge

        result = await asyncio.to_thread(_speak_via_bridge, text)
        if result is None:
            self.last_error = "the primary bridge is not running"
            return SendResult(success=False, error=self.last_error, retryable=True)
        if result.get("error"):
            self.last_error = str(result["error"])
            return SendResult(success=False, error=self.last_error, retryable=True)
        self.last_error = ""
        self.last_spoken = text
        return SendResult(success=True, message_id=f"gh-proxy-{uuid.uuid4().hex[:8]}")

    async def play_tts(self, chat_id: str, audio_path: str, **kwargs: Any) -> SendResult:
        """Hermes already synthesized audio for this turn: just play it."""
        try:
            await self._play([str(audio_path)])
        except castdevice.CastError as exc:
            self.last_error = str(exc)
            return SendResult(success=False, error=str(exc), retryable=True)
        return SendResult(success=True, message_id=f"gh-{uuid.uuid4().hex[:10]}")

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        return await self.play_tts(chat_id, audio_path, **kwargs)

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        """No visual channel: typing has nowhere to appear."""
        return None

    async def stop_typing(self, chat_id: str) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {
            "name": self._device.get("name") or "Google Home",
            "type": "dm",
            "chat_id": chat_id,
        }

    # ── inbound ──────────────────────────────────────────────────────────────
    async def submit_utterance(self, text: str, *, origin: str = "text", audio_path: str = "") -> bool:
        """Treat ``text`` as something the user just said to the speaker."""
        text = (text or "").strip()
        if not text:
            return False
        if not self._message_handler and self._loop is None:
            logger.warning("[%s] cannot accept input before connect()", PLATFORM_NAME)
            return False

        self.last_transcript = text
        source = self.build_source(
            chat_id=self.settings.chat_id,
            chat_name=str(self._device.get("name") or "Google Home"),
            chat_type="dm",
            user_id="speaker",
            user_name="Speaker",
        )
        if self.settings.profile and not getattr(source, "profile", None):
            # Newer Hermes resolves the profile from ``gateway.profile_routes``
            # during build_source and keys the session's authority on it; a
            # second, plugin-side stamp would disagree with that scope and the
            # turn fails with RuntimeStoreError('profile_mismatch'). Only the
            # unrouted case needs the legacy stamp.
            source.profile = self.settings.profile

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"gh-{uuid.uuid4().hex[:10]}",
        )
        logger.info("[%s] turn from %s (profile=%s): %s",
                    PLATFORM_NAME, origin, self.settings.profile or "default", text[:120])
        await self.handle_message(event)
        return True

    async def submit_audio(self, data: bytes, mime: str = "audio/webm") -> dict[str, Any]:
        """Transcribe a recording with Hermes's STT stack, then run the turn."""
        if not data:
            return {"ok": False, "error": "empty recording"}
        normalized = (mime or "").split(";", 1)[0].strip().lower()
        extension = _AUDIO_EXTENSIONS.get(normalized, ".webm")
        inbox = ghconfig.state_dir() / "inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"utt-{uuid.uuid4().hex[:10]}{extension}"
        await asyncio.to_thread(path.write_bytes, data)

        try:
            result = await asyncio.to_thread(self._transcribe_blocking, str(path))
        except Exception as exc:  # noqa: BLE001 - STT surfaces many errors
            return {"ok": False, "error": f"transcription failed: {exc}"}

        transcript = str(result.get("transcript") or "").strip()
        if not result.get("success"):
            error = str(result.get("error") or "transcription failed")
            if "empty transcript" in error.lower():
                return {"ok": True, "transcript": "", "submitted": False}
            return {"ok": False, "error": error}
        if not transcript:
            return {"ok": True, "transcript": "", "submitted": False}

        submitted = await self.submit_utterance(transcript, origin="mic", audio_path=str(path))
        return {
            "ok": True,
            "transcript": transcript,
            "submitted": submitted,
            "provider": result.get("provider"),
        }

    @staticmethod
    def _transcribe_blocking(path: str) -> dict[str, Any]:
        from tools.voice_mode import transcribe_recording

        return transcribe_recording(path)

    # ── status / control helpers ─────────────────────────────────────────────
    async def status(self) -> dict[str, Any]:
        playing = playback.snapshot()
        payload: dict[str, Any] = {
            "ok": True,
            "platform": PLATFORM_NAME,
            "version": __version__,
            "started": self._started,
            "uptime_seconds": round(time.time() - self.started_at, 1) if self.started_at else 0,
            "connected": self._started,
            # ``speaking`` covers the instant between LOAD and the registry
            # entry; ``playback`` covers agent speech cast outside this adapter
            # (a conversation pointed at its own speaker).
            "speaking": bool(self.speaking or playing["speaking"]),
            "playback": playing,
            "profile": self.settings.profile or "",
            "chat_id": self.settings.chat_id,
            "device": {
                "name": self._device.get("name", ""),
                "model": self._device.get("model", ""),
                "label": (
                    discovery.display_label(self._device)
                    if self._device.get("host") or self._device.get("address")
                    else ""
                ),
                "address": self._device.get("address") or self._device.get("host", ""),
                "host": self._device.get("host", ""),
                "port": self._device.get("port", 8009),
                "reachable": bool(self._device.get("reachable")),
                "volume": self._device.get("volume"),
                "active_app": self._device.get("active_app"),
            },
            "settings": {
                "device": self.settings.device,
                "profile": self.settings.profile,
                "volume": self.settings.volume,
                "speak_replies": self.settings.speak_replies,
                "speak_cron": self.settings.speak_cron,
                "proxy": self.settings.proxy,
                "max_chars": self.settings.max_chars,
                "cron_max_chars": self.settings.cron_max_chars,
                "control_port": self.settings.control_port,
                "speech_backend": self.settings.speech_backend,
            },
            "speech": {
                "configured_backend": self.settings.speech_backend,
                "last_backend": speech.last_backend(),
                "voice": self.settings.speech_voice,
            },
            "media": self._media.stats,
            "last_error": self.last_error,
            "last_spoken": self.last_spoken[-400:],
            "last_transcript": self.last_transcript[-400:],
        }
        # Reading the live volume takes ``_speak_lock``, which the playback
        # thread holds for the whole clip. Doing it while speaking would make
        # every status poll wait for the speech to end — exactly the window in
        # which the Desktop needs to see "speaking" and offer its stop control.
        if self._device.get("host") and not payload["speaking"]:
            try:
                payload["device"]["volume"] = await asyncio.to_thread(self._live_volume)
            except castdevice.CastError:
                pass
        return payload

    def _live_volume(self) -> float:
        with self._speak_lock:
            return self._client_blocking().get_volume()

    async def speak_text(self, text: str) -> dict[str, Any]:
        """Speak arbitrary text without running an agent turn."""
        result = await self.send(self.settings.chat_id, text)
        return {"ok": result.success, "error": result.error}

    async def set_volume(self, level: int) -> dict[str, Any]:
        level = max(0, min(100, int(level)))
        self.settings.volume = level
        try:
            await asyncio.to_thread(self._apply_volume, level)
        except castdevice.CastError as exc:
            return {"ok": False, "error": str(exc), "volume": level}
        return {"ok": True, "volume": level}

    def _apply_volume(self, level: int) -> None:
        with self._speak_lock:
            self._client_blocking().set_volume(level / 100.0)

    async def set_device(self, spec: str) -> dict[str, Any]:
        self.settings.device = (spec or "").strip()
        self._drop_client()
        self._device = {}
        await self._refresh_device()
        self._persist("device", self.settings.device)
        self._write_control_file()
        return await self.status()

    async def set_profile(self, name: str) -> dict[str, Any]:
        self.settings.profile = (name or "").strip()
        self._persist("profile", self.settings.profile)
        self._write_control_file()
        return await self.status()

    @staticmethod
    def _persist(key: str, value: str) -> None:
        """Write a UI choice through the plugin settings writer (best effort)."""
        ctx = ghconfig.plugin_ctx()
        if ctx is None:
            logger.warning("[%s] no plugin context; %s=%r is session-only", PLATFORM_NAME, key, value)
            return
        try:
            ctx.set_config(key, value)
        except Exception as exc:  # noqa: BLE001 - persistence must not break the bridge
            logger.warning("[%s] could not persist %s=%r: %s", PLATFORM_NAME, key, value, exc)

    # ── Desktop discovery file ───────────────────────────────────────────────
    def control_info(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "platform": PLATFORM_NAME,
            "running": self._started,
            "port": self._control.port if self._control is not None else 0,
            "token": ghconfig.load_token() if self.settings.token_required else "",
            "device": self._device.get("name", ""),
            "address": self._device.get("host", ""),
            "profile": self.settings.profile,
            "chat_id": self.settings.chat_id,
            "pid": _pid(),
            "updated_at": time.time(),
        }

    def _write_control_file(self) -> None:
        try:
            path = ghconfig.state_dir() / "control.json"
            path.write_text(json.dumps(self.control_info(), indent=2), encoding="utf-8")
        except OSError as exc:
            logger.debug("[%s] cannot write control.json: %s", PLATFORM_NAME, exc)

    def _clear_control_file(self) -> None:
        try:
            (ghconfig.state_dir() / "control.json").unlink(missing_ok=True)
        except OSError:
            pass


def _pid() -> int:
    import os

    return os.getpid()
