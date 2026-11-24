// Regression: the ErrorBoundary's "the frontend hit an error it could not
// render" screen, `TypeError: (t.options ?? []).map is not a function`,
// from the real bundle (index-DvLl8Ply.js, `ig` = AskUserTrace). The user
// could not recover by closing and reopening YAAH; only a hard refresh
// helped.
//
// Root cause: #564 normalized the ask_user options payload at the LIVE
// ingestion points (AskUserCard's pending question, the tape path) but
// NOT at the two places that render a SAVED call's args. AskUserTrace
// does `(args.options ?? []).map(...)` — and `?? []` only catches
// null/undefined. A saved call whose options arrived as a JSON *string*
// (the model double-encoded the array; three such rows exist in the real
// database) sails straight into `.map` and throws during render. Because
// the bad row lives in the transcript, every reload re-renders it and
// throws again — the ErrorBoundary's Reload button loops forever. That is
// why only a hard refresh recovered: the asset-hash change dropped the
// cached bundle, and the crash screen was mistaken for a stale-frontend
// problem.
//
// These tests drive the REAL history-load path (buildMessages, the same
// function loadHistory runs) and the REAL MessageView render, with the
// exact saved shapes from the live database. They fail on the pre-fix
// tree with the reported TypeError.

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { buildMessages, useAgent } from './store'
import { MessageView } from './components'
import { normalizeAskOptions } from './askOptions'
import type { StoredMessage } from './api'

const KEY = '1351'

const row = (
  id: number,
  role: string,
  content: string,
  extra: Record<string, unknown> = {},
): StoredMessage =>
  ({
    id,
    role,
    content,
    images: null,
    sub_agent_transcript: null,
    tool_call_id: null,
    tool_calls: null,
    ...extra,
  }) as StoredMessage

/** The real shape from the live database (message 242029, conv 1351): the
 *  model passed the options array as a JSON *string* instead of an array.
 *  Both rows in conv 1351 look like this. */
const doubleEncodedOptions = JSON.stringify([
  {
    label: 'All role addresses',
    description: 'All role mail lands in one mailbox.',
  },
  { label: 'Per-address, I will specify', description: 'Decide each one.' },
])

const askCall = (callId: string, options: unknown) => ({
  id: callId,
  type: 'function',
  function: {
    name: 'ask_user',
    arguments: JSON.stringify({
      question: "Role addresses aren't personal — where should they land?",
      options,
    }),
  },
})

const callRow = (id: number, callId: string, result: unknown) =>
  row(id, 'tool', JSON.stringify(result), {
    tool_call_id: callId,
    tool_calls: [{ id: callId, type: 'function', function: { name: 'ask_user' } }],
  })

function historyRows() {
  return [
    row(1, 'user', 'where should role mail land?'),
    row(2, 'assistant', 'Two ways to split it.', {
      tool_calls: [askCall('chatcmpl-tool-8914c1a61f7b64f7', doubleEncodedOptions)],
    }),
    callRow(3, 'chatcmpl-tool-8914c1a61f7b64f7', {
      answer: 'ahh, got it. ok you can route the rfc required email addresses to jesse',
    }),
    row(4, 'assistant', 'Routing them to jesse.'),
  ]
}

function renderHistory() {
  cleanup()
  const msgs = buildMessages(historyRows())
  render(
    <div>
      {msgs.map((m) => (
        <MessageView key={m.id} msg={m} />
      ))}
    </div>,
  )
}

describe('AskUserTrace renders a saved call whose options arrived as a string', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: Number(KEY),
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
    })
  })

  it('normalizeAskOptions recovers a double-encoded JSON array', () => {
    // The payload the model actually produced. `?? []` cannot save this.
    expect(normalizeAskOptions(doubleEncodedOptions)).toEqual([
      { label: 'All role addresses', description: 'All role mail lands in one mailbox.' },
      { label: 'Per-address, I will specify', description: 'Decide each one.' },
    ])
  })

  it('normalizeAskOptions still drops a string it cannot parse', () => {
    // The other real shape (message 182143, conv 1054): a numbered list in
    // plain prose, not JSON. Unrecoverable — render no option rows rather
    // than throwing.
    const prose = '#1 strict sections\n#1b guided sections\n#2 loose order'
    expect(normalizeAskOptions(prose)).toEqual([])
  })

  it('renders the saved answered question instead of throwing', () => {
    // Pre-fix this throws TypeError: (args.options ?? []).map is not a
    // function — the exact reported crash.
    expect(() => renderHistory()).not.toThrow()
    // The recovered options render as real rows.
    expect(document.body.textContent).toContain('All role addresses')
    expect(document.body.textContent).toContain('Per-address, I will specify')
    // The answer the user gave still shows, and is not duplicated as an
    // extra "chosen" row: normalizeAskOptions recovered the labels, so the
    // free-text answer is the only one that needs the fallback row.
    expect(document.body.textContent).toContain('route the rfc required email addresses')
  })

  it('renders an unparseable options string without throwing', () => {
    cleanup()
    const msgs = buildMessages([
      row(1, 'user', 'which compaction shape?'),
      row(2, 'assistant', 'One design choice.', {
        tool_calls: [askCall('call_01a17528a89a7721ab92ee11', '#1 strict sections\n#1b guided\n#2 loose')],
      }),
      callRow(3, 'call_01a17528a89a7721ab92ee11', { answer: null, note: 'not answered' }),
    ])
    expect(() =>
      render(
        <div>
          {msgs.map((m) => (
            <MessageView key={m.id} msg={m} />
          ))}
        </div>,
      ),
    ).not.toThrow()
    expect(document.body.textContent).toContain('One design choice.')
  })

  it('does not crash when the whole args payload is a string', () => {
    // safeParse returns the raw string when JSON.parse fails, so a call
    // row can hold args as a string too. The trace must not throw on it.
    cleanup()
    const msgs = buildMessages([
      row(1, 'user', 'go'),
      row(2, 'assistant', 'Broken args.', {
        tool_calls: [
          {
            id: 'broken-1',
            type: 'function',
            function: { name: 'ask_user', arguments: 'not json at all' },
          },
        ],
      }),
      callRow(3, 'broken-1', { answer: 'yes' }),
    ])
    expect(() =>
      render(
        <div>
          {msgs.map((m) => (
            <MessageView key={m.id} msg={m} />
          ))}
        </div>,
      ),
    ).not.toThrow()
  })
})

describe('the ErrorBoundary screen no longer blocks recovery', () => {
  it('a saved bad row renders as a card, so the boundary never mounts', () => {
    // The user's symptom: the boundary screen came back on every reload.
    // With the fix the transcript renders, so there is nothing to recover
    // from. Spy on console.error to prove React logged no render throw.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    renderHistory()
    expect(spy).not.toHaveBeenCalled()
    spy.mockRestore()
  })
})
