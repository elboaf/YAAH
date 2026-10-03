// #275 feedback loop: a live text selection must survive streamed deltas.
//
// PR #281 made the SEGMENT keys stable, but the selection lives deeper: in
// the text nodes AgentMarkdown renders INSIDE each segment. If a delta
// re-render replaces that DOM, the browser selection (a DOM Range) dies or
// re-anchors even though every key matched. These tests pin the user-visible
// contract: anchor a selection, append one delta, re-render, and the same
// text must still be selected.
//
// Symptom 1 ("selection clears itself"): any delta-driven unmount/remount of
// the selected text's DOM.

import { describe, expect, it, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { AgentMarkdown } from './markdown'

const sel = () => document.getSelection()!

function selectInFirstParagraph(container: HTMLElement, start: number, end: number) {
  // StreamText (#275) wraps plain runs in a data-stream-text span that owns
  // its text node imperatively; the selection anchors there.
  const span = container.querySelector('p [data-stream-text]') ?? container.querySelector('p')
  const text = span!.firstChild!
  expect(text.nodeType).toBe(3) // a text node: the selection anchors into it
  const range = document.createRange()
  range.setStart(text, start)
  range.setEnd(text, end)
  sel().removeAllRanges()
  sel().addRange(range)
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('#275: selection survives streamed deltas', () => {
  it('survives a delta appended to the selected paragraph', () => {
    const { container, rerender } = render(<AgentMarkdown content="first paragraph stays here" />)
    selectInFirstParagraph(container, 2, 10)
    expect(sel().type).toBe('Range')
    expect(sel().toString()).toBe('rst para')

    // One streamed delta lands at the tail of the SAME paragraph.
    rerender(<AgentMarkdown content="first paragraph stays here plus more streamed tail" />)

    expect(sel().type).toBe('Range')
    expect(sel().toString()).toBe('rst para')
  })

  it('survives a delta that adds a new paragraph after the selection', () => {
    const { container, rerender } = render(<AgentMarkdown content="first paragraph stays here" />)
    selectInFirstParagraph(container, 2, 10)
    expect(sel().toString()).toBe('rst para')

    rerender(<AgentMarkdown content="first paragraph stays here\n\nbrand new second paragraph" />)

    expect(sel().type).toBe('Range')
    expect(sel().toString()).toBe('rst para')
  })

  it('survives a delta appended to a LATER paragraph while the selection sits in an earlier one', () => {
    const { container, rerender } = render(
      <AgentMarkdown content={'early paragraph here\n\nlater paragraph tail'} />,
    )
    selectInFirstParagraph(container, 2, 10)
    expect(sel().toString()).toBe('rly para')

    rerender(
      <AgentMarkdown content={'early paragraph here\n\nlater paragraph tail and more of it'} />,
    )

    expect(sel().type).toBe('Range')
    expect(sel().toString()).toBe('rly para')
  })

  it('selection extends over streamed tail growth: boundary at the growing edge keeps counting new text', () => {
    // The reader anchored mid-sentence and the stream keeps appending to the
    // same paragraph: the anchor offset must not move (the text before it is
    // untouched), and a boundary AT the old end stays a live boundary.
    const { container, rerender } = render(<AgentMarkdown content="growing" />)
    selectInFirstParagraph(container, 0, 7)
    expect(sel().toString()).toBe('growing')

    rerender(<AgentMarkdown content="growing tail" />)

    expect(sel().type).toBe('Range')
    expect(sel().toString()).toBe('growing')
  })

  it('survives a delta growing text inside bold mid-selection', () => {
    // p children: [span 'keep '][strong[span 'bo']][span ' selected text']
    const { rerender } = render(<AgentMarkdown content="keep **bo** selected text" />)
    const spans = document.querySelectorAll('p [data-stream-text]')
    expect(spans.length).toBe(3)
    const range = document.createRange()
    range.setStart(spans[1].firstChild!, 0)
    range.setEnd(spans[2].firstChild!, 9)
    sel().removeAllRanges()
    sel().addRange(range)
    expect(sel().toString()).toBe('bo selected')

    // The bold span itself grows: 'bo' -> 'bold and more' (a pure append).
    rerender(<AgentMarkdown content="keep **bold and more** selected text" />)

    expect(sel().type).toBe('Range')
    // The anchor inside <strong> stayed at 0; the tail anchor stayed at 9.
    expect(sel().toString()).toBe('bold and more selected')
  })

  it('survives a whole MESSAGES worth of streamed deltas (stream simulation)', () => {
    // The acceptance-criteria scenario: tokens arriving while the reader
    // drags. Ten deltas, selection re-anchored before each render batch.
    const { rerender } = render(<AgentMarkdown content="The answer is" />)
    const select = () => {
      const span = document.querySelector('p [data-stream-text]')!
      const range = document.createRange()
      range.setStart(span.firstChild!, 5)
      range.setEnd(span.firstChild!, 11)
      sel().removeAllRanges()
      sel().addRange(range)
    }
    select()
    expect(sel().toString()).toBe('nswer ')
    const tails = [' The', ' full', ' pieced', ' together', ' answer', ' with', ' many', ' streamed', ' deltas', ' arriving']
    let expect_ = 0 // the offset window re-derives from the final text
    for (const tail of tails) {
      rerender(<AgentMarkdown content={`The answer is${tail}`} />)
      select() // re-drag (the reader keeps holding the button on new text)
      expect(sel().type).toBe('Range')
      expect_++
    }
    expect(expect_).toBe(tails.length)
    expect(sel().toString()).toBe('nswer ')
  })
})
