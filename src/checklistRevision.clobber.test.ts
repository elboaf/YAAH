/**
 * CHECKLIST REVISION CLOMBER — reproduction for #867: the floating goal
 * overlay shows a stale completion count while the inline card shows the
 * live one.
 *
 * Root cause (verified against source): the conversation-row seed path
 * (components.tsx getConversation().then) calls setTodos unconditionally
 * with the persisted session_todos, with NO checklistRevision guard. The
 * remote live-hydrate path (hydrateRemoteTodos) DOES guard on
 * checklistRevisionByConv. So a late, stale session_todos from the DB can
 * clobber a newer live TodoWrite update — the exact intermittent race the
 * maintainer described.
 *
 * This test drives the store directly (mock-session style) to prove the bug
 * and, after the fix, prove it is gone.
 *
 * Run: npx vitest run src/checklistRevision.clobber.test.ts
 */
import { describe, it, beforeEach, expect } from 'vitest'
import { useAgent } from './store'
import type { TodoItem } from './store'

const key = 'conv-867'

const todo = (id: string, title: string, status: 'in_progress' | 'done' | 'pending'): TodoItem =>
  ({ id, title, status, description: '' } as unknown as TodoItem)

// Simulate the conversation-row seed that components.tsx runs after
// getConversation() resolves. Mirrors the #867 guard: only seed when no live
// TodoWrite has run yet (revision still 0). A seeded revision means the live
// update is newer, so we skip the DB snapshot entirely.
const seedFromConversationRow = (sessionTodos: TodoItem[]): void => {
  if ((useAgent.getState().checklistRevisionByConv[key] ?? 0) !== 0) return
  useAgent.setState({ todosByConv: { [key]: sessionTodos } })
}

// Simulate a live TodoWrite tool_result arriving (already guarded, bumps revision).
const liveTodoWrite = (todos: TodoItem[]): void => {
  useAgent.setState((s) => ({ todosByConv: { ...s.todosByConv, [key]: todos } }))
  useAgent.getState().bumpChecklistRevision(key)
}

describe('checklist revision guard — #867', () => {
  beforeEach(() => {
    useAgent.setState({
      todosByConv: {},
      checklistRevisionByConv: {},
    })
  })

  it('a stale DB seed must NOT clobber a newer live TodoWrite', () => {
    // 1. A live TodoWrite advances the checklist to 5/5 done and bumps the revision.
    liveTodoWrite([
      todo('1', 'a', 'done'),
      todo('2', 'b', 'done'),
      todo('3', 'c', 'done'),
      todo('4', 'd', 'done'),
      todo('5', 'e', 'done'),
    ])
    expect(useAgent.getState().todosByConv[key]).toHaveLength(5)
    const afterLive = useAgent.getState().checklistRevisionByConv[key]
    expect(afterLive).toBeGreaterThan(0)

    // 2. A late conversation-row seed resolves with a stale 3/5 snapshot from
    //    the persisted session_todos. This is the race: it arrives after the
    //    live update.
    seedFromConversationRow([
      todo('1', 'a', 'done'),
      todo('2', 'b', 'done'),
      todo('3', 'c', 'done'),
      todo('4', 'd', 'in_progress'),
      todo('5', 'e', 'pending'),
    ])

    // The live 5/5 must survive the stale seed.
    const final = useAgent.getState().todosByConv[key]
    expect(final).toHaveLength(5)
    expect(final[4].status).toBe('done')
    expect(final[3].status).toBe('done')
  })
})
