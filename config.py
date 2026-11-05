"""Settings and on-disk locations for the googlehome plugin.

Values resolve in this order, most specific first:

1. ``platforms.googlehome.extra`` in the profile's ``config.yaml``
2. the plugin's own Desktop settings (``plugins.entries.googlehome.settings.*``)
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

logger = logging.getLogger(__name__)

ENV_PREFIX = "GOOGLEHOME_"

#: fallback spoken-reply length before chunking
DEFAULT_MAX_CHARS = 1400
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
    max_chars: int = DEFAULT_MAX_CHARS
    control_port: int = DEFAULT_CONTROL_PORT
    media_port: int = 0
    token_required: bool = True
    speak_replies: bool = True
    restore_volume: bool = True
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


def resolve_settings(config: Any = None) -> Settings:
    """Build ``Settings`` from platform extra, plugin settings, env, defaults."""
    extra = {}
    if config is not None:
        raw_extra = getattr(config, "extra", None)
        if isinstance(raw_extra, dict):
            extra = raw_extra

    device = _first(extra.get("device"), _plugin_setting("device"), _env("device"), default="")
    profile = _first(extra.get("profile"), _plugin_setting("profile"), _env("profile"), default="")
    volume = _as_int(
        _first(extra.get("volume"), _plugin_setting("volume"), _env("volume"), default=0), 0
    )
    tts_provider = _first(
        extra.get("tts_provider"), _plugin_setting("tts_provider"), _env("tts_provider"), default=""
    )
    max_chars = _as_int(
        _first(extra.get("max_chars"), _plugin_setting("max_chars"), _env("max_chars"),
               default=DEFAULT_MAX_CHARS),
        DEFAULT_MAX_CHARS,
    )
    control_port = _as_int(
        _first(extra.get("control_port"), _plugin_setting("control_port"), _env("control_port"),
               default=DEFAULT_CONTROL_PORT),
        DEFAULT_CONTROL_PORT,
    )
    media_port = _as_int(
        _first(extra.get("media_port"), _plugin_setting("media_port"), _env("media_port"), default=0), 0
    )
    token_required = _as_bool(
        _first(extra.get("token_required"), _plugin_setting("token_required"), _env("token_required")),
        True,
    )
    speak_replies = _as_bool(
        _first(extra.get("speak_replies"), _plugin_setting("speak_replies"), _env("speak_replies")),
        True,
    )
    restore_volume = _as_bool(
        _first(extra.get("restore_volume"), _plugin_setting("restore_volume"),
               _env("restore_volume")),
        True,
    )
    return Settings(
        device=str(device).strip(),
        profile=str(profile).strip(),
        volume=max(0, min(100, volume)),
        tts_provider=str(tts_provider).strip(),
        max_chars=max(120, min(8000, max_chars)),
        control_port=control_port,
        media_port=media_port,
        token_required=token_required,
        speak_replies=speak_replies,
        restore_volume=restore_volume,
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
