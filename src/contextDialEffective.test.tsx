import { describe, expect, it, vi } from 'vitest'
import { act, render, waitFor } from '@testing-library/react'
import { ContextChip } from './components'

const { getConfig, getCompactionEffective } = vi.hoisted(() => ({
  getConfig: vi.fn(),
  getCompactionEffective: vi.fn(),
}))

vi.mock('./api', async (importActual) => {
  const actual = await importActual<typeof import('./api')>()
  return { ...actual, getConfig, getCompactionEffective }
})

// The module-level caches (compaction config + effective answers) persist
// for the whole file, so the FIRST render seeds them for the rest: the
// global trigger here is 100k, and later assertions that a DIFFERENT
// value won prove the per-model answer outranks the global one.
describe('ContextChip effective per-model trigger (#588)', () => {
  const TIEL = 'x2.Tiel-Coder-35B-A3B-MTP-UD-Q4_K_S'

  /** Invert the shared DialTick geometry: the trigger a tick sits at,
   *  in tokens of the fixed 200k dial. atan2 on both endpoints (asin on
   *  y2 alone mirrors past the dial's bottom). */
  function tickTokens(x2: string | null, y2: string | null): number | null {
    if (x2 === null || y2 === null) return null
    const a = Math.atan2(Number(y2) - 12, Number(x2) - 12)
    return (((a + Math.PI / 2) / (2 * Math.PI)) % 1 + 1) % 1 * 200_000
  }

  it('keeps the global tick while the effective probe has no answer', async () => {
    getConfig.mockResolvedValue({ compaction: { enabled: true, trigger_tokens: 100_000 } })
    getCompactionEffective.mockRejectedValue(new Error('backend down'))
    const { container } = render(
      <ContextChip info={{ tokens: 120_000, window: 131_072, model: 'whatever' }} />,
    )
    await waitFor(() => expect(container.querySelectorAll('line')).toHaveLength(1))
    expect(tickTokens(container.querySelector('line')!.getAttribute('x2'), container.querySelector('line')!.getAttribute('y2'))).toBeCloseTo(100_000, 0)
  })

  it('ticks at the entry trigger an alias sibling saved for this family', async () => {
    // The live #1092 shape: the chat runs x2.Tiel-..., the entry was
    // saved under x2.qwen36-... — the backend resolves the alias and
    // reports ITS trigger as effective for this model. The global cache
    // holds 100k; the tick must move to 24k.
    getConfig.mockResolvedValue({ compaction: { enabled: true, trigger_tokens: 100_000 } })
    getCompactionEffective.mockResolvedValue({
      model: TIEL,
      entry: true,
      entry_key: 'x2.qwen36-reap50-Q4_K_M-imat',
      enabled: true,
      trigger_tokens: 24_000,
      keep_tokens: 9_600,
      window: 131_072,
    })
    const { container } = render(
      <ContextChip info={{ tokens: 31_000, window: 131_072, model: TIEL }} />,
    )
    await waitFor(() =>
      expect(tickTokens(container.querySelector('line')?.getAttribute('x2') ?? null, container.querySelector('line')?.getAttribute('y2') ?? null)).toBeCloseTo(24_000, 0),
    )
    expect(container.querySelectorAll('line')).toHaveLength(1)
  })

  it('shows the derived fallback trigger for an entry-less model (fault B made visible)', async () => {
    // No entry anywhere: the runtime derives ~80% of the window and WILL
    // fire there — the dial now says so instead of hiding it.
    getCompactionEffective.mockResolvedValue({
      model: 'mystery-local',
      entry: false,
      entry_key: null,
      enabled: true,
      trigger_tokens: 104_857,
      keep_tokens: 41_943,
      window: 131_072,
    })
    const { container } = render(
      <ContextChip info={{ tokens: 31_000, window: 131_072, model: 'mystery-local' }} />,
    )
    await waitFor(() =>
      expect(tickTokens(container.querySelector('line')?.getAttribute('x2') ?? null, container.querySelector('line')?.getAttribute('y2') ?? null)).toBeCloseTo(104_857, 0),
    )
  })

  it('shows no tick when the covering entry disables compaction', async () => {
    getCompactionEffective.mockResolvedValue({
      model: 'local-model',
      entry: true,
      entry_key: 'local-model',
      enabled: false,
      trigger_tokens: 0,
      keep_tokens: 52_428,
      window: 131_072,
    })
    const { container } = render(
      <ContextChip info={{ tokens: 31_000, window: 131_072, model: 'local-model' }} />,
    )
    // The fetch for this model lands, and even after the answer is
    // applied the chip shows no tick: runtime compaction is OFF here.
    await waitFor(() =>
      expect(getCompactionEffective.mock.calls.some((c) => c[0] === 'local-model')).toBe(true),
    )
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(container.querySelectorAll('line')).toHaveLength(0)
  })
})
