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
    appendRawMessage,
  }
})

vi.mock('./store', () => ({
  useAgent: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      appendRawMessage,
      workspace: '',
      conversationId: 7,
    }),
}))

import { GitChipCluster } from './components'
import type { GitInfo } from './api'
import { baseGitInfo } from './gitInfoFixture'

// #363: the branch chip is the ONE workspace tree's checked-out branch.
// The dropdown's checkout calls the branch-select endpoint - a plain
// checkout of the workspace - and the action lands as a trace row.

function mountCluster() {
  return render(
    <GitChipCluster
      info={baseGitInfo()}
      streaming={false}
      conversationId={7}
      onCommandDone={() => {}}
    />,
  )
}

describe('workspace branch chip (#363 direct world)', () => {
  beforeEach(() => {
    selectConversationBranch.mockReset()
    getGitBranches.mockReset()
    appendRawMessage.mockReset()
  })

  afterEach(cleanup)

  it('shows the workspace branch from git-info', () => {
    mountCluster()
    expect(screen.getByRole('button', { name: /workspace branch/i }).textContent).toContain('master')
  })

  it('the dropdown checkout calls the branch-select endpoint and traces the action', async () => {
    getGitBranches.mockResolvedValue({ branches: ['feature', 'master'] })
    selectConversationBranch.mockResolvedValue({ ok: true, branch: 'feature', created: false })
    mountCluster()
    fireEvent.click(screen.getByRole('button', { name: /workspace branch/i }))
    await waitFor(() => expect(getGitBranches).toHaveBeenCalledTimes(1))
    const featureItem = screen.getByRole('button', { name: /feature/ })
    expect(featureItem, 'feature item in the dropdown').toBeTruthy()
    fireEvent.click(featureItem)
    await waitFor(() => expect(selectConversationBranch).toHaveBeenCalledTimes(1))
    expect(selectConversationBranch).toHaveBeenCalledWith(7, 'feature')
    expect(appendRawMessage).toHaveBeenCalledWith(
      '7',
      expect.objectContaining({ role: 'tool' }),
    )
  })

  it('marks the checked-out branch in the dropdown', async () => {
    getGitBranches.mockResolvedValue({ branches: ['feature', 'master'] })
    mountCluster()
    fireEvent.click(screen.getByRole('button', { name: /workspace branch/i }))
    await waitFor(() => expect(getGitBranches).toHaveBeenCalledTimes(1))
    const item = screen.getByRole('button', { name: /master/ })
    const checkmark = item.querySelector('span')
    expect(checkmark).toBeTruthy()
    expect(item.textContent).toContain('master')
  })
})