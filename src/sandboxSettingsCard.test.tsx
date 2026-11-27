// Sandbox settings card (issue #793): the Windows-only Settings section for
// the disposable VM. The backend sandbox module gates every path on
// `os.name == "nt"`, so the whole card must not exist on a Linux/macOS build
// — not even its status fetch. Platform is stubbed at the same seam the app
// reads (navigator.userAgent).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

const { getSandboxStatus } = vi.hoisted(() => ({
  getSandboxStatus: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, getSandboxStatus }
})

import { SandboxSettingsCard } from './components'

const windowsAgent =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const linuxAgent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

beforeEach(() => {
  getSandboxStatus.mockReset()
  // The card reads the platform the way the app does; tests stub the UA.
  // (jsdom's default UA contains no "Windows".)
  Object.defineProperty(window.navigator, 'userAgent', {
    value: windowsAgent,
    configurable: true,
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Sandbox settings card (#793)', () => {
  it('renders the Windows-only section on Windows builds', async () => {
    // A status answer lets the card render its full body.
    getSandboxStatus.mockResolvedValue({ enabled: true })
    render(<SandboxSettingsCard />)
    expect(screen.getByText('Windows Sandbox')).toBeTruthy()
    expect(getSandboxStatus).toHaveBeenCalled()
  })

  it('hides the whole section on Linux builds', () => {
    Object.defineProperty(window.navigator, 'userAgent', {
      value: linuxAgent,
      configurable: true,
    })
    render(<SandboxSettingsCard />)
    // The card and its hooks are gone entirely on non-Windows.
    expect(screen.queryByText('Windows Sandbox')).toBeNull()
    expect(screen.queryByText(/Disposable Windows VMs/)).toBeNull()
    expect(getSandboxStatus).not.toHaveBeenCalled()
  })
})
