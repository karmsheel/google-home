# Hermes ⇄ Google Home

A Hermes Agent plugin (plus a Hermes Desktop extension) that puts a **Google Home
Mini** — or any Cast-only Google Home / Nest speaker — on the other end of a
Hermes profile's conversation: you talk, a profile answers, the speaker says it
out loud.

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
hermes plugins install file:///C:/Users/you/Projects/google-home/googlehome --enable
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
hermes googlehome check           # can this machine make the speaker talk?
hermes googlehome devices         # what Cast devices are on this network?
hermes googlehome speak "dinner is ready"
hermes googlehome status          # the running bridge, as JSON
hermes googlehome cron on         # also speak every cron job's output
hermes googlehome cron status     # which jobs target the speaker
hermes googlehome cron test       # speak a synthetic cron delivery now
hermes googlehome firewall        # open the media port (UAC prompt)
```

- **Desktop** → sidebar **Google Home** (or ⌘K → "Talk to the speaker").
  Press the mic, speak, press again. The transcript appears, the turn runs, the
  speaker answers.
- **While audio is playing**, the composer's Speaker control shows
  `<speaker> · speaking` and a stop button; its menu also gets a **Stop
  speaking** row. This is deliberately transient: a cron delivery belongs to
  the job, not to the conversation, so the session has no saved destination —
  the control reflects the audio actually coming out of the room.
- **Type instead**: the same page has a text box, so a quiet room works too.
- **Stop it talking**: while audio is coming out of the speaker, the status-bar
  chip turns into **⏹ stop** and the Google Home page shows **Stop speaking**
  next to the microphone. A click cuts the current clip immediately (and skips
  any queued chunks) — useful when the reply is long, was already read in the
  chat, or is simply the wrong moment. It also stops turn-end speech from a
  conversation whose Speaker picker is on.
- **Composer control**: every conversation gets a **Speaker** picker beside the
  model selector — point that chat at a speaker, or Off. See
  [One conversation, one speaker](#one-conversation-one-speaker).
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
  GET  /playback                           # what agent audio is audible now
  POST /ask         {"text": "…"}
  POST /speak       {"text": "…"}          # speak without an agent turn
  POST /stop                               # silence the current clip
  POST /listen      {"data_url": "data:audio/webm;base64,…"}
  POST /device      {"device": "192.168.1.42"}
  POST /profile     {"profile": "coder"}
  POST /volume      {"level": 0-100}
  POST /refresh
  GET  /destination?session=<id>
  POST /destination {"session": "…", "mode": "off|speaker", "device": "…", "name": "…"}
  ```

  `GET /playback` is intentionally cheap: it reads an in-process registry and
  never touches the Cast socket, so the Desktop can poll it while a long reply
  plays. `POST /stop` sends Cast `STOP` for the clip's `mediaSessionId` and
  returns `{"ok": true, "stopped": 1, "playback": {"speaking": false, …}}`.

### One conversation, one speaker

The composer has a **Speaker** control for chats that are not the voice bridge:
a normal Desktop conversation whose replies you want in the room.

| Choice | Effect |
|---|---|
| **Off** (default) | that conversation stays silent |
| a speaker | the final answer of each completed turn is cast to it |

The choice belongs to the conversation — another chat keeps its own — and it is
separate from the bridge's bound device, so the Google Home page can talk
through the kitchen speaker while one chat talks through the living room.

Turn-end speech runs in the gateway and only for conversations that opted in,
so it never doubles the platform adapter; a turn that already called
`googlehome_speak` is not spoken again. The choice is stored under
`$HERMES_HOME/googlehome/destinations.json` and expires after 12 hours.

While one of those replies is playing, the same stop control described above
works from any conversation: the chip reads `⏹ stop`, and clicking it silences
the speaker even though the reply belongs to another chat.

## Cron notifications

A scheduled job can speak its result on the speaker **in addition to** its
normal text target: Hermes cron already fans one job out to a comma-separated
`deliver` list, so the speaker is just one more target.

```bash
hermes googlehome cron on       # add the speaker to every job in this profile
hermes googlehome cron status   # what is attached, plus the target it uses
hermes googlehome cron off      # remove it again
```

Cron stores are per profile. The `googlehome` command is discovered from the
default profile, so target another profile with `--cron-profile` rather than
`-p` (the global `--profile` flag would switch profiles before the plugin
loads):

```bash
hermes googlehome cron on --cron-profile researcher
hermes googlehome cron status --cron-profile researcher
```

`on`/`off` rewrite each job's `deliver` list under the cron store's own lock;
`--dry-run` prints the plan, `--chat-id` overrides the target. A job whose only
target was `local` ends up with just the speaker; `off` returns it to `local`.
The output file is written either way.

For one job instead of all of them:

```bash
hermes cron edit <job> --deliver buzz,googlehome:speaker-192.168.1.145
```

**What gets spoken.** Cron wraps output with `Cronjob Response: …`,
`(job_id: …)` and a "To stop or manage…" footer; none of it is read aloud. The
speaker hears the job name plus a **digest** of the opening — 400 characters by
default (`cron_max_chars`), cut at a sentence and ending "…and more in the
chat." A job can choose the spoken sentence exactly by ending its output with:

```
SPEAK: Two new agent frameworks shipped today; the rest is in the digest.
```

The full report still goes to the text target; only the spoken part uses the
marker. Set `speak_cron: false` to keep the target but mute it (useful when a
profile should get the speaker only for interactive replies).

**Why an explicit target rather than a home channel.** `googlehome` is one
gateway-wide adapter. Enabling `platforms.googlehome` in every profile's config
would start a second adapter per profile, and two adapters on one speaker fight
over the device and its media port. An explicit `googlehome:<chat>` target
rides the gateway's existing adapter, so it works from any profile without
touching its `platforms` block. The helper takes the chat id from the running
bridge's `control.json` (falling back to the configured device).

**Caveats.** Delivery prefers the running bridge; if the gateway is down, the
standalone path synthesizes and casts directly (which needs the media port to be
reachable). Casting takes the speaker over (it interrupts music). Failure
notices follow `deliver` unless the job sets `failure_deliver`, which the helper
does not change.

### Testing cron delivery

Three levels, cheapest first:

```bash
hermes googlehome cron test                 # digest + TTS + cast, no scheduler
hermes cron run <job-id>                    # one real run, manual
hermes googlehome cron status --cron-profile researcher   # wiring check
```

`cron test` speaks a synthetic `Cronjob Response: …` payload through the same
digest and bridge code a real delivery uses, so a pass proves the speaker path.
`hermes cron run` proves the standalone/CLI lane. For the automatic gateway
lane, create a throwaway no-agent probe and let the ticker fire it:

```bash
# script: <HERMES_HOME>/scripts/cron_probe.py -> print("cron probe")
hermes cron create "1m" --name "speaker probe" --script cron_probe.py \
  --no-agent --deliver googlehome --repeat 1
# wait one tick, then
hermes cron remove <job-id>
```

A probe that prints one line is enough; the digest turns it into "speaker
probe. cron probe." on the speaker.

### Why a job can run but stay silent

| Job status / error | Cause | Fix |
|---|---|---|
| `no delivery target resolved for deliver=googlehome` | Bare platform token but no home channel | The adapter sets one on connect; otherwise `platforms.googlehome.home_channel` or use `googlehome:speaker-…` |
| `No live adapter for platform 'googlehome' … standalone_sender_fn` | The run was out-of-process (CLI/external worker) | The plugin registers a standalone sender; reinstall/restart if this message persists |
| `[blocked_config] delivery platform 'googlehome' is not a known cron delivery target` | The job's profile cannot see the plugin | Enable it in that profile (below), or keep speaker targets on jobs in the gateway's own profile |
| Speaker was told to speak, no audio | Media fetch blocked | `media.stats.requests`/gateway log; open the media port |

A secondary profile needs the plugin enabled **and** a (proxy) platform block,
because cron validates delivery inside the job's profile:

```bash
hermes -p researcher plugins enable googlehome
hermes -p researcher config set platforms.googlehome.enabled true
hermes -p researcher config set platforms.googlehome.extra.proxy true
```

`proxy: true` makes that profile forward speech to the primary bridge instead
of opening a second media server and Cast connection for the same speaker.

## Configuration

`platforms.googlehome.extra.*` (or plugin settings in Desktop):

| Key | Default | Meaning |
|---|---|---|
| `device` | `""` | speaker IP or friendly name; empty = first audio-only Cast device |
| `profile` | `""` | profile that answers; empty = the gateway's own profile |
| `volume` | `0` | speech volume 0–100; `0` leaves the device volume alone |
| `max_chars` | `1400` | characters spoken per reply |
| `cron_max_chars` | `400` | characters of a cron delivery that are spoken (80–2000) |
| `tts_provider` | `""` | override the profile's `tts.provider` (Hermes ignores an override that disagrees, see below) |
| `speech_backend` | `auto` | `auto` \| `hermes` \| `sapi` \| `edge` |
| `speech_voice` | `""` | voice name for `sapi` (`Microsoft Zira Desktop`) or `edge` (`en-US-AriaNeural`) |
| `speak_replies` | `true` | cast every reply |
| `speak_cron` | `true` | cast cron deliveries addressed to the speaker |
| `proxy` | `false` | secondary-profile mode: forward to the primary bridge instead of opening a second media server |
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
- **On this machine the profile's TTS provider is not usable** (it has been
  `xai` without a key, and later `elevenlabs` without the package). `auto`
  tries `hermes` first, and a provider call that hangs there also wedges the
  adapter's control API — the composer never sees `speaking` and the stop
  button never appears. The bridge is therefore pinned to the offline engine:
  `platforms.googlehome.extra.speech_backend: sapi`. Fix the provider (or
  install `edge-tts` in the gateway environment and pin `edge`) if you want the
  nicer voices: `hermes googlehome status` shows `speech.last_backend`.

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
| `hermes googlehome cron on` says no jobs | Cron stores are per profile: run it with the same `-p <profile>` the jobs belong to, or check `hermes -p <profile> cron list`. |
| Cron jobs deliver but nothing is spoken | `speak_cron` is off, or the gateway is running the old plugin. Check `hermes googlehome cron status`, then restart the gateway after syncing the plugin. |
| The speaker reads the job's whole report | The digest limit is `cron_max_chars` (default 400); make the job end with a `SPEAK: …` line to choose the exact sentence. |
| Composer keeps saying Off while the speaker talks | The desktop extension is stale or the bridge is down: reload desktop plugins and check `hermes googlehome status`. The picker now mirrors `/playback`. |
| Stop button never appears, bridge stops answering | The TTS provider is hanging inside the adapter (Hermes provider/network). Pin the offline engine: `hermes config set platforms.googlehome.extra.speech_backend sapi`, then restart the gateway. |
| No stop control while the speaker talks | The gateway is running an older plugin, or the Desktop extension is stale: restart the gateway, then Ctrl+K → Reload desktop plugins. |

## Tests

```bash
python -m unittest discover -s tests -t .                    # 204 tests; adapter tests skip without core
$env:PYTHONPATH="$env:LOCALAPPDATA\hermes\hermes-agent"
python -m unittest tests.test_adapter_integration            # 16 tests, needs Hermes core
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
  speech.py                 markdown → spoken text, cron digests, Hermes TTS bridge
  cronlink.py               cron deliver-list wiring for ``hermes googlehome cron``
  pysite.py                 import repair for launchers that strip site-packages
  control.py                loopback control API for the Desktop extension
  playback.py               what is audible now, and how to stop it
  tools.py                  googlehome_* tools
  ambient.py                per-conversation destinations + turn-end hook
  desktop/plugin.js         Desktop extension (plain ESM, no build)
tests/                      protocol, media server, adapter, Desktop harness
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
