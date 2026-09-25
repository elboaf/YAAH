// The end-of-turn merge-back handshake (issue #58 decision 5) persists as a
// role='system' row containing {"worktree_merge": ...}. The system-row
// renderer was built for "turn failed" markers and painted EVERY system
// line red — a successful merge ("merged": true) loaded from history looked
// like an error. Red is reserved for failures: merge-back rows render as
// the same git_merge_back tool pill the live stream showed.

import { describe, expect, it, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react'
import { MessageView, gitMermaid } from './components'
import type { ChatMessage } from './store'

const sys = (content: string): ChatMessage => ({ id: 's1', role: 'system', content })

afterEach(() => cleanup())

describe('persisted merge-back system rows', () => {
  it('builds a branch-flow Mermaid graph with separate workspace lanes and explicit outcomes', () => {
    const graph = gitMermaid({
      run_id: 'run-42',
      outcome: 'completed',
      lanes: [{
        id: 'parent', label: 'Agent', branch: 'agent/42/fix', base_branch: 'main',
        branch_action: 'created', commits_ahead: 1, worktree: 'kept', integrated: false,
        operations: [
          { sequence: 1, operation: 'commit', outcome: 'succeeded', source: 'agent-tool', commit: 'abc123', subject: 'Fix parser' },
          { sequence: 2, operation: 'push', outcome: 'failed', source: 'agent-tool', remote: 'origin', target_branch: 'agent/42/fix' },
        ],
      }],
    })
    expect(graph).toContain('flowchart LR')
    expect(graph).toContain('Primary workspace')
    expect(graph).toContain('created agent/42/fix')
    expect(graph).toContain('Commit: completed')
    expect(graph).toContain('Push: failed')
    expect(graph).toContain('Not merged')
    expect(graph).not.toContain('merged into primary')
  })
  it('omits exploration lanes and clean worktree setup without hiding Git activity', () => {
    const graph = gitMermaid({
      run_id: 'run-exploration',
      outcome: 'completed',
      lanes: [
        {
          id: 'explore', label: 'Explore', branch: 'agent/42/task',
          branch_action: 'reused', commits_ahead: null, dirty: null,
          worktree: 'shared', integrated: null, operations: [],
        },
        {
          id: 'empty', label: 'Empty agent', branch: 'agent/42/empty',
          branch_action: 'created', commits_ahead: 0, dirty: false,
          worktree: 'removed', integrated: false,
          operations: [{ sequence: 1, operation: 'checkout', outcome: 'succeeded', source: 'harness', detail: 'created isolated worktree' }],
        },
        {
          id: 'work', label: 'Changed agent', branch: 'agent/42/work',
          branch_action: 'created', commits_ahead: 1, dirty: false,
          worktree: 'removed', integrated: false,
          operations: [{ sequence: 2, operation: 'commit', outcome: 'succeeded', source: 'agent-tool', commit: 'abc123' }],
        },
      ],
    })

    expect(graph).not.toContain('Explore')
    expect(graph).not.toContain('Empty agent')
    expect(graph).toContain('Changed agent')
    expect(graph).toContain('Commit: completed')
  })

  it('omits a redundant no-work terminal when a clean isolated lane is drained', () => {
    const graph = gitMermaid({
      run_id: 'run-drained',
      outcome: 'completed',
      lanes: [{
        id: 'parent', label: 'Agent', branch: 'agent/42/empty', base_branch: 'master',
        branch_action: 'created', commits_ahead: 0, dirty: false, worktree: 'removed',
        integrated: false, operations: [],
      }],
    })
    expect(graph).not.toContain('Not merged')
    expect(graph).not.toContain('no unmerged work')

    render(
      <MessageView
        msg={sys(JSON.stringify({
          git_activity: {
            run_id: 'run-drained',
            outcome: 'completed',
            lanes: [{
              id: 'parent', label: 'Agent', branch: 'agent/42/empty', base_branch: 'master',
              branch_action: 'created', commits_ahead: 0, dirty: false, worktree: 'removed',
              integrated: false, operations: [],
            }],
          },
        }))}
      />,
    )
    const chip = screen.getByRole('button', { name: /no unmerged work/ })
    fireEvent.click(chip)
    expect(screen.queryByText(/Not merged/)).toBeNull()
  })

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

  it('renders an independent expandable Git activity summary with branch and commit details', async () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            git_activity: {
              version: 1,
              run_id: 'run-42',
              outcome: 'completed',
              coverage: 'structured Git tools',
              lanes: [
                {
                  id: 'parent',
                  label: 'Agent',
                  branch: 'agent/42/fix',
                  base_branch: 'main',
                  branch_action: 'created',
                  commits_ahead: 1,
                  dirty: false,
                  worktree: 'kept',
                  integrated: false,
                  operations: [
                    { sequence: 1, operation: 'commit', outcome: 'succeeded', source: 'agent-tool', commit: 'abc123', subject: 'Fix parser' },
                    { sequence: 2, operation: 'push', outcome: 'failed', source: 'agent-tool', remote: 'origin', target_branch: 'agent/42/fix', detail: 'network unavailable' },
                  ],
                },
              ],
            },
          }),
        )}
      />,
    )
    const chip = screen.getByRole('button', { name: /2 Git operations.*1 unsuccessful/ })
    expect(chip.getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByText('Fix parser')).toBeNull()
    fireEvent.click(chip)
    expect(screen.getByText(/Created branch from main/)).toBeTruthy()
    expect(screen.getByText(/abc123 Fix parser/)).toBeTruthy()
    expect(screen.getByText(/origin\/agent\/42\/fix/)).toBeTruthy()
    expect(screen.getByText(/Not merged/)).toBeTruthy()
    expect(screen.getByText(/Coverage: structured Git tools/)).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/Branch-flow diagram unavailable/)).toBeTruthy())
  })

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

  it('explains that committed work is not yet integrated without asking the user to manage branches', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_status: {
              branch: 'agent/221/work',
              base_branch: 'master',
              worktree_id: '221',
              commits: 2,
              dirty: false,
            },
          }),
        )}
      />,
    )
    expect(screen.getByText(/2 committed change\(s\) are not yet integrated into the main workspace/)).toBeTruthy()
    expect(screen.getByText(/target branch master/)).toBeTruthy()
    expect(screen.queryByText(/push the agent branch/)).toBeNull()
    expect(screen.queryByText(/worktree #221/)).toBeNull()
  })

  it('warns that uncommitted changes are not yet integrated', () => {
    render(
      <MessageView
        msg={sys(
          JSON.stringify({
            worktree_status: {
              branch: 'agent/221/work',
              commits: 1,
              dirty: true,
            },
          }),
        )}
      />,
    )
    expect(screen.getByText(/additional uncommitted changes are not included/)).toBeTruthy()
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
