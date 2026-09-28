/**
 * MOCK SESSION — reproduction transcript for "agent chat emissions disappear
 * mid run". NOT a real session: drives the store directly with a scripted
 * emission pattern (chat -> tools -> chat -> tools), interleaving a
 * `loadHistory` poll at the moment the running-guard misses, and dumps the
 * rendered transcript after every phase.
 *
 * Run: npx vitest run src/mockSession.repro.test.ts --reporter=verbose
 * (the transcript goes to stdout — CI's typecheck has no node types here).
 */
import { describe, it, beforeEach, expect } from 'vitest'
import { useAgent } from './store'

const lines: string[] = []
let liveKey = 'draft'
const snap = (label: string) => {
  const msgs = useAgent.getState().messagesByConv[liveKey] ?? []
  lines.push(`\n### ${label}\n`)
  if (msgs.length === 0) lines.push('_(buffer empty)_')
  for (const m of msgs) {
    const body = (m.content ?? '').replace(/\n/g, ' ⏎ ').slice(0, 120)
    let extra = ''
    if (m.toolCalls?.length) {
      extra = ' | tools: ' + m.toolCalls
        .map((tc) => `${tc.name}(${JSON.stringify(tc.args)}) -> ${tc.result ? JSON.stringify(tc.result).slice(0, 60) : 'RUNNING'}`)
        .join(' ; ')
    }
    lines.push(`- **[${m.role}]** id=${m.id} "${body}"${extra}`)
  }
}

describe('mock session: emissions disappear mid run', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: 42,
      statusByConv: {},
      messagesByConv: {},
      finishedByConv: {},
    })
    lines.push('# Mock session transcript — conv 42\n')
  })

  it('chat → tools → chat → tools; draft adopted mid-run, then history poll', () => {
    const s = () => useAgent.getState()
    // A fresh chat starts as a DRAFT: the run streams under key 'draft'
    // until the first response files the conversation and adoptDraft
    // re-keys the buffer to the real conversation id (42).
    let key = 'draft'
    liveKey = key

    // --- turn starts (as `send()` does) ---
    s().setStatus(key, 'thinking')
    s().appendUserMessage(key, 'Run the mock task, please.')
    const asst = s().appendAssistantPlaceholder(key)
    snap('Phase 0 — turn started, placeholder created')

    // --- emission block 1: chat ---
    s().appendTextDelta(key, asst, 'Hello! Emitting the FIRST chat block before any tools. ')
    s().appendTextDelta(key, asst, 'Still streaming text for block one.')
    snap('Phase 1 — first chat emission streamed')

    // --- tool calls, round 1 ---
    s().setStatus(key, 'running-tool')
    s().startToolCall(key, asst, 'call_1', 'bash', { command: 'echo round-one' })
    s().finishToolCall(key, asst, 'call_1', { stdout: 'round-one', exit: 0 })
    snap('Phase 2 — tool round 1 done')

    // --- emission block 2: chat ---
    s().setStatus(key, 'thinking')
    s().appendTextDelta(key, asst, 'Tools finished. Emitting the SECOND chat block now — this one must survive.')
    snap('Phase 3 — second chat emission streamed')

    // ===== THE INCIDENT: draft adopted + a history poll lands mid-run =====
    // The first response filed the conversation (id 42). adoptDraft re-keys
    // the MESSAGE buffer draft→42 — but statusByConv.draft is left behind,
    // so statusByConv['42'] stays unset ('idle') while the run streams on.
    s().adoptDraft(42)
    key = '42'
    liveKey = key
    lines.push(`\n> after adoptDraft: statusByConv['42'] = ${s().statusByConv['42'] ?? 'idle (unset!)'}, statusByConv['draft'] = ${s().statusByConv['draft'] ?? '(gone)'}\n`)
    // This is what AgentChatLiveFollow / conversation-open do every 2.5s.
    // The rows here are the *persisted* rows — the last chat block and the
    // finished tool call are NOT yet persisted, exactly like a real run.
    const persistedRows = [
      { id: 1, role: 'user', content: 'Run the mock task, please.', images: null, sub_agent_transcript: null, tool_call_id: null, tool_calls: null },
      { id: 2, role: 'assistant', content: 'Hello! Emitting the FIRST chat block before any tools. Still streaming text for block one.', images: null, sub_agent_transcript: null, tool_call_id: null, tool_calls: null },
      // NOTE: no rows yet for the tool call, nor the second chat block —
      // they exist only in the live buffer.
    ]
    s().loadHistory(42, persistedRows as any)
    snap('Phase 4 — ⚠️ loadHistory landed MID-RUN (rows lag the live buffer)')
    // REGRESSION (adoptDraft must migrate statusByConv.draft → the new key):
    // the running-guard must fire and the live buffer must survive intact.
    expect(s().statusByConv['42']).toBe('thinking')
    const after = s().messagesByConv['42'] ?? []
    expect(after.map((m) => m.id)).toEqual(['m1', 'm2'])
    expect(after[1].content).toContain('SECOND chat block')
    expect(after[1].toolCalls?.some((tc) => tc.id === 'call_1')).toBe(true)

    // --- tool calls, round 2: deltas now target the (possibly wiped) id ---
    s().setStatus(key, 'running-tool')
    s().startToolCall(key, asst, 'call_2', 'bash', { command: 'echo round-two' })
    s().finishToolCall(key, asst, 'call_2', { stdout: 'round-two', exit: 0 })
    s().appendTextDelta(key, asst, 'Post-tool-2 tail text.')
    snap('Phase 5 — post-incident emissions (tool round 2 + tail text)')

    console.log(lines.join('\n'))
  })

  it('CONTROL: same session without the mid-run poll (baseline)', () => {
    const s = () => useAgent.getState()
    const key = '42'
    s().setStatus(key, 'thinking')
    s().appendUserMessage(key, 'Run the mock task, please.')
    const asst = s().appendAssistantPlaceholder(key)
    snap('Phase 0 — turn started, placeholder created')

    s().appendTextDelta(key, asst, 'Hello! Emitting the FIRST chat block before any tools. Still streaming text for block one.')
    snap('Phase 1 — first chat emission streamed')

    s().setStatus(key, 'running-tool')
    s().startToolCall(key, asst, 'call_1', 'bash', { command: 'echo round-one' })
    s().finishToolCall(key, asst, 'call_1', { stdout: 'round-one', exit: 0 })
    snap('Phase 2 — tool round 1 done')

    s().setStatus(key, 'thinking')
    s().appendTextDelta(key, asst, 'Tools finished. Emitting the SECOND chat block now — this one must survive.')
    snap('Phase 3 — second chat emission streamed')

    s().setStatus(key, 'running-tool')
    s().startToolCall(key, asst, 'call_2', 'bash', { command: 'echo round-two' })
    s().finishToolCall(key, asst, 'call_2', { stdout: 'round-two', exit: 0 })
    s().appendTextDelta(key, asst, 'Post-tool-2 tail text.')
    snap('Phase 5 — post-incident emissions (tool round 2 + tail text)')

    console.log(lines.join('\n'))
  })
})
