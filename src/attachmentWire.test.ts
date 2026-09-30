import { afterEach, describe, expect, it, vi } from 'vitest'
import { queueMessage, streamAgentTurn } from './api'

const fetchMock = vi.fn()

afterEach(() => { vi.unstubAllGlobals(); fetchMock.mockReset() })

const okJson = (body: unknown) =>
  new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })

const sseBody = () => {
  const lines = [JSON.stringify({ type: 'done' })].join('\n') + '\n'
  return new ReadableStream({
    start(c) { c.enqueue(new TextEncoder().encode(lines)); c.close() },
  })
}

describe('structured attachments on the wire (#142)', () => {
  it('streamAgentTurn sends the attachments array alongside images', async () => {
    fetchMock.mockImplementation(async (_u: unknown, init?: RequestInit) => {
      expect(String(_u)).toMatch(/\/api\/agent\/7$/)
      return new Response(sseBody(), { status: 200 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const records = [{ name: 'a.txt', size: 3, content: 'abc' }]
    await streamAgentTurn(7, 'hello', '.', () => {}, undefined, [], [], false, undefined, records)
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body))
    expect(body.attachments).toEqual(records)
    expect(body.message).toBe('hello')
  })

  it('queueMessage sends the attachments array', async () => {
    fetchMock.mockImplementation(async (_u: unknown, init?: RequestInit) => {
      expect(String(_u)).toMatch(/\/api\/agent\/7\/queue$/)
      return okJson({ ok: true, item: { id: 1, text: 'look', skills: [], images: [] } })
    })
    vi.stubGlobal('fetch', fetchMock)
    const records = [{ name: 'big.log', size: 204_800, path: '.yaah-attachments/big.log' }]
    await queueMessage(7, 'look', [], [], records)
    const body = JSON.parse(String(fetchMock.mock.calls[0][1].body))
    expect(body.attachments).toEqual(records)
  })
})
