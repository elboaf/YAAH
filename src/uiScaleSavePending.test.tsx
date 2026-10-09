// #171 CodeRabbit return trip #2 — while a Save is in flight, the slider must
// be disabled: `save()` captures the scale when it starts, and a drag after
// that would leave the React value + % label on a newer preview that the
// save-completion then overwrites (the close-time restore also skips because
// the setting looks clean) — i.e. the preview is silently lost.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

let resolveUpdate: (() => void) | null = null

vi.mock('./api', async (importOriginal) => {
  const config = {
    providers: { p1: { api_base: 'http://x', model: 'm', api_key: 'set' } },
    active_provider: 'p1',
    ui_scale: 1.25,
  }
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig: vi.fn(() => Promise.resolve(structuredClone(config))),
    updateConfig: vi.fn(
      () =>
        new Promise<{ ok: boolean }>((resolve) => {
          resolveUpdate = () => resolve({ ok: true })
        }),
    ),
    listAvailableModels: vi.fn(() => Promise.resolve({ models: [] })),
    setTtsKey: vi.fn(() => Promise.resolve({ ok: true })),
  }
})

import { SettingsModal } from './components'

afterEach(() => {
  cleanup()
  resolveUpdate = null
})

describe('scale slider is inert while Save is pending (#171 return trip)', () => {
  it('disables the slider between clicking Save and the save resolving', async () => {
    const events: number[] = []
    const onEvt = (e: Event) => events.push((e as CustomEvent).detail.scale)
    window.addEventListener('ui-scale-changed', onEvt)
    try {
      render(<SettingsModal onClose={() => {}} />)
      // Interface lives on the Advanced tab (task-organized settings)
      fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }))
      const slider = () =>
        screen.getByRole('slider', { name: /interface scale/i }) as HTMLInputElement
      await waitFor(() => expect(slider().disabled).toBe(false)) // loaded

      fireEvent.change(slider(), { target: { value: '1.5' } })
      expect(events).toEqual([1.5])

      fireEvent.click(
        screen
          .getAllByRole('button', { name: 'Save' })
          .find((b) => b.className.includes('bg-blue-600'))!,
      )
      // Save is in flight (updateConfig pending) — the slider must be inert
      // so its value cannot change after save() captured the scale.
      await waitFor(() => expect(slider().disabled).toBe(true))

      resolveUpdate?.()
      await waitFor(() =>
        expect(
          screen.getAllByRole('button', { name: /Saved!/i }).length,
        ).toBeGreaterThan(0),
      )
      expect(slider().disabled).toBe(false) // interactive again once settled
      expect(events).toEqual([1.5, 1.5]) // save-completion re-emits the saved scale
    } finally {
      window.removeEventListener('ui-scale-changed', onEvt)
    }
  })
})
