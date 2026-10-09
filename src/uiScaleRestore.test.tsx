// #171 CodeRabbit return trip — two fixes on the interface-scale slider:
// 1. the card row wraps at narrow widths (min-w-0 on the description, flex-wrap
//    on the row);
// 2. the Settings modal restores the persisted zoom synchronously on unmount
//    (no async re-fetch: a rejected fetch can't strand the preview zoom, and a
//    late response can't clobber a newer modal), and Save updates the saved
//    scale so a following close doesn't "restore" the stale value.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  let calls = 0
  const config = {
    providers: { p1: { api_base: 'http://x', model: 'm', api_key: 'set' } },
    active_provider: 'p1',
    ui_scale: 1.25,
  }
  const actual = await importOriginal<typeof import('./api')>()
  return {
    ...actual,
    getConfig: vi.fn(() => {
      calls++
      return Promise.resolve(structuredClone(config))
    }),
    updateConfig: vi.fn(() => Promise.resolve({ ok: true })),
    listAvailableModels: vi.fn(() => Promise.resolve({ models: [] })),
    setTtsKey: vi.fn(() => Promise.resolve({ ok: true })),
  }
})
// call counter lives outside the module mock (hoisting) — attach via a probe

import { InterfaceScaleCard, SettingsModal } from './components'
import { getConfig } from './api'

afterEach(() => cleanup())

describe('interface-scale card row wraps (#171 return trip)', () => {
  it('lets the description shrink and the row wrap at narrow widths', () => {
    render(<InterfaceScaleCard scale={1.25} onChange={() => {}} />)
    const row = screen.getByRole('slider', { name: /interface scale/i })
      .closest('div.justify-between')! as HTMLElement
    expect(row.className).toContain('flex-wrap')
    const desc = row.querySelector('p')!
    expect(desc.className).toContain('min-w-0')
  })
})

describe('settings modal restores the persisted zoom synchronously (#171 return trip)', () => {
  let events: number[] = []
  const listen = () => {
    events = []
    window.addEventListener('ui-scale-changed', onEvt)
  }
  const onEvt = (e: Event) => events.push((e as CustomEvent).detail.scale)
  const unlisten = () => window.removeEventListener('ui-scale-changed', onEvt)

  afterEach(unlisten)

  it('re-emits the persisted scale on unmount after an unsaved drag — synchronously, without re-fetching config', async () => {
    listen()
    const { unmount } = render(<SettingsModal onClose={() => {}} />)
    // Interface lives on the Advanced tab (task-organized settings)
    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }))
    await waitFor(() =>
      expect(screen.getByRole('slider', { name: /interface scale/i })).toBeTruthy(),
    )
    fireEvent.change(screen.getByRole('slider', { name: /interface scale/i }), {
      target: { value: '1.8' },
    })
    expect(events).toEqual([1.8]) // live preview fired
    const callsAtSteady = (getConfig as ReturnType<typeof vi.fn>).mock.calls.length
    unmount()
    // restored to the persisted 1.25, dispatched synchronously (no await),
    // and no additional getConfig call was made on unmount
    expect(events[events.length - 1]).toBe(1.25)
    expect((getConfig as ReturnType<typeof vi.fn>).mock.calls.length).toBe(callsAtSteady)
  })

  it('does not re-emit a stale restore after Save → close', async () => {
    listen()
    const { unmount } = render(<SettingsModal onClose={() => {}} />)
    // Interface lives on the Advanced tab (task-organized settings)
    fireEvent.click(screen.getByRole('tab', { name: 'Advanced' }))
    await waitFor(() =>
      expect(screen.getByRole('slider', { name: /interface scale/i })).toBeTruthy(),
    )
    fireEvent.change(screen.getByRole('slider', { name: /interface scale/i }), {
      target: { value: '1.5' },
    })
    fireEvent.click(
      screen.getAllByRole('button', { name: 'Save' }).find((b) => b.className.includes('bg-blue-600'))!,
    )
    await waitFor(() => expect(screen.getByRole('button', { name: /Saved!/i })).toBeTruthy())
    const n = events.length
    unmount()
    expect(events.length).toBe(n) // saved scale IS the live zoom — no-op restore
  })
})
