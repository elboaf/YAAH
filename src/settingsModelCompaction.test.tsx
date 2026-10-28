// Regression tests for the per-model compaction editor inside SettingsModal.
// Reproduces the owner report: removing a model entry and saving brought it
// back, edited fold (trigger) values did not stick, and entries for models
// that no longer exist stayed stuck in the panel.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const {
  getConfig,
  updateConfig,
  getProviders,
  listAvailableModels,
  transcribeStatus,
  ttsStatus,
  getResolvedContextWindow,
  getCompactionEffective,
} = vi.hoisted(() => ({
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
  getProviders: vi.fn(),
  listAvailableModels: vi.fn(),
  transcribeStatus: vi.fn(),
  ttsStatus: vi.fn(),
  getResolvedContextWindow: vi.fn(),
  getCompactionEffective: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig,
    updateConfig,
    getProviders,
    listAvailableModels,
    transcribeStatus,
    ttsStatus,
    getResolvedContextWindow,
    getCompactionEffective,
  }
})

import { SettingsModal } from './components'
import { useAgent } from './store'

const TIEL = 'x2.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S'
const QWEN = 'x2.qwen36-reap50-Q4_K_M-imat'
const TIEL1 = '1.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S'

/** The saved config the modal loads: one provider, two models configured,
 *  both with per-model compaction entries — as after the #588 fix shipped. */
function savedConfig() {
  return {
    providers: {
      local: { api_base: 'http://127.0.0.1:8080/v1', model: TIEL, api_key: 'set' },
    },
    model_compaction: {
      [TIEL]: { enabled: true, trigger_tokens: 24000 },
      [QWEN]: { enabled: true, trigger_tokens: 15000 },
    },
  }
}

/** Stateful backend stand-in: getConfig serves the stored config;
 *  updateConfig merges into it exactly like the real endpoint does for
 *  model_compaction (the incoming map IS the full map). */
let stored: ReturnType<typeof savedConfig>

/** Expand the provider row and wait for its per-model entries to render. */
async function openProviderEditor() {
  fireEvent.click(await screen.findByRole('button', { name: 'Toggle local settings' }))
  await screen.findByLabelText(`Compaction trigger for ${TIEL}`)
}

beforeEach(() => {
  stored = savedConfig()
  getConfig.mockImplementation(() => Promise.resolve(stored))
  updateConfig.mockImplementation((patch: Record<string, unknown>) => {
    stored = { ...stored, ...patch }
    return Promise.resolve({ ok: true })
  })
  getProviders.mockResolvedValue({})
  listAvailableModels.mockResolvedValue({ providers: { local: { models: [TIEL, QWEN, TIEL1] } } })
  transcribeStatus.mockResolvedValue({ local_available: false, local_model: null })
  ttsStatus.mockResolvedValue({ model_available: true, downloading: false, kokoro: true })
  getResolvedContextWindow.mockResolvedValue({ context_window: 131072 })
  getCompactionEffective.mockResolvedValue({
    model: TIEL,
    entry: true,
    entry_key: TIEL,
    enabled: true,
    trigger_tokens: 24000,
    keep_tokens: 9600,
    window: 131072,
  })
  useAgent.setState({ errorByConv: {}, finishedByConv: {} })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

async function saveSettings() {
  const save = await screen.findByRole('button', { name: 'Save' })
  await waitFor(() => expect(save).not.toBeDisabled())
  fireEvent.click(save)
  await waitFor(() => expect(updateConfig).toHaveBeenCalled())
}

describe('per-model compaction editor: removal, persistence, phantoms', () => {
  it('a removed entry stays removed after Save (it must not be resurrected)', async () => {
    render(<SettingsModal onClose={() => {}} />)
    await openProviderEditor()
    fireEvent.click(screen.getByRole('button', { name: `remove model entry ${QWEN}` }))
    await saveSettings()
    const sent = updateConfig.mock.calls[0][0]
    expect(sent.model_compaction).toEqual({ [TIEL]: { enabled: true, trigger_tokens: 24000 } })
  })

  it('an edited fold value sticks through Save', async () => {
    render(<SettingsModal onClose={() => {}} />)
    await openProviderEditor()
    fireEvent.change(screen.getByLabelText(`Compaction trigger for ${TIEL}`), {
      target: { value: '32' },
    })
    await saveSettings()
    const sent = updateConfig.mock.calls[0][0]
    expect(sent.model_compaction[TIEL]).toEqual({ enabled: true, trigger_tokens: 32000 })
  })

  it('adding an entry and saving writes it (the add must persist too)', async () => {
    render(<SettingsModal onClose={() => {}} />)
    await openProviderEditor()
    fireEvent.click(screen.getByRole('button', { name: /add a model to configure/ }))
    fireEvent.change(screen.getByLabelText('model to add for local'), { target: { value: TIEL1 } })
    fireEvent.change(screen.getByLabelText(`Compaction trigger for ${TIEL1}`), {
      target: { value: '18' },
    })
    await saveSettings()
    const sent = updateConfig.mock.calls[0][0]
    expect(sent.model_compaction[TIEL1].trigger_tokens).toBe(18000)
    expect(sent.model_compaction[TIEL].trigger_tokens).toBe(24000)
    expect(sent.model_compaction[QWEN].trigger_tokens).toBe(15000)
  })

  it('reopening Settings after a saved removal does not bring the entry back', async () => {
    const { unmount } = render(<SettingsModal onClose={() => {}} />)
    await openProviderEditor()
    fireEvent.click(screen.getByRole('button', { name: `remove model entry ${QWEN}` }))
    await saveSettings()
    unmount()
    cleanup()

    render(<SettingsModal onClose={() => {}} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Toggle local settings' }))
    await screen.findByLabelText(`Compaction trigger for ${TIEL}`)
    expect(screen.queryByLabelText(`Compaction trigger for ${QWEN}`)).toBeNull()
  })
})
