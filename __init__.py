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

__version__ = "0.1.0"
PLUGIN_NAME = "googlehome"
PLATFORM_NAME = "googlehome"
__all__ = ["register", "PLUGIN_NAME", "PLATFORM_NAME", "__version__"]


def register(ctx: Any) -> None:
    """Hermes plugin entry point."""
    from .adapter import GoogleHomeAdapter, check_requirements, validate_config
    from .platform_entry import build_platform_kwargs
    from .tools import register_tools

    ctx.register_platform(adapter_factory=GoogleHomeAdapter, **build_platform_kwargs(
        ctx=ctx,
        adapter=GoogleHomeAdapter,
        check_fn=check_requirements,
        validate_config=validate_config,
    ))
    register_tools(ctx)
