// The end-of-turn merge-back handshake (issue #58 decision 5) persists as a
// role='system' row containing {"worktree_merge": ...}. The system-row
// renderer was built for "turn failed" markers and painted EVERY system
// line red — a successful merge ("merged": true) loaded from history looked
// like an error. Red is reserved for failures: merge-back rows render as
// the same git_merge_back tool pill the live stream showed.

import { describe, expect, it, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import { MessageView } from './components'
import type { ChatMessage } from './store'

const sys = (content: string): ChatMessage => ({ id: 's1', role: 'system', content })

afterEach(() => cleanup())

describe('persisted merge-back system rows', () => {
  it('renders a successful merge-back as a git_merge_back pill, not red text', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_merge: {
              merged: true,
              commits: 1,
              branch: 'agent/221/147e8c06c74f',
              note: 'merged around 71 unrelated uncommitted file(s) in the main tree',
            },
          }),
        )}
      />,
    )
    expect(screen.getByText('git_merge_back')).toBeTruthy()
    // The failure marker (⚠ + red) must not appear for a successful merge.
    expect(screen.queryByText('⚠')).toBeNull()
  })

  it('a refused merge-back still renders as the pill — its reason lives in the detail', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_merge: { merged: false, reason: 'main tree is mid-merge; resolve that merge first' },
          }),
        )}
      />,
    )
    expect(screen.getByText('git_merge_back')).toBeTruthy()
    expect(screen.queryByText('⚠')).toBeNull()
  })

  it('a refused merge-back tints the chip red — red keeps meaning failure', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_merge: { merged: false, reason: 'refused' },
          }),
        )}
      />,
    )
    const chip = screen.getByText('git_merge_back').closest('span.inline-flex') as HTMLElement | null
    expect(chip).toBeTruthy()
    expect(chip!.className).toContain('text-red-300')
  })

  it('a successful merge chip is neutral, not red', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_merge: { merged: true, commits: 1, branch: 'agent/x' },
          }),
        )}
      />,
    )
    const chip = screen.getByText('git_merge_back').closest('span.inline-flex') as HTMLElement | null
    expect(chip).toBeTruthy()
    expect(chip!.className).not.toContain('text-red-300')
  })

  it('a zero-commit merge-back (work already merged) is a no-op, not red', () => {
    // The agent's branch was merged mid-turn; at end-of-turn the merge-back
    // finds no commits beyond HEAD and returns merged:false + zero_commits.
    // Nothing failed — the work IS in the main tree — so no red.
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_merge: {
              merged: false,
              reason: 'branch agent/221/x has no commits beyond HEAD',
              zero_commits: true,
            },
          }),
        )}
      />,
    )
    const chip = screen.getByText('git_merge_back').closest('span.inline-flex') as HTMLElement | null
    expect(chip).toBeTruthy()
    expect(chip!.className).not.toContain('text-red-300')
  })

  it('keeps genuine failure markers red (⚠ + turn failed)', () => {
    render(<MessageView msg={sys('turn failed: RuntimeError: provider down')} />)
    expect(screen.getByText(/turn failed/)).toBeTruthy()
    expect(screen.getByText('⚠')).toBeTruthy()
  })

  it('a malformed system row falls back to the failure-marker rendering', () => {
    render(<MessageView msg={sys('{not json')} />)
    expect(screen.getByText('⚠')).toBeTruthy()
  })
})
