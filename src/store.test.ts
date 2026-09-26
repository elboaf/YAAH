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

describe('buildMessages sub-agent snapshot', () => {
  it('rehydrates nested tool args/results and aggregates assistant text', () => {
    const msgs = buildMessages([
      row(1, 'assistant', 'spawned', {
        tool_calls: [{
          id: 'spawn-1',
          function: { name: 'spawn_agent', arguments: JSON.stringify({ agent_type: 'explore', prompt: 'inspect' }) },
        }],
      }),
      row(2, 'tool', JSON.stringify({ status: 'completed' }), {
        tool_call_id: 'spawn-1',
        tool_calls: [{ id: 'spawn-1', function: { name: 'spawn_agent' } }],
        sub_agent_transcript: {
          agent_type: 'explore',
          status: 'completed',
          transcript: [
            { role: 'user', content: 'inspect' },
            { role: 'assistant', content: 'checking' },
            { role: 'tool', tool_call_id: 'inner-1', name: 'read_file', args: { path: 'a.txt' }, content: JSON.stringify({ content: 'hello' }) },
            { role: 'assistant', content: 'found it' },
          ],
        },
      }),
    ])
    expect(msgs[0].toolCalls?.[0].contentOffset).toBe('spawned'.length)
    const run = msgs[0].toolCalls?.[0].subAgent
    expect(run?.preview).toBe('found it')
    expect(run?.text).toBe('checking\nfound it')
    expect(run?.telemetry).toBe('')
    expect(run?.tools[0]).toMatchObject({
      id: 'inner-1',
      name: 'read_file',
      args: { path: 'a.txt' },
      result: { content: 'hello' },
    })
  })
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

describe('buildMessages ask_user contentOffsets (#63)', () => {
  const askCall = (callId: string, q: string) => ({
    id: callId,
    function: { name: 'ask_user', arguments: JSON.stringify({ question: q }) },
  })

  it('stamps each call with its position in the coalesced block text', () => {
    const msgs = buildMessages([
      row(1, 'user', 'go'),
      row(2, 'assistant', 'before one', { tool_calls: [askCall('q1', 'first?')] }),
      callRow(3, 'q1', 'ask_user', { answer: 'Option A' }),
      row(4, 'assistant', 'between'),
      row(5, 'assistant', 'before two', { tool_calls: [askCall('q2', 'second?')] }),
      callRow(6, 'q2', 'ask_user', { answer: 'Option X' }),
      row(7, 'assistant', 'after two'),
    ])
    const turn = msgs[1]
    // Both questions survive the coalesce; the block text is newline-joined.
    expect(turn.toolCalls?.map((t) => t.name)).toEqual(['ask_user', 'ask_user'])
    expect(turn.content).toBe('before one\nbetween\nbefore two\nafter two')
    // Offsets land where each call started: after its emission's text.
    const c1 = turn.toolCalls?.[0].contentOffset ?? -1
    const c2 = turn.toolCalls?.[1].contentOffset ?? -1
    expect(c1).toBe('before one'.length)
    expect(c2).toBe('before one\nbetween\nbefore two'.length)
    // And the offsets are strictly increasing slice points inside the text.
    expect(c1).toBeGreaterThan(0)
    expect(c2).toBeGreaterThan(c1)
    expect(c2).toBeLessThan(turn.content.length)
  })

  it('renders question cards chronologically, one per answered ask_user (regression: rc.5 rendered only the first, at the top of the turn)', () => {
    const msgs = buildMessages([
      row(1, 'user', 'go'),
      row(2, 'assistant', 'before one', { tool_calls: [askCall('q1', 'first?')] }),
      callRow(3, 'q1', 'ask_user', { answer: 'Option A' }),
      row(4, 'assistant', 'between'),
      row(5, 'assistant', 'before two', { tool_calls: [askCall('q2', 'second?')] }),
      callRow(6, 'q2', 'ask_user', { answer: 'Option X' }),
      row(7, 'assistant', 'after two'),
    ])
    const turn = msgs[1]
    const asks = (turn.toolCalls ?? []).filter(
      (t) => t.name === 'ask_user' && t.result !== undefined,
    )
    expect(asks).toHaveLength(2)
    // Chronology is derivable: both offsets exist and slice cleanly.
    const ordered = [...asks].sort((a, b) => (a.contentOffset ?? -1) - (b.contentOffset ?? -1))
    expect(ordered.every((t) => (t.contentOffset ?? -1) >= 0)).toBe(true)
    const before = turn.content.slice(0, ordered[0].contentOffset ?? 0).trim()
    expect(before).toBe('before one')
  })

  it('leaves offsets undefined for rows persisted before the field existed', () => {
    const msgs = buildMessages([
      row(1, 'user', 'go'),
      row(2, 'assistant', 'emission', {
        tool_calls: [askCall('q1', 'only?')],
      }),
      callRow(3, 'q1', 'ask_user', { answer: 'nope' }),
    ])
    expect(msgs[1].toolCalls?.[0].contentOffset).toBe((  'emission').length)
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

describe('sub-agent live state', () => {
  beforeEach(() => {
    useAgent.setState({ messagesByConv: {} })
  })

  const seed = () => {
    useAgent.setState({
      messagesByConv: {
        t: [{
          id: 'a',
          role: 'assistant',
          content: '',
          toolCalls: [{ id: 'spawn-1', name: 'spawn_agent', args: {} }],
        }],
      },
    })
    useAgent.getState().startSubAgent('t', 'a', 'spawn-1', 1, 'explore', 'inspect')
  }

  it('keeps a bounded per-spawn tape and routes nested chunks/results by child tool id', () => {
    seed()
    const store = useAgent.getState()
    store.subAgentTextDelta('t', 'a', 'spawn-1', 'first emission')
    store.subAgentToolStart('t', 'a', 'spawn-1', 'inner-1', 'bash', { command: 'build' })
    store.subAgentTextDelta('t', 'a', 'spawn-1', 'second emission')
    store.subAgentToolProgress('t', 'a', 'spawn-1', 'inner-1', 'output chunk')
    store.subAgentToolResult('t', 'a', 'spawn-1', 'inner-1', { output: 'done' })
    store.appendSubAgentTelemetry('t', 'a', 'spawn-1', 'thinking trace')
    store.appendSubAgentTelemetry('t', 'a', 'other-spawn', 'must not route')
    const run = useAgent.getState().messagesByConv.t[0].toolCalls?.[0].subAgent
    expect(run?.text).toBe('first emission\nsecond emission')
    expect(run?.preview).toBe('second emission')
    expect(run?.tools).toHaveLength(1)
    expect(run?.telemetry).toContain('spawned explore')
    expect(run?.telemetry).toContain('thinking trace')
    expect(run?.telemetry).not.toContain('must not route')
    expect(run?.tools).toHaveLength(1)
    expect(run?.tools[0].id).toBe('inner-1')
    expect(run?.tools[0].args).toEqual({ command: 'build' })
    expect(run?.tools[0].output).toBe('output chunk')
    expect(run?.tools[0].result).toEqual({ output: 'done' })
    expect(run?.tools[0].finishedAt).toBeGreaterThanOrEqual(run?.tools[0].startedAt ?? 0)

    store.appendSubAgentTelemetry('t', 'a', 'spawn-1', 'x'.repeat(20_000))
    expect(useAgent.getState().messagesByConv.t[0].toolCalls?.[0].subAgent?.telemetry).toHaveLength(16_000)
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

describe('draft destination (#32/#88/#94)', () => {
  it('newConversation pins the draft to the workspace active at creation; explicit pin overrides', () => {
    useAgent.setState({ draftDestination: null, conversationId: null, workspace: 'C:/repos/active' })
    // A draft chat is born pinned to its category (#88): the pin survives
    // active-workspace churn until the first send files it.
    useAgent.getState().newConversation()
    expect(useAgent.getState().draftDestination).toBe('C:/repos/active')
    // Flipping the active workspace must not move a pinned destination.
    useAgent.getState().setWorkspace('C:/repos/elsewhere')
    expect(useAgent.getState().draftDestination).toBe('C:/repos/active')
    // The card's Change… picker overrides the default pin (#32).
    useAgent.getState().pinDraftDestination('C:/repos/other')
    expect(useAgent.getState().draftDestination).toBe('C:/repos/other')
  })

  it('pin is cleared when the draft is filed (adopt consumes it)', () => {
    useAgent.setState({ draftDestination: null, conversationId: null, workspace: 'C:/repos/active' })
    useAgent.getState().newConversation()
    expect(useAgent.getState().draftDestination).toBe('C:/repos/active')
    // Filing the draft consumes the pin: the card's job is done.
    useAgent.getState().adoptDraft(42)
    expect(useAgent.getState().draftDestination).toBeNull()
  })

  it('clearOrphanedDraftPin drops an unfiled draft pin, never a filed chat pin', () => {
    useAgent.setState({ draftDestination: null, conversationId: null, workspace: 'C:/repos/active' })
    // Orphaned pin: a release committed on an unfiled draft, but the send
    // never happened (empty transcript, routing, transcription failure).
    useAgent.getState().newConversation()
    useAgent.getState().clearOrphanedDraftPin()
    expect(useAgent.getState().draftDestination).toBeNull()
    // A late cleanup arriving AFTER the draft was filed must not stomp a
    // pin that now belongs to the filed conversation's own state.
    useAgent.getState().pinDraftDestination('C:/repos/pinned')
    useAgent.setState({ conversationId: 7 })
    useAgent.getState().clearOrphanedDraftPin()
    expect(useAgent.getState().draftDestination).toBe('C:/repos/pinned')
  })
})

describe('scheduled-agent question cards (#93)', () => {
  beforeEach(() => {
    useAgent.setState({ pendingQuestions: {}, conversationId: null })
  })

  it('opens a card in the asking conversation when the tape sync runs', () => {
    useAgent.getState().syncTapeQuestion({
      op: 'set',
      callId: 'q1',
      question: 'Deploy now?',
      options: [{ label: 'Yes' }],
      convKey: '42',
    })
    const q = useAgent.getState().pendingQuestions['42']
    expect(q).toMatchObject({ callId: 'q1', question: 'Deploy now?', convKey: '42' })
  })

  it('clears only that conversation\u2019s card — other chats\u2019 questions stay', () => {
    useAgent.setState({
      pendingQuestions: {
        '42': { callId: 'q1', question: '?', options: [], convKey: '42' },
        '9': { callId: 'q0', question: '?', options: [], convKey: '9' },
      },
    })
    useAgent.getState().syncTapeQuestion({ op: 'clear', convKey: '42' })
    expect(useAgent.getState().pendingQuestions['42']).toBeUndefined()
    expect(useAgent.getState().pendingQuestions['9']).toBeDefined()
  })

  it('a null action is a no-op (no phantom store write)', () => {
    useAgent.setState({
      pendingQuestions: { '9': { callId: 'q0', question: '?', options: [], convKey: '9' } },
    })
    useAgent.getState().syncTapeQuestion(null)
    expect(Object.keys(useAgent.getState().pendingQuestions)).toEqual(['9'])
  })

  it('re-asking in the same conversation replaces the card (callId swap)', () => {
    useAgent.setState({
      pendingQuestions: { '42': { callId: 'q1', question: 'one?', options: [], convKey: '42' } },
    })
    useAgent.getState().syncTapeQuestion({
      op: 'set',
      callId: 'q2',
      question: 'two?',
      options: [],
      convKey: '42',
    })
    expect(useAgent.getState().pendingQuestions['42']).toMatchObject({ callId: 'q2' })
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


describe('steering transcript order', () => {
  beforeEach(() => {
    useAgent.setState({ messagesByConv: {} })
  })

  it('starts a new assistant emission after injected users with its first text delta', () => {
    const store = useAgent.getState()
    store.appendAssistantPlaceholder('42')
    store.appendTextDelta('42', useAgent.getState().messagesByConv['42'][0].id, 'before steering')
    store.appendUserMessage('42', 'first steering message', [], [], true)
    store.appendUserMessage('42', 'second steering message', [], [], true)

    const emissionId = useAgent.getState().startAssistantEmissionAfterUser('42', 'after steering')
    expect(emissionId).toBeTruthy()
    expect(useAgent.getState().startAssistantEmissionAfterUser('42')).toBeNull()

    const messages = useAgent.getState().messagesByConv['42']
    expect(messages.map((message) => message.role)).toEqual(['assistant', 'user', 'user', 'assistant'])
    expect(messages[0].content).toBe('before steering')
    expect(messages[3].id).toBe(emissionId)
    expect(messages[3].content).toBe('after steering')
  })
})

describe('agentBranch persistence (branch-first chip)', () => {
  it('mirrors bound branches to localStorage and drops them on release; merged flag survives', () => {
    useAgent.getState().setAgentBranch('c1', { branch: 'agent/1/fix-x-runabc123', boundAt: 1 })
    useAgent.getState().setAgentBranch('c2', { branch: 'agent/2/fix-y-rundef456', boundAt: 2 })
    let stored = JSON.parse(localStorage.getItem('yaah-agent-branch-by-conv') || '{}')
    expect(Object.keys(stored).sort()).toEqual(['c1', 'c2'])

    // an explicit merge keeps the binding but flips the chip to merged
    const cur = useAgent.getState().agentBranchByConv['c1']
    if (!cur) throw new Error('c1 binding missing')
    useAgent.getState().setAgentBranch('c1', { ...cur, merged: true })
    expect(useAgent.getState().agentBranchByConv['c1']?.merged).toBe(true)

    // release removes the entry from the mirror too — no stale chips
    useAgent.getState().setAgentBranch('c2', null)
    stored = JSON.parse(localStorage.getItem('yaah-agent-branch-by-conv') || '{}')
    expect(Object.keys(stored).sort()).toEqual(['c1'])
    expect(stored.c1.merged).toBe(true)
    useAgent.getState().setAgentBranch('c1', null)
  })
})
