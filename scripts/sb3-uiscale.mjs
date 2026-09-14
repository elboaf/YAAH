// Interface-scale evidence v3 — fully sandboxed: fetch is stubbed so the
// Settings round-trip never touches the real ~/.yaah/config.json.
// Verifies: startup restore (GET ui_scale 1.25), Settings control, save
// (PUT payload carries ui_scale) and live re-zoom. Shots sb3-*.png.
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu'],
})
const page = await browser.newPage()
const errs = []
page.on('console', (m) => { if (m.type() === 'error') errs.push(m.text().slice(0, 120)) })
page.on('pageerror', (e) => errs.push('pageerror: ' + String(e).slice(0, 120)))

// Sandbox the config endpoints before any app script runs.
await page.evaluateOnNewDocument(() => {
  const fakeConfig = {
    providers: {},
    active_provider: '',
    api_base: '',
    api_key: '',
    model: '',
    temperature: 0.2,
    max_tokens: 0,
    max_steps: 200,
    last_workspace: '',
    ui_scale: 1.25,
    voice: { engine: 'local', cloud_endpoint: '', cloud_api_key: '', cloud_model: '', ptt_hotkey: 'Ctrl+Space' },
    remote: { hosting_enabled: true, passphrase: '', display_name: '' },
  }
  window.__savedPatch = null
  const orig = window.fetch.bind(window)
  window.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url
    const method = (init?.method ?? 'GET').toUpperCase()
    if (url.endsWith('/api/config') && method === 'GET') {
      return new Response(JSON.stringify(fakeConfig), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    if (url.endsWith('/api/config') && method === 'PUT') {
      window.__savedPatch = JSON.parse(init.body)
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
    return orig(input, init)
  }
})

const zoom = () => page.evaluate(() => document.documentElement.style.zoom || '(none)')

// 1. Startup restore from config ui_scale 1.25
await page.setViewport({ width: 1440, height: 900 })
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1800))
console.log('startup-zoom: ' + JSON.stringify(await zoom()))
await page.screenshot({ path: '.impeccable/shots/sb3-1-startup-125.png' })

// 2. Open Settings, verify the Interface radiogroup, pick 110%, Save
await page.evaluate(`(() => {
  const nav = [...document.querySelectorAll('aside')].find((a) => a.textContent.includes('New chat'))
  const b = [...nav.querySelectorAll('button[aria-label="Settings"]')][0]
  return b
})()?.click()`)
await new Promise((r) => setTimeout(r, 1000))
const control = await page.evaluate(() => {
  const rg = document.querySelector('div[role="radiogroup"][aria-label="Interface scale"]')
  if (!rg) return null
  return [...rg.querySelectorAll('button[role="radio"]')].map((b) => ({
    label: b.textContent.trim(),
    checked: b.getAttribute('aria-checked'),
  }))
})
console.log('settings-control: ' + JSON.stringify(control))
await page.screenshot({ path: '.impeccable/shots/sb3-3-settings.png' })

await page.evaluate(`(() => {
  const rg = document.querySelector('div[role="radiogroup"][aria-label="Interface scale"]')
  const b = [...rg.querySelectorAll('button[role="radio"]')].find((x) => x.textContent.trim() === '110%')
  return b
})()?.click()`)
await new Promise((r) => setTimeout(r, 250))
await page.evaluate(`(() => {
  const btns = [...document.querySelectorAll('div.fixed.z-50 button')]
  return btns.find((b) => b.textContent.trim() === 'Save')
})()?.click()`)
await new Promise((r) => setTimeout(r, 1800))
const post = await page.evaluate(() => ({
  zoom: document.documentElement.style.zoom || '(none)',
  savedUiScale: window.__savedPatch ? window.__savedPatch.ui_scale : null,
}))
console.log('post-save: ' + JSON.stringify(post))
await page.screenshot({ path: '.impeccable/shots/sb3-4-after-save-110.png' })

console.log('console-errors: ' + JSON.stringify(errs))
await browser.close()
