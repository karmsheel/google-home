"""Cron wiring: make the speaker an extra delivery target for scheduled jobs.

Hermes cron fans a job's ``deliver`` value out to comma-separated targets
(``buzz,telegram``), so the speaker does not need its own delivery mechanism —
it needs a token in that list. The token is explicit
(``googlehome:speaker-…``) rather than a bare platform name because the
speaker is one gateway-wide adapter: enabling ``platforms.googlehome`` in every
profile's config would start a second adapter for each profile and the two
would fight over the device and its media port.

The deliver-string rewriting here is deliberately free of Hermes imports so
the stdlib-only test suite can cover it. Only :func:`apply_jobs` and
:func:`platform_ready` touch cron/gateway internals, and they import lazily.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator

logger = logging.getLogger(__name__)

PLATFORM = "googlehome"
LOCAL = "local"


# ── deliver strings ──────────────────────────────────────────────────────────
def parse_deliver(value: Any) -> list[str]:
    """``deliver`` as a list of tokens. Lists/tuples (hand-edited stores) flatten."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw = [str(part).strip() for part in value]
    else:
        raw = [part.strip() for part in str(value).split(",")]
    return [part for part in raw if part]


def deliver_to_string(parts: Iterable[str]) -> str:
    kept = [str(part).strip() for part in parts if str(part).strip()]
    return ",".join(kept) if kept else LOCAL


def token_platform(token: str) -> str:
    return str(token).split(":", 1)[0].strip().lower()


def is_attached(value: Any) -> bool:
    return any(token_platform(token) == PLATFORM for token in parse_deliver(value))


def attach(value: Any, chat_id: str) -> tuple[str, bool]:
    """Add the speaker to a job's deliver list. Returns ``(new_value, changed)``.

    A bare ``googlehome`` token is upgraded to the explicit form (bare only
    resolves when a home channel is configured, which this plugin does not
    require). ``local`` alone is replaced: cron still writes the output file,
    and ``local`` as a *delivery* token is a no-op.
    """
    chat_id = (chat_id or "").strip()
    if not chat_id:
        raise ValueError("a speaker chat id is required to attach cron delivery")
    target = f"{PLATFORM}:{chat_id}"

    parts = parse_deliver(value)
    out: list[str] = []
    changed = False
    for token in parts:
        if token_platform(token) == PLATFORM:
            if token.lower() == PLATFORM:
                out.append(target)
                changed = True
            else:
                out.append(token)
        else:
            out.append(token)

    if any(token_platform(token) == PLATFORM for token in out):
        return deliver_to_string(out), changed

    if not [token for token in out if token.lower() != LOCAL]:
        # The job only saved its output locally; replace that no-op token.
        return target, True

    out.append(target)
    return deliver_to_string(out), True


def detach(value: Any) -> tuple[str, bool]:
    """Remove the speaker from a job's deliver list. Returns ``(new_value, changed)``."""
    parts = parse_deliver(value)
    kept = [token for token in parts if token_platform(token) != PLATFORM]
    if len(kept) == len(parts):
        return deliver_to_string(parts), False
    return deliver_to_string(kept), True


# ── which speaker chat id? ───────────────────────────────────────────────────
def resolve_chat_id(settings_chat_id: str, override: str = "") -> str:
    """Chat id for the delivery target: explicit override → running bridge → settings.

    The running bridge's id is preferred because it is the id the adapter uses
    for its own session. It is not strictly required for playback (the adapter
    has exactly one speaker and ignores the chat argument), so a fallback is
    harmless.
    """
    override = (override or "").strip()
    if override:
        return override
    live = live_chat_id()
    return live or (settings_chat_id or "").strip()


def live_chat_id() -> str:
    """The chat id written by the running bridge, if one is discoverable."""
    for home in candidate_homes():
        path = Path(home) / "googlehome" / "control.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        chat_id = str(data.get("chat_id") or "").strip()
        if chat_id:
            return chat_id
    return ""


def candidate_homes() -> list[Path]:
    """Hermes homes that could hold the gateway's ``control.json``.

    The gateway usually runs as the default profile while a ``-p <name>`` CLI
    invocation has ``HERMES_HOME`` pointed at the profile, so both are checked.
    """
    homes: list[Path] = []
    raw = os.environ.get("HERMES_HOME", "").strip()
    if raw:
        here = Path(raw)
        homes.append(here)
        if here.parent.name.lower() == "profiles":
            homes.append(here.parent.parent)
    elif sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        if local:
            homes.append(Path(local) / "hermes")
    else:
        homes.append(Path.home() / ".hermes")

    seen: set[str] = set()
    unique: list[Path] = []
    for home in homes:
        key = os.path.normcase(str(home))
        if key not in seen:
            seen.add(key)
            unique.append(home)
    return unique


def profile_label(home: Path | str | None = None) -> str:
    """Display name of a Hermes home, or of this process's profile."""
    raw = str(home) if home is not None else os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return "default"
    path = Path(raw)
    return path.name if path.parent.name.lower() == "profiles" else "default"


# ── Hermes glue (lazy imports) ───────────────────────────────────────────────
def _cron_jobs() -> Any:
    from cron import jobs as cron_jobs

    return cron_jobs


@contextlib.contextmanager
def _store(cron_jobs: Any, home: Path | None) -> Iterator[None]:
    """Route the cron store at another profile's home, when asked.

    The plugin is discoverable from the default home only (Hermes scans
    ``$HERMES_HOME/plugins``), so ``hermes -p researcher googlehome`` cannot
    load this command. ``--profile`` keeps one command working for every
    profile while the store is switched underneath it.
    """
    if home is None:
        yield
        return
    use = getattr(cron_jobs, "use_cron_store", None)
    if not callable(use):
        raise RuntimeError("this Hermes build cannot address another profile's cron store")
    with use(home):
        yield


@contextlib.contextmanager
def _locked(cron_jobs: Any) -> Iterator[None]:
    """The same load→modify→save critical section Hermes' own CLI uses."""
    lock = getattr(cron_jobs, "_jobs_lock", None)
    if lock is None:
        yield
        return
    with lock():
        yield


def apply_jobs(
    action: str, chat_id: str, *, dry_run: bool = False, home: Path | None = None
) -> list[dict[str, Any]]:
    """Attach (``on``) or detach (``off``) the speaker on every job of a profile."""
    if action not in ("on", "off"):
        raise ValueError(f"unknown cron action {action!r}")
    cron_jobs = _cron_jobs()
    rows: list[dict[str, Any]] = []
    with _store(cron_jobs, home), _locked(cron_jobs):
        jobs = cron_jobs.load_jobs()
        for job in jobs:
            before = job.get("deliver", LOCAL)
            after, changed = attach(before, chat_id) if action == "on" else detach(before)
            rows.append({
                "id": job.get("id", ""),
                "name": job.get("name", "") or job.get("id", ""),
                "enabled": bool(job.get("enabled", True)),
                "before": deliver_to_string(parse_deliver(before)),
                "after": after,
                "changed": changed,
            })
            if changed and not dry_run:
                job["deliver"] = after
        if not dry_run and any(row["changed"] for row in rows):
            cron_jobs.save_jobs(jobs)
    return rows


def list_jobs(home: Path | None = None) -> list[dict[str, Any]]:
    """Current jobs with their attach state, for ``cron status``."""
    cron_jobs = _cron_jobs()
    with _store(cron_jobs, home), _locked(cron_jobs):
        jobs = cron_jobs.load_jobs()
        return [
            {
                "id": job.get("id", ""),
                "name": job.get("name", "") or job.get("id", ""),
                "enabled": bool(job.get("enabled", True)),
                "deliver": deliver_to_string(parse_deliver(job.get("deliver", LOCAL))),
                "failure_deliver": (
                    deliver_to_string(parse_deliver(job.get("failure_deliver")))
                    if job.get("failure_deliver") else ""
                ),
                "attached": is_attached(job.get("deliver")),
            }
            for job in jobs
        ]


def platform_ready() -> bool:
    """True when this Hermes knows googlehome as a cron delivery platform.

    Explicit ``googlehome:<chat>`` targets work without this, but the field is
    what makes bare tokens, ``deliver=all``-adjacent flows and the cron target
    pickers aware of the speaker. A False here usually means the plugin needs
    reinstalling/restarting (the running gateway still has the old manifest).
    """
    try:
        from hermes_cli.plugins import discover_plugins

        discover_plugins()
        from gateway.platform_registry import platform_registry

        entry = platform_registry.get(PLATFORM)
        return bool(entry is not None and getattr(entry, "cron_deliver_env_var", ""))
    except Exception as exc:  # noqa: BLE001 - status must never explode
        logger.debug("cron platform probe failed: %s", exc)
        return False
