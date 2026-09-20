// Tests for the plan-approval chat split (approved exit_plan): the live
// splitAtPlanApproval action and the buildMessages history-load rule must
// agree — the planning emission and the execution half render as separate
// messages, the execution one flagged with the plan it implements.

import { describe, expect, it, beforeEach } from 'vitest'
import { useAgent, buildMessages } from './store'
import { shouldChimeFinish, isUnfocused } from './NotificationSounds'

const row = (id: number, role: string, content: string, extra: Record<string, unknown> = {}) => ({
  id,
  role,
  content,
  images: null,
  sub_agent_transcript: null,
  tool_call_id: null,
  tool_calls: null,
  ...extra,
})

const exitPlanCall = (callId: string, plan: string) => ({
  id: callId,
  function: { name: 'exit_plan', arguments: JSON.stringify({ plan }) },
})

const callRow = (id: number, callId: string, name: string, result: unknown) =>
  row(id, 'tool', JSON.stringify(result), {
    tool_call_id: callId,
    tool_calls: [{ id: callId, function: { name } }],
  })

describe('buildMessages plan split', () => {
  it('flags the message after an approved exit_plan with the plan text', () => {
    const msgs = buildMessages([
      row(1, 'user', 'do the thing'),
      row(2, 'assistant', 'researching...', { tool_calls: [exitPlanCall('c1', 'THE PLAN')] }),
      callRow(3, 'c1', 'exit_plan', { decision: 'approved', note: 'ok' }),
      row(4, 'assistant', 'implementing', {
        tool_calls: [{ id: 'c2', function: { name: 'edit_file', arguments: '{}' } }],
      }),
      callRow(5, 'c2', 'edit_file', { ok: true }),
      row(6, 'assistant', 'done!'),
    ])
    // Tool-result rows are absorbed into their assistant call's trace.
    // Issue #17: the final 'done!' emission is part of the same turn block
    // as 'implementing' (one block, newline-joined) — live parity.
    expect(msgs).toHaveLength(3)
    expect(msgs[1].implementsPlan).toBeUndefined()
    expect(msgs[2].implementsPlan).toBe('THE PLAN')
    expect(msgs[2].content).toBe('implementing\ndone!')
  })

  it('does not split on a revised (unapproved) exit_plan', () => {
    const msgs = buildMessages([
      row(2, 'assistant', 'planning v1', { tool_calls: [exitPlanCall('c1', 'PLAN')] }),
      callRow(3, 'c1', 'exit_plan', { decision: 'revised', feedback: 'smaller' }),
      row(4, 'assistant', 'planning v2', { tool_calls: [exitPlanCall('c9', 'PLAN 2')] }),
    ])
    expect(msgs.every((m) => m.implementsPlan === undefined)).toBe(true)
  })

  it('keeps the flag off entirely when no exit_plan is present', () => {
    const msgs = buildMessages([
      row(1, 'assistant', 'hi', {
        tool_calls: [{ id: 'c1', function: { name: 'read_file', arguments: '{}' } }],
      }),
      callRow(2, 'c1', 'read_file', { content: 'x' }),
    ])
    expect(msgs.every((m) => m.implementsPlan === undefined)).toBe(true)
  })
})

describe('buildMessages emission coalescing (#17)', () => {
  it('joins consecutive emission rows of one turn with a single line feed', () => {
    const msgs = buildMessages([
      row(1, 'user', 'go'),
      row(2, 'assistant', 'first emission', {
        tool_calls: [{ id: 'c1', function: { name: 'bash', arguments: '{}' } }],
      }),
      callRow(3, 'c1', 'bash', { ok: true }),
      row(4, 'assistant', 'second emission', {
        tool_calls: [{ id: 'c2', function: { name: 'bash', arguments: '{}' } }],
      }),
      callRow(5, 'c2', 'bash', { ok: true }),
      row(6, 'assistant', 'third emission'),
    ])
    // One turn block: all three emissions newline-joined, tool calls pooled.
    expect(msgs).toHaveLength(2)
    expect(msgs[1].content).toBe('first emission\nsecond emission\nthird emission')
    expect(msgs[1].toolCalls?.map((t) => t.id)).toEqual(['c1', 'c2'])
  })

  it('starts a new block after a user row', () => {
    const msgs = buildMessages([
      row(1, 'user', 'q1'),
      row(2, 'assistant', 'answer one'),
      row(3, 'user', 'q2'),
      row(4, 'assistant', 'answer two'),
    ])
    expect(msgs).toHaveLength(4)
    expect(msgs[1].content).toBe('answer one')
    expect(msgs[3].content).toBe('answer two')
  })

  it('keeps the planning emission split from the plan-approved implementation block', () => {
    const msgs = buildMessages([
      row(1, 'user', 'do it'),
      row(2, 'assistant', 'planning...', { tool_calls: [exitPlanCall('c1', 'THE PLAN')] }),
      callRow(3, 'c1', 'exit_plan', { decision: 'approved' }),
      row(4, 'assistant', 'implementing'),
    ])
    expect(msgs).toHaveLength(3)
    expect(msgs[1].content).toBe('planning...')
    expect(msgs[2].implementsPlan).toBe('THE PLAN')
    expect(msgs[2].content).toBe('implementing')
  })
})

describe('splitAtPlanApproval (live)', () => {
  beforeEach(() => {
    useAgent.setState({ messagesByConv: {} })
  })

  const seedPlanning = () => {
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        t: [
          { id: 'u', role: 'user', content: 'do it' },
          {
            id: 'a',
            role: 'assistant',
            content: 'thinking...',
            toolCalls: [
              {
                id: 'c1',
                name: 'exit_plan',
                args: { plan: 'THE PLAN' },
                result: { decision: 'approved' },
              },
            ],
          },
        ],
      },
    }))
  }

  it('inserts a new execution message carrying the plan and returns its id', () => {
    seedPlanning()
    const newId = useAgent.getState().splitAtPlanApproval('t', 'a')
    expect(newId).toBeTruthy()
    const msgs = useAgent.getState().messagesByConv.t
    expect(msgs).toHaveLength(3)
    expect(msgs[1].id).toBe('a')
    expect(msgs[2].implementsPlan).toBe('THE PLAN')
    expect(msgs[2].content).toBe('')
  })

  it('is a no-op when the exit_plan result is not approved', () => {
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        t: [
          {
            id: 'a',
            role: 'assistant',
            content: 'planning',
            toolCalls: [
              { id: 'c1', name: 'exit_plan', args: { plan: 'P' }, result: undefined },
            ],
          },
        ],
      },
    }))
    expect(useAgent.getState().splitAtPlanApproval('t', 'a')).toBeNull()
    expect(useAgent.getState().messagesByConv.t).toHaveLength(1)
  })
})

// ------------------------------------------------------------------ #10
// Multiple concurrent chats: per-conversation status, per-conversation
// abort handles, per-conversation pending gates, and loadHistory refusing
// to clobber a buffer a live run owns.

describe('concurrent chats (issue #10)', () => {
  beforeEach(() => {
    useAgent.setState({
      messagesByConv: {},
      statusByConv: {},
      errorByConv: {},
      abortByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
    })
  })

  it('keeps run status isolated per conversation', () => {
    useAgent.getState().setStatus('1', 'thinking')
    useAgent.getState().setStatus('2', 'running-tool')
    useAgent.getState().setStatus('1', 'idle')
    expect(useAgent.getState().statusByConv['1']).toBe('idle')
    expect(useAgent.getState().statusByConv['2']).toBe('running-tool')
  })

  it('scopes abort controllers to their conversation', () => {
    const a = new AbortController()
    const b = new AbortController()
    useAgent.getState().setAbortController('1', a)
    useAgent.getState().setAbortController('2', b)
    expect(useAgent.getState().abortByConv['1']).toBe(a)
    useAgent.getState().setAbortController('1', null)
    expect(useAgent.getState().abortByConv['1']).toBeUndefined()
    // The other conversation's run is untouched.
    expect(useAgent.getState().abortByConv['2']).toBe(b)
  })

  it('keeps pending ask_user questions isolated per conversation', () => {
    useAgent.getState().setPendingQuestion({
      callId: 'c1',
      question: 'q?',
      options: [],
      convKey: '1',
    })
    expect(Object.keys(useAgent.getState().pendingQuestions)).toEqual(['1'])
    // Clearing by callId leaves other conversations' gates alone.
    useAgent.getState().setPendingQuestion((q) => (q && q.callId === 'nope' ? null : q))
    expect(useAgent.getState().pendingQuestions['1']).toBeDefined()
    useAgent.getState().setPendingQuestion((q) => (q && q.callId === 'c1' ? null : q))
    expect(useAgent.getState().pendingQuestions['1']).toBeUndefined()
  })

  it('keeps pending plan approvals isolated per conversation', () => {
    useAgent.getState().setPendingPlanApproval({
      callId: 'p1',
      plan: 'do it',
      convKey: '7',
    })
    useAgent.getState().setPendingPlanApproval({
      callId: 'p2',
      plan: 'also',
      convKey: '8',
    })
    expect(Object.keys(useAgent.getState().pendingPlanApprovals).sort()).toEqual(['7', '8'])
    useAgent.getState().setPendingPlanApproval((p) => (p && p.callId === 'p1' ? null : p))
    expect(useAgent.getState().pendingPlanApprovals['7']).toBeUndefined()
    expect(useAgent.getState().pendingPlanApprovals['8']).toBeDefined()
  })

  it('loadHistory skips the reload while a run owns the buffer', () => {
    useAgent.setState({
      statusByConv: { '5': 'thinking' },
      messagesByConv: {
        '5': [{ id: 'live1', role: 'assistant', content: 'streaming…' }],
      },
    })
    useAgent.getState().loadHistory(5, [
      { id: 1, role: 'user', content: 'old', images: null, sub_agent_transcript: null, tool_call_id: null, tool_calls: null },
    ])
    // The live in-flight message survives — history must not clobber it.
    expect(useAgent.getState().messagesByConv['5'].map((m) => m.id)).toEqual(['live1'])
    // Once the run is over, the same reload goes through.
    useAgent.getState().setStatus('5', 'idle')
    useAgent.getState().loadHistory(5, [
      { id: 1, role: 'user', content: 'old', images: null, sub_agent_transcript: null, tool_call_id: null, tool_calls: null },
    ])
    expect(useAgent.getState().messagesByConv['5'].map((m) => m.id)).toEqual(['db1'])
  })
})

describe('sidebar finish signal (issue #25)', () => {
  beforeEach(() => {
    useAgent.setState({ statusByConv: {}, finishedByConv: {}, conversationId: null })
  })

  it('sets an ok signal when a background chat finishes', () => {
    useAgent.setState({ statusByConv: { '7': 'running-tool' }, conversationId: null })
    useAgent.getState().setStatus('7', 'idle')
    expect(useAgent.getState().finishedByConv['7']).toBe('ok')
  })

  it('sets an error signal when a background chat fails', () => {
    useAgent.setState({ statusByConv: { '7': 'running-tool' }, conversationId: null })
    useAgent.getState().setStatus('7', 'error')
    expect(useAgent.getState().finishedByConv['7']).toBe('error')
  })

  it('does not signal when the run finishes in the on-screen chat (Q4/Q8)', () => {
    useAgent.setState({ statusByConv: { '7': 'thinking' }, conversationId: 7 })
    useAgent.getState().setStatus('7', 'idle')
    expect(useAgent.getState().finishedByConv['7']).toBeUndefined()
  })

  it('does not signal retroactively on switch-away (Q8)', () => {
    // Finish watched on-screen -> no signal; opening another chat afterwards
    // must not manufacture one.
    useAgent.setState({ statusByConv: { '7': 'thinking' }, conversationId: 7 })
    useAgent.getState().setStatus('7', 'idle')
    useAgent.getState().setConversationId(5)
    expect(useAgent.getState().finishedByConv['7']).toBeUndefined()
  })

  it('does not signal when there was no run to finish (abort-style status delete)', () => {
    useAgent.setState({ statusByConv: {}, conversationId: null })
    useAgent.getState().setStatus('7', 'idle')
    expect(useAgent.getState().finishedByConv['7']).toBeUndefined()
  })

  it('does not signal for keys without a sidebar row (draft buffers)', () => {
    useAgent.setState({ statusByConv: { draft: 'thinking' }, conversationId: null })
    useAgent.getState().setStatus('draft', 'idle')
    expect(useAgent.getState().finishedByConv['draft']).toBeUndefined()
  })

  it('clears the signal when the chat is opened', () => {
    useAgent.setState({ statusByConv: { '7': 'idle' }, finishedByConv: { '7': 'ok' }, conversationId: null })
    useAgent.getState().setConversationId(7)
    expect(useAgent.getState().finishedByConv['7']).toBeUndefined()
  })

  it('keeps signals isolated per conversation', () => {
    useAgent.setState({
      statusByConv: { '7': 'running-tool', '9': 'running-tool' },
      conversationId: null,
    })
    useAgent.getState().setStatus('7', 'idle')
    useAgent.getState().setStatus('9', 'error')
    expect(useAgent.getState().finishedByConv).toEqual({ '7': 'ok', '9': 'error' })
    useAgent.getState().setConversationId(7)
    expect(useAgent.getState().finishedByConv).toEqual({ '9': 'error' })
  })
})

describe('notification chimes (#29) - pure helpers', () => {
  it('chimes on running -> idle and running -> error, not on idle -> idle', () => {
    expect(shouldChimeFinish('thinking', 'idle')).toBe(true)
    expect(shouldChimeFinish('running-tool', 'error')).toBe(true)
    expect(shouldChimeFinish('idle', 'idle')).toBe(false)
    expect(shouldChimeFinish('idle', 'thinking')).toBe(false)
    expect(shouldChimeFinish(undefined, 'idle')).toBe(false)
  })

  it('unfocused = hidden document or lost window focus', () => {
    const doc = (hidden: boolean) => ({ hidden }) as Document
    const win = (focused: boolean) =>
      ({ document: { hasFocus: () => focused } }) as unknown as Window
    expect(isUnfocused(doc(true), win(true))).toBe(true)
    expect(isUnfocused(doc(false), win(false))).toBe(true)
    expect(isUnfocused(doc(false), win(true))).toBe(false)
  })
})
