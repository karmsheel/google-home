// Loads googlehome/desktop/plugin.js the way Hermes Desktop does — as a single
// ESM module whose only resolvable specifiers are the SDK, react and
// jsx-runtime — then exercises register() and every render path.
//
//   node tests/desktop/test_desktop_plugin.mjs
import { readFile, writeFile, mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import assert from 'node:assert/strict'

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

check('single default-exported plugin object', () => {
  assert.match(source, /export default\s*\{/)
})

// ── load it like the host ────────────────────────────────────────────────────
const rewritten = source
  .replace(/from\s+'@hermes\/plugin-sdk'/g, `from '${stubs['@hermes/plugin-sdk']}'`)
  .replace(/from\s+'react'/g, `from '${stubs.react}'`)
  .replace(/from\s+'react\/jsx-runtime'/g, `from '${stubs['react/jsx-runtime']}'`)

const dir = await mkdtemp(join(tmpdir(), 'gh-desktop-'))
const modulePath = join(dir, 'plugin.mjs')
await writeFile(modulePath, rewritten, 'utf8')
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
const ctx = {
  storage: { get: (_k, fallback) => fallback, set() {}, remove() {} },
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
  rest: async () => ({}),
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

check('registers a settings page', () => {
  assert.equal(settingsPages.length, 1)
  assert.equal(settingsPages[0].title, 'Google Home')
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
for (const contribution of contributions) {
  if (typeof contribution.render !== 'function') continue
  check(`render ${contribution.area}:${contribution.id}`, () => {
    const tree = contribution.render()
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

console.log('\nbehaviour')
const sdkStub = await import(stubs['@hermes/plugin-sdk'])
check('navigating from the chip uses the registered route', () => {
  let navigated = null
  const original = sdkStub.host.navigate
  sdkStub.host.navigate = route => {
    navigated = route
  }
  try {
    const chip = byArea('statusBar.right')[0]
    const findButton = node => {
      if (!node || typeof node !== 'object') return null
      if (node.type === 'button') return node
      if (typeof node.type === 'function') return findButton(node.type(node.props || {}))
      const children = node.props && node.props.children
      for (const child of Array.isArray(children) ? children : [children]) {
        const found = findButton(child)
        if (found) return found
      }
      return null
    }
    const button = findButton(chip.render())
    assert.ok(button, 'chip should render a button')
    button.props.onClick()
    assert.equal(navigated, '/googlehome')
  } finally {
    sdkStub.host.navigate = original
  }
})

const failed = results.filter(result => !result.ok)
console.log(`\n${results.length - failed.length}/${results.length} checks passed`)
if (failed.length) {
  process.exitCode = 1
}
