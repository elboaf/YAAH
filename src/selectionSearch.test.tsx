// #201: right-click a chat-transcript selection -> "Search on Google" in the
// default browser via the existing openExternal path. Pins (a) the selection
// trigger rules and query construction (helpers), and (b) the menu visibility
// on the real ChatPanel (DOM test).

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, cleanup, fireEvent, createEvent } from '@testing-library/react'
import { transcriptSelection, googleSearchUrl, SEARCH_QUERY_MAX_CHARS } from './selectionSearch'

// --- helpers: trigger rules + query construction -----------------------------

describe('transcriptSelection', () => {
  it('returns collapsed whitespace and trims a multi-line selection', () => {
    document.body.innerHTML = '<div id="t"><pre>line one\n\n   line   two  </pre></div>'
    const t = document.getElementById('t')!
    const pre = t.querySelector('pre')!
    const range = document.createRange()
    range.selectNodeContents(pre)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    expect(transcriptSelection(t)).toBe('line one line two')
  })

  it('returns null for a collapsed (caret) selection', () => {
    document.body.innerHTML = '<div id="t"><p>hello</p></div>'
    const t = document.getElementById('t')!
    const p = t.querySelector('p')!
    const range = document.createRange()
    range.setStart(p.firstChild!, 2)
    range.collapse(true)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    expect(transcriptSelection(t)).toBeNull()
  })

  it('returns null when the selection anchor is outside the transcript', () => {
    document.body.innerHTML = '<div id="t"></div><div id="other"><p>outside text</p></div>'
    const t = document.getElementById('t')!
    const p = document.querySelector('#other p')!
    const range = document.createRange()
    range.selectNodeContents(p)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    expect(transcriptSelection(t)).toBeNull()
  })
})

describe('googleSearchUrl', () => {
  it('builds the google search URL with an encoded query', () => {
    expect(googleSearchUrl('hello world & more')).toBe(
      'https://www.google.com/search?q=hello%20world%20%26%20more',
    )
  })

  it('caps the ENCODED query and never ends mid percent-escape', () => {
    // 'é' encodes to %C3%A9 (6 chars); a cap landing mid-escape must drop the
    // partial escape rather than emit a corrupt URL.
    const url = googleSearchUrl('é'.repeat(1000))
    const q = url.replace('https://www.google.com/search?q=', '')
    expect(q.length).toBeLessThanOrEqual(SEARCH_QUERY_MAX_CHARS)
    // A partial escape would be a trailing '%' or '%<one hex digit>'; a
    // complete %XX is fine (the sample text is full of them).
    expect(q).not.toMatch(/%(?:[0-9a-fA-F])?$/)
  })
})

// --- DOM: menu visibility on the real ChatPanel ------------------------------
// Mirrors narrationQueue.test.tsx's setup: api mocked, store seeded directly.

const apiMocks = vi.hoisted(() => ({
  getConfig: vi.fn(async () => ({})),
  getContext: vi.fn(async () => ({ context_tokens: null, context_window: null, context_model: null })),
  getGitInfo: vi.fn(async () => ({ info: {} })),
  getConversation: vi.fn(async () => { throw new Error('unused') }),
  ttsStatus: vi.fn(async () => ({ available: false, tts_enabled: false })),
  updateAgentModelEffort: vi.fn(async () => ({})),
  updateConversation: vi.fn(async () => ({})),
}))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  ...apiMocks,
}))

import { ChatPanel } from './components'
import { useAgent } from './store'

const TEXT = 'selectable transcript text here'

function seedConversation() {
  useAgent.setState({
    conversationId: 7,
    statusByConv: { '7': 'idle' },
    messagesByConv: { '7': [{ id: 'a1', role: 'assistant', content: TEXT }] },
    pendingQuestions: {},
    pendingApprovals: {},
    pendingPlanApprovals: {},
    errorByConv: {},
  } as never)
}

describe('ChatPanel search-selection context menu', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    window.localStorage.clear()
    seedConversation()
  })
  afterEach(() => {
    cleanup()
    window.getSelection()?.removeAllRanges()
  })

  it('shows the Search on Google item on right-click with a transcript selection', async () => {
    const { findByRole, getByText } = render(<ChatPanel />)
    const para = getByText(TEXT)
    const range = document.createRange()
    range.selectNodeContents(para)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.contextMenu(para)
    const item = await findByRole('menuitem', { name: /search on google/i })
    expect(item).toBeTruthy()
  })

  it('shows no menu item on right-click without a selection', () => {
    const { getByText, queryByRole } = render(<ChatPanel />)
    const para = getByText(TEXT)
    fireEvent.contextMenu(para)
    expect(queryByRole('menuitem', { name: /search on google/i })).toBeNull()
  })

  // #276: ONE menu, at the cursor. When a selection exists the custom menu
  // must be positioned at the contextmenu pointer coordinates.
  it('positions the menu at the contextmenu event coordinates', async () => {
    const { findByRole, getByText, getByRole } = render(<ChatPanel />)
    const para = getByText(TEXT)
    const range = document.createRange()
    range.selectNodeContents(para)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.contextMenu(para, { clientX: 240, clientY: 180 })
    const menu = await findByRole('menu', { name: /search selection/i })
    expect((menu as HTMLElement).style.left).toBe('240px')
    expect((menu as HTMLElement).style.top).toBe('180px')
    expect(getByRole('menuitem', { name: /search on google/i })).toBeTruthy()
  })

  // #276: the menu carries Copy alongside Search on Google.
  it('offers a Copy item that writes the selection to the clipboard', async () => {
    const writeText = vi.fn(async () => {})
    Object.assign(navigator, { clipboard: { writeText } })
    const { findByRole, getByText } = render(<ChatPanel />)
    const para = getByText(TEXT)
    const range = document.createRange()
    range.selectNodeContents(para)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.contextMenu(para)
    fireEvent.click(await findByRole('menuitem', { name: /^copy$/i }))
    expect(writeText).toHaveBeenCalledWith('selectable transcript text here')
    // The menu closes after acting on it.
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  // #276: Escape closes the custom menu.
  it('closes on Escape', async () => {
    const { findByRole, getByText } = render(<ChatPanel />)
    const para = getByText(TEXT)
    const range = document.createRange()
    range.selectNodeContents(para)
    const sel = window.getSelection()!
    sel.removeAllRanges()
    sel.addRange(range)
    fireEvent.contextMenu(para)
    expect(await findByRole('menu', { name: /search selection/i })).toBeTruthy()
    fireEvent.keyDown(document.querySelector('[role="menu"]')!, { key: 'Escape' })
    expect(document.querySelector('[role="menu"]')).toBeNull()
  })

  // #276: with NO selection the transcript must not suppress the native
  // menu (preventDefault is only called for the custom at-cursor menu).
  it('does not preventDefault the contextmenu when there is no selection', () => {
    const { getByText } = render(<ChatPanel />)
    const para = getByText(TEXT)
    const event = createEvent.contextMenu(para)
    fireEvent(para, event)
    expect(event.defaultPrevented).toBe(false)
  })
})
