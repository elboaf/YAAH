import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAgent } from './store'

/**
 * Issue #59 regression tests. The original bug: module-eval IS_TAURI gate +
 * dynamic plugin import + `.catch(() => {})` — link clicks died silently.
 * The first fix surfaced failures as a toast but still failed (the opener
 * plugin's JS binding is broken in the packaged app), so openExternal now
 * invokes the Rust `open_external` command — the same invoke path the
 * update chip uses successfully.
 */

const invokeMock = vi.fn<(cmd: string, args?: unknown) => Promise<void>>()

vi.mock('@tauri-apps/api/core', () => ({
  invoke: (cmd: string, args?: unknown) => invokeMock(cmd, args),
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

const ev = () => ({ preventDefault: vi.fn() })

describe('openExternal (#59)', () => {
  beforeEach(() => {
    invokeMock.mockReset()
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => {
    for (const t of useAgent.getState().toasts) useAgent.getState().dismissToast(t.id)
    vi.restoreAllMocks()
  })

  it('hands https links to the Rust open_external command and prevents default navigation', async () => {
    await withTauri(true, async () => {
      const { openExternal } = await import('./openExternal')
      invokeMock.mockResolvedValueOnce()
      const e = ev()
      openExternal('https://github.com/elboaf/YAAH', e)
      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith('open_external', {
          url: 'https://github.com/elboaf/YAAH',
        })
      })
      expect(e.preventDefault).toHaveBeenCalled()
    })
  })

  it('opens mailto links through the command too', async () => {
    await withTauri(true, async () => {
      const { openExternal } = await import('./openExternal')
      invokeMock.mockResolvedValueOnce()
      openExternal('mailto:x@y.z', ev())
      await vi.waitFor(() => {
        expect(invokeMock).toHaveBeenCalledWith('open_external', { url: 'mailto:x@y.z' })
      })
    })
  })

  it('falls back to native navigation outside Tauri (no preventDefault, no invoke)', async () => {
    await withTauri(false, async () => {
      const { openExternal } = await import('./openExternal')
      const e = ev()
      openExternal('https://github.com', e)
      expect(e.preventDefault).not.toHaveBeenCalled()
      expect(invokeMock).not.toHaveBeenCalled()
    })
  })

  it('a failed open is loud: console error + toast carrying the raw cause (#59)', async () => {
    await withTauri(true, async () => {
      const { openExternal } = await import('./openExternal')
      invokeMock.mockRejectedValueOnce(new Error('ShellExecuteW failed (code 2)'))
      openExternal('https://github.com/elboaf/YAAH', ev())
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
        expect(last?.body).toContain('ShellExecuteW failed (code 2)')
      })
    })
  })
})
