import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getWorkspaceBranches, selectWorkspaceBranch } = vi.hoisted(() => ({
  getWorkspaceBranches: vi.fn(),
  selectWorkspaceBranch: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getWorkspaceBranches,
    selectWorkspaceBranch,
  }
})

import { DraftDestinationCard } from './components'
import { useAgent } from './store'
import { useRemote } from './remoteStore'

const workspaces = [
  {
    id: 1,
    path: 'C:/repos/project',
    label: 'project',
    last_opened_at: null,
    exists: true,
    conversation_count: 0,
  },
]

// The draft card's branch chip, restored after #361 dropped it with the
// per-chat stored branch: the destination workspace's checked-out branch,
// picking one checks the ONE workspace tree out (backend branch_select).
describe('draft card branch chip', () => {
  beforeEach(() => {
    useAgent.setState({
      conversationId: null,
      workspace: 'C:/repos/project',
      draftDestination: null,
    })
    useRemote.setState({ scope: { connected: false }, devices: [] })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows the workspace branch and checks out a picked branch', async () => {
    getWorkspaceBranches.mockResolvedValue({ branch: 'main', branches: ['main', 'feat'] })
    selectWorkspaceBranch.mockResolvedValue({ ok: true, branch: 'feat' })

    render(<DraftDestinationCard />)

    const chip = await screen.findByRole('button', { name: 'Branch main; switch branch' })
    fireEvent.click(chip)
    const item = await screen.findByRole('menuitem', { name: /feat/ })
    fireEvent.click(item)

    await waitFor(() => expect(selectWorkspaceBranch).toHaveBeenCalledWith('C:/repos/project', 'feat'))
    expect(await screen.findByRole('button', { name: 'Branch feat; switch branch' })).toBeInTheDocument()
  })

  it('surfaces a refused checkout verbatim', async () => {
    getWorkspaceBranches.mockResolvedValue({ branch: 'main', branches: ['main', 'feat'] })
    selectWorkspaceBranch.mockResolvedValue({ ok: false, error: 'local changes would be overwritten' })

    render(<DraftDestinationCard />)

    fireEvent.click(await screen.findByRole('button', { name: 'Branch main; switch branch' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /feat/ }))

    expect(await screen.findByRole('alert')).toHaveTextContent('local changes would be overwritten')
  })

  it('hides the chip when the destination is not a local git workspace', async () => {
    getWorkspaceBranches.mockResolvedValue({ branch: null, branches: [] })
    useAgent.setState({ workspace: '' })

    render(<DraftDestinationCard />)

    await waitFor(() => expect(getWorkspaceBranches).not.toHaveBeenCalled())
    expect(screen.queryByRole('button', { name: /switch branch/ })).not.toBeInTheDocument()
  })
})
