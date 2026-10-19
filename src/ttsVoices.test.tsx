// #231 + #448 — remote TTS discovery: the api wrapper's contract
// (voices AND models), the RemoteVoiceField and RemoteModelField
// dropdown/free-text dualities.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ttsVoices, ttsTest } from './api'
import { RemoteVoiceField, RemoteModelField, effectiveRemoteModel } from './components'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import React from 'react'

const fetchMock = vi.fn()

afterEach(() => {
  vi.unstubAllGlobals()
  fetchMock.mockReset()
  cleanup()
})

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

describe('#231/#448 ttsVoices (discovery probe wrapper)', () => {
  it('POSTs the drafts to /api/tts/voices and returns voices + models', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ voices: ['af_heart'], models: ['moss-tts-realtime'] }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const out = await ttsVoices('http://box:8081', 'sk-draft')
    expect(out).toEqual({ voices: ['af_heart'], models: ['moss-tts-realtime'] })
    const [u, init] = fetchMock.mock.calls[0]
    expect(u).toBe('/api/tts/voices')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ endpoint: 'http://box:8081', api_key: 'sk-draft' })
  })

  it('resolves to empty lists on 409 not-configured — no error, free-text fallback', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'no endpoint configured', code: 'not-configured' }, 409))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsVoices('http://box:8081')).resolves.toEqual({ voices: [], models: [] })
  })

  it('resolves to empty lists when the backend is unreachable (never throws)', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsVoices('http://box:8081')).resolves.toEqual({ voices: [], models: [] })
  })

  it('#449: ttsTest forwards the on-screen model draft', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsTest('af_heart', 1.0, 'remote', 'moss-tts-realtime')).resolves.toEqual({
      ok: true,
    })
    const [, init] = fetchMock.mock.calls[0]
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({
      voice: 'af_heart',
      speed: 1.0,
      engine: 'remote',
      model: 'moss-tts-realtime',
    })
  })

  it('tolerates a pre-#448 backend answering voices only', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ voices: ['af_heart'] }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsVoices('http://box:8081')).resolves.toEqual({ voices: ['af_heart'], models: [] })
  })
})

describe('#231 RemoteVoiceField', () => {
  it('renders free-text input while probing with no list', () => {
    render(<RemoteVoiceField voiceDraft="af_heart" voices={[]} probing onChange={() => {}} />)
    const input = screen.getByLabelText('Read-aloud voice') as HTMLInputElement
    expect(input.tagName).toBe('INPUT')
    expect(input.placeholder).toMatch(/probing voices/i)
  })

  it('renders a dropdown from the server list and selects the saved voice', () => {
    render(
      <RemoteVoiceField
        voiceDraft="af_nicole"
        voices={['af_heart', 'af_nicole', 'zm_yunyang']}
        probing={false}
        onChange={() => {}}
      />,
    )
    const sel = screen.getByLabelText('Read-aloud voice') as HTMLSelectElement
    expect(sel.tagName).toBe('SELECT')
    expect(sel.value).toBe('af_nicole')
    expect(sel.options.length).toBe(3)
  })

  it('keeps a saved voice selectable even when the server no longer offers it', () => {
    render(
      <RemoteVoiceField voiceDraft="zz_legacy" voices={['af_heart', 'af_nicole']} probing={false} onChange={() => {}} />,
    )
    const sel = screen.getByLabelText('Read-aloud voice') as HTMLSelectElement
    expect(sel.options[0].textContent).toBe('zz_legacy (saved)')
    expect(sel.value).toBe('zz_legacy')
  })

  it('falls back to the first server voice when the draft is empty', () => {
    render(
      <RemoteVoiceField voiceDraft="" voices={['af_heart', 'af_nicole']} probing={false} onChange={() => {}} />,
    )
    expect((screen.getByLabelText('Read-aloud voice') as HTMLSelectElement).value).toBe('af_heart')
  })

  it('emits the picked voice through onChange', () => {
    let picked = ''
    render(
      <RemoteVoiceField voiceDraft="af_heart" voices={['af_heart', 'af_nicole']} probing={false} onChange={(v) => (picked = v)} />,
    )
    fireEvent.change(screen.getByLabelText('Read-aloud voice'), { target: { value: 'af_nicole' } })
    expect(picked).toBe('af_nicole')
  })

  it('falls back to free-text when the list is empty after a probe', () => {
    render(<RemoteVoiceField voiceDraft="af_heart" voices={[]} probing={false} onChange={() => {}} />)
    const input = screen.getByLabelText('Read-aloud voice') as HTMLInputElement
    expect(input.tagName).toBe('INPUT')
    expect(input.value).toBe('af_heart')
  })
})

describe('#448 RemoteModelField', () => {
  it('renders free-text while probing with no model list (voices-only servers)', () => {
    render(<RemoteModelField modelDraft="kokoro" models={[]} probing onChange={() => {}} />)
    const input = screen.getByLabelText('Remote narration model') as HTMLInputElement
    expect(input.tagName).toBe('INPUT')
    expect(input.placeholder).toMatch(/probing models/i)
    expect(input.value).toBe('kokoro')
  })

  it('renders a dropdown from /v1/models and selects the listed draft', () => {
    render(
      <RemoteModelField
        modelDraft="moss-tts-realtime"
        models={['moss-tts-realtime']}
        probing={false}
        onChange={() => {}}
      />,
    )
    const sel = screen.getByLabelText('Remote narration model') as HTMLSelectElement
    expect(sel.tagName).toBe('SELECT')
    expect(sel.value).toBe('moss-tts-realtime')
  })

  it('keeps a saved model selectable when the server no longer lists it', () => {
    render(
      <RemoteModelField modelDraft="zz_old" models={['moss-tts-realtime']} probing={false} onChange={() => {}} />,
    )
    const sel = screen.getByLabelText('Remote narration model') as HTMLSelectElement
    expect(sel.options[0].textContent).toBe('zz_old (saved)')
    expect(sel.value).toBe('zz_old')
  })

  it('falls back to the first server model when the draft is empty', () => {
    render(
      <RemoteModelField modelDraft="" models={['moss-tts-realtime']} probing={false} onChange={() => {}} />,
    )
    expect((screen.getByLabelText('Remote narration model') as HTMLSelectElement).value).toBe(
      'moss-tts-realtime',
    )
  })

  it('emits the picked model through onChange', () => {
    let picked = ''
    render(
      <RemoteModelField modelDraft="a" models={['a', 'b']} probing={false} onChange={(v) => (picked = v)} />,
    )
    fireEvent.change(screen.getByLabelText('Remote narration model'), { target: { value: 'b' } })
    expect(picked).toBe('b')
  })

  it('falls back to free-text when the server lists no models', () => {
    render(<RemoteModelField modelDraft="kokoro" models={[]} probing={false} onChange={() => {}} />)
    const input = screen.getByLabelText('Remote narration model') as HTMLInputElement
    expect(input.tagName).toBe('INPUT')
    expect(input.value).toBe('kokoro')
  })
})
