// Tests for the plan-approval chat split (approved exit_plan): the live
// splitAtPlanApproval action and the buildMessages history-load rule must
// agree — the planning emission and the execution half render as separate
// messages, the execution one flagged with the plan it implements.

import { describe, expect, it, beforeEach } from 'vitest'
import { useAgent, buildMessages } from './store'

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
