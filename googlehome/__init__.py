"""googlehome — a Hermes Agent plugin that puts a Google Home/Nest speaker on the
other end of a profile's conversation.

The speaker is the mouth: replies are synthesized with Hermes's own TTS stack
and cast to the device over the Cast v2 protocol. The ear is the host machine's
microphone, because Google Home hardware exposes no microphone stream to third
parties (see docs/REVERSE_ENGINEERING.md).

Importing this package never imports Hermes core: the gateway imports it in
every process, so ``register()`` defers every core import until it runs.
"""
from __future__ import annotations

from typing import Any

__version__ = "0.2.0"
PLUGIN_NAME = "googlehome"
PLATFORM_NAME = "googlehome"
__all__ = ["register", "PLUGIN_NAME", "PLATFORM_NAME", "__version__"]


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    from .adapter import GoogleHomeAdapter, check_requirements, validate_config
    from . import ambient
    from .cli import run_action, setup_cli
    from .config import remember_ctx
    from .platform_entry import build_platform_kwargs
    from .tools import register_tools

    # The gateway builds the adapter from PlatformConfig alone, so stash the
    # plugin context now: it is how the adapter reads Desktop settings and
    # persists a device/profile chosen from the UI.
    remember_ctx(ctx)

    ctx.register_platform(adapter_factory=GoogleHomeAdapter, **build_platform_kwargs(
        ctx=ctx,
        adapter=GoogleHomeAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
    ))
    register_tools(ctx)

    # Per-conversation destinations chosen from the Desktop composer: a normal
    # chat speaks only when it was pointed at a speaker there.
    ambient.register_hooks(ctx)
    ctx.register_cli_command(
        name="googlehome",
        help="Google Home speaker bridge: check, devices, speak, status, firewall",
        description=(
            "Check that a Google Home / Nest speaker is reachable and can play audio "
            "from this machine, list Cast devices, speak a phrase, read the running "
            "bridge status, or open the media port in Windows Firewall."
        ),
        setup_fn=setup_cli,
        handler_fn=run_action,
    )
