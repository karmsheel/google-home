# AGENTS.md

## What this project is

`google-home` is a **Hermes Agent plug-in** that puts a Google Home / Nest
speaker on the other end of a Hermes profile's conversation.

It has two surfaces in one package:

- **`googlehome/`** — the Python plug-in: a `kind: platform` gateway adapter that
  synthesizes replies, casts them over the Cast v2 protocol, accepts voice from
  the host microphone, and exposes five tools.
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
| Tools | `googlehome_speak`, `googlehome_notify`, `googlehome_status`, `googlehome_volume`, `googlehome_devices` |
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
| `googlehome/control.py` | Loopback HTTP API (`/status`, `/profiles`, `/reply`, `/playback`, `/devices`, `/ask`, `/speak`, `/stop`, `/listen`, `/device`, `/profile`, `/volume`, `/mute`, `/destination`). |
| `googlehome/playback.py` | In-process registry of audible clips; the stop hooks and the `/playback` snapshot. |
| `googlehome/ambient.py` | Per-conversation destinations chosen in the composer, and the turn-end hook that speaks them. |
| `googlehome/tools.py` | The five tools the model can call. |
| `googlehome/notify.py` | One-line task outcomes, status wording, speaker-name matching. |
| `googlehome/cli.py` | `hermes googlehome check\|devices\|speak\|notify\|status\|cron\|firewall`. |
| `googlehome/config.py` | Settings resolution, clamped; state and token paths. |
| `googlehome/speech.py` | TTS backends: Hermes, Windows SAPI, edge-tts. |
| `googlehome/castdevice.py`, `castproto.py` | Cast v2 over TLS 8009 and its protobuf envelope. |
| `googlehome/discovery.py` | mDNS discovery and device normalisation. |
| `googlehome/audioserver.py` | HTTP server the speaker fetches the MP3 from. |
| `googlehome/cronlink.py` | Speaks a short digest of cron deliveries. |
| `googlehome/platform_entry.py` | `PlatformEntry` kwargs, feature-detected. |
| `googlehome/desktop/plugin.js` | Hermes Desktop extension (single uncompiled file). |
| `docs/REVERSE_ENGINEERING.md` | Protocol findings; why speaker-in is impossible. |
| `tests/` | 294 stdlib tests (56 need Hermes core); `tests/desktop/` is the plugin.js harness. |

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

Long task, "notify me on the living room speaker when done":
  googlehome_notify ──▶ notify.py (one line, speaker matched by name)
                          └─ live adapter if it owns that device,
                             else tools.speak_to() ── direct cast
```

## Invariants worth preserving

- **The control API is loopback-only and token-gated** (`X-Hermes-Token` from
  `control-token`, mirrored into `control.json`). It can run turns; never widen
  the bind address or drop the token.
- **The page can see its own turn.** `/ask` and `/listen` return a token and the
  adapter records the reply (or the failure) under it; `/reply?token=…` serves it.
  Captures expire (15 min, bounded count). Without this the speaker is the only
  output and a finished turn looks lost.
- **The platform is `trusted_inbound`.** Its only inbound door is that
  token-gated loopback API — no remote sender exists — so the user allowlist and
  the pairing gate must not apply. Otherwise every turn is answered with a
  pairing code instead of the agent's reply.
- **One voice per reply.** Hermes's auto-TTS (`voice.auto_tts`) also hands the
  final text to `send_voice`; this adapter's `send` already speaks it, so auto-TTS
  clips are acknowledged and skipped, never cast twice.
- **The adapter lives in the gateway process.** Tools and the control API reach
  it through `active_adapter()`; it is the only holder of the Cast session.
- **The gateway route is the only profile authority.** `gateway.profile_routes`
  decides who answers. The page reports `routed_profile` in a read-only
  **Answered by** row; there is no profile picker that a route can override.
  The adapter's TTS credential scope follows `effective_profile()` too, so a
  routed turn is voiced with the same profile's provider. Transcription follows
  it as well: `/listen` runs outside any turn, so `_transcribe_blocking` installs
  the same scope around the STT provider.
- **One speaker per conversation.** `chat_id` is stable so one device keeps one
  Hermes session.
- **Registration does no network IO.** It runs in every process and Plugin
  Doctor blocks sockets during it.
- **Missing prerequisites are UI states.** No ffmpeg, no TTS backend, or no
  speaker is reported, never raised into the chat. A turn-end cast that failed is
  recorded (`notices.json`) and reported by the composer's `/playback` poll as
  **Couldn't speak the reply on …** plus a toast, because a silent speaker must
  not look like a delivered reply.
- **A conversation speaks only if it opted in.** `ambient.py` reads a
  per-session destination; no entry means silence. It skips the `googlehome`
  platform (the adapter already speaks those turns) and turns that already
  called `googlehome_speak` or `googlehome_notify`, so nothing is heard twice.
  The clip is synthesized and cast on a worker thread, so the hook copies its
  context into that thread: the gateway's per-profile scope (`HERMES_HOME`
  override and secret mapping) is context-local, and without the copy Hermes TTS
  fails closed on a multiplexed gateway and the room hears nothing.
- **Cast is tolerated, not guaranteed.** Google supports the protocol
  begrudgingly; a cast takes over the speaker and adds latency.
- **Audible output is interruptible.** Every cast path registers with
  `playback.py` between LOAD and the wait, so `/stop` can send the media STOP
  from outside the playback thread. `status()` must never take the speak lock
  while `speaking` — the Desktop's stop control depends on that poll returning.
- **The page is a bridge control, not a profile feature.** The Desktop extension
  is one global file and the page stays reachable from every profile because
  stop, mute, volume, and push-to-talk belong to the room. It names its owner
  (`bridge: <profile>`) when routing differs, and says when the viewed profile
  has no plugin; it must never imply that the viewed profile owns the bridge.
  `/profiles` is the authority for that notice and for the composer's
  per-profile gate.
- **The status-bar chip is a control, not a link.** It opens a popover (volume,
  mute, hide composer picker); the page carries the same three controls in a
  **Speaker controls** box beside the boxed Speaker / Answered by rows (one
  `SpeakerSection`, two columns via a container query at `@lg`), rendered by the
  one `SpeakerControls` component so the surfaces cannot drift. The two boxes
  stretch to a shared height (`flex-1` in a stretched grid item) and share the
  same surface (`bg-quinary` / `stroke-secondary`). The routing rows use
  `ListRow wide` (the Speaker picker on the title line; the Answered-by chip is
  read-only) with their explanations in `Tip`s, keeping the left box short. The page column
  is padded with `px-12`, marked `@container` so the section measures the column
  (not the window), and sized `min(calc(50% + 24rem), 88rem)` — half the side
  gutters of a 48rem column, capped for ultrawide panes. The page stays
  reachable from the
  sidebar and the palette. While audio is audible the chip is still a stop
  button, and the popover closes. Mute is a distinct `muted` setting that gates
  every cast path (adapter `send`, tools `_speak`/`speak_to`, ambient, notify,
  standalone cron), never the device's volume: a muted reply is still recorded
  for the page and cast nowhere.
- **Plugin classes must exist in the compiled Desktop CSS.** The Desktop builds
  Tailwind at build time and loads `plugin.js` at runtime, so a class the build
  never emitted silently does nothing (`max-w-4xl`, `lg:grid-cols-2` were both
  no-ops). Use classes that already appear in the shipped CSS, prefer container
  queries (`@container` + `@lg:`), and run
  `node tests/desktop/class-coverage.cjs <dist/assets/*.css> googlehome/desktop/plugin.js`;
  the harness runs the same check automatically when `HERMES_HOME` holds a build.
- **A notification is one line, never the reply.** `googlehome_notify` composes
  `Task complete|blocked|failed: …` from the model's short label; it never
  accepts the final answer. It also counts as a speaking tool, so a conversation
  pointed at that speaker does not read the whole reply afterwards.
- **A named speaker is matched, never guessed.** `notify.resolve_target` skips
  mDNS for an address, matches names token-wise ("living room speaker" finds
  "Living Room speaker"), and raises with the available names rather than
  falling back to the first speaker when the room is unknown or ambiguous.
- **Markdown is flattened before speech** and clipped at a sentence boundary.
- **Reasoning is never spoken.** `display.show_reasoning` prepends a `💭`
  reasoning block for on-screen surfaces; the adapter strips a leading rendered
  block before speaking (and before the page shows it), and
  `display.platforms.googlehome.show_reasoning: false` stops the gateway
  producing it for this platform at all.

## Working on it

```powershell
python -m unittest discover -s tests -t .        # 294 stdlib tests
node tests/desktop/test_desktop_plugin.mjs       # plugin.js loader/render/behaviour harness
hermes plugins doctor googlehome                 # manifest, import, register
hermes googlehome check                          # speaker reachability
hermes googlehome devices                        # mDNS scan
hermes googlehome speak "hello"                  # one sentence
hermes googlehome notify --status done --task "the build"   # one-line outcome
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
