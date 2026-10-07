// Issue #112: Windows Sandbox toggle in Settings. The card shows the
// feature state; toggling on when the Windows feature is missing must
// surface the Enable-WindowsOptionalFeature command and the BIOS
// virtualization hint, and never disable the Windows feature destructively.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { getConfig, updateConfig, getSandboxStatus } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  updateConfig: vi.fn(),
  getSandboxStatus: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, getConfig, updateConfig, getSandboxStatus }
})

import { SandboxSettingsCard } from './components'

describe('Windows Sandbox settings card (#112)', () => {
  beforeEach(() => {
    getConfig.mockResolvedValue({
      sandbox: { enabled: true, memory_mb: 8192 },
    })
    updateConfig.mockResolvedValue({ ok: true })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('reflects the saved toggle state from config', async () => {
    getSandboxStatus.mockResolvedValue({ available: true, enabled: true, running: false })
    render(<SandboxSettingsCard />)
    const box = await screen.findByRole('checkbox', { name: /Windows Sandbox/i })
    await waitFor(() => expect((box as HTMLInputElement).checked).toBe(true))
  })

  it('shows the enable command and BIOS hint when the feature is missing', async () => {
    getSandboxStatus.mockResolvedValue({
      available: false,
      enabled: true,
      running: false,
      enable_command:
        "Enable-WindowsOptionalFeature -Online -FeatureName 'Containers-DisposableClientVM' -All",
      bios_hint: 'requires virtualization to be enabled in the BIOS/UEFI (Intel VT-x / AMD-V)',
    })
    render(<SandboxSettingsCard />)
    await screen.findByRole('checkbox', { name: /Windows Sandbox/i })
    await waitFor(() =>
      expect(screen.getByText(/Enable-WindowsOptionalFeature/)).toBeInTheDocument(),
    )
    expect(screen.getByText(/BIOS\/UEFI \(Intel VT-x \/ AMD-V\)/)).toBeInTheDocument()
  })

  it('saves sandbox.enabled through updateConfig on toggle', async () => {
    getSandboxStatus.mockResolvedValue({ available: true, enabled: true, running: false })
    render(<SandboxSettingsCard />)
    const box = (await screen.findByRole('checkbox', { name: /Windows Sandbox/i })) as HTMLInputElement
    await waitFor(() => expect(box.checked).toBe(true))
    fireEvent.click(box)
    await waitFor(() =>
      expect(updateConfig).toHaveBeenCalledWith({ sandbox: { enabled: false } }),
    )
  })

  it('disabling never touches the Windows feature (only the config flag)', async () => {
    getSandboxStatus.mockResolvedValue({ available: true, enabled: true, running: false })
    render(<SandboxSettingsCard />)
    const box = (await screen.findByRole('checkbox', { name: /Windows Sandbox/i })) as HTMLInputElement
    await waitFor(() => expect(box.checked).toBe(true))
    fireEvent.click(box)
    await waitFor(() => expect(updateConfig).toHaveBeenCalled())
    // The only mutation is the config flag; no destructive call exists.
    const patch = updateConfig.mock.calls[0][0] as { sandbox: { enabled: boolean } }
    expect(patch.sandbox.enabled).toBe(false)
    expect(Object.keys(patch)).toEqual(['sandbox'])
  })
})

describe('SandboxSettingsCard copy (issue #340)', () => {
  it('promises only-the-user can re-enable, never the old agent-can-use phrasing', async () => {
    getSandboxStatus.mockResolvedValue({ available: true, enabled: true, running: false })
    render(<SandboxSettingsCard />)
    await screen.findByRole('checkbox', { name: /Windows Sandbox/i })
    const copy = document.body.textContent || ''
    expect(copy).toMatch(/Only you can change this from the Settings window/)
    expect(copy).not.toMatch(/stops the agent from using the sandbox/)
  })
})
