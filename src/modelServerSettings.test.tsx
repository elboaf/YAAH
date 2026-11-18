// Local model server settings card (issue #730): status row, start/stop,
// download action, model picker. API layer is mocked at the module boundary.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const {
  getModelServerStatus,
  getModelServerTaskStatus,
  getModelServerModels,
  startModelServer,
  startModelServerTask,
  stopModelServer,
  stopModelServerTask,
  setServerGpuOffload,
  setServerTaskGpuOffload,
  setServerGpuIndex,
  setServerTaskGpuIndex,
  downloadModelServer,
  downloadModelServerTask,
} = vi.hoisted(() => ({
  getModelServerStatus: vi.fn(),
  getModelServerTaskStatus: vi.fn(),
  getModelServerModels: vi.fn(),
  startModelServer: vi.fn(),
  startModelServerTask: vi.fn(),
  stopModelServer: vi.fn(),
  stopModelServerTask: vi.fn(),
  setServerGpuOffload: vi.fn(),
  setServerTaskGpuOffload: vi.fn(),
  setServerGpuIndex: vi.fn(),
  setServerTaskGpuIndex: vi.fn(),
  downloadModelServer: vi.fn(),
  downloadModelServerTask: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getModelServerStatus,
    getModelServerTaskStatus,
    getModelServerModels,
    startModelServer,
    startModelServerTask,
    stopModelServer,
    stopModelServerTask,
    setServerGpuOffload,
    setServerTaskGpuOffload,
    setServerGpuIndex,
    setServerTaskGpuIndex,
    downloadModelServer,
    downloadModelServerTask,
  }
})

import { ModelServerSettingsCard, RerankerServerSettingsCard } from './components'

const models = [
  { id: 'qwen3-embedding-0.6b', name: 'Qwen3-Embedding-0.6B (GGUF)', tasks: ['embedding'] },
  { id: 'qwen3-reranker-0.6b', name: 'Qwen3-Reranker-0.6B (GGUF)', tasks: ['reranking'] },
]

beforeEach(() => {
  getModelServerStatus.mockReset()
  getModelServerTaskStatus.mockReset()
  getModelServerModels.mockReset()
  startModelServer.mockReset()
  startModelServerTask.mockReset()
  stopModelServer.mockReset()
  stopModelServerTask.mockReset()
  setServerGpuOffload.mockReset()
  setServerTaskGpuOffload.mockReset()
  setServerGpuIndex.mockReset()
  setServerTaskGpuIndex.mockReset()
  downloadModelServer.mockReset()
  downloadModelServerTask.mockReset()
  // #760: the card now reads per-tenant status; keep the no-task mock as
  // the same resolved value so both shapes agree.
  // The card reads per-tenant status: delegate to the same mock so the
  // existing resolved values apply to task-scoped calls too.
  getModelServerTaskStatus.mockImplementation(() => getModelServerStatus())
  getModelServerStatus.mockResolvedValue({
    configured: true,
    running: false,
    port: null,
    model: 'qwen3-embedding-0.6b',
    endpoint: null,
    healthy: false,
    gpu_offload: false,
  })
  getModelServerModels.mockResolvedValue({ models })
})

afterEach(() => cleanup())

describe('Model server settings card (#730)', () => {
  it('renders the OFF status and model picker', async () => {
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    expect(screen.getByRole('button', { name: 'Start' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Download' })).toBeTruthy()
    expect(
      (screen.getByRole('button', { name: 'Stop' }) as HTMLButtonElement).disabled,
    ).toBe(true)
  })

  it('shows running status with port when server is up', async () => {
    getModelServerStatus.mockResolvedValue({
      configured: true,
      running: true,
      port: 8081,
      model: 'qwen3-embedding-0.6b',
      endpoint: 'http://127.0.0.1:8081/v1',
      healthy: true,
    })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('running on port 8081'))
    expect(
      (screen.getByRole('button', { name: 'Start' }) as HTMLButtonElement).disabled,
    ).toBe(true)
    expect(
      (screen.getByRole('button', { name: 'Stop' }) as HTMLButtonElement).disabled,
    ).toBe(false)
  })

  it('starts the server and refreshes status', async () => {
    startModelServerTask.mockResolvedValue({ ok: true, port: 8082 })
    let statusCall = 0
    getModelServerStatus.mockImplementation(() => {
      statusCall++
      if (statusCall === 1) {
        return Promise.resolve({
          configured: true, running: false, port: null,
          model: 'qwen3-embedding-0.6b', endpoint: null, healthy: false,
        })
      }
      return Promise.resolve({
        configured: true, running: true, port: 8082,
        model: 'qwen3-embedding-0.6b', endpoint: 'http://127.0.0.1:8082/v1', healthy: true,
      })
    })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(screen.getByText(/Server started on port 8082/)).toBeTruthy())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('running on port 8082'))
    expect(startModelServerTask).toHaveBeenCalledWith('qwen3-embedding-0.6b', 'embedding')
  })

  it('surfaces a start error', async () => {
    startModelServerTask.mockResolvedValue({ ok: false, error: 'download failed: timeout' })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(screen.getByText(/download failed: timeout/)).toBeTruthy())
  })

  it('stops the server and refreshes status', async () => {
    stopModelServerTask.mockResolvedValue({ ok: true })
    let statusCall = 0
    getModelServerStatus.mockImplementation(() => {
      statusCall++
      if (statusCall === 1) {
        return Promise.resolve({
          configured: true, running: true, port: 8083,
          model: 'qwen3-embedding-0.6b', endpoint: 'http://127.0.0.1:8083/v1', healthy: true,
        })
      }
      return Promise.resolve({
        configured: true, running: false, port: null,
        model: 'qwen3-embedding-0.6b', endpoint: null, healthy: false,
      })
    })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('running on port 8083'))
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(screen.getByText(/Server stopped/)).toBeTruthy())
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    expect(stopModelServerTask).toHaveBeenCalledTimes(1)
  })

  it('downloads a model without starting', async () => {
    downloadModelServerTask.mockResolvedValue({ ok: true, message: 'downloaded' })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    fireEvent.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(screen.getByText(/downloaded/)).toBeTruthy())
    expect(downloadModelServerTask).toHaveBeenCalledWith('qwen3-embedding-0.6b', 'embedding')
    expect(startModelServerTask).not.toHaveBeenCalled()
  })

  it('preselects the default embedding model', async () => {
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByLabelText('Model server model')).toBeTruthy())
    const select = screen.getByLabelText('Model server model') as HTMLSelectElement
    expect(select.value).toBe('qwen3-embedding-0.6b')
  })

  it('reads the saved gpu_offload preference from status', async () => {
    getModelServerStatus.mockResolvedValue({
      configured: true, running: false, port: null,
      model: 'qwen3-embedding-0.6b', endpoint: null, healthy: false,
      gpu_offload: true,
    })
    render(<ModelServerSettingsCard />)
    await waitFor(
      () => expect(screen.getByRole('switch', { name: 'GPU acceleration' })).toHaveAttribute('aria-checked', 'true'),
    )
  })

  it('persists the toggle when flipped off while stopped', async () => {
    getModelServerStatus.mockResolvedValue({
      configured: true, running: false, port: null,
      model: 'qwen3-embedding-0.6b', endpoint: null, healthy: false,
      gpu_offload: true,
    })
    setServerGpuOffload.mockResolvedValue({ ok: true, gpu_offload: false })
    render(<ModelServerSettingsCard />)
    await waitFor(
      () => expect(screen.getByRole('switch', { name: 'GPU acceleration' })).toHaveAttribute('aria-checked', 'true'),
    )
    fireEvent.click(screen.getByRole('switch', { name: 'GPU acceleration' }))
    await waitFor(() => expect(setServerTaskGpuOffload).toHaveBeenCalledWith(false, 'embedding'))
  })

  it('restarts the running server when GPU acceleration is toggled on', async () => {
    startModelServerTask.mockResolvedValue({ ok: true, port: 8084 })
    stopModelServerTask.mockResolvedValue({ ok: true })
    setServerTaskGpuOffload.mockResolvedValue({ ok: true, gpu_offload: true })
    let statusCall = 0
    getModelServerStatus.mockImplementation(() => {
      statusCall++
      if (statusCall === 1) {
        return Promise.resolve({
          configured: true, running: true, port: 8083,
          model: 'qwen3-embedding-0.6b',
          endpoint: 'http://127.0.0.1:8083/v1', healthy: true,
          gpu_offload: false,
        })
      }
      return Promise.resolve({
        configured: true, running: false, port: null,
        model: 'qwen3-embedding-0.6b', endpoint: null, healthy: false,
        gpu_offload: true,
      })
    })
    render(<ModelServerSettingsCard />)
    await waitFor(
      () => expect(screen.getByRole('switch', { name: 'GPU acceleration' })).toHaveAttribute('aria-checked', 'false'),
    )
    fireEvent.click(screen.getByRole('switch', { name: 'GPU acceleration' }))
    await waitFor(() => expect(setServerTaskGpuOffload).toHaveBeenCalledWith(true, 'embedding'))
    await waitFor(() => expect(stopModelServerTask).toHaveBeenCalledTimes(1))
    // A running server must restart (stop + start), not just stop, to apply
    // the new flag.
    await waitFor(() => expect(startModelServerTask).toHaveBeenCalledTimes(1))
    await waitFor(
      () => expect(screen.getByRole('switch', { name: 'GPU acceleration' })).toHaveAttribute('aria-checked', 'true'),
    )
  })

  it('disables the Stop button while a GPU toggle save is pending', async () => {
    // Running on the first status call, stopped thereafter.
    let statusCall = 0
    getModelServerStatus.mockImplementation(() => {
      statusCall++
      return statusCall === 1
        ? Promise.resolve({
            configured: true,
            running: true,
            port: 8083,
            model: 'qwen3-embedding-0.6b',
            endpoint: 'http://127.0.0.1:8083/v1',
            healthy: true,
            gpu_offload: false,
          })
        : Promise.resolve({
            configured: true,
            running: false,
            port: null,
            model: 'qwen3-embedding-0.6b',
            endpoint: null,
            healthy: false,
            gpu_offload: true,
          })
    })
    // Never resolve the save, so toggleBusy stays true after the click.
    const pending = new Promise<{ ok: boolean; gpu_offload: boolean }>(() => undefined)
    setServerTaskGpuOffload.mockReturnValue(pending)
    render(<ModelServerSettingsCard />)
    await waitFor(
      () => expect(screen.getByRole('switch', { name: 'GPU acceleration' })).toHaveAttribute('aria-checked', 'false'),
    )
    fireEvent.click(screen.getByRole('switch', { name: 'GPU acceleration' }))
    // The toggle save is in flight, so toggleBusy is true and the Stop
    // button must be disabled to prevent a double-stop during the
    // restart cascade.
    await waitFor(() => expect(setServerTaskGpuOffload).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: 'Stop' })).toHaveAttribute('disabled')
    // Clicking the (disabled) Stop must not fire a second stop.
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    expect(stopModelServerTask).not.toHaveBeenCalled()
  })
})

describe('Reranker server settings card (#760)', () => {
  it('renders its own card scoped to the reranking tenant', async () => {
    render(<RerankerServerSettingsCard />)
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Reranker server' })).toBeTruthy(),
    )
    expect(screen.getByLabelText('Reranker model')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Start' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeTruthy()
  })

  it('lists only reranking models in its picker', async () => {
    render(<RerankerServerSettingsCard />)
    const select = (await screen.findByLabelText('Reranker model')) as HTMLSelectElement
    await waitFor(() => expect(select.value).toBe('qwen3-reranker-0.6b'))
    const options = Array.from(select.options).map((o) => o.value)
    expect(options).toEqual(['qwen3-reranker-0.6b'])
  })

  it('starts the reranking tenant with the reranker model', async () => {
    startModelServerTask.mockResolvedValue({ ok: true, port: 8085 })
    render(<RerankerServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(screen.getByText(/Server started on port 8085/)).toBeTruthy())
    expect(startModelServerTask).toHaveBeenCalledWith('qwen3-reranker-0.6b', 'reranking')
  })

  it('filters the embedding card to embedding models only (#760 misroute)', async () => {
    render(<ModelServerSettingsCard />)
    const select = (await screen.findByLabelText('Model server model')) as HTMLSelectElement
    await waitFor(() => expect(select.value).toBe('qwen3-embedding-0.6b'))
    const options = Array.from(select.options).map((o) => o.value)
    expect(options).toEqual(['qwen3-embedding-0.6b'])
  })
})
