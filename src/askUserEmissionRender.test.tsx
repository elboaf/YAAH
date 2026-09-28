// Regression (#63 render path): "after ask_user was answered, the run's chat
// vanished; only tool calls were visible" (2026-09-30). The #63 anchor render
// had TWO guards for the interleaved emission view — one population guard
// (built the segments) and one branch guard (rendered them) — and they
// disagreed for the ask_user-with-offset shape, so the branch rendered an
// empty array: intact buffer, invisible text. These tests pin the fixed
// behavior: every emission of the incident's turn shape stays visible.
//
// Drives the REAL store mutation path (the same helpers handleStreamEvent
// calls, in the same order) for the incident's turn shape:
//   text -> tools -> text -> ask_user -> [user answers] -> text -> tools -> done
// then renders the buffer through the real ChatPanel message list and asserts
// every emission is visible. A second phase fires a loadHistory poll with
// realistic lagging rows (the AgentChatLiveFollow / re-open shape) to see
// whether the live text survives.
//
// Run: npx vitest run src/askUserEmissionRender.test.tsx
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { useAgent } from './store'
import { MessageView } from './components'
import type { StoredMessage } from './api'

const KEY = '77'

function buffer() {
  return useAgent.getState().messagesByConv[KEY] ?? []
}

function renderAll() {
  cleanup()
  return render(
    <div>
      {buffer().map((m) => (
        <MessageView key={m.id} msg={m} />
      ))}
    </div>,
  )
}

// The markdown body renderer splits sentences across inline elements, so
// substring probes go through the flattened text content of the transcript.
function flat() {
  return document.body.textContent ?? ''
}

// The same mutation calls handleStreamEvent makes, in stream order.
function streamEvents() {
  const s = () => useAgent.getState()
  s().setStatus(KEY, 'thinking')
  s().appendUserMessage(KEY, '$ask-matt what’s next? to spec?')
  const asst = s().appendAssistantPlaceholder(KEY)

  // emission 1 (text), then two tools
  s().appendTextDelta(KEY, asst, 'The report is in the conversation above. Quick recap of where we stand.')
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'c1', 'bash', { command: 'git log --oneline -8' })
  s().finishToolCall(KEY, asst, 'c1', { exit_code: 0 })
  s().startToolCall(KEY, asst, 'c2', 'edit_file', { path: 'a.py' })
  s().finishToolCall(KEY, asst, 'c2', { replaced: 1 })

  // emission 2 (text) then the ask_user
  s().setStatus(KEY, 'thinking')
  s().appendTextDelta(KEY, asst, 'On the ask-matt map, the next station is /implement. Want me to proceed?')
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'q1', 'ask_user', { question: 'Proceed?', options: [{ label: 'Go' }] })
  s().finishToolCall(KEY, asst, 'q1', { answer: 'Go' })

  // post-answer emission + tools + final text (this is what vanished)
  s().setStatus(KEY, 'thinking')
  s().appendTextDelta(KEY, asst, 'Committed as 761017e. The implementation is done and unit-tested.')
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'c3', 'bash', { command: 'git commit -m fix' })
  s().finishToolCall(KEY, asst, 'c3', { exit_code: 0 })
  s().setStatus(KEY, 'thinking')
  s().appendTextDelta(KEY, asst, 'Sounds good — it’s all yours. Everything is committed on master.')
  s().setStatus(KEY, 'idle')
}

describe('ask_user answer keeps emissions visible (render regression)', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: 77,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
    })
  })

  it('live buffer keeps every emission through the ask roundtrip', () => {
    streamEvents()
    renderAll()
    expect(flat()).toContain('Quick recap of where we stand')
    expect(flat()).toContain('Want me to proceed')
    expect(flat()).toContain('Committed as 761017e')
    expect(flat()).toContain('Sounds good — it’s all yours')
  })

  it('survives a history reload with lagging rows mid-run', () => {
    streamEvents()
    // A poll lands mid-run with only the persisted prefix (last emission +
    // final tool still unpersisted). The running-guard must skip this.
    useAgent.setState({ statusByConv: { [KEY]: 'thinking' } })
    const lagging: StoredMessage[] = [
      { id: 1, role: 'user', content: '$ask-matt what’s next? to spec?', tool_calls: null },
      { id: 2, role: 'assistant', content: 'The report is in the conversation above. Quick recap of where we stand.', tool_calls: null },
      { id: 3, role: 'tool', tool_call_id: 'c1', content: '{"exit_code":0}', tool_calls: [{ id: 'c1', function: { name: 'bash', arguments: '{}' } }] },
      { id: 4, role: 'assistant', tool_calls: [{ id: 'c2', type: 'function', function: { name: 'edit_file', arguments: '{"path":"a.py"}' } }], content: '' },
      { id: 5, role: 'tool', tool_call_id: 'c2', content: '{"replaced":1}', tool_calls: null },
      { id: 6, role: 'assistant', content: 'On the ask-matt map, the next station is /implement. Want me to proceed?', tool_calls: null },
      { id: 7, role: 'assistant', tool_calls: [{ id: 'q1', type: 'function', function: { name: 'ask_user', arguments: '{"question":"Proceed?"}' } }], content: '' },
      { id: 8, role: 'tool', tool_call_id: 'q1', content: '{"answer":"Go"}', tool_calls: null },
    ] as unknown as StoredMessage[]
    useAgent.getState().loadHistory(77, lagging)
    renderAll()
    expect(flat()).toContain('Quick recap of where we stand')
    expect(flat()).toContain('Want me to proceed')
    expect(flat()).toContain('Committed as 761017e')
    expect(flat()).toContain('Sounds good — it’s all yours')
  })
})
