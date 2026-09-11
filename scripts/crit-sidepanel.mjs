// Critique evidence: the FILES side panel (expanded, preview interplay,
// context menu, collapsed rail, narrow viewport). Shots to sp-*.png.
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
await new Promise((r) => setTimeout(r, 1500))
const log = (k, v) => console.log(k + ': ' + JSON.stringify(v))
const asideBtn = (sel) => `(() => {
  const aside = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('FILES'))
  const b = [...aside.querySelectorAll('button')].find((x) => (((x.textContent ?? '').trim()).endsWith('${sel}')))
  return b
})()`

// 1. expanded panel with real tree
log('tree-loaded', await page.evaluate(() => {
  const aside = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('FILES'))
  return { rows: aside ? aside.querySelectorAll('button').length : 0 }
}))
await page.screenshot({ path: '.impeccable/shots/sp-1-expanded.png' })

// 2. open preview on a known file via the tree
await page.evaluate(`${asideBtn('package.json')}?.click()`)
await new Promise((r) => setTimeout(r, 900))
log('preview-open', await page.evaluate(() => ({
  modal: !!document.querySelector('h2.font-mono'),
  title: document.querySelector('h2.font-mono')?.textContent ?? null,
})))
await page.screenshot({ path: '.impeccable/shots/sp-2-preview.png' })

// 3. close preview (Esc), open context menu on a file row
await page.keyboard.press('Escape')
await new Promise((r) => setTimeout(r, 300))
await page.evaluate(`${asideBtn('package.json')}?.dispatchEvent(new MouseEvent('contextmenu', {
  bubbles: true, cancelable: true,
  clientX: 300, clientY: 400,
}))`)
await new Promise((r) => setTimeout(r, 300))
log('context-menu', await page.evaluate(() =>
  [...document.querySelectorAll('div.fixed.z-50 button')].map((b) => b.textContent.trim()),
))
await page.screenshot({ path: '.impeccable/shots/sp-3-context-menu.png' })
await page.mouse.click(700, 300)
await new Promise((r) => setTimeout(r, 200))

// 4. collapse the panel
await page.evaluate(`(() => {
  const aside = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('FILES'))
  const b = [...aside.querySelectorAll('button[title]')].find((x) => x.title === 'Hide files')
  return b
})()?.click()`)
await new Promise((r) => setTimeout(r, 300))
log('collapsed', await page.evaluate(() => ({
  railVisible: !!document.querySelector('aside button[title="Show files"]'),
})))
await page.screenshot({ path: '.impeccable/shots/sp-4-collapsed.png' })

// 5. re-expand, then narrow viewport
await page.evaluate(`document.querySelector('aside button[title="Show files"]')?.click()`)
await new Promise((r) => setTimeout(r, 300))
await page.setViewport({ width: 1000, height: 800 })
await new Promise((r) => setTimeout(r, 400))
log('at-1000', await page.evaluate(() => ({
  panelVisible: [...document.querySelectorAll('aside')].some((a) => a.offsetParent !== null && a.textContent.includes('FILES')),
  hOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
})))
await page.screenshot({ path: '.impeccable/shots/sp-5-narrow-1000.png' })

log('console-errors', errs)
await browser.close()
process.exit(0)
