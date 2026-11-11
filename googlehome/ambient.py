"""Per-conversation spoken output, chosen from the composer.

The platform adapter speaks turns that arrive *through the googlehome platform*.
This module covers the other surface: a conversation in Hermes Desktop (or the
CLI) whose Speaker control was pointed at a device. A conversation with no
choice is silent, so the two paths never double up.

State is a small JSON file under the profile's ``googlehome`` directory, because
the Desktop extension (renderer) and the hook (gateway) are different processes.
"""
from __future__ import annotations

import contextvars
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

#: A turn that already spoke through a tool is not spoken again. A notification
#: is the deliberate alternative to reading the reply out, so it counts too:
#: the room hears the one-line outcome, not the whole answer.
SPEAKING_TOOLS = {"googlehome_speak", "googlehome_notify"}

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
    _replace_json(_path(), entries)


def _replace_json(target: Path, payload: Any) -> None:
    """Write JSON atomically, so a reader never sees a half-written file."""
    temporary = target.with_suffix(f".{id(payload)}.tmp")
    try:
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(target)
    except OSError as exc:
        logger.debug("[googlehome] could not store %s: %s", target.name, exc)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# ── unspoken replies ─────────────────────────────────────────────────────────
# A reply that never reached the room must not look delivered. The speaking
# worker cannot raise into the turn (it is a daemon thread), and the Desktop is
# a different process, so failures are recorded here and ride the control API's
# ``/playback`` poll — the request the composer already makes to decide whether
# the speaker is busy. Bounded and short-lived: this is a status line, not a log.
NOTICE_TTL_MS = 15 * 60 * 1000
MAX_NOTICES = 20

_notices_lock = threading.Lock()


def _notices_path() -> Path:
    return ghconfig.state_dir() / "notices.json"


def _notice_fresh(entry: Dict[str, Any], now: Optional[int] = None) -> bool:
    try:
        at = int(entry.get("at") or 0)
    except (TypeError, ValueError):
        return False
    moment = int(time.time() * 1000) if now is None else now
    return bool(at) and moment - at <= NOTICE_TTL_MS


def _read_notices() -> Dict[str, Any]:
    """The notice file as ``{"seq": int, "entries": [...]}``, never raising."""
    try:
        raw = _notices_path().read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, TypeError, ValueError):
        return {"seq": 0, "entries": []}
    if not isinstance(payload, dict):
        return {"seq": 0, "entries": []}
    entries = payload.get("entries")
    try:
        seq = int(payload.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    return {
        "seq": seq,
        "entries": [entry for entry in entries if isinstance(entry, dict)]
        if isinstance(entries, list) else [],
    }


def record_failure(session_id: Any, entry: Dict[str, Any], error: Any) -> None:
    """Record that this conversation's reply never reached its speaker."""
    key = str(session_id or "").strip()
    message = str(error or "").strip()
    if not key or not message:
        return
    now = int(time.time() * 1000)
    with _notices_lock:
        state = _read_notices()
        entries = [item for item in state["entries"] if _notice_fresh(item, now)]
        seq = int(state.get("seq") or 0) + 1
        entries.append({
            "seq": seq,
            "session": key,
            "device": str(entry.get("device") or "").strip(),
            "name": str(entry.get("name") or "").strip(),
            "at": now,
            "error": message[:600],
        })
        if len(entries) > MAX_NOTICES:
            entries = entries[-MAX_NOTICES:]
        _replace_json(_notices_path(), {"seq": seq, "entries": entries})


def notices() -> list[Dict[str, Any]]:
    """Replies that were not spoken recently, oldest first (``seq`` ascending)."""
    now = int(time.time() * 1000)
    with _notices_lock:
        state = _read_notices()
    return [dict(item) for item in state["entries"] if _notice_fresh(item, now)]


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
def speak(text: str, entry: Dict[str, Any], session_id: str = "") -> Dict[str, Any]:
    """Synthesize and cast one reply to the conversation's speaker."""
    # Imported here: ``tools`` reaches the gateway adapter, and this module is
    # imported by registration in every process (and by the tests) without core.
    from . import tools

    settings = replace(ghconfig.resolve_settings(None), device=str(entry.get("device") or ""))
    with _speak_lock:
        result = tools.speak_to(text, settings)
    if result.get("ok") is False:
        message = result.get("error") or "could not speak the reply"
        logger.warning("[googlehome] %s", message)
        # The room stayed quiet, so the Desktop must say so: the text arrives,
        # the speaker does not. Silent failures look like the bug this hooks.
        record_failure(session_id, entry, message)
    return result


def _speak_in_background(text: str, entry: Dict[str, Any], session_id: str = "") -> None:
    def run() -> None:
        try:
            speak(text, entry, session_id)
        except Exception as exc:  # noqa: BLE001 - a worker must not die loudly
            logger.debug("[googlehome] background speak failed: %s", exc)

    # The turn runs inside the gateway's per-profile scope (a HERMES_HOME override
    # and the profile's secret mapping, both contextvars). A new thread starts
    # with an empty context, so TTS would read another profile's credentials — or,
    # on a multiplexed gateway, fail closed with UnscopedSecretError ("Hermes could
    # not read this profile's XAI_BASE_URL") and the reply would never be heard.
    # Copy the hook's context into the worker, exactly as Hermes' own hook
    # dispatcher does for the callback thread.
    context = contextvars.copy_context()
    threading.Thread(
        target=context.run,
        args=(run,),
        name="googlehome-speak",
        daemon=True,
    ).start()


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
        _speak_in_background(text, entry, str(session_id or ""))
    except Exception:  # noqa: BLE001 - a hook must never raise into the turn
        logger.debug("[googlehome] post_llm_call skipped", exc_info=True)


def register_hooks(ctx: Any) -> None:
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)
