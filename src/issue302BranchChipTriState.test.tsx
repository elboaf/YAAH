import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const { selectConversationBranch, getGitBranches } = vi.hoisted(() => ({
  selectConversationBranch: vi.fn(),
  getGitBranches: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    selectConversationBranch,
    getGitBranches,
  }
})

vi.mock('./store', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./store')>()
  return {
    ...actual,
    useAgent: Object.assign(
      (selector: (s: Record<string, unknown>) => unknown) => selector({ appendRawMessage: vi.fn() }),
      { getState: () => ({ appendRawMessage: vi.fn() }) },
    ),
  }
})

import { GitChipCluster } from './components'
import type { GitInfo } from './api'

// #302 (ADR-0010 amendment, decision 1): the chip distinguishes an
// inherited pin from an explicit pick ("· workspace" marker) and flags a
// stale pin whose branch no longer exists locally.
function info(overrides: Partial<GitInfo> = {}): GitInfo {
  return {
    branch: 'master',
    upstream: null,
    local_hash: 'abc1234',
    remote_hash: null,
    ahead: 0,
    behind: 0,
    added: 0,
    deleted: 0,
    dirty: false,
    untracked: 0,
    changed: 0,
    ...overrides,
  }
}

function mountCluster(opts: {
  info: GitInfo
  selectedBranch: string | null
  selectedOrigin?: 'explicit' | 'inherited' | null
  selectedStale?: boolean
}) {
  return render(
    <GitChipCluster
      info={opts.info}
      streaming={false}
      conversationId={7}
      selectedBranch={opts.selectedBranch}
      selectedOrigin={opts.selectedOrigin ?? null}
      selectedStale={opts.selectedStale ?? false}
      onCommandDone={() => {}}
    />,
  )
}

const chipButton = () => screen.getByRole('button', { name: /branch for this chat/i })

describe('branch chip tri-state (#302)', () => {
  beforeEach(() => {
    selectConversationBranch.mockResolvedValue({ ok: true, selected_branch: 'feature' })
    getGitBranches.mockResolvedValue({ branches: ['feature', 'master'] })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('an explicit pick shows no inherited marker', () => {
    mountCluster({ info: info(), selectedBranch: 'feature', selectedOrigin: 'explicit' })
    expect(chipButton().textContent).not.toContain('workspace')
  })

  it('an inherited pin is marked "· workspace"', () => {
    mountCluster({ info: info(), selectedBranch: 'master', selectedOrigin: 'inherited' })
    expect(chipButton().textContent).toContain('master')
    expect(chipButton().textContent).toContain('workspace')
  })

  it('no pick at all — the workspace branch carries the inherited marker', () => {
    // Production props: the endpoint's fallback fills selectedBranch with
    // the workspace's branch and leaves pin_origin null — still inherited.
    mountCluster({ info: info({ branch: 'main' }), selectedBranch: 'main', selectedOrigin: null })
    expect(chipButton().textContent).toContain('main')
    expect(chipButton().textContent).toContain('workspace')
  })

  it('a stale pin is flagged in the chip, not silently rendered', () => {
    mountCluster({ info: info(), selectedBranch: 'doomed', selectedOrigin: 'explicit', selectedStale: true })
    expect(chipButton().textContent).toContain('doomed')
    expect(chipButton().textContent).toContain('stale')
    expect(chipButton().textContent).not.toContain('workspace')
  })

  it('a live branch is never flagged stale', () => {
    mountCluster({ info: info(), selectedBranch: 'feature', selectedOrigin: 'explicit', selectedStale: false })
    expect(chipButton().textContent).not.toContain('stale')
  })

  it('picking a branch from the dropdown still flips to the per-chat endpoint (explicit)', async () => {
    const { container } = mountCluster({ info: info(), selectedBranch: null, selectedOrigin: null })

    fireEvent.click(chipButton())
    await screen.findByText('feature')

    const items = container.querySelectorAll('.max-h-56 button')
    const featureItem = Array.from(items).find((b) => b.textContent?.includes('feature'))
    expect(featureItem, 'feature item in the dropdown').toBeTruthy()
    fireEvent.click(featureItem!)

    await vi.waitFor(() => expect(selectConversationBranch).toHaveBeenCalledWith(7, 'feature'))
  })

  it('the dropdown checkmark follows the chat branch even when stale', async () => {
    // The dropdown was opened while the branch still listed (or the list
    // predates the deletion): the checkmark must still follow the chat's
    // own branch, stale or not.
    getGitBranches.mockResolvedValue({ branches: ['doomed', 'feature', 'master'] })
    const { container } = mountCluster({
      info: info(),
      selectedBranch: 'doomed',
      selectedOrigin: 'explicit',
      selectedStale: true,
    })

    fireEvent.click(chipButton())
    await screen.findByText('feature')
    const checkmark = container.querySelector('.text-blue-400')!
    expect(checkmark).toBeTruthy()
    expect(checkmark.closest('button')!.textContent).toContain('doomed')
  })
})
