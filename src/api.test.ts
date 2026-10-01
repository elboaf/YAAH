import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, streamRemoteTurn, ttsTest } from './api'

const fetchMock = vi.fn()

const ndjsonResponse = (events: unknown[]) =>
  new Response(events.map((e) => JSON.stringify(e) + '\n').join(''), {
    status: 200,
    headers: { 'content-type': 'application/x-ndjson' },
  })

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

describe('remote turn stream bridging (#110)', () => {
  it('posts to the owner-qualified device turn endpoint and parses NDJSON events', async () => {
    fetchMock.mockResolvedValue(ndjsonResponse([
      { type: 'remote_turn_started', owner_id: 'host-a', conversation_id: '7' },
      { type: 'model_call', provider: 'p', model: 'm' },
      { type: 'text', text: 'hello' },
      { type: 'remote_turn_committed' },
      { type: 'done' },
    ]))
    vi.stubGlobal('fetch', fetchMock)

    const events: unknown[] = []
    const modelCalls: unknown[] = []
    await streamRemoteTurn('host-a', '7', 'fix the bug', 'remote:host-a:/repo', (ev) => events.push(ev), undefined, (mc) => modelCalls.push(mc))

    const [calledUrl, init] = fetchMock.mock.calls[0]
    expect(calledUrl).toBe('/api/remote/devices/host-a/turns/7')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ message: 'fix the bug', workspace: 'remote:host-a:/repo' })
    expect(JSON.stringify(init.headers)).not.toContain('passphrase')
      expect((events as Array<{ type: string }>).map((e) => e.type)).toEqual([
      'remote_turn_started', 'model_call', 'text', 'remote_turn_committed', 'done',
    ])
    // Initial clear, one "waiting" readout when model_call arrives, then a
    // clear per subsequent event. (Chunk boundaries can reorder the final
    // clears relative to the events, so assert the multiset, not the order.)
    expect(modelCalls.filter(Boolean)).toEqual([
      { provider: 'p', model: 'm', startedAt: expect.any(Number) },
    ])
    expect(modelCalls.filter((mc) => mc === null)).toHaveLength(modelCalls.length - 1)
  })

  it('surfaces HTTP failures from the turn endpoint', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: 'a turn is already running in this conversation' }), { status: 409 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(streamRemoteTurn('host-a', '7', 'go', 'remote:host-a:/repo', () => {}))
      .rejects.toThrow(/409.*already running/s)
  })
})

// ------------------------------------------------------------------ #205 remote TTS

describe('#205 ttsTest (Settings Test button)', () => {
  it('POSTs the sample to /api/tts/test and reports ok', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const r = await ttsTest('af_heart', 1.0)
    expect(r.ok).toBe(true)
    const [u, init] = fetchMock.mock.calls[0]
    expect(u).toBe('/api/tts/test')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ voice: 'af_heart', speed: 1.0, engine: undefined })
  })

  it('carries the on-screen engine draft so Test reflects unsaved edits', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await ttsTest('af_heart', 1.0, 'remote')
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ voice: 'af_heart', speed: 1.0, engine: 'remote' })
  })

  it('surfaces the backend detail on failure (502 remote error, 400 validation)', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: 'HTTP 401: Incorrect API key' }), { status: 502 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsTest('af_heart', 1.0)).rejects.toThrow('Incorrect API key')
    fetchMock.mockResolvedValue(new Response(JSON.stringify({ detail: 'text must not be empty' }), { status: 400 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsTest('af_heart', 1.0)).rejects.toThrow('text must not be empty')
  })
})
