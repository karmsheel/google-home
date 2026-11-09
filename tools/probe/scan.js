// Direct TCP scan for Google Cast receivers (port 8009) on the local /24.
// Used because Windows is dual-homed on 192.168.1.0/24 (Ethernet + WiFi),
// which makes mDNS unreliable.
const net = require('net')

const BASE = process.argv[2] || '192.168.1'
const PORT = Number(process.argv[3] || 8009)
const TIMEOUT = Number(process.argv[4] || 400)
const CONCURRENCY = 96

function probe(ip) {
  return new Promise(resolve => {
    const sock = new net.Socket()
    let done = false
    const finish = open => {
      if (done) return
      done = true
      sock.destroy()
      resolve(open ? ip : null)
    }
    sock.setTimeout(TIMEOUT)
    sock.once('connect', () => finish(true))
    sock.once('timeout', () => finish(false))
    sock.once('error', () => finish(false))
    sock.connect(PORT, ip)
  })
}

async function main() {
  const ips = []
  for (let i = 1; i <= 254; i++) ips.push(`${BASE}.${i}`)
  const open = []
  let idx = 0
  async function worker() {
    while (idx < ips.length) {
      const ip = ips[idx++]
      const hit = await probe(ip)
      if (hit) open.push(hit)
    }
  }
  await Promise.all(Array.from({ length: CONCURRENCY }, worker))
  console.log(JSON.stringify({ scanned: ips.length, port: PORT, open }, null, 2))
  process.exit(0)
}

main()
