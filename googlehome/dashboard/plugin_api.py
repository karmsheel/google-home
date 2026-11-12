"""Google Home backend for the Desktop extension.

The bridge lives in the gateway process on this machine and speaks JSON over a
token-gated loopback port recorded in ``<HERMES_HOME>/googlehome/control.json``.
The Desktop renderer must not read that file or hold that token — a plugin is
not allowed to reach into Electron's native bridge for it, and a remote Desktop
cannot read the gateway's disk at all. So the renderer calls
``ctx.rest('/status')`` and this router forwards to the bridge:

    GET  /status  /profiles  /playback  /devices  /info
    GET  /reply?token=…  /destination?session=…
    POST /ask  /speak  /stop  /listen  /device  /profile  /volume  /mute
         /destination  /refresh

Every route resolves the bridge at request time (the plugin host scopes each
request to the viewing profile, while the adapter may live under another), and
nothing is cached beyond a couple of seconds. The control API's own token never
leaves this process.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from http import client as http_client
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

_log = logging.getLogger(__name__)
router = APIRouter()

#: Verbs the control API serves. Anything else is refused instead of forwarded.
_GET_VERBS = frozenset({"status", "profiles", "playback", "devices", "info", "reply", "destination"})
_POST_VERBS = frozenset({
    "ask", "speak", "stop", "listen", "device", "profile", "volume", "mute",
    "destination", "refresh",
})
#: A turn may wait on STT, an approval or a long TTS clip; a status poll may not.
_TIMEOUTS = {"ask": 200.0, "listen": 300.0, "speak": 300.0, "refresh": 60.0}
_DEFAULT_TIMEOUT = 30.0
#: ``/playback`` is polled about once a second; do not stat every profile each
#: time. The copy is still re-read well before a restarted bridge matters.
_DISCOVERY_TTL = 2.0
_CACHE: dict[str, Any] = {"at": 0.0, "rows": []}


def _homes() -> list[Path]:
    """Homes whose ``googlehome/control.json`` could belong to the bridge."""
    try:
        from hermes_constants import get_default_hermes_root, get_hermes_home

        root = Path(get_default_hermes_root())
        homes = [Path(get_hermes_home()), root]
        try:
            homes.extend(child for child in sorted((root / "profiles").iterdir()) if child.is_dir())
        except OSError:
            pass
        return homes
    except Exception:  # noqa: BLE001 - a host without core cannot serve the bridge
        _log.debug("googlehome backend: hermes_constants unavailable", exc_info=True)
        return []


def _bridge_rows() -> list[dict[str, Any]]:
    """Live-looking control files, newest first.

    ``updated_at`` is written whenever the adapter (re)writes the file, so the
    freshest one is tried first; a stale entry that cannot be reached falls
    through to the next. Nothing is trusted as "the" bridge until it answers.
    """
    now = time.time()
    cached = _CACHE["rows"]
    if cached and now - _CACHE["at"] < _DISCOVERY_TTL:
        return cached
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for home in _homes():
        path = home / "googlehome" / "control.json"
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or not data.get("port"):
            continue
        rows.append(data)
    rows.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    _CACHE.update({"at": now, "rows": rows})
    return rows


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """The bridge's own error sentence, for the status FastAPI relays."""
    try:
        raw = exc.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a broken error body is not worth an exception
        raw = ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw.strip()[:300] or f"the bridge answered HTTP {exc.code}"
    if isinstance(data, dict):
        for key in ("error", "detail"):
            if data.get(key):
                return str(data[key])
    return raw.strip()[:300] or f"the bridge answered HTTP {exc.code}"


def _call(verb: str, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    """Forward one request to the first bridge that answers."""
    rows = _bridge_rows()
    if not rows:
        raise HTTPException(
            status_code=503,
            detail=(
                "The Google Home bridge is not running. Start the Hermes gateway that "
                "hosts the googlehome plugin, then retry."
            ),
        )
    timeout = _TIMEOUTS.get(verb, _DEFAULT_TIMEOUT)
    last_error = ""
    for index, info in enumerate(rows):
        port = int(info.get("port") or 0)
        if not port:
            continue
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}", data=payload, method=method
        )
        if payload is not None:
            request.add_header("Content-Type", "application/json")
        token = str(info.get("token") or "")
        if token:
            request.add_header("X-Hermes-Token", token)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {"ok": True}
        except urllib.error.HTTPError as exc:
            # 401/403 means this candidate is a different bridge (or an old
            # token); another profile's file may still answer correctly.
            if exc.code in (401, 403) and index + 1 < len(rows):
                last_error = _error_detail(exc)
                continue
            raise HTTPException(status_code=exc.code, detail=_error_detail(exc)) from exc
        except (urllib.error.URLError, OSError, ValueError, http_client.HTTPException) as exc:
            # Connection refused, a closed socket, or some other service that
            # grabbed the port: this candidate is not the bridge, try the next.
            last_error = str(exc)
            continue
    raise HTTPException(
        status_code=503,
        detail=(
            "The Google Home bridge is not reachable on its control port"
            + (f": {last_error}" if last_error else ".")
        ),
    )


@router.get("/{verb}")
async def get_verb(verb: str, request: Request):
    """GET proxy: status, playback, devices, and the polling readers."""
    if verb not in _GET_VERBS:
        raise HTTPException(status_code=404, detail=f"unknown Google Home route {verb!r}")
    query = request.url.query
    return _call(verb, "GET", f"/{verb}" + (f"?{query}" if query else ""))


@router.post("/{verb}")
async def post_verb(verb: str, request: Request):
    """POST proxy: turns, push-to-talk, and the speaker controls."""
    if verb not in _POST_VERBS:
        raise HTTPException(status_code=404, detail=f"unknown Google Home route {verb!r}")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty/!JSON body is the same as {}
        body = {}
    if not isinstance(body, dict):
        body = {}
    return _call(verb, "POST", f"/{verb}", body)
