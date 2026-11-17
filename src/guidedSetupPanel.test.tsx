// Guided setup panel (issue #636, spec #630, ticket t6): the numbered
// Chrome steps, the real unpacked-extension folder with a one-click copy,
// and the live status poll that runs only while the panel is visible and
// stops once the extension is actually detected. Same seams as the #635
// suite: the API layer mocked at the module boundary, platform stubbed at
// navigator.userAgent. Timers are faked for the poll cadence, so every
// async wait goes through advanceTimersByTimeAsync + sync queries (RTL's
// own findBy/waitFor rely on real timers and starve under fake ones).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'

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
  pinned: null,
}

const installedNoExtension = {
  ...notInstalled,
  installed: 'installed',
  version: '2.1.6',
  skill_links: { router: 'linked', browser: 'linked', research: 'linked' },
  skill_links_summary: '3/3',
  extension: 'extension missing',
  cli: 'C:\\Users\\u\\AppData\\Local\\Programs\\Interceptor\\interceptor.exe',
  extension_path: 'C:\\Users\\u\\AppData\\Local\\Programs\\Interceptor\\extension',
}

const ready = {
  ...installedNoExtension,
  extension: 'ready' as const,
}

// The install POST's nested status carries no `pinned` (only the GET
// route attaches the manifest) but DOES carry `extension_path` (the
// panel needs the real folder right after install).
const { pinned: _dropReady, ...readyNoPinned } = ready
const { pinned: _dropInstalled, ...installedNoExtNoPinned } = installedNoExtension

const installOk = (statusNoPinned: Omit<typeof installedNoExtension, 'pinned'>) => ({
  installed: true,
  did_install: true,
  junctions: {},
  marker: true,
  status: statusNoPinned,
})

const windowsAgent =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

beforeEach(() => {
  getInterceptorStatus.mockReset()
  installInterceptor.mockReset()
  deleteInterceptorLinks.mockReset()
  getInterceptorStatus.mockResolvedValue(notInstalled)
  Object.defineProperty(window.navigator, 'userAgent', {
    value: windowsAgent,
    configurable: true,
  })
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

// With fake timers, advance the clock (default: no tick, just a full
// microtask flush so mocked promises and their .then chains settle) and
// let React settle inside act(). Real timers never fire in these tests.
const flush = async (ms = 0) => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

const chip = (label: string) =>
  screen.getByRole('status', { name: `Browser control status: ${label}` })

const stepsButton = () => screen.getByRole('button', { name: /setup steps/ })

describe('guided extension setup panel (#636)', () => {
  it('opens by itself after a successful install and shows the numbered steps', async () => {
    // The mount GET reads not installed; every GET after the install
    // (the panel's own polls included) reads the real post-install
    // state -- the same way the backend actually answers.
    getInterceptorStatus.mockResolvedValueOnce(notInstalled).mockResolvedValue(installedNoExtension)
    installInterceptor.mockResolvedValue(installOk(installedNoExtNoPinned))
    render(<BrowserControlSettingsCard />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await flush()
    // The panel appears without a second click: the moment the install
    // lands, the one remaining step is the user's.
    expect(screen.getByTestId('guided-panel')).toBeTruthy()
    expect(screen.getByText(/chrome:\/\/extensions/)).toBeTruthy()
    expect(screen.getByText(/Developer mode/)).toBeTruthy()
    expect(screen.getByText(/Load unpacked/)).toBeTruthy()
    // The real folder is offered for picking (and copying) right away.
    expect(
      screen.getByText('C:\\Users\\u\\AppData\\Local\\Programs\\Interceptor\\extension'),
    ).toBeTruthy()
    expect(chip('installed — extension missing')).toBeTruthy()
  })

  it('shows the real install path with a one-click copy button', async () => {
    getInterceptorStatus.mockResolvedValue(installedNoExtension)
    render(<BrowserControlSettingsCard />)
    await flush()
    fireEvent.click(stepsButton())
    await flush()
    const path = screen.getByText(
      'C:\\Users\\u\\AppData\\Local\\Programs\\Interceptor\\extension',
    )
    expect(path).toBeTruthy()
    // jsdom ships no clipboard: define the property the component reads.
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
    })
    fireEvent.click(screen.getByRole('button', { name: 'copy path' }))
    await flush()
    expect(writeText).toHaveBeenCalledWith(
      'C:\\Users\\u\\AppData\\Local\\Programs\\Interceptor\\extension',
    )
    expect(screen.getByRole('button', { name: 'copied!' })).toBeTruthy()
  })

  it('polls status only while the panel is visible', async () => {
    getInterceptorStatus.mockResolvedValue(installedNoExtension)
    render(<BrowserControlSettingsCard />)
    await flush()
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1) // mount GET only
    await flush(7000)
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1) // no panel: no poll
    fireEvent.click(stepsButton())
    await flush()
    expect(getInterceptorStatus).toHaveBeenCalledTimes(2) // poll at panel open
    await flush(3000)
    expect(getInterceptorStatus).toHaveBeenCalledTimes(3) // poll t+3s
    await flush(3000)
    expect(getInterceptorStatus).toHaveBeenCalledTimes(4) // poll t+6s
    // Close the panel: the poll stops.
    fireEvent.click(stepsButton())
    await flush(7000)
    expect(getInterceptorStatus).toHaveBeenCalledTimes(4) // nothing further
  })
  it('stops polling for good once the status lands ready (actual detection)', async () => {
    // Route: the mount already reads installed+missing, the panel opens
    // (the only way the toggle exists), polls, and lands ready.
    getInterceptorStatus.mockResolvedValue(installedNoExtension)
    render(<BrowserControlSettingsCard />)
    await flush()
    fireEvent.click(stepsButton())
    await flush()
    await flush(3000)
    expect(chip('installed — extension missing')).toBeTruthy()
    // The extension is detected on the next poll -- the CLI's own
    // diagnosis behind the status endpoint, never a timeout.
    getInterceptorStatus.mockResolvedValue(ready)
    await flush(3000)
    expect(chip('ready')).toBeTruthy()
    const callsAtDetection = getInterceptorStatus.mock.calls.length
    // The poll stops for good: no further ticks, no further calls.
    await flush(7000)
    expect(getInterceptorStatus.mock.calls.length).toBe(callsAtDetection)
  })

  it('turning green requires detection: a failing status answer cannot flip the panel', async () => {
    getInterceptorStatus.mockResolvedValue(installedNoExtension)
    render(<BrowserControlSettingsCard />)
    await flush()
    fireEvent.click(stepsButton())
    await flush()
    // The live check fails from here on: the panel keeps polling and
    // stays amber -- a timeout or an error never reads as success.
    getInterceptorStatus.mockRejectedValue(new Error('backend down'))
    await flush(10000)
    expect(screen.getByText(/live check unavailable/)).toBeTruthy()
    expect(screen.queryByText(/done: browser control is ready/)).toBeNull()
    expect(chip('installed — extension missing')).toBeTruthy()
  })

  it('closing the panel mid-setup leaves the valid partial state, and reopening works', async () => {
    getInterceptorStatus.mockResolvedValue(installedNoExtension)
    render(<BrowserControlSettingsCard />)
    await flush()
    // Before the panel is ever opened: chip says the partial state, no
    // steps anywhere, no poll running.
    expect(chip('installed — extension missing')).toBeTruthy()
    expect(screen.queryByTestId('guided-panel')).toBeNull()
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1)
    // Open, then close: ignoring the panel is a valid outcome.
    fireEvent.click(stepsButton())
    await flush()
    expect(screen.getByTestId('guided-panel')).toBeTruthy()
    fireEvent.click(stepsButton())
    await flush()
    expect(screen.queryByTestId('guided-panel')).toBeNull()
    await flush(7000)
    // The partial state stands (chip unchanged), and reopening works.
    expect(chip('installed — extension missing')).toBeTruthy()
    fireEvent.click(stepsButton())
    expect(screen.getByTestId('guided-panel')).toBeTruthy()
  })

  it('a mount that is already ready shows no panel, no steps, no poll', async () => {
    getInterceptorStatus.mockResolvedValue(ready)
    render(<BrowserControlSettingsCard />)
    await flush()
    expect(chip('ready')).toBeTruthy()
    expect(screen.queryByTestId('guided-panel')).toBeNull()
    expect(screen.queryByRole('button', { name: /setup steps/ })).toBeNull()
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1)
    await flush(7000)
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1)
  })

  it('after detection the section confirms in green instead of showing steps', async () => {
    getInterceptorStatus
      .mockResolvedValueOnce(notInstalled) // mount GET
      .mockResolvedValue(installedNoExtension) // panel polls
    installInterceptor.mockResolvedValue(installOk(installedNoExtNoPinned))
    render(<BrowserControlSettingsCard />)
    await flush()
    // Install: the panel opens by itself (post-install status is not
    // ready), polling starts.
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await flush()
    expect(screen.getByTestId('guided-panel')).toBeTruthy()
    // The extension is detected on a later poll.
    getInterceptorStatus.mockResolvedValue(ready)
    await flush(3000)
    expect(chip('ready')).toBeTruthy()
    // The steps are gone; a single green confirmation line remains (and
    // only because the panel had actually been open).
    expect(screen.queryByTestId('guided-panel')).toBeNull()
    expect(screen.getByText('done: browser control is ready')).toBeTruthy()
    // The poll is over.
    const callsAtDetection = getInterceptorStatus.mock.calls.length
    await flush(7000)
    expect(getInterceptorStatus.mock.calls.length).toBe(callsAtDetection)
  })

  it('an install landing already-ready skips the panel entirely', async () => {
    // The extension was loaded while YAAH was closed: the install POST's
    // post-install status already reads ready. There is nothing left to
    // guide -- no panel, no poll, no green transition line (nothing
    // transitioned). The chip alone speaks the ready state.
    getInterceptorStatus.mockResolvedValue(notInstalled)
    installInterceptor.mockResolvedValue(installOk(readyNoPinned))
    render(<BrowserControlSettingsCard />)
    await flush()
    fireEvent.click(screen.getByRole('button', { name: 'Install' }))
    await flush()
    expect(chip('ready')).toBeTruthy()
    expect(screen.queryByTestId('guided-panel')).toBeNull()
    expect(screen.queryByText(/done: browser control is ready/)).toBeNull()
    // Only the mount GET ever ran: the ready state opens no poll.
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1)
    await flush(7000)
    expect(getInterceptorStatus).toHaveBeenCalledTimes(1)
  })

  it('a mount that lands ready without the panel ever opening shows no done line', async () => {
    // Route: the extension was already loaded at mount. Nobody was
    // guided (the panel never ran), so there is nothing to confirm --
    // the chip alone speaks the ready state.
    getInterceptorStatus.mockResolvedValue(ready)
    render(<BrowserControlSettingsCard />)
    await flush()
    expect(chip('ready')).toBeTruthy()
    expect(screen.queryByText(/done: browser control is ready/)).toBeNull()
  })
})
