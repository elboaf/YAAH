import { afterEach, describe, expect, it, vi } from 'vitest'
import type { RemoteDevice, WorkspaceRow } from './api'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { DeviceGroups, RemoteTranscriptDialog } from './components'
import { useAgent } from './store'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'

const { getRemoteDeviceMessages, listRemoteDeviceConversations, streamRemoteTurn, cancelRemoteDeviceTurn } = vi.hoisted(() => ({
  getRemoteDeviceMessages: vi.fn(),
  listRemoteDeviceConversations: vi.fn(),
  streamRemoteTurn: vi.fn(),
  cancelRemoteDeviceTurn: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getRemoteDeviceMessages,
  listRemoteDeviceConversations,
  streamRemoteTurn,
  cancelRemoteDeviceTurn,
}))

afterEach(() => {
  cleanup()
  getRemoteDeviceMessages.mockReset()
  listRemoteDeviceConversations.mockReset()
  useAgent.setState({ conversationId: 7, messagesByConv: { '7': [{ id: 'local', role: 'user', content: 'local transcript' }] } })
  useRemoteConversations.setState({ transcripts: {} })
})

const row = (content: string, image?: string) => ({
  id: 1,
  role: 'user',
  content,
  images: image ? [image] : [],
  tool_call_id: null,
  tool_calls: null,
  sub_agent_transcript: null,
})

describe('read-only remote transcript viewer', () => {
  it('shows host-owned chats per device without opening them as local chats', async () => {
    listRemoteDeviceConversations.mockImplementation(async (hostId: string) => ({
      status: hostId === 'host-a' ? 'online' : 'cached',
      conversations: [{ host_id: hostId, conversation_id: '7', title: hostId === 'host-a' ? 'same ID from A' : 'same ID from B', workspace: null, updated_at: '2025-01-01', revision: '', sync_status: 'synced' }],
    }))
    getRemoteDeviceMessages.mockImplementation(async (hostId: string) => [row(`transcript from ${hostId}`)])
    const devices: RemoteDevice[] = [
      { host_id: 'host-a', url: 'http://a', name: 'Host A', status: 'online', workspaces: [] },
      { host_id: 'host-b', url: 'http://b', name: 'Host B', status: 'offline', workspaces: [] },
    ]
    const onOpenConversation = vi.fn()
    const conversations = [{ id: 7, title: 'Local chat on A workspace', workspace: 'remote:host-a:/workspace', updated_at: '2025-01-01' }]
    const workspaces: WorkspaceRow[] = [{ id: 1, path: 'remote:host-a:/workspace', label: 'workspace', last_opened_at: null, exists: true, conversation_count: 1, owner_id: 'host-a', device_status: 'online' }]

    render(<DeviceGroups devices={devices} workspaces={workspaces} conversations={conversations} onChange={() => {}} onOpenConversation={onOpenConversation} adding={false} setAdding={() => {}} />)

    expect(await screen.findByText('same ID from A')).toBeTruthy()
    expect(await screen.findByText('same ID from B')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Local chat on A workspace/ }).textContent).toContain('local chat')
    expect(screen.getByText('same ID from B').textContent).toContain('cached · read-only')
    expect(onOpenConversation).not.toHaveBeenCalled()
  })
  it('keeps colliding conversation IDs owner-scoped and never selects the local chat', async () => {
    getRemoteDeviceMessages.mockImplementation(async (hostId: string, conversationId: string) => {
      expect(conversationId).toBe('7')
      return [row(`transcript from ${hostId}`)]
    })
    useAgent.setState({ conversationId: 7, messagesByConv: { '7': [{ id: 'local', role: 'user', content: 'local transcript' }] } })

    render(
      <>
        <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />
        <RemoteTranscriptDialog hostId="host-b" conversationId="7" title="B chat" deviceName="B" online={false} onClose={() => {}} />
      </>,
    )

    expect(await screen.findByText('transcript from host-a')).toBeTruthy()
    expect(await screen.findByText('transcript from host-b')).toBeTruthy()
    expect(screen.getByText(/cached transcript · read-only offline/)).toBeTruthy()
    expect(getRemoteDeviceMessages).toHaveBeenCalledWith('host-a', '7')
    expect(getRemoteDeviceMessages).toHaveBeenCalledWith('host-b', '7')
    expect(useAgent.getState().conversationId).toBe(7)
    expect(useAgent.getState().messagesByConv['7'][0].content).toBe('local transcript')
    expect(useRemoteConversations.getState().getTranscript('host-a', '7')?.[0].content).toBe('transcript from host-a')
    expect(useRemoteConversations.getState().getTranscript('host-b', '7')?.[0].content).toBe('transcript from host-b')
    expect(useRemoteConversations.getState().transcripts[remoteConversationKey('host-a', '7')]).toBeDefined()
    // #110: remote chats are no longer read-only — each dialog offers a composer.
    expect(screen.getAllByLabelText('Message this device chat')).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: 'Edit transcript' })).toHaveLength(2)
  })

  it('routes remote transcript images through that host owner proxy', async () => {
    getRemoteDeviceMessages.mockResolvedValue([row('image from host', 'captures/one.png')])
    const { container } = render(
      <RemoteTranscriptDialog hostId="image-host" conversationId="21" title="Images" deviceName="Image host" online onClose={() => {}} />,
    )

    await waitFor(() => expect(container.querySelector('img')).toBeTruthy())
    expect(container.querySelector('img')?.getAttribute('src')).toBe('/api/remote/devices/image-host/images/captures/one.png')
    expect(useAgent.getState().conversationId).toBe(7)
  })

  it('shows a recoverable message when the cached transcript cannot be loaded', async () => {
    getRemoteDeviceMessages.mockRejectedValue(new Error('503 unavailable'))
    render(
      <RemoteTranscriptDialog hostId="offline-host" conversationId="99" title="Offline chat" deviceName="Offline" online={false} onClose={() => {}} />,
    )

    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('Reconnect to this device'))
    getRemoteDeviceMessages.mockResolvedValue([row('cached after retry')])
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('cached after retry')).toBeTruthy()
  })
})

describe('remote turn composer (#110)', () => {
  const renderDialog = () =>
    render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )

  it('offers a composer for online remote chats and streams the turn into the transcript', async () => {
    getRemoteDeviceMessages.mockResolvedValue([row('earlier message')])
    streamRemoteTurn.mockImplementation(async (_host, _cid, message, _ws, onEvent) => {
      onEvent({ type: 'remote_turn_started' })
      onEvent({ type: 'text', text: 'working on it' })
      onEvent({ type: 'remote_turn_committed' })
      onEvent({ type: 'done' })
    })

    renderDialog()
    await screen.findByText('earlier message')

    const input = screen.getByLabelText('Message this device chat')
    fireEvent.change(input, { target: { value: 'run the tests' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await screen.findByText('run the tests')
    expect(streamRemoteTurn).toHaveBeenCalledWith(
      'host-a', '7', 'run the tests', expect.any(String), expect.any(Function), expect.anything(), expect.anything(),
    )
    await screen.findByText('working on it')
  })

  it('clears the input optimistically and restores it if the turn fails to start', async () => {
    getRemoteDeviceMessages.mockResolvedValue([row('earlier message')])
    streamRemoteTurn.mockRejectedValue(new Error('503 device offline'))

    renderDialog()
    await screen.findByText('earlier message')

    const input = screen.getByLabelText('Message this device chat')
    fireEvent.change(input, { target: { value: 'do a thing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))

    await waitFor(() => expect((screen.getByLabelText('Message this device chat') as HTMLTextAreaElement).value).toBe('do a thing'))
    expect(screen.getByRole('status').textContent).toContain('could not be sent')
  })
})
