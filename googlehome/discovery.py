"""Finding the speaker: mDNS, eureka metadata, and which local NIC to advertise.

Two jobs:

1. Turn a configured device string (``""``, an IP, a hostname, or a friendly
   name like "Living Room speaker") into a reachable ``ip:port``.
2. Work out which local IP address the speaker can reach *us* on. This machine
   is dual-homed on the same /24 (Ethernet + Wi-Fi), so "pick any interface"
   silently advertises an address the speaker may need to route back to. The
   UDP-connect trick asks the OS routing table for the right source address.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import ssl
import struct
import threading
import time
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

CAST_SERVICE = "_googlecast._tcp.local"
MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353
CAST_PORT = 8009
EUREKA_PORT = 8443

T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33

_discovery_lock = threading.Lock()


# ── DNS primitives ───────────────────────────────────────────────────────────
def _encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna") if label else b""
        if len(raw) > 63:
            raise ValueError(f"mDNS label too long: {label}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def _read_name(buf: bytes, off: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    after = off
    hops = 0
    while True:
        if off >= len(buf):
            raise ValueError("truncated DNS name")
        length = buf[off]
        if length == 0:
            off += 1
            if not jumped:
                after = off
            return ".".join(labels), after
        if length & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                after = off + 2
            off = pointer
            jumped = True
            hops += 1
            if hops > 64:
                raise ValueError("DNS pointer loop")
            continue
        off += 1
        labels.append(buf[off:off + length].decode("utf-8", "replace"))
        off += length


def _build_query() -> bytes:
    header = struct.pack(">HHHHHH", 0, 0x0000, 1, 0, 0, 0)
    question = _encode_name(CAST_SERVICE) + struct.pack(">HH", T_PTR, 0x8001)
    return header + question


def _parse_response(buf: bytes) -> list[dict[str, Any]]:
    _id, _flags, questions, answers, _ns, additional = struct.unpack(">HHHHHH", buf[:12])
    off = 12
    for _ in range(questions):
        _, off = _read_name(buf, off)
        off += 4
    records: list[dict[str, Any]] = []
    for _ in range(answers + additional):
        try:
            name, off = _read_name(buf, off)
            if off + 10 > len(buf):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
            off += 10
            rdata_off = off
            rdata = buf[off:off + rdlen]
            off += rdlen
            record: dict[str, Any] = {"name": name, "type": rtype}
            if rtype == T_PTR:
                record["value"], _ = _read_name(buf, rdata_off)
            elif rtype == T_SRV:
                _prio, _weight, port = struct.unpack(">HHH", rdata[:6])
                host, _ = _read_name(buf, rdata_off + 6)
                record.update({"port": port, "value": host})
            elif rtype == T_TXT:
                items: dict[str, str] = {}
                i = 0
                while i < len(rdata):
                    ln = rdata[i]
                    i += 1
                    chunk = rdata[i:i + ln].decode("utf-8", "replace")
                    i += ln
                    if "=" in chunk:
                        key, value = chunk.split("=", 1)
                        items[key] = value
                record["txt"] = items
            elif rtype == T_A and len(rdata) == 4:
                record["value"] = socket.inet_ntoa(rdata)
            records.append(record)
        except (ValueError, struct.error):
            break
    return records


# ── discovery ────────────────────────────────────────────────────────────────
def local_ipv4_addresses() -> list[str]:
    """Every non-loopback IPv4 address this host owns."""
    addresses: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    try:  # routing table view: one address per route, catches secondary NICs
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 53))
            addresses.add(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith("127."))


def local_ip_for(peer_ip: str) -> str:
    """The local address the OS would use to talk to ``peer_ip``."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect((peer_ip, CAST_PORT))
            return probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        return ""


def _query_interface(iface_ip: str, timeout: float) -> list[dict[str, Any]]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
        sock.bind(("", 0))
        sock.settimeout(0.3)
        sock.sendto(_build_query(), (MDNS_ADDR, MDNS_PORT))

        devices: dict[str, dict[str, Any]] = {}
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data, (source_ip, _src_port) = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                records = _parse_response(data)
            except Exception:  # malformed packet from an unrelated responder
                continue
            for record in records:
                if record["type"] == T_SRV:
                    entry = devices.setdefault(record["name"], {"service": record["name"],
                                                                "address": source_ip})
                    entry.update({"port": record["port"], "host": record["value"]})
                elif record["type"] == T_TXT:
                    entry = devices.setdefault(record["name"], {"service": record["name"],
                                                                "address": source_ip})
                    entry["txt"] = record.get("txt", {})
                elif record["type"] == T_A:
                    for entry in devices.values():
                        if entry.get("host") == record["name"]:
                            entry["address"] = record["value"]
        return list(devices.values())
    finally:
        sock.close()


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Lift the interesting identity out of the TXT blob into top-level keys.

    ``_query_interface`` returns raw records; everything downstream (naming,
    audio-only selection, display) expects ``name``/``model``/``audio_only`` on
    the dict itself, so there is exactly one place that does this.
    """
    device = dict(raw)
    txt = device.get("txt") or {}
    device.setdefault("id", "")
    device["id"] = device["id"] or txt.get("id", "")
    device["name"] = device.get("name") or txt.get("fn", "")
    device["model"] = device.get("model") or txt.get("md", "")
    if "capabilities" not in device:
        try:
            # ca is a capability bitmask: bit 2 (value 4) = AUDIO_OUT only.
            # Real firmware also sets high undocumented bits (a Home Mini
            # reports 199172), so this is a mask test, never equality.
            device["capabilities"] = int(txt.get("ca", "0"))
        except (TypeError, ValueError):
            device["capabilities"] = 0
    capabilities = device["capabilities"]
    device["audio_only"] = device.get("audio_only", bool(capabilities & 4) and not bool(capabilities & 1))
    device["address"] = device.get("address") or device.get("host", "")
    device["port"] = device.get("port") or CAST_PORT
    return device


def is_cast_record(raw: dict[str, Any]) -> bool:
    """True for a genuine Cast advertisement (not an unrelated mDNS answer)."""
    txt = raw.get("txt") or {}
    if txt.get("id"):
        return True
    return str(raw.get("service", "")).lower().endswith(CAST_SERVICE)


def discover(timeout: float = 3.0) -> list[dict[str, Any]]:
    """mDNS-discover Cast devices, querying every local interface concurrently.

    Querying interfaces one after another multiplies the wall-clock cost by the
    number of adapters (this host has four), which is long enough to blow a
    status request's timeout. All interfaces share one discovery window instead.
    """
    with _discovery_lock:
        interfaces = local_ipv4_addresses() or [""]
        results: list[dict[str, Any]] = []
        lock = threading.Lock()

        def worker(iface_ip: str) -> None:
            try:
                found = _query_interface(iface_ip, timeout)
            except OSError as exc:
                logger.debug("mDNS probe on %s failed: %s", iface_ip, exc)
                return
            with lock:
                for device in found:
                    device["via_interface"] = iface_ip
                    results.append(device)

        threads = [threading.Thread(target=worker, args=(ip,), daemon=True) for ip in interfaces]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout + 1.0
        for thread in threads:
            thread.join(timeout=max(0.1, deadline - time.monotonic()))

        merged: dict[str, dict[str, Any]] = {}
        for device in results:
            if not is_cast_record(device):
                continue
            device = normalize(device)
            key = device["id"] or device.get("service", "")
            if not key:
                continue
            merged[key] = device
        return list(merged.values())


def describe(device: dict[str, Any]) -> str:
    device = normalize(device or {})
    name = device.get("name") or device.get("address") or "Cast device"
    model = device.get("model") or ""
    return f"{name} ({model})".strip() if model else name


def display_name(device: dict[str, Any]) -> str:
    """What the speaker *is*: its model, else its friendly name, else its address.

    The friendly name is whatever the owner typed in the Google Home app
    ("Living Room speaker"), which is not the same thing as the hardware. A
    bound device that was resolved from a bare IP has neither until
    ``identify()`` fills them in; it reports its address until then.
    """
    device = normalize(device or {})
    model = str(device.get("model") or "").strip()
    if model:
        return model
    name = str(device.get("name") or "").strip()
    address = str(device.get("address") or device.get("host") or "").strip()
    if name and name != address:
        return name
    return address or "Cast device"


def display_label(device: dict[str, Any]) -> str:
    """``display_name`` with the address in brackets: "Google Home Mini (10.0.0.42)".

    The address is omitted when it would just repeat the name, so an
    unidentified device reads as "10.0.0.42", not "10.0.0.42 (10.0.0.42)".
    """
    device = normalize(device or {})
    name = display_name(device)
    address = str(device.get("address") or device.get("host") or "").strip()
    return f"{name} ({address})" if address and address not in name else name


# ── device identity ──────────────────────────────────────────────────────────
def eureka_info(host: str, timeout: float = 3.0) -> dict[str, Any]:
    """Read the unauthenticated setup API (port 8443) for model/firmware identity."""
    url = (
        f"https://{host}:{EUREKA_PORT}/setup/eureka_info"
        "?params=device_info,name,build_info,settings"
    )
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    request = urllib.request.Request(url, headers={"User-Agent": "hermes-googlehome"})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - identity is best-effort
        logger.debug("eureka_info(%s) failed: %s", host, exc)
        return {}


def identify(host: str, timeout: float = 2.0) -> dict[str, str]:
    """Ask the speaker itself what it is: ``{name, model}``, best effort.

    mDNS TXT carries the same two fields (``fn``/``md``), but only for a device
    that answered a multicast query. This unicast lookup also works when the
    speaker is pinned by IP or the network drops multicast, which are exactly
    the cases where a bound device would otherwise be named by its address.
    """
    info = eureka_info(host, timeout=timeout)
    if not info:
        return {}
    device_info = info.get("device_info") or {}
    identity = {
        "name": str(info.get("name") or "").strip(),
        "model": str(device_info.get("model_name") or "").strip(),
    }
    return {key: value for key, value in identity.items() if value}


def _enriched(device: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Fill in the identity of a device the discovery answer did not describe."""
    if not enabled or not device or device.get("model"):
        return device
    host = str(device.get("address") or device.get("host") or "").strip()
    if not host:
        return device
    identity = identify(host)
    if not identity:
        return device
    enriched = dict(device)
    enriched["name"] = identity.get("name") or enriched.get("name", "")
    enriched["model"] = identity.get("model") or enriched.get("model", "")
    return enriched


def resolve_device(
    spec: str,
    *,
    timeout: float = 3.0,
    enrich: bool = False,
) -> dict[str, Any]:
    """Resolve a config string to ``{host, port, name, model, address}``.

    ``spec`` may be empty (pick the first audio-only Cast device), an IP, a
    hostname, or a friendly name matched against the mDNS ``fn`` field.

    ``enrich`` asks the device's setup API for its model/friendly name when the
    fast path (an IP literal dialled without discovery) did not learn them.
    That is one extra ~100 ms request, so callers that only need a dial target
    leave it off.
    """
    spec = (spec or "").strip()
    if spec:
        try:
            ipaddress.ip_address(spec)
            return _enriched(
                {"host": spec, "port": CAST_PORT, "name": spec, "model": "", "address": spec},
                enrich,
            )
        except ValueError:
            pass

    devices = [normalize(device) for device in discover(timeout=timeout)]
    if not devices:
        if spec:
            return _enriched(
                {"host": spec, "port": CAST_PORT, "name": spec, "model": "", "address": spec},
                enrich,
            )
        return {}

    if spec:
        lowered = spec.lower()
        for device in devices:
            haystack = " ".join(
                str(device.get(field, "")) for field in ("name", "model", "host", "address")
            ).lower()
            if lowered in haystack:
                return _enriched(device, enrich)
        logger.warning("no Cast device matched %r; using %s", spec, describe(devices[0]))
    audio_only = [d for d in devices if d.get("audio_only")]
    return _enriched((audio_only or devices)[0], enrich)


def dial_host(device: dict[str, Any]) -> str:
    """The address to open a socket to: an IP literal wins over a hostname.

    A Cast SRV target is ``<uuid>.local``, and a Windows host has no resolver
    for that name, so dialling the hostname fails even though the same mDNS
    answer carried the A record with the address we actually need.
    """
    address = str(device.get("address") or "").strip()
    try:
        ipaddress.ip_address(address)
        return address
    except ValueError:
        return str(device.get("host") or address).strip()
