"""Per-conversation spoken output, chosen from the composer.

The platform adapter speaks turns that arrive *through the googlehome platform*.
This module covers the other surface: a conversation in Hermes Desktop (or the
CLI) whose Speaker control was pointed at a device. A conversation with no
choice is silent, so the two paths never double up.

State is a small JSON file under the profile's ``googlehome`` directory, because
the Desktop extension (renderer) and the hook (gateway) are different processes.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

from . import config as ghconfig

logger = logging.getLogger(__name__)

#: What a conversation can be pointed at. Audio on "this computer" is not a
#: destination this plugin offers: it has no local playback path, only cast.
MODES = {"off", "speaker"}

STALE_MS = 12 * 60 * 60 * 1000
MAX_ENTRIES = 200

#: A turn that already spoke through a tool is not spoken again.
SPEAKING_TOOLS = {"googlehome_speak"}

#: Platforms whose own adapter speaks the reply (cron has cronlink).
PLATFORMS_THAT_SPEAK = {"googlehome", "cron"}

_MARK_TTL_S = 600.0
_MAX_MARKS = 500

_speak_lock = threading.Lock()
_marks_lock = threading.Lock()
_spoken_turns: Dict[str, float] = {}


# ── storage ──────────────────────────────────────────────────────────────────
def _path() -> Path:
    return ghconfig.state_dir() / "destinations.json"


def read_all() -> Dict[str, Dict[str, Any]]:
    try:
        raw = _path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): value for key, value in payload.items() if isinstance(value, dict)}


def _fresh(entry: Dict[str, Any]) -> bool:
    try:
        at = int(entry.get("at") or 0)
    except (TypeError, ValueError):
        return False
    return bool(at) and (time.time() * 1000) - at <= STALE_MS


def destination_for(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """This conversation's destination, or ``None`` when none was chosen."""
    key = str(session_id or "").strip()
    if not key:
        return None
    entry = read_all().get(key)
    if not entry or not _fresh(entry):
        return None
    mode = str(entry.get("mode") or "").strip().lower()
    if mode not in MODES:
        return None
    return {
        "mode": mode,
        "device": str(entry.get("device") or "").strip(),
        "name": str(entry.get("name") or "").strip(),
        "at": int(entry.get("at") or 0),
    }


def set_destination(
    session_id: str,
    mode: str,
    *,
    device: str = "",
    name: str = "",
) -> Dict[str, Any]:
    """Record where one conversation's replies go. Raises ``ValueError``."""
    key = str(session_id or "").strip()
    if not key:
        raise ValueError("a session id is required")
    clean = str(mode or "").strip().lower()
    if clean not in MODES:
        raise ValueError("mode must be 'off' or 'speaker'")

    entries = {k: v for k, v in read_all().items() if _fresh(v)}
    entries[key] = {
        "mode": clean,
        "device": str(device or "").strip(),
        "name": str(name or "").strip(),
        "at": int(time.time() * 1000),
    }
    if len(entries) > MAX_ENTRIES:
        newest = sorted(entries.items(), key=lambda item: int(item[1].get("at") or 0), reverse=True)
        entries = dict(newest[:MAX_ENTRIES])
    _write(entries)
    return entries[key]


def clear_destination(session_id: str) -> None:
    key = str(session_id or "").strip()
    if not key:
        return
    entries = read_all()
    if key in entries:
        entries.pop(key, None)
        _write(entries)


def _write(entries: Dict[str, Dict[str, Any]]) -> None:
    """Atomic replace: the hook may read while the renderer writes."""
    target = _path()
    temporary = target.with_suffix(f".{id(entries)}.tmp")
    try:
        temporary.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        logger.debug("[googlehome] could not store destinations: %s", exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# ── turn marks ───────────────────────────────────────────────────────────────
def _mark_key(session_id: Any, turn_id: Any) -> str:
    turn = str(turn_id or "").strip()
    return turn or str(session_id or "").strip()


def _mark(session_id: Any, turn_id: Any) -> None:
    key = _mark_key(session_id, turn_id)
    if not key:
        return
    now = time.time()
    with _marks_lock:
        _spoken_turns[key] = now
        if len(_spoken_turns) > _MAX_MARKS:
            for stale in [name for name, at in _spoken_turns.items() if now - at > _MARK_TTL_S]:
                _spoken_turns.pop(stale, None)


def _already(session_id: Any, turn_id: Any) -> bool:
    key = _mark_key(session_id, turn_id)
    if not key:
        return False
    with _marks_lock:
        at = _spoken_turns.get(key)
        if at is None:
            return False
        if time.time() - at > _MARK_TTL_S:
            _spoken_turns.pop(key, None)
            return False
        return True


# ── speaking ─────────────────────────────────────────────────────────────────
def speak(text: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """Synthesize and cast one reply to the conversation's speaker."""
    # Imported here: ``tools`` reaches the gateway adapter, and this module is
    # imported by registration in every process (and by the tests) without core.
    from . import tools

    settings = replace(ghconfig.resolve_settings(None), device=str(entry.get("device") or ""))
    with _speak_lock:
        result = tools.speak_to(text, settings)
    if result.get("ok") is False:
        logger.warning("[googlehome] %s", result.get("error") or "could not speak the reply")
    return result


def _speak_in_background(text: str, entry: Dict[str, Any]) -> None:
    def run() -> None:
        try:
            speak(text, entry)
        except Exception as exc:  # noqa: BLE001 - a worker must not die loudly
            logger.debug("[googlehome] background speak failed: %s", exc)

    threading.Thread(target=run, name="googlehome-speak", daemon=True).start()


# ── hooks ────────────────────────────────────────────────────────────────────
def on_post_tool_call(
    tool_name: Any = None,
    session_id: Any = None,
    turn_id: Any = None,
    **_kwargs: Any,
) -> None:
    """Remember that this turn already spoke through a tool."""
    if str(tool_name or "").strip() in SPEAKING_TOOLS:
        _mark(session_id, turn_id)


def on_post_llm_call(
    assistant_response: Any = None,
    session_id: Any = None,
    platform: Any = None,
    turn_id: Any = None,
    **_kwargs: Any,
) -> None:
    """Speak the final answer of a completed turn, when this chat has a speaker."""
    try:
        text = str(assistant_response or "").strip()
        if not text:
            return
        if str(platform or "").strip().lower() in PLATFORMS_THAT_SPEAK:
            return
        entry = destination_for(session_id)
        if not entry or entry["mode"] != "speaker":
            return
        if _already(session_id, turn_id):
            return
        _mark(session_id, turn_id)
        _speak_in_background(text, entry)
    except Exception:  # noqa: BLE001 - a hook must never raise into the turn
        logger.debug("[googlehome] post_llm_call skipped", exc_info=True)


def register_hooks(ctx: Any) -> None:
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
