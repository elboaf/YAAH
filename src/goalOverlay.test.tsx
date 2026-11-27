import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { GoalOverlay } from './components'

afterEach(() => cleanup())

describe('goal checklist overlay (#855)', () => {
  it('renders the objective and each todo item', () => {
    render(
      <GoalOverlay
        objective="ship the release"
        iteration={1}
        todos={[
          { content: 'bump version', status: 'completed', priority: 'high' },
          { content: 'run tests', status: 'in_progress', priority: 'high' },
          { content: 'cut rc', status: 'pending', priority: 'low' },
        ]}
        onClose={() => {}}
      />,
    )
    expect(screen.getByText('ship the release')).toBeTruthy()
    expect(screen.getByText('bump version')).toBeTruthy()
    expect(screen.getByText('run tests')).toBeTruthy()
    expect(screen.getByText('cut rc')).toBeTruthy()
    // Completion tally renders.
    expect(screen.getByText(/1\/3 done/)).toBeTruthy()
  })

  it('the close button hides the overlay', () => {
    const onClose = vi.fn()
    const { container } = render(
      <GoalOverlay
        objective="keep this"
        iteration={1}
        todos={[{ content: 'do the thing', status: 'pending', priority: 'high' }]}
        onClose={onClose}
      />,
    )
    const close = container.querySelector('button[aria-label="Dismiss goal checklist"]') as HTMLButtonElement
    expect(close).toBeTruthy()
    close?.click()
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
