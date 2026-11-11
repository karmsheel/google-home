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
 * Transport: the Python adapter writes <HERMES_HOME>/googlehome/control.json
 * with {port, token}; we read it through the Electron bridge and talk to that
 * loopback port directly.
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
const CONTROL_TTL_MS = 5000
const STATUS_POLL_MS = 3000

// Read through the namespace import so an older shell without the composer
// seam simply hides the control instead of failing to load the module.
const COMPOSER_AREAS = sdk.COMPOSER_AREAS
const SDK_ICONS = sdk.icons

const $status = atom({ kind: 'idle' })
const $devices = atom([])
const $profiles = atom([])
const $recording = atom(false)
const $busy = atom(false)
const $transcript = atom('')
const $reply = atom('')
const $notice = atom('')
// Cheap one-second poll of /playback. It says whether agent audio is audible
// right now, which is what turns the status chip into a stop control.
const $playback = atom({ speaking: false, kind: 'idle' })

let control = { info: null, at: 0 }
let recorder = null
let chunks = []
let noticeTimer = null
let pollTimer = null

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

function dirname(path) {
  const trimmed = String(path || '').replace(/[\\/]+$/, '')
  const index = Math.max(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
  return index > 0 ? trimmed.slice(0, index) : ''
}

function joinPath(base, parts) {
  const separator = base.includes('\\') ? '\\' : '/'
  return [String(base).replace(/[\\/]+$/, ''), ...parts].join(separator)
}

async function controlInfo(force) {
  const now = Date.now()
  if (!force && control.info && now - control.at < CONTROL_TTL_MS) return control.info
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop : null
  if (!bridge || typeof bridge.desktopPluginsRoot !== 'function') {
    control = { info: null, at: now }
    return null
  }
  let root = ''
  try {
    root = await bridge.desktopPluginsRoot()
  } catch {
    root = ''
  }
  const home = root ? dirname(root) : ''
  const activeProfile = host.state && host.state.profile ? host.state.profile.get() : ''
  const candidates = []
  if (home) {
    candidates.push(joinPath(home, ['googlehome', 'control.json']))
    if (activeProfile) {
      candidates.push(joinPath(home, ['profiles', activeProfile, 'googlehome', 'control.json']))
    }
  }
  for (const path of candidates) {
    try {
      const result = typeof bridge.readFileText === 'function' ? await bridge.readFileText(path) : null
      if (result && result.text && !result.truncated) {
        const info = JSON.parse(result.text)
        control = { info, at: now }
        return info
      }
    } catch {
      /* try the next candidate */
    }
  }
  control = { info: null, at: now }
  return null
}

async function bridgeApi(path, options) {
  const opts = options || {}
  const info = opts.info || (await controlInfo())
  if (!info || !info.port) {
    throw new Error(
      'The Google Home bridge is not connected. Install the plugin, start the Hermes gateway, then retry.'
    )
  }
  const headers = { 'Content-Type': 'application/json' }
  if (info.token) headers['X-Hermes-Token'] = info.token
  const response = await fetch(`http://127.0.0.1:${info.port}${path}`, {
    method: opts.method || 'GET',
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined
  })
  let data = {}
  try {
    data = await response.json()
  } catch {
    data = {}
  }
  if (!response.ok) throw new Error(data.error || `Bridge responded ${response.status}`)
  return data
}

async function refreshStatus() {
  try {
    const status = await bridgeApi('/status')
    $status.set({ kind: 'ready', status })
  } catch (error) {
    $status.set({ kind: 'error', message: errorText(error, 'Bridge unreachable') })
  }
}

async function refreshPlayback() {
  try {
    $playback.set({ ...(await bridgeApi('/playback')), kind: 'ready' })
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
  const bridge = typeof window !== 'undefined' ? window.hermesDesktop : null
  try {
    if (bridge && typeof bridge.requestMicrophoneAccess === 'function') {
      await bridge.requestMicrophoneAccess()
    }
  } catch {
    /* the getUserMedia call below is the real gate */
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
      body: { data_url: dataUrl, mime_type: blob.type }
    })
    if (result.transcript) {
      $transcript.set(result.transcript)
      $reply.set('')
      hapticTap()
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
  try {
    await bridgeApi('/ask', { method: 'POST', body: { text: trimmed } })
    $transcript.set(trimmed)
    say('Sent to the speaker')
  } catch (error) {
    say(errorText(error, 'Could not send that'))
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
  const label = recording ? 'Stop and send' : 'Hold a conversation'

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
          status.profile ? jsx(Chip, { children: `profile: ${status.profile}` }) : null,
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
  const profiles = useValue($profiles)
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

  const bindProfile = async value => {
    hapticTap()
    try {
      await bridgeApi('/profile', { method: 'POST', body: { profile: value } })
      await refreshStatus()
      say('Profile updated')
    } catch (error) {
      say(errorText(error, 'Could not change the profile'))
    }
  }

  const deviceValue = device.address || device.host || ''
  const profileValue = status.profile || ''

  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsx(ListRow, {
        title: 'Speaker',
        description: 'The device that will voice this profile.',
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
        title: 'Hermes profile',
        description: 'Who answers when you talk to the speaker.',
        action: jsxs(Select, {
          value: profileValue,
          onValueChange: value => void bindProfile(value),
          children: [
            jsx(SelectTrigger, { className: 'w-56', children: jsx(SelectValue, { placeholder: 'Gateway default' }) }),
            jsx(SelectContent, {
              children: profiles.map(name =>
                jsx(SelectItem, { value: name, children: name }, name)
              )
            })
          ]
        })
      })
    ]
  })
}

function Conversation() {
  const transcript = useValue($transcript)
  const reply = useValue($reply)
  const busy = useValue($busy)

  if (!transcript && !reply) {
    return jsx('p', {
      className: 'text-sm text-(--ui-text-tertiary)',
      children:
        'Press the microphone, say what you want, then press it again. The speaker answers out loud.'
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
      busy
        ? jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: 'Working…' })
        : null
    ]
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
          'h-8 flex-1 rounded-md border border-(--ui-border) bg-transparent px-2 text-sm text-(--ui-text-primary) outline-none focus:border-(--ui-accent)',
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
      await bridgeApi('/refresh', { method: 'POST', body: {} })
    } catch {
      /* refresh is best effort */
    }
    await refreshStatus()
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-4 overflow-y-auto p-4',
    children: [
      jsx(StatusHeader, {}),
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
      jsx('div', { className: 'my-1 h-px bg-(--ui-border)' }),
      jsx(ConnectionRow, {}),
      statusState && statusState.kind === 'error'
        ? jsx('p', { className: 'text-xs text-(--ui-red)', children: statusState.message })
        : null
    ]
  })
}

function SettingsPage() {
  const statusState = useValue($status)
  const devices = useValue($devices)
  const profiles = useValue($profiles)

  useEffect(() => {
    void refreshStatus()
    void loadDevices()
    void loadProfiles()
  }, [])

  const info = control.info

  return jsxs('div', {
    className: 'flex flex-col gap-3',
    children: [
      jsx(StatusHeader, {}),
      jsx(ConnectionRow, {}),
      jsxs('div', {
        className: 'flex flex-col gap-1 text-xs text-(--ui-text-tertiary)',
        children: [
          jsx('span', {
            children: info
              ? `Bridge control API on 127.0.0.1:${info.port} · ${String(info.running ? 'running' : 'stopped')}`
              : 'Bridge control file not found — start the gateway after installing the agent plugin.'
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
          jsx('span', { children: 'Volume, spoken-reply length and the control port are in the agent plugin settings (Capabilities → Plugins → Google Home). ' }),
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

function StatusChip() {
  const statusState = useValue($status)
  const recording = useValue($recording)
  const playback = usePlaybackPolling(1500)

  useEffect(() => {
    void refreshStatus()
    const timer = setInterval(() => void refreshStatus(), STATUS_POLL_MS * 3)
    return () => clearInterval(timer)
  }, [])

  const status = (statusState && statusState.status) || {}
  const device = status.device || {}
  const ready = statusState && statusState.kind === 'ready' && device.reachable
  const speaking = Boolean(playback && playback.speaking)
  const stopping = Boolean(playback && playback.stopping)
  const label = speaking
    ? stopping
      ? 'stopping'
      : 'stop'
    : recording
      ? 'listening'
      : ready
        ? 'speaker'
        : 'speaker off'

  // While audio is audible the chip's job changes: the click that used to open
  // the page now silences the speaker. That is the whole point of the control —
  // a long or already-read reply must be interruptible from anywhere.
  const button = jsxs('button', {
    type: 'button',
    disabled: stopping,
    'aria-label': speaking ? 'Stop playback on the speaker' : 'Open the Google Home bridge',
    className: speaking
      ? 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-red) hover:text-(--ui-text-primary)'
      : 'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
    onClick: () => {
      if (speaking) void stopSpeaking()
      else {
        hapticTap()
        host.navigate(ROUTE)
      }
    },
    children: [
      jsx('span', {
        'aria-hidden': true,
        children: speaking ? '⏹' : recording ? '🔴' : ready ? '🔊' : '🔇'
      }),
      jsx('span', { children: label })
    ]
  })

  return jsx(Tip, {
    label: speaking ? 'Stop the speaker' : 'Google Home bridge',
    children: button
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
