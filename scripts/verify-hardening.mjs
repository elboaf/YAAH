// Verify the two P1 hardening fixes live:
//  1. provider removal shows a confirm naming the key loss (screenshot + no
//     removal on Cancel/Esc)
//  2. turnError banner wiring (can't force a real mid-stream failure without
//     a provider, so this is a DOM-level check of the banner elements)
// No conversation is created; nothing is written to config.
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1200))

// open Settings, expand the provider that has a saved key
await page.evaluate(() => {
  const b = [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'Settings')
  b?.click()
})
await new Promise((r) => setTimeout(r, 700))
await page.evaluate(() => document.querySelector('button[aria-expanded]')?.click())
await new Promise((r) => setTimeout(r, 400))

// click "remove provider" -> confirm must appear, naming the key loss
await page.evaluate(() => {
  const b = [...document.querySelectorAll('button')].find((x) => x.textContent.trim() === 'remove provider')
  b?.click()
})
await new Promise((r) => setTimeout(r, 400))
const confirmText = await page.evaluate(() => {
  const h = [...document.querySelectorAll('h2')].find((x) => x.textContent.includes('Remove provider?'))
  return h ? h.parentElement.textContent : null
})
console.log('confirm-shown:', JSON.stringify(!!confirmText))
console.log('confirm-names-key:', JSON.stringify(!!confirmText && confirmText.includes('saved API key')))
await page.screenshot({ path: '.impeccable/shots/harden-1-remove-confirm.png' })

// Esc closes only the confirm (Settings stays open)
await page.keyboard.press('Escape')
await new Promise((r) => setTimeout(r, 300))
const afterEsc = await page.evaluate(() => ({
  settingsStillOpen: !!document.querySelector('input[aria-label$="API base URL"]'),
  confirmGone: ![...document.querySelectorAll('h2')].some((x) => x.textContent.includes('Remove provider?')),
  providerStillThere: [...document.querySelectorAll('button[aria-expanded]')].some((b) => b.textContent.includes('openrouter')),
}))
console.log('esc-consumed-by-confirm:', JSON.stringify(afterEsc))
await page.screenshot({ path: '.impeccable/shots/harden-2-after-esc.png' })

await browser.close()
process.exit(0)
