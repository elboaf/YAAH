// Regression (#460): queued messages handed back at run end must land in the
// conversation THAT RAN — never open a new chat, never ride into whatever
// chat the composer has switched to.
//
// The production sequence this pins:
//  1. The user types into the DRAFT (new-chat screen) and sends. `send`
//     creates the conversation, adopts it, and streams the turn.
//  2. The run ends with items still queued; the backend emits
//     `queued_autosend`, and send's `finally` hands them to
//     `drainQueueOnEnd`.
//  3. The drain re-enters `send` — but through the closure captured by the
//     FIRST render (where `conversationId` was still null, because the
//     composer sat on the draft when the component mounted). Before the fix,
//     that closure resolved its destination from render-time state and took
//     the `createConversation` branch: a SECOND chat was born with the
//     queued message as its first turn.
//
// The fix pins an explicit `target` (the drained conversation id) onto the
// hand-off send, and resolves the target's workspace from the conversation's
// own row (via the convWorkspaceCache the sidebar feeds) instead of the
// active workspace at drain time.
//
// Run: npx vitest run src/queueDrainTarget.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const streamCalls: Array<{ cid: number; message: string; workspace: string }> = []
let createConversationCalls = 0

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig: vi.fn(async () => ({})),
    getContext: vi.fn(async () => ({ context_tokens: null, context_window: null, context_model: null })),
    getGitInfo: vi.fn(async () => ({ info: {} })),
    getConversation: vi.fn(async (id: number) => ({
      id,
      title: 'first',
      workspace: 'C:/w/a',
      created_at: '',
      updated_at: '',
    })),
    listConversations: vi.fn(async () => []),
    updateAgentModelEffort: vi.fn(async () => ({})),
    updateConversation: vi.fn(async () => ({})),
    createConversation: vi.fn(async (title: string) => {
      createConversationCalls += 1
      // The queued drain must never file a new conversation. Fail loudly if
      // it does — a thrown error here IS the regression signal.
      if (createConversationCalls > 1) {
        throw new Error(`#460 regression: a SECOND conversation was created ("${title}")`)
      }
      return { id: 7 }
    }),
    streamAgentTurn: vi.fn(
      async (cid: number, message: string, workspace: string, onEvent: (ev: { type: string; items?: Array<{ id: number; text: string }> }) => void) => {
        streamCalls.push({ cid, message, workspace })
        if (message === 'first') {
          // The run ends with an item still queued: the backend hands it
          // back through this event; send's finally drains it.
          onEvent({ type: 'queued_autosend', items: [{ id: 1, text: 'follow up' }] })
        }
      },
    ),
  }
})

import { ChatPanel } from './components'
import { convWorkspaceCache, useAgent } from './store'

beforeEach(() => {
  streamCalls.length = 0
  createConversationCalls = 0
  convWorkspaceCache.clear()
  convWorkspaceCache.set('7', 'C:/w/a')
  useAgent.setState({
    conversationId: null,
    messagesByConv: { draft: [] },
    statusByConv: {},
    queueEchoByConv: {},
    pendingQuestions: {},
    pendingApprovals: {},
    pendingPlanApprovals: {},
    errorByConv: {},
    draftDestination: null,
    draftScope: null,
  })
})

afterEach(() => {
  cleanup()
  convWorkspaceCache.clear()
})

describe('end-of-run queue drain targets the conversation that ran (#460)', () => {
  it('a drain from a draft-born run never opens a second chat — it continues conversation 7', async () => {
    render(<ChatPanel />)
    const textarea = await screen.findByLabelText('Message the agent')
    fireEvent.change(textarea, { target: { value: 'first' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    // Turn 1 streams to the newly created conversation 7; the queued item
    // is handed back at run end and must land in the SAME conversation.
    await waitFor(() => expect(streamCalls.length).toBe(2))

    expect(createConversationCalls).toBe(1)
    // The draft turn uses the active workspace (historical #94 behavior);
    // in this test the store's active workspace is the '' default.
    expect(streamCalls[0]).toEqual({ cid: 7, message: 'first', workspace: '' })
    // The drained item: same conversation, its own workspace — not the
    // active workspace (which the user may have switched mid-run).
    expect(streamCalls[1].cid).toBe(7)
    expect(streamCalls[1].message).toBe('follow up')
    expect(streamCalls[1].workspace).toBe('C:/w/a')
  })

  it('the drain resolves the workspace from the conversation row, not the active workspace', async () => {
    render(<ChatPanel />)
    const textarea = await screen.findByLabelText('Message the agent')
    fireEvent.change(textarea, { target: { value: 'first' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })
    await waitFor(() => expect(streamCalls.length).toBe(2))
    // convWorkspaceCache holds C:/w/a for conversation 7 while the store's
    // active workspace sat elsewhere (untouched default here) — the drained
    // turn must use the conversation's own workspace.
    expect(streamCalls[1].workspace).toBe('C:/w/a')
  })
})
