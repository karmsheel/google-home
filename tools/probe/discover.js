// LAN discovery of Google Cast devices (Google Home / Nest speakers).
// Pure mDNS listen; no device contact yet.
const { Bonjour } = require('bonjour-service')

const FOUND = new Map()
const SECONDS = Number(process.argv[2] || 12)

const bonjour = new Bonjour()
const browser = bonjour.find({ type: 'googlecast' })

browser.on('up', svc => {
  const key = (svc.txt && svc.txt.id) || svc.name
  const addrs = (svc.addresses || []).filter(a => !a.includes(':'))
  FOUND.set(key, {
    name: svc.name,
    fqdn: svc.fqdn,
    host: svc.host,
    port: svc.port,
    addresses: addrs,
    txt: svc.txt
  })
})

setTimeout(() => {
  bonjour.destroy()
  const list = [...FOUND.values()]
  console.log(JSON.stringify({ found: list.length, devices: list }, null, 2))
  process.exit(0)
}, SECONDS * 1000)
