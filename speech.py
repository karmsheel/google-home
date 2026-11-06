"""Turning agent replies into audio files.

Three interchangeable backends, because a smart speaker with no voice is
useless when the profile's speech stack is misconfigured:

``hermes``
    Hermes's own ``text_to_speech_tool`` — the profile's configured provider and
    voice, long-text chunking included. This is the intended path and the only
    one that honours ``tts.*`` in config.yaml.
``sapi``
    The Windows speech engine, driven through ``System.Speech``. Offline, no
    package, no API key, WAV output.
``edge``
    Microsoft Edge's neural voices via the ``edge-tts`` package. No key, but it
    needs the network and Microsoft's endpoint rejects some client builds.

``auto`` tries the backends in order and remembers which one worked, so a broken
provider costs one slow call rather than one per reply. The chosen backend is
reported in the bridge status, never applied silently to the agent's own TTS.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

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

BACKENDS = ("hermes", "sapi", "edge")
DEFAULT_EDGE_VOICE = "en-US-AriaNeural"
EDGE_CHUNK_CHARS = 1800

#: Backend that last worked in this process; tried first next time.
_preferred: str = ""
_last_backend: str = ""


class TtsUnavailable(Exception):
    """No speech backend could produce audio for this text."""


def last_backend() -> str:
    """Which backend produced the most recent audio ("" if none yet)."""
    return _last_backend


# ── text ─────────────────────────────────────────────────────────────────────
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


def split_for_tts(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries, never mid-sentence when avoidable."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind("\n"))
        if cut < limit * 0.4:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut + 1].strip())
        remaining = remaining[cut + 1:].strip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


# ── backend: hermes ──────────────────────────────────────────────────────────
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


def _synth_hermes(text: str, provider: str, voice: str) -> list[str]:
    from tools.tts_tool import text_to_speech_tool

    kwargs: dict[str, Any] = {"text": text}
    if provider:
        # Hermes ignores this when it disagrees with tts.provider (#90109); it is
        # still passed so an operator who pinned the same value sees no change.
        kwargs["provider"] = provider
    raw = text_to_speech_tool(**kwargs)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        payload = {"success": False, "error": str(raw)[:300]}
    if not payload.get("success"):
        raise RuntimeError(str(payload.get("error") or "synthesis failed")[:500])
    paths = _paths_from_payload(payload)
    if not paths:
        raise RuntimeError("Hermes TTS reported success but produced no audio file")
    return paths


# ── backend: sapi (Windows, offline) ────────────────────────────────────────
def sapi_available() -> bool:
    return sys.platform == "win32"


def _synth_sapi(text: str, provider: str, voice: str) -> list[str]:
    if not sapi_available():
        raise RuntimeError("the Windows speech engine is only available on Windows")

    work = Path(tempfile.gettempdir()) / "hermes-googlehome-tts"
    work.mkdir(parents=True, exist_ok=True)
    stem = uuid.uuid4().hex[:12]
    script_path = work / f"speak-{stem}.ps1"
    text_path = work / f"text-{stem}.txt"
    audio_path = work / f"tts-{stem}.wav"

    text_path.write_text(text, encoding="utf-8")
    voice_line = ""
    if voice:
        # Ignore an unknown voice instead of failing the utterance.
        safe_voice = voice.replace("'", "''")
        voice_line = (
            f"try {{ $s.SelectVoice('{safe_voice}') }} "
            f"catch {{ Write-Output \"voice '$safe_voice' unavailable\" }}\n"
        )
    script = (
        "Add-Type -AssemblyName System.Speech\n"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer\n"
        f"{voice_line}"
        "$s.Rate = 0\n"
        f"$s.SetOutputToWaveFile('{audio_path}')\n"
        f"$s.Speak([System.IO.File]::ReadAllText('{text_path}', [System.Text.Encoding]::UTF8))\n"
        "$s.Dispose()\n"
    )
    script_path.write_text(script, encoding="utf-8")
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script_path)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"the Windows speech engine could not run: {exc}") from exc
    finally:
        for path in (script_path, text_path):
            try:
                path.unlink()
            except OSError:
                pass

    if not audio_path.is_file() or audio_path.stat().st_size < 1024:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"the Windows speech engine produced no audio{': ' + detail if detail else ''}")
    return [str(audio_path)]


# ── backend: edge (network, keyless) ────────────────────────────────────────
def _synth_edge(text: str, provider: str, voice: str) -> list[str]:
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("the edge-tts package is not installed") from exc

    work = Path(tempfile.gettempdir()) / "hermes-googlehome-tts"
    work.mkdir(parents=True, exist_ok=True)
    chosen = voice or DEFAULT_EDGE_VOICE
    paths: list[str] = []
    chunks = split_for_tts(text, EDGE_CHUNK_CHARS)
    for index, chunk in enumerate(chunks):
        target = work / f"edge-{uuid.uuid4().hex[:10]}-{index}.mp3"

        async def _run(chunk_text: str, out_path: Path) -> None:
            communicate = edge_tts.Communicate(chunk_text, chosen)
            await communicate.save(str(out_path))

        import asyncio

        try:
            asyncio.run(_run(chunk, target))
        except Exception as exc:  # noqa: BLE001 - network, protocol, token errors
            raise RuntimeError(f"edge-tts failed: {str(exc)[:200]}") from exc
        if not target.is_file() or target.stat().st_size < 512:
            raise RuntimeError("edge-tts produced no audio")
        paths.append(str(target))
    return paths


_BACKEND_IMPL: dict[str, Callable[[str, str, str], list[str]]] = {
    "hermes": _synth_hermes,
    "sapi": _synth_sapi,
    "edge": _synth_edge,
}


# ── public API ───────────────────────────────────────────────────────────────
def synthesize(
    text: str, *, provider: str = "", backend: str = "auto", voice: str = ""
) -> list[str]:
    """Synthesize ``text`` and return local audio file paths, in play order.

    ``backend`` is ``auto`` (default), ``hermes``, ``sapi`` or ``edge``.
    Raises :class:`TtsUnavailable` when every attempted backend failed, with
    each backend's reason included.
    """
    global _preferred, _last_backend

    text = (text or "").strip()
    if not text:
        return []

    requested = (backend or "auto").strip().lower()
    if requested in ("", "auto"):
        order = list(BACKENDS)
        if _preferred in order:
            order.remove(_preferred)
            order.insert(0, _preferred)
    elif requested in _BACKEND_IMPL:
        order = [requested]
    else:
        raise TtsUnavailable(
            f"unknown speech backend {backend!r}; use one of: auto, hermes, sapi, edge"
        )

    errors: list[str] = []
    for name in order:
        try:
            paths = _BACKEND_IMPL[name](text, provider, voice)
        except Exception as exc:  # noqa: BLE001 - every backend raises broadly
            message = str(exc) or exc.__class__.__name__
            errors.append(f"{name}: {message}")
            logger.info("speech backend %s failed: %s", name, message)
            continue
        if name != order[0]:
            logger.info("speech fell back to the %s backend (preferred: %s)", name, _preferred or "auto")
        _preferred = name
        _last_backend = name
        logger.debug("synthesized %d audio file(s) via %s for %d chars", len(paths), name, len(text))
        return paths

    raise TtsUnavailable("No speech backend could speak. " + " | ".join(errors))


def describe_audio(path: str) -> dict[str, Any]:
    try:
        stat = Path(path).stat()
    except OSError:
        return {"path": path, "exists": False}
    return {"path": path, "exists": True, "bytes": stat.st_size}
