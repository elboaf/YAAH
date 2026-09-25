import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'

const fetchMock = vi.fn()

afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset() })

describe('remote conversation edit API contract', () => {
  it('uses owner-scoped local routes for edits, never exposing remote credentials', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await api('/api/remote/devices/host%2Fa/conversations/7/lease', { method: 'POST', body: JSON.stringify({ revision: 'r:1' }) })
    expect(fetchMock.mock.calls[0][0]).toBe('/api/remote/devices/host%2Fa/conversations/7/lease')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ revision: 'r:1' })
    expect(JSON.stringify(fetchMock.mock.calls[0][1].headers)).not.toContain('passphrase')
  })
})
