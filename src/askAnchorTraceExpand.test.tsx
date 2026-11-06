// Regression (ask-anchor "did next" swallow): the answered ask_user card's
// summary of the calls after the question was a CallNameCounts <p> that
// mimicked the turn's collapsed-trace look (same glyphs, same ×N counts) but
// had no click target - the user read it as the chip row and could not
// expand anything ("the ask user card swallows tool call chips"). The fix
// renders the after-calls as a real TraceLine. These tests pin the
// interaction, not just the look: the card's trace button must exist and
// must expand to the calls' rows and args detail.
//
// Drives the real store mutation path (handleStreamEvent helpers, in stream
// order) for the incident's turn shape:
//   text -> tool -> ask_user -> [user answers] -> text -> tool -> done
// then renders the buffer through the real MessageView and asserts the
// anchor card's own trace expands.
//
// Run: npx vitest run src/askAnchorTraceExpand.test.tsx
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { useAgent } from './store'
import { MessageView } from './components'

const KEY = '91'

// Longer than toolTarget's 48-char chip truncate, so the tail of the
// command only appears once the row's detail is expanded. No quote
// characters: JSON-escaped quotes would not survive as plain textContent.
const POST_ANSWER_CMD =
  'git commit -m fix answered ask card swallowed the tool call chips after reply'

function buffer() {
  return useAgent.getState().messagesByConv[KEY] ?? []
}

function renderAll() {
  cleanup()
  render(
    <div>
      {buffer().map((m) => (
        <MessageView key={m.id} msg={m} />
      ))}
    </div>,
  )
}

// The same mutation calls handleStreamEvent makes, in stream order.
function streamEvents() {
  const s = () => useAgent.getState()
  s().setStatus(KEY, 'thinking')
  s().appendUserMessage(KEY, 'update the bundled skill copy')
  const asst = s().appendAssistantPlaceholder(KEY)

  // emission + a tool, then the ask_user (answered)
  s().appendTextDelta(KEY, asst, 'Recon done. One decision before I write anything.')
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'c1', 'bash', { command: 'git log --oneline -3' })
  s().finishToolCall(KEY, asst, 'c1', { exit_code: 0 })
  s().startToolCall(KEY, asst, 'q1', 'ask_user', {
    question: 'Update everything?',
    options: [{ label: 'Update everything' }],
  })
  s().finishToolCall(KEY, asst, 'q1', { answer: 'Update everything' })

  // Post-answer emission + the tool the card's trace must be able to show
  s().setStatus(KEY, 'thinking')
  s().appendTextDelta(KEY, asst, 'Updating now.')
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'c3', 'bash', { command: POST_ANSWER_CMD })
  s().finishToolCall(KEY, asst, 'c3', { exit_code: 0 })
  s().setStatus(KEY, 'idle')
}

// Same shape, but every call AFTER the answer is a spawn_agent: the card's
// filtered trace list is empty, so the card must render no trace toggle at
// all (a "0 calls" button that expands to nothing is the CodeRabbit #692
// regression).
function streamEventsSpawnOnly() {
  const s = () => useAgent.getState()
  s().setStatus(KEY, 'thinking')
  s().appendUserMessage(KEY, 'delegate the copy update')
  const asst = s().appendAssistantPlaceholder(KEY)
  s().appendTextDelta(KEY, asst, 'One decision first.')
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'q1', 'ask_user', {
    question: 'Delegate the whole update?',
    options: [{ label: 'Delegate it' }],
  })
  s().finishToolCall(KEY, asst, 'q1', { answer: 'Delegate it' })
  s().setStatus(KEY, 'running-tool')
  s().startToolCall(KEY, asst, 'd1', 'spawn_agent', { prompt: 'update the bundled skill copy' })
  s().finishToolCall(KEY, asst, 'd1', { summary: 'delegated run finished' })
  s().setStatus(KEY, 'idle')
}

describe('answered ask card carries a working expandable trace (#anchor-swallow)', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: 91,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
    })
  })

  it('the card renders its own collapsed trace line', () => {
    streamEvents()
    renderAll()
    // The anchor's after-calls trace: exactly one post-answer call.
    expect(screen.getByText('1 call')).toBeTruthy()
  })

  it('expanding the card trace reveals the post-answer call row and detail', () => {
    streamEvents()
    renderAll()
    // Collapsed: only the chip's 48-char target preview is visible.
    expect(document.body.textContent).not.toContain('after reply')
    fireEvent.click(screen.getByText('1 call').closest('button')!)
    // The expanded trace carries the call's row (chip target, truncated).
    expect(document.body.textContent).toContain('swallowed')
    // Expanding the row itself reveals the full args JSON.
    fireEvent.click(screen.getByText('[+]').closest('button')!)
    expect(document.body.textContent).toContain('after reply')
  })

  it('the turn-level trace still expands with every call', () => {
    streamEvents()
    renderAll()
    fireEvent.click(screen.getByText('3 calls').closest('button')!)
    // All three calls as rows: the pre-answer bash, the ask, the post-answer bash.
    expect(document.body.textContent).toContain('git log --oneline -3')
    expect(document.body.textContent).toContain('swallowed')
    // The post-answer row still expands to its full args.
    const expandButtons = screen.getAllByText('[+]')
    const c3Row = expandButtons
      .map((b) => b.closest('button')!)
      .find((b) => b.textContent?.includes('swallowed'))!
    fireEvent.click(c3Row)
    expect(document.body.textContent).toContain('after reply')
  })

  it('an ask followed only by spawn_agent renders no card trace toggle (no "0 calls")', () => {
    streamEventsSpawnOnly()
    renderAll()
    // The spawn still exists as its own block in the flow — it is filtered
    // from the card's trace, not hidden from the transcript.
    expect(document.body.textContent).toContain('spawn_agent')
    // The card must not render an empty trace control.
    expect(screen.queryByText('0 calls')).toBeNull()
  })
})
