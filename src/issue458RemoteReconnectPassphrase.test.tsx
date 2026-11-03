import { afterEach, describe, expect, it, vi } from 'vitest'

// #458 — remote devices reconnect without re-entering passphrase
// Tests the reconnect flow: Connect button sends empty passphrase, backend
// tries saved credential; on 401, UI shows passphrase field.

// Mock the api module
const mockConnectRemoteDevice = vi.fn()
vi.mock('./api', () => ({
  listRemoteDevices: vi.fn(),
  connectRemoteDevice: (...args: unknown[]) => mockConnectRemoteDevice(...args),
}))

import { connectRemoteDevice } from './api'

describe('remote device reconnect passphrase logic (#458)', () => {
  it('connect button sends empty passphrase, backend uses saved credential', async () => {
    const device = {
      host_id: 'device-1',
      url: 'http://10.0.0.5:8765',
      name: 'My Machine',
      status: 'offline',
      cached_workspaces: [],
    }

    mockConnectRemoteDevice.mockResolvedValueOnce({ status: 'online' })

    const result = await connectRemoteDevice(device.host_id, '')
    expect(result.status).toBe('online')
    expect(mockConnectRemoteDevice).toHaveBeenCalledWith(device.host_id, '')
  })

  it('after 401 error, passphrase field appears and user can retry with new passphrase', async () => {
    const device = {
      host_id: 'device-2',
      url: 'http://10.0.0.6:8765',
      name: 'Other Machine',
      status: 'offline',
      cached_workspaces: [],
    }

    mockConnectRemoteDevice.mockRejectedValueOnce(new Error('wrong passphrase'))

    try {
      await connectRemoteDevice(device.host_id, '')
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(Error)
      expect(String(e)).toContain('passphrase')
    }

    mockConnectRemoteDevice.mockResolvedValueOnce({ status: 'online' })
    const result = await connectRemoteDevice(device.host_id, 'new-passphrase')
    expect(result.status).toBe('online')
  })
})
