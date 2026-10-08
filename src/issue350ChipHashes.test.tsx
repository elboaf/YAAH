import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { GitChipCluster } from './components'
import type { GitInfo } from './api'
import { baseGitInfo } from './gitInfoFixture'

// Issue #350: the sync readout shows two short hashes - the checked-out
// branch's tip (local) and its upstream's tip - each colored by its own
// divergence instead of the old whole-pair color. The regression being
// pinned: a detached tree used to lose the upstream hash entirely
// (unresolvable @{upstream}); the backend resolves from the branch, and
// the UI renders every hash the data carries. #361: the worktree hash
// trio is gone - one tree, two hashes.
function info(overrides: Partial<GitInfo> = {}): GitInfo {
  return baseGitInfo(overrides)

}

function mountCluster(opts: { info: GitInfo }) {
  return render(
    <GitChipCluster
      info={opts.info}
      streaming={false}
      conversationId={7}
      onCommandDone={() => {}}
    />,
  )
}

const hashButton = (hash: string) =>
  screen.getByTitle(new RegExp(`copy ${hash}`))

describe('two-hash sync readout (#350, direct world)', () => {
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

  it('renders no worktree hash - the direct world has ONE tree (#361)', () => {
    mountCluster({ info: info() })
    expect(screen.queryByTitle(/worktree/)).toBeNull()
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

describe('always-visible column labels above each hash', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  const mountAll = () =>
    mountCluster({
      info: info({
        upstream: 'origin/master',
        remote_hash: 'def5678',
      }),
    })

  it('labels each hash with its role as visible text (not hover-only)', () => {
    mountAll()
    expect(screen.getByText('local')).toBeTruthy()
    expect(screen.getByText('origin')).toBeTruthy()
  })

  it('keeps the labels when a hash drops out — every rendered slot is named', () => {
    mountCluster({ info: info() })
    expect(screen.getByText('local')).toBeTruthy()
    expect(screen.queryByText('origin')).toBeNull()
  })

  it('the label is plain text — the hover/copy affordance stays on the hash below it', () => {
    mountAll()
    for (const label of ['local', 'origin']) {
      expect(screen.getByText(label).getAttribute('title') ?? '').not.toMatch(/copy/)
    }
    expect(hashButton('def5678').getAttribute('title')).toMatch(/copy def5678/)
  })
})
