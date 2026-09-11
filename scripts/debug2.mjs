import puppeteer from 'puppeteer-core'
const b = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: 'new', args: ['--no-sandbox'] })
const p = await b.newPage()
await p.setViewport({ width: 1440, height: 900 })
await p.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1500))
const names = await p.evaluate(() => {
  const aside = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('FILES'))
  return [...aside.querySelectorAll('button')].map((x) => x.textContent.trim()).slice(0, 60)
})
console.log(JSON.stringify(names))
await b.close()
