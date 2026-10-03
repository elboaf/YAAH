// #274 — dragging the interface-scale slider flickers between adjacent
// values: each onChange applies CSS zoom to #root, the zoom moves the
// slider's own hit geometry mid-drag, and the browser re-derives the
// native range value from the moved geometry — a self-sustaining loop
// that settles on release. Fix: snapshot the pointer→value mapping at
// pointerdown and drive the value from the FROZEN track rect while the
// pointer is down; the browser's geometry re-derived change events are
// ignored until pointerup. Live preview is kept (the frozen mapping is
// still evaluated on every pointermove, so zoom tracks the thumb).
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

import { InterfaceScaleCard } from './components'

afterEach(() => cleanup())

const getSlider = () =>
  screen.getByRole('slider', { name: /interface scale/i }) as HTMLInputElement

/** jsdom rects are all zeros — pin the track geometry so the frozen
 *  pointer→value mapping is deterministic: left=100, width=200 means
 *  clientX 200 maps to the midpoint, scale 1.5. */
function pinTrackRect(slider: HTMLInputElement) {
  slider.getBoundingClientRect = () =>
    ({ left: 100, top: 0, right: 300, bottom: 10, width: 200, height: 10, x: 100, y: 0, toJSON: () => ({}) }) as DOMRect
}

describe('interface-scale slider drag stability (#274)', () => {
  it('drives the value from the pointerdown rect during drag, ignoring re-derived change events', () => {
    const seen: number[] = []
    function Holder() {
      const [s, setS] = useState(1)
      return <InterfaceScaleCard scale={s} onChange={(v) => { seen.push(v); setS(v) }} />
    }
    render(<Holder />)
    const slider = getSlider()
    pinTrackRect(slider)

    // Grab the thumb: the pointer→value mapping freezes here.
    fireEvent.pointerDown(slider, { clientX: 100, clientY: 5 })

    // Mid-drag the pointer reaches the midpoint; the zoom from the previous
    // move has shifted the geometry, so the browser ALSO fires a change with
    // a re-derived (wrong) value. Only the frozen-mapping value may report.
    fireEvent.pointerMove(slider, { clientX: 200, clientY: 5 })
    fireEvent.change(slider, { target: { value: '1.03' } })
    expect(seen).toEqual([1.5])

    // The controlled value follows the frozen mapping, not the re-derivation.
    expect(slider.value).toBe('1.5')

    fireEvent.pointerUp(slider, { clientX: 200, clientY: 5 })
  })

  it('honors change events again after release (normal keyboard/pointer flow)', () => {
    const seen: number[] = []
    render(<InterfaceScaleCard scale={1} onChange={(s) => seen.push(s)} />)
    const slider = getSlider()
    pinTrackRect(slider)

    fireEvent.pointerDown(slider, { clientX: 100, clientY: 5 })
    fireEvent.pointerUp(slider, { clientX: 100, clientY: 5 })
    fireEvent.change(slider, { target: { value: '1.03' } })
    expect(seen).toEqual([1.03])
  })

  it('keeps reporting continuously along the frozen track (live preview intact)', () => {
    const seen: number[] = []
    render(<InterfaceScaleCard scale={1} onChange={(s) => seen.push(s)} />)
    const slider = getSlider()
    pinTrackRect(slider)

    fireEvent.pointerDown(slider, { clientX: 100, clientY: 5 })
    fireEvent.pointerMove(slider, { clientX: 150, clientY: 5 })
    fireEvent.pointerMove(slider, { clientX: 200, clientY: 5 })
    fireEvent.pointerMove(slider, { clientX: 300, clientY: 5 })
    expect(seen).toEqual([1.25, 1.5, 2])
    fireEvent.pointerUp(slider, { clientX: 300, clientY: 5 })
  })

  it('clamps frozen-mapping values to the slider range', () => {
    const seen: number[] = []
    render(<InterfaceScaleCard scale={1.5} onChange={(s) => seen.push(s)} />)
    const slider = getSlider()
    pinTrackRect(slider)

    fireEvent.pointerDown(slider, { clientX: 150, clientY: 5 })
    fireEvent.pointerMove(slider, { clientX: 40, clientY: 5 }) // left of track
    fireEvent.pointerMove(slider, { clientX: 900, clientY: 5 }) // right of track
    expect(seen).toEqual([1, 2])
    fireEvent.pointerUp(slider, { clientX: 900, clientY: 5 })
  })
})

describe("interface-scale slider drag teardown (CodeRabbit return trip)", () => {
  it("ends the drag on a window pointerup (capture lost / released off-element)", () => {
    const seen: number[] = []
    render(<InterfaceScaleCard scale={1} onChange={(s) => seen.push(s)} />)
    const slider = getSlider()
    pinTrackRect(slider)

    fireEvent.pointerDown(slider, { clientX: 100, clientY: 5, pointerId: 7 })
    fireEvent.pointerMove(slider, { clientX: 200, clientY: 5, pointerId: 7 })
    expect(seen).toEqual([1.5])

    // The zoom moved the track; release lands OUTSIDE the input — the
    // input never fires pointerup, the window does.
    fireEvent.pointerUp(window, { pointerId: 7 })
    fireEvent.change(slider, { target: { value: "1.03" } })
    expect(seen).toEqual([1.5, 1.03])
  })

  it("ignores window pointerup from a different pointer (drag stays frozen)", () => {
    const seen: number[] = []
    render(<InterfaceScaleCard scale={1} onChange={(s) => seen.push(s)} />)
    const slider = getSlider()
    pinTrackRect(slider)

    fireEvent.pointerDown(slider, { clientX: 100, clientY: 5, pointerId: 7 })
    fireEvent.pointerUp(window, { pointerId: 99 })
    fireEvent.change(slider, { target: { value: "1.03" } })
    expect(seen).toEqual([])
  })

  it("clears drag state when disabled flips mid-drag", () => {
    const seen: number[] = []
    function Holder({ disabled }: { disabled: boolean }) {
      return (
        <InterfaceScaleCard
          scale={1}
          disabled={disabled}
          onChange={(s) => seen.push(s)}
        />
      )
    }
    const { rerender } = render(<Holder disabled={false} />)
    const slider = getSlider()
    pinTrackRect(slider)
    fireEvent.pointerDown(slider, { clientX: 100, clientY: 5 })
    // Config load resolves mid-drag and flips the slider disabled; the
    // drag state must be wiped so a stale frozen rect can't survive it.
    rerender(<Holder disabled={true} />)
    rerender(<Holder disabled={false} />)
    fireEvent.change(slider, { target: { value: "1.03" } })
    expect(seen).toEqual([1.03])
  })
})
