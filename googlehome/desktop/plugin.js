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
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PLUGIN_ID = 'googlehome'
const ROUTE = '/googlehome'
const CONTROL_TTL_MS = 5000
const STATUS_POLL_MS = 3000

const $status = atom({ kind: 'idle' })
const $devices = atom([])
const $profiles = atom([])
const $recording = atom(false)
const $busy = atom(false)
const $transcript = atom('')
const $reply = atom('')
const $notice = atom('')

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
  const name = device.name || device.address || 'Speaker'
  if (status.speaking) return `${name} · speaking`
  return `${name} · ready`
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

function StatusHeader() {
  const statusState = useValue($status)
  const notice = useValue($notice)
  const status = (statusState && statusState.status) || {}
  const device = status.device || {}

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
      device.address
        ? jsx('span', { className: 'text-xs text-(--ui-text-tertiary)', children: `${device.address}${device.model ? ` · ${device.model}` : ''}` })
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
                        children: `${row.name || row.address}${row.model ? ` · ${row.model}` : ''}`
                      },
                      row.id || row.address
                    )
                  )
                : jsx(SelectItem, { value: deviceValue, children: device.name || deviceValue || 'No device found' })
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
              ? devices.map(row => `${row.name || row.address} (${row.address})`).join(', ')
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

  useEffect(() => {
    void refreshStatus()
    const timer = setInterval(() => void refreshStatus(), STATUS_POLL_MS * 3)
    return () => clearInterval(timer)
  }, [])

  const status = (statusState && statusState.status) || {}
  const device = status.device || {}
  const ready = statusState && statusState.kind === 'ready' && device.reachable
  const label = recording ? 'listening' : ready ? 'speaker' : 'speaker off'

  const button = jsxs('button', {
    type: 'button',
    className:
      'inline-flex h-full items-center gap-1 px-1.5 text-[0.6875rem] text-(--ui-text-tertiary) hover:text-(--ui-text-secondary)',
    onClick: () => {
      hapticTap()
      host.navigate(ROUTE)
    },
    children: [
      jsx('span', { 'aria-hidden': true, children: recording ? '🔴' : ready ? '🔊' : '🔇' }),
      jsx('span', { children: label })
    ]
  })

  return jsx(Tip, { label: 'Google Home bridge', children: button })
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
