// Sidebar rework evidence: structure, states, interactions. Shots to sb-*.png.
// Run: node scripts/sb-evidence.mjs   (dev server on :1420 must be up)
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
const errs = []
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text().slice(0, 160)) })
page.on('pageerror', (e) => errs.push('pageerror: ' + String(e).slice(0, 160)))

await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1800))
const log = (k, v) => console.log(k + ': ' + JSON.stringify(v))

// The LEFT sidebar (nav column), not the FILES aside
const NAV = `(() => {
  const asides = [...document.querySelectorAll('aside')]
  return asides.find((a) => a.textContent.includes('New chat'))
})()`

// 1. Structure: sections, counts, add row, footer
log('structure', await page.evaluate(`${NAV} ? {
  sections: [...${NAV}.querySelectorAll('button')].filter((b) => (b.className || '').includes('font-semibold')).length,
  counts: [...${NAV}.querySelectorAll('span')].filter((s) => /^[0-9]+$/.test(s.textContent.trim())).map((s) => s.textContent.trim()),
  addRow: [...${NAV}.querySelectorAll('button')].some((b) => b.textContent.includes('Add workspace')),
  modelSelect: !!${NAV}.querySelector('select[aria-label="Model"]'),
  settingsGear: !!${NAV}.querySelector('button[aria-label="Settings"]'),
  oldWorkspaceSelect: !!${NAV}.querySelector('select[aria-label="Workspace"]'),
  newChat: [...${NAV}.querySelectorAll('button')].some((b) => b.textContent.includes('New chat')),
} : null`))
await page.screenshot({ path: '.impeccable/shots/sb-1-structure.png' })

// 2. Active workspace marker
log('active-marker', await page.evaluate(() => {
  const nav = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('New chat'))
  const dots = [...nav.querySelectorAll('span[title="Active workspace"]')]
  return { activeMarkers: dots.length }
}))

// 3. Open the row-action menu on the first conversation row
await page.evaluate(`(() => {
  const nav = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('New chat'))
  const b = [...nav.querySelectorAll('button[aria-label="Conversation actions"]')][0]
  return b
})()?.click()`)
await new Promise((r) => setTimeout(r, 300))
log('row-menu', await page.evaluate(() => {
  const items = [...document.querySelectorAll('div.absolute.right-0.top-6 button')].map((b) => b.textContent.trim())
  return items
}))
await page.keyboard.press('Escape')
await new Promise((r) => setTimeout(r, 200))

// 4. Narrow viewport sanity
await page.setViewport({ width: 1000, height: 800 })
await new Promise((r) => setTimeout(r, 400))
log('at-1000', await page.evaluate(() => ({
  hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
})))
await page.screenshot({ path: '.impeccable/shots/sb-2-narrow-1000.png' })

log('console-errors', errs)
await browser.close()
