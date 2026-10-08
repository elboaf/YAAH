import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'

const { listLocalWorkspaces, listWorkspaces, addWorkspace } = vi.hoisted(() => ({
  listLocalWorkspaces: vi.fn(),
  listWorkspaces: vi.fn(),
  addWorkspace: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    listLocalWorkspaces,
    listWorkspaces,
    addWorkspace,
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

// #253 pinned the draft-card branch chip's truncation against the
// workspace select; the chip is gone with the draft branch picker
// (#361 - the direct world, no per-chat branch picks). The surviving
// layout contract: the workspace select stays flex-1 min-w-0 so nothing
// can squeeze it.
describe('draft destination select layout (#253 remainder)', () => {
  beforeEach(() => {
    listLocalWorkspaces.mockResolvedValue(workspaces)
    listWorkspaces.mockResolvedValue(workspaces)
    addWorkspace.mockResolvedValue(workspaces[0])
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

  it('keeps the workspace select a flex-1 min-w-0 item', async () => {
    render(<DraftDestinationCard />)

    const select = await screen.findByRole('combobox', { name: 'Save this chat to' })
    expect(select.className).toContain('flex-1')
    expect(select.className).toContain('min-w-0')
  })
})

function useAgentState() {
  useAgent.setState({
    conversationId: null,
    workspace: 'C:/repos/project',
    draftDestination: null,
  })
}
