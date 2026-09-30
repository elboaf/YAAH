/**
 * Anti-jitter helpers (issue #133): the whole client area was reported
 * dancing by ~1px inside an otherwise still window frame. Two layout-level
 * candidates get hardened here:
 *
 * 1. Ticker tapes translate with `translateX(px)`; a fractional offset puts
 *    the line at a subpixel position that the rasterizer resolves
 *    differently between frames — a visible ~1px oscillation. `tapeOffsetPx`
 *    floors the offset to a whole pixel.
 * 2. A fractional `zoom` on #root makes device-pixel rounding oscillate at
 *    some DPI scales. `quantizeUiScale` snaps the persisted interface scale
 *    to hundredths and clamps to the documented 50–300% range.
 *
 * The third candidate (scrollbar gutter reflow) is pure CSS — see the
 * `scrollbar-gutter: stable` rule for the overflow scroll classes in
 * index.css.
 */

/** Whole-pixel horizontal offset for a ticker tape: how far left the tape
 *  must shift so its right edge meets the viewport's right edge. Returns 0
 *  when the tape fits. Always an integer, never positive. */
export function tapeOffsetPx(viewportWidth: number, tapeWidth: number): number {
  if (!(viewportWidth > 0) || !(tapeWidth > 0)) return 0
  return Math.min(0, Math.floor(viewportWidth - tapeWidth))
}

/** Quantized interface scale: finite, clamped to [0.5, 3], rounded to two
 *  decimals so CSS zoom never carries subpixel noise into device-pixel
 *  rounding. Garbage (NaN, 0, negatives beyond the clamp) falls back sanely
 *  via the clamp. */
export function quantizeUiScale(scale: number): number {
  const n = Number(scale)
  if (!Number.isFinite(n) || n <= 0) return 1
  return Math.min(3, Math.max(0.5, Math.round(n * 100) / 100))
}
