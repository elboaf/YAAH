// Browser control settings card (issue #635, spec #630): the Windows-only
// Settings → Automate section for the baked Interceptor capability — a
// three-state status chip (not installed / installed, extension missing /
// ready) and an Install button wired to the install endpoint. The API
// layer is mocked at the module boundary; platform is stubbed at the
// same seam (navigator.userAgent) the app itself reads.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const {
  getInterceptorStatus,
  installInterceptor,
  deleteInterceptorLinks,
} = vi.hoisted(() => ({
  getInterceptorStatus: vi.fn(),
  installInterceptor: vi.fn(),
  deleteInterceptorLinks: vi.fn(),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getInterceptorStatus,
    installInterceptor,
    deleteInterceptorLinks,
  }
})

import { BrowserControlSettingsCard } from './components'

// ---- Status fixtures straight from the manager's contract (t1) ----

const notInstalled = {
  installed: 'not installed',
  version: null,
  skill_links: { router: 'not linked', browser: 'not linked', research: 'not linked' },
  skill_links_summary: '0/3',
  extension: 'extension missing',
  cli: null,
  pinned: {
    name: 'Interceptor',
    version: '2.1.6',
    url: 'https://github.com/elboaf/Interceptor/releases/download/v2.1.6/Interceptor-Browser-2.1.6-windows-x64.exe',
    sha256: '5d46…',
    license: 'Elastic License 2.0',
    notes: '',
  },
}

const installedNoExtension = {
  ...notInstalled,
  installed: 'installed',
  version: '2.1.6',
  skill_links: { router: 'linked', browser: 'linked', research: 'linked' },
  skill_links_summary: '3/3',
  extension: 'extension missing',
  cli: 'C:\\Users\\u\\AppData\\Local\\Programs\\Interceptor\\interceptor.exe',
}

const ready = {
  ...installedNoExtension,
  extension: 'ready',
}

// The install POST's nested status carries NO `pinned` (only the GET
// route attaches the manifest) -- these are the payloads as the backend
// actually answers them.
const { pinned: _dropReady, ...readyNoPinned } = ready
const { pinned: _dropInstalled, ...installedNoExtNoPinned } = installedNoExtension

const installOk = (statusNoPinned: typeof readyNoPinned) => ({
  installed: true,
  did_install: true,
  junctions: {},
  marker: true,
  status: statusNoPinned,
})

const windowsAgent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
const linuxAgent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

beforeEach(() => {
  getInterceptorStatus.mockReset()
  installInterceptor.mockReset()
  deleteInterceptorLinks.mockReset()
  getInterceptorStatus.mockResolvedValue(notInstalled)
  // The card reads the platform the way the app does; tests stub the UA.
  // (jsdom's default UA contains no "Windows".)
  Object.defineProperty(window.navigator, 'userAgent', { value: windowsAgent, configurable: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('Browser control settings card (#635)', () => {
  it('renders the not-installed chip state from the API', async () => {
    render(<BrowserControlSettingsCard />)
    const chip = await screen.findByRole('status', {
      name: 'Browser control status: not installed',
    })
    expect(chip).toBeTruthy()
    expect(screen.getByText('not installed')).toBeTruthy()
    // Nothing installed: no skills or version readouts.
    expect(screen.queryByText(/skills linked/)).toBeNull()
    expect(screen.queryByText('Interceptor 2.1.6')).toBeNull()
  })

  it('renders "installed — extension missing" as its own chip state', async () => {
    getInterceptorStatus.mockResolvedValue(installedNoExtension)
    render(<BrowserControlSettingsCard />)
    expect(
      await screen.findByRole('status', {
        name: 'Browser control status: installed — extension missing',
      }),
    ).toBeTruthy()
    expect(screen.getByText(/skills linked 3\/3/)).toBeTruthy()
    expect(screen.getByText('Interceptor 2.1.6')).toBeTruthy()
    // The remaining step is the user's: name it, never fake readiness.
    expect(screen.getByText(/load the unpacked extension in Chrome/)).toBeTruthy()
  })

  it('renders the ready state with no extension homework line', async () => {
    getInterceptorStatus.mockResolvedValue(ready)
    render(<BrowserControlSettingsCard />)
    expect(
      await screen.findByRole('status', {
        name: 'Browser control status: ready',
      }),
    ).toBeTruthy()
    expect(screen.queryByText(/load the unpacked extension/)).toBeNull()
  })

  it('says when the status endpoint is unavailable (backend down)', async () => {
    getInterceptorStatus.mockRejectedValue(new Error('Backend is unreachable'))
    render(<BrowserControlSettingsCard />)
    expect(await screen.findByText(/status unavailable/)).toBeTruthy()
  })

  it('hides the whole section on Linux builds', () => {
    Object.defineProperty(window.navigator, 'userAgent', {
      value: linuxAgent,
      configurable: true,
    })
    render(<BrowserControlSettingsCard />)
    expect(screen.queryByText('Browser control')).toBeNull()
    expect(screen.queryByText('Install')).toBeNull()
    expect(getInterceptorStatus).not.toHaveBeenCalled()
  })

  it('disables the Install button while in flight, then refreshes the chip from the returned status', async () => {
    let resolveInstall!: (v: unknown) => void
    installInterceptor.mockReturnValue(
      new Promise((resolve) => {
        resolveInstall = resolve
      }),
    )
    render(<BrowserControlSettingsCard />)
    const button = await screen.findByRole('button', { name: 'Install' })
    expect((button as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(button)
    // In-flight: disabled and relabelled; a second click cannot fire.
    expect(screen.getByRole('button', { name: 'Installing…' })).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Installing…' }) as HTMLButtonElement).disabled).toBe(true)
    // Success answers with the post-install status (no `pinned` in it):
    // the chip refreshes to the new state, and the pinned hint survives
    // from the GET rather than vanishing.
    resolveInstall(installOk(readyNoPinned))
    await waitFor(() =>
      expect(
        screen.getByRole('status', { name: 'Browser control status: ready' }),
      ).toBeTruthy(),
    )
    expect(screen.getByText(/pins Interceptor 2\.1\.6/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Installing…' })).toBeNull()
    expect((screen.getByRole('button', { name: 'Install' }) as HTMLButtonElement).disabled).toBe(false)
    expect(installInterceptor).toHaveBeenCalledTimes(1)
  })

  it('shows the actionable error on failure and the button re-arms for retry', async () => {
    installInterceptor.mockRejectedValue(
      new Error(
        '502: {"detail":{"code":"hash_mismatch","message":"the downloaded installer did not match the pinned SHA-256; nothing was run or installed. If the pinned release changed upstream, the manifest needs a reviewable bump; otherwise retry later. (installer hash mismatch: expected ab, got cd.)"}}',
      ),
    )
    render(<BrowserControlSettingsCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeTruthy())
    expect(screen.getByText(/hash_mismatch/)).toBeTruthy()
    expect(screen.getByText(/nothing was run or installed/)).toBeTruthy()
    // Re-armed: retry is possible.
    expect((screen.getByRole('button', { name: 'Install' }) as HTMLButtonElement).disabled).toBe(false)
    // The chip never faked success: it still reads not installed.
    expect(
      screen.getByRole('status', { name: 'Browser control status: not installed' }),
    ).toBeTruthy()
    // A second click fires again (retry).
    installInterceptor.mockResolvedValue(installOk(readyNoPinned))
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await waitFor(() =>
      expect(screen.getByRole('status', { name: 'Browser control status: ready' })).toBeTruthy(),
    )
  })

  it('reports the idempotent re-click through the flow\'s note', async () => {
    getInterceptorStatus.mockResolvedValue(ready)
    installInterceptor.mockResolvedValue({
      ...installOk(installedNoExtNoPinned),
      did_install: false,
      note: 'already installed; junctions verified/repaired, nothing re-run',
    })
    render(<BrowserControlSettingsCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    await waitFor(() =>
      expect(screen.getByRole('note')).toBeTruthy(),
    )
    expect(screen.getByText(/already installed; junctions verified\/repaired/)).toBeTruthy()
  })

  it('survives a non-JSON error body by showing the raw text', async () => {
    installInterceptor.mockRejectedValue(new Error('500: <html>gateway timeout</html>'))
    render(<BrowserControlSettingsCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    await waitFor(() => expect(screen.getByText(/gateway timeout/)).toBeTruthy())
  })

  it('a mount GET that resolves after install began cannot overwrite the installed state', async () => {
    // The mount GET is still pending when the user clicks Install.
    let resolveGet!: (v: unknown) => void
    getInterceptorStatus.mockReturnValue(
      new Promise((resolve) => {
        resolveGet = resolve
      }),
    )
    installInterceptor.mockResolvedValue(installOk(readyNoPinned))
    render(<BrowserControlSettingsCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    await waitFor(() =>
      expect(screen.getByRole('status', { name: 'Browser control status: ready' })).toBeTruthy(),
    )
    // The stale "not installed" answer lands afterwards: dropped. The
    // act() flush guarantees the stale .then has RUN before we assert,
    // so the pass is not merely the assertion winning a race.
    await act(async () => {
      resolveGet(notInstalled)
    })
    expect(screen.getByRole('status', { name: 'Browser control status: ready' })).toBeTruthy()
    expect(screen.queryByText(/status unavailable/)).toBeNull()
  })

  it('a mount GET that fails after install began cannot flag status unavailable', async () => {
    let rejectGet!: (e: unknown) => void
    getInterceptorStatus.mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectGet = reject
      }),
    )
    installInterceptor.mockResolvedValue(installOk(readyNoPinned))
    render(<BrowserControlSettingsCard />)
    fireEvent.click(await screen.findByRole('button', { name: 'Install' }))
    await waitFor(() =>
      expect(screen.getByRole('status', { name: 'Browser control status: ready' })).toBeTruthy(),
    )
    await act(async () => {
      rejectGet(new Error('Backend is unreachable'))
    })
    expect(screen.getByRole('status', { name: 'Browser control status: ready' })).toBeTruthy()
    expect(screen.queryByText(/status unavailable/)).toBeNull()
  })
})
