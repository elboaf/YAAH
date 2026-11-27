// #860 CodeRabbit Minor finding: preserve dismissal during checklist updates.
//
// The overlay's effect opened it on EVERY checklist replacement
// (`useEffect(() => { if (todos?.length) setShowGoalOverlay(true) }, [todos])`).
// After the user dismissed, the next TodoWrite replaced `todos`, which reopened
// the overlay — each progress update undid dismissal. The fix opens the overlay
// only when a checklist first appears or the conversation changes, tracking the
// conversation in a ref, so later TodoWrite replacements do not reopen it.
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { useAgent } from './store'
import { ChatPanel } from './components'
import { TodoItem } from './store'

const todo = (content: string): TodoItem => ({ content, status: 'pending', priority: 'high' })

describe('goal overlay preserves dismissal (PR #860)', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: 42,
      messagesByConv: {},
      todosByConv: {},
    })
  })

  it('stays dismissed when a later TodoWrite replaces the todos', () => {
    // First checklist for conversation 42.
    useAgent.setState({
      todosByConv: { '42': [todo('first task')] },
    })
    render(<ChatPanel />)

    // The overlay appears.
    expect(screen.getByLabelText('Goal checklist')).toBeTruthy()

    // The user dismisses it.
    const close = screen.getByLabelText('Dismiss goal checklist') as HTMLButtonElement
    act(() => {
      close.click()
    })
    expect(screen.queryByLabelText('Goal checklist')).toBeNull()

    // A later TodoWrite replaces the todos for the same conversation.
    act(() => {
      useAgent.setState({
        todosByConv: { '42': [todo('second task')] },
      })
    })

    // The overlay must NOT reopen — dismissal was preserved.
    expect(screen.queryByLabelText('Goal checklist')).toBeNull()
  })

  it('opens again when the conversation changes to one with a checklist', () => {
    useAgent.setState({
      todosByConv: { '42': [todo('task a')] },
    })
    render(<ChatPanel />)
    expect(screen.getByLabelText('Goal checklist')).toBeTruthy()

    // Dismiss, then switch to a different conversation that has its own
    // checklist — the overlay must reopen for the new conversation.
    const close = screen.getByLabelText('Dismiss goal checklist') as HTMLButtonElement
    act(() => {
      close.click()
    })
    expect(screen.queryByLabelText('Goal checklist')).toBeNull()

    act(() => {
      useAgent.setState({
        conversationId: 99,
        todosByConv: { '42': [todo('task a')], '99': [todo('task b')] },
      })
    })

    expect(screen.getByLabelText('Goal checklist')).toBeTruthy()
  })

  it('opens when the first checklist arrives after mount (conversation started empty)', () => {
    // Conversation 42 starts with NO todos — the exact case the tracking-by-id
    // fix missed: the effect recorded 42 without opening, so the first
    // TodoWrite that later supplies todos was blocked by the unchanged ID.
    useAgent.setState({
      conversationId: 42,
      todosByConv: { '42': [] },
    })
    render(<ChatPanel />)
    expect(screen.queryByLabelText('Goal checklist')).toBeNull()

    // The first TodoWrite supplies the checklist.
    act(() => {
      useAgent.setState({
        todosByConv: { '42': [todo('first task')] },
      })
    })

    // The overlay must open on that first checklist.
    expect(screen.getByLabelText('Goal checklist')).toBeTruthy()
  })
})
