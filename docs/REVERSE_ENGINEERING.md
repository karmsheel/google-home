# Reverse engineering the Google Home Mini

What the device actually speaks, what it will let a third party do, and where the
wall is. Findings marked **verified** were reproduced on this machine (against a
protocol-faithful mock); findings marked **sourced** come from public
documentation and teardown write-ups, cited inline.

---

## 1. Finding it on the network

**verified** — `discovery.py` implements a pure-stdlib mDNS query (no
`zeroconf`, no `dnspython`) and was used to probe this LAN.

Cast devices advertise `_googlecast._tcp.local` over mDNS (UDP 5353,
`224.0.0.251`). The TXT record carries the identity:

| Key | Meaning |
|---|---|
| `id` | stable device UUID |
| `fn` | friendly name ("Living Room speaker") |
| `md` | model string ("google home mini", "google nest mini") |
| `ca` | **capability bitmask** |
| `ve`, `ic`, `st`, `rs`, `bs`, `nf` | version / misc, semantics undocumented |

`ca` is the useful one: bit 0 video-out, bit 1 video-in, bit 2 **audio-out**,
bit 3 audio-in, bit 4 dev-mode, bit 5 group. A speaker is `ca=4`; a Chromecast is
`ca=5`. (Sourced: pychromecast `const.py`, Chromium `openscreen/cast/docs/protocol_flow.md`.)

**verified** — real firmware sets high undocumented bits too: the Home Mini on
this LAN advertises `ca=199172` (`0x30A04`). Bit 2 is still the audio-out bit, so
classification must be a mask test (`ca & 4 and not ca & 1`); an equality check
against `4` mislabels a genuine speaker as a display device.

The SRV record gives the control port: **8009**.

Two operational notes learned the hard way on this machine:

1. **mDNS goes out one interface.** This host is dual-homed on the same `/24`
   (Ethernet `192.168.1.10` + Wi-Fi `192.168.1.11`), and a single multicast
   query only leaves one of them. `discovery.discover()` therefore probes every
   local IPv4 address in turn (`IP_MULTICAST_IF`).
2. **mDNS can fail while the device is still reachable.** A unicast TCP sweep for
   port 8009 is the reliable fallback.

Identity beyond mDNS lives on an unauthenticated HTTPS endpoint on **8443**:

```
GET https://<device>:8443/setup/eureka_info?params=device_info,name,build_info
→ { name, device_info: { model_name, product_name, ssdp_udn }, build_info: { cast_build_revision } }
```

(Also reachable on `:8008` plain HTTP on some firmware.)

---

## 2. The control channel

**verified** — `googlehome/castproto.py` + `googlehome/castdevice.py`, exercised
against `tests/mock_cast_device.py`.

Transport is **TLS on TCP 8009**. The certificate is self-signed and every working
sender skips verification (`verify_mode = CERT_NONE`), so no pairing, no account,
no certificate is needed to open a session.

Every frame is:

```
uint32 big-endian length  |  CastMessage (protobuf)
```

`CastMessage` is small enough to hand-encode. The fields that matter:

| # | Field | Type | Value |
|---|---|---|---|
| 1 | `protocol_version` | varint | `0` (`CASTV2_1_0`) |
| 2 | `source_id` | string | our sender id (`sender-0`) |
| 3 | `destination_id` | string | `receiver-0` or an app's `transportId` |
| 4 | `namespace` | string | which subprotocol |
| 5 | `payload_type` | varint | `0` = STRING |
| 6 | `payload_utf8` | string | JSON body |

That is why this plugin carries **no protobuf dependency**: ~120 lines of
varint/length-delimited encoding is the whole of it.

### Virtual channels

Before a namespace will answer, you open a channel to its endpoint:

```
→ tp.connection / receiver-0   {"type":"CONNECT"}
```

**verified** — that CONNECT is per endpoint, and an app that is *already running*
still needs its own. `GET_STATUS` may report the Default Media Receiver with a
`transportId`; sending `LOAD` to that transport without a `CONNECT` first makes
the receiver close the sender's connection outright (`WinError 10054` /
`device closed the connection`). Launching the app is not the only way to learn a
`transportId`, so the client must always open the channel before the first
`LOAD`, whether it launched the app or found it up.

**verified** — the sender id is a session identity, not a constant. Two clients
presenting `sender-0` to one device (a running bridge plus
`hermes googlehome check`) make the receiver close one of them. Generate an id
per connection.

The device drops silent senders, so a heartbeat must run:

```
→ tp.heartbeat / receiver-0    {"type":"PING"}     every ~5 s
← tp.heartbeat / sender-0      {"type":"PONG"}
```

**verified** — the receiver does not reliably echo `requestId` on
`MEDIA_STATUS` replies, so a poll that only accepts its own id can time out for
the whole clip even though playback is fine. Accept any `MEDIA_STATUS` on a
single-session client, and bound the wait by the clip's advertised duration.

A read that times out is **not** a dead connection — the media namespace only
pushes on state *changes*. Treating a read timeout as fatal is the classic way to
write a Cast client that drops every long clip. This implementation raises a
non-fatal `CastTimeout` and keeps the socket.

### Namespaces used here

| Namespace | Purpose |
|---|---|
| `urn:x-cast:com.google.cast.tp.connection` | open/close virtual channels |
| `urn:x-cast:com.google.cast.tp.heartbeat` | PING/PONG keepalive |
| `urn:x-cast:com.google.cast.receiver` | device status, app launch, volume |
| `urn:x-cast:com.google.cast.media` | media load/stop/status |

`urn:x-cast:com.google.cast.tp.deviceauth` exists, but **senders are not required
to use it** — media playback over the Default Media Receiver works without device
authentication. (It is enforced by Google's own senders, not by the receiver.)

---

## 3. Making it talk

The path that works, verified end to end against the mock:

```
1. GET_STATUS        → receiver-0        {"type":"GET_STATUS","requestId":1}
                    ← RECEIVER_STATUS    {status:{volume:{level}, applications:[…]}}

2. LAUNCH            → receiver-0        {"type":"LAUNCH","appId":"CC1AD845",
                                          "requestId":2}
                    ← RECEIVER_STATUS    applications[0].transportId = "web-42"

3. CONNECT           → web-42            {"type":"CONNECT"}

4. LOAD              → web-42            {"type":"LOAD","requestId":3,
                                          "autoplay":true,
                                          "media":{"contentId":"http://…/x.mp3",
                                                   "contentType":"audio/mpeg",
                                                   "streamType":"BUFFERED",
                                                   "metadata":{…}}}
                    ← MEDIA_STATUS       {status:[{playerState:"PLAYING",
                                                    mediaSessionId:…}]}
```

`CC1AD845` is the **Default Media Receiver**: a built-in app id that needs no
registration, which is what makes third-party casting possible at all.

### The audio is pulled, not pushed

`contentId` is a URL and **the speaker fetches it itself**. Consequences that
decide the design:

- The URL must be reachable **from the device**, so it needs this machine's LAN
  address — not `localhost`, not `127.0.0.1`. `discovery.local_ip_for(peer)` asks
  the routing table which source address it would use, which is the only correct
  answer on a dual-homed host.
- Cast devices **ignore DHCP DNS and use 8.8.8.8**, so `*.local` hostnames do not
  resolve. Use a bare IP literal. (Sourced: Home Assistant Cast docs.)
- There is no place to attach auth headers, so the media path is unauthenticated.
  Bind it to the LAN and serve only files the plugin generated.
- A **Windows Firewall rule for the media port is required**. The plugin counts
  fetches: if a reply was cast but the media server served zero requests, the
  firewall is the first suspect. That is a diagnosable failure rather than a
  mystery.
- Receivers issue `HEAD` and `Range` requests, so the server answers `206` with
  `Content-Range` and advertises `Accept-Ranges: bytes`.

Supported audio: MP3, AAC (LC/HE), FLAC, Opus, Vorbis, WAV. `audio/mpeg` MP3 is
the safest common denominator. (Sourced: developers.google.com/cast/docs/media.)

### Volume is device-global

```
→ receiver / receiver-0   {"type":"SET_VOLUME","volume":{"level":0.8}}
```

One level for the whole device, no per-app volume. The media-namespace `VOLUME`
command is a *stream* fade and is not device volume. This plugin saves the level,
applies the configured speech level, and restores it afterwards.

### Knowing when it stopped

`MEDIA_STATUS` arrives on change, not continuously. A clip that runs for 30 s
produces no frames after `PLAYING`, so waiting on frames stalls. Poll instead:

```
→ media / <transport>   {"type":"GET_STATUS","requestId":N}
← MEDIA_STATUS          {status:[{playerState:"IDLE","idleReason":"FINISHED"}]}
```

Latency from `LOAD` to first audio is roughly **1–3 s** (receiver launch is the
dominant term on the first cast after idle). Budget it in the voice loop.

---

## 4. The wall: there is no microphone stream

This is the part worth being blunt about, because it decides the whole
architecture.

**The Home Mini's microphone is not exposed to third parties, by any supported
or reverse-engineered local means.** Each avenue, and why it fails:

| Avenue | Verdict |
|---|---|
| A Cast namespace for Assistant / voice | **No such namespace.** The documented, used namespaces are connection, heartbeat, receiver, media, deviceauth. Searches for `urn:x-cast:com.google.cast.assistant` return nothing real. |
| Local HTTP/gRPC audio endpoint | None exists. The only extra services are `:8443`/`:8008` eureka setup info — JSON identity, no audio. |
| Google Assistant SDK | Implements *your* device as an Assistant client. It cannot tap someone else's speaker. (And it now returns audio-only responses.) |
| Conversational Actions (custom "OK Google, ask …") | **Shut down 2023-06-13.** The old hobbyist route to custom voice intents is gone. |
| Assistant "Broadcast" | Cloud-only, requires a GCP OAuth setup, relays a fixed phrase — it is not a handler you can answer. |
| Home APIs (2024/2025) | Structures, devices, traits, Matter commissioning. No microphone, no TTS. |
| Bluetooth | The speaker supports using *an external* Bluetooth speaker as its output. There is no A2DP-sink mode and no HFP microphone. |
| Hardware | The 2017 Home Mini has no exposed JTAG/UART, secure boot, and `dm-verity` on rootfs. Code execution was achieved only by desoldering the NAND and using a YAFFS2 bug, with a reported **5–10 % boot success rate**. Not a product path. |

(Sources: courk.cc Home Mini teardown parts 1–2, developers.google.com/assistant/ca-sunset,
pychromecast `dial.py`, Home Assistant Cast + Assistant SDK docs.)

**Therefore: split the I/O.** The speaker is the *mouth*; the host machine's
microphone is the *ear*. That is exactly how production voice assistants with
these speakers are built (Home Assistant + Wyoming satellites put the mic in a
separate device for the same reason).

```
   your voice ──▶ host microphone ──▶ STT ──▶ Hermes profile ──▶ TTS
                                                                  │
   the room  ◀────── Google Home speaker ◀──── Cast LOAD ◀── MP3 ─┘
```

Put the PC and the speaker in the same room and the loop feels natural: you talk
towards the speaker, the PC nearby hears you, the speaker answers.

---

## 5. Identity and failure modes observed

- **Device auth is real but sender-chosen.** A receiver cannot demand it for
  media. However, Google can ship certificates with baked-in expiry — in March
  2025, older Chromecasts began refusing senders with "Untrusted device" until
  firmware and app updates shipped. Third-party casting on this class of device
  is *tolerated*, not promised.
- **An idle session dies in ~10 s** without heartbeats.
- **`LOAD` takes over the device.** If someone is playing music, casting a reply
  interrupts it. There is no silent delivery channel.
- **The first cast after idle is slow** because the receiver app has to launch.
- **The media fetch is the fragile hop.** Everything else is TLS to a device on
  the LAN; the audio byte-path additionally depends on the firewall and on
  picking the right local IP.

---

## 6. Reproducing this without the hardware

`tests/mock_cast_device.py` is a TLS Cast endpoint that speaks the same
namespaces: `GET_STATUS`, `LAUNCH`, `LOAD`, `SET_VOLUME`, `STOP`, heartbeats, and
`MEDIA_STATUS` transitions to `IDLE`. `tests/test_castdevice.py` drives the real
client against it, so the protocol implementation is verified on every run
without a speaker present.

The investigation itself used throwaway probes — a per-interface mDNS query, a
unicast sweep of port 8009, and a `bonjour-service` cross-check on the same LAN.
They were scratch scripts, not shipped code: `discovery.py` carries the mDNS
implementation the plugin actually uses, so the findings above stay reproducible
from the repository alone.
