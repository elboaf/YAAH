// Critique evidence run (Assessment B, browser half):
// fresh tab -> empty state -> skill menu -> settings (collapsed+expanded)
// -> reload conversation 291 (history P0 regression vs DB ground truth)
// -> narrow 760. Screenshots to .impeccable/shots/, console errors reported.
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const CID = 291
const out = []
const log = (k, v) => { out.push([k, v]); console.log(k + ': ' + JSON.stringify(v)) }

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars'],
})
const page = await browser.newPage()
const consoleErrs = []
page.on('console', (m) => { if (m.type() === 'error') consoleErrs.push(m.text().slice(0, 200)) })
page.on('pageerror', (e) => consoleErrs.push('pageerror: ' + String(e).slice(0, 200)))

await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1500))

// 1. empty state
log('empty-state', await page.evaluate(() => ({
  hasSteps: [...document.querySelectorAll('main ol li')].length,
  composerPlaceholder: document.querySelector('textarea')?.placeholder?.length ?? 0,
})))
await page.screenshot({ path: '.impeccable/shots/crit2-01-empty-1440.png' })

// 2. skill menu
await page.click('textarea')
await page.type('textarea', '/s')
await new Promise((r) => setTimeout(r, 600))
log('skill-menu', await page.evaluate(() => ({
  rows: [...document.querySelectorAll('div.absolute button')].filter((b) => (b.textContent ?? '').includes('/s')).length,
  footer: [...document.querySelectorAll('div.absolute div')].some((d) => (d.textContent ?? '').includes('navigate')),
})))
await page.screenshot({ path: '.impeccable/shots/crit2-02-skill-menu.png' })
await page.keyboard.press('Escape')
await page.keyboard.down('Control'); await page.keyboard.press('a'); await page.keyboard.up('Control')
await page.keyboard.press('Backspace')

// 3. settings modal, collapsed then expanded
await page.evaluate(() => {
  const b = [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Settings')
  b?.click()
})
await new Promise((r) => setTimeout(r, 800))
log('settings-collapsed', await page.evaluate(() => ({
  rows: [...document.querySelectorAll('button[aria-expanded]')].length,
  summaries: [...document.querySelectorAll('button[aria-expanded] span.text-\\[10px\\]')].map((s) => s.textContent.trim()).slice(0, 4),
})))
await page.screenshot({ path: '.impeccable/shots/crit2-03-settings.png' })
await page.evaluate(() => document.querySelector('button[aria-expanded]')?.click())
await new Promise((r) => setTimeout(r, 400))
log('settings-expanded', await page.evaluate(() => ({
  openRow: document.querySelector('button[aria-expanded="true"]')?.textContent?.trim().slice(0, 40) ?? null,
  fields: [...document.querySelectorAll('div.border-t input, div.border-t select')].length,
})))
await page.screenshot({ path: '.impeccable/shots/crit2-04-settings-expanded.png' })
await page.keyboard.press('Escape')
await new Promise((r) => setTimeout(r, 400))

// 4. history: DB ground truth + UI rendering (regression checks)
const rows = await (await fetch(`http://localhost:8765/api/conversations/${CID}/messages`)).json()
const asst = rows.filter((r) => r.role === 'assistant')
const toolRows = rows.filter((r) => r.role === 'tool')
const expectedCalls = asst.reduce((n, r) => n + (r.tool_calls?.length ?? 0), 0)
const asstTurnsWithCalls = asst.filter((r) => r.tool_calls?.length).length
const callIds = new Set(asst.flatMap((r) => (r.tool_calls ?? []).map((c) => c.id ?? '')))
const orphanResults = toolRows.filter((r) => !callIds.has(r.tool_call_id ?? r.tool_calls?.[0]?.id ?? '')).length
log('db-291', { rows: rows.length, asstTurnsWithCalls, expectedCalls, toolRows: toolRows.length, orphanResults })

const convs = await (await fetch('http://localhost:8765/api/conversations')).json()
const title = convs.find((c) => c.id === CID)?.title
await page.evaluate((t) => {
  const row = [...document.querySelectorAll('div.group > button')].find((b) => (b.textContent ?? '').trim() === t)
  row?.click()
}, title)
await new Promise((r) => setTimeout(r, 2000))

const hist = await page.evaluate(() => {
  const text = (el) => el.textContent ?? ''
  return {
    placeholders: [...document.querySelectorAll('main span.run-pulse')].filter((el) => /^[\u258a\u258c]$/.test(text(el).trim())).length,
    traceLines: [...document.querySelectorAll('main button')].filter((b) => /\d+ calls?\b/.test(text(b).trim())).length,
    userBubbles: [...document.querySelectorAll('main div.flex.justify-end')].length,
  }
})
log('history-ui', hist)
log('history-checks', {
  noPhantoms: hist.placeholders === 0,
  oneTracePerTurn: hist.traceLines === asstTurnsWithCalls,
})
await page.screenshot({ path: '.impeccable/shots/crit2-05-history-collapsed.png' })

await page.evaluate(() => {
  for (const b of [...document.querySelectorAll('main button')]) {
    if (/\d+ calls?\b/.test((b.textContent ?? '').trim())) b.click()
  }
})
await new Promise((r) => setTimeout(r, 600))
log('history-expanded', await page.evaluate(() => ({
  expanders: [...document.querySelectorAll('main span')].filter((el) => (el.textContent ?? '').trim() === '[+]').length,
})))
await page.screenshot({ path: '.impeccable/shots/crit2-06-history-expanded.png' })

// 5. narrow viewport
await page.setViewport({ width: 760, height: 820 })
await new Promise((r) => setTimeout(r, 600))
log('narrow', await page.evaluate(() => ({
  horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  visibleAsides: [...document.querySelectorAll('aside')].filter((a) => a.offsetParent !== null).length,
})))
await page.screenshot({ path: '.impeccable/shots/crit2-07-narrow-760.png' })

log('console-errors', consoleErrs)
await browser.close()
process.exit(0)
