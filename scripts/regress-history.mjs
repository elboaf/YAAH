// Regression loop for the history-rendering P0.
// Asserts: reloading a real conversation renders every tool call exactly once,
// with results, and no phantom "thinking" placeholders.
//
// Usage: node scripts/regress-history.mjs [conversationId]
// Exit 0 = pass, 1 = fail. Requires dev servers on :1420 (vite) and :8765 (api).
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const cid = process.argv[2] ? Number(process.argv[2]) : 291

// Ground truth from the DB (via the API the app itself uses)
const rows = await (await fetch(`http://localhost:8765/api/conversations/${cid}/messages`)).json()
const asst = rows.filter((r) => r.role === 'assistant')
const toolRows = rows.filter((r) => r.role === 'tool')
const expectedCalls = asst.reduce((n, r) => n + (r.tool_calls?.length ?? 0), 0)
const expectedResults = toolRows.length
const asstTurnsWithCalls = asst.filter((r) => r.tool_calls?.length).length
console.log(
  `db: ${rows.length} rows | assistant ${asst.length} (${asstTurnsWithCalls} turns with ${expectedCalls} calls) | tool ${toolRows.length} (results: ${expectedResults})`,
)

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1200))

// Load the conversation the way a user does: click its sidebar row (matched
// by title from the API, since rows don't carry conversation ids in the DOM)
const conv = await (await fetch('http://localhost:8765/api/conversations')).json()
const title = conv.find((c) => c.id === cid)?.title
if (!title) {
  console.log(`FAIL conversation ${cid} not found in sidebar list`)
  process.exit(1)
}
await page.evaluate((t) => {
  const rows = [...document.querySelectorAll('div.group > button')]
  const row = rows.find((b) => (b.textContent ?? '').trim() === t)
  if (!row) throw new Error(`sidebar row not found for title: ${t}`)
  row.click()
}, title)
await new Promise((r) => setTimeout(r, 2000))

// Expand every collapsed trace so per-call rows render
await page.evaluate(() => {
  for (const b of [...document.querySelectorAll('main button')]) {
    if (/\d+ calls?\b/.test((b.textContent ?? '').trim())) b.click()
  }
})
await new Promise((r) => setTimeout(r, 600))

const stats = await page.evaluate(() => {
  const text = (el) => el.textContent ?? ''
  return {
    // phantom placeholders: empty assistant messages render the pulsing block cursor
    placeholders: [...document.querySelectorAll('main span.run-pulse')].filter((el) =>
      /^[\u258a\u258c]$/.test(text(el).trim()),
    ).length,
    // collapsed trace lines ("N calls ...")
    traceLines: [...document.querySelectorAll('main button')].filter((b) =>
      /\d+ calls?\b/.test(text(b).trim()),
    ).length,
    // per-call expander spans ([+]) after expanding all traces
    expanders: [...document.querySelectorAll('main span')].filter((el) => text(el).trim() === '[+]')
      .length,
  }
})

console.log('ui:', JSON.stringify(stats))
let fail = 0
const check = (name, ok, detail) => {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}${detail ? ` \u2014 ${detail}` : ''}`)
  if (!ok) fail = 1
}
check('no phantom thinking placeholders', stats.placeholders === 0, `found ${stats.placeholders}`)
check(
  'one collapsed trace per assistant turn with calls',
  stats.traceLines === asstTurnsWithCalls,
  `ui ${stats.traceLines} vs db ${asstTurnsWithCalls}`,
)
check(
  'every call + result rendered as an expandable row',
  stats.expanders === expectedCalls + expectedResults,
  `ui ${stats.expanders} vs db ${expectedCalls + expectedResults}`,
)

await page.screenshot({ path: '.impeccable/shots/regress-history.png' })
await browser.close()
process.exit(fail)