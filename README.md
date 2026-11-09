# Hermes ⇄ Google Home

A Hermes Agent plugin (plus a Hermes Desktop extension) that puts a Google Home /
Nest speaker on the other end of a Hermes profile's conversation: you talk, a
profile answers, the speaker says it out loud.

- **Agent plugin** (`googlehome/`) — a `kind: platform` gateway adapter. Replies are
  synthesized with Hermes's own TTS stack and cast to the device over the Cast v2
  protocol. Zero third-party dependencies. Adds four tools.
- **Desktop extension** (`googlehome/desktop/plugin.js`) — a page, a status-bar
  chip, palette commands and a settings page, with the push-to-talk microphone.

## What is actually possible

Read [`docs/REVERSE_ENGINEERING.md`](docs/REVERSE_ENGINEERING.md) for the protocol
work. The short version:

| Direction | How | Status |
|---|---|---|
| **Speaker out** (it talks) | Cast v2 → Default Media Receiver → HTTP MP3 | Works |
| **Speaker in** (it listens) | — | **Not possible**: no Cast namespace, API, or practical hardware route exposes the Home Mini microphone |
| **Voice in** | Host microphone → Hermes STT | Works (push-to-talk in Desktop; any audio client over the local API) |

So the speaker is the mouth and the computer's microphone is the ear. Put them in
the same room and the loop is natural. This is the same split Home Assistant uses
for these speakers.

## Architecture

```
┌──────────────── Hermes Desktop ────────────────┐
│ push-to-talk  →  MediaRecorder → base64        │
└───────────────────────┬────────────────────────┘
                        │  POST http://127.0.0.1:<port>/listen
                        │  (X-Hermes-Token, from control.json)
┌───────────────────────▼────────────────────────┐
│ gateway process                                │
│  googlehome adapter                            │
│    transcribe (Hermes STT)                     │
│    SessionSource(profile="coder") ─┐           │
│    handle_message()                ├─▶ turn    │
│    send(reply) ◀───────────────────┘           │
│      flatten markdown → TTS (Hermes) → MP3     │
│      media server  ◀── HTTP GET ── speaker     │
│      Cast LOAD (TLS 8009) ─────────▶ speaker   │
└────────────────────────────────────────────────┘
```

Two details that matter:

- **The profile is bound per turn.** `submit_utterance()` stamps
  `SessionSource.profile` with the configured profile, so a gateway with
  `gateway.multiplex_profiles: true` runs the turn inside that profile's
  `HERMES_HOME` — its model, skills, memory and credentials. Turns show up in
  Hermes like any other chat. On current Hermes the gateway also wants the route
  declared (`gateway.profile_routes`), because the session authority keys the
  turn on it; see *Install* below.
- **The Desktop half needs a loopback door.** The adapter writes
  `<HERMES_HOME>/googlehome/control.json` with `{port, token}`; the extension
  reads it through the Electron bridge and talks to that port.

## Install

### 1. Agent plugin

```bash
hermes plugins install file:///C:/Users/you/Projects/googlehome-mini/googlehome --enable
```

(or copy `googlehome/` into `%HERMES_HOME%\plugins\` and `hermes plugins enable googlehome`)

Then bind a speaker and a profile:

```bash
hermes config set platforms.googlehome.enabled true
hermes config set platforms.googlehome.extra.media_port 8766     # pin it; needed for the firewall
hermes config set platforms.googlehome.extra.profile coder
hermes config set platforms.googlehome.extra.device              # optional IP; empty = auto-discover
```

Current Hermes also needs the route declared, or the turn is refused with
`RuntimeStoreError('profile_mismatch')` — the gateway's session authority keys
the turn on a route, and a plugin-side `SessionSource.profile` stamp alone leaves
that authority in the default scope:

```bash
hermes config set gateway.profile_routes '[{"platform":"googlehome","profile":"coder"}]'
hermes config set gateway.multiplex_profiles true
hermes gateway restart
```

The same fields are also in **Capabilities → Plugins → Google Home** in Desktop
(`config_schema`), which needs no extra code.

### Authorize the speaker once

The speaker is a *sender*, so the coder profile's pairing guard answers the first
turn with a pairing code. Approve it, or every later turn is refused as an
unpaired sender:

```bash
hermes -p coder pairing approve googlehome <CODE>
```

The first approved message may also ask whether this chat becomes the profile's
**home channel** (cron and cross-platform delivery target). Answer it once —
`ignore` is fine — and later turns go straight to the model.

### Bringing the speaker online

The speaker must be on the same network as this PC. Google Home devices are set
up through a phone, not a desktop app:

1. Phone → **Google Home** app → **+** → *New device* → the Home Mini.
2. Give it the **same Wi-Fi** this PC uses (both must be on the same `/24`).
3. If this PC is dual-homed (Ethernet `192.168.1.10` + Wi-Fi `192.168.1.11`, say),
   either is fine, but both must stay on the same `/24` as the speaker for
   discovery to work.
4. Verify from the PC:

   ```bash
   hermes googlehome check
   ```

   It prints mDNS results, the resolved IP, the local address it would serve
   audio from, whether the Cast handshake succeeded, and whether the device
   actually fetched the test tone. Once that says "the bridge works", you are done.

If mDNS is blocked by the router but the device is reachable, skip discovery:

```bash
hermes googlehome devices          # or use the Google Home app to find its IP
hermes config set platforms.googlehome.extra.device 192.168.1.42
```

### 2. Desktop extension

Copied (no build step — it is plain ESM, loaded uncompiled):

```powershell
New-Item -ItemType Directory -Force "$env:LOCALAPPDATA\hermes\desktop-plugins\googlehome"
Copy-Item .\googlehome\desktop\plugin.js "$env:LOCALAPPDATA\hermes\desktop-plugins\googlehome\plugin.js"
```

The folder name must equal the plugin id (`googlehome`). It hot-reloads on save;
⌘K → "Reload desktop plugins" forces it.

### 3. Firewall (required)

The speaker fetches the audio itself, so it must reach this machine:

```powershell
netsh advfirewall firewall add rule name="Hermes Google Home media" `
  dir=in action=allow protocol=TCP localport=8766
```

The media port is ephemeral by default (`media.stats.port` in `/status`); pin it
with `platforms.googlehome.extra.media_port` and open that port. Symptom of a
missing rule: the cast starts, then the speaker goes quiet and
`media.stats.requests` stays `0`.

## Use

```bash
hermes googlehome check      # can this machine make the speaker talk?
hermes googlehome devices    # what Cast devices are on this network?
hermes googlehome speak "dinner is ready"
hermes googlehome status     # the running bridge, as JSON
hermes googlehome firewall   # open the media port (UAC prompt)
```

- **Desktop** → sidebar **Google Home** (or ⌘K → "Talk to the speaker").
  Press the mic, speak, press again. The transcript appears, the turn runs, the
  speaker answers.
- **Type instead**: the same page has a text box, so a quiet room works too.
- **From the agent**: the `googlehome` toolset —

  | Tool | Purpose |
  |---|---|
  | `googlehome_speak` | say something on the speaker |
  | `googlehome_status` | device, reachability, volume, bound profile, last error |
  | `googlehome_volume` | set device volume 0–100 |
  | `googlehome_devices` | list Cast devices on the LAN |

- **Local API** (same port as above, token required):

  ```
  GET  /status      GET  /devices      GET  /info
  POST /ask         {"text": "…"}
  POST /speak       {"text": "…"}          # speak without an agent turn
  POST /listen      {"data_url": "data:audio/webm;base64,…"}
  POST /device      {"device": "192.168.1.42"}
  POST /profile     {"profile": "coder"}
  POST /volume      {"level": 0-100}
  POST /refresh
  ```

## Configuration

`platforms.googlehome.extra.*` (or plugin settings in Desktop):

| Key | Default | Meaning |
|---|---|---|
| `device` | `""` | speaker IP or friendly name; empty = first audio-only Cast device |
| `profile` | `""` | profile that answers; empty = the gateway's own profile |
| `volume` | `0` | speech volume 0–100; `0` leaves the device volume alone |
| `max_chars` | `1400` | characters spoken per reply |
| `tts_provider` | `""` | override the profile's `tts.provider` (Hermes ignores an override that disagrees, see below) |
| `speech_backend` | `auto` | `auto` \| `hermes` \| `sapi` \| `edge` |
| `speech_voice` | `""` | voice name for `sapi` (`Microsoft Zira Desktop`) or `edge` (`en-US-AriaNeural`) |
| `speak_replies` | `true` | cast every reply |
| `control_port` | `8765` | loopback port for the Desktop extension (auto-increments if busy) |
| `media_port` | `0` | audio server port; `0` = ephemeral |
| `restore_volume` | `true` | restore the device volume after speaking |

## Speech backends

A speaker with no voice is useless, so the plugin has three interchangeable
speech backends and `auto` walks them in order, remembering what worked:

| Backend | What it is | Needs |
|---|---|---|
| `hermes` | Hermes's own `text_to_speech_tool`, so the profile's `tts.provider` and voice are used and long replies are chunked | a working provider — an API key, or `edge` configured in Hermes |
| `sapi` | Windows speech engine (`System.Speech`), WAV | Windows; nothing else. Offline |
| `edge` | Microsoft Edge neural voices (`edge-tts`) | the `edge-tts` package + network |

Two things worth knowing:

- **Hermes ignores a per-call provider that disagrees with `tts.provider`**
  (upstream issue #90109 — operators must control the vendor). Passing
  `provider="edge"` from this plugin therefore cannot reroute the agent's TTS;
  switching backends here changes only what the *speaker* uses, and `hermes` is
  always tried first.
- **On this machine `tts.provider` is `xai` with no `XAI_API_KEY`,** and Hermes
  reports a profile-scoping error for `XAI_BASE_URL` on the multiplexed gateway,
  so Hermes TTS cannot speak at all today. `auto` consequently lands on `sapi`
  and the bridge still works. Fix the provider (or `hermes config set tts.provider edge`)
  if you want the nicer voices: `hermes googlehome status` shows
  `speech.last_backend`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no Cast device answered` | Speaker off, on another network, or mDNS blocked by the dual-NIC host. Set `device` to its IP. |
| `device found … but not answering` | Something is on 8009 but not a healthy receiver; power-cycle it. |
| Reply cast, silence after 1–2 s | The media server is unreachable: firewall, or wrong local IP (`media.stats.last_request_at` stale). |
| `No TTS provider is configured` | Hermes TTS has no usable provider. `auto` should fall back to the Windows engine; check `speech.last_backend` in `hermes googlehome status`, or pin `speech_backend: sapi`. |
| Desktop page says bridge not connected | Gateway not running, or `control.json` missing: check `<HERMES_HOME>/googlehome/control.json`. |
| `RuntimeStoreError('profile_mismatch')` | Declare the route: `hermes config set gateway.profile_routes '[{"platform":"googlehome","profile":"coder"}]'`. A plugin-side `SessionSource.profile` stamp alone does not move the session authority. |
| Reply is a pairing code | Approve the speaker as a sender: `hermes -p <profile> pairing approve googlehome <CODE>`. |
| First turn asks about a "home channel" | Answer it once (`ignore` is fine); it is the profile's delivery target for cron and cross-platform messages. |
| `device closed the connection` on LOAD | The receiver was already running and the client LOADed on a transport it never opened a channel to. Fixed in `ensure_media_receiver()`; if you see it on an old install, re-sync the plugin. |
| Flaky or slow cast while the gateway is up | Two clients on one device need distinct sender ids and their own `CONNECT`; both are handled now, but a stale plugin copy will not be. |
| `hermes googlehome check` says the media port is busy | Normal when the bridge is running — it owns 8766. The check falls back to an ephemeral port and still proves the device can fetch from this machine. |

## Tests

```bash
python -m unittest discover -s tests -t .                    # 118 stdlib tests
$env:PYTHONPATH="$env:LOCALAPPDATA\hermes\hermes-agent"
python -m unittest tests.test_adapter_integration            # 9 tests, needs Hermes core
node tests/desktop/test_desktop_plugin.mjs                   # 16 loader/render checks
```

The Cast protocol is verified against `tests/mock_cast_device.py`, a TLS endpoint
that speaks the same namespaces, so the suite runs without a physical speaker.

## Layout

```
googlehome/                 the installable plugin
  plugin.yaml               manifest (kind: platform, config_schema, tools)
  __init__.py               register(ctx)
  adapter.py                BasePlatformAdapter: turns, TTS out, profile binding
  castproto.py              CastMessage protobuf framing (stdlib only)
  castdevice.py             Cast v2 client: receiver, media, volume, heartbeat
  discovery.py              mDNS + eureka identity + which local IP to advertise
  audioserver.py            LAN media server with Range support and fetch stats
  speech.py                 markdown → spoken text, Hermes TTS bridge
  pysite.py                 import repair for launchers that strip site-packages
  control.py                loopback control API for the Desktop extension
  tools.py                  googlehome_* tools
  desktop/plugin.js         Desktop extension (plain ESM, no build)
tests/                      protocol, media server, adapter, Desktop harness
tools/probe/                live probes used during reverse engineering
docs/REVERSE_ENGINEERING.md protocol findings and the microphone wall
```

## Limitations

- The Google Home microphone is not accessible (see above). Voice input needs a
  host microphone.
- One speaker, one bound profile at a time.
- Casting takes the device over; it will interrupt whatever is playing, and a
  reply takes ~1–3 s to start.
- Third-party casting is tolerated by Google, not guaranteed. A firmware or
  certificate change can break it (it has broken older Chromecasts before).
