#!/usr/bin/env python3
"""Pure-stdlib mDNS discovery for Google Cast devices.

No third-party deps. Used standalone (tools/probe) and reused by the Hermes
plugin. Sends a one-shot PTR query for _googlecast._tcp.local with the
unicast-response (QU) bit set, so replies come back to our ephemeral port
instead of the shared 5353 group.

Usage: python mdns_cast.py [timeout_seconds]
"""
from __future__ import annotations

import socket
import struct
import sys
import time

CAST_SERVICE = "_googlecast._tcp.local"
MDNS_ADDR = "224.0.0.251"
MDNS_PORT = 5353

T_A, T_PTR, T_TXT, T_SRV = 1, 12, 16, 33


def encode_name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("idna") if label else b""
        if len(raw) > 63:
            raise ValueError(f"label too long: {label}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def read_name(buf: bytes, off: int) -> tuple[str, int]:
    """Decode a possibly-compressed DNS name. Returns (name, offset_after)."""
    labels: list[str] = []
    jumped = False
    after = off
    hops = 0
    while True:
        if off >= len(buf):
            raise ValueError("truncated name")
        length = buf[off]
        if length == 0:
            off += 1
            if not jumped:
                after = off
            break
        if length & 0xC0 == 0xC0:  # compression pointer
            if off + 1 >= len(buf):
                raise ValueError("truncated pointer")
            ptr = ((length & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                after = off + 2
            off = ptr
            jumped = True
            hops += 1
            if hops > 64:
                raise ValueError("pointer loop")
            continue
        off += 1
        labels.append(buf[off:off + length].decode("utf-8", "replace"))
        off += length
    return ".".join(labels), after


def build_query() -> bytes:
    # ID 0, standard query, 1 question.
    header = struct.pack(">HHHHHH", 0, 0x0000, 1, 0, 0, 0)
    question = encode_name(CAST_SERVICE) + struct.pack(">HH", T_PTR, 0x8001)  # QU bit
    return header + question


def parse_response(buf: bytes) -> dict:
    """Parse an mDNS response into records + a PTR->SRV/TXT/A join."""
    _id, _flags, qd, an, _ns, ar = struct.unpack(">HHHHHH", buf[:12])
    off = 12
    for _ in range(qd):
        _, off = read_name(buf, off)
        off += 4  # qtype + qclass

    records = []
    for _ in range(an + ar):
        try:
            name, off = read_name(buf, off)
            if off + 10 > len(buf):
                break
            rtype, rclass, _ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
            off += 10
            rdata_off = off
            rdata = buf[off:off + rdlen]
            off += rdlen
            rec = {"name": name, "type": rtype, "class": rclass}
            if rtype == T_PTR:
                rec["target"], _ = read_name(buf, rdata_off)
                rec["value"] = rec["target"]
            elif rtype == T_SRV:
                prio, weight, port = struct.unpack(">HHH", rdata[:6])
                host, _ = read_name(buf, rdata_off + 6)
                rec.update({"priority": prio, "weight": weight, "port": port, "target": host})
                rec["value"] = host
            elif rtype == T_TXT:
                items = []
                i = 0
                while i < len(rdata):
                    ln = rdata[i]
                    i += 1
                    items.append(rdata[i:i + ln].decode("utf-8", "replace"))
                    i += ln
                rec["txt"] = dict(x.split("=", 1) for x in items if "=" in x)
                rec["value"] = items
            elif rtype == T_A and len(rdata) == 4:
                rec["value"] = socket.inet_ntoa(rdata)
            records.append(rec)
        except (ValueError, struct.error):
            break
    return {"records": records}


def interfaces() -> list[tuple[str, str]]:
    """Return [(iface_name, ipv4)] for non-loopback IPv4 addresses."""
    found: list[tuple[str, str]] = []
    try:
        import psutil  # optional, only for friendly names

        for ifname, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    found.append((ifname, addr.address))
        return found
    except Exception:
        pass
    try:
        host = socket.gethostname()
        for ip in socket.gethostbyname_ex(host)[2]:
            if not ip.startswith("127."):
                found.append((host, ip))
    except Exception:
        pass
    return found


def discover(timeout: float = 4.0, iface_ip: str | None = None) -> list[dict]:
    """Send the query and collect devices. `iface_ip` pins the egress interface."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    if iface_ip:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    sock.bind(("", 0))
    sock.settimeout(0.3)

    query = build_query()
    sock.sendto(query, (MDNS_ADDR, MDNS_PORT))

    devices: dict[str, dict] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = sock.recvfrom(9000)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            parsed = parse_response(data)
        except Exception:
            continue
        for rec in parsed["records"]:
            if rec["type"] == T_SRV:
                key = rec["name"]
                dev = devices.setdefault(key, {"service": key, "address": addr[0]})
                dev.update({"port": rec["port"], "host": rec["target"]})
            elif rec["type"] == T_TXT and rec["name"].endswith(CAST_SERVICE):
                dev = devices.setdefault(rec["name"], {"service": rec["name"], "address": addr[0]})
                dev["txt"] = rec.get("txt", {})
                dev["id"] = rec.get("txt", {}).get("id", "")
            elif rec["type"] == T_A:
                for dev in devices.values():
                    if dev.get("host") == rec["name"]:
                        dev["address"] = rec["value"]
    sock.close()
    return list(devices.values())


def main() -> int:
    timeout = float(sys.argv[1]) if len(sys.argv) > 1 else 4.0
    ifaces = interfaces()
    print(f"interfaces: {ifaces}")
    seen: dict[str, dict] = {}
    for name, ip in ifaces:
        print(f"-- probing {name} ({ip}) --")
        try:
            for dev in discover(timeout, iface_ip=ip):
                dev["via_iface"] = f"{name} ({ip})"
                seen[dev.get("id") or dev["service"]] = dev
                print(f"   FOUND {dev.get('txt', {}).get('fn', '?')} @ {dev.get('address')}:{dev.get('port')} md={dev.get('txt', {}).get('md')}")
        except OSError as exc:
            print(f"   interface error: {exc}")
    import json

    print(json.dumps(list(seen.values()), indent=2))
    return 0 if seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
