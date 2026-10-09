import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { listLocalWorkspaces, listWorkspaces } = vi.hoisted(() => ({
  listLocalWorkspaces: vi.fn(),
  listWorkspaces: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    listLocalWorkspaces,
    listWorkspaces,
  }
})

import { DraftDestinationCard } from './components'
import { useAgent } from './store'
import { useRemote } from './remoteStore'

// #363: the draft destination card carries no branch picker — the draft
// picks a workspace, nothing else. #361 removed the branch pick; the card
// just lists workspaces (local + remote, offline hosts hidden).
describe('draft destination card on a remote workspace (#335)', () => {
  beforeEach(() => {
    listLocalWorkspaces.mockResolvedValue([])
    listWorkspaces.mockResolvedValue([
      {
        id: 2,
        path: 'remote:h:C:/repo',
        label: 'repo',
        last_opened_at: null,
        exists: true,
        conversation_count: 0,
        owner_id: 'h',
        device_status: 'online',
      },
    ])
    useAgent.setState({
      conversationId: null,
      workspace: 'remote:h:C:/repo',
      draftDestination: null,
      draftScope: { model: '', effort: '' },
    })
    useRemote.setState({
      scope: { connected: false },
      devices: [
        { host_id: 'h', url: 'http://host', name: 'Host', status: 'online', workspaces: [] },
      ],
    })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('does not preload host branches when no destination is pinned', async () => {
    // The dest-less draft (Default) reads nothing — unchanged rule.
    useAgent.setState({ workspace: '', draftDestination: null })
    render(<DraftDestinationCard />)
    await waitFor(() => expect(listLocalWorkspaces).toHaveBeenCalledOnce())
  })
})
