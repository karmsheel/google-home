"""Turning agent replies into audio files, using Hermes's own TTS stack.

The plugin deliberately does not bundle a speech engine: Hermes already has
``text_to_speech_tool`` with a dozen providers, chunking, and the profile's
configured voice. We call it and play the files it returns.
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


class TtsUnavailable(Exception):
    """Hermes has no usable TTS provider configured."""


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


def synthesize(text: str, *, provider: str = "", timeout_hint: float = 60.0) -> list[str]:
    """Synthesize ``text`` and return the local audio file paths.

    Raises :class:`TtsUnavailable` when Hermes cannot speak at all, and
    ``RuntimeError`` when the provider failed for this utterance.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        from tools.tts_tool import check_tts_requirements, text_to_speech_tool
    except Exception as exc:  # noqa: BLE001 - core import surface may move
        raise TtsUnavailable(f"Hermes TTS tools are not importable: {exc}") from exc

    if not check_tts_requirements():
        raise TtsUnavailable(
            "No TTS provider is configured. Set one with `hermes config` (tts.provider) "
            "or `hermes setup`."
        )

    kwargs: dict[str, Any] = {"text": text}
    if provider:
        kwargs["provider"] = provider
    try:
        raw = text_to_speech_tool(**kwargs)
    except Exception as exc:  # noqa: BLE001 - provider/network failures land here
        raise RuntimeError(f"TTS synthesis failed: {exc}") from exc

    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        payload = {"success": False, "error": str(raw)[:300]}

    if not payload.get("success"):
        raise RuntimeError(str(payload.get("error") or "TTS synthesis failed"))

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
    if not paths:
        raise RuntimeError("TTS reported success but produced no audio file")
    logger.debug("synthesized %d audio file(s) for %d chars", len(paths), len(text))
    return paths


def describe_audio(path: str) -> dict[str, Any]:
    try:
        stat = Path(path).stat()
    except OSError:
        return {"path": path, "exists": False}
    return {"path": path, "exists": True, "bytes": stat.st_size}
