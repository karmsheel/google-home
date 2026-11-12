/**
 * googlehome — Hermes Desktop extension.
 *
 * Loaded UNCOMPILED as a single ESM file from
 * %HERMES_HOME%/desktop-plugins/googlehome/plugin.js, so:
 *  - single file, no relative imports
 *  - no JSX: only jsx()/jsxs() from react/jsx-runtime
 *  - only @hermes/plugin-sdk, react and react/jsx-runtime resolve
 *  - colours come from var(--ui-*), never a literal hex
 *
 * The speaker's mouth is the Python half of this plugin (it casts TTS audio).
 * Its ears are here: Google Home hardware exposes no microphone stream to
 * third parties, so this page records from the host microphone, sends the clip
 * to the bridge, and the bridge transcribes it and runs the turn as the bound
 * profile. The reply is spoken by the speaker.
 *
 * Transport: the renderer never reads the bridge's control file and never holds
 * its token. It calls this package's dashboard backend through ctx.rest
 * (`/api/plugins/googlehome/...`), and that backend — running on the machine
 * with the gateway — reads the file and proxies to the loopback bridge. That
 * keeps the token in Python and works against a remote Desktop.
 */
import {
  atom,
  host,
  Button,
  ListRow,
  PALETTE_AREA,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  STATUSBAR_AREAS,
  Tip,
  haptic,
  useValue
} from '@hermes/plugin-sdk'
// Namespace read for the composer seam and the icons set: a named import of an
// export an older shell lacks fails the whole module, and the page must keep
// working when the composer control is unavailable.
import * as sdk from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'googlehome'
const ROUTE = '/googlehome'
const STATUS_POLL_MS = 3000
// A turn can take minutes (tools, approvals). The page keeps polling without
// holding the talk button hostage, then settles on whatever the bridge recorded.
const REPLY_POLL_MS = 700
const REPLY_TIMEOUT_MS = 15 * 60 * 1000
const REPLY_SETTLE_MS = 1600

// Read through the namespace import so an older shell without the composer
// seam simply hides the control instead of failing to load the module.
const COMPOSER_AREAS = sdk.COMPOSER_AREAS
const SESSION_ROW_AREAS = sdk.SESSION_ROW_AREAS
const SDK_ICONS = sdk.icons
// The status-bar chip opens a small speaker popover instead of navigating to
// the page. Read through the namespace (not the named import) so a Desktop
// older than the popover simply keeps the route it has always opened.
const SDK_POPOVER = {
  root: sdk.Popover,
  content: sdk.PopoverContent,
  trigger: sdk.PopoverTrigger,
  toggle: sdk.Switch
}
const HAS_POPOVER = Boolean(
  SDK_POPOVER.root && SDK_POPOVER.content && SDK_POPOVER.trigger && SDK_POPOVER.toggle
)

const $status = atom({ kind: 'idle' })
const $devices = atom([])
const $profiles = atom([])
const $recording = atom(false)
const $busy = atom(false)
const $transcript = atom('')
const $reply = atom('')
const $replyError = atom('')
// A submitted turn whose reply has not landed yet. Separate from `$busy` so the
// microphone is usable while the agent is still thinking.
const $waiting = atom(false)
const $turnToken = atom('')
// Google Home conversations, read from the routed profile's own store. This is
// what makes the session findable: the Desktop files it under "Googlehome" with
// a title from its first message, not under a speaker section.
const $conversations = atom({ kind: 'idle', rows: [], error: '' })
const $conversationIds = atom([])
const $notice = atom('')
// Cheap one-second poll of /playback. It says whether agent audio is audible
// right now, which is what turns the status chip into a stop control.
const $playback = atom({ speaking: false, kind: 'idle' })
// The chip's speaker popover, and the one preference it owns. Both are UI
// state, not adapter state: "hide the picker" never stops a conversation from
// speaking, it only gives the composer back.
const $chipOpen = atom(false)
const $composerHidden = atom(false)
// The profile the Desktop is currently viewing. The Google Home page is a
// bridge control visible everywhere, but the composer picker is a per-profile
// capability: a profile without the plugin has no post_llm_call hook, so the
// picker would record a destination nothing reads.
const $activeProfile =
  host.state && host.state.profile && typeof host.state.profile.get === 'function'
    ? host.state.profile
    : atom('')
// Who owns the bridge and which profiles can actually speak. `known` stays
// false against an older bridge that has no /profiles endpoint, in which case
// the UI keeps its previous (always-visible) behaviour.
const $profileInfo = atom({ known: false, bridge_profile: '', plugin_profiles: [] })

let recorder = null
let chunks = []
let noticeTimer = null
let pollTimer = null
let volumeTimer = null
let profileInfoAt = 0
// Plugin-scoped persistence, handed over by register(); a missing door keeps
// the popover working for this run and simply forgets the picker preference.
let pluginStorage = null
// The host context, kept for its `rest` door: every bridge call goes through
// this package's dashboard backend, never through Electron's native bridge.
let pluginCtx = null
const COMPOSER_HIDDEN_KEY = 'composerPickerHidden'
// Which profile the conversations list was last read for; the routed profile
// can change under us (a settings edit, a profile_routes change).
let sessionsProfile = ''

// ── helpers ──────────────────────────────────────────────────────────────────
function errorText(error, fallback) {
  if (typeof error === 'string' && error && error !== '[object Object]') return error
  if (error && typeof error.message === 'string' && error.message) return error.message
  return fallback
}

function say(message) {
  $notice.set(String(message || ''))
  if (noticeTimer) clearTimeout(noticeTimer)
  noticeTimer = setTimeout(() => $notice.set(''), 5000)
}

function hapticTap() {
  try {
    if (typeof haptic === 'function') haptic('tap')
  } catch {
    /* older shell */
  }
}

/** The host's rejection into something the page can show. */
function restError(error) {
  const message = errorText(error, 'the bridge did not answer')
  // `ctx.rest` reports "<status>: {json}" for a non-2xx answer; unwrap the
  // backend's `detail`/`error` sentence when there is one.
  const match = /^(\d{3}):\s*([\s\S]*)$/.exec(message)
  if (!match) return message
  const status = Number(match[1])
  let detail = ''
  try {
    const parsed = JSON.parse(match[2])
    detail = String((parsed && (parsed.detail || parsed.error)) || '')
  } catch {
    detail = match[2].trim()
  }
  if (status === 404) {
    return (
      'The Google Home backend is not loaded in this profile. Enable the plugin, ' +
      'then restart the gateway or reload desktop plugins.'
    )
  }
  if (detail) return detail
  return `The Google Home bridge answered ${status}`
}

/**
 * One call to the bridge, through this package's dashboard backend.
 *
 * `ctx.rest` is namespace-scoped (`/api/plugins/googlehome/<path>`), carries the
 * profile/connection scope, and runs on the Hermes host — so the renderer needs
 * neither the control port nor the token.
 */
async function bridgeApi(path, options) {
  const opts = options || {}
  if (!pluginCtx || typeof pluginCtx.rest !== 'function') {
    throw new Error(
      'This Hermes Desktop cannot reach the Google Home backend. Update Desktop, then retry.'
    )
  }
  const route = String(path || '').replace(/^\/+/, '')
  if (!route) throw new Error('the Google Home bridge route is empty')
  const request = { method: opts.method || 'GET' }
  if (opts.body !== undefined) request.body = opts.body
  if (opts.timeoutMs) request.timeoutMs = opts.timeoutMs
  try {
    return await pluginCtx.rest(route, request)
  } catch (error) {
    throw new Error(restError(error))
  }
}

async function refreshStatus() {
  try {
    const status = await bridgeApi('/status')
    $status.set({ kind: 'ready', status })
    const profile = String(status.routed_profile || status.profile || '').trim()
    // A different routed profile means a different session store: re-read the
    // conversations so the page never shows another agent's turns.
    if (profile && profile !== sessionsProfile) {
      sessionsProfile = profile
      void loadConversations({ force: true })
    }
  } catch (error) {
    $status.set({ kind: 'error', message: errorText(error, 'Bridge unreachable') })
  }
}

async function refreshProfileInfo(force) {
  if (!force && $profileInfo.get().known && Date.now() - profileInfoAt < 15000) return
  try {
    const info = await bridgeApi('/profiles')
    // An older bridge answers 404, and a hand-rolled one may omit the field:
    // only a real list proves anything, so anything else stays "unknown" and
    // the UI keeps its forgiving default rather than hiding a working control.
    if (!info || !Array.isArray(info.plugin_profiles)) return
    profileInfoAt = Date.now()
    $profileInfo.set({
      known: true,
      bridge_profile: String(info.bridge_profile || '').trim(),
      plugin_profiles: info.plugin_profiles.map(name => String(name || '').trim().toLowerCase()).filter(Boolean)
    })
  } catch {
    /* old bridge or bridge down: keep the last known state */
  }
}

// A reply the speaker never said must not look delivered. The gateway records
// every failed ambient cast and serves those notices on the /playback poll this
// page already makes, so the composer can name the failure without a second
// request. `noticesSeq` is page-wide: the chip and the composer share one poll.
const NOTICE_BACKLOG_MS = 2 * 60 * 1000
let noticesSeq = null

/** A backend error as one readable line, without the restart advice. */
function failureSummary(error) {
  let flat = String(error || '').replace(/\s+/g, ' ').trim()
  flat = flat.replace(/^No speech backend could speak\.\s*/i, '')
  flat = flat.replace(/\s*Run `?hermes gateway restart`[\s\S]*$/i, '').trim()
  if (!flat) return 'speech synthesis failed'
  return flat.length > 160 ? `${flat.slice(0, 159)}…` : flat
}

/** Is this session the chat the composer is looking at? */
function isFocusedSession(session) {
  const id = String(session || '')
  if (!id) return false
  try {
    if (String(host.state.focusedSessionId.get() || '') === id) return true
    const stored = host.state && host.state.focusedStoredSessionId
    return Boolean(stored && typeof stored.get === 'function' && String(stored.get() || '') === id)
  } catch {
    return false
  }
}

function showUnspoken(notice) {
  const where = withoutAddress(notice.name || notice.device || '') || 'the speaker'
  const reason = failureSummary(notice.error)
  const mine = isFocusedSession(notice.session)
  if (mine) $speakerNotice.set(`Couldn't speak the reply on ${where}: ${reason}`)
  host.notify({
    kind: 'error',
    title: mine ? 'Reply not spoken' : `Reply not spoken on ${where}`,
    message: `${where}: ${reason}`,
    icon: 'broadcast',
    durationMs: 10000
  })
}

/**
 * Announce each unspoken reply once. The first poll adopts the bridge's history
 * (except a failure from the last couple of minutes for this very chat, which is
 * still news), so opening a conversation does not replay old ones.
 */
function announceUnspoken(snapshot) {
  const notices = Array.isArray(snapshot && snapshot.notices) ? snapshot.notices : []
  if (!notices.length) {
    if (noticesSeq === null) noticesSeq = 0
    return
  }
  const newest = notices.reduce((top, item) => Math.max(top, Number(item.seq) || 0), 0)
  if (noticesSeq === null) {
    noticesSeq = newest
    for (const notice of notices) {
      if (isFocusedSession(notice.session) && Date.now() - Number(notice.at || 0) <= NOTICE_BACKLOG_MS) {
        showUnspoken(notice)
      }
    }
    return
  }
  const fresh = notices.filter(item => (Number(item.seq) || 0) > noticesSeq)
  noticesSeq = Math.max(noticesSeq, newest)
  for (const notice of fresh) showUnspoken(notice)
}

async function refreshPlayback() {
  try {
    const snapshot = await bridgeApi('/playback')
    $playback.set({ ...snapshot, kind: 'ready' })
    announceUnspoken(snapshot)
  } catch {
    // An unreachable bridge is already reported by refreshStatus; do not turn
    // a missed poll into a stop button for audio that may not be playing.
    $playback.set({ speaking: false, kind: 'error' })
  }
}

async function stopSpeaking() {
  const before = $playback.get()
  if (before && before.stopping) return
  hapticTap()
  $playback.set({ ...before, stopping: true })
  try {
    const result = await bridgeApi('/stop', { method: 'POST', body: {} })
    $playback.set({ ...(result.playback || { speaking: false }), kind: 'ready', stopping: false })
    if (result.stopped) say('Stopped the speaker')
    else say('Nothing was playing')
    await refreshStatus()
  } catch (error) {
    $playback.set({ ...before, stopping: false })
    say(errorText(error, 'Could not stop the speaker'))
    await refreshPlayback()
  }
}

/** Persisted "hide the composer picker" choice; false when the door is absent. */
function readComposerHidden() {
  try {
    return Boolean(pluginStorage && pluginStorage.get(COMPOSER_HIDDEN_KEY, false))
  } catch {
    return false
  }
}

function setComposerHidden(hidden) {
  const next = Boolean(hidden)
  $composerHidden.set(next)
  try {
    if (pluginStorage) pluginStorage.set(COMPOSER_HIDDEN_KEY, next)
  } catch {
    /* a broken store must not make the picker un-hideable */
  }
}

/** Commit a slider level to the device (and to the adapter's cast volume). */
async function setSpeakerVolume(level) {
  const value = Math.max(0, Math.min(100, Math.round(Number(level) || 0)))
  try {
    await bridgeApi('/volume', { method: 'POST', body: { level: value } })
    await refreshStatus()
  } catch (error) {
    say(errorText(error, 'Could not set the speaker volume'))
  }
}

/** Silence (or resume) spoken replies without losing them on the page. */
async function setSpeakerMuted(muted) {
  const next = Boolean(muted)
  hapticTap()
  try {
    await bridgeApi('/mute', { method: 'POST', body: { muted: next } })
    await refreshStatus()
    say(next ? 'Speaker muted' : 'Speaker unmuted')
  } catch (error) {
    say(errorText(error, 'Could not mute the speaker'))
  }
}

async function loadDevices() {
  try {
    const result = await bridgeApi('/devices')
    $devices.set(result.devices || [])
  } catch {
    $devices.set([])
  }
}

async function loadProfiles() {
  try {
    const result = await host.profiles.list()
    const rows = (result && result.profiles) || []
    $profiles.set(rows.map(row => String(row.name || '')).filter(Boolean))
  } catch {
    $profiles.set([])
  }
}

// ── replies the page would otherwise never see ──────────────────────────────
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))

/** The profile whose session store holds this speaker's conversations. */
function sessionProfile() {
  const state = $status.get()
  const status = (state && state.status) || {}
  const routed = String(status.routed_profile || status.profile || '').trim()
  if (routed) return routed
  try {
    const ref = host.state && host.state.profile
    return ref && typeof ref.get === 'function' ? String(ref.get() || '').trim() : ''
  } catch {
    return ''
  }
}

async function readGoogleHomeSessions() {
  if (typeof host.listPersistedSessions !== 'function') return []
  const profile = sessionProfile() || 'default'
  const result = await host.listPersistedSessions(null, { profile, limit: 200 })
  return ((result && result.sessions) || [])
    .filter(row => String((row && row.source) || '').toLowerCase() === 'googlehome')
    .map(row => ({ ...row, profile: String(row.profile || profile) }))
    .sort((left, right) => (right.last_active || right.started_at || 0) - (left.last_active || left.started_at || 0))
}

async function loadConversations(options) {
  const force = Boolean(options && options.force)
  if (!force && $conversations.get().kind === 'loading') return
  $conversations.set({ ...$conversations.get(), kind: 'loading' })
  try {
    const rows = await readGoogleHomeSessions()
    $conversations.set({ kind: 'ready', rows, error: '' })
    $conversationIds.set(rows.map(row => String(row.id || '')).filter(Boolean))
  } catch (error) {
    $conversations.set({
      ...$conversations.get(),
      kind: 'error',
      error: errorText(error, 'Could not read the Google Home conversations')
    })
  }
}

async function openConversation(row) {
  if (!row || !row.id) return
  if (typeof host.openSession !== 'function') {
    say('This Hermes build cannot open conversations.')
    return
  }
  hapticTap()
  try {
    await host.openSession(String(row.id), row.profile ? { profile: String(row.profile) } : {})
  } catch (error) {
    say(errorText(error, 'Could not open that conversation'))
  }
}

async function readReply(token) {
  const result = await bridgeApi(`/reply?token=${encodeURIComponent(token)}`)
  if (!result || result.ok === false) {
    return { done: true, reply: '', error: (result && result.error) || 'this turn expired' }
  }
  return result
}

/** Follow one submitted turn to its reply and paint it on the page. */
async function watchReply(token) {
  if (!token) return
  $turnToken.set(token)
  $waiting.set(true)
  const deadline = Date.now() + REPLY_TIMEOUT_MS
  let lastError = ''
  try {
    while (Date.now() < deadline) {
      await sleep(REPLY_POLL_MS)
      let state
      try {
        state = await readReply(token)
      } catch (error) {
        lastError = errorText(error, 'Could not read the reply')
        continue
      }
      if (!state.done) continue
      if (state.error) {
        // The gateway retries a failed send once with a plain-text fallback;
        // give that retry the chance to replace the error with the reply.
        await sleep(REPLY_SETTLE_MS)
        try {
          state = await readReply(token)
        } catch {
          /* keep the first answer */
        }
      }
      if ($turnToken.get() !== token) return
      if (state.reply) $reply.set(state.reply)
      if (state.error) $replyError.set(state.error)
      if (!state.reply && !state.error) {
        $reply.set('The reply was spoken. Hermes did not give the bridge its text.')
      }
      return
    }
    if ($turnToken.get() === token) {
      $replyError.set(
        lastError ||
          'No reply arrived yet. The turn may still be running — open the conversation to follow it.'
      )
    }
  } finally {
    if ($turnToken.get() === token) $waiting.set(false)
    // The first turn of a conversation creates the session row; pick it up so
    // "Open the conversation" and the sidebar badge appear without a reload.
    void loadConversations({ force: true })
  }
}

function pickMimeType() {
  if (typeof MediaRecorder === 'undefined') return ''
  const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
  for (const candidate of candidates) {
    try {
      if (!MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(candidate)) return candidate
    } catch {
      /* try the next one */
    }
  }
  return ''
}

function blobToDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result || ''))
    reader.onerror = () => reject(new Error('could not read the recording'))
    reader.readAsDataURL(blob)
  })
}

async function startRecording() {
  if ($recording.get()) return
  if (!navigator || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    say('This Hermes build cannot record audio.')
    return
  }
  let stream
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 }
    })
  } catch (error) {
    say(`Microphone unavailable: ${errorText(error, 'permission denied')}`)
    return
  }
  const mimeType = pickMimeType()
  recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
  chunks = []
  recorder.ondataavailable = event => {
    if (event.data && event.data.size > 0) chunks.push(event.data)
  }
  recorder.start()
  hapticTap()
  $recording.set(true)
  $transcript.set('')
}

async function stopRecording() {
  if (!recorder) return
  const active = recorder
  const finished = new Promise(resolve => {
    active.onstop = () => resolve()
  })
  try {
    active.stop()
  } catch {
    /* already stopped */
  }
  await finished
  try {
    active.stream.getTracks().forEach(track => track.stop())
  } catch {
    /* stream already gone */
  }
  recorder = null
  const blob = new Blob(chunks, { type: (chunks[0] && chunks[0].type) || 'audio/webm' })
  chunks = []
  $recording.set(false)
  if (!blob.size) return

  $busy.set(true)
  $transcript.set('Transcribing…')
  try {
    const dataUrl = await blobToDataUrl(blob)
    const result = await bridgeApi('/listen', {
      method: 'POST',
      // STT plus a gateway submit can outlast the host's default REST timeout.
      timeoutMs: 180000,
      body: { data_url: dataUrl, mime_type: blob.type }
    })
    if (result.transcript) {
      $transcript.set(result.transcript)
      $reply.set('')
      $replyError.set('')
      hapticTap()
      if (result.submitted && result.token) void watchReply(result.token)
      else if (result.error) $replyError.set(result.error)
    } else {
      $transcript.set('(no speech detected)')
    }
  } catch (error) {
    $transcript.set('')
    say(errorText(error, 'Could not send the recording'))
  } finally {
    $busy.set(false)
  }
}

async function askText(text) {
  const trimmed = String(text || '').trim()
  if (!trimmed) return
  $busy.set(true)
  $reply.set('')
  $replyError.set('')
  try {
    const result = await bridgeApi('/ask', { method: 'POST', body: { text: trimmed } })
    $transcript.set(trimmed)
    if (result.submitted && result.token) void watchReply(result.token)
    else $replyError.set(result.error || 'The gateway did not accept the turn.')
  } catch (error) {
    say(errorText(error, 'Could not send that'))
    $replyError.set(errorText(error, 'Could not send that'))
  } finally {
    $busy.set(false)
  }
}

function statusSummary(statusState) {
  if (!statusState) return 'Not connected'
  if (statusState.kind === 'error') return statusState.message
  if (statusState.kind !== 'ready') return 'Checking…'
  const status = statusState.status || {}
  const device = status.device || {}
  if (!device.reachable) return status.last_error || 'Speaker unreachable'
  const name = speakerLabel(device) || 'Speaker'
  if (status.speaking) return `${name} · speaking`
  return `${name} · ready`
}

/**
 * The speaker as the page should name it: the hardware model with the address
 * in brackets ("Google Home Mini (192.168.1.145)"). The friendly name set in
 * the Google Home app ("Living Room speaker") is a room label, not a type, so
 * it only stands in when the model is unknown. Compose here as well as using
 * the bridge's label, so a freshly copied extension still reads correctly
 * against a gateway that predates the field.
 */
function speakerLabel(device) {
  if (!device) return ''
  const provided = String(device.label || '').trim()
  if (provided) return provided
  const address = String(device.address || device.host || '').trim()
  const friendly = String(device.name || '').trim()
  const model = String(device.model || '').trim()
  const base = model || (friendly && friendly !== address ? friendly : address)
  if (!base) return ''
  if (address && address !== base && !base.includes(address)) return `${base} (${address})`
  return base
}

/**
 * Just the speaker's name, as a person says it: the name set in the Google Home
 * app ("Living Room speaker"), falling back to the model and then the address.
 * This is what the composer chip has room for; the long "model (address)" label
 * stays with `speakerLabel` and the picker's rows, where two speakers of the
 * same model have to be told apart.
 */
function speakerName(device) {
  if (!device) return ''
  const address = String(device.address || device.host || '').trim()
  const friendly = String(device.name || '').trim()
  if (friendly && friendly !== address) return friendly
  const model = String(device.model || '').trim()
  return model || address
}

/** A destination name stored before this rule, with a trailing IP removed. */
function withoutAddress(name) {
  return String(name || '')
    .replace(/\s*\(\s*\d{1,3}(?:\.\d{1,3}){3}\s*\)\s*$/, '')
    .trim()
}

/** Poll /playback while a surface is visible; returns the latest snapshot. */
function usePlaybackPolling(intervalMs) {
  const playback = useValue($playback)
  useEffect(() => {
    void refreshPlayback()
    const timer = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      void refreshPlayback()
    }, intervalMs || 1500)
    return () => clearInterval(timer)
  }, [intervalMs])
  return playback
}

// ── components ───────────────────────────────────────────────────────────────
function Chip({ children }) {
  return jsx('span', {
    className:
      'rounded-full border border-(--ui-border) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    children
  })
}

function TalkButton() {
  const recording = useValue($recording)
  const busy = useValue($busy)
  const label = recording ? 'Stop and send' : 'Start Conversation'

  return jsx(Button, {
    variant: recording ? 'destructive' : 'default',
    disabled: busy,
    onClick: () => {
      hapticTap()
      if (recording) void stopRecording()
      else void startRecording()
    },
    children: jsxs('span', {
      className: 'inline-flex items-center gap-2',
      children: [
        jsx('span', { 'aria-hidden': true, children: recording ? '■' : '🎙' }),
        jsx('span', { children: label })
      ]
    })
  })
}

function StopSpeakingButton() {
  const playback = useValue($playback)
  if (!playback || !playback.speaking) return null
  const stopping = Boolean(playback.stopping)
  return jsxs(Button, {
    variant: 'destructive',
    disabled: stopping,
    'aria-label': 'Stop playback on the speaker',
    onClick: () => void stopSpeaking(),
    children: [
      jsx('span', { 'aria-hidden': true, children: '⏹' }),
      jsx('span', { children: stopping ? 'Stopping…' : 'Stop speaking' })
    ]
  })
}

function StatusHeader() {
  const statusState = useValue($status)
  const notice = useValue($notice)
  const status = (statusState && statusState.status) || {}
  const device = status.device || {}
  const label = speakerLabel(device)
  const friendly = String(device.name || '').trim()
  const subtitle =
    friendly && friendly !== device.address && friendly !== label && !label.includes(friendly)
      ? friendly
      : ''

  return jsxs('div', {
    className: 'flex flex-col gap-1',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', { className: 'text-base font-medium text-(--ui-text-primary)', children: statusSummary(statusState) }),
          status.routed_profile || status.profile
            ? jsx(Chip, {
                children: `profile: ${status.routed_profile || status.profile}${
                  status.profile_overridden ? ' · routed' : ''
                }`
              })
            : null,
          // The page is reachable from every profile; when routing sends turns
          // somewhere other than the profile hosting the adapter, say where the
          // bridge itself lives. This is not a selector, so it is a chip.
          status.bridge_profile &&
          status.bridge_profile !== (status.routed_profile || status.profile)
            ? jsx(Chip, { children: `bridge: ${status.bridge_profile}` })
            : null,
          typeof status.device?.volume === 'number'
            ? jsx(Chip, { children: `volume ${Math.round(status.device.volume * 100)}%` })
            : null,
          status.media && status.media.port ? jsx(Chip, { children: `media :${status.media.port}` }) : null
        ]
      }),
      subtitle
        ? jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: subtitle })
        : null,
      notice
        ? jsx('span', { className: 'text-xs text-(--ui-accent)', children: notice })
        : null
    ]
  })
}

function ConnectionRow() {
  const statusState = useValue($status)
  const devices = useValue($devices)
  const status = (statusState && statusState.status) || {}
  const device = status.device || {}

  const bindDevice = async value => {
    hapticTap()
    try {
      await bridgeApi('/device', { method: 'POST', body: { device: value } })
      await refreshStatus()
      say('Speaker updated')
    } catch (error) {
      say(errorText(error, 'Could not change the speaker'))
    }
  }

  const deviceValue = device.address || device.host || ''
  const routedProfile = String(status.routed_profile || '')
  const configuredProfile = String(status.profile || '')
  const profileOverridden = Boolean(status.profile_overridden) || Boolean(routedProfile && routedProfile !== configuredProfile)

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      // `wide` puts the Select on the title line instead of under it, which is
      // both the "picker beside its name" layout and one control-row shorter.
      // The one-line explanations live in the tooltips, not in the box.
      jsx(ListRow, {
        wide: true,
        title: jsx(Tip, {
          label: 'The speaker the bridge controls. Every profile talks through this one device.',
          children: jsx('span', { children: 'Speaker' })
        }),
        action: jsxs(Select, {
          value: deviceValue,
          onValueChange: value => void bindDevice(value),
          children: [
            jsx(SelectTrigger, { className: 'w-56', children: jsx(SelectValue, { placeholder: 'No speaker' }) }),
            jsx(SelectContent, {
              children: devices.length
                ? devices.map(row =>
                    jsx(
                      SelectItem,
                      {
                        value: row.address || row.host || '',
                        children: speakerLabel(row) || row.address
                      },
                      row.id || row.address
                    )
                  )
                : jsx(SelectItem, { value: deviceValue, children: speakerLabel(device) || deviceValue || 'No device found' })
            })
          ]
        })
      }),
      jsx(ListRow, {
        wide: true,
        title: jsx(Tip, {
          label: "Set by the gateway's profile_routes. This page reports it; it cannot change it.",
          children: jsx('span', { children: 'Answered by' })
        }),
        // The route is the only authority. The old picker wrote a plugin setting
        // that profile_routes overrode, and that setting still scopes the TTS
        // credentials — a control that changes something other than its label.
        // It is a read-only chip now; the route belongs in Hermes config.
        description: profileOverridden
          ? `Gateway profile_routes answers as ${routedProfile || 'the gateway default'}; the plugin profile setting (${configuredProfile || 'unset'}) cannot override it.`
          : '',
        action: jsx(Chip, {
          children: routedProfile || configuredProfile || 'gateway default'
        })
      })
    ]
  })
}

/**
 * The page is a bridge control, visible from every profile. That is useful
 * (stop/mute/push-to-talk belong to the room, not to the selected profile), but
 * it must not imply that the profile you are viewing owns the speaker.
 */
function PluginNotice() {
  const info = useValue($profileInfo)
  const active = useValue($activeProfile)
  if (!info.known || !active) return null
  const current = String(active).trim().toLowerCase()
  if (!current || info.plugin_profiles.includes(current)) return null
  const bridge = info.bridge_profile || 'the gateway profile'
  return jsx('p', {
    className: 'text-xs text-(--ui-text-tertiary)',
    children: `This profile (${active}) does not have the Google Home plugin. This page is controlling the bridge in ${bridge}; install and enable the plugin here for this profile's chats to use the speaker.`
  })
}

/**
 * Routing on the left, the speaker's quick controls on the right.
 *
 * The two routing rows used to run the full page width, which pushed every
 * Select to the far edge and left a dead gap between a row's label and its
 * control. Half the width puts each ListRow under its own `@2xl` breakpoint,
 * so the control sits under the description instead of across a canyon.
 */
function SpeakerSection() {
  return jsxs('div', {
    // A container query, not a viewport one: the page is capped at max-w-3xl,
    // so the section must react to its own column width. `@container` lives on
    // the page column; every class here exists in the compiled Desktop CSS.
    className: HAS_POPOVER ? 'grid gap-6 @lg:grid-cols-2' : 'grid gap-6',
    children: [
      jsxs('div', {
        className: 'flex min-w-0 flex-col gap-2',
        children: [
          jsx('span', {
            className:
              'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
            children: 'Speaker & profile'
          }),
          // The two cards stretch with the grid row, so this one matches the
          // Speaker controls card height whichever is taller, and they share
          // the same surface so the section reads as one block.
          jsx('div', {
            className:
              'flex-1 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-quinary) p-4',
            children: jsx(ConnectionRow, {})
          })
        ]
      }),
      HAS_POPOVER
        ? jsxs('div', {
            className: 'flex min-w-0 flex-col gap-2',
            children: [
              jsx('span', {
                className:
                  'text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-quaternary)',
                children: 'Speaker controls'
              }),
              jsx('div', {
                className:
                  'flex-1 rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-quinary) p-4',
                children: jsx(SpeakerControls, {})
              })
            ]
          })
        : null
    ]
  })
}

function Conversation() {
  const transcript = useValue($transcript)
  const reply = useValue($reply)
  const replyError = useValue($replyError)
  const busy = useValue($busy)
  const waiting = useValue($waiting)
  const conversations = useValue($conversations)
  const latest = (conversations.rows || [])[0] || null

  if (!transcript && !reply && !replyError) {
    return jsx('p', {
      className: 'text-sm text-(--ui-text-tertiary)',
      children:
        'Press the microphone, say what you want, then press it again. The speaker answers out loud, and the reply appears here too.'
    })
  }

  return jsxs('div', {
    className: 'flex flex-col gap-2 text-sm',
    children: [
      transcript
        ? jsxs('div', {
            className: 'flex flex-col gap-0.5',
            children: [
              jsx('span', { className: 'text-xs uppercase tracking-wide text-(--ui-text-quaternary)', children: 'You said' }),
              jsx('span', { className: 'text-(--ui-text-primary)', children: transcript })
            ]
          })
        : null,
      busy || waiting
        ? jsx('span', {
            className: 'text-xs text-(--ui-text-tertiary)',
            children: busy ? 'Working…' : 'Waiting for the reply…'
          })
        : null,
      reply
        ? jsxs('div', {
            className: 'flex flex-col gap-0.5',
            children: [
              jsx('span', { className: 'text-xs uppercase tracking-wide text-(--ui-text-quaternary)', children: 'Hermes said' }),
              jsx('span', { className: 'whitespace-pre-wrap text-(--ui-text-primary)', children: reply })
            ]
          })
        : null,
      replyError
        ? jsx('p', { className: 'text-xs text-(--ui-red)', children: replyError })
        : null,
      latest
        ? jsx(Button, {
            variant: 'outline',
            className: 'h-7 self-start px-2 text-xs',
            onClick: () => void openConversation(latest),
            children: 'Open the conversation'
          })
        : null
    ]
  })
}

/** One saved Google Home conversation: clicking opens it like any chat. */
function ConversationRow({ row }) {
  const title = String(row.title || '').trim() || 'Google Home conversation'
  const when = relativeWhen(row.last_active || row.started_at)
  return jsxs('button', {
    type: 'button',
    className:
      'flex w-full items-baseline justify-between gap-3 rounded-md px-2 py-1 text-left text-sm hover:bg-(--ui-row-hover-background)',
    title: `Open "${title}"`,
    onClick: () => void openConversation(row),
    children: [
      jsx('span', { className: 'truncate text-(--ui-text-primary)', children: title }),
      when ? jsx('span', { className: 'shrink-0 text-xs text-(--ui-text-tertiary)', children: when }) : null
    ]
  })
}

function Conversations() {
  const state = useValue($conversations)
  useEffect(() => {
    void loadConversations()
  }, [])
  const rows = state.rows || []
  const visible = rows.slice(0, 6)
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', { className: 'text-sm font-medium text-(--ui-text-primary)', children: 'Conversations with this speaker' }),
          jsx(Button, {
            variant: 'ghost',
            className: 'h-7 px-2 text-xs',
            onClick: () => void loadConversations({ force: true }),
            children: 'Refresh'
          })
        ]
      }),
      state.error
        ? jsx('p', { className: 'text-xs text-(--ui-red)', children: state.error })
        : null,
      state.kind === 'loading' && !rows.length
        ? jsx('p', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Loading…' })
        : visible.length
          ? jsxs('div', {
              className: 'flex flex-col gap-0.5',
              children: visible.map(row => jsx(ConversationRow, { row }, String(row.id)))
            })
          : jsx('p', {
              className: 'text-xs text-(--ui-text-tertiary)',
              children:
                'No conversations yet. Your first turn starts one, and it is saved with the profile that answered — open it here or from the sidebar.'
            })
    ]
  })
}

/** "just now" / "12m ago" / a date, from the rows' epoch seconds. */
function relativeWhen(seconds) {
  const value = Number(seconds || 0) * 1000
  if (!value) return ''
  const delta = Date.now() - value
  if (delta < 0) return ''
  if (delta < 60 * 1000) return 'just now'
  if (delta < 60 * 60 * 1000) return `${Math.max(1, Math.round(delta / 60000))}m ago`
  if (delta < 24 * 60 * 60 * 1000) return `${Math.round(delta / 3600000)}h ago`
  return new Date(value).toLocaleDateString()
}

/** Marks the speaker's session where it appears in the sidebar's Recents. */
function GoogleHomeRowBadge({ sessionId }) {
  const ids = useValue($conversationIds)
  const id = String(sessionId || '')
  if (!id || !ids.includes(id)) return null
  return jsx('span', {
    className:
      'shrink-0 rounded-full border border-(--ui-border) px-1.5 text-[0.625rem] text-(--ui-text-tertiary)',
    title: 'Conversation with the Google Home speaker',
    children: 'Google Home'
  })
}

function QuickAsk() {
  const [draft, setDraft] = useState('')

  const submit = useCallback(
    value => {
      const text = String(value || '').trim()
      if (!text) return
      void askText(text)
      setDraft('')
    },
    []
  )

  return jsxs('form', {
    className: 'flex items-center gap-2',
    onSubmit: event => {
      event.preventDefault()
      submit(draft)
    },
    children: [
      jsx('input', {
        className:
          'h-8 flex-1 rounded-md border border-(--ui-border) bg-transparent px-2 text-sm text-(--ui-text-primary) outline-none focus:border-(--ui-stroke-secondary)',
        placeholder: 'Type instead of talking…',
        value: draft,
        onChange: event => setDraft(event.target.value)
      }),
      jsx(Button, { type: 'submit', variant: 'outline', children: 'Ask' })
    ]
  })
}

function BridgePage() {
  const statusState = useValue($status)
  const playback = usePlaybackPolling(1500)

  useEffect(() => {
    void refreshStatus()
    void refreshProfileInfo()
    void loadDevices()
    void loadProfiles()
    pollTimer = setInterval(() => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      void refreshStatus()
    }, STATUS_POLL_MS)
    return () => {
      if (pollTimer) clearInterval(pollTimer)
      pollTimer = null
    }
  }, [])

  const rescan = async () => {
    hapticTap()
    await loadDevices()
    try {
      await bridgeApi('/refresh', { method: 'POST', body: {}, timeoutMs: 60000 })
    } catch {
      /* refresh is best effort */
    }
    await refreshStatus()
  }

  return jsx('div', {
    className: 'h-full overflow-y-auto',
    children: jsxs('div', {
      // Side gutters are exactly half of a max-w-3xl (48rem) column's gutters:
      // the cap is (pane + 48rem) / 2 = 50% + 24rem, so the empty space on each
      // side halves at every window width. 88rem stops the boxes stretching
      // across an ultrawide pane. The column is also the container the
      // two-column settings section measures.
      className: 'mx-auto flex w-full @container flex-col gap-4 px-12 py-6',
      style: { maxWidth: 'min(calc(50% + 24rem), 88rem)' },
      children: [
        jsx(StatusHeader, {}),
        jsx(PluginNotice, {}),
        jsxs('div', {
          className: 'flex items-center gap-2',
          children: [
            jsx(TalkButton, {}),
            playback && playback.speaking ? jsx(StopSpeakingButton, {}) : null,
            jsx(Button, { variant: 'ghost', onClick: () => void rescan(), children: 'Rescan speaker' })
          ]
        }),
        jsx(Conversation, {}),
        jsx(QuickAsk, {}),
        jsx(Conversations, {}),
        jsx(SpeakerSection, {}),
        statusState && statusState.kind === 'error'
          ? jsx('p', { className: 'text-xs text-(--ui-red)', children: statusState.message })
          : null
      ]
    })
  })
}

function SettingsPage() {
  const statusState = useValue($status)
  const devices = useValue($devices)
  const profiles = useValue($profiles)

  useEffect(() => {
    void refreshStatus()
    void refreshProfileInfo()
    void loadDevices()
    void loadProfiles()
  }, [])

  // The renderer no longer knows a control port or a control file: the
  // backend owns that. Report what the bridge itself says instead.
  const status = statusState && statusState.kind === 'ready' ? statusState.status : null

  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsx(StatusHeader, {}),
      jsx(PluginNotice, {}),
      jsx(ConnectionRow, {}),
      jsxs('div', {
        className: 'flex flex-col gap-1 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', {
            children: status
              ? `Bridge connected${status.device && status.device.name ? ` · ${status.device.name}` : ''}`
              : statusState.kind === 'error'
                ? 'Bridge unreachable — start the Hermes gateway after installing the agent plugin.'
                : 'Checking the bridge…'
          }),
          jsx('span', { children: 'Discoverable speakers:' }),
          jsx('span', {
            children: devices.length
              ? devices
                  .map(row => {
                    const label = speakerLabel(row)
                    const friendly = String(row.name || '').trim()
                    return friendly && friendly !== row.address && !label.includes(friendly)
                      ? `${label} · ${friendly}`
                      : label
                  })
                  .join(', ')
              : 'none found on this network'
          }),
          jsx('span', { children: `Profiles: ${profiles.length ? profiles.join(', ') : 'none listed'}` })
        ]
      }),
      jsxs('p', {
        className: 'text-xs text-(--ui-text-quaternary)',
        children: [
          jsx('span', { children: 'Spoken-reply length and the control port are in the agent plugin settings (Capabilities → Plugins → Google Home). Volume and mute are on the Google Home page. ' }),
          jsx('span', {
            children: statusState && statusState.kind === 'ready' && statusState.status.last_error
              ? `Last error: ${statusState.status.last_error}`
              : ''
          })
        ]
      })
    ]
  })
}

/**
 * The speaker's three quick controls: volume, mute, and whether the composer
 * shows its picker. The status-bar popover and the page both render this one
 * component, so the two surfaces can never disagree about a switch.
 */
function SpeakerControls({ compact }) {
  const statusState = useValue($status)
  const composerHidden = useValue($composerHidden)
  const [draftVolume, setDraftVolume] = useState(null)

  // An older Desktop has no Switch/range styling to offer; the page hides the
  // whole card rather than showing dead controls.
  if (!SDK_POPOVER.toggle) return null

  const status = (statusState && statusState.status) || {}
  const device = status.device || {}
  const settings = status.settings || {}
  const ready = Boolean(statusState && statusState.kind === 'ready' && device.reachable)
  // Mute is the adapter's silence-all setting: replies, tools, notifications
  // and cron all stop. Volume stays the device's own level.
  const muted = Boolean(ready && settings.muted === true)
  const volume = draftVolume != null
    ? draftVolume
    : typeof device.volume === 'number'
      ? Math.round(device.volume * 100)
      : 0

  const commitVolume = level => {
    if (volumeTimer) clearTimeout(volumeTimer)
    volumeTimer = setTimeout(() => {
      volumeTimer = null
      void setSpeakerVolume(level).then(() => setDraftVolume(null))
    }, 280)
  }

  const row = (text, control) =>
    jsxs('div', {
      className: 'flex items-center justify-between gap-3',
      children: [
        jsx('span', { className: 'text-[0.75rem] text-(--ui-text-secondary)', children: text }),
        control
      ]
    })

  return jsxs('div', {
    className: compact ? 'flex w-full flex-col gap-2.5' : 'flex w-full flex-col gap-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsx('span', {
            className: 'min-w-0 truncate text-[0.75rem] font-medium text-(--ui-text-primary)',
            children: ready ? speakerName(device) || 'Speaker' : 'Speaker unreachable'
          }),
          jsx('span', {
            className: 'shrink-0 text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)',
            children: ready ? `${volume}%` : 'off'
          })
        ]
      }),
      jsx('input', {
        type: 'range',
        min: 0,
        max: 100,
        step: 1,
        value: volume,
        disabled: !ready,
        'aria-label': 'Speaker volume',
        className:
          'h-1 w-full cursor-pointer appearance-none rounded-full bg-(--ui-stroke-tertiary) disabled:cursor-not-allowed disabled:opacity-50',
        style: { accentColor: 'var(--dt-primary)' },
        onChange: event => {
          const level = Number(event && event.target ? event.target.value : 0)
          setDraftVolume(level)
          commitVolume(level)
        }
      }),
      jsx('div', { 'aria-hidden': true, className: 'h-px bg-(--ui-stroke-tertiary)' }),
      row(
        'Mute speaker',
        jsx(SDK_POPOVER.toggle, {
          size: 'xs',
          checked: muted,
          disabled: !ready,
          'aria-label': 'Mute the speaker',
          onCheckedChange: next => void setSpeakerMuted(Boolean(next))
        })
      ),
      row(
        'Hide composer picker',
        jsx(SDK_POPOVER.toggle, {
          size: 'xs',
          checked: composerHidden,
          'aria-label': 'Hide the composer speaker picker',
          onCheckedChange: next => {
            hapticTap()
            setComposerHidden(Boolean(next))
          }
        })
      )
    ]
  })
}

function StatusChip() {
  const statusState = useValue($status)
  const recording = useValue($recording)
  const playback = usePlaybackPolling(1500)
  const open = useValue($chipOpen)

  useEffect(() => {
    void refreshStatus()
    const timer = setInterval(() => void refreshStatus(), STATUS_POLL_MS * 3)
    return () => clearInterval(timer)
  }, [])

  const status = (statusState && statusState.status) || {}
  const device = status.device || {}
  const settings = status.settings || {}
  const ready = statusState && statusState.kind === 'ready' && device.reachable
  const speaking = Boolean(playback && playback.speaking)
  const stopping = Boolean(playback && playback.stopping)
  const muted = Boolean(ready && settings.muted === true)
  const label = speaking
    ? stopping
      ? 'stopping'
      : 'stop'
    : recording
      ? 'listening'
      : ready
        ? muted
          ? 'muted'
          : 'speaker'
        : 'speaker off'

  // Audio starting while the popover is open must win: the same click that
  // opened the menu cannot leave a stop control two clicks away.
  useEffect(() => {
    if (speaking && $chipOpen.get()) $chipOpen.set(false)
  }, [speaking])

  // While audio is audible the chip's job changes: the click that opens the
  // speaker controls becomes the click that silences the speaker. That is the
  // whole point of the control — a long or already-read reply must be
  // interruptible from anywhere.
  const button = jsx('button', {
    type: 'button',
    disabled: stopping,
    'aria-label': speaking ? 'Stop playback on the speaker' : 'Open the speaker controls',
    className: speaking
      ? 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-red) hover:text-(--ui-text-primary)'
      : 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
    // The popover trigger owns the click when the shell has one; only the
    // fallback for an older Desktop navigates to the page.
    onClick: speaking
      ? () => void stopSpeaking()
      : HAS_POPOVER
        ? undefined
        : () => {
            hapticTap()
            host.navigate(ROUTE)
          },
    children: [
      jsx('span', {
        'aria-hidden': true,
        children: speaking ? '⏹' : recording ? '🔴' : ready ? (muted ? '🔇' : '🔊') : '🔇'
      }),
      jsx('span', { children: label })
    ]
  })

  const tip = speaking ? 'Stop the speaker' : 'Speaker controls'
  if (speaking || !HAS_POPOVER) {
    return jsx(Tip, { label: tip, children: button })
  }

  // No Tip around the popover: Radix's tooltip trigger clones its child, and a
  // Popover root is a component, so the tip would anchor to nothing. The
  // button's aria-label carries the same words for screen readers.
  return jsx(SDK_POPOVER.root, {
    open: Boolean(open),
    onOpenChange: next => {
      if (next) {
        hapticTap()
        void refreshStatus()
      }
      $chipOpen.set(Boolean(next))
    },
    children: [
      jsx(SDK_POPOVER.trigger, { asChild: true, children: button }),
      jsx(SDK_POPOVER.content, {
        side: 'top',
        align: 'end',
        className: 'w-72 p-3',
        children: jsx(SpeakerControls, { compact: true })
      })
    ]
  })
}

// ── composer: per-conversation speaker ───────────────────────────────────────
// Its own destination, not the adapter's bound device: a chat can talk through
// the living-room speaker while the bridge page keeps talking through another,
// and Off silences one chat without touching the plug-in's settings.
//
// Destinations are held PER SESSION. A single shared value made every composer
// show whichever chat was selected last, and because a Select only reports a
// change when the value actually differs, picking the same speaker again was a
// no-op — which is what made a second conversation look locked out.
const $speakerDestinations = atom({}) // session id -> entry, or null for a known Off
const $speakerDevices = atom([])
const $speakerNotice = atom('')

/** A choice made in a chat that has no id yet (a fresh draft). */
let pendingChoice = null
const PENDING_CHOICE_TTL_MS = 10 * 60 * 1000

async function readDestination(session) {
  const result = await bridgeApi(`/destination?session=${encodeURIComponent(session)}`)
  return result && result.destination ? result.destination : null
}

async function readSpeakerDevices(force) {
  if (!force && $speakerDevices.get().length) return $speakerDevices.get()
  const result = await bridgeApi('/devices')
  const devices = (result && result.devices) || []
  $speakerDevices.set(devices)
  return devices
}

/** The gateway reports turns by the runtime id; a resumed chat also has a
 *  durable one. Storing both keeps the choice attached across a resume. */
function composerSessionIds(session) {
  const ids = [String(session || '')].filter(Boolean)
  try {
    const ref = host.state && host.state.focusedStoredSessionId
    const stored = ref && typeof ref.get === 'function' ? String(ref.get() || '') : ''
    if (stored && !ids.includes(stored)) ids.push(stored)
  } catch {
    /* an older shell without the durable id */
  }
  return ids
}

async function chooseSpeaker(session, value) {
  if (value === '__refresh') {
    try {
      await readSpeakerDevices(true)
      $speakerNotice.set('')
    } catch (error) {
      $speakerNotice.set(errorText(error, 'Could not refresh the speakers'))
    }
    return
  }

  const ids = composerSessionIds(session)
  if (!ids.length) {
    // A fresh draft has no id to key on yet: hold the choice and apply it to
    // the conversation that starts here, so the control works before send.
    pendingChoice = value === 'off' ? null : { value, at: Date.now() }
    $speakerNotice.set(value === 'off' ? '' : 'Applies to this chat as soon as it starts')
    hapticTap()
    return
  }

  const device = $speakerDevices.get().find(item => (item.address || item.id) === value) || {}
  const next = { ...$speakerDestinations.get() }
  try {
    for (const id of ids) {
      const body = value === 'off'
        ? { session: id, mode: 'off' }
        // The friendly name is what the chip and the "Spoken on …" toast say;
        // the address is only the routing key.
        : { session: id, mode: 'speaker', device: value, name: speakerName(device) }
      const result = await bridgeApi('/destination', { body, method: 'POST' })
      next[id] = result.destination || null
    }
    pendingChoice = null
    $speakerDestinations.set(next)
    $speakerNotice.set('')
    hapticTap()
  } catch (error) {
    const message = errorText(error, 'Could not set the speaker')
    $speakerNotice.set(message)
    say(message)
  }
}

function ComposerSpeaker() {
  const runtimeId = useValue(host.state.focusedSessionId)
  const destinations = useValue($speakerDestinations)
  const devices = useValue($speakerDevices)
  const notice = useValue($speakerNotice)
  const hidden = useValue($composerHidden)
  const profileInfo = useValue($profileInfo)
  const activeProfile = useValue($activeProfile)
  const [storedId, setStoredId] = useState('')
  // The picker is also the closest thing to a transport control: while audio is
  // audible (including a cron delivery this conversation never chose), show the
  // active device and offer a stop without leaving the composer.
  const playback = usePlaybackPolling(1500)
  const speaking = Boolean(playback && playback.speaking)
  const speakingDevice = (() => {
    const names = (playback && playback.devices) || []
    return withoutAddress(names.length ? String(names[0]) : '').trim()
  })()

  // A draft (or a session whose runtime is not bound yet) has no runtime id; the
  // durable id is the fallback. Subscribed by hand, because an older shell may
  // not have the atom at all and `useValue(undefined)` would throw.
  useEffect(() => {
    const ref = host.state && host.state.focusedStoredSessionId
    if (!ref || typeof ref.get !== 'function') {
      setStoredId('')
      return undefined
    }
    const read = () => setStoredId(String(ref.get() || ''))
    read()
    return typeof ref.subscribe === 'function' ? ref.subscribe(read) : undefined
  }, [runtimeId])

  // Only THIS conversation's entry is ever displayed. Several conversations can
  // hold the same speaker at once; the picker must never borrow another's value.
  const session = String(runtimeId || storedId || '')
  const destination = session ? destinations[session] || null : null
  const pendingValue = pendingChoice ? pendingChoice.value : ''
  const on = Boolean(destination && destination.mode === 'speaker')
  const value = on ? String(destination.device || '') : pendingValue || 'off'
  // The row shows a name, not a network address: "Google Home Mini", not
  // "Google Home Mini (192.168.1.145)". The menulist below keeps the full
  // label, so two identical speakers are still distinguishable when picking.
  const selected = on
    ? devices.find(device => String(device.address || device.id || '') === value) || null
    : null
  const label = on
    ? speakerName(selected) || withoutAddress(destination.name || destination.device || '') || 'Speaker'
    : pendingValue
      ? 'Starts with this chat'
      : 'Off'
  // A cron delivery has no per-session destination — the job chooses the
  // speaker, not the composer. Reflect the sound that is actually playing
  // instead of insisting the conversation is Off while the room is talking.
  const displayLabel = speaking
    ? `${on ? label : speakingDevice || 'Speaker'} · speaking`
    : label

  useEffect(() => {
    if (!session) return undefined
    let alive = true
    ;(async () => {
      try {
        const [entry] = await Promise.all([readDestination(session), readSpeakerDevices(false)])
        if (!alive) return
        $speakerDestinations.set({ ...$speakerDestinations.get(), [session]: entry })
        $speakerNotice.set('')
        const held = pendingChoice
        if (!entry && held && Date.now() - held.at < PENDING_CHOICE_TTL_MS) {
          pendingChoice = null
          void chooseSpeaker(session, held.value)
        }
      } catch (error) {
        if (!alive) return
        $speakerDestinations.set({ ...$speakerDestinations.get(), [session]: null })
        $speakerNotice.set(errorText(error, 'The Google Home bridge is not running'))
      }
    })()
    return () => {
      alive = false
    }
  }, [session])

  useEffect(() => {
    void refreshProfileInfo()
  }, [])

  // Hidden from the chip's popover: the conversation keeps whichever speaker it
  // already chose, the composer just stops spending a control on it. The
  // popover is the way back, so this can never strand the choice.
  //
  // Also absent where the profile has no plugin: this picker writes a
  // destination, and the turn-end hook that reads it is registered by the
  // plugin in *this* profile. Showing it elsewhere would be a no-op control.
  // An older bridge has no /profiles answer, so `known` stays false and the
  // picker keeps its previous forgiving behaviour.
  const profileName = String(activeProfile || '').trim().toLowerCase()
  const speaksHere = !profileInfo.known || !profileName || profileInfo.plugin_profiles.includes(profileName)
  if (hidden || !speaksHere) return null

  const Icon = SDK_ICONS && (on || pendingValue || speaking ? SDK_ICONS.Volume2 : SDK_ICONS.VolumeX)

  const items = [
    ...(speaking
      ? [jsx(SelectItem, {
          key: '__stop',
          value: '__stop',
          children: '⏹ Stop speaking'
        })]
      : []),
    jsx(SelectItem, { key: 'off', value: 'off', children: 'Off — stay silent' }),
    ...devices.map(device => {
      const key = String(device.address || device.id || '')
      return jsx(SelectItem, { key, value: key, children: speakerLabel(device) || key || 'Speaker' })
    }),
    jsx(SelectItem, { key: '__refresh', value: '__refresh', children: 'Refresh speakers…' })
  ]
  if (on && value && !devices.some(device => (device.address || device.id) === value)) {
    items.splice(speaking ? 2 : 1, 0, jsx(SelectItem, { key: value, value, children: label }))
  }

  const select = jsxs(Select, {
    value,
    onValueChange: next => {
      if (next === '__stop') {
        void stopSpeaking()
        return
      }
      pendingChoice = null
      void chooseSpeaker(session, next)
    },
    onOpenChange: open => {
      // Discover when the list is empty: an empty menu must never be the reason
      // a conversation cannot be given a speaker.
      if (!open) return
      void readSpeakerDevices(!$speakerDevices.get().length).catch(() => {})
    },
    children: [
      jsx(SelectTrigger, {
        className: 'h-7 w-44 text-xs',
        // Explicit children, so the trigger shows the short name even though the
        // rows it selects from carry the address as well.
        children: jsx(SelectValue, { placeholder: 'Off', children: displayLabel })
      }),
      jsx(SelectContent, { children: items })
    ]
  })

  const stopButton = speaking
    ? jsx(Button, {
        variant: 'ghost',
        className: 'h-7 px-2 text-xs',
        title: 'Stop the speaker',
        'aria-label': 'Stop playback on the speaker',
        onClick: () => {
          hapticTap()
          void stopSpeaking()
        },
        children: '⏹'
      })
    : null

  const control = Icon
    ? jsxs('div', {
        className: 'flex items-center gap-1.5',
        children: [
          jsx(Icon, { className: 'size-4 shrink-0 text-(--ui-text-secondary)' }),
          select,
          stopButton
        ]
      })
    : jsxs('div', { className: 'flex items-center gap-1.5', children: [select, stopButton] })

  return jsx(Tip, {
    label: notice || (speaking
      ? `Audio is playing on ${speakingDevice || 'the speaker'} — stop it here`
      : `Speak this conversation's replies on ${label}`),
    children: jsx('div', { className: 'flex items-center', children: control })
  })
}

// ── replies spoken in other conversations ────────────────────────────────────
/** One flat sentence a toast can show: the reply's opening, not a wall of text. */
function previewText(text, max) {
  const flat = String(text || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[#*_>`]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  if (!flat) return ''
  const limit = max || 140
  const clipped = flat.length > limit ? flat.slice(0, limit) : flat
  const first = /^(.*?[.!?])(\s|$)/.exec(clipped)
  // A three-word opener ("Done.") tells the user nothing, so keep the longer
  // opening instead of stopping at it.
  const sentence = first && first[1].length >= 40 ? first[1] : clipped
  if (sentence.length >= flat.length) return sentence
  return /[.!?…]$/.test(sentence) ? sentence : `${sentence.trimEnd()}…`
}

/** This conversation's destination, from the cache or asked for once. */
async function sessionDestination(session) {
  const map = $speakerDestinations.get()
  if (Object.prototype.hasOwnProperty.call(map, session)) return map[session]
  try {
    const entry = await readDestination(session)
    $speakerDestinations.set({ ...$speakerDestinations.get(), [session]: entry })
    return entry
  } catch {
    return null
  }
}

/** Say so when a reply the user is not looking at was spoken out loud. */
function announceOtherSession(event) {
  try {
    if (!event || event.replayed) return
    const payload = event.payload || {}
    if (payload.status && payload.status !== 'complete') return
    const session = String(event.session_id || '')
    if (!session) return
    const focused = String(host.state.focusedSessionId.get() || '')
    const storedRef = host.state && host.state.focusedStoredSessionId
    const focusedStored = storedRef && typeof storedRef.get === 'function' ? String(storedRef.get() || '') : ''
    if (session === focused || session === focusedStored) return
    const line = previewText(typeof payload.text === 'string' ? payload.text : '')
    if (!line) return
    void sessionDestination(session).then(entry => {
      if (!entry || entry.mode !== 'speaker') return
      const where = entry.name || entry.device || 'the speaker'
      host.notify({
        kind: 'info',
        title: `Spoken on ${where}`,
        message: line,
        icon: 'broadcast',
        durationMs: 8000
      })
    })
  } catch {
    /* a listener must never break the app's own event dispatch */
  }
}

// ── plugin ───────────────────────────────────────────────────────────────────
export default {
  id: PLUGIN_ID,
  name: 'Google Home',
  description: 'Talk to a Hermes profile through a Google Home / Nest speaker.',
  defaultEnabled: true,

  register(ctx) {
    $status.set({ kind: 'idle' })
    pluginCtx = ctx
    pluginStorage = ctx.storage || null
    $composerHidden.set(readComposerHidden())

    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: ROUTE },
      render: () => jsx(BridgePage, {})
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 65,
      data: { path: ROUTE, label: 'Google Home', codicon: 'broadcast', tier: 'advanced' }
    })

    ctx.registerMany([
      {
        id: 'open',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.open`,
          label: 'Open Google Home bridge',
          keywords: ['speaker', 'cast', 'voice', 'talk', 'nest'],
          run: () => host.navigate(ROUTE)
        }
      },
      {
        id: 'talk',
        area: PALETTE_AREA,
        data: {
          id: `${PLUGIN_ID}.talk`,
          label: 'Talk to the speaker',
          keywords: ['voice', 'record', 'microphone'],
          run: () => {
            host.navigate(ROUTE)
            void startRecording()
          }
        }
      }
    ])

    if (STATUSBAR_AREAS && STATUSBAR_AREAS.right) {
      ctx.register({
        id: 'chip',
        area: STATUSBAR_AREAS.right,
        order: 140,
        render: () => jsx(StatusChip, {})
      })
    }

    if (COMPOSER_AREAS && COMPOSER_AREAS.actions && host.state && host.state.focusedSessionId) {
      ctx.register({
        id: 'composer-speaker',
        area: COMPOSER_AREAS.actions,
        order: 5,
        render: () => jsx(ComposerSpeaker, {})
      })
    }

    // A reply spoken while the user is looking at another conversation should
    // not be a mystery sound in the next room: say which speaker said it.
    if (typeof ctx.onEvent === 'function') {
      ctx.onEvent('message.complete', announceOtherSession)
    }

    // The speaker's session is an ordinary Hermes chat (source "googlehome"),
    // so it lives in Recents under a title taken from its first message. A
    // badge is the only thing that says "this one is your speaker", and it is
    // applied only to sessions the routed profile's store actually holds.
    if (SESSION_ROW_AREAS && SESSION_ROW_AREAS.trailing) {
      ctx.register({
        id: 'session-badge',
        area: SESSION_ROW_AREAS.trailing,
        // Row slots carry their renderer in ``data``, not at the top level.
        data: {
          render: props => jsx(GoogleHomeRowBadge, { sessionId: props && props.sessionId })
        }
      })
    }
    if (typeof host.listPersistedSessions === 'function') {
      void loadConversations({ force: true })
    }

    if (typeof ctx.registerSettingsPage === 'function') {
      ctx.registerSettingsPage({
        id: 'settings',
        title: 'Google Home',
        icon: 'broadcast',
        render: () => jsx(SettingsPage, {})
      })
    }

    ctx.onDispose(() => {
      if (pollTimer) clearInterval(pollTimer)
      pollTimer = null
      if (recorder) {
        try {
          recorder.stop()
        } catch {
          /* already stopped */
        }
        recorder = null
      }
    })
  }
}
