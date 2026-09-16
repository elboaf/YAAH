// Live verification: PTT interrupt + voice answers to ask_user questions.
// Drives the REAL component flow in a real Chromium against the real dev
// bundle; only the browser-unavailable seams are stubbed:
//   - VoiceRecorder (no physical mic in CI) — records silence, speech=true
//   - POST /api/transcribe — scripted transcripts (and detected language)
//   - POST /api/agent/:id, POST /api/conversations, POST .../answer —
//     recorded instead of executed, so assertions see exact payloads
// Everything else (store, Composer, AskUserCard, hotkey event bridge,
// matchOptionLabel, reject toasts) is the production code.
// No real conversation is created; no config is written.
import puppeteer from 'puppeteer-core'

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'
const failures = []
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}: ${name}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failures.push(name)
}

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars', '--use-fake-ui-for-media-stream'],
})
const page = await browser.newPage()
await page.setViewport({ width: 1440, height: 900 })
page.on('pageerror', (e) => failures.push(`pageerror: ${e.message}`))
page.on('console', (m) => {
  const t = m.text()
  if (t.includes('[vite]')) console.log('VITE:', t)
})
let navCount = 0
page.on('framenavigated', (f) => {
  if (f === page.mainFrame()) {
    navCount++
    console.log(`NAV #${navCount}`)
  }
})

// NOTE: on a fresh dev-server start vite may re-optimize deps (lockfile
// changed) and FULL-RELOAD the page right after first connect — window-level
// stubs would be wiped. The app also dynamic-imports the Tauri shortcut
// plugin at startup (its own optimize+reload). Load, warm that import, let
// every optimize settle, force one reload ourselves, and only then install.
await page.goto('http://localhost:1420/', { waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 3000))
await page.evaluate(() => import('/@id/@tauri-apps/plugin-global-shortcut').catch(() => {}))
await new Promise((r) => setTimeout(r, 2500))
await page.reload({ waitUntil: 'networkidle2' })
await new Promise((r) => setTimeout(r, 1500))

async function installStubs() {
  return page.evaluate(async () => {
    if (window.__stubsInstalled) return true
    window.__stubsInstalled = true
    const realFetch = window.fetch.bind(window)
    window.__turnCalls = []
    window.__answerCalls = []
    window.__voiceQueue = []
    window.__voiceLog = []
    window.fetch = async (input, init) => {
      const url = String(input instanceof Request ? input.url : input)
      const method = (init?.method ?? 'GET').toUpperCase()
      const body = init?.body
      // Health returns 503 forever: the recovery banner reloads the page
      // when a failing probe recovers, which would wipe these stubs.
      if (url.includes('/api/health')) {
        return new Response(JSON.stringify({ detail: 'stubbed' }), { status: 503 })
      }
      if (method === 'POST' && /\/api\/agent\/\d+$/.test(url)) {
        window.__turnCalls.push({ url, body: body ? JSON.parse(body) : null })
        const stream = new ReadableStream({
          start(c) {
            c.enqueue(
              new TextEncoder().encode(
                JSON.stringify({
                  type: 'tool_start',
                  call_id: 'call-x',
                  name: 'ask_user',
                  args: { question: 'intercepted', options: [] },
                }) + '\n',
              ),
            )
            c.close()
          },
        })
        return new Response(stream, { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } })
      }
      if (method === 'POST' && /\/api\/conversations\/\d+\/answer$/.test(url)) {
        window.__answerCalls.push(body ? JSON.parse(body) : null)
        return new Response(JSON.stringify({ ok: true }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (method === 'POST' && /\/api\/conversations$/.test(url)) {
        return new Response(JSON.stringify({ id: 901 }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (method === 'POST' && /\/api\/transcribe$/.test(url)) {
        const next = window.__voiceQueue.shift() ?? { text: '', language: null }
        window.__voiceLog.push({ size: body ? body.size : 0, ...next })
        return new Response(JSON.stringify(next), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      return realFetch(input, init)
    }
    // VoiceRecorder stub: no physical mic here; start() resolves, speech started.
    const { VoiceRecorder } = await import('/src/voice.ts')
    const { useAgent } = await import('/src/store.ts')
    VoiceRecorder.prototype.start = async function () {}
    VoiceRecorder.prototype.stop = async function () {
      return new Blob([new ArrayBuffer(3200)], { type: 'audio/wav' })
    }
    VoiceRecorder.prototype.metrics = function () {
      return { speechStarted: true, silenceMs: 0 }
    }
    window.useAgentBridge = useAgent
    return true
  })
}
const installed = await installStubs()
check('setup: stubs installed', installed === true)

// Self-heal: if vite full-reloads mid-run (a late dep optimize), window
// stubs vanish — reinstall and re-seed, then re-run the failed section.
const heal = async () => {
  const alive = await page.evaluate(() => !!window.__stubsInstalled).catch(() => false)
  if (!alive) {
    await installStubs()
    await page.evaluate(() => window.useAgentBridge.getState().setConversationId(901))
  }
}

// Point the store at a real conversation id so submitAnswer's guard passes.
await page.evaluate(() => window.useAgentBridge.getState().setConversationId(901))
await new Promise((r) => setTimeout(r, 300))

const seedQuestion = (callId, labels) =>
  page.evaluate(
    (callId, labels) => {
      window.useAgentBridge.getState().setPendingQuestion({
        callId,
        question: 'Deploy now?',
        options: labels.map((label) => ({ label })),
        convKey: '901',
      })
    },
    callId,
    labels,
  )

const ptt = async () => {
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('ptt-press')))
  await new Promise((r) => setTimeout(r, 150)) // real hold latency
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('ptt-release')))
  await new Promise((r) => setTimeout(r, 450))
}

const questionGone = () =>
  page.evaluate(() => window.useAgentBridge.getState().pendingQuestion === null)

let calls = []

// ---- A: dictated option label answers the question -------------------------
await heal()
await seedQuestion('call-a', ['Ship it', 'Hold off'])
await page.evaluate(() => window.__voiceQueue.push({ text: 'ship it', language: 'en' }))
await ptt()
calls = await page.evaluate(() => window.__answerCalls)
check('A: option label submitted', Array.isArray(calls) && calls.some((c) => c && c.call_id === 'call-a' && c.answer === 'Ship it'), JSON.stringify(calls))
check('A: card cleared after voice answer', await questionGone())
check('A: composer draft untouched', (await page.evaluate(() => document.querySelector('textarea')?.value)) === '')

// ---- B: non-matching speech stages the free-text answer --------------------
await heal()
await seedQuestion('call-b', ['Ship it', 'Hold off'])
await page.evaluate(() => window.__voiceQueue.push({ text: 'use sqlite instead please', language: 'en' }))
await ptt()
calls = await page.evaluate(() => window.__answerCalls)
check('B: no auto-submit for free text', !calls.some((c) => c && c.call_id === 'call-b'))
const staged = await page.evaluate(() => {
  const inp = document.querySelector('input[placeholder*="Voice answer staged"]')
  return { found: !!inp, value: inp?.value ?? null }
})
check('B: free-text box opened and staged', staged.found && staged.value === 'use sqlite instead please', JSON.stringify(staged))
await page.evaluate(() => window.useAgentBridge.getState().setPendingQuestion(null))

// ---- C: cross-language dictation is rejected, nothing submitted ------------
await heal()
await seedQuestion('call-c', ['Ship it', 'Hold off'])
await page.evaluate(() => window.__voiceQueue.push({ text: '東京都渋谷区', language: 'zh' }))
await ptt()
calls = await page.evaluate(() => window.__answerCalls)
check('C: no answer submitted on language mismatch', !calls.some((c) => c && c.call_id === 'call-c'))
const toast = await page.evaluate(() => document.body.textContent.includes('different language'))
check('C: rejection toast names the language problem', toast)
await page.evaluate(() => window.useAgentBridge.getState().setPendingQuestion(null))

// ---- D: PTT press cancels a running turn, transcript becomes a message -----
await heal()
await page.evaluate(() => {
  const s = window.useAgentBridge.getState()
  const ac = new AbortController()
  s.setAbortController(ac)
  s.setStatus('thinking')
  window.__turnAborted = false
  ac.signal.addEventListener('abort', () => (window.__turnAborted = true))
})
await page.evaluate(() => window.__voiceQueue.push({ text: 'start over with python', language: 'en' }))
await page.evaluate(() => window.dispatchEvent(new CustomEvent('ptt-press')))
await new Promise((r) => setTimeout(r, 150))
const aborted = await page.evaluate(() => window.__turnAborted)
check('D: press aborts the in-flight turn', aborted)
await page.evaluate(() => {
  // simulate the dying stream's finally: it releases the store controller
  window.useAgentBridge.getState().setAbortController(null)
  window.useAgentBridge.getState().setStatus('idle')
})
await page.evaluate(() => window.dispatchEvent(new CustomEvent('ptt-release')))
await new Promise((r) => setTimeout(r, 500))
const turns = await page.evaluate(() => window.__turnCalls)
check('D: transcript sent as its own message', Array.isArray(turns) && turns.some((t) => t.body?.message === 'start over with python'), JSON.stringify(turns?.map((t) => t.body?.message)))

// ---- E: a pending question shields the turn from the interrupt -------------
await heal()
await seedQuestion('call-e', ['Ship it', 'Hold off'])
await page.evaluate(() => {
  const s = window.useAgentBridge.getState()
  const ac = new AbortController()
  s.setAbortController(ac)
  s.setStatus('thinking')
  window.__turnAborted2 = false
  ac.signal.addEventListener('abort', () => (window.__turnAborted2 = true))
})
await page.evaluate(() => window.dispatchEvent(new CustomEvent('ptt-press')))
await new Promise((r) => setTimeout(r, 150))
check('E: press does NOT cancel a question-blocked turn', !(await page.evaluate(() => window.__turnAborted2)))
await page.evaluate(() => {
  window.__voiceQueue.push({ text: '', language: null }) // silent release: no-op
  window.useAgentBridge.getState().setAbortController(null)
  window.useAgentBridge.getState().setStatus('idle')
  window.useAgentBridge.getState().setPendingQuestion(null)
})
await page.evaluate(() => window.dispatchEvent(new CustomEvent('ptt-release')))
await new Promise((r) => setTimeout(r, 400))

// ---- F: voice path sanity (every hold reached the transcribe stub) ---------
const voiceLog = await page.evaluate(() => window.__voiceLog)
check(
  'F: transcription endpoint reached with audio on every hold',
  voiceLog.length === 5 && voiceLog.every((v) => v.size > 0),
  JSON.stringify(voiceLog.map((v) => ({ size: v.size, text: v.text }))),
)

await page.screenshot({ path: '.impeccable/shots/ptt-voice-final.png' })
await browser.close()

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed: ${failures.join(' | ')}`)
  process.exit(1)
}
console.log('\nAll PTT voice checks passed.')
process.exit(0)
