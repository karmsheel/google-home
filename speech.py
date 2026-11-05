"""Turning agent replies into audio files, using Hermes's own TTS stack.

The plugin deliberately does not bundle a speech engine: Hermes already has
``text_to_speech_tool`` with a dozen providers, long-text chunking, and the
profile's configured voice. We call it and play the files it returns.

A profile whose configured provider cannot speak (a paid provider with no key,
say) still gets a voice: the free ``edge`` provider is tried next.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.MULTILINE)
_MD_NUMBERED = re.compile(r"^\s{0,3}\d+[.)]\s+", re.MULTILINE)
_MD_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~)")
_BLOCKQUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]{6,}\|?\s*$", re.MULTILINE)
_EXTRA_SPACE = re.compile(r"[ \t]{2,}")
_EXTRA_NEWLINES = re.compile(r"\n{3,}")

#: Providers tried when the profile's own TTS provider cannot speak. ``edge`` is
#: Hermes's free built-in, so a missing API key never leaves the speaker mute.
FALLBACK_PROVIDERS = ("edge",)


class TtsUnavailable(Exception):
    """No TTS provider could speak this text."""


def flatten_for_speech(text: str, max_chars: int = 1400) -> str:
    """Strip markdown so the model's formatting does not get read out loud."""
    if not text:
        return ""
    out = _CODE_FENCE.sub(" Code omitted. ", text)
    out = _IMAGE.sub("", out)
    out = _LINK.sub(r"\1", out)
    out = _INLINE_CODE.sub(r"\1", out)
    out = _MD_HEADING.sub("", out)
    out = _BLOCKQUOTE.sub("", out)
    out = _TABLE_RULE.sub("", out)
    out = _MD_BULLET.sub("", out)
    out = _MD_NUMBERED.sub("", out)
    out = _MD_EMPHASIS.sub("", out)
    out = out.replace("|", ", ")
    out = _EXTRA_SPACE.sub(" ", out)
    out = _EXTRA_NEWLINES.sub("\n\n", out).strip()
    if len(out) <= max_chars:
        return out

    clipped = out[:max_chars]
    for separator in (". ", "! ", "? ", "\n"):
        cut = clipped.rfind(separator)
        if cut > max_chars * 0.5:
            return clipped[:cut + 1].strip() + " …and more in the Hermes window."
    return clipped.rstrip() + " …and more in the Hermes window."


def _paths_from_payload(payload: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("file_paths", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            candidates.extend(str(item) for item in value if item)
    single = payload.get("file_path") or payload.get("audio_path")
    if single:
        candidates.append(str(single))

    paths: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if Path(candidate).is_file():
            paths.append(candidate)
    return paths


def _attempt(text: str, provider: str) -> list[str]:
    """One synthesis attempt with an explicit provider ('' = profile default)."""
    from tools.tts_tool import text_to_speech_tool

    kwargs: dict[str, Any] = {"text": text}
    if provider:
        kwargs["provider"] = provider
    raw = text_to_speech_tool(**kwargs)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        payload = {"success": False, "error": str(raw)[:300]}

    if not payload.get("success"):
        raise RuntimeError(str(payload.get("error") or "synthesis failed"))

    paths = _paths_from_payload(payload)
    if not paths:
        raise RuntimeError("TTS reported success but produced no audio file")
    return paths


def synthesize(text: str, *, provider: str = "") -> list[str]:
    """Synthesize ``text`` and return the local audio file paths.

    Tries the requested provider (or the profile's configured one), then the
    free built-ins. Raises :class:`TtsUnavailable` only when every attempt
    failed, with each provider's reason included.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        from tools.tts_tool import text_to_speech_tool  # noqa: F401 - availability check
    except Exception as exc:  # noqa: BLE001 - core import surface may move
        raise TtsUnavailable(f"Hermes TTS tools are not importable: {exc}") from exc

    candidates: list[str] = []
    for candidate in (provider, *FALLBACK_PROVIDERS):
        if candidate not in candidates:
            candidates.append(candidate)

    errors: list[str] = []
    for index, candidate in enumerate(candidates):
        label = candidate or "configured provider"
        try:
            paths = _attempt(text, candidate)
        except RuntimeError as exc:
            errors.append(f"{label}: {exc}")
            logger.info("TTS via %s failed: %s", label, exc)
            continue
        if index > 0:
            logger.info("TTS fell back to %s (the configured provider was unavailable)", label)
        logger.debug("synthesized %d audio file(s) for %d chars", len(paths), len(text))
        return paths

    raise TtsUnavailable(
        "No TTS provider could speak. "
        + " | ".join(errors)
        + ". Set one with `hermes config` (tts.provider); `edge` is free and needs no key."
    )


def describe_audio(path: str) -> dict[str, Any]:
    try:
        stat = Path(path).stat()
    except OSError:
        return {"path": path, "exists": False}
    return {"path": path, "exists": True, "bytes": stat.st_size}
