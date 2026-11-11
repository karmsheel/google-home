"""Out-of-process delivery for the speaker: cron runs and CLI sends.

The platform adapter can only speak while the gateway is running. Hermes also
delivers cron output from short-lived processes — `hermes cron run`, a restart-
safe worker, or an external scheduler — where there is no live adapter. For
those the platform entry registers ``standalone_sender_fn``; this module is it.

Preferred path: ask the running bridge to speak through its loopback control
API. That reuses the adapter's Cast connection and the media server the speaker
already knows how to reach. If no bridge answers, fall back to synthesizing and
casting inline (the same path the tools use when the gateway is down).
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

from . import config as ghconfig
from . import cronlink, speech

logger = logging.getLogger(__name__)

_BRIDGE_TIMEOUT_SECONDS = 180.0


def _looks_like_cron(message: str) -> bool:
    return (message or "").lstrip().startswith("Cronjob Response:")


def _spoken_text(message: str, settings: ghconfig.Settings) -> str:
    """What this delivery should sound like, honouring the mute settings."""
    if getattr(settings, "muted", False):
        return ""
    text = message or ""
    if _looks_like_cron(text):
        if not settings.speak_cron:
            return ""
        return speech.cron_digest(text, settings.cron_max_chars).strip()
    if not settings.speak_replies:
        return ""
    return speech.flatten_for_speech(text, settings.max_chars).strip()


def _bridge_endpoints() -> list[tuple[int, str]]:
    """(port, token) of every running bridge discoverable from this profile."""
    endpoints: list[tuple[int, str]] = []
    seen: set[int] = set()
    for home in cronlink.candidate_homes():
        path = home / "googlehome" / "control.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        port = int(data.get("port") or 0)
        if port and port not in seen:
            seen.add(port)
            endpoints.append((port, str(data.get("token") or "")))
    return endpoints


def _speak_via_bridge(text: str) -> Optional[dict[str, Any]]:
    """Ask a live bridge to say ``text``; None when no bridge is reachable."""
    endpoints = _bridge_endpoints()
    if not endpoints:
        return None
    last_error: Optional[Exception] = None
    for port, token in endpoints:
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/speak",
            data=json.dumps({"text": text}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"X-Hermes-Token": token} if token else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_BRIDGE_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
            continue
        if not payload.get("ok"):
            return {"error": str(payload.get("error") or "bridge refused to speak")}
        return {"success": True, "via": "bridge"}
    logger.info("googlehome standalone: no bridge answered (%s); speaking directly", last_error)
    return None


def _direct_speak(text: str, settings: ghconfig.Settings) -> dict[str, Any]:
    """Synthesize and cast without a gateway (tools' direct path)."""
    try:
        files = speech.synthesize(
            text,
            provider=settings.tts_provider,
            backend=settings.speech_backend,
            voice=settings.speech_voice,
        )
    except speech.TtsUnavailable as exc:
        return {"error": str(exc)}
    except RuntimeError as exc:
        return {"error": str(exc)}

    from .tools import _direct_play

    result = _direct_play(files, settings)
    if not result.get("ok"):
        return {"error": str(result.get("error") or "could not cast the audio")}
    return {"success": True, "via": "direct", "device": result.get("device", "")}


async def send_standalone(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Any = None,
    force_document: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """PlatformEntry.standalone_sender_fn: deliver one cron/CLI message.

    Returns ``{"success": True, ...}`` or ``{"error": "..."}`` as Hermes'
    standalone lane expects. Attachments are ignored: the speaker has no screen.
    """
    settings = ghconfig.resolve_settings(pconfig)
    text = _spoken_text(message, settings)
    if not text:
        # Muted by configuration: a successful no-op keeps the job green.
        return {
            "success": True,
            "platform": "googlehome",
            "chat_id": str(chat_id or ""),
            "message_id": f"gh-muted-{uuid.uuid4().hex[:8]}",
        }
    if media_files:
        logger.debug("googlehome standalone: ignoring %d attachment(s)", len(media_files))

    result = await asyncio.to_thread(_speak_via_bridge, text)
    if result is None:
        result = await asyncio.to_thread(_direct_speak, text, settings)
    if result.get("error"):
        return {"error": str(result["error"])}
    return {
        "success": True,
        "platform": "googlehome",
        "chat_id": str(chat_id or ""),
        "message_id": f"gh-{uuid.uuid4().hex[:10]}",
        "via": result.get("via", ""),
    }
