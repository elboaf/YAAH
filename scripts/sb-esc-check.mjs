// Verifies the row menu closes on Escape (regression for the sidebar rework).
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1800))

const openMenu = `(() => {
  const nav = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('New chat'))
  const b = [...nav.querySelectorAll('button[aria-label="Conversation actions"]')][0]
  if (!b) return false
  b.click()
  return true
})()`

const menuVisible = `(() => {
  const nav = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('New chat'))
  return [...nav.querySelectorAll('button')].some((b) => b.textContent.trim() === 'Export as Markdown')
})()`

await page.evaluate(openMenu)
await new Promise((r) => setTimeout(r, 250))
const before = await page.evaluate(menuVisible)
await page.keyboard.press('Escape')
await new Promise((r) => setTimeout(r, 250))
const after = await page.evaluate(menuVisible)
console.log(JSON.stringify({ opened: before, closedByEsc: !after, pass: before && !after }))
await browser.close()
