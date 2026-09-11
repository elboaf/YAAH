// Debug: why didn't the tree-row click open the preview?
import puppeteer from 'puppeteer-core'
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const browser = await puppeteer.launch({
  executablePath: CHROME, headless: 'new', args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1500))

// how many asides match 'FILES'?
console.log(await page.evaluate(() => {
  const asides = [...document.querySelectorAll('aside')]
  return asides.map((a) => ({
    w: a.offsetWidth,
    text: (a.textContent ?? '').slice(0, 40),
    buttons: a.querySelectorAll('button').length,
  }))
}))

// find the package.json row and click it via React's event path
const clicked = await page.evaluate(() => {
  const aside = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('FILES'))
  const btns = [...aside.querySelectorAll('button')]
  const exact = btns.filter((b) => (b.textContent ?? '').trim() === 'package.json')
  const info = exact.map((b) => ({
    html: b.outerHTML.slice(0, 120),
    rect: b.getBoundingClientRect().toJSON(),
    disabled: b.disabled,
  }))
  exact[0]?.click()
  return { matches: exact.length, info }
})
console.log('click:', JSON.stringify(clicked))

await new Promise((r) => setTimeout(r, 1000))
console.log('after-click:', await page.evaluate(() => ({
  modalPresent: !!document.querySelector('div.fixed.inset-0.z-50'),
  modalHtml: document.querySelector('div.fixed.inset-0.z-50')?.outerHTML.slice(0, 100) ?? null,
})))
await browser.close()
