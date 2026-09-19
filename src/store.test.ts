// Tests for the plan-approval chat split (approved exit_plan): the live
// splitAtPlanApproval action and the buildMessages history-load rule must
// agree — the planning emission and the execution half render as separate
// messages, the execution one flagged with the plan it implements.
// Also covers splitAtStepBoundary (issue #17): the live stream pins each
// model call's emission and opens a fresh message for the next one.

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
    expect(msgs).toHaveLength(4)
    expect(msgs[1].implementsPlan).toBeUndefined()
    expect(msgs[2].implementsPlan).toBe('THE PLAN')
    // Only the first execution message carries the flag.
    expect(msgs[3].implementsPlan).toBeUndefined()
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

describe('splitAtStepBoundary (live, issue #17)', () => {
  beforeEach(() => {
    useAgent.setState({ messagesByConv: {} })
  })

  it('closes the emission and opens a fresh assistant message', () => {
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        t: [
          { id: 'u', role: 'user', content: 'go' },
          {
            id: 'a1',
            role: 'assistant',
            content: 'first emission',
            toolCalls: [{ id: 'c1', name: 'bash', args: {}, result: { ok: 1 } }],
          },
        ],
      },
    }))
    const newId = useAgent.getState().splitAtStepBoundary('t', 'a1')
    expect(newId).toBeTruthy()
    const msgs = useAgent.getState().messagesByConv.t
    expect(msgs).toHaveLength(3)
    // The finished emission keeps its text and its own tool calls...
    expect(msgs[1].content).toBe('first emission')
    expect(msgs[1].toolCalls).toHaveLength(1)
    // ...and the next one starts empty for the following model call.
    expect(msgs[2].id).toBe(newId)
    expect(msgs[2].role).toBe('assistant')
    expect(msgs[2].content).toBe('')
    expect(msgs[2].toolCalls).toBeUndefined()
  })

  it('does not stack an empty message after a plan-approval split', () => {
    // The stream advanced past msgId (splitAtPlanApproval opened a fresh
    // tail); the boundary must leave that fresh tail alone.
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        t: [
          { id: 'a1', role: 'assistant', content: 'planning' },
          { id: 'a2', role: 'assistant', content: '', implementsPlan: 'P' },
        ],
      },
    }))
    expect(useAgent.getState().splitAtStepBoundary('t', 'a1')).toBeNull()
    const msgs = useAgent.getState().messagesByConv.t
    expect(msgs).toHaveLength(2)
    expect(msgs[1].id).toBe('a2')
  })

  it('is a no-op for an unknown message id', () => {
    useAgent.setState((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        t: [{ id: 'a1', role: 'assistant', content: 'x' }],
      },
    }))
    expect(useAgent.getState().splitAtStepBoundary('t', 'missing')).toBeNull()
    expect(useAgent.getState().messagesByConv.t).toHaveLength(1)
  })
})
