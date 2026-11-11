"""PlatformEntry kwargs for the googlehome platform.

``PlatformEntry`` grows fields across Hermes releases and an unknown kwarg is a
hard ``TypeError``, so every optional key is feature-detected against the
installed dataclass instead of being passed blindly.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any

logger = logging.getLogger(__name__)

LABEL = "Google Home"

PLATFORM_HINT = (
    "You are talking with the user through a smart speaker in their room. "
    "Every reply is read aloud, so answer for the ear: short, warm, plain prose. "
    "No markdown, no bullet lists, no code blocks, no tables, no URLs spelled out. "
    "If something is long, give the one-sentence answer first and offer detail. "
    "Read aloud numbers and dates the way a person would say them."
)


def _supported_fields() -> set[str]:
    try:
        from gateway.platform_registry import PlatformEntry

        return {field.name for field in dataclasses.fields(PlatformEntry)}
    except Exception:  # noqa: BLE001 - older core or import surface change
        return {
            "name", "label", "adapter_factory", "check_fn", "validate_config",
            "required_env", "install_hint", "max_message_length", "emoji",
            "platform_hint", "source", "plugin_name",
        }


def build_platform_kwargs(*, ctx: Any, adapter: Any, check_fn: Any, validate_config: Any) -> dict[str, Any]:
    """Assemble the kwargs for ``ctx.register_platform``."""
    from . import PLATFORM_NAME
    from .standalone import send_standalone
    from .standalone import send_standalone

    kwargs: dict[str, Any] = {
        "name": PLATFORM_NAME,
        "label": LABEL,
        "check_fn": check_fn,
        "validate_config": validate_config,
        "required_env": [],
        "install_hint": "",
        "max_message_length": 0,
        "emoji": "🔊",
        "platform_hint": PLATFORM_HINT,
    }
    optional: dict[str, Any] = {
        "allow_update_command": True,
        "display_tier": "minimal",
        "display_overrides": {},
        "shared_env_prefixes": ("GOOGLEHOME_",),
        "dashboard_label": LABEL,
        "dashboard_description": (
            "Speak through a Google Home / Nest speaker. Replies from the bound "
            "profile are synthesized and cast to the device."
        ),
        "docs_url": "https://github.com/NousResearch/hermes-agent",
        "pii_safe": True,
        # Cron reads this env var when a job's ``deliver`` names this platform
        # without a chat id. The helper writes explicit targets instead (one
        # gateway-wide adapter serves every profile), but declaring the field
        # keeps ``googlehome`` a valid delivery platform for bare tokens and
        # for the cron target pickers.
        "cron_deliver_env_var": "GOOGLEHOME_HOME_CHANNEL",
        # Cron output also arrives from short-lived processes (`hermes cron run`,
        # restart-safe workers) where no live adapter exists. This sender asks
        # the running bridge to speak, falling back to an inline cast.
        "standalone_sender_fn": send_standalone,
    }
    supported = _supported_fields()
    for key, value in optional.items():
        if key in supported:
            kwargs[key] = value
    return kwargs
