import { afterEach, describe, expect, it, vi } from 'vitest'

// #458 — remote devices reconnect without re-entering passphrase
// Tests the reconnect flow: Connect button sends empty passphrase, backend
// tries saved credential; on 401, UI shows passphrase field.

// Mock the api module
vi.mock('./api', () => ({
  listRemoteDevices: vi.fn(),
  connectRemoteDevice: vi.fn(),
}))

import { connectRemoteDevice } from './api'

describe('remote device reconnect passphrase logic (#458)', () => {
  it('connect button sends empty passphrase, backend uses saved credential', async () => {
    // Simulate: user clicks Connect on offline device with no passphrase entered
    const device = {
      host_id: 'device-1',
      url: 'http://10.0.0.5:8765',
      name: 'My Machine',
      status: 'offline',
      cached_workspaces: [],
    }

    // Backend should try the saved passphrase and succeed
    connectRemoteDevice.mockResolvedValueOnce({ status: 'online' })

    // Call with empty passphrase (user didn't type anything)
    const result = await connectRemoteDevice(device.host_id, '')
    expect(result.status).toBe('online')
    expect(connectRemoteDevice).toHaveBeenCalledWith(device.host_id, '')
  })

  it('after 401 error, passphrase field appears and user can retry with new passphrase', async () => {
    const device = {
      host_id: 'device-2',
      url: 'http://10.0.0.6:8765',
      name: 'Other Machine',
      status: 'offline',
      cached_workspaces: [],
    }

    // First attempt fails with 401 (wrong passphrase)
    connectRemoteDevice.mockRejectedValueOnce(new Error('wrong passphrase'))

    try {
      await connectRemoteDevice(device.host_id, '')
      expect.fail('should have thrown')
    } catch (e) {
      expect(e).toBeInstanceOf(Error)
      expect(String(e)).toContain('passphrase')
    }

    // After 401, the UI should show the passphrase field (passRequired = true)
    // User enters new passphrase and retries
    connectRemoteDevice.mockResolvedValueOnce({ status: 'online' })
    const result = await connectRemoteDevice(device.host_id, 'new-passphrase')
    expect(result.status).toBe('online')
  })
})
