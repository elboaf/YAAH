// Local model server settings card (issue #730): status row, start/stop,
// download action, model picker. API layer is mocked at the module boundary.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const {
  getModelServerStatus,
  getModelServerModels,
  startModelServer,
  stopModelServer,
  setServerGpuOffload,
  downloadModelServer,
} = vi.hoisted(() => ({
  getModelServerStatus: vi.fn(),
  getModelServerModels: vi.fn(),
  startModelServer: vi.fn(),
  stopModelServer: vi.fn(),
  setServerGpuOffload: vi.fn(),
  downloadModelServer: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getModelServerStatus,
    getModelServerModels,
    startModelServer,
    stopModelServer,
    setServerGpuOffload,
    downloadModelServer,
  }
})

import { ModelServerSettingsCard } from './components'

const models = [
  { id: 'qwen3-embedding-0.6b', name: 'Qwen3-Embedding-0.6B (GGUF)', tasks: ['embedding'] },
  { id: 'qwen3-reranker-0.6b', name: 'Qwen3-Reranker-0.6B (GGUF)', tasks: ['reranking'] },
]

beforeEach(() => {
  getModelServerStatus.mockReset()
  getModelServerModels.mockReset()
  startModelServer.mockReset()
  stopModelServer.mockReset()
  setServerGpuOffload.mockReset()
  downloadModelServer.mockReset()
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
    startModelServer.mockResolvedValue({ ok: true, port: 8082 })
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
    expect(startModelServer).toHaveBeenCalledWith('qwen3-embedding-0.6b')
  })

  it('surfaces a start error', async () => {
    startModelServer.mockResolvedValue({ ok: false, error: 'download failed: timeout' })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    await waitFor(() => expect(screen.getByText(/download failed: timeout/)).toBeTruthy())
  })

  it('stops the server and refreshes status', async () => {
    stopModelServer.mockResolvedValue({ ok: true })
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
    expect(stopModelServer).toHaveBeenCalledTimes(1)
  })

  it('downloads a model without starting', async () => {
    downloadModelServer.mockResolvedValue({ ok: true, message: 'downloaded' })
    render(<ModelServerSettingsCard />)
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('off'))
    fireEvent.click(screen.getByRole('button', { name: 'Download' }))
    await waitFor(() => expect(screen.getByText(/downloaded/)).toBeTruthy())
    expect(downloadModelServer).toHaveBeenCalledWith('qwen3-embedding-0.6b')
    expect(startModelServer).not.toHaveBeenCalled()
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
    await waitFor(() => expect(setServerGpuOffload).toHaveBeenCalledWith(false))
  })

  it('restarts the running server when GPU acceleration is toggled on', async () => {
    stopModelServer.mockResolvedValue({ ok: true })
    setServerGpuOffload.mockResolvedValue({ ok: true, gpu_offload: true })
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
    await waitFor(() => expect(setServerGpuOffload).toHaveBeenCalledWith(true))
    await waitFor(() => expect(stopModelServer).toHaveBeenCalledTimes(1))
    await waitFor(
      () => expect(screen.getByRole('switch', { name: 'GPU acceleration' })).toHaveAttribute('aria-checked', 'true'),
    )
  })
})