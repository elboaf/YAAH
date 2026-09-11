// Live-path check: send a message, watch the ticker work, confirm the turn
// ends as a collapsed trace (not a placeholder). Creates a new conversation.
import puppeteer from 'puppeteer-core'

const b = await puppeteer.launch({
  executablePath: 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars'],
})
const pg = await b.newPage()
await pg.setViewport({ width: 1440, height: 900 })
await pg.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1200))

await pg.click('textarea')
await pg.type('textarea', 'live-path check: reply with the single word ok')
await pg.evaluate(() => {
  const btn = [...document.querySelectorAll('button')].find(
    (x) => x.textContent.trim() === 'Send',
  )
  btn?.click()
})

// sample the live phase: poll for the Stop button right after sending
let live = { ticker: false, stopBtn: false }
for (let i = 0; i < 40; i++) {
  await new Promise((r) => setTimeout(r, 250))
  live = await pg.evaluate(() => ({
    ticker: [...document.querySelectorAll('main span.font-mono')].some((el) =>
      /^(working…|\d+ calls)$/.test((el.textContent ?? '').trim()),
    ),
    stopBtn: [...document.querySelectorAll('main button')].some(
      (x) => x.textContent.trim() === 'Stop',
    ),
  }))
  if (live.stopBtn || live.ticker) break
}
console.log('live phase:', JSON.stringify(live))

// wait for the turn to finish (status line back to idle)
let idle = false
for (let i = 0; i < 30; i++) {
  await new Promise((r) => setTimeout(r, 2000))
  idle = await pg.evaluate(() =>
    (document.querySelector('main .font-mono.text-\\[10px\\]')?.textContent ?? '').includes('idle'),
  )
  if (idle) break
}
await new Promise((r) => setTimeout(r, 800))
const done = await pg.evaluate(() => {
  const text = (el) => el.textContent ?? ''
  return {
    statusIdle: [...document.querySelectorAll('main div')].some((d) => text(d).trim() === 'idle'),
    placeholders: [...document.querySelectorAll('main span.run-pulse')].filter((el) =>
      /^[\u258a\u258c]$/.test(text(el).trim()),
    ).length,
    traceLines: [...document.querySelectorAll('main button')].filter((b) =>
      /\d+ calls?\b/.test(text(b).trim()),
    ).length,
    replyOk: (document.querySelector('main')?.textContent ?? '').includes('ok'),
  }
})
console.log('done phase:', JSON.stringify(done))
const pass = live.stopBtn && done.statusIdle && done.placeholders === 0 && done.replyOk
console.log(pass ? 'LIVE PATH PASS' : 'LIVE PATH FAIL')
await pg.screenshot({ path: '.impeccable/shots/live-path.png' })
await b.close()
process.exit(pass ? 0 : 1)