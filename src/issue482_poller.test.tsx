// Issue #482: offscreen poller path. Chat 7 runs on remote device, this device
// only views it in the sidebar (no modal). The poller writes statusByConv[remoteKey]
// and the sidebar row must render .run-dots.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor, act } from '@testing-library/react'

const { listRemoteDeviceConversations, listRemoteDevices, getRemoteDeviceRunning } = vi.hoisted(() => ({
  listRemoteDeviceConversations: vi.fn(), listRemoteDevices: vi.fn(), getRemoteDeviceRunning: vi.fn(),
}))
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  listRemoteDeviceConversations, listRemoteDevices, getRemoteDeviceRunning,
}))
import { DeviceGroups, RemoteRunWatcher } from './components'
import { useAgent } from './store'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'
import type { RemoteDevice } from './api'

const KEY = remoteConversationKey('host-a', '7')
const device: RemoteDevice = { host_id: 'host-a', url: 'http://a', name: 'Host A', status: 'online', workspaces: [] }
const conv = { host_id: 'host-a', conversation_id: '7', title: 'device chat', workspace: null, updated_at: '2025-01-01', revision: '', sync_status: 'synced' }
const wait = (ms: number) => new Promise(r => setTimeout(r, ms))

beforeEach(() => {
  listRemoteDeviceConversations.mockResolvedValue({ status: 'online', conversations: [conv] })
  listRemoteDevices.mockResolvedValue({ devices: [device] })
  getRemoteDeviceRunning.mockResolvedValue({ running: ['7'], pending: [] })
  useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {}, pendingQuestions: {} })
  useRemoteConversations.setState({ transcripts: {} })
})
afterEach(() => { cleanup(); vi.clearAllMocks(); useAgent.setState({ statusByConv: {}, finishedByConv: {}, activeRemoteKey: null, conversationId: null, messagesByConv: {}, pendingQuestions: {} }); useRemoteConversations.setState({ transcripts: {} }) })

describe('issue #482: offscreen poller path', () => {
  it('poller writes offscreen running status and sidebar renders dots', async () => {
    render(
      <>
        <RemoteRunWatcher />
        <DeviceGroups devices={[device]} workspaces={[]} conversations={[]} onChange={() => {}} onOpenConversation={() => {}} adding={false} setAdding={() => {}} />
      </>,
    )
    await screen.findByText('device chat')
    expect(useAgent.getState().activeRemoteKey).toBeNull()
    await waitFor(() => expect(useAgent.getState().statusByConv[KEY]).toBe('running-tool'), { timeout: 6000 })
    await waitFor(() => expect(document.querySelector('.run-dots')).not.toBeNull(), { timeout: 2000 })
  }, 20000)
})
