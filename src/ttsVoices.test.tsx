// #231 — remote TTS voice discovery: the api wrapper's contract and the
// RemoteVoiceField's dropdown/free-text duality.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ttsVoices } from './api'
import { RemoteVoiceField } from './components'
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

describe('#231 ttsVoices (discovery probe wrapper)', () => {
  it('POSTs the drafts to /api/tts/voices and returns the voice list', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ voices: ['af_heart', 'af_nicole'] }))
    vi.stubGlobal('fetch', fetchMock)
    const voices = await ttsVoices('http://box:8081', 'sk-draft')
    expect(voices).toEqual(['af_heart', 'af_nicole'])
    const [u, init] = fetchMock.mock.calls[0]
    expect(u).toBe('/api/tts/voices')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ endpoint: 'http://box:8081', api_key: 'sk-draft' })
  })

  it('resolves to [] on 409 not-configured — no error, free-text fallback', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'no endpoint configured', code: 'not-configured' }, 409))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsVoices('http://box:8081')).resolves.toEqual([])
  })

  it('resolves to [] when the backend is unreachable (never throws)', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    vi.stubGlobal('fetch', fetchMock)
    await expect(ttsVoices('http://box:8081')).resolves.toEqual([])
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
