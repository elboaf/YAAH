// Issue #254 — the recovery banner must never auto-restart the backend.
// A slow (but healthy) backend — cold PyInstaller extraction, loaded VM —
// used to get killed by the watchdog ~33s in and ping-ponged forever. Now:
// poll + count-up timer only; the kill is a user-initiated button (Tauri).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

const invoke = vi.fn((_cmd: string, ..._rest: unknown[]) => Promise.resolve())

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (cmd: string, ...rest: unknown[]) => invoke(cmd, ...rest),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return { ...actual, getConfig: vi.fn(() => Promise.reject(new Error('down'))) }
})

import { BackendRecoveryBanner } from './App'
import { IS_TAURI } from './api'

// jsdom has no navigation; the banner reloads when health answers.
const reload = vi.fn()
beforeEach(() => {
  Object.defineProperty(window, 'location', {
    value: { ...window.location, reload },
    writable: true,
  })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const downEvent = () => window.dispatchEvent(new Event('backend-down'))

const flushChecks = async (ms: number) => {
  await vi.advanceTimersByTimeAsync(ms)
}

const failFetch = () =>
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.reject(new TypeError('network down'))),
  )

describe('recovery banner (#254)', () => {
  beforeEach(() => {
    invoke.mockClear()
    reload.mockClear()
    vi.useFakeTimers()
  })
  afterEach(() => vi.useRealTimers())

  it('never calls restart_backend automatically, even after minutes down', async () => {
    failFetch()
    render(<BackendRecoveryBanner />)
    downEvent()
    await flushChecks(5 * 60_000) // far past the old 33s kill threshold
    expect(invoke).not.toHaveBeenCalled()
  })

  it('shows a count-up timer of the current downtime', async () => {
    failFetch()
    render(<BackendRecoveryBanner />)
    downEvent()
    await flushChecks(30_000)
    expect(screen.getByText(/unreachable/).textContent).toMatch(/\b30 s\b/)
  })

  it(`offers a manual Restart backend button that invokes restart_backend (Tauri=${IS_TAURI})`, async () => {
    failFetch()
    render(<BackendRecoveryBanner />)
    downEvent()
    await flushChecks(10_000)
    const btn = screen.queryByRole('button', { name: /restart backend/i })
    if (IS_TAURI) {
      expect(btn).toBeTruthy()
      fireEvent.click(btn!)
      await vi.advanceTimersByTimeAsync(0)
      expect(invoke).toHaveBeenCalledWith('restart_backend')
      expect(screen.getByText(/unreachable/).textContent).toMatch(/restarting it/)
    } else {
      expect(btn).toBeNull()
    }
  })

  it('reloads (no restart) once health answers again after a slow start', async () => {
    let healthy = false
    vi.stubGlobal(
      'fetch',
      vi.fn(() => (healthy ? Promise.resolve({ ok: true }) : Promise.reject(new TypeError('down')))),
    )
    render(<BackendRecoveryBanner />)
    downEvent()
    await flushChecks(60_000) // slow start: dark for a full minute...
    expect(invoke).not.toHaveBeenCalled()
    healthy = true
    await flushChecks(10_000) // ...then answers
    expect(reload).toHaveBeenCalled()
  })
})
