import { describe, expect, it } from 'vitest'
import { tapeOffsetPx, quantizeUiScale } from './jitter'

// Issue #133: the whole client area of the app was seen jittering by ~1px.
// One candidate cause: the telemetry / sub-agent-preview tickers translate
// their tape with fractional `translateX(px)` offsets, which browsers
// anti-alias at subpixel positions — the line visibly oscillates as the
// fractional part flips between renders. Offsets must be integers.
describe('tapeOffsetPx', () => {
  it('returns 0 when the tape fits the viewport', () => {
    expect(tapeOffsetPx(300, 200)).toBe(0)
  })

  it('floors to an integer when the tape overflows', () => {
    expect(tapeOffsetPx(300, 412.6)).toBe(-113)
    expect(tapeOffsetPx(100, 100.5)).toBe(-1)
  })

  it('never produces a positive offset', () => {
    expect(tapeOffsetPx(100, 50)).toBe(0)
  })

  it('handles missing measurements', () => {
    expect(tapeOffsetPx(0, 0)).toBe(0)
  })
})

// Issue #133 candidate 3: fractional CSS zoom on #root makes WebView2 round
// device pixels differently frame-to-frame — a ~1px dance of the whole client
// area. Quantize the persisted scale to hundredths and clamp it to the
// documented 50–300% range so zoom never carries subpixel noise.
describe('quantizeUiScale', () => {
  it('snaps to two decimal places', () => {
    expect(quantizeUiScale(1.234567)).toBe(1.23)
    expect(quantizeUiScale(0.999)).toBe(1)
  })

  it('clamps to the supported 0.5–3 range', () => {
    expect(quantizeUiScale(9)).toBe(3)
    expect(quantizeUiScale(0.1)).toBe(0.5)
  })

  it('passes through sane values untouched', () => {
    expect(quantizeUiScale(1.25)).toBe(1.25)
    expect(quantizeUiScale(1)).toBe(1)
  })

  it('falls back to 1 for garbage', () => {
    expect(quantizeUiScale(Number.NaN)).toBe(1)
    expect(quantizeUiScale(0)).toBe(1)
  })
})
