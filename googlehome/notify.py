"""One-line task notifications on a Google Home speaker.

A reply belongs in the chat window. When somebody asks to be told that a long
task finished -- or that it is blocked -- they want one sentence in the room,
not the whole answer read aloud:

    Task complete: the report. Waiting for your review.
    Task blocked: the deploy. Needs your input.

This module builds that sentence, finds the speaker the person named ("the
living room speaker"), and hands it to the same cast path the other tools use.
The speech/cast import is deferred because ``tools`` needs the gateway core;
the composition and speaker matching here stay usable and testable without it.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import replace
from typing import Any, Callable, Iterable, Mapping, Optional

from . import config as ghconfig
from . import discovery, speech

logger = logging.getLogger(__name__)

STATUSES = ("done", "blocked", "failed")

_STATUS_WORDS = {"done": "complete", "blocked": "blocked", "failed": "failed"}
_STATUS_TAILS = {
    "done": "Waiting for your review.",
    "blocked": "Needs your input.",
    "failed": "Needs your attention.",
}
#: Agents (and people, through the CLI) phrase an outcome differently; accept
#: the common ways to say the same three things.
_STATUS_ALIASES = {
    "done": {
        "done", "complete", "completed", "finish", "finished", "success",
        "succeeded", "passed", "ready", "ready_for_review", "waiting_review",
        "awaiting_review", "review",
    },
    "blocked": {
        "blocked", "block", "needs_input", "need_input", "needs_me",
        "waiting_input", "waiting_for_input", "awaiting_input", "input_required",
        "question", "paused", "stuck",
    },
    "failed": {
        "failed", "fail", "failure", "error", "errored", "broke", "broken",
    },
}

#: A notification is a snippet, not a summary: keep the label and the optional
#: detail short enough that the speaker is done talking in a few seconds.
MAX_TASK_CHARS = 70
MAX_DETAIL_CHARS = 100
MAX_LINE_CHARS = 260

#: Unicode-aware so a room named in another language still matches; excludes
#: the underscore, which DNS-style ids carry but people do not say.
_WORD = re.compile(r"[^\W_]+")
#: Words that describe the medium rather than the room, so "the living room
#: speaker" and the device's name "Living Room speaker" compare equal.
_FILLER = {
    "the", "a", "an", "my", "our", "your", "his", "her", "their",
    "on", "in", "at", "to", "for", "of", "and", "please",
    "speaker", "speakers", "device", "cast", "google", "home", "nest", "mini",
}


class SpeakerLookupError(Exception):
    """The named speaker could not be found, or the name was ambiguous."""

    def __init__(self, message: str, available: Optional[Iterable[str]] = None) -> None:
        super().__init__(message)
        self.available = list(available or [])


# ── the sentence ─────────────────────────────────────────────────────────────
def normalize_status(value: Any) -> str:
    """Map a status word or alias to ``done``, ``blocked`` or ``failed``."""
    key = re.sub(r"[\s-]+", "_", str(value or "").strip().lower())
    if key in STATUSES:
        return key
    for canonical, aliases in _STATUS_ALIASES.items():
        if key in aliases:
            return canonical
    return ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = window.rfind(" ")
    if cut > limit * 0.5:
        window = window[:cut]
    return window.rstrip(" ,;:-") + "…"


def _oneline(value: Any, limit: int) -> str:
    """Flatten markdown and newlines into a short single line."""
    if value is None:
        return ""
    text = speech.flatten_for_speech(str(value), max(limit * 2, 200), tail="")
    return _clip(text, limit).strip()


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if not text.endswith((".", "!", "?", "…")):
        text += "."
    return text


def compose(status: Any, task: Any = "", detail: Any = "") -> str:
    """The whole notification, as one speakable line.

    Raises ``ValueError`` for an unknown status so a caller never announces
    something the person did not ask to hear.
    """
    canonical = normalize_status(status)
    if not canonical:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}")

    label = _oneline(task, MAX_TASK_CHARS).rstrip(" .!?…")
    note = _oneline(detail, MAX_DETAIL_CHARS)

    head = f"Task {_STATUS_WORDS[canonical]}"
    if label:
        head += f": {label}"
    head += "."

    parts = [head]
    if note:
        parts.append(_sentence(note))
    parts.append(_STATUS_TAILS[canonical])
    return _clip(" ".join(parts), MAX_LINE_CHARS)


# ── which speaker ────────────────────────────────────────────────────────────
def _tokens(value: Any) -> set[str]:
    return {word for word in _WORD.findall(str(value or "").lower()) if word not in _FILLER}


def _device_tokens(device: Mapping[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for field in ("name", "model", "address", "host"):
        tokens |= _tokens(device.get(field))
    return tokens


def match_devices(devices: Iterable[Mapping[str, Any]], spec: str) -> list[dict[str, Any]]:
    """Devices that match ``spec`` equally well, best first.

    Matching is token-based so "living room speaker" finds "Living Room
    speaker" without depending on case, punctuation, or the word "speaker".
    Returns every device tied for the best score: a tie is ambiguous, and
    guessing a room is worse than asking for a clearer name.
    """
    wanted = _tokens(spec)
    if not wanted:
        return []
    # "Speaker" is filler for name matching, but it is a useful filter: a room
    # can hold a Cast TV and a speaker with the same room name.
    prefer_audio = bool(set(_WORD.findall(str(spec).lower())) & {"speaker", "speakers", "audio"})

    scored: list[tuple[float, dict[str, Any]]] = []
    for raw in devices:
        device = discovery.normalize(raw)
        haystack = _device_tokens(device)
        overlap = wanted & haystack
        if not overlap:
            continue
        score = len(overlap) / len(wanted)
        name_tokens = _tokens(device.get("name"))
        if name_tokens and wanted <= name_tokens:
            score += 1.0  # the name contains every word the person said
        elif wanted <= haystack:
            score += 0.5  # spread across name/model/address, still all present
        if prefer_audio and device.get("audio_only"):
            score += 0.25
        scored.append((score, device))

    if not scored:
        return []
    scored.sort(key=lambda pair: pair[0], reverse=True)
    best = scored[0][0]
    return [device for score, device in scored if abs(score - best) < 1e-9]


def speaker_label(device: Mapping[str, Any]) -> str:
    """The room's own name first: that is what the person asked for."""
    device = discovery.normalize(dict(device))
    name = str(device.get("name") or "").strip()
    address = str(device.get("address") or device.get("host") or "").strip()
    model = str(device.get("model") or "").strip()
    if name and name != address:
        return name
    return model or address or "Cast device"


def speaker_names(devices: Iterable[Mapping[str, Any]]) -> list[str]:
    """Named choices for an error message, de-duplicated.

    The friendly name comes first because "Google Home Mini" alone cannot tell
    a living-room speaker from a bedroom one.
    """
    names: list[str] = []
    for raw in devices:
        device = discovery.normalize(dict(raw))
        label = speaker_label(device)
        address = str(device.get("address") or device.get("host") or "").strip()
        if address and address not in label:
            label = f"{label} ({address})"
        if label and label not in names:
            names.append(label)
    return names


def _is_ip(spec: str) -> bool:
    try:
        ipaddress.ip_address(spec)
        return True
    except ValueError:
        return False


def _direct_device(host: str, timeout: float = 3.0) -> dict[str, Any]:
    """A dialable device for an IP, named by its setup API when possible."""
    return discovery.resolve_device(host, timeout=timeout, enrich=True)


def _configured_device(configured: str, devices: list[dict[str, Any]], *,
                       timeout: float) -> dict[str, Any]:
    """Resolve the plugin's configured default, keeping its existing fallback."""
    if _is_ip(configured):
        return _direct_device(configured, timeout=timeout)
    matches = match_devices(devices, configured)
    if matches:
        return matches[0]
    if devices:
        logger.warning("configured speaker %r matched nothing; using %s",
                       configured, discovery.describe(devices[0]))
        audio = [device for device in devices if device.get("audio_only")] or devices
        return discovery.normalize(audio[0])
    return _direct_device(configured, timeout=timeout)


def resolve_target(
    spec: str = "",
    *,
    settings: Optional[ghconfig.Settings] = None,
    timeout: float = 3.0,
    discover_fn: Optional[Callable[..., list[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """Turn a speaker name, IP, or empty string into one dialable device.

    Empty means the configured speaker: the conversation's chosen speaker is
    resolved by the caller before this point. A name the person explicitly said
    is matched strictly -- an unknown room raises instead of silently using the
    first speaker on the network.
    """
    settings = settings or ghconfig.resolve_settings(None)
    spec = str(spec or "").strip()
    configured = str(settings.device or "").strip()
    # "The speaker", "my Google Home", or "the mini" name no particular room:
    # they mean the default, exactly like an omitted argument.
    if spec and not _tokens(spec):
        spec = ""

    # An address needs no multicast round-trip. This is the common case: the
    # composer stores the chosen device by address, so a notification should
    # not pay a 3-second discovery window before it can speak.
    if spec and _is_ip(spec):
        return _direct_device(spec, timeout=timeout)
    if not spec and configured and _is_ip(configured):
        return _direct_device(configured, timeout=timeout)

    discover = discover_fn or discovery.discover
    raw_devices = discover(timeout=timeout)
    devices = [discovery.normalize(device) for device in raw_devices]

    if spec:
        matches = match_devices(devices, spec)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = speaker_names(matches)
            raise SpeakerLookupError(
                f"{spec!r} matches more than one speaker: {', '.join(names)}. "
                "Use a more specific name or an IP address.",
                speaker_names(devices),
            )
        if configured and _tokens(configured) == _tokens(spec):
            return _configured_device(configured, devices, timeout=timeout)
        if configured:
            # mDNS may be blocked while the configured speaker is reachable by
            # IP; let its setup API confirm the friendly name before failing.
            candidate = _configured_device(configured, devices, timeout=timeout)
            if match_devices([candidate], spec):
                return candidate
        available = speaker_names(devices)
        found = ", ".join(available) if available else "none found on this network"
        raise SpeakerLookupError(
            f"No speaker matches {spec!r}. Found: {found}.", available
        )

    if configured:
        return _configured_device(configured, devices, timeout=timeout)
    if not devices:
        raise SpeakerLookupError(
            "No Cast device was found. Power the speaker on or set its IP in the "
            "plugin settings.",
            [],
        )
    audio = [device for device in devices if device.get("audio_only")] or devices
    return discovery.normalize(audio[0])


# ── speaking it ──────────────────────────────────────────────────────────────
def _adapter_targets(adapter: Any, host: str) -> bool:
    """True when the running adapter already holds this speaker's session."""
    if not adapter or not host:
        return False
    device = getattr(adapter, "_device", None) or {}
    bound = str(device.get("host") or device.get("address") or "").strip()
    if not bound:
        settings = getattr(adapter, "settings", None)
        configured = str(getattr(settings, "device", "") or "").strip()
        if configured:
            if _is_ip(configured):
                bound = configured
            else:
                try:
                    bound = discovery.dial_host(
                        discovery.resolve_device(configured, timeout=2.0, enrich=False)
                    )
                except Exception:  # noqa: BLE001 - a lookup failure means "not it"
                    bound = ""
    return bool(bound) and bound == host


def _cast(line: str, device: dict[str, Any], settings: ghconfig.Settings) -> dict[str, Any]:
    """Speak through the live adapter when it owns this speaker, else directly."""
    from . import tools  # deferred: tools imports the gateway adapter

    target_host = discovery.dial_host(device)
    adapter = tools.active_adapter()
    if adapter is not None and _adapter_targets(adapter, target_host):
        outcome = tools._speak(line)
        outcome.setdefault("via", "gateway")
        return outcome
    outcome = tools.speak_to(line, settings, source="notify")
    outcome.setdefault("via", "direct")
    return outcome


def announce(
    status: Any,
    task: Any = "",
    detail: Any = "",
    speaker: Any = "",
    *,
    settings: Optional[ghconfig.Settings] = None,
    cast: Optional[Callable[[str, dict[str, Any], ghconfig.Settings], dict[str, Any]]] = None,
    discover_fn: Optional[Callable[..., list[dict[str, Any]]]] = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Compose and speak one task notification; return a JSON-ready result."""
    settings = settings or ghconfig.resolve_settings(None)
    canonical = normalize_status(status)
    line = compose(canonical, task, detail)
    device = resolve_target(
        str(speaker or ""), settings=settings, timeout=timeout, discover_fn=discover_fn
    )
    host = discovery.dial_host(device)
    if not host:
        raise SpeakerLookupError(
            "The speaker has no network address.",
            speaker_names([device]),
        )

    target = replace(settings, device=host)
    outcome = (cast or _cast)(line, device, target)
    if not isinstance(outcome, dict):
        outcome = {"ok": False, "error": "the speaker bridge returned no result"}
    result = {
        **outcome,
        "ok": bool(outcome.get("ok")),
        "status": canonical,
        "spoken": line,
        "device": outcome.get("device") or speaker_label(device),
        "address": host,
    }
    if not result["ok"]:
        logger.warning("[googlehome] notification not spoken: %s",
                       result.get("error") or "unknown error")
    return result
