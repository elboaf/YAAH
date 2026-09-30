// The system-row renderer must not paint every role='system' line red.
// Red is reserved for failures; structured rows (file changes) and genuine
// failure markers render distinctly.

import { describe, expect, it, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

const sys = (content: string): ChatMessage => ({ id: 's1', role: 'system', content })

afterEach(() => cleanup())

describe('persisted system rows', () => {
  it('renders a collapsed run summary that expands to per-file line changes', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            file_changes: {
              files: [
                { path: 'src/App.tsx', added: 3, deleted: 1 },
                { path: 'package.json', added: 1, deleted: 1 },
              ],
              added: 4,
              deleted: 2,
            },
          }),
        )}
      />,
    )
    const chip = screen.getByRole('button', { name: /2 files changed\+4-2/ })
    expect(chip.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('package.json')).toBeNull()
    fireEvent.click(chip)
    expect(chip.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('package.json')).toBeTruthy()
    expect(screen.getByText('src/App.tsx')).toBeTruthy()
  })

  it('shows the short commit sha when the run was committed', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            file_changes: {
              files: [{ path: 'src/App.tsx', added: 3, deleted: 1 }],
              added: 3,
              deleted: 1,
              commit: '821438d',
              extra_commits: 2,
            },
          }),
        )}
      />,
    )
    const chip = screen.getByRole('button', { name: /821438d/ })
    expect(chip.textContent).toMatch(/\+2 more/)
  })

  it('shows "not committed" when changes stayed uncommitted', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            file_changes: {
              files: [{ path: 'src/App.tsx', added: 3, deleted: 1 }],
              added: 3,
              deleted: 1,
              commit: null,
              extra_commits: 0,
            },
          }),
        )}
      />,
    )
    expect(screen.getByRole('button', { name: /not committed/ })).toBeTruthy()
  })

  it('legacy summaries without commit fields still render', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            file_changes: {
              files: [{ path: 'src/App.tsx', added: 3, deleted: 1 }],
              added: 3,
              deleted: 1,
            },
          }),
        )}
      />,
    )
    expect(screen.getByRole('button', { name: /1 file changed\+3-1/ })).toBeTruthy()
    expect(screen.queryByText(/not committed/)).toBeNull()
  })

  it('keeps genuine failure markers red (\u26a0 + turn failed)', () => {
    render(<MessageView msg={sys('turn failed: RuntimeError: provider down')} />)
    expect(screen.getByText(/turn failed/)).toBeTruthy()
    expect(screen.getByText('\u26a0')).toBeTruthy()
  })

  it('a malformed system row falls back to the failure-marker rendering', () => {
    render(<MessageView msg={sys('{not json')} />)
    expect(screen.getByText('\u26a0')).toBeTruthy()
  })
})