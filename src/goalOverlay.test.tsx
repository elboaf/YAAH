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

  it('summarizes a long objective instead of reciting it verbatim (#893)', () => {
    const longObjective =
      'Please refactor the entire settings page so that providers, sub-agent ' +
      'routing, sandbox, memory, remote hosting, interface, step budget, and ' +
      'scheduled agents are grouped into logical tabs with a clear hierarchy, ' +
      'and move the global max steps field out of the buried Advanced tab'
    render(
      <GoalOverlay
        objective={longObjective}
        iteration={1}
        todos={[{ content: 'group the tabs', status: 'pending', priority: 'high' }]}
        onClose={() => {}}
      />,
    )
    const overlay = screen.getByLabelText('Goal checklist')
    // The full verbatim prompt must NOT be rendered...
    expect(overlay.textContent).not.toContain(longObjective)
    // ...a shortened form is: it ends with the ellipsis marker.
    expect(overlay.textContent).toContain('…')
    // And it starts with the same opening words (the summary is a prefix,
    // not invented copy).
    expect(overlay.textContent).toContain('Please refactor the entire settings page')
  })

  it('leaves a short objective verbatim (#893)', () => {
    render(
      <GoalOverlay
        objective="ship the landing page"
        iteration={1}
        todos={[{ content: 'build the page', status: 'pending', priority: 'high' }]}
        onClose={() => {}}
      />,
    )
    expect(screen.getByText('ship the landing page')).toBeTruthy()
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
