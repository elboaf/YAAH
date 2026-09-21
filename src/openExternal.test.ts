import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAgent } from './store'

/**
 * Issue #59 regression tests: the pre-fix openExternal gated on IS_TAURI at
 * module-eval time, dynamically imported the opener glue, and swallowed every
 * failure with `.catch(() => {})` — so a misfired gate or a rejected import
 * left link clicks doing nothing, with zero diagnosability.
 */

const openUrl = vi.fn<(href: string) => Promise<void>>()

vi.mock('@tauri-apps/plugin-opener', () => ({
  openUrl: (href: string) => openUrl(href),
}))

function withTauri(inside: boolean, run: () => void | Promise<void>): Promise<void> {
  const had = '__TAURI_INTERNALS__' in window
  if (inside && !had) {
    ;(window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {}
  } else if (!inside && had) {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__
  }
  return Promise.resolve()
    .then(run)
    .finally(() => {
      if (!had) delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__
      else ;(window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {}
    })
}

const ev = () => {
  const e = { preventDefault: vi.fn() }
  return e
}

describe('openExternal (#59)', () => {
  beforeEach(() => {
    openUrl.mockReset()
    vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.clearAllMocks()
  })

  afterEach(() => {
    // keep the toast stack from leaking between tests
    for (const t of useAgent.getState().toasts) useAgent.getState().dismissToast(t.id)
    vi.restoreAllMocks()
  })

  it('hands https links to the opener plugin and prevents default navigation', async () => {
    await withTauri(true, async () => {
      const { openExternal } = await import('./openExternal')
      openUrl.mockResolvedValueOnce()
      const e = ev()
      openExternal('https://github.com/elboaf/YAAH', e)
      expect(openUrl).toHaveBeenCalledWith('https://github.com/elboaf/YAAH')
      expect(e.preventDefault).toHaveBeenCalled()
    })
  })

  it('opens mailto links through the plugin too', async () => {
    await withTauri(true, async () => {
      const { openExternal } = await import('./openExternal')
      openUrl.mockResolvedValueOnce()
      openExternal('mailto:x@y.z', ev())
      expect(openUrl).toHaveBeenCalledWith('mailto:x@y.z')
    })
  })

  it('falls back to native navigation outside Tauri (no preventDefault, no plugin call)', async () => {
    await withTauri(false, async () => {
      const { openExternal } = await import('./openExternal')
      const e = ev()
      openExternal('https://github.com', e)
      expect(e.preventDefault).not.toHaveBeenCalled()
      expect(openUrl).not.toHaveBeenCalled()
    })
  })

  it('a failed open is no longer silent: console error + error toast (#59)', async () => {
    await withTauri(true, async () => {
      const { openExternal } = await import('./openExternal')
      openUrl.mockRejectedValueOnce(new Error('plugin not registered'))
      const e = ev()
      openExternal('https://github.com/elboaf/YAAH', e)
      await vi.waitFor(() => {
        expect(console.error).toHaveBeenCalledWith(
          expect.stringContaining('openExternal'),
          'https://github.com/elboaf/YAAH',
          expect.any(Error),
        )
        const toasts = useAgent.getState().toasts
        const last = toasts[toasts.length - 1]
        expect(last?.kind).toBe('error')
        expect(last?.title).toBe('Could not open link')
        expect(last?.body).toContain('https://github.com/elboaf/YAAH')
      })
    })
  })
})
