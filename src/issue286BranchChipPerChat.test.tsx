import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { selectConversationBranch, getGitBranches, appendRawMessage } = vi.hoisted(() => ({
  selectConversationBranch: vi.fn(),
  getGitBranches: vi.fn(),
  appendRawMessage: vi.fn(),
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
      (selector: (s: Record<string, unknown>) => unknown) => selector({ appendRawMessage }),
      { getState: () => ({ appendRawMessage }) },
    ),
  }
})

import { GitChipCluster } from './components'
import type { GitInfo } from './api'

// #286: the status-strip branch chip is the chat's own branch. Flipping it
// calls the per-chat branch-select endpoint (which stores the pick and runs
// no git checkout) — never the workspace checkout, which stays the draft
// card's / the human's primary-worktree tool.
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

function mountCluster(opts: { info: GitInfo; selectedBranch: string | null }) {
  return render(
    <GitChipCluster
      info={opts.info}
      streaming={false}
      conversationId={7}
      selectedBranch={opts.selectedBranch}
      onCommandDone={() => {}}
    />,
  )
}

describe('per-chat branch chip (#286)', () => {
  beforeEach(() => {
    selectConversationBranch.mockResolvedValue({ ok: true, selected_branch: 'feature' })
    getGitBranches.mockResolvedValue({ branches: ['feature', 'master'] })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows the stored per-chat selection over the workspace branch', () => {
    mountCluster({ info: info(), selectedBranch: 'feature' })
    expect(screen.getByRole('button', { name: /branch for this chat/i }).textContent).toContain('feature')
  })

  it('falls back to the workspace branch when the chat has no stored pick', () => {
    mountCluster({ info: info({ branch: 'master' }), selectedBranch: null })
    expect(screen.getByRole('button', { name: /branch for this chat/i }).textContent).toContain('master')
  })

  it('flip goes to the per-chat branch-select endpoint, not a workspace checkout', async () => {
    const { container } = mountCluster({ info: info(), selectedBranch: null })

    fireEvent.click(screen.getByRole('button', { name: /branch for this chat/i }))
    await screen.findByText('feature') // dropdown lists local branches

    const items = container.querySelectorAll('.max-h-56 button')
    const featureItem = Array.from(items).find((b) => b.textContent?.includes('feature'))
    expect(featureItem, 'feature item in the dropdown').toBeTruthy()
    fireEvent.click(featureItem!)

    await waitFor(() => expect(selectConversationBranch).toHaveBeenCalledTimes(1))
    expect(selectConversationBranch).toHaveBeenCalledWith(7, 'feature')
    // The flip still lands in the transcript as a checkout trace row.
    expect(appendRawMessage).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({ role: 'tool' }),
    )
  })

  it('marks the stored pick — not the checked-out branch — in the dropdown', async () => {
    const { container } = mountCluster({ info: info({ branch: 'master' }), selectedBranch: 'feature' })

    fireEvent.click(screen.getByRole('button', { name: /branch for this chat/i }))
    await screen.findByText('feature')
    const checkmark = container.querySelector('.text-blue-400')!
    expect(checkmark).toBeTruthy()
    const item = checkmark.closest('button')!
    expect(item.textContent).toContain('feature')
  })
})
