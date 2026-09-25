import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn(async () => ({ svg: '<svg data-testid="rendered-diagram"></svg>' })),
  },
}))

import mermaid from 'mermaid'
import { MessageView } from './components'
import type { ChatMessage } from './store'

const summaryMessage = (): ChatMessage => ({
  id: 'git-activity-row',
  role: 'system',
  content: JSON.stringify({
    git_activity: {
      run_id: 'run-stable',
      outcome: 'completed',
      lanes: [{
        id: 'parent',
        label: 'Agent',
        branch: 'agent/fix',
        base_branch: 'master',
        branch_action: 'created',
        commits_ahead: 1,
        dirty: false,
        worktree: 'kept',
        integrated: false,
        operations: [],
      }],
    },
  }),
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Git activity diagram rendering', () => {
  it('does not redraw when an unrelated parent rerender recreates the parsed summary', async () => {
    const { rerender } = render(<MessageView msg={summaryMessage()} />)
    fireEvent.click(screen.getByRole('button', { name: /Git branch activity/ }))

    await waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('rendered-diagram')).toBeTruthy()

    rerender(<MessageView msg={summaryMessage()} />)
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(mermaid.render).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('rendered-diagram')).toBeTruthy()
  })

  it('toggles the diagram between its default and enlarged size', async () => {
    render(<MessageView msg={summaryMessage()} />)
    fireEvent.click(screen.getByRole('button', { name: /Git branch activity/ }))
    await waitFor(() => expect(mermaid.render).toHaveBeenCalledTimes(1))

    const enlargeButton = screen.getByRole('button', { name: 'Enlarge Git branch activity diagram' })
    expect(enlargeButton.getAttribute('aria-pressed')).toBe('false')
    expect(enlargeButton.className).toContain('w-full')

    fireEvent.click(enlargeButton)
    const restoreButton = screen.getByRole('button', { name: 'Restore Git branch activity diagram size' })
    expect(restoreButton.getAttribute('aria-pressed')).toBe('true')
    expect(restoreButton.className).toContain('fixed')
    expect(restoreButton.className).toContain('inset-[1%]')

    fireEvent.click(restoreButton)
    const restoredButton = screen.getByRole('button', { name: 'Enlarge Git branch activity diagram' })
    expect(restoredButton.getAttribute('aria-pressed')).toBe('false')
    expect(restoredButton.className).toContain('w-full')
  })
})
