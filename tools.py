"""Agent tools: let a profile use the speaker as a voice, not just a chat surface.

These work whether or not the gateway adapter is running. With the adapter up
they reuse its live Cast session; without it they open a short-lived one, so
``hermes`` from a terminal can still make the speaker talk.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import castdevice, discovery, speech
from . import config as ghconfig
from .adapter import active_adapter
from .audioserver import MediaServer, content_type_for

logger = logging.getLogger(__name__)

TOOLSET = "googlehome"
_EMOJI = "🔊"


# ── shared helpers ───────────────────────────────────────────────────────────
def _settings() -> ghconfig.Settings:
    return ghconfig.resolve_settings(None)


def _direct_play(files: list[str], settings: ghconfig.Settings) -> dict[str, Any]:
    """Speak without a running adapter: one connection, one temp media server."""
    if not files:
        return {"ok": False, "error": "nothing to speak"}
    resolved = discovery.resolve_device(settings.device, timeout=3.0)
    host = resolved.get("host") or resolved.get("address")
    if not host:
        return {
            "ok": False,
            "error": (
                "No Cast device found on this network. Power the speaker on, or set "
                "its IP in the plugin settings (device)."
            ),
        }
    port = int(resolved.get("port") or 8009)
    local_ip = discovery.local_ip_for(host)
    media = MediaServer(ghconfig.media_dir(), port=0, advertise_host=local_ip)
    client = castdevice.CastClient(host, port, timeout=8.0)
    try:
        media.start()
        client.connect()
        volume_before: Optional[float] = None
        if settings.volume:
            try:
                volume_before = client.get_volume()
                client.set_volume(settings.volume / 100.0)
            except castdevice.CastError:
                volume_before = None
        try:
            for path in files:
                filename = media.publish(path)
                client.play_and_wait(
                    media.url_for(filename, local_ip),
                    content_type=content_type_for(path),
                    timeout=15.0,
                    max_wait=max(20.0, min(600.0, _size(path) / 8000.0)),
                )
        finally:
            if volume_before is not None and settings.restore_volume:
                try:
                    client.set_volume(volume_before)
                except castdevice.CastError:
                    pass
        return {"ok": True, "device": resolved.get("name") or host, "files": len(files)}
    except castdevice.CastError as exc:
        return {"ok": False, "error": str(exc), "device": resolved.get("name") or host}
    finally:
        client.close()
        media.stop()


def _size(path: str) -> float:
    from pathlib import Path

    try:
        return float(Path(path).stat().st_size)
    except OSError:
        return 0.0


def _speak(text: str, *, settings: Optional[ghconfig.Settings] = None) -> dict[str, Any]:
    settings = settings or _settings()
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    spoken = speech.flatten_for_speech(text, settings.max_chars)
    try:
        files = speech.synthesize(
            spoken,
            provider=settings.tts_provider,
            backend=settings.speech_backend,
            voice=settings.speech_voice,
        )
    except speech.TtsUnavailable as exc:
        return {"ok": False, "error": str(exc), "kind": "tts_unavailable"}
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc), "kind": "tts_failed"}

    adapter = active_adapter()
    if adapter is not None:
        try:
            adapter._play_blocking(files)
            return {"ok": True, "device": adapter._device.get("name", ""), "via": "gateway"}
        except castdevice.CastError as exc:
            return {"ok": False, "error": str(exc), "via": "gateway"}
    result = _direct_play(files, settings)
    result["via"] = "direct"
    return result


def _status(settings: ghconfig.Settings) -> dict[str, Any]:
    adapter = active_adapter()
    if adapter is not None:
        return {
            "ok": True,
            "via": "gateway",
            "profile": adapter.settings.profile or "(gateway default)",
            "device": adapter._device,
            "volume": adapter._device.get("volume"),
            "speaking": adapter.speaking,
            "last_error": adapter.last_error,
            "media": adapter._media.stats,
        }
    resolved = discovery.resolve_device(settings.device, timeout=3.0)
    host = resolved.get("host") or resolved.get("address")
    if not host:
        return {"ok": False, "via": "direct", "error": "no Cast device discovered",
                "configured_device": settings.device}
    try:
        probed = castdevice.probe(host, int(resolved.get("port") or 8009), timeout=4.0)
    except castdevice.CastError as exc:
        return {"ok": False, "via": "direct", "error": str(exc), "device": resolved}
    return {"ok": True, "via": "direct", "profile": settings.profile or "(unbound)",
            "device": {**resolved, "reachable": True}, "volume": probed.get("volume")}


# ── handlers ─────────────────────────────────────────────────────────────────
def _handle_speak(args: dict[str, Any], **_kwargs: Any) -> str:
    return json.dumps(_speak(str(args.get("text") or "")))


def _handle_status(args: dict[str, Any], **_kwargs: Any) -> str:
    return json.dumps(_status(_settings()))


def _handle_volume(args: dict[str, Any], **_kwargs: Any) -> str:
    try:
        level = int(args.get("level"))
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "level must be an integer 0-100"})
    level = max(0, min(100, level))
    settings = _settings()
    adapter = active_adapter()
    try:
        if adapter is not None:
            adapter._apply_volume(level)
            adapter.settings.volume = level
            return json.dumps({"ok": True, "volume": level, "via": "gateway"})
        resolved = discovery.resolve_device(settings.device, timeout=3.0)
        host = resolved.get("host") or resolved.get("address")
        if not host:
            return json.dumps({"ok": False, "error": "no Cast device discovered"})
        client = castdevice.CastClient(host, int(resolved.get("port") or 8009), timeout=8.0)
        try:
            client.connect()
            client.set_volume(level / 100.0)
        finally:
            client.close()
        return json.dumps({"ok": True, "volume": level, "via": "direct"})
    except castdevice.CastError as exc:
        return json.dumps({"ok": False, "error": str(exc)})


def _handle_devices(args: dict[str, Any], **_kwargs: Any) -> str:
    timeout = float(args.get("timeout") or 3.0)
    try:
        devices = discovery.discover(timeout=max(1.0, min(10.0, timeout)))
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)})
    rows = [
        {
            "name": device.get("name", ""),
            "model": device.get("model", ""),
            "address": device.get("address", ""),
            "host": device.get("host", ""),
            "port": device.get("port", 8009),
            "audio_only": bool(device.get("audio_only")),
            "id": device.get("id", ""),
        }
        for device in devices
    ]
    return json.dumps({"ok": True, "count": len(rows), "devices": rows})


_SPEAK_SCHEMA = {
    "name": "googlehome_speak",
    "description": (
        "Say something out loud on the connected Google Home / Nest speaker. "
        "Use it to notify the room, read a result aloud, or answer a voice "
        "question when the reply did not come through the speaker automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "What to say. Plain prose; it is synthesized and cast.",
            }
        },
        "required": ["text"],
    },
}

_STATUS_SCHEMA = {
    "name": "googlehome_status",
    "description": (
        "Report the smart-speaker bridge state: which device is bound, whether it "
        "is reachable, its volume, the bound Hermes profile, and the last error."
    ),
    "parameters": {"type": "object", "properties": {}},
}

_VOLUME_SCHEMA = {
    "name": "googlehome_volume",
    "description": "Set the smart speaker's global volume (0-100).",
    "parameters": {
        "type": "object",
        "properties": {
            "level": {"type": "integer", "minimum": 0, "maximum": 100,
                      "description": "Device volume percentage."}
        },
        "required": ["level"],
    },
}

_DEVICES_SCHEMA = {
    "name": "googlehome_devices",
    "description": (
        "List Google Cast devices visible on the local network (name, model, IP). "
        "Use it to find the speaker's address before binding it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "timeout": {"type": "number", "description": "Discovery window in seconds (1-10)."}
        },
    },
}


def register_tools(ctx: Any) -> None:
    """Register the googlehome toolset with the Hermes tool registry."""
    ctx.register_tool(
        name=_SPEAK_SCHEMA["name"],
        toolset=TOOLSET,
        schema=_SPEAK_SCHEMA,
        handler=_handle_speak,
        description="Speak text through the Google Home speaker.",
        emoji=_EMOJI,
    )
    ctx.register_tool(
        name=_STATUS_SCHEMA["name"],
        toolset=TOOLSET,
        schema=_STATUS_SCHEMA,
        handler=_handle_status,
        description="Report smart-speaker bridge status.",
        emoji=_EMOJI,
    )
    ctx.register_tool(
        name=_VOLUME_SCHEMA["name"],
        toolset=TOOLSET,
        schema=_VOLUME_SCHEMA,
        handler=_handle_volume,
        description="Set the smart speaker volume.",
        emoji=_EMOJI,
    )
    ctx.register_tool(
        name=_DEVICES_SCHEMA["name"],
        toolset=TOOLSET,
        schema=_DEVICES_SCHEMA,
        handler=_handle_devices,
        description="Discover Google Cast devices on the LAN.",
        emoji=_EMOJI,
    )
