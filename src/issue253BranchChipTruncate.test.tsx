import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

const { listLocalWorkspaces, listWorkspaces, addWorkspace, getWorkspaceGitBranches, checkoutWorkspaceBranch } = vi.hoisted(() => ({
  listLocalWorkspaces: vi.fn(),
  listWorkspaces: vi.fn(),
  addWorkspace: vi.fn(),
  getWorkspaceGitBranches: vi.fn(),
  checkoutWorkspaceBranch: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    listLocalWorkspaces,
    listWorkspaces,
    addWorkspace,
    getWorkspaceGitBranches,
    checkoutWorkspaceBranch,
  }
})

import { DraftDestinationCard } from './components'
import { useAgent } from './store'

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

// #253: a long branch name must not squish the workspace select — the branch
// chip truncates (matching the status-strip chip) and the workspace always wins.
describe('draft destination branch chip (#253)', () => {
  beforeEach(() => {
    listLocalWorkspaces.mockResolvedValue(workspaces)
    listWorkspaces.mockResolvedValue(workspaces)
    addWorkspace.mockResolvedValue(workspaces[0])
    getWorkspaceGitBranches.mockResolvedValue({
      branch: 'feature/very-long-branch-name-that-would-otherwise-squish-the-workspace-select',
      branches: ['feature/very-long-branch-name-that-would-otherwise-squish-the-workspace-select', 'main'],
    })
    checkoutWorkspaceBranch.mockResolvedValue({ ok: true, output: '' })
    useAgent.setState({
      conversationId: null,
      workspace: 'C:/repos/project',
      draftDestination: null,
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders the chip with an inner truncating span, full name in title, capped max width', async () => {
    render(<DraftDestinationCard />)

    const chip = await screen.findByRole('button', { name: /branch feature/i })
    const label = chip.querySelector('span.truncate')
    expect(label, 'chip label should have the truncate class').not.toBeNull()
    expect(label!.className).toContain('min-w-0')
    expect(label!.className).toMatch(/max-w-\[/)
    expect(label!.textContent).toBe('feature/very-long-branch-name-that-would-otherwise-squish-the-workspace-select')
    expect(chip.getAttribute('title')).toContain('feature/very-long-branch-name-that-would-otherwise-squish-the-workspace-select')
  })

  it('keeps the workspace select a flex-1 min-w-0 item that the chip cannot squeeze', async () => {
    render(<DraftDestinationCard />)

    const select = await screen.findByRole('combobox', { name: 'Save this chat to' })
    await waitFor(() => expect(screen.getByRole('button', { name: /branch feature/i })).toBeInTheDocument())
    expect(select.className).toContain('flex-1')
    expect(select.className).toContain('min-w-0')
  })
})

function useAgentState() {
  // eslint-disable-next-line @typescript-eslint/no-var-requires
  const { useAgent } = require('./store') as typeof import('./store')
  useAgent.setState({
    conversationId: null,
    workspace: 'C:/repos/project',
    draftDestination: null,
  })
}
