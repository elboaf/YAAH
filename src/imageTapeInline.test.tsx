// Issue #401 regression tests: a finished generate_image / upscale_image result
// must render the artwork INLINE in the message flow (like an answered
// ask_user card), with the audit chip + [+] expander carrying the prompt -
// not hidden behind the collapsed "1 call" TraceLine, which is where the
// user actually hit it (85fc84a made ToolCallRow inline, but MessageView
// only ever renders ToolCallRow inside TraceLine's expand-on-demand list).

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return actual
})

import { MessageView } from './components'
import { useAgent, type ChatMessage, type ToolCall } from './store'

const genResult = {
  images: ['generate_image/abc.png'],
  path: 'ai-images/abc.png',
  saved_to: 'ai-images/abc.png',
  display: 'inline',
}

const agentMsg = (over: Partial<ChatMessage>): ChatMessage => ({
  id: 'a1',
  role: 'assistant',
  content: '',
  ...over,
})

const genCall = (over: Partial<ToolCall>): ToolCall => ({
  id: 't1',
  name: 'generate_image',
  args: { prompt: 'a vivid green gecko lizard on a branch' },
  ...over,
})

const msg = () =>
  agentMsg({
    content: "Here's your lizard - a green gecko on a mossy branch.",
    toolCalls: [genCall({ result: genResult, contentOffset: 0 })],
  })

afterEach(() => {
  cleanup()
  useAgent.setState({ lightboxSrc: null })
})

describe('generated images anchor inline in the message (#401)', () => {
  it('the image renders in the tape without expanding the trace', () => {
    const { container } = render(<MessageView msg={msg()} />)
    const img = container.querySelector('img[alt="generated image"]')
    expect(img).toBeTruthy()
    expect(img!.getAttribute('src')).toContain('/api/images/generate_image/abc.png')
  })

  it('the emission text renders alongside the image', () => {
    const { container } = render(<MessageView msg={msg()} />)
    expect(container.textContent).toContain("Here's your lizard")
  })

  it('the audit chip stays: prompt hidden until the expander opens', () => {
    const { container } = render(<MessageView msg={msg()} />)
    expect(container.textContent).toContain('generate_image')
    expect(container.textContent).not.toContain('"prompt"')
    // The [+] span sits inside the chip button (whose textContent also
    // carries the chip label); clicking the span bubbles to the button.
    const expander = Array.from(container.querySelectorAll('span')).find(
      (b) => b.textContent === '[+]',
    )
    expect(expander).toBeTruthy()
    fireEvent.click(expander!)
    expect(container.textContent).toContain('"prompt"')
    expect(container.textContent).toContain('a vivid green gecko lizard')
    // the image stays inline after expanding
    expect(container.querySelector('img[alt="generated image"]')).toBeTruthy()
  })

  it('mid-run (no result yet) nothing anchors - the ticker chip runs', () => {
    const { container } = render(
      <MessageView
        live
        msg={agentMsg({
          content: ' Generating',
          toolCalls: [genCall({ contentOffset: 0 })],
        })}
      />,
    )
    expect(container.querySelector('img[alt="generated image"]')).toBeNull()
    expect(container.textContent).toContain('generate_image')
  })

  it('a failed generation anchors nothing and shows no phantom image', () => {
    const { container } = render(
      <MessageView
        msg={agentMsg({
          content: 'trying that again',
          toolCalls: [genCall({ result: { error: 'HTTP 500' }, contentOffset: 0 })],
        })}
      />,
    )
    expect(container.querySelector('img[alt="generated image"]')).toBeNull()
  })

  it('legacy rows without a contentOffset still render via the trace', () => {
    const { container } = render(
      <MessageView
        msg={agentMsg({
          content: 'old row',
          toolCalls: [genCall({ result: genResult })],
        })}
      />,
    )
    expect(container.textContent).toContain('generate_image')
    // trace expander still reaches the inline image render
    fireEvent.click(container.querySelector('button')!)
    expect(container.querySelector('img[alt="generated image"]')).toBeTruthy()
  })

  it('upscale_image results anchor inline too', () => {
    const { container } = render(
      <MessageView
        msg={agentMsg({
          content: 'bigger version',
          toolCalls: [
            genCall({
              name: 'upscale_image',
              result: { ...genResult, images: ['generate_image/big.png'] },
              contentOffset: 0,
            }),
          ],
        })}
      />,
    )
    expect(container.querySelector('img[alt="generated image"]')).toBeTruthy()
  })
})
