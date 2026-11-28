import { afterEach, describe, expect, it, beforeEach } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { useAgent, TodoItem } from './store'
import { ChatPanel, ObjectiveCard } from './components'

const todo = (content: string): TodoItem => ({ content, status: 'pending', priority: 'high' })

describe('goal card removal (#892) — ChatPanel', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: 42,
      messagesByConv: {},
      objectiveByConv: {},
      todosByConv: {},
    })
  })
  afterEach(() => cleanup())

  it('renders no inline goal card in the transcript (the card is gone)', () => {
    useAgent.setState({
      objectiveByConv: { '42': 'ship the landing page' },
    })
    render(<ChatPanel />)
    // The inline card drew a 'goal' ledger header at the top of the transcript;
    // with an objective but no checklist there is now no goal surface at all.
    expect(screen.queryByText(/ship the landing page/)).toBeNull()
  })

  it('renders the goal surface only as the floating overlay (no card + overlay duplication)', () => {
    useAgent.setState({
      objectiveByConv: { '42': 'ship the landing page' },
      todosByConv: { '42': [todo('wire the form'), todo('add tests')] },
    })
    render(<ChatPanel />)
    // The overlay (the surviving surface) shows the objective + checklist...
    const overlay = screen.getByLabelText('Goal checklist')
    expect(overlay).toBeTruthy()
    expect(overlay.textContent).toContain('ship the landing page')
    expect(overlay.textContent).toContain('wire the form')
    // ...and the objective text appears exactly once on screen — the inline
    // card used to add a second copy of the same objective + checklist.
    expect(screen.getAllByText('ship the landing page')).toHaveLength(1)
  })
})

// The ObjectiveCard component itself is still used by the read-only remote
// transcript viewer (RemoteTranscriptDialog), which has no overlay — these unit
// tests cover that surface. The local ChatPanel no longer renders it.
describe('objective card component (#844 follow-up; remote viewer)', () => {
  afterEach(() => cleanup())

  it('shows the durable objective', () => {
    render(<ObjectiveCard objective="add tests for the auth module" iteration={1} />)
    expect(screen.getByText('add tests for the auth module')).toBeTruthy()
  })

  it('shows no iteration badge on the first attempt', () => {
    render(<ObjectiveCard objective="add tests for the auth module" iteration={1} />)
    expect(screen.queryByText(/iteration 1/)).toBeNull()
  })

  it('surfaces the verifier-loop iteration once it has run past the first', () => {
    render(<ObjectiveCard objective="add tests for the auth module" iteration={3} />)
    expect(screen.getByText(/iteration 3/)).toBeTruthy()
  })

  it('renders the multi-goal checklist with completion markers', () => {
    render(
      <ObjectiveCard
        objective="fix the login bug"
        iteration={1}
        todos={[
          { content: 'reproduce the crash', status: 'completed', priority: 'high' },
          { content: 'patch the handler', status: 'in_progress', priority: 'high' },
          { content: 'add regression tests', status: 'pending', priority: 'low' },
        ]}
      />,
    )
    // All three sub-goals render.
    expect(screen.getByText('reproduce the crash')).toBeTruthy()
    expect(screen.getByText('patch the handler')).toBeTruthy()
    expect(screen.getByText('add regression tests')).toBeTruthy()
    // Completion tally shows.
    expect(screen.getByText(/1\/3 done/)).toBeTruthy()
  })

  it('renders no checklist when there are none', () => {
    render(<ObjectiveCard objective="fix the login bug" iteration={1} todos={[]} />)
    // No tally, no checklist rows — just the objective.
    expect(screen.queryByText(/done/)).toBeNull()
  })
})
