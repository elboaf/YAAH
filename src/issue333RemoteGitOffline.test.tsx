// #333: a remote host that cannot be reached is an explicit state — the
// status strip renders a "host offline" chip instead of the git cluster
// silently vanishing (the old `if (!info) return null` absence).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

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
import { baseGitInfo } from './gitInfoFixture'

function info(overrides: Partial<GitInfo> = {}): GitInfo {
  return baseGitInfo(overrides)

}

function renderCluster(gitInfo: GitInfo | null) {
  return render(
    <GitChipCluster
      info={gitInfo}
      streaming={false}
      conversationId={1}
      selectedBranch={null}
      selectedOrigin={null}
      selectedStale={false}
      onCommandDone={() => {}}
    />,
  )
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('GitChipCluster offline state (#333)', () => {
  it('renders an explicit host-offline chip when the info carries offline', () => {
    renderCluster(info({ offline: true }))
    expect(screen.getByText('host offline')).toBeTruthy()
    // The normal branch chip does not render alongside it — the host's
    // branch is unknown, so showing one would be invention.
    expect(screen.queryByText('master')).toBeNull()
  })

  it('renders nothing when there is no info at all (unchanged)', () => {
    const { container } = renderCluster(null)
    expect(container.textContent).toBe('')
  })

  it('renders the normal chip for a live remote readout (no offline flag)', () => {
    renderCluster(info({ branch: 'master' }))
    expect(screen.queryByText('host offline')).toBeNull()
    expect(screen.getByText('master')).toBeTruthy()
  })
})
