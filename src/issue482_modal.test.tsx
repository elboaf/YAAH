// Issue #482 (owner-reopened): open a NEW remote chat, send a message, the
// running dots on the sidebar row must show while the modal is streaming.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, act, fireEvent } from '@testing-library/react'

const { getRemoteDeviceMessages, listRemoteDeviceConversations, streamRemoteTurn } = vi.hoisted(() => ({
  getRemoteDeviceMessages: vi.fn(), listRemoteDeviceConversations: vi.fn(), streamRemoteTurn: vi.fn(),
}))
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  getRemoteDeviceMessages, listRemoteDeviceConversations, streamRemoteTurn,
}))
import { DeviceGroups } from './components'
import { useAgent } from './store'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'
import type { RemoteDevice, WorkspaceRow } from './api'

const KEY = remoteConversationKey('host-a', '7')
const device: RemoteDevice = { host_id: 'host-a', url: 'http://a', name: 'Host A', status: 'online', workspaces: [] as WorkspaceRow[] }

beforeEach(() => {
  getRemoteDeviceMessages.mockResolvedValue([{ id: 1, role: 'user' as const, content: 'earlier', images: [], tool_call_id: null, tool_calls: null, sub_agent_transcript: null }])
  listRemoteDeviceConversations.mockResolvedValue({ status: 'online', conversations: [{ host_id: 'host-a', conversation_id: '7', title: 'device chat', workspace: null, updated_at: '2025-01-01', revision: '', sync_status: 'synced' }] })
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {}, pendingQuestions: {} })
  useRemoteConversations.setState({ transcripts: {} })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {}, pendingQuestions: {} }); useRemoteConversations.setState({ transcripts: {} }) })

describe('issue #482: modal streaming populates sidebar row dots', () => {
  it('the sidebar row shows dots while the remote modal streams', async () => {
    render(<DeviceGroups devices={[device]} workspaces={[]} conversations={[]} onChange={() => {}} onOpenConversation={() => {}} adding={false} setAdding={() => {}} />)
    await screen.findByText('device chat')
    fireEvent.click(screen.getByText(/device chat/))
    await screen.findByText('earlier')
    expect(useAgent.getState().activeRemoteKey).toBe(KEY)

    let deliverEvent: ((ev: { type: string }) => void) | null = null
    streamRemoteTurn.mockImplementation(((_h: string, _c: string, _m: string, _w: string, onEvent: (ev: { type: string }) => void) => {
      deliverEvent = onEvent
      return new Promise(() => {})
    }))
    fireEvent.change(screen.getByLabelText('Message this device chat'), { target: { value: 'go' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('thinking'), { timeout: 3000 })
    // Dots must show on the sidebar row even while the modal is open.
    await waitFor(() => expect(document.querySelector('.run-dots')).not.toBeNull(), { timeout: 3000 })
  }, 20000)
})
