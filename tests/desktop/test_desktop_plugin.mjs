// Loads googlehome/desktop/plugin.js the way Hermes Desktop does — as a single
// ESM module whose only resolvable specifiers are the SDK, react and
// jsx-runtime — then exercises register() and every render path.
//
//   node tests/desktop/test_desktop_plugin.mjs
import { readFile, writeFile, mkdtemp } from 'node:fs/promises'
import { existsSync, readdirSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'
import coverage from './class-coverage.cjs'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = join(here, '..', '..')
const sourcePath = join(repoRoot, 'googlehome', 'desktop', 'plugin.js')

const ALLOWED = new Set(['@hermes/plugin-sdk', 'react', 'react/jsx-runtime'])
const stubs = {
  '@hermes/plugin-sdk': pathToFileURL(join(here, 'sdk-stub.mjs')).href,
  react: pathToFileURL(join(here, 'react-stub.mjs')).href,
  'react/jsx-runtime': pathToFileURL(join(here, 'jsx-stub.mjs')).href
}

const results = []
function check(name, fn) {
  try {
    fn()
    results.push({ name, ok: true })
    console.log(`  ok  ${name}`)
  } catch (error) {
    results.push({ name, ok: false, error })
    console.log(`FAIL  ${name}\n      ${error && error.message}`)
  }
}

const source = await readFile(sourcePath, 'utf8')

// ── static loader rules ──────────────────────────────────────────────────────
console.log('runtime-loader rules')

check('only SDK/react/jsx-runtime specifiers', () => {
  const found = [...source.matchAll(/from\s+['"]([^'"]+)['"]/g)].map(match => match[1])
  const staticImports = [...source.matchAll(/\bimport\s+(\*|[^'"\s]+)[^'"]*?from\s+['"]([^'"]+)['"]/g)]
  for (const match of staticImports) {
    assert.ok(ALLOWED.has(match[2]), `disallowed specifier: ${match[2]}`)
  }
  assert.ok(found.length > 0, 'expected static imports')
})

check('no dynamic import()', () => {
  assert.equal(/\bimport\s*\(/.test(source), false, 'dynamic import is rejected by the loader')
})

check('no JSX syntax', () => {
  assert.equal(/<[A-Z][A-Za-z0-9]*[\s/>]/.test(source), false, 'JSX is a syntax error in a runtime plugin')
})

check('the renderer has no direct bridge transport', () => {
  // ctx.rest is the only door: the backend owns control.json and the token.
  for (const forbidden of [
    'hermesDesktop',
    'readFileText',
    'requestMicrophoneAccess',
    'control.json',
    'X-Hermes-Token',
    'fetch('
  ]) {
    assert.equal(
      source.includes(forbidden),
      false,
      `plugin.js must not use ${forbidden}; call ctx.rest instead`
    )
  }
})

check('single default-exported plugin object', () => {
  assert.match(source, /export default\s*\{/)
})

// Tailwind is compiled into the Desktop at build time; the plugin loads at
// runtime, so a class the build never saw silently does nothing. Check against
// the installed build when this machine has one, and say so when it does not.
const compiledCss = (() => {
  const home = process.env.HERMES_HOME
  if (!home) return ''
  const assets = join(home, 'hermes-agent', 'apps', 'desktop', 'dist', 'assets')
  try {
    const files = readdirSync(assets).filter(name => name.endsWith('.css')).map(name => join(assets, name))
    return files.sort((a, b) => statSync(b).size - statSync(a).size)[0] || ''
  } catch {
    return ''
  }
})()

check('every plugin class exists in the compiled Desktop CSS', () => {
  if (!compiledCss) {
    console.log('      (no compiled Desktop CSS found; skipped)')
    return
  }
  const missing = coverage.missingClasses(compiledCss, sourcePath)
  assert.deepEqual(missing, [], `classes the build never emitted: ${missing.join(', ')}`)
})

// ── load it like the host ────────────────────────────────────────────────────
const rewritten = source
  .replace(/from\s+'@hermes\/plugin-sdk'/g, `from '${stubs['@hermes/plugin-sdk']}'`)
  .replace(/from\s+'react'/g, `from '${stubs.react}'`)
  .replace(/from\s+'react\/jsx-runtime'/g, `from '${stubs['react/jsx-runtime']}'`)

const dir = await mkdtemp(join(tmpdir(), 'gh-desktop-'))
const modulePath = join(dir, 'plugin.mjs')
// The runtime plugin only exports its default object; the harness adds the
// internals it drives so behaviour is exercised through the real code paths
// rather than re-implemented here.
const TEST_EXPORTS =
  '\nexport { askText, $status, $reply, $replyError, $waiting, $conversations, $conversationIds, $composerHidden, $profileInfo, $speakerNotice, refreshProfileInfo, refreshPlayback }\n'
await writeFile(modulePath, rewritten + TEST_EXPORTS, 'utf8')
const mod = await import(pathToFileURL(modulePath).href)
const plugin = mod.default

console.log('\nplugin contract')
check('has id/name/register', () => {
  assert.equal(typeof plugin.id, 'string')
  assert.equal(plugin.id, 'googlehome')
  assert.equal(typeof plugin.name, 'string')
  assert.equal(typeof plugin.register, 'function')
})

// ── register() ───────────────────────────────────────────────────────────────
const contributions = []
const settingsPages = []
const disposers = []
// Plugin-scoped persistence, mirroring the host's namespaced store.
const storage = new Map()

// ── the backend the plugin reaches through ctx.rest ──────────────────────────
// `ctx.rest` is the plugin's own namespace (/api/plugins/googlehome/…); the
// Python plugin_api.py proxies to the bridge. The harness answers that door,
// so plugin.js must not know a port, a token, or a file path.
let replyPolls = 0
let failNextAsk = null
let profilesResponse = {
  ok: true,
  bridge_profile: 'default',
  plugin_profiles: ['default', 'coder']
}
// What the control API answers on the poll that also carries unspoken replies.
let playbackResponse = { ok: true, speaking: false, notices: [] }
const bridgeCalls = []

function bridgeResponse(rawPath, options) {
  const opts = options || {}
  const path = '/' + String(rawPath || '').replace(/^\/+/, '')
  bridgeCalls.push(path)
  let body = { ok: true }
  if (path.startsWith('/ask')) {
    if (failNextAsk) {
      body = { ok: true, submitted: false, error: failNextAsk }
      failNextAsk = null
    } else {
      body = { ok: true, submitted: true, token: 'turn-1', text: 'hello' }
    }
  } else if (path.startsWith('/reply')) {
    replyPolls += 1
    body =
      replyPolls < 2
        ? { ok: true, done: false, reply: '', error: '' }
        : { ok: true, done: true, reply: 'Hello from the speaker.', error: '' }
  } else if (path.startsWith('/profiles')) {
    body = profilesResponse
  } else if (path.startsWith('/playback')) {
    body = playbackResponse
  }
  if (opts.body !== undefined && opts.method === 'POST') {
    // A POST must reach the backend as a body, not a query string.
    assert.equal(typeof opts.body, 'object', `POST ${path} needs a JSON body`)
  }
  return body
}

const ctx = {
  storage: {
    get: (_key, fallback) => (storage.has(_key) ? storage.get(_key) : fallback),
    set: (_key, value) => storage.set(_key, value),
    remove: _key => storage.delete(_key)
  },
  register(contribution) {
    contributions.push(contribution)
    return () => {}
  },
  registerMany(list) {
    for (const contribution of list) contributions.push(contribution)
    return () => {}
  },
  registerSettingsPage(page) {
    settingsPages.push(page)
    return () => {}
  },
  onDispose(fn) {
    disposers.push(fn)
  },
  onEvent() {
    return () => {}
  },
  onCleanup(fn) {
    disposers.push(fn)
  },
  setInterval() {
    return () => {}
  },
  setTimeout() {
    return () => {}
  },
  addEventListener() {
    return () => {}
  },
  rest: (path, options) => bridgeResponse(path, options),
  socket: () => () => {},
  os: {},
  i18n: {},
  pet: {}
}

console.log('\nregister()')
check('register() does not throw', () => {
  plugin.register(ctx)
})

const byArea = area => contributions.filter(contribution => contribution.area === area)

check('registers a route page', () => {
  const routes = byArea('routes')
  assert.equal(routes.length, 1)
  assert.equal(routes[0].data.path, '/googlehome')
  assert.equal(typeof routes[0].render, 'function')
})

check('registers a sidebar nav row', () => {
  const nav = byArea('sidebar.nav')
  assert.equal(nav.length, 1)
  assert.equal(nav[0].data.path, '/googlehome')
  assert.equal(nav[0].data.label, 'Google Home')
})

check('registers palette commands', () => {
  const palette = byArea('palette')
  assert.ok(palette.length >= 2)
  for (const entry of palette) {
    assert.ok(entry.data.id.startsWith('googlehome.'))
    assert.equal(typeof entry.data.run, 'function')
  }
})

check('registers a status bar chip', () => {
  const chip = byArea('statusBar.right')
  assert.equal(chip.length, 1)
  assert.equal(typeof chip[0].render, 'function')
})

check('registers the composer speaker control', () => {
  const composer = byArea('composer.actions')
  assert.ok(composer.length >= 1)
  assert.equal(composer[0].id, 'composer-speaker')
  assert.equal(typeof composer[0].render, 'function')
})

check('registers a settings page', () => {
  assert.equal(settingsPages.length, 1)
  assert.equal(settingsPages[0].title, 'Google Home')
})

check('registers a session-row badge', () => {
  const badge = byArea('sessionRow.trailing')
  assert.equal(badge.length, 1)
  // A row slot carries its renderer in `data` (see SessionRowSlot in the shell).
  assert.equal(typeof badge[0].data?.render, 'function')
})

check('registers a disposer', () => {
  assert.ok(disposers.length >= 1)
})

// ── render every surface ─────────────────────────────────────────────────────
function walk(node, depth) {
  if (node === null || node === undefined || depth > 40) return 0
  if (Array.isArray(node)) {
    let total = 0
    for (const child of node) total += walk(child, depth + 1)
    return total
  }
  if (typeof node === 'string' || typeof node === 'number') return 1
  if (typeof node === 'function') {
    return walk(node({}), depth + 1)
  }
  if (typeof node === 'object') {
    if (typeof node.type === 'function') {
      return 1 + walk(node.type(node.props || {}), depth + 1)
    }
    return 1 + walk(node.props && node.props.children, depth + 1)
  }
  return 0
}

console.log('\nrender paths')
function contributionRender(contribution) {
  if (typeof contribution.render === 'function') return contribution.render
  const render = contribution.data && contribution.data.render
  return typeof render === 'function' ? render : null
}
for (const contribution of contributions) {
  const render = contributionRender(contribution)
  if (!render) continue
  if (String(contribution.area).startsWith('sessionRow.')) {
    // A row decoration is handed one session at a time, not rendered bare.
    check(`render ${contribution.area}:${contribution.id} for the speaker's session`, () => {
      const nodes = walk(render({ sessionId: '20261109_200058_ad554bed' }), 0)
      assert.ok(nodes > 0, 'render produced no nodes')
    })
    check(`render ${contribution.area}:${contribution.id} stays out of other rows`, () => {
      const element = render({ sessionId: 'local-chat-1' })
      // The slot wraps the badge component; it must render nothing at all here.
      assert.equal(element.type(element.props), null)
    })
    continue
  }
  check(`render ${contribution.area}:${contribution.id}`, () => {
    const tree = render()
    const nodes = walk(tree, 0)
    assert.ok(nodes > 0, 'render produced no nodes')
  })
}
for (const page of settingsPages) {
  check(`render settings page ${page.id}`, () => {
    const nodes = walk(page.render(), 0)
    assert.ok(nodes > 0, 'settings page produced no nodes')
  })
}

// ── behavior against a stubbed bridge ────────────────────────────────────────
console.log('\nbehaviour against the bridge')
const sdkStub = await import(stubs['@hermes/plugin-sdk'])

async function acheck(name, fn) {
  try {
    await fn()
    results.push({ name, ok: true })
    console.log(`  ok  ${name}`)
  } catch (error) {
    results.push({ name, ok: false, error })
    console.log(`FAIL  ${name}\n      ${error && error.message}`)
  }
}

check('the chip opens a speaker popover instead of navigating', () => {
  const chip = byArea('statusBar.right')[0]
  const tree = chip.render()
  const button = findButton(tree)
  assert.ok(button, 'chip should render a button')
  // The popover trigger owns the click; a bare click must not route away from
  // the chat. The page stays reachable from the sidebar and the palette.
  assert.equal(button.props.onClick, undefined)
  const mute = findNode(tree, node => node.props && node.props['aria-label'] === 'Mute the speaker')
  const hide = findNode(
    tree,
    node => node.props && node.props['aria-label'] === 'Hide the composer speaker picker'
  )
  const volume = findNode(tree, node => node.type === 'input' && node.props && node.props.type === 'range')
  assert.ok(mute, 'the popover should offer a mute switch')
  assert.ok(hide, 'the popover should offer the picker preference')
  assert.ok(volume, 'the popover should offer a volume slider')
})

/** Render components deeply and collect every string they produce. */
function strings(node, depth = 0, out = []) {
  if (node === null || node === undefined || depth > 60) return out
  if (Array.isArray(node)) {
    for (const child of node) strings(child, depth + 1, out)
    return out
  }
  if (typeof node === 'string' || typeof node === 'number') {
    out.push(String(node))
    return out
  }
  if (typeof node === 'function') {
    strings(node({}), depth + 1, out)
    return out
  }
  if (typeof node === 'object') {
    if (typeof node.type === 'function') strings(node.type(node.props || {}), depth + 1, out)
    else strings(node.props && node.props.children, depth + 1, out)
  }
  return out
}

/** First node in a rendered tree matching `predicate`. */
function findNode(node, predicate, depth = 0) {
  if (!node || depth > 60) return null
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findNode(child, predicate, depth + 1)
      if (found) return found
    }
    return null
  }
  if (typeof node === 'object' && typeof node.type === 'function') {
    return findNode(node.type(node.props || {}), predicate, depth + 1)
  }
  if (typeof node !== 'object') return null
  if (predicate(node)) return node
  return findNode(node.props && node.props.children, predicate, depth + 1)
}

/** First plain `<button>` in a rendered tree, through every component layer. */
function findButton(node, depth = 0) {  if (!node || typeof node !== 'object' || depth > 60) return null
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findButton(child, depth + 1)
      if (found) return found
    }
    return null
  }
  if (node.type === 'button') return node
  if (typeof node.type === 'function') return findButton(node.type(node.props || {}), depth + 1)
  return findButton(node.props && node.props.children, depth + 1)
}

/** A ListRow title, whether plain text or wrapped in a `Tip`. */
function titleOf(node) {
  const title = node?.props?.title
  if (typeof title === 'string') return title
  const child = title && title.props && title.props.children
  return child && child.props ? child.props.children : ''
}

/** Every node in a rendered tree matching `predicate`, rendering components. */
function collect(node, predicate, depth = 0, out = []) {
  if (!node || depth > 60) return out
  if (Array.isArray(node)) {
    for (const child of node) collect(child, predicate, depth + 1, out)
    return out
  }
  if (typeof node !== 'object') return out
  if (typeof node.type === 'function') {
    collect(node.type(node.props || {}), predicate, depth + 1, out)
    return out
  }
  if (predicate(node)) out.push(node)
  collect(node.props && node.props.children, predicate, depth + 1, out)
  return out
}

async function until(predicate, timeoutMs = 8000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return true
    await new Promise(resolve => setTimeout(resolve, 40))
  }
  return false
}

await new Promise(resolve => setTimeout(resolve, 80))

await until(() => mod.$conversations.get().kind === 'ready')

await acheck('loads the speaker conversations from the routed profile', () => {
  const state = mod.$conversations.get()
  assert.equal(state.kind, 'ready')
  assert.equal(state.error, '')
  assert.equal(state.rows.length, 1, 'only source=googlehome rows belong here')
  assert.equal(state.rows[0].id, '20261109_200058_ad554bed')
  assert.ok(mod.$conversationIds.get().includes('20261109_200058_ad554bed'))
})

await acheck('a submitted turn is followed to its reply', async () => {
  mod.$reply.set('')
  mod.$replyError.set('')
  replyPolls = 0
  bridgeCalls.length = 0
  await mod.askText('hello')
  const landed = await until(() => mod.$reply.get() === 'Hello from the speaker.')
  assert.ok(landed, `reply never landed (last: ${JSON.stringify(mod.$reply.get())})`)
  assert.ok(bridgeCalls.some(path => path.startsWith('/reply?token=turn-1')))
  assert.equal(mod.$waiting.get(), false)
})

await acheck('the page renders the reply it received', () => {
  const page = byArea('routes')[0]
  const text = strings(page.render()).join('\n')
  assert.match(text, /Hermes said/)
  assert.match(text, /Hello from the speaker\./)
  assert.match(text, /Open the conversation/)
})

await acheck('the page carries the same speaker controls as the popover', () => {
  const tree = byArea('routes')[0].render()
  assert.match(strings(tree).join('\n'), /Speaker controls/)
  assert.ok(
    findNode(tree, node => node.type === 'input' && node.props && node.props.type === 'range'),
    'the page should offer a volume slider'
  )
  assert.ok(
    findNode(tree, node => node.props && node.props['aria-label'] === 'Mute the speaker'),
    'the page should offer a mute switch'
  )
  assert.ok(
    findNode(tree, node => node.props && node.props['aria-label'] === 'Hide the composer speaker picker'),
    'the page should offer the picker preference'
  )
})

await acheck('the page keeps side gutters instead of sitting on the pane edge', () => {
  const tree = byArea('routes')[0].render()
  const padded = findNode(
    tree,
    node =>
      typeof node.props?.className === 'string' &&
      /\bpx-12\b/.test(node.props.className) &&
      /(^|\s)@container(\s|$)/.test(node.props.className)
  )
  assert.ok(padded, 'the page column should be padded and act as the container')
  // Half the empty space of a 48rem column: (pane + 48rem) / 2 = 50% + 24rem.
  assert.match(String(padded.props.style?.maxWidth || ''), /50% \+ 24rem/)
})

await acheck('routing and the speaker controls share a two-column section', () => {
  const tree = byArea('routes')[0].render()
  const grid = findNode(
    tree,
    node => typeof node.props?.className === 'string' && node.props.className.includes('@lg:grid-cols-2')
  )
  assert.ok(grid, 'the page should divide routing and controls into two columns')
  // The titles are wrapped in a Tip, so read the label out of either shape.
  assert.ok(
    findNode(grid, node => titleOf(node) === 'Speaker'),
    'the Speaker setting should sit in the left column'
  )
  assert.ok(
    findNode(grid, node => titleOf(node) === 'Answered by'),
    'the answered-by row should sit in the left column'
  )
  assert.ok(
    findNode(grid, node => node.props && node.props['aria-label'] === 'Mute the speaker'),
    'the speaker controls should sit in the right column'
  )

  // Both columns are cards that stretch to a shared row height and share the
  // same surface.
  const cards = collect(
    grid,
    node =>
      typeof node.props?.className === 'string' &&
      /(^|\s)rounded-lg(\s|$)/.test(node.props.className) &&
      /(^|\s)border(\s|$)/.test(node.props.className)
  )
  assert.equal(cards.length, 2, 'both columns should be boxed')
  for (const card of cards) {
    assert.match(card.props.className, /(^|\s)flex-1(\s|$)/, 'cards should stretch to one height')
    assert.match(card.props.className, /bg-\(--ui-bg-quinary\)/, 'both boxes should share a background')
    assert.match(card.props.className, /border-\(--ui-stroke-secondary\)/, 'both boxes should share a border')
  }
})

await acheck('opening the conversation asks for the routed profile', async () => {
  const opened = []
  const original = sdkStub.host.openSession
  sdkStub.host.openSession = async (...args) => {
    opened.push(args)
  }
  try {
    const page = byArea('routes')[0]
    const row = findNode(
      page.render(),
      node => node.type === 'button' && String(node.props.title || '').startsWith('Open "')
    )
    assert.ok(row, 'the conversation row should be a button')
    row.props.onClick()
    const settled = await until(() => opened.length > 0)
    assert.ok(settled)
    assert.equal(opened[0][0], '20261109_200058_ad554bed')
    assert.deepEqual(opened[0][1], { profile: 'coder' })
  } finally {
    sdkStub.host.openSession = original
  }
})

await acheck('a turn the gateway refuses is reported on the page', async () => {
  mod.$reply.set('')
  mod.$replyError.set('')
  failNextAsk = 'the gateway did not accept the turn'
  await mod.askText('hello')
  assert.match(mod.$replyError.get(), /did not accept/)
})

await acheck('the profile row is a read-only report of the gateway route', () => {
  mod.$status.set({
    kind: 'ready',
    status: {
      profile: 'default',
      routed_profile: 'coder',
      bridge_profile: 'default',
      profile_overridden: true,
      device: { reachable: true, name: 'Google Home Mini', address: '192.168.1.145' }
    }
  })
  const page = byArea('routes')[0].render()
  const text = strings(page).join('\n')
  assert.match(text, /profile: coder · routed/)
  assert.match(text, /bridge: default/)
  // ListRow carries its copy in props; the SDK stub does not render it as text.
  const profileRow = findNode(page, node => titleOf(node) === 'Answered by')
  assert.ok(profileRow, 'the answered-by row should be rendered')
  assert.match(profileRow.props.description, /Gateway profile_routes answers as coder/)
  assert.match(profileRow.props.description, /plugin profile setting \(default\)/)
  assert.equal(profileRow.props.action.props.children, 'coder')
  assert.equal(profileRow.props.action.props.onValueChange, undefined)
})

await acheck('the page says when the viewed profile has no plugin', async () => {
  profilesResponse = {
    ok: true,
    bridge_profile: 'default',
    plugin_profiles: ['default']
  }
  await mod.refreshProfileInfo(true)
  assert.deepEqual(mod.$profileInfo.get().plugin_profiles, ['default'])
  const page = byArea('routes')[0].render()
  const text = strings(page).join('\n')
  assert.match(text, /does not have the Google Home plugin/)
  assert.match(text, /bridge in default/)
  assert.match(text, /install and enable the plugin here/)
})

await acheck('the composer picker stays out of profiles without the plugin', () => {
  mod.$composerHidden.set(false)
  mod.$profileInfo.set({
    known: true,
    bridge_profile: 'default',
    plugin_profiles: ['default', 'designer']
  })
  const composer = byArea('composer.actions')[0]
  assert.equal(composer.render().type({}), null, 'a profile without the plugin has no speech hook')
  mod.$profileInfo.set({
    known: true,
    bridge_profile: 'default',
    plugin_profiles: ['default', 'coder']
  })
  assert.notEqual(composer.render().type({}), null, 'a profile with the plugin keeps the picker')
})

await acheck('the popover mutes through the bridge', async () => {
  mod.$status.set({
    kind: 'ready',
    status: {
      device: { reachable: true, volume: 0.35, name: 'Living Room speaker' },
      settings: { muted: false }
    }
  })
  const chip = byArea('statusBar.right')[0]
  const mute = findNode(chip.render(), node => node.props && node.props['aria-label'] === 'Mute the speaker')
  assert.ok(mute, 'the mute switch should render')
  bridgeCalls.length = 0
  mute.props.onCheckedChange(true)
  assert.ok(
    await until(() => bridgeCalls.includes('/mute')),
    `mute never reached the bridge (${bridgeCalls.join(', ')})`
  )
})

await acheck('the volume slider commits through the bridge', async () => {
  const chip = byArea('statusBar.right')[0]
  const slider = findNode(
    chip.render(),
    node => node.type === 'input' && node.props && node.props.type === 'range'
  )
  assert.ok(slider, 'the volume slider should render')
  bridgeCalls.length = 0
  slider.props.onChange({ target: { value: '55' } })
  assert.ok(
    await until(() => bridgeCalls.includes('/volume')),
    `volume never reached the bridge (${bridgeCalls.join(', ')})`
  )
})

await acheck('a reply that was not spoken is reported by the composer', async () => {
  const seen = []
  const original = sdkStub.host.notify
  sdkStub.host.notify = notice => {
    seen.push(notice)
  }
  try {
    // The first poll adopts what the bridge already knew: opening a chat must not
    // replay every old failure.
    playbackResponse = {
      ok: true,
      speaking: false,
      notices: [
        { seq: 4, session: 'local-other', name: 'Kitchen speaker', at: Date.now() - 600000, error: 'old failure' }
      ]
    }
    await mod.refreshPlayback()
    assert.equal(seen.length, 0, 'history must not be announced')
    assert.equal(mod.$speakerNotice.get(), '')

    playbackResponse = {
      ok: true,
      speaking: false,
      notices: [
        { seq: 4, session: 'local-other', name: 'Kitchen speaker', at: Date.now() - 600000, error: 'old failure' },
        {
          seq: 5,
          session: 'session-1',
          name: 'Living Room speaker',
          at: Date.now(),
          error:
            'No speech backend could speak. hermes: TTS chunk 1 failed (xai): quota. ' +
            'Run `hermes gateway restart`; if it keeps happening, report it with `hermes debug share`.'
        }
      ]
    }
    await mod.refreshPlayback()
    assert.match(mod.$speakerNotice.get(), /Couldn't speak the reply on Living Room speaker/)
    assert.match(mod.$speakerNotice.get(), /TTS chunk 1 failed/)
    assert.equal(seen.length, 1)
    assert.equal(seen[0].kind, 'error')
    assert.equal(seen[0].title, 'Reply not spoken')
    assert.doesNotMatch(seen[0].message, /gateway restart/, 'restart advice is noise in a toast')

    // The same notice must not be announced twice by the next poll.
    await mod.refreshPlayback()
    assert.equal(seen.length, 1)

    // Another conversation's failure is a toast, not this composer's status line.
    playbackResponse = {
      ok: true,
      speaking: false,
      notices: [
        { seq: 5, session: 'session-1', name: 'Living Room speaker', at: Date.now(), error: 'TTS chunk 1 failed' },
        { seq: 6, session: 'local-other', name: 'Kitchen speaker', at: Date.now(), error: 'the cast was refused' }
      ]
    }
    await mod.refreshPlayback()
    assert.equal(seen.length, 2)
    assert.match(seen[1].title, /Kitchen speaker/)
    assert.match(seen[1].message, /cast was refused/)
    assert.match(
      mod.$speakerNotice.get(),
      /Living Room speaker/,
      'another conversation must not overwrite this status line'
    )
  } finally {
    sdkStub.host.notify = original
    playbackResponse = { ok: true, speaking: false, notices: [] }
  }
})

await acheck('hiding the composer picker is remembered and empties the slot', () => {
  const chip = byArea('statusBar.right')[0]
  const toggle = findNode(
    chip.render(),
    node => node.props && node.props['aria-label'] === 'Hide the composer speaker picker'
  )
  assert.ok(toggle, 'the picker preference should render')
  toggle.props.onCheckedChange(true)
  assert.equal(mod.$composerHidden.get(), true)
  assert.equal(storage.get('composerPickerHidden'), true, 'the choice should persist')
  const composer = byArea('composer.actions')[0]
  assert.equal(composer.render().type({}), null, 'the hidden picker should render nothing')
  toggle.props.onCheckedChange(false)
  assert.equal(storage.get('composerPickerHidden'), false)
})

const failed = results.filter(result => !result.ok)
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
if (failed.length) {
  process.exitCode = 1
}
