import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { listLocalWorkspaces, listWorkspaces, addWorkspace, getWorkspaceGitBranches, checkoutWorkspaceBranch } = vi.hoisted(() => ({
  listLocalWorkspaces: vi.fn(),
  listWorkspaces: vi.fn(),
  addWorkspace: vi.fn(),
  getWorkspaceGitBranches: vi.fn(),
  checkoutWorkspaceBranch: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, listLocalWorkspaces, listWorkspaces, addWorkspace, getWorkspaceGitBranches, checkoutWorkspaceBranch }
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

describe('draft destination card (#90)', () => {
  beforeEach(() => {
    listLocalWorkspaces.mockResolvedValue(workspaces)
    listWorkspaces.mockResolvedValue(workspaces)
    addWorkspace.mockResolvedValue(workspaces[0])
    getWorkspaceGitBranches.mockResolvedValue({ branch: 'main', branches: ['feature', 'main'] })
    checkoutWorkspaceBranch.mockResolvedValue({ ok: true, output: 'Switched to branch feature' })
    useAgent.setState({ conversationId: null, workspace: 'C:/repos/project', draftDestination: null })
    useRemote.getState().setScope({ connected: false })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('loads destinations on mount and pins a choice directly from the dropdown', async () => {
    render(<DraftDestinationCard />)

    const select = screen.getByRole('combobox', { name: 'Save this chat to' })
    expect(screen.queryByRole('button', { name: /change/i })).not.toBeInTheDocument()
    await waitFor(() => expect(listLocalWorkspaces).toHaveBeenCalledOnce())
    expect(await screen.findByRole('option', { name: 'project' })).toBeInTheDocument()

    fireEvent.change(select, { target: { value: 'C:/repos/project' } })
    expect(useAgent.getState().draftDestination).toBe('C:/repos/project')
  })

  it('shows and checks out a git branch before the draft is saved', async () => {
    render(<DraftDestinationCard />)

    expect(await screen.findByRole('button', { name: /branch main/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /branch main/i }))
    expect(await screen.findByRole('menuitem', { name: /feature/ })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: /feature/ }))

    await waitFor(() => expect(checkoutWorkspaceBranch).toHaveBeenCalledWith('C:/repos/project', 'feature'))
    expect(await screen.findByRole('button', { name: /branch feature/i })).toBeInTheDocument()
  })

  it('surfaces checkout failures in the draft card', async () => {
    checkoutWorkspaceBranch.mockResolvedValueOnce({ ok: false, error: 'local changes would be overwritten' })
    render(<DraftDestinationCard />)

    fireEvent.click(await screen.findByRole('button', { name: /branch main/i }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /feature/ }))

    expect(await screen.findByText('local changes would be overwritten')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /branch main/i })).toBeInTheDocument()
  })

  it('keeps the connected-host path fallback reachable from the card', async () => {
    useRemote.getState().setScope({ connected: true })
    render(<DraftDestinationCard />)

    await waitFor(() => expect(listWorkspaces).toHaveBeenCalledOnce())
    fireEvent.click(screen.getByRole('button', { name: /add folder on host/i }))
    const pathInput = screen.getByRole('textbox', { name: 'Folder path on the host' })
    expect(pathInput).toBeInTheDocument()
    fireEvent.change(pathInput, { target: { value: 'D:/work/new-project' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))

    await waitFor(() => expect(addWorkspace).toHaveBeenCalledWith('D:/work/new-project'))
    expect(useAgent.getState().draftDestination).toBe('D:/work/new-project')
  })
})
