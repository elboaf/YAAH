import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { GitChipCluster } from './components'
import type { GitInfo } from './api'
import { baseGitInfo } from './gitInfoFixture'

// Issue #350: the sync readout shows three short hashes - the selected
// branch's tip (local), its upstream's tip, and the chat tree's HEAD
// (worktree) - each colored by its own divergence instead of the old
// whole-pair color. The regression being pinned: local chats whose tree
// sits detached used to lose the upstream hash entirely (unresolvable
// @{upstream}); the backend now resolves from the branch, and the UI
// renders every hash the data carries.
function info(overrides: Partial<GitInfo> = {}): GitInfo {
  return baseGitInfo(overrides)

}

function mountCluster(opts: { info: GitInfo; selectedBranch?: string | null }) {
  return render(
    <GitChipCluster
      info={opts.info}
      streaming={false}
      conversationId={7}
      selectedBranch={opts.selectedBranch ?? null}
      onCommandDone={() => {}}
    />,
  )
}

const hashButton = (hash: string) =>
  screen.getByTitle(new RegExp(`copy ${hash}`))

describe('three-hash sync readout (#350)', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders local and upstream hashes when the backend supplies both', () => {
    mountCluster({
      info: info({ upstream: 'origin/master', remote_hash: 'def5678' }),
    })
    expect(hashButton('abc1234')).toBeTruthy()
    expect(hashButton('def5678')).toBeTruthy()
  })

  it('omits the worktree hash when the chat has no tree (never zero-filled)', () => {
    mountCluster({ info: info() })
    expect(screen.queryByTitle(/worktree/)).toBeNull()
  })

  it('shows the worktree hash, amber when ahead of the primary tree', () => {
    const { rerender } = mountCluster({
      info: info({ worktree_hash: 'wip1234', worktree_ahead: 0 }),
    })
    const sync = hashButton('wip1234')
    expect(sync.className).toContain('text-zinc-400')

    rerender(
      <GitChipCluster
        info={info({ worktree_hash: 'wip1234', worktree_ahead: 2 })}
        streaming={false}
        conversationId={7}
        selectedBranch={null}
        onCommandDone={() => {}}
      />,
    )
    expect(hashButton('wip1234').className).toContain('text-yellow-400')
  })

  it('paints the upstream hash blue when ahead, red when behind', () => {
    const { rerender } = mountCluster({
      info: info({ upstream: 'origin/master', remote_hash: 'def5678', ahead: 2 }),
    })
    expect(hashButton('def5678').className).toContain('text-blue-400')

    rerender(
      <GitChipCluster
        info={info({ upstream: 'origin/master', remote_hash: 'def5678', behind: 1 })}
        streaming={false}
        conversationId={7}
        selectedBranch={null}
        onCommandDone={() => {}}
      />,
    )
    expect(hashButton('def5678').className).toContain('text-red-400')
  })

  it('paints the upstream hash red when diverged (behind wins the button)', () => {
    mountCluster({
      info: info({ upstream: 'origin/master', remote_hash: 'def5678', ahead: 3, behind: 1 }),
    })
    const btn = hashButton('def5678')
    expect(btn.className).toContain('text-red-400')
    // and both counters still render - magnitude lives beside the color.
    expect(screen.getByText('↑3')).toBeTruthy()
    expect(screen.getByText('↓1')).toBeTruthy()
  })

  it('leaves the upstream hash zinc when in sync', () => {
    mountCluster({
      info: info({ upstream: 'origin/master', remote_hash: 'def5678' }),
    })
    expect(hashButton('def5678').className).toContain('text-zinc-400')
  })

  it('copies a hash to the clipboard on click', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    mountCluster({
      info: info({ upstream: 'origin/master', remote_hash: 'def5678' }),
    })
    fireEvent.click(hashButton('def5678'))
    await vi.waitFor(() => expect(writeText).toHaveBeenCalledWith('def5678'))
  })
})
