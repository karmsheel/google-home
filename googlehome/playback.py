"""What is coming out of a speaker right now, and how to make it stop.

Every path that casts agent audio — the platform adapter speaking a reply, a
tool speaking on demand, and a Desktop conversation speaking its turn-end answer
— registers here for as long as the clip is playing. The control API asks this
registry to stop, which is what lets the Desktop cut a long reply short without
owning the Cast connection itself.

The registry is process-local and stdlib-only: the gateway's adapter, the tools
and the loopback control server all live in one process, and importing this
module during plugin registration must never pull in Hermes core.

One clip can be requested from several places at once (the bridge adapter and a
conversation pointed at another speaker), so the registry holds every active
playback. ``stop()`` asks all of them to stop; the UI shows one "stop the
speaker" control because from the user's side the room is simply talking.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional

_lock = threading.RLock()
_active: Dict[int, "Playback"] = {}
_next_token = 0


class Playback:
    """One registered clip: its stop hook plus the details a status line needs."""

    __slots__ = ("token", "source", "device", "started_at", "stopped", "ended", "_hook")

    def __init__(
        self,
        token: int,
        hook: Callable[[], Any],
        source: str,
        device: str,
    ) -> None:
        self.token = token
        self.source = source
        self.device = device
        self.started_at = time.time()
        self.stopped = threading.Event()
        self.ended = False
        self._hook = hook

    def stop(self) -> bool:
        """Ask the player to stop. Returns whether it says it did."""
        self.stopped.set()
        return bool(self._hook())


def begin(
    stop: Callable[[], Any],
    *,
    source: str = "reply",
    device: str = "",
) -> Playback:
    """Register a clip that is now playing; ``stop`` interrupts it."""
    global _next_token
    with _lock:
        _next_token += 1
        handle = Playback(_next_token, stop, str(source or ""), str(device or ""))
        _active[handle.token] = handle
        return handle


def end(handle: Optional[Playback]) -> None:
    """Unregister a clip. Safe to call after ``stop()`` already removed it."""
    if handle is None:
        return
    with _lock:
        handle.ended = True
        _active.pop(handle.token, None)


def is_speaking() -> bool:
    with _lock:
        return bool(_active)


def snapshot() -> Dict[str, Any]:
    """A small, cheap status payload; no Cast round trip is involved."""
    with _lock:
        handles = list(_active.values())
    if not handles:
        return {
            "speaking": False,
            "count": 0,
            "sources": [],
            "devices": [],
            "started_at": 0.0,
            "elapsed_seconds": 0.0,
        }
    now = time.time()
    started = min(handle.started_at for handle in handles)
    return {
        "speaking": True,
        "count": len(handles),
        "sources": sorted({handle.source for handle in handles if handle.source}),
        "devices": sorted({handle.device for handle in handles if handle.device}),
        "started_at": round(started, 3),
        "elapsed_seconds": round(max(0.0, now - started), 1),
    }


def stop() -> Dict[str, Any]:
    """Stop every registered clip. Never raises; reports per-clip failures."""
    # Claim the whole set under the lock: a second click (or a second poll)
    # must not send a duplicate STOP for a clip the first click is already
    # stopping. Failed clips are put back so the control can be retried.
    with _lock:
        handles = list(_active.values())
        for handle in handles:
            _active.pop(handle.token, None)
    if not handles:
        return {"ok": True, "stopped": 0, "count": 0, "errors": []}

    stopped = 0
    errors: List[str] = []
    for handle in handles:
        try:
            if handle.stop():
                stopped += 1
            else:
                errors.append(f"{handle.source or 'playback'} did not report a stop")
                with _lock:
                    if not handle.ended:
                        _active[handle.token] = handle
        except Exception as exc:  # noqa: BLE001 - the control UI gets the reason
            errors.append(str(exc) or exc.__class__.__name__)
            with _lock:
                if not handle.ended:
                    _active[handle.token] = handle

    return {
        "ok": not errors,
        "stopped": stopped,
        "count": len(handles),
        "errors": errors,
    }


def reset() -> None:
    """Forget every clip without stopping it. Tests and bridge teardown use this."""
    with _lock:
        _active.clear()
