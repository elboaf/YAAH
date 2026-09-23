// #93: scheduled agents that ask. A scheduled run streams inside the backend
// and reaches the UI only as tape events — these tests pin (a) the pure
// mapping from tape event to pending-question card state and (b) the store
// action that applies it; the integration test drives the real
// AgentChatLiveFollow poll with a mocked tape feed (the card can never
// render — or route its answer — without this channel).

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, waitFor, cleanup } from '@testing-library/react'
import { tapeQuestionAction } from './store'

// --- pure mapping: tape event -> card action ---------------------------------

describe('tapeQuestionAction', () => {
  it('opens a card from an ask_user tool_start with the question payload', () => {
    const act = tapeQuestionAction(
      {
        type: 'tool_start',
        name: 'ask_user',
        call_id: 'q1',
        args: { question: 'Ship rc.7?', options: [{ label: 'Yes' }, { label: 'No' }] },
      },
      '42',
    )
    expect(act).toEqual({
      op: 'set',
      callId: 'q1',
      question: 'Ship rc.7?',
      options: [{ label: 'Yes' }, { label: 'No' }],
      convKey: '42',
    })
  })

  it('clears the card on the ask_user tool_result (answer or not-answered)', () => {
    expect(
      tapeQuestionAction({ type: 'tool_result', name: 'ask_user', call_id: 'q1' }, '42'),
    ).toEqual({ op: 'clear', convKey: '42' })
  })

  it('clears on turn-terminal events — a crashed run leaves no stale card', () => {
    for (const type of ['done', 'stopped', 'error']) {
      expect(tapeQuestionAction({ type, name: 'ask_user', call_id: 'q1' }, '42')).toEqual({
        op: 'clear',
        convKey: '42',
      })
    }
  })

  it('ignores non-question events and other tools', () => {
    expect(tapeQuestionAction({ type: 'text', text: 'hi' }, '42')).toBeNull()
    expect(
      tapeQuestionAction({ type: 'tool_start', name: 'bash', call_id: 'b1', args: {} }, '42'),
    ).toBeNull()
    expect(tapeQuestionAction({ type: 'tool_progress', name: 'ask_user' }, '42')).toBeNull()
  })
})

// --- integration: the live-follow poll feeds the card store ------------------

// Controllable tape feed; the describe block below programs it per test.
let tapePages: Array<{
  running: boolean
  offset: number
  events: Array<Record<string, unknown>>
}> = []

vi.mock('./api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('./api')>()
  return {
    ...orig,
    getAgentTape: vi.fn(async (_cid: number, after: number) => {
      const page = tapePages[Math.min(after, tapePages.length - 1)]
      return page ?? { running: false, offset: after, events: [] }
    }),
    getMessages: vi.fn(async () => []),
  }
})

describe('AgentChatLiveFollow question sync (#93)', () => {
  beforeEach(() => {
    tapePages = []
  })

  afterEach(() => {
    cleanup()
    vi.resetModules()
  })

  it('a taped ask_user lands in pendingQuestions with the question payload', async () => {
    tapePages = [
      {
        running: true,
        offset: 1,
        events: [
          {
            type: 'tool_start',
            name: 'ask_user',
            call_id: 'q1',
            args: { question: 'Ship it?', options: [{ label: 'Yes' }] },
          },
        ],
      },
    ]
    vi.resetModules()
    const { useAgent } = await import('./store')
    const { AgentChatLiveFollow } = await import('./components')
    useAgent.setState({
      agents: [{ running: true, conversation_id: 42 }] as never,
      conversationId: 42,
      pendingQuestions: {},
    })
    render(<AgentChatLiveFollow />)
    await waitFor(() => {
      const q = useAgent.getState().pendingQuestions['42']
      if (!q || q.callId !== 'q1') throw new Error('card not synced yet')
    })
    expect(useAgent.getState().pendingQuestions['42']).toMatchObject({
      callId: 'q1',
      question: 'Ship it?',
      convKey: '42',
    })
  })

  it('an answered tape tool_result clears the card', async () => {
    tapePages = [
      {
        running: true,
        offset: 1,
        events: [
          {
            type: 'tool_result',
            name: 'ask_user',
            call_id: 'q1',
            result: { answer: 'Yes' },
          },
        ],
      },
    ]
    vi.resetModules()
    const { useAgent } = await import('./store')
    const { AgentChatLiveFollow } = await import('./components')
    useAgent.setState({
      agents: [{ running: true, conversation_id: 42 }] as never,
      conversationId: 42,
      pendingQuestions: {
        '42': { callId: 'q1', question: 'Ship it?', options: [], convKey: '42' },
      },
    })
    render(<AgentChatLiveFollow />)
    await waitFor(() => {
      if (useAgent.getState().pendingQuestions['42']) throw new Error('card still pending')
    })
  })
})
