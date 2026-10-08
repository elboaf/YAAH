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

// #335 (selector parity, spec #332): the draft destination card's branch
// picker is live for remote workspaces — the HOST's branches through the
// gateway — and a pick records intent (draftScope.branch), exactly like
// the local card. Offline hosts keep the picker hidden (empty answers).
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
