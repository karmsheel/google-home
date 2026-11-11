# AGENTS.md

## What this project is

`google-home` is a **Hermes Agent plug-in** that puts a Google Home / Nest
speaker on the other end of a Hermes profile's conversation.

It has two surfaces in one package:

- **`googlehome/`** — the Python plug-in: a `kind: platform` gateway adapter that
  synthesizes replies, casts them over the Cast v2 protocol, accepts voice from
  the host microphone, and exposes four tools.
- **`googlehome/desktop/plugin.js`** — the Hermes Desktop extension: a page, a
  sidebar row, a status-bar chip, palette commands, push-to-talk, and settings.
  It is a thin client of the adapter's loopback control API.

There is **one project and one plug-in**. `googlehome-mini` was a second,
Node-based take on the same idea; it has been retired and its useful parts live
here. Never reintroduce a second project, a second Cast stack, or a second
manifest.

## The goal

Let a person talk to a speaker and get an answer out loud, without giving up the
text chat.

1. **The speaker is the mouth.** Replies are synthesized with Hermes' own TTS
   stack (or the offline Windows engine, or edge-tts) and cast to the device.
2. **The computer is the ear.** Google Home hardware exposes no microphone
   stream to third parties and never will; audio comes from the host machine and
   Hermes' STT. See `docs/REVERSE_ENGINEERING.md`.
3. **One conversation, one speaker.** A profile is bound to the platform, a
   turn is routed to that profile, and the reply comes back through the speaker.
4. **Nothing third-party.** Cast, mDNS, protobuf, and the media server are
   implemented with the standard library.

Explicit **non-goals**: the speaker's microphone, casting music, video, or
arbitrary media, and third-party Cast SDKs.

## Naming — one convention

| Thing | Value |
|---|---|
| Repository | `google-home` |
| Python package | `googlehome/` |
| Plug-in id and platform name | `googlehome` |
| Display label | Google Home |
| Tools | `googlehome_speak`, `googlehome_status`, `googlehome_volume`, `googlehome_devices` |
| CLI | `hermes googlehome <action>` |
| Environment prefix | `GOOGLEHOME_` |
| Desktop extension id | `googlehome` |
| Control service | `hermes-googlehome` |

"Mini" appears **only** as hardware — the Google Home Mini / Nest Mini device.
It never names a project, package, plug-in, module, or setting.

## Shape of the code

| Path | Role |
|---|---|
| `googlehome/__init__.py` | `register(ctx)`: platform, tools, CLI. Defers core imports. |
| `googlehome/adapter.py` | The platform adapter: connect, cast, `send()`, voice turns, profile binding. |
| `googlehome/control.py` | Loopback HTTP API (`/status`, `/playback`, `/devices`, `/ask`, `/speak`, `/stop`, `/listen`, `/device`, `/profile`, `/volume`, `/destination`). |
| `googlehome/playback.py` | In-process registry of audible clips; the stop hooks and the `/playback` snapshot. |
| `googlehome/ambient.py` | Per-conversation destinations chosen in the composer, and the turn-end hook that speaks them. |
| `googlehome/tools.py` | The four tools the model can call. |
| `googlehome/cli.py` | `hermes googlehome check\|devices\|speak\|status\|cron\|firewall`. |
| `googlehome/config.py` | Settings resolution, clamped; state and token paths. |
| `googlehome/speech.py` | TTS backends: Hermes, Windows SAPI, edge-tts. |
| `googlehome/castdevice.py`, `castproto.py` | Cast v2 over TLS 8009 and its protobuf envelope. |
| `googlehome/discovery.py` | mDNS discovery and device normalisation. |
| `googlehome/audioserver.py` | HTTP server the speaker fetches the MP3 from. |
| `googlehome/cronlink.py` | Speaks a short digest of cron deliveries. |
| `googlehome/platform_entry.py` | `PlatformEntry` kwargs, feature-detected. |
| `googlehome/desktop/plugin.js` | Hermes Desktop extension (single uncompiled file). |
| `docs/REVERSE_ENGINEERING.md` | Protocol findings; why speaker-in is impossible. |
| `tests/` | 204 stdlib tests (16 need Hermes core). |

## How a turn becomes speech

```text
Desktop (push-to-talk) ──POST /listen──▶ control API ──▶ adapter.submit_audio
                                                          │ STT
                                                          ▼
                                                    Hermes turn (profile)
                                                          │
                                    adapter.send(reply) ◀─┘
                                      flatten markdown → TTS → MP3
                                      media server ◀── HTTP GET ── speaker
                                      Cast TLS 8009: LOAD ───────▶ speaker

Text chat, speaker picked in the composer:
  post_llm_call ──▶ ambient.py (destination for this session)
                     └─ tools.speak_to() ── TTS ── direct cast ──▶ that speaker
```

## Invariants worth preserving

- **The control API is loopback-only and token-gated** (`X-Hermes-Token` from
  `control-token`, mirrored into `control.json`). It can run turns; never widen
  the bind address or drop the token.
- **The adapter lives in the gateway process.** Tools and the control API reach
  it through `active_adapter()`; it is the only holder of the Cast session.
- **One speaker per conversation.** `chat_id` is stable so one device keeps one
  Hermes session.
- **Registration does no network IO.** It runs in every process and Plugin
  Doctor blocks sockets during it.
- **Missing prerequisites are UI states.** No ffmpeg, no TTS backend, or no
  speaker is reported, never raised into the chat.
- **A conversation speaks only if it opted in.** `ambient.py` reads a
  per-session destination; no entry means silence. It skips the `googlehome`
  platform (the adapter already speaks those turns) and turns that already
  called `googlehome_speak`, so nothing is heard twice.
- **Cast is tolerated, not guaranteed.** Google supports the protocol
  begrudgingly; a cast takes over the speaker and adds latency.
- **Audible output is interruptible.** Every cast path registers with
  `playback.py` between LOAD and the wait, so `/stop` can send the media STOP
  from outside the playback thread. `status()` must never take the speak lock
  while `speaking` — the Desktop's stop control depends on that poll returning.
- **Markdown is flattened before speech** and clipped at a sentence boundary.

## Working on it

```powershell
python -m unittest discover -s tests -t .        # 204 stdlib tests
hermes plugins doctor googlehome                 # manifest, import, register
hermes googlehome check                          # speaker reachability
hermes googlehome devices                        # mDNS scan
hermes googlehome speak --text "hello"           # one sentence
```

The package is developed in this directory and linked into Hermes:

```powershell
# once
Remove-Item "$env:HERMES_HOME\plugins\googlehome" -Recurse -Force
New-Item -ItemType Junction -Path "$env:HERMES_HOME\plugins\googlehome" `
  -Target "C:\Users\karms\Projects\google-home\googlehome"
hermes plugins enable googlehome
```

The Desktop half is a **copy**: `googlehome/desktop/plugin.js` must be copied to
`$env:HERMES_HOME\desktop-plugins\googlehome\plugin.js`, then reloaded from the
Desktop (Ctrl+K → Reload desktop plugins). The Desktop never reads this
directory.

See `README.md` for install, settings, and troubleshooting.
