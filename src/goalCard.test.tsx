import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ObjectiveCard } from './components'

afterEach(() => cleanup())

describe('objective card (#844 follow-up)', () => {
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
