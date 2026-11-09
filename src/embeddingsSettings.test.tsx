// Embeddings Settings card (issue #677): endpoint/model/key drafts, live
// Test probe, one-click Rebuild with progress, and the index status line.
// The API layer is mocked at the module boundary; rebuild progress rides
// the real polling loop (real timers, like the #398 LoRA-notice tests).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const {
  getEmbeddingsStatus,
  getEmbeddingsRebuildProgress,
  probeEmbeddings,
  rebuildEmbeddings,
  dismissEmbeddingsRebuildProgress,
} = vi.hoisted(() => ({
  getEmbeddingsStatus: vi.fn(),
  getEmbeddingsRebuildProgress: vi.fn(),
  probeEmbeddings: vi.fn(),
  rebuildEmbeddings: vi.fn(),
  dismissEmbeddingsRebuildProgress: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getEmbeddingsStatus,
    getEmbeddingsRebuildProgress,
    probeEmbeddings,
    rebuildEmbeddings,
    dismissEmbeddingsRebuildProgress,
  }
})

import { EmbeddingsSettingsCard } from './components'

const draft = (over: Partial<{ api_base: string; api_key: string; model: string; key_saved: boolean }> = {}) => ({
  api_base: 'http://127.0.0.1:8099/v1',
  api_key: '',
  model: 'bge-m3',
  key_saved: false,
  ...over,
})

const statusReady = {
  configured: true,
  api_base: 'http://127.0.0.1:8099/v1',
  api_key: 'set',
  model: 'bge-m3',
  index: { model: 'bge-m3', dimensions: 1024, last_build: 1, vectors: 42 },
  readiness: 'ready' as const,
  available: null,
}

beforeEach(() => {
  getEmbeddingsStatus.mockReset()
  getEmbeddingsRebuildProgress.mockReset()
  probeEmbeddings.mockReset()
  rebuildEmbeddings.mockReset()
  dismissEmbeddingsRebuildProgress.mockReset()
  getEmbeddingsStatus.mockResolvedValue(statusReady)
  getEmbeddingsRebuildProgress.mockResolvedValue({ running: false, phase: '', percent: 0, message: '' })
})

afterEach(() => cleanup())

describe('Embeddings settings card (#677)', () => {
  it('renders the saved endpoint and model, and the index status line', async () => {
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    expect((screen.getByLabelText('Embeddings endpoint') as HTMLInputElement).value).toBe(
      'http://127.0.0.1:8099/v1',
    )
    expect((screen.getByLabelText('Embeddings model') as HTMLInputElement).value).toBe('bge-m3')
    // Masked key renders as the "key saved" placeholder, never a value.
    expect((screen.getByLabelText('Embeddings API key') as HTMLInputElement).placeholder).not.toBe(
      'set',
    )
    await waitFor(() => expect(screen.getByText(/42 vectors/)).toBeTruthy())
    expect(screen.getByText(/built with bge-m3 \(1024 dims\)/)).toBeTruthy()
  })

  it('marks a configured-model mismatch and tells the user to rebuild', async () => {
    getEmbeddingsStatus.mockResolvedValue({
      ...statusReady,
      model: 'bge-new',
      readiness: 'mismatch',
    })
    render(<EmbeddingsSettingsCard draft={draft({ model: 'bge-new' })} onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText(/differs from the index/)).toBeTruthy())
  })

  it('keeps Test and Rebuild disabled while the endpoint or model is blank', () => {
    render(
      <EmbeddingsSettingsCard
        draft={draft({ api_base: '', model: '' })}
        onChange={() => {}}
      />,
    )
    expect((screen.getByRole('button', { name: 'Test' }) as HTMLButtonElement).disabled).toBe(true)
    expect(
      (screen.getByRole('button', { name: 'Rebuild index' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(screen.getByText(/semantic search off/)).toBeTruthy()
  })

  it('probes the endpoint and reports what came back', async () => {
    probeEmbeddings.mockResolvedValue({ configured: true, available: true, dimensions: 1024, model: 'bge-m3' })
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Test' }))
    await waitFor(() =>
      expect(screen.getByText(/ok - endpoint reachable \(1024 dims, model bge-m3\)/)).toBeTruthy(),
    )
    expect(probeEmbeddings).toHaveBeenCalledTimes(1)
  })

  it('surfaces a failed probe as an error, not a success', async () => {
    probeEmbeddings.mockResolvedValue({ configured: true, available: false, error: 'endpoint unreachable: http://x' })
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Test' }))
    await waitFor(() => expect(screen.getByText(/endpoint unreachable/)).toBeTruthy())
  })

  it('rebuilds on click and shows the build summary with progress', async () => {
    rebuildEmbeddings.mockResolvedValue({
      ok: true, vectors_stored: 120, chunks_embedded: 120, total_chunks: 120,
      model: 'bge-m3', dimensions: 1024,
    })
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    const button = screen.getByRole('button', { name: 'Rebuild index' })
    expect((button as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(button)
    // Busy label while the request is in flight...
    await waitFor(() => expect(screen.getByRole('button', { name: 'Rebuilding…' })).toBeTruthy())
    // ...then the final summary replaces the spinner.
    await waitFor(() =>
      expect(screen.getByText(/Index built: 120 vectors \(bge-m3, 1024 dims\)/)).toBeTruthy(),
    )
    // The index stats refreshed after the build.
    await waitFor(() => expect(screen.getByText(/42 vectors/)).toBeTruthy())
  })

  it('shows a rebuild that failed with errors, not a fake success', async () => {
    rebuildEmbeddings.mockResolvedValue({
      ok: false, vectors_stored: 0, error: 'embeddings endpoint unreachable: http://x',
      model: 'bge-m3', dimensions: null,
    })
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Rebuild index' }))
    await waitFor(() => expect(screen.getByText(/Build finished with errors/)).toBeTruthy())
  })

  it('reports a rejected rebuild (in-flight 409) as an error line', async () => {
    rebuildEmbeddings.mockRejectedValue(new Error('an index rebuild is already running'))
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'Rebuild index' }))
    await waitFor(() =>
      expect(screen.getByText(/an index rebuild is already running/)).toBeTruthy(),
    )
    expect(screen.queryByText(/Index built:/)).toBeNull()
  })

  it('polls a rebuild started elsewhere and shows its progress', async () => {
    getEmbeddingsRebuildProgress
      .mockResolvedValueOnce({ running: true, phase: 'embedding', percent: 40, message: 'Embedding 10/25 chunks' })
      .mockResolvedValueOnce({ running: true, phase: 'embedding', percent: 80, message: 'Embedding 20/25 chunks' })
      .mockResolvedValue({ running: false, phase: 'done', percent: 100, message: 'ok: 25 vectors stored' })
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    // The in-flight build appears without the user clicking anything.
    await waitFor(() => expect(screen.getByText(/Embedding 10\/25 chunks/)).toBeTruthy(), {
      timeout: 4000,
    })
    // Polling follows it to done (1s interval, real timers).
    await waitFor(() => expect(screen.getByText(/ok: 25 vectors stored/)).toBeTruthy(), {
      timeout: 4000,
    })
    expect(screen.getByRole('button', { name: 'Dismiss rebuild status' })).toBeTruthy()
  })

  it('dismisses the finished summary and clears it server-side', async () => {
    getEmbeddingsRebuildProgress.mockResolvedValue({
      running: false, phase: 'done', percent: 100, message: 'ok: 25 vectors stored',
    })
    dismissEmbeddingsRebuildProgress.mockResolvedValue({ ok: true })
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText(/ok: 25 vectors stored/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss rebuild status' }))
    await waitFor(() =>
      expect(screen.queryByText(/ok: 25 vectors stored/)).toBeNull(),
    )
    expect(dismissEmbeddingsRebuildProgress).toHaveBeenCalledTimes(1)
  })

  it('keeps working when the status endpoints are unavailable (old backend)', async () => {
    getEmbeddingsStatus.mockRejectedValue(new Error('404'))
    getEmbeddingsRebuildProgress.mockRejectedValue(new Error('404'))
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    // No status line, no crash; the draft fields remain editable.
    expect((screen.getByLabelText('Embeddings endpoint') as HTMLInputElement).value).toBe(
      'http://127.0.0.1:8099/v1',
    )
    expect(screen.queryByText(/vectors/)).toBeNull()
  })
})

describe('Embeddings settings card: draft vs saved gating', () => {
  it('disables Test/Rebuild while the draft differs from the saved config', async () => {
    // Draft points at a different model than the saved one: the endpoints
    // read the SAVED config, so acting on the unsaved draft would rebuild
    // with the old model (after clearing the vectors).
    render(
      <EmbeddingsSettingsCard
        draft={draft({ model: 'bge-new' })}
        onChange={() => {}}
      />,
    )
    await waitFor(() => expect(screen.getByRole('status')).toBeTruthy())
    expect(screen.getByText(/Save first/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Test' }) as HTMLButtonElement).disabled).toBe(true)
    expect(
      (screen.getByRole('button', { name: 'Rebuild index' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('keeps Test/Rebuild enabled when the draft matches the saved config', async () => {
    render(<EmbeddingsSettingsCard draft={draft()} onChange={() => {}} />)
    await waitFor(() => expect(screen.getByText(/42 vectors/)).toBeTruthy())
    expect(screen.queryByText(/Save first/)).toBeNull()
    expect((screen.getByRole('button', { name: 'Test' }) as HTMLButtonElement).disabled).toBe(false)
    expect(
      (screen.getByRole('button', { name: 'Rebuild index' }) as HTMLButtonElement).disabled,
    ).toBe(false)
  })
})
