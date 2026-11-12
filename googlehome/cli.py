"""``hermes googlehome`` — command-line checks for the speaker bridge.

Exists so the bridge is diagnosable without Hermes Desktop and without the
gateway: bring the speaker online, run ``hermes googlehome check``, and it tells
you what it found, which local address it would serve audio from, and whether it
could actually make the device talk.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import castdevice, discovery
from . import config as ghconfig
from . import cronlink, speech
from .audioserver import MediaServer, content_type_for

logger = logging.getLogger(__name__)

FIREWALL_RULE = "Hermes Google Home media"


def _settings() -> Any:
    """Resolve settings without importing the gateway-side adapter."""
    return ghconfig.resolve_settings(None)


def _speak(text: str) -> dict[str, Any]:
    from .tools import _speak as speak_impl

    return speak_impl(text)


# ── argument parsing ─────────────────────────────────────────────────────────
def setup_cli(parser: Any) -> None:
    actions = parser.add_subparsers(dest="googlehome_action")

    check = actions.add_parser("check", help="find the speaker and verify it can play audio")
    check.add_argument("--json", action="store_true", help="machine-readable output")
    check.add_argument("--speak", metavar="TEXT", default="", help="say TEXT if the device works")

    devices = actions.add_parser("devices", help="list Google Cast devices on this network")
    devices.add_argument("--json", action="store_true", help="machine-readable output")
    devices.add_argument("--timeout", type=float, default=4.0, help="discovery window in seconds")

    speak = actions.add_parser("speak", help="say something on the speaker")
    speak.add_argument("text", nargs="+", help="words to speak")

    notify = actions.add_parser(
        "notify",
        help="announce a one-line task outcome (done, blocked, or failed)",
    )
    notify.add_argument(
        "--status", choices=("done", "blocked", "failed"), default="done",
        help="outcome to announce (default: done)",
    )
    notify.add_argument("--task", default="", help="short label for the task")
    notify.add_argument("--detail", default="", help="optional one-line detail")
    notify.add_argument(
        "--speaker", default="",
        help="speaker name or IP (default: the configured speaker)",
    )
    notify.add_argument("--json", action="store_true", help="machine-readable output")

    actions.add_parser("status", help="read the running bridge through its control API")

    cron = actions.add_parser(
        "cron",
        help="add the speaker to cron job output (on, off, or status)",
    )
    cron.add_argument(
        "state", nargs="?", choices=("on", "off", "status", "test"), default="status",
        help="attach the speaker to every job, detach it, show the wiring, or speak a test",
    )
    cron.add_argument(
        "--chat-id", default="",
        help="override the speaker chat id used in deliver targets",
    )
    cron.add_argument(
        "--cron-profile", default="",
        help="profile whose cron jobs to change (default: the current profile)",
    )
    cron.add_argument(
        "--dry-run", action="store_true",
        help="show what would change without writing jobs.json",
    )
    cron.add_argument("--json", action="store_true", help="machine-readable output")

    firewall = actions.add_parser("firewall", help="open the media port (prompts for UAC)")
    firewall.add_argument("--port", type=int, default=0, help="override the media port")

    parser.set_defaults(func=run_action)


def run_action(args: Any) -> int:
    action = getattr(args, "googlehome_action", None) or "check"
    handlers = {
        "check": _check,
        "devices": _devices,
        "speak": _speak_action,
        "notify": _notify_action,
        "status": _status,
        "cron": _cron,
        "firewall": _firewall,
    }
    handler = handlers.get(action)
    if handler is None:
        print(
            f"unknown action {action!r}; try: check, devices, speak, notify, "
            "status, cron, firewall"
        )
        return 2
    return int(handler(args) or 0)


# ── actions ──────────────────────────────────────────────────────────────────
def _print_devices(devices: list[dict[str, Any]]) -> None:
    if not devices:
        print("  (none found)")
        return
    for device in devices:
        flags = "audio-only" if device.get("audio_only") else "display/video"
        print(
            f"  · {device.get('name') or '?'} — {device.get('address')}:{device.get('port', 8009)}"
            f"  [{device.get('model') or 'unknown model'}, {flags}]"
        )


def _check(args: Any) -> int:
    settings = _settings()
    configured = settings.device or "(auto)"
    print(f"Google Home bridge — device: {configured}")

    devices = discovery.discover(timeout=4.0)
    print(f"\n1. mDNS discovery: {len(devices)} Cast device(s)")
    _print_devices(devices)

    resolved = discovery.resolve_device(settings.device, timeout=3.0, enrich=True)
    host = discovery.dial_host(resolved)
    report: dict[str, Any] = {
        "configured_device": settings.device,
        "discovered": devices,
        "resolved": resolved,
        "reachable": False,
        "local_ip": "",
        "media_port": 0,
        "spoke": None,
        "error": "",
    }

    if not host:
        report["error"] = "no device"
        print(
            "\nVERDICT: the speaker is not reachable.\n"
            "  Put it on the same network as this PC (Google Home app → device → Wi-Fi)\n"
            "  or set its address:  hermes config set platforms.googlehome.extra.device <ip>"
        )
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, default=str))
        return 1

    local_ip = discovery.local_ip_for(host)
    report["local_ip"] = local_ip
    print(f"\n2. Resolved device: {resolved.get('name') or host} at {host}:{resolved.get('port', 8009)}")
    print(f"   this machine would serve audio as {local_ip or '(no route)'}")

    try:
        probed = castdevice.probe(host, int(resolved.get("port") or 8009), timeout=5.0)
        report["reachable"] = True
        report["volume"] = probed.get("volume")
        report["active_app"] = probed.get("app")
        print(f"3. Cast handshake: OK (volume {probed.get('volume')}, app {probed.get('app') or 'idle'})")
    except castdevice.CastError as exc:
        report["error"] = str(exc)
        print(f"3. Cast handshake: FAILED — {exc}")
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, default=str))
        return 1

    # Serve a tiny real audio file and actually cast it: this is the only test
    # that proves the speaker can fetch bytes from this machine.
    tone = _write_tone(ghconfig.media_dir() / "selftest.wav")
    library = MediaServer(ghconfig.media_dir(), port=settings.media_port or 0,
                          advertise_host=local_ip)
    client = castdevice.CastClient(host, int(resolved.get("port") or 8009), timeout=8.0)
    try:
        try:
            port = library.start()
            note = ""
        except OSError:
            # The running bridge already owns the pinned port. Two servers on
            # one port split the device's fetches, so this check would count
            # zero on its own server and blame a firewall that is fine.
            library.stop()
            library = MediaServer(ghconfig.media_dir(), port=0, advertise_host=local_ip)
            port = library.start()
            note = f"  (port {settings.media_port} is busy — the bridge is serving it)"
        report["media_port"] = port
        print(f"4. Media server: listening on {local_ip}:{port}{note}")
        client.connect()
        filename = library.publish(str(tone))
        result = client.play_and_wait(
            library.url_for(filename, local_ip),
            content_type=content_type_for(str(tone)),
            timeout=15.0,
            max_wait=30.0,
        )
        fetched = library.stats.get("requests", 0)
        print(f"5. Cast LOAD: {result.get('player_state') or 'sent'}; media fetches: {fetched}")
        if fetched == 0:
            report["error"] = "the device did not fetch the audio"
            print(
                "\nVERDICT: the device accepted the cast but never fetched the audio.\n"
                "  That is almost always Windows Firewall. Open the port:\n"
                f"    hermes googlehome firewall --port {port}"
            )
            if getattr(args, "json", False):
                print(json.dumps(report, indent=2, default=str))
            return 1
    except castdevice.CastError as exc:
        report["error"] = str(exc)
        print(f"   playback failed: {exc}")
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, default=str))
        return 1
    finally:
        client.close()
        library.stop()

    if getattr(args, "speak", ""):
        report["spoke"] = _speak(args.speak)
        print(f"6. speech: {report['spoke']}")

    print("\nVERDICT: the bridge works — the speaker plays audio from this machine.")
    if not settings.profile:
        print("  Bound profile: (gateway default). Pick one in Desktop → Google Home, or:")
        print("    hermes config set platforms.googlehome.extra.profile <name>")
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2, default=str))
    return 0


def _write_tone(path):
    """A short two-note chime, generated with the stdlib."""
    import math
    import struct
    import wave

    rate = 16000
    frames = bytearray()
    for note_hz, seconds in ((660.0, 0.12), (880.0, 0.16)):
        count = int(rate * seconds)
        for index in range(count):
            envelope = min(1.0, index / 400.0, (count - index) / 400.0)
            sample = int(11000 * envelope * math.sin(2 * math.pi * note_hz * index / rate))
            frames += struct.pack("<h", sample)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


def _devices(args: Any) -> int:
    devices = discovery.discover(timeout=float(getattr(args, "timeout", 4.0)))
    if getattr(args, "json", False):
        print(json.dumps(devices, indent=2, default=str))
    else:
        print(f"{len(devices)} Cast device(s) on this network:")
        _print_devices(devices)
        if not devices:
            print(
                "\n  If the speaker is on, its network may isolate clients, or this PC has\n"
                "  several adapters and mDNS is leaving the wrong one. Try the IP directly:\n"
                "    hermes config set platforms.googlehome.extra.device 192.168.x.y"
            )
    return 0 if devices else 1


def _speak_action(args: Any) -> int:
    result = _speak(" ".join(args.text))
    print(json.dumps(result, indent=2, default=str))
    return 0 if result.get("ok") else 1


def _notify_action(args: Any) -> int:
    from . import notify

    try:
        result = notify.announce(
            args.status,
            task=args.task,
            detail=args.detail,
            speaker=args.speaker,
            settings=_settings(),
        )
    except notify.SpeakerLookupError as exc:
        result = {"ok": False, "error": str(exc), "available_speakers": exc.available}
    except ValueError as exc:
        result = {"ok": False, "error": str(exc)}

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
    elif result.get("ok"):
        print(result.get("spoken") or "")
        print(
            f"  spoken on {result.get('device') or result.get('address')} "
            f"({result.get('via') or 'cast'})"
        )
    else:
        print(f"Notification failed: {result.get('error') or 'unknown error'}")
        for name in result.get("available_speakers") or []:
            print(f"  available: {name}")
    return 0 if result.get("ok") else 1


def _status(args: Any) -> int:
    info = _read_control()
    if not info or not info.get("port"):
        print("The bridge is not running. Start the gateway: hermes gateway restart")
        return 1
    import urllib.request

    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/status",
        headers={"X-Hermes-Token": info.get("token", "")} if info.get("token") else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            print(json.dumps(json.loads(response.read().decode("utf-8")), indent=2, default=str))
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach the bridge on port {info['port']}: {exc}")
        return 1
    return 0


def _read_control() -> dict[str, Any]:
    try:
        return json.loads((ghconfig.state_dir() / "control.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ── cron wiring ──────────────────────────────────────────────────────────────
def _cron(args: Any) -> int:
    """``hermes googlehome cron [on|off|status]`` — speaker as a cron target."""
    settings = _settings()
    chat_id = cronlink.resolve_chat_id(settings.chat_id, getattr(args, "chat_id", ""))
    state = getattr(args, "state", None) or "status"
    as_json = bool(getattr(args, "json", False))
    dry_run = bool(getattr(args, "dry_run", False))

    try:
        home = _cron_profile_home(getattr(args, "cron_profile", ""))
        label = cronlink.profile_label(home)
        if state == "status":
            return _cron_status(chat_id, settings, as_json, home=home, label=label)
        if state == "test":
            return _cron_test(chat_id, as_json)
        return _cron_toggle(state, chat_id, settings, as_json, dry_run=dry_run,
                            home=home, label=label)
    except ValueError as exc:
        print(str(exc))
        return 2
    except RuntimeError as exc:
        print(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - a CLI failure should be readable
        logger.debug("cron wiring failed", exc_info=True)
        print(f"could not update cron jobs: {exc}")
        return 1


def _cron_profile_home(name: str):
    """Hermes home for ``--profile``, or None for the profile this process serves."""
    name = (name or "").strip()
    if not name:
        return None
    try:
        from hermes_constants import (
            get_default_hermes_root,
            named_profile_home,
            named_profile_is_live,
        )
    except Exception as exc:  # noqa: BLE001 - stdlib-only installs have no profiles
        raise RuntimeError(
            "this Hermes build cannot address another profile; drop --cron-profile"
        ) from exc
    home = named_profile_home(Path(get_default_hermes_root()) / "profiles" / name)
    if home is None or not named_profile_is_live(home):
        raise ValueError(f"no Hermes profile named {name!r}")
    return home


def _cron_test(chat_id: str, as_json: bool) -> int:
    """Speak a synthetic cron payload through the same digest/bridge path."""
    from .standalone import send_standalone

    payload_text = (
        "Cronjob Response: Google Home test\n"
        "(job_id: selftest)\n"
        "-------------\n\n"
        "This is a cron delivery test. If you can hear this, scheduled jobs "
        "will speak on this speaker.\n\n"
        "To stop or manage this job, send me a new message "
        "(e.g. \"stop reminder Google Home test\")."
    )
    result = asyncio.run(send_standalone(None, chat_id, payload_text))
    ok = bool(result.get("success"))
    report = {"target": f"googlehome:{chat_id}", "success": ok, "result": result}
    if as_json:
        print(json.dumps(report, indent=2, default=str))
        return 0 if ok else 1
    if ok:
        print("Cron delivery test spoken on the Google Home speaker.")
        print("If you heard it, a scheduled job with this target will speak too.")
        return 0
    print(f"Cron delivery test failed: {result.get('error') or 'unknown error'}")
    print("Check `hermes googlehome check` and `hermes googlehome cron status`.")
    return 1


def _cron_status(chat_id: str, settings: Any, as_json: bool, *, home: Any, label: str) -> int:
    ready = cronlink.platform_ready()
    jobs = cronlink.list_jobs(home=home)
    attached = [job for job in jobs if job["attached"]]
    payload = {
        "profile": label,
        "target": f"googlehome:{chat_id}",
        "cron_platform_registered": ready,
        "speak_cron": settings.speak_cron,
        "cron_max_chars": settings.cron_max_chars,
        "jobs_total": len(jobs),
        "jobs_attached": len(attached),
        "jobs": jobs,
    }
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    print(f"Google Home cron notifications — profile: {label}")
    print(f"  target:    {payload['target']}")
    print(
        "  platform:  "
        + ("registered for cron delivery" if ready
           else "NOT registered — the installed plugin is older, or the gateway needs a restart")
    )
    print(
        f"  speech:    {'on' if settings.speak_cron else 'off'}, "
        f"{settings.cron_max_chars} chars per job"
    )
    if not jobs:
        print("  jobs:      none in this profile")
        return 0

    print(f"  jobs:      {len(jobs)} total, {len(attached)} attached")
    for job in jobs:
        mark = "speaker" if job["attached"] else "       "
        state = "" if job["enabled"] else " (paused)"
        print(f"  · [{mark}] {job['name']}{state}")
        print(f"      deliver: {job['deliver']}")
    return 0


def _cron_toggle(
    state: str, chat_id: str, settings: Any, as_json: bool, *, dry_run: bool,
    home: Any, label: str,
) -> int:
    rows = cronlink.apply_jobs(state, chat_id, dry_run=dry_run, home=home)
    changed = [row for row in rows if row["changed"]]
    payload = {
        "action": state,
        "dry_run": dry_run,
        "profile": label,
        "target": f"googlehome:{chat_id}",
        "jobs_total": len(rows),
        "jobs_changed": len(changed),
        "jobs": rows,
    }
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    verb = "attach" if state == "on" else "detach"
    if not rows:
        print(f"No cron jobs in profile '{label}' (nothing to {verb}).")
        return 0

    if dry_run:
        print(f"Dry run — would {verb} the speaker on profile '{label}':")
    else:
        print(f"{'Attached' if state == 'on' else 'Detached'} the speaker on profile '{label}':")
    print(f"  target: googlehome:{chat_id}\n")

    for row in rows:
        if row["changed"]:
            print(f"  · {row['name']}")
            print(f"      {row['before']}  →  {row['after']}")
            if dry_run and row.get("command"):
                print(f"      {row['command']}")
        else:
            print(f"  · {row['name']}  (already {'attached' if state == 'on' else 'clear'})")

    if changed:
        print(
            f"\n{len(changed)} of {len(rows)} job(s) "
            f"{'would change' if dry_run else 'updated'}."
        )
    else:
        print(f"\nNothing to change; all {len(rows)} job(s) are already in the right state.")

    if dry_run and changed:
        print(
            "\nDry run — nothing was written. Run the commands above, or drop\n"
            "--dry-run to have this command run them."
        )
    if state == "on" and not dry_run:
        print(
            "The next scheduler tick reads the new delivery list (no gateway restart needed).\n"
            "Test a job now:  hermes cron run <job-id>"
        )
    if state == "off" and not dry_run:
        local_only = [row for row in changed if row["after"] == "local"]
        if local_only:
            print(
                f"\n{len(local_only)} job(s) now have no delivery target and will only save "
                "output locally. Add a text target with:\n"
                "  hermes cron edit <job-id> --deliver <platform>"
            )
    if state == "on" and not settings.speak_cron:
        print(
            "\nHeads-up: speak_cron is off in this profile's plugin settings, so the jobs will "
            "deliver to the speaker but stay silent.\n"
            "  hermes config set plugins.entries.googlehome.settings.speak_cron true"
        )
    return 0


def _firewall(args: Any) -> int:
    settings = _settings()
    port = int(getattr(args, "port", 0) or settings.media_port or 8766)
    if _firewall_rule_exists(port):
        print(f"Firewall rule for TCP {port} already exists.")
        return 0

    command = (
        f'netsh advfirewall firewall add rule name="{FIREWALL_RULE} {port}" '
        f"dir=in action=allow protocol=TCP localport={port}"
    )
    print(f"Adding a Windows Firewall rule for inbound TCP {port} (needs elevation)…")
    if not _run_elevated(command):
        print(
            "\nElevation was declined or unavailable. Run this in an elevated terminal:\n"
            f"  {command}"
        )
        return 1
    if _firewall_rule_exists(port):
        print(f"Rule added. The speaker can now fetch audio on TCP {port}.")
        suggestion = f"hermes config set platforms.googlehome.extra.media_port {port}"
        print(f"  Pin the same port so it is stable:  {suggestion}")
        return 0
    print("The rule was not visible after adding it. Check Windows Defender Firewall.")
    return 1


def _firewall_rule_exists(port: int) -> bool:
    script = (
        f"Get-NetFirewallRule -DisplayName '*{FIREWALL_RULE}*' -ErrorAction SilentlyContinue | "
        f"Where-Object {{ $_.Enabled -eq 'True' }} | Out-String"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=25,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = result.stdout or ""
    return f"localport={port}" in text.lower() or "localport" in text.lower() and bool(text.strip())


def _run_elevated(command: str) -> bool:
    """Run a command in an elevated shell (triggers UAC)."""
    if sys.platform != "win32":
        return False
    import tempfile
    from pathlib import Path

    script = Path(tempfile.gettempdir()) / "hermes-googlehome-firewall.ps1"
    try:
        script.write_text(command + "\nStart-Sleep -Seconds 1\n", encoding="utf-8")
    except OSError:
        return False
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Start-Process powershell -Verb RunAs -Wait -ArgumentList "
             f"'-NoProfile','-ExecutionPolicy','Bypass','-File','{script}'"],
            capture_output=True, text=True, timeout=120,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
