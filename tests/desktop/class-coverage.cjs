// Report classes used in the runtime plugin that the compiled Desktop CSS does
// not contain. Tailwind only emits classes it saw at build time, and the plugin
// is loaded at runtime, so a class the build never saw silently does nothing
// (this is how `max-w-4xl` and `lg:grid-cols-2` became no-ops on the page).
//
//   node tests/desktop/class-coverage.cjs <compiled.css> <plugin.js>
const fs = require('fs')

// Tailwind escapes these in the emitted selector; a class token cannot match
// the stylesheet text without the same escapes.
const ESCAPE = new Set([
  '@', ':', '(', ')', '[', ']', '.', '/', '%', '#', ',', '!', '+', '*', '&', "'", '"', '>', '<', '='
])

/** Classes written as plain `className: '…'` literals in the plugin. */
function pluginClasses(pluginPath) {
  const source = fs.readFileSync(pluginPath, 'utf8')
  const classes = new Set()
  for (const match of source.matchAll(/className:\s*'([^']*)'/g)) {
    for (const token of match[1].split(/\s+/)) {
      if (token) classes.add(token)
    }
  }
  return classes
}

function missingClasses(cssPath, pluginPath) {
  const css = fs.readFileSync(cssPath, 'utf8')
  const missing = []
  for (const token of pluginClasses(pluginPath)) {
    const selector = '.' + [...token].map(ch => (ESCAPE.has(ch) ? '\\' + ch : ch)).join('')
    if (!css.includes(selector)) missing.push(token)
  }
  return missing.sort()
}

module.exports = { missingClasses, pluginClasses }

if (require.main === module) {
  const [, , cssPath, pluginPath] = process.argv
  if (!cssPath || !pluginPath) {
    console.error('usage: node class-coverage.cjs <compiled.css> <plugin.js>')
    process.exit(2)
  }
  const missing = missingClasses(cssPath, pluginPath)
  console.log(`missing classes: ${missing.length}`)
  for (const token of missing) console.log(`MISSING  ${token}`)
  process.exitCode = missing.length ? 1 : 0
}
