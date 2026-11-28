// Regression (#895): a NEW run in an existing chat must refresh the goal
// overlay's SUMMARY (the objective) to the new prompt at run-start — the same
// trigger that refreshes the STEPS (every TodoWrite). Before the fix the
// summary was only (re)seeded from the conversation row's session_goal by
// ChatScopePickers — on chat switch AND on every streaming transition — so
// during a new run it kept showing the PREVIOUS run's goal, and only caught
// up when the run ended (the row finally holds the fresh session_goal).
//
// The fix has two halves, both pinned here:
//  1. send() writes the objective at run-start (mirroring the backend's
//     session_goal reset in loop.py), and flags the run live (runLiveByConv).
//  2. ChatScopePickers' row-seed skips the objective while a run is live for
//     that conversation — without the guard, the run-start effect firing
//     (streaming true) re-reads the row, which STILL holds the previous
//     run's goal, and clobbers the fresh write mid-run.
//
// The fake getConversation deliberately returns the STALE session_goal for
// the WHOLE test: if either half of the fix is missing, the objective ends
// up stale while the run is in flight. The stream is deferred so the
// assertions run MID-RUN (the exact window the bug lived in), and the write
// is captured via a spy so the assertions are content-based.
//
// Run: npx vitest run src/goalObjectiveRunStart.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const streamCalls: Array<{ cid: number; message: string }> = []
const objectiveWrites: Array<{ key: string; text: string }> = []
let resolveStream: (() => void) | null = null
let setObjectiveSpy: ReturnType<typeof vi.spyOn>

const { listAvailableModels } = vi.hoisted(() => ({ listAvailableModels: vi.fn() }))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    // ChatScopePickers reads byProvider straight off this (no catch on the
    // render path), so a missing/undefined `providers` crashes the tree
    // before send ever runs. Same shape as the real /api/models/available.
    listAvailableModels,
    getConfig: vi.fn(async () => ({})),
    getContext: vi.fn(async () => ({ context_tokens: null, context_window: null, context_model: null })),
    getGitInfo: vi.fn(async () => ({ info: {} })),
    getConversation: vi.fn(async (id: number) => ({
      id,
      title: 'chat 9',
      workspace: 'C:/w/a',
      created_at: '',
      updated_at: '',
      // #895: the row carries the PREVIOUS run's goal for the entire test.
      // In production the backend refreshes it when the turn starts; the
      // stale read at run-start is exactly what used to clobber the overlay.
      session_goal: 'build the login flow',
    })),
    listConversations: vi.fn(async () => []),
    updateAgentModelEffort: vi.fn(async () => ({})),
    updateConversation: vi.fn(async () => ({})),
    createConversation: vi.fn(async (title: string) => ({ id: 7, title })),
    streamAgentTurn: vi.fn(
      (cid: number, message: string, _workspace: string, _onEvent: (ev: { type: string }) => void) => {
        streamCalls.push({ cid, message, workspace: _workspace } as never)
        // Deferred: the run stays IN FLIGHT until the test resolves it, so
        // assertions observe the mid-run state the bug lived in — and the
        // send finally (which clears runLive) cannot run early and mask the
        // row-seed guard.
        return new Promise<void>((resolve) => {
          resolveStream = resolve
        })
      },
    ),
  }
})

import { ChatPanel } from './components'
import { useAgent } from './store'
import { typeIntoComposer } from './composer/testUtils'

beforeEach(async () => {
  streamCalls.length = 0
  objectiveWrites.length = 0
  // Backstop: a test that failed BEFORE settling its deferred run leaves it
  // hanging. Resolve it NOW — before this test mounts and starts its own
  // run — so a stale resolver cannot un-stick THIS test's stream from this
  // test's own settle/afterEach. (Tests that end cleanly settle inside
  // their body via settleRun, so this only fires on failure paths.)
  resolveStream?.()
  resolveStream = null
  // The model probe: a missing `providers` leaves byProvider undefined and
  // the picker crashes on Object.entries before send ever runs.
  listAvailableModels.mockResolvedValue({ providers: {}, active_provider: '', model: '' })
  // Catch-all for any API call the vi.mock above did not name. Shape matters:
  // listSkills returns { skills: [...] } and the Composer does
  // setSkills(r.skills) — an empty-array body makes r.skills undefined, the
  // next render crashes on skills.map, and the remount loop clears the
  // composer input. /api/processes carries { processes: [...] } —
  // DetachedProcessesChip does setRows(r.processes) with no catch.
  vi.spyOn(window, 'fetch').mockImplementation(async (input) => {
    const u = String(input)
    if (u.includes('skills')) return new Response(JSON.stringify({ skills: [] }), { status: 200, headers: { 'content-type': 'application/json' } })
    if (u.includes('processes')) return new Response(JSON.stringify({ processes: [], known: 0 }), { status: 200, headers: { 'content-type': 'application/json' } })
    return new Response(JSON.stringify({}), { status: 200, headers: { 'content-type': 'application/json' } })
  })
  useAgent.setState({
    conversationId: 9,
    messagesByConv: { 9: [] },
    objectiveByConv: { 9: 'build the login flow' }, // stale, from the prior run
    runLiveByConv: {},
    todosByConv: { 9: [{ content: 'old step', status: 'completed' }] },
    statusByConv: {},
    queueEchoByConv: {},
    pendingQuestions: {},
    pendingApprovals: {},
    pendingPlanApprovals: {},
    errorByConv: {},
    draftDestination: null,
    draftScope: null,
  })
  setObjectiveSpy = vi
    .spyOn(useAgent.getState(), 'setObjective')
    .mockImplementation((key, text) => {
      objectiveWrites.push({ key, text })
      useAgent.setState((s) => ({ objectiveByConv: { ...s.objectiveByConv, [key]: text } }))
    })
})

afterEach(() => {
  // Unstick this test's deferred stream so the (aborted) send finally can
  // run. A test that asserted mid-run leaves it hanging on purpose — the
  // NEXT beforeEach resolves it, before that test's run can start.
  resolveStream?.()
  resolveStream = null
  cleanup()
  setObjectiveSpy.mockRestore()
  vi.restoreAllMocks()
})

/** Read the editor's live plain text straight off its node map (the same
 *  coordinates serializeEditorState uses), so we can confirm a type landed. */
function editorTextOf(box: HTMLElement): string {
  const ed = (box as unknown as { __editor?: { getEditorState: () => { _nodeMap: Map<string, { __type: string; __text?: string }> } } }).__editor
  if (!ed) return ''
  let out = ''
  ed.getEditorState()._nodeMap.forEach((n) => {
    if (n.__type === 'text') out += n.__text ?? ''
  })
  return out
}

async function sendNewRun(text: string): Promise<void> {
  const box = await screen.findByLabelText('Message the agent')
  // The freshly-mounted Lexical editor's value-sync effect can rebuild to the
  // parent's (empty) value in the same tick as the first editor.update,
  // silently dropping the typed text. A dropped type makes send() see empty
  // input and start NO run — which would fail the wrong assertion. Verify the
  // text actually landed (via the editor's own state) and retype until it
  // does, so this helper always produces a real run to assert against.
  for (let attempt = 0; attempt < 5; attempt++) {
    await typeIntoComposer(box, text)
    // Poll for the text to appear in the editor's own state (500 ms).
    const deadline = Date.now() + 500
    let landed = false
    while (Date.now() < deadline) {
      if (editorTextOf(box).includes(text)) {
        landed = true
        break
      }
      await new Promise((r) => setTimeout(r, 25))
    }
    if (landed) break
  }
  if (!editorTextOf(box).includes(text)) {
    throw new Error(`composer never received the typed text (5 attempts): ${JSON.stringify(text)}`)
  }
  fireEvent.keyDown(box, { key: 'Enter' })
  await waitFor(() => expect(streamCalls.length).toBe(1))
}

/** End a deferred run and wait for send()'s `finally` to fully settle
 *  (runLive cleared + the resulting React re-render flushed). A test that
 *  asserts MID-RUN must settle in its OWN body before it ends — leaving the
 *  stream hanging means the leaked `send()` continuation fires during the
 *  NEXT test's mount/type step and corrupts its freshly-mounted editor. */
async function settleRun(): Promise<void> {
  resolveStream?.()
  resolveStream = null
  await waitFor(() => expect(useAgent.getState().runLiveByConv['9']).toBe(false), { timeout: 2000 })
}

describe('goal summary refreshes at run-start (#895)', () => {
  it('a new run in an existing chat shows the NEW goal mid-run, even though the row still carries the old one', async () => {
    render(<ChatPanel />)
    await sendNewRun('now fix the logout bug')
    expect(streamCalls[0].cid).toBe(9)
    expect(streamCalls[0].message).toBe('now fix the logout bug')

    // send() wrote the new goal at run-start...
    expect(objectiveWrites.some((w) => w.key === '9' && w.text === 'now fix the logout bug')).toBe(true)
    // ...and while the run is in flight the summary is the NEW one. The
    // row-seed legitimately applied the stale goal once at MOUNT (no run
    // live yet — that seeding path is unchanged), but it must not have
    // re-applied it AFTER send()'s write: that mid-run re-seed is the bug.
    const staleWrites = objectiveWrites.filter((w) => w.key === '9' && w.text === 'build the login flow')
    const newWrite = objectiveWrites.find((w) => w.key === '9' && w.text === 'now fix the logout bug')
    expect(newWrite).toBeDefined()
    for (const stale of staleWrites) {
      expect(objectiveWrites.indexOf(stale)).toBeLessThan(objectiveWrites.indexOf(newWrite!))
    }
    expect(useAgent.getState().objectiveByConv['9']).toBe('now fix the logout bug')
    // Settle the run before this test ends: the deferred stream's `send()`
    // continuation (finally → setRunLive(false) + idle re-render) must land
    // while THIS tree is mounted, never during the next test's mount/type
    // step, where it would clear the freshly-mounted editor's input.
    await settleRun()
  })

  it('runLive is set for the run and cleared when it ends (so the next switch re-seeds from the row)', async () => {
    render(<ChatPanel />)
    await sendNewRun('now fix the logout bug')
    // send() flagged the buffer live while the stream is in flight...
    expect(useAgent.getState().runLiveByConv['9']).toBe(true)
    // ...and the run-end path (finally) clears it, so a later chat switch
    // re-seeds the objective from the row as before. settleRun also flushes
    // the idle re-render before the test ends (see settleRun's contract).
    await settleRun()
  })

  it('an image/attachment-only send (empty text) does NOT blank the objective', async () => {
    render(<ChatPanel />)
    const box = await screen.findByLabelText('Message the agent')
    // Send with NO text, no attachments, no images: the send guard aborts
    // before any run starts — objective and run flag untouched.
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(streamCalls.length).toBe(0), { timeout: 500 })
    expect(useAgent.getState().objectiveByConv['9']).toBe('build the login flow')
    // No run ever started, so no live flag was ever set for this buffer.
    expect(useAgent.getState().runLiveByConv['9']).toBeFalsy()
    expect(objectiveWrites.filter((w) => w.text !== 'build the login flow').length).toBe(0)
  })
})

describe('adoptDraft re-homes the objective and run flag to the new cid (#895)', () => {
  it('a draft-born run keeps its objective and run flag under the real conversation id', () => {
    useAgent.setState({
      conversationId: null,
      messagesByConv: { draft: [{ id: 'u1', role: 'user', content: 'build the login flow' }] },
      objectiveByConv: { draft: 'build the login flow' },
      runLiveByConv: { draft: true },
      statusByConv: { draft: 'thinking' },
    })
    useAgent.getState().adoptDraft(7)
    const s = useAgent.getState()
    // The on-screen objective is keyed on the real cid — it must follow the
    // draft, or the card is empty for the whole draft-born run.
    expect(s.objectiveByConv['7']).toBe('build the login flow')
    expect(s.objectiveByConv['draft']).toBe('')
    // And the row-seed guard must see the live run under the real cid, or
    // the stale row clobbers the summary on the next effect firing.
    expect(s.runLiveByConv['7']).toBe(true)
    expect(s.runLiveByConv['draft']).toBeUndefined()
    // Unrelated state still re-homes as before.
    expect(s.statusByConv['7']).toBe('thinking')
    expect(s.statusByConv.draft).toBeUndefined()
  })
})
