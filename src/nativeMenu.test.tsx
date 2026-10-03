// #287: the WebView2 default right-click menu (Back / Refresh / Save as /
// Print) is browser chrome, not Yaah UI. A document-level capture-phase gate
// suppresses it everywhere EXCEPT where its items are the right desktop
// affordances: editable fields (Paste lives there) and images (Save/Copy
// image). The transcript's custom selection menu (#276) already
// preventDefaults on qualifying selections and is untouched by this gate.

import { describe, expect, it, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { NativeMenuGate } from './nativeMenu'

function fireContextMenu(target: Element): Event {
  const event = new MouseEvent('contextmenu', {
    bubbles: true,
    cancelable: true,
    clientX: 10,
    clientY: 10,
  })
  target.dispatchEvent(event)
  return event
}

describe('NativeMenuGate (#287)', () => {
  afterEach(cleanup)

  it('suppresses the context menu on plain (non-editable, non-image) targets', () => {
    const { container } = render(<NativeMenuGate />)
    const div = document.createElement('div')
    container.appendChild(div)
    const span = document.createElement('span')
    div.appendChild(span)
    expect(fireContextMenu(span).defaultPrevented).toBe(true)
  })

  it('keeps the native menu inside a textarea (Paste etc. must survive)', () => {
    const { container } = render(<NativeMenuGate />)
    const ta = document.createElement('textarea')
    container.appendChild(ta)
    expect(fireContextMenu(ta).defaultPrevented).toBe(false)
  })

  it('keeps the native menu inside an input', () => {
    const { container } = render(<NativeMenuGate />)
    const input = document.createElement('input')
    container.appendChild(input)
    expect(fireContextMenu(input).defaultPrevented).toBe(false)
  })

  it('keeps the native menu inside a contenteditable', () => {
    const { container } = render(<NativeMenuGate />)
    const ce = document.createElement('div')
    ce.setAttribute('contenteditable', 'true')
    container.appendChild(ce)
    expect(fireContextMenu(ce).defaultPrevented).toBe(false)
  })

  it('keeps the native menu on an image (Save/Copy image are congruent)', () => {
    const { container } = render(<NativeMenuGate />)
    const img = document.createElement('img')
    container.appendChild(img)
    expect(fireContextMenu(img).defaultPrevented).toBe(false)
  })

  it('honors the closest() walk: a mark inside a paragraph is suppressed, a mark inside an editable is not', () => {
    const { container } = render(<NativeMenuGate />)
    const para = document.createElement('p')
    const mark = document.createElement('mark')
    para.appendChild(mark)
    container.appendChild(para)
    expect(fireContextMenu(mark).defaultPrevented).toBe(true)

    const editable = document.createElement('div')
    editable.setAttribute('contenteditable', 'true')
    const inner = document.createElement('mark')
    editable.appendChild(inner)
    container.appendChild(editable)
    expect(fireContextMenu(inner).defaultPrevented).toBe(false)
  })

  it('removes the listener on unmount', () => {
    const { container, unmount } = render(<NativeMenuGate />)
    const div = document.createElement('div')
    container.appendChild(div)
    unmount()
    expect(fireContextMenu(div).defaultPrevented).toBe(false)
  })
})
