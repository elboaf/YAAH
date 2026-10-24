// #443: the remote narration dialect selector in Settings -> Make it talk.
// MOSS must show the language + ref-clip controls and hide the OpenAI-only
// model/key fields; the openai flavor must keep the original fields.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig: vi.fn(() =>
      Promise.resolve({
        providers: {},
        active_provider: '',
        ui_scale: 1,
        voice: {
          engine: 'local',
          cloud_endpoint: '',
          cloud_api_key: '',
          cloud_model: '',
          ptt_hotkey: '',
          say_emissions: true,
          say_in_chat: false,
          say_persona: 'neutral',
          tts_engine: 'remote',
          tts_endpoint: 'http://herp.local',
          tts_api_key: '',
          tts_model: 'kokoro',
          tts_voice: 'af_heart',
          tts_speed: 1,
          sounds_enabled: true,
          tts_remote_api: 'moss',
          tts_moss_lang: 'en',
          tts_moss_ref: 'mes',
        },
      }),
    ),
    updateConfig: vi.fn(() => Promise.resolve({ ok: true })),
    listAvailableModels: vi.fn(() => Promise.resolve({ providers: {} })),
    getProviders: vi.fn(() => Promise.resolve({})),
    transcribeStatus: vi.fn(() => Promise.resolve({ local_available: true, local_model: 'x' })),
    ttsStatus: vi.fn(() =>
      Promise.resolve({
        engine: 'remote',
        remote_api: 'moss',
        available: true,
        model_available: false,
        model: 'kokoro-multi-lang-v1_0',
        model_bytes: 0,
        voices: [],
        default_voice: 'af_heart',
        tts_enabled: false,
        tts_voice: 'mes',
        tts_speed: 1,
        downloading: false,
      }),
    ),
    ttsVoices: vi.fn(() => Promise.resolve(['mes', 'ardy'])),
    getResolvedContextWindow: vi.fn(() => Promise.resolve({ context_window: 100000 })),
  }
})

import { SettingsModal } from './components'
import { updateConfig } from './api'

afterEach(() => cleanup())

describe('#443 remote narration dialect', () => {
  it('hydrates the MOSS dialect and ref-clip picker, hiding OpenAI-only fields', async () => {
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(screen.getByRole('tab', { name: 'Make it talk' }))
    await waitFor(() => expect(screen.getByRole('radiogroup', { name: 'Remote narration dialect' })).toBeTruthy())
    expect(screen.getByRole('radio', { name: 'MOSS' }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('combobox', { name: 'MOSS language' })).toBeTruthy()
    expect(screen.getByLabelText('Read-aloud voice')).toBeTruthy()
    expect(screen.queryByRole('textbox', { name: 'Remote narration API key' })).toBeNull()
    expect(screen.queryByRole('textbox', { name: 'Remote narration model' })).toBeNull()
  })

  it('saves the selected dialect and MOSS fields', async () => {
    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(screen.getByRole('tab', { name: 'Make it talk' }))
    await waitFor(() => expect(screen.getByRole('radiogroup', { name: 'Remote narration dialect' })).toBeTruthy())
    fireEvent.click(screen.getByRole('radio', { name: 'OpenAI-compatible' }))
    fireEvent.click(screen.getByRole('button', { name: /^Save$/ }))
    await waitFor(() => expect(updateConfig).toHaveBeenCalled())
    const calls = (updateConfig as ReturnType<typeof vi.fn>).mock.calls
    const args = calls[calls.length - 1]?.[0] as {
      voice?: Record<string, unknown>
    }
    expect(args.voice?.tts_remote_api).toBe('openai')
    expect(args.voice?.tts_moss_lang).toBe('en')
    expect(args.voice?.tts_moss_ref).toBe('mes')
    expect(args.voice?.tts_voice).toBe('mes')
  })
})
