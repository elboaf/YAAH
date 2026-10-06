// Issue #308: remote-device conversation rows never show sidebar liveness
// signals. Remote status writes land under the `remote:<host>:<conv>` key
// while the sidebar read only numeric keys, and the remote turn path wrote
// statusByConv directly (bypassing setStatus), so the issue #25 finish
// signal could never fire for a remote turn.
//
// Key discipline (AC 4): ONE approach — the shared `setStatus` setter is made
// key-shape-agnostic. Remote turn writes flow through `setStatus`, the store
// tracks the on-screen remote chat via `activeRemoteKey` (the mirror of
// `conversationId` for `remote:` keys), and the finish-signal rule applies
// uniformly. Sidebar row liveness then just reads both key shapes.
// Run: npx vitest run src/issue308RemoteSidebarLiveness.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getRemoteDeviceMessages, listRemoteDeviceConversations, streamRemoteTurn } = vi.hoisted(() => ({
  getRemoteDeviceMessages: vi.fn(),
  listRemoteDeviceConversations: vi.fn(),
  streamRemoteTurn: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getRemoteDeviceMessages,
  listRemoteDeviceConversations,
  streamRemoteTurn,
}))

import { DeviceGroups, RemoteTranscriptDialog } from './components'
import { useAgent } from './store'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'
import type { RemoteDevice, WorkspaceRow } from './api'

const KEY = remoteConversationKey('host-a', '7')

const row = (content: string) => ({
  id: 1,
  role: 'user' as const,
  content,
  images: [],
  tool_call_id: null,
  tool_calls: null,
  sub_agent_transcript: null,
})

const device: RemoteDevice = { host_id: 'host-a', url: 'http://a', name: 'Host A', status: 'online', workspaces: [] }
const workspaces: WorkspaceRow[] = []

beforeEach(() => {
  getRemoteDeviceMessages.mockResolvedValue([row('earlier message')])
  listRemoteDeviceConversations.mockResolvedValue({ status: 'online', conversations: [] })
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {} })
  useRemoteConversations.setState({ transcripts: {} })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {} })
  useRemoteConversations.setState({ transcripts: {} })
})

describe('store: key-shape-agnostic setStatus (#308)', () => {
  it('a remote turn that ends off-screen leaves an ok signal under its remote key', () => {
    useAgent.setState({ statusByConv: { [KEY]: 'running-tool' }, activeRemoteKey: null })
    useAgent.getState().setStatus(KEY, 'idle')
    expect(useAgent.getState().finishedByConv[KEY]).toBe('ok')
  })

  it('a remote turn that fails off-screen leaves an error signal', () => {
    useAgent.setState({ statusByConv: { [KEY]: 'thinking' }, activeRemoteKey: null })
    useAgent.getState().setStatus(KEY, 'error')
    expect(useAgent.getState().finishedByConv[KEY]).toBe('error')
  })

  it('a remote turn watched in its own chat does not signal (Q4/Q8)', () => {
    useAgent.setState({ statusByConv: { [KEY]: 'thinking' }, activeRemoteKey: KEY })
    useAgent.getState().setStatus(KEY, 'idle')
    expect(useAgent.getState().finishedByConv[KEY]).toBeUndefined()
  })

  it('opening the remote chat clears its stale signal', () => {
    useAgent.setState({ statusByConv: { [KEY]: 'idle' }, finishedByConv: { [KEY]: 'ok' }, activeRemoteKey: null })
    useAgent.getState().setActiveRemoteKey(KEY)
    expect(useAgent.getState().finishedByConv[KEY]).toBeUndefined()
  })

  it('numeric-key behavior is unchanged (local rows never signal while on screen)', () => {
    useAgent.setState({ statusByConv: { '7': 'thinking' }, conversationId: 7, activeRemoteKey: KEY })
    useAgent.getState().setStatus('7', 'idle')
    expect(useAgent.getState().finishedByConv['7']).toBeUndefined()
  })
})

describe('sidebar: remote rows show liveness signals (#308)', () => {
  const renderSidebar = () =>
    render(
      <DeviceGroups
        devices={[device]}
        workspaces={workspaces}
        conversations={[]}
        onChange={() => {}}
        onOpenConversation={() => {}}
        adding={false}
        setAdding={() => {}}
      />,
    )

  const mountChatRow = async () => {
    listRemoteDeviceConversations.mockResolvedValue({
      status: 'online',
      conversations: [{ host_id: 'host-a', conversation_id: '7', title: 'device chat', workspace: null, updated_at: '2025-01-01', revision: '', sync_status: 'synced' }],
    })
    renderSidebar()
    await screen.findByText('device chat')
  }

  it('shows the running dots while a remote turn is thinking', async () => {
    await mountChatRow()
    expect(document.querySelector('.run-dots')).toBeNull()
    act(() => {
      useAgent.setState({ statusByConv: { [KEY]: 'thinking' } })
    })
    await waitFor(() => expect(document.querySelector('.run-dots')).not.toBeNull())
  })

  it('shows the red pill for a finished-with-error remote turn', async () => {
    await mountChatRow()
    act(() => {
      useAgent.setState({ finishedByConv: { [KEY]: 'error' } })
    })
    await waitFor(() => expect(document.querySelector('.run-bar-red')).not.toBeNull())
  })

  it('shows the green bar for a finished-ok remote turn', async () => {
    await mountChatRow()
    act(() => {
      useAgent.setState({ finishedByConv: { [KEY]: 'ok' } })
    })
    await waitFor(() => expect(document.querySelector('.run-bar-green')).not.toBeNull())
  })
})

describe('remote turn path (#308): status flows through setStatus', () => {
  it('a turn that ends after its chat closes leaves the finished signal', async () => {
    let finishTurn: () => void = () => {}
    streamRemoteTurn.mockImplementation(
      () => new Promise<void>((resolve) => { finishTurn = resolve }),
    )
    const { unmount } = render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )
    // On-screen while mounted: the store knows this remote chat is open.
    await screen.findByText('earlier message')
    expect(useAgent.getState().activeRemoteKey).toBe(KEY)

    fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'run it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('thinking'))

    // Switch away: dialog closes, its stream keeps running in the background.
    unmount()
    expect(useAgent.getState().activeRemoteKey).toBeNull()

    await act(async () => {
      finishTurn()
    })
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('idle'))
    expect(useAgent.getState().finishedByConv[KEY]).toBe('ok')
  })

  it('a turn watched in its own chat does not signal', async () => {
    streamRemoteTurn.mockImplementation(async (_h: string, _c: string, _m: string, _w: string, onEvent: (ev: { type: string }) => void) => {
      onEvent({ type: 'done' })
    })
    render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )
    await screen.findByText('earlier message')
    fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'run it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('idle'))
    expect(useAgent.getState().finishedByConv[KEY]).toBeUndefined()
  })

  it('a mid-stream failure keeps the turn rows and leaves an error signal', async () => {
    streamRemoteTurn.mockImplementation(
      (_h: string, _c: string, _m: string, _w: string, onEvent: (ev: { type: string; text?: string }) => void) =>
        new Promise<void>((_resolve, reject) => {
          onEvent({ type: 'text', text: 'partial reply' })
          setTimeout(() => reject(new Error('stream dropped')), 10)
        }),
    )
    render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )
    await screen.findByText('earlier message')
    fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'run it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    // The failure lands mid-stream: the turn's rows stay, and the remote row
    // gets an error signal instead of a silent rollback to idle.
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('error'))
    const messages = useAgent.getState().messagesByConv[KEY] ?? []
    expect(messages.some((m) => m.role === 'assistant' && m.content.includes('partial reply'))).toBe(true)
    expect(useAgent.getState().finishedByConv[KEY]).toBeUndefined() // watched in its own chat
  })

  it('a rejected send (never started) still rolls back and does not signal', async () => {
    streamRemoteTurn.mockRejectedValue(new Error('offline'))
    render(
      <RemoteTranscriptDialog hostId="host-a" conversationId="7" title="A chat" deviceName="A" online onClose={() => {}} />,
    )
    await screen.findByText('earlier message')
    fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'run it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() =>
      expect((useAgent.getState().messagesByConv[KEY] ?? []).some((m) => m.role === 'user' && m.content === 'run it')).toBe(false),
    )
    expect(useAgent.getState().statusByConv[KEY]).toBe('idle')
    expect(useAgent.getState().finishedByConv[KEY]).toBeUndefined()
  })
})
