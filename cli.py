"""``hermes googlehome`` — command-line checks for the speaker bridge.

Exists so the bridge is diagnosable without Hermes Desktop and without the
gateway: bring the speaker online, run ``hermes googlehome check``, and it tells
you what it found, which local address it would serve audio from, and whether it
could actually make the device talk.
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

from . import castdevice, discovery
from . import config as ghconfig
from . import speech
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

    actions.add_parser("status", help="read the running bridge through its control API")

    firewall = actions.add_parser("firewall", help="open the media port (prompts for UAC)")
    firewall.add_argument("--port", type=int, default=0, help="override the media port")

    parser.set_defaults(func=run_action)


def run_action(args: Any) -> int:
    action = getattr(args, "googlehome_action", None) or "check"
    handlers = {
        "check": _check,
        "devices": _devices,
        "speak": _speak_action,
        "status": _status,
        "firewall": _firewall,
    }
    handler = handlers.get(action)
    if handler is None:
        print(f"unknown action {action!r}; try: check, devices, speak, status, firewall")
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

    resolved = discovery.resolve_device(settings.device, timeout=3.0)
    host = resolved.get("host") or resolved.get("address")
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
        port = library.start()
        report["media_port"] = port
        print(f"4. Media server: listening on {local_ip}:{port}")
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
