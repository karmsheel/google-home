"""Settings and on-disk locations for the googlehome plugin.

Values resolve in this order, most specific first:

1. the plugin's own settings (``plugins.entries.googlehome.settings.*``) — this
   is what Hermes Desktop's Google Home page writes when you pick a speaker or a
   profile, and what ``ctx.set_config`` persists
2. ``platforms.googlehome.extra`` in the profile's ``config.yaml``
3. ``GOOGLEHOME_*`` environment variables
4. defaults below

The plugin context is stashed at registration time because the gateway builds
the adapter from ``adapter_factory(PlatformConfig)`` and does not hand it the
plugin context.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import pysite

logger = logging.getLogger(__name__)

ENV_PREFIX = "GOOGLEHOME_"

#: fallback spoken-reply length before chunking
DEFAULT_MAX_CHARS = 1400
#: a cron delivery is a notification, not a podcast: speak a digest, not the report
DEFAULT_CRON_MAX_CHARS = 400
DEFAULT_CONTROL_PORT = 8765

_PLUGIN_CTX: Optional[Any] = None


def remember_ctx(ctx: Any) -> None:
    """Called from ``register(ctx)`` so the adapter can read Desktop settings."""
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx


def plugin_ctx() -> Optional[Any]:
    return _PLUGIN_CTX


def _plugin_setting(key: str) -> Any:
    ctx = _PLUGIN_CTX
    if ctx is None:
        return None
    try:
        return ctx.get_config(key, None)
    except Exception:  # noqa: BLE001 - settings are best-effort
        return None


def _env(key: str) -> str:
    return os.environ.get(ENV_PREFIX + key.upper(), "") or ""


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


@dataclass
class Settings:
    """Effective configuration for one adapter instance."""

    device: str = ""
    profile: str = ""
    volume: int = 0
    tts_provider: str = ""
    speech_backend: str = "auto"
    speech_voice: str = ""
    max_chars: int = DEFAULT_MAX_CHARS
    cron_max_chars: int = DEFAULT_CRON_MAX_CHARS
    control_port: int = DEFAULT_CONTROL_PORT
    media_port: int = 0
    token_required: bool = True
    speak_replies: bool = True
    speak_cron: bool = True
    restore_volume: bool = True
    # Secondary multiplex profile: forward speech to the primary bridge instead
    # of opening a second Cast connection and media server for the same speaker.
    proxy: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def chat_id(self) -> str:
        """Stable chat key so one speaker keeps one Hermes session."""
        slug = (self.device or "default").strip().lower().replace(" ", "-") or "default"
        return f"speaker-{slug}"


def _first(*candidates: Any, default: Any = None) -> Any:
    for candidate in candidates:
        if candidate is None:
            continue
        if isinstance(candidate, str) and not candidate.strip():
            continue
        return candidate
    return default


_EXTRA_CACHE: dict[str, Any] = {"key": None, "value": {}}


def config_file_extra() -> dict[str, Any]:
    """``platforms.googlehome.extra`` from the active profile's config.yaml.

    Only a fallback: the gateway hands the adapter a PlatformConfig carrying the
    same block, while CLI commands and agent tools run without one and used to
    ignore config.yaml altogether (so ``hermes googlehome check`` reported
    "device: (auto)" on a pinned speaker).
    """
    path = hermes_home() / "config.yaml"
    try:
        stat = path.stat()
    except OSError:
        return {}
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _EXTRA_CACHE["key"] == key:
        return dict(_EXTRA_CACHE["value"])
    try:
        yaml = pysite.ensure_import("yaml")
    except ImportError:  # pragma: no cover - Hermes always ships PyYAML
        logger.debug("pyyaml unavailable; platforms.googlehome.extra ignored")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return {}
    platforms = data.get("platforms") if isinstance(data, dict) else None
    entry = platforms.get("googlehome") if isinstance(platforms, dict) else None
    extra = entry.get("extra") if isinstance(entry, dict) else None
    value = dict(extra) if isinstance(extra, dict) else {}
    _EXTRA_CACHE.update({"key": key, "value": value})
    return dict(value)


def resolve_settings(config: Any = None) -> Settings:
    """Build ``Settings``: plugin settings, then platform extra, then env."""
    extra: dict = {}
    if config is not None:
        raw_extra = getattr(config, "extra", None)
        if isinstance(raw_extra, dict):
            extra = raw_extra
    else:
        # No PlatformConfig: this is the CLI/tool path, where the gateway has
        # not handed us one. Read the same ``platforms.googlehome.extra`` block
        # out of config.yaml so ``hermes googlehome check`` agrees with the
        # adapter instead of silently falling back to auto-discovery.
        extra = config_file_extra()

    def pick(key: str, default: Any = None) -> Any:
        """Desktop setting wins, then config.yaml extra, then environment."""
        return _first(_plugin_setting(key), extra.get(key), _env(key), default=default)

    volume = max(0, min(100, _as_int(pick("volume", 0), 0)))
    max_chars = max(120, min(8000, _as_int(pick("max_chars", DEFAULT_MAX_CHARS),
                                            DEFAULT_MAX_CHARS)))
    cron_max_chars = max(80, min(2000, _as_int(pick("cron_max_chars", DEFAULT_CRON_MAX_CHARS),
                                               DEFAULT_CRON_MAX_CHARS)))
    control_port = _as_int(pick("control_port", DEFAULT_CONTROL_PORT), DEFAULT_CONTROL_PORT)
    media_port = _as_int(pick("media_port", 0), 0)

    return Settings(
        device=str(pick("device", "") or "").strip(),
        profile=str(pick("profile", "") or "").strip(),
        volume=volume,
        tts_provider=str(pick("tts_provider", "") or "").strip(),
        speech_backend=(str(pick("speech_backend", "auto") or "auto").strip().lower() or "auto"),
        speech_voice=str(pick("speech_voice", "") or "").strip(),
        max_chars=max_chars,
        cron_max_chars=cron_max_chars,
        control_port=control_port,
        media_port=media_port,
        token_required=_as_bool(pick("token_required"), True),
        speak_replies=_as_bool(pick("speak_replies"), True),
        speak_cron=_as_bool(pick("speak_cron"), True),
        restore_volume=_as_bool(pick("restore_volume"), True),
        proxy=_as_bool(pick("proxy"), False),
        extra=extra,
    )


# ── paths ────────────────────────────────────────────────────────────────────
def hermes_home() -> Path:
    """The active profile's Hermes home (set by the gateway per profile)."""
    raw = os.environ.get("HERMES_HOME", "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".hermes"


def state_dir() -> Path:
    """Per-profile scratch/state directory for this plugin."""
    path = hermes_home() / "googlehome"
    path.mkdir(parents=True, exist_ok=True)
    return path


def media_dir() -> Path:
    path = state_dir() / "media"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_path() -> Path:
    return state_dir() / "control-token"


def load_token() -> str:
    path = token_path()
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ensure_token() -> str:
    """Create the local control token once and return it."""
    import secrets

    existing = load_token()
    if existing:
        return existing
    token = secrets.token_urlsafe(24)
    try:
        path = token_path()
        path.write_text(token, encoding="utf-8")
        try:  # best effort on POSIX; Windows inherits the user's profile ACL
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning("could not persist googlehome control token: %s", exc)
    return token
