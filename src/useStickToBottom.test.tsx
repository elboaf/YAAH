// Tests for the #52 scroll discipline: a streaming transcript follows the
// bottom only while the READER is at (near) the bottom. Scrolling up to
// re-read history during a run must stick; `force` snaps back when input is
// required (question / approval / plan) even from scrolled-up; scrolling
// (back) near the bottom re-arms the follow.

import { describe, expect, it, beforeEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import type { ReactElement } from 'react'
import { useStickToBottom } from './useStickToBottom'

function Harness({ content, force }: { content: number; force?: boolean }) {
  const { containerRef, onScroll } = useStickToBottom(Boolean(force), [content])
  return (
    <div ref={containerRef} onScroll={onScroll} data-testid="container">
      <div style={{ height: content }} />
    </div>
  )
}

// jsdom does no layout: stub the scroll metrics the hook reads. scrollTop is
// shadowed with a plain own property — jsdom's real setter clamps against its
// (empty) layout, which would silently zero every assignment. Assigning it
// fires no scroll event, which keeps tests deterministic: only explicit
// fireEvent.scroll calls derive the pin, exactly like a user scroll.
const VIEWPORT = 100
const stubLayout = (el: HTMLElement, scrollHeight: number) => {
  if (!Object.getOwnPropertyDescriptor(el, 'scrollTop')) {
    Object.defineProperty(el, 'scrollTop', { configurable: true, writable: true, value: 0 })
  }
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => VIEWPORT })
}

const container = () => screen.getByTestId('container')

const scrollUp = (el: HTMLElement, to: number) => {
  act(() => {
    el.scrollTop = to
    fireEvent.scroll(el)
  })
}

const grow = (rerender: (ui: ReactElement) => void, height: number) => {
  stubLayout(container(), height)
  act(() => {
    rerender(<Harness content={height} />)
  })
}

beforeEach(() => {
  // Fresh jsdom document per test: no stale containers between cases.
  document.body.innerHTML = ''
})

describe('useStickToBottom', () => {
  it('follows streamed content while the reader is parked at the bottom', () => {
    const { rerender } = render(<Harness content={200} />)
    stubLayout(container(), 200)
    grow(rerender, 5_000)
    expect(container().scrollTop).toBe(5_000)
    grow(rerender, 9_000)
    expect(container().scrollTop).toBe(9_000)
  })

  it('sticks where the reader scrolled up — streamed chunks do not yank the view', () => {
    const { rerender } = render(<Harness content={200} />)
    const el = container()
    stubLayout(el, 5_000)
    grow(rerender, 5_000)
    expect(el.scrollTop).toBe(5_000) // pinned: followed the growth
    scrollUp(el, 1_200) // reader scrolled up to re-read
    grow(rerender, 9_000)
    expect(el.scrollTop).toBe(1_200) // view stays where the reader put it
    grow(rerender, 12_000)
    expect(el.scrollTop).toBe(1_200)
  })

  it('force snaps back to the newest content even from scrolled-up', () => {
    const { rerender } = render(<Harness content={200} />)
    const el = container()
    stubLayout(el, 5_000)
    grow(rerender, 5_000)
    scrollUp(el, 800)
    grow(rerender, 9_000)
    expect(el.scrollTop).toBe(800) // unpinned, stuck
    stubLayout(el, 9_000)
    act(() => {
      rerender(<Harness content={9_000} force />) // e.g. a question needs an answer
    })
    expect(el.scrollTop).toBe(9_000) // snapped to the latest
  })

  it('after a forced snap the pin is re-derived, not stuck on', () => {
    const { rerender } = render(<Harness content={200} />)
    const el = container()
    stubLayout(el, 5_000)
    grow(rerender, 5_000)
    scrollUp(el, 800)
    act(() => {
      rerender(<Harness content={5_000} force />)
    })
    expect(el.scrollTop).toBe(5_000) // forced snap
    grow(rerender, 9_000)
    // The question was answered; the reader had been scrolled up — streaming
    // must not resume yanking the view against their will.
    expect(el.scrollTop).toBe(5_000)
  })

  it('scrolling back near the bottom re-arms the follow', () => {
    const { rerender } = render(<Harness content={200} />)
    const el = container()
    stubLayout(el, 5_000)
    grow(rerender, 5_000)
    scrollUp(el, 3_000)
    grow(rerender, 8_000)
    expect(el.scrollTop).toBe(3_000) // detached
    // Reader returns to within the 50px tolerance of the bottom.
    stubLayout(el, 8_000)
    scrollUp(el, 8_000 - 100 - 30)
    grow(rerender, 10_000)
    expect(el.scrollTop).toBe(10_000) // following again
  })
})
