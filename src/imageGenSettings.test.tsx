// Image generation (sd-server) Settings card: endpoint + default size +
// live Test probe. Blank endpoint = unconfigured = no generate_image tool,
// so the availability line must track the field.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { probeImageProvider, getLoraCatalog } = vi.hoisted(() => ({
  probeImageProvider: vi.fn(),
  getLoraCatalog: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, probeImageProvider, getLoraCatalog }
})

import { ImageGenSettingsCard } from './components'

const base = { provider: 'sdcpp', api_base: '' }

describe('Image generation settings card', () => {
  beforeEach(() => {
    probeImageProvider.mockReset()
    getLoraCatalog.mockReset()
    getLoraCatalog.mockResolvedValue({ loras: [], count: 0, unknown_count: 0, scanned_at: null, notice: null })
  })

  afterEach(() => {
    cleanup()
  })

  it('renders the saved endpoint and size', () => {
    render(
      <ImageGenSettingsCard
        imageGen={{ ...base, api_base: 'http://127.0.0.1:1234' }}
        onChange={() => {}}
      />,
    )
    const input = screen.getByPlaceholderText(/stable-diffusion\.cpp server/) as HTMLInputElement
    expect(input.value).toBe('http://127.0.0.1:1234')
    expect(screen.getByText(/tool is available/)).toBeTruthy()
  })

  it('disables Test while the endpoint is blank and says the tool is off', () => {
    render(<ImageGenSettingsCard imageGen={{ ...base }} onChange={() => {}} />)
    const button = screen.getByRole('button', { name: 'Test' }) as HTMLButtonElement
    expect(button.disabled).toBe(true)
    expect(screen.getByText(/no generate_image tool/)).toBeTruthy()
  })

  it('shows the /loras notice when the scan reports unknown LoRAs (#398)', async () => {
    getLoraCatalog.mockResolvedValue({
      loras: [{ path: 'x.safetensors', name: 'x', class: 'unknown', trigger: null, multiplier: null }],
      count: 1,
      unknown_count: 1,
      scanned_at: 'now',
      notice: '1 LoRA could not be classified automatically - start a chat and run /loras to finish setup.',
    })
    render(
      <ImageGenSettingsCard
        imageGen={{ ...base, api_base: 'http://127.0.0.1:1234' }}
        onChange={() => {}}
      />,
    )
    await waitFor(() =>
      expect(screen.getByRole('status').textContent).toMatch(/run \/loras/),
    )
  })

  it('stays silent when the scan classifies everything (#398)', async () => {
    getLoraCatalog.mockResolvedValue({
      loras: [{ path: 'a.safetensors', name: 'a', class: 'triggered', trigger: 'tok', multiplier: 0.8 }],
      count: 1,
      unknown_count: 0,
      scanned_at: 'now',
      notice: null,
    })
    render(
      <ImageGenSettingsCard
        imageGen={{ ...base, api_base: 'http://127.0.0.1:1234' }}
        onChange={() => {}}
      />,
    )
    await new Promise((r) => setTimeout(r, 30))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('shows probe success with the reported model', async () => {
    probeImageProvider.mockResolvedValue({ ok: true, models: ['sd-cpp-local'] })
    render(
      <ImageGenSettingsCard
        imageGen={{ ...base, api_base: 'http://127.0.0.1:1234' }}
        onChange={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Test' }))
    await waitFor(() =>
      expect(screen.getByText(/sd-server reachable/).textContent).toMatch(/sd-cpp-local/),
    )
    expect(probeImageProvider).toHaveBeenCalledWith('http://127.0.0.1:1234')
  })

  it('shows the probe error verbatim on failure', async () => {
    probeImageProvider.mockResolvedValue({ ok: false, error: 'HTTP 500' })
    render(
      <ImageGenSettingsCard
        imageGen={{ ...base, api_base: 'http://127.0.0.1:9999' }}
        onChange={() => {}}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Test' }))
    await waitFor(() => expect(screen.getByText('HTTP 500')).toBeTruthy())
  })

  it('reports edits through onChange', () => {
    const onChange = vi.fn()
    render(<ImageGenSettingsCard imageGen={{ ...base }} onChange={onChange} />)
    fireEvent.change(screen.getByPlaceholderText(/stable-diffusion\.cpp server/), {
      target: { value: 'http://localhost:1234' },
    })
    expect(onChange).toHaveBeenCalledWith({
      provider: 'sdcpp',
      api_base: 'http://localhost:1234',
    })
  })
})
