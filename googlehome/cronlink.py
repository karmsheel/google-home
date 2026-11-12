"""Cron wiring: make the speaker an extra delivery target for scheduled jobs.

Hermes cron fans a job's ``deliver`` value out to comma-separated targets
(``buzz,telegram``), so the speaker does not need its own delivery mechanism —
it needs a token in that list. The token is explicit
(``googlehome:speaker-…``) rather than a bare platform name because the
speaker is one gateway-wide adapter: enabling ``platforms.googlehome`` in every
profile's config would start a second adapter for each profile and the two
would fight over the device and its media port.

This module never opens the cron store. Reads go through ``hermes cron list``
and writes through ``hermes cron edit <id> --deliver <target>``, both public
commands, so a change to the on-disk job shape can never strand the plugin
(or race the scheduler's own lock). The deliver-string rewriting stays free of
Hermes imports so the stdlib-only test suite can cover it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

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
    invocation has a profile home, so both the active home and the root that
    holds ``profiles/`` are checked. Paths come from ``hermes_constants`` (the
    public helpers) rather than a bare environment read.
    """
    homes: list[Path] = []
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        homes = [Path(get_hermes_home()), Path(get_default_hermes_root())]
    except Exception:  # noqa: BLE001 - stdlib tests do not import Hermes core
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
    if home is not None:
        raw = str(home)
    else:
        try:
            from hermes_constants import get_hermes_home

            raw = str(get_hermes_home())
        except Exception:  # noqa: BLE001 - stdlib tests do not import Hermes core
            raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return "default"
    path = Path(raw)
    return path.name if path.parent.name.lower() == "profiles" else "default"


# ── Hermes CLI (public commands only) ────────────────────────────────────────
_COMMAND_TIMEOUT = 120.0
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
#: `hermes cron list` prints one job per `  <id> [state]` line, then indented
#: `Label: value` detail rows. Read only the two fields this plugin needs.
_JOB_LINE = re.compile(r"^\s{2}(?P<id>\S+)\s+\[(?P<state>active|paused|disabled|completed)\]\s*$")
_DETAIL_LINE = re.compile(r"^\s{4}(?P<label>[A-Za-z][A-Za-z ]*):\s+(?P<value>.*)$")


def hermes_command() -> list[str]:
    """The ``hermes`` executable, for the two public cron commands used here."""
    exe = shutil.which("hermes")
    if exe:
        return [exe]
    argv0 = str(sys.argv[0] if sys.argv else "").strip()
    if argv0 and Path(argv0).name.lower().startswith("hermes"):
        return [argv0]
    raise RuntimeError(
        "could not find the `hermes` command; run this through the hermes CLI"
    )


def _run_hermes(
    args: list[str], *, home: Path | None = None, timeout: float = _COMMAND_TIMEOUT
) -> str:
    """Run a public ``hermes`` command and return its stdout.

    ``home`` selects another profile's store by pointing the child at that
    ``HERMES_HOME`` — the same contract ``hermes -p <name>`` uses — so no
    in-process store switching (and no private lock) is involved.
    """
    env = dict(os.environ)
    if home is not None:
        env["HERMES_HOME"] = str(home)
    command = [*hermes_command(), *args]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not run `{' '.join(command)}`: {exc}") from exc
    if result.returncode != 0:
        detail = _ANSI.sub("", (result.stderr or result.stdout or "").strip())
        reason = detail[-400:] or f"exit {result.returncode}"
        raise RuntimeError(f"`{' '.join(command)}` failed: {reason}")
    return result.stdout or ""


def edit_command(job_id: str, deliver: str) -> str:
    """The public command that sets a job's delivery list."""
    return f"hermes cron edit {job_id} --deliver {deliver}"


def edit_deliver(job_id: str, deliver: str, *, home: Path | None = None) -> str:
    """Apply a job's new delivery list through ``hermes cron edit``."""
    _run_hermes(["cron", "edit", job_id, "--deliver", deliver], home=home)
    return edit_command(job_id, deliver)


def parse_jobs(text: str) -> list[dict[str, Any]]:
    """Parse ``hermes cron list`` output into the rows this plugin reports.

    Raises when the output does not look like a job listing at all (an empty
    store says so in words); silently returning "no jobs" would make
    ``cron on`` claim success while doing nothing after a CLI layout change.
    """
    jobs: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        header = _JOB_LINE.match(line)
        if header:
            current = {
                "id": header.group("id"),
                "name": "",
                "enabled": header.group("state") == "active",
                "deliver": LOCAL,
                "attached": False,
            }
            jobs.append(current)
            continue
        if current is None:
            continue
        detail = _DETAIL_LINE.match(line)
        if not detail:
            continue
        label = detail.group("label").strip().lower()
        value = detail.group("value").strip()
        if label == "name" and value:
            current["name"] = value
        elif label == "deliver":
            current["deliver"] = deliver_to_string(parse_deliver(value))

    if not jobs and text.strip() and "no scheduled jobs" not in text.lower():
        raise RuntimeError(
            "could not read `hermes cron list` output; this Hermes version may "
            "have changed its layout"
        )
    for job in jobs:
        job["name"] = job["name"] or job["id"]
        job["attached"] = is_attached(job["deliver"])
    return jobs


def list_jobs(home: Path | None = None) -> list[dict[str, Any]]:
    """Current jobs with their attach state, read through ``hermes cron list``."""
    return parse_jobs(_ANSI.sub("", _run_hermes(["cron", "list", "--all"], home=home)))


def apply_jobs(
    action: str, chat_id: str, *, dry_run: bool = False, home: Path | None = None
) -> list[dict[str, Any]]:
    """Attach (``on``) or detach (``off``) the speaker on every job of a profile.

    Each changed job is updated with its own ``hermes cron edit`` call, so the
    scheduler's store lock stays Hermes's business. ``dry_run`` only computes
    the commands (and never runs them), which the CLI prints.
    """
    if action not in ("on", "off"):
        raise ValueError(f"unknown cron action {action!r}")
    rows: list[dict[str, Any]] = []
    for job in list_jobs(home=home):
        before = job.get("deliver", LOCAL)
        after, changed = attach(before, chat_id) if action == "on" else detach(before)
        command = edit_command(job.get("id", ""), after) if changed else ""
        row = {
            "id": job.get("id", ""),
            "name": job.get("name", "") or job.get("id", ""),
            "enabled": bool(job.get("enabled", True)),
            "before": deliver_to_string(parse_deliver(before)),
            "after": after,
            "changed": changed,
            "command": command,
        }
        rows.append(row)
        if changed and not dry_run:
            edit_deliver(job.get("id", ""), after, home=home)
    return rows


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
