// Verify: Esc closes the file-tree context menu.
import puppeteer from 'puppeteer-core'
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const browser = await puppeteer.launch({
  executablePath: CHROME, headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1500))

// open the context menu on a file row
await page.evaluate(`(() => {
  const aside = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('FILES'))
  const b = [...aside.querySelectorAll('button')].find((x) => ((x.textContent ?? '').trim()).endsWith('package.json'))
  b?.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 300, clientY: 400 }))
})()`)
await new Promise((r) => setTimeout(r, 300))
const before = await page.evaluate(() => ({
  menuOpen: [...document.querySelectorAll('div[role="menu"]')].length,
}))
await page.keyboard.press('Escape')
await new Promise((r) => setTimeout(r, 300))
const after = await page.evaluate(() => ({
  menuOpen: [...document.querySelectorAll('div[role="menu"]')].length,
}))
console.log('menu-before-esc:', JSON.stringify(before))
console.log('menu-after-esc:', JSON.stringify(after))
console.log(before.menuOpen === 1 && after.menuOpen === 0 ? 'ESC-MENU PASS' : 'ESC-MENU FAIL')
await browser.close()
process.exit(before.menuOpen === 1 && after.menuOpen === 0 ? 0 : 1)
