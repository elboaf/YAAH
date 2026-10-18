// Generated images must appear in the tape the moment the tool result
// lands - not hidden behind the ToolCallRow [+] expander. The backend
// marks results display:"inline"; ToolCallRow renders them as an always-
// visible image grid (name-based fallback covers pre-marker results).
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  return actual
})

import { ToolCallRow } from './components'
import { useAgent, type ToolCall } from './store'

const tc = (over: Partial<ToolCall>): ToolCall => ({
  id: 'call_1',
  name: 'generate_image',
  args: { prompt: 'a cat' },
  ...over,
})

const inlineResult = {
  images: ['generate_image/abc.png'],
  path: 'ai-images/abc.png',
  saved_to: 'ai-images/abc.png',
  display: 'inline',
}

afterEach(() => cleanup())

describe('generate_image tape rendering', () => {
  it('shows the image in the tape without expanding anything', () => {
    render(<ToolCallRow tc={tc({ result: inlineResult })} />)
    const img = screen.getByAltText('generated image') as HTMLImageElement
    expect(img.src).toContain('/api/images/generate_image/abc.png')
  })

  it('shows where the file was saved', () => {
    render(<ToolCallRow tc={tc({ result: inlineResult })} />)
    expect(screen.getByText(/saved to ai-images\/abc\.png/)).toBeTruthy()
  })

  it('opens the lightbox on click instead of hiding behind [+] ', () => {
    render(<ToolCallRow tc={tc({ result: inlineResult })} />)
    fireEvent.click(screen.getByAltText('generated image'))
    expect(useAgent.getState().lightboxSrc).toBe('generate_image/abc.png')
  })

  it('renders upscale_image results inline (marker-driven, not name-gated)', () => {
    render(
      <ToolCallRow
        tc={tc({
          name: 'upscale_image',
          result: { ...inlineResult, path: 'ai-images/upscaled-abc.png', saved_to: 'ai-images/upscaled-abc.png' },
        })}
      />,
    )
    expect(screen.getByAltText('generated image')).toBeTruthy()
    expect(screen.getByText(/saved to ai-images\/upscaled-abc\.png/)).toBeTruthy()
  })

  it('renders pre-marker results (no display field) inline too', () => {
    const legacy = { ...inlineResult } as Record<string, unknown>
    delete legacy.display
    render(<ToolCallRow tc={tc({ result: legacy })} />)
    expect(screen.getByAltText('generated image')).toBeTruthy()
  })

  it('exposes args + result behind the [+] expander (audit parity)', () => {
    // The image IS the message, but the prompt that produced it must
    // stay inspectable like every other tool call.
    render(
      <ToolCallRow
        tc={tc({
          args: { prompt: 'a cat', width: 768, height: 768, negative_prompt: 'dogs' },
          result: inlineResult,
        })}
      />)
    expect(screen.queryByText(/"prompt"/)).toBeNull() // hidden until expanded
    fireEvent.click(screen.getByText('[+]'))
    expect(screen.getByText(/"prompt"/)).toBeTruthy()
    expect(screen.getByText(/a cat/)).toBeTruthy()
    expect(screen.getByText(/"negative_prompt": "dogs"/)).toBeTruthy()
    expect(screen.getByText(/"saved_to"/)).toBeTruthy() // full result JSON
    // image still inline after expanding
    expect(screen.getByAltText('generated image')).toBeTruthy()
  })

  it('the expander toggles closed again', () => {
    render(<ToolCallRow tc={tc({ result: inlineResult })} />)
    fireEvent.click(screen.getByText('[+]'))
    expect(screen.getByText(/"prompt"/)).toBeTruthy()
    fireEvent.click(screen.getByText('[-]'))
    expect(screen.queryByText(/"prompt"/)).toBeNull()
  })

  it('falls back to the collapsed JSON trace for error results', () => {
    render(
      <ToolCallRow
        tc={tc({ result: { error: 'image generation failed: HTTP 500' } })}
      />,
    )
    // No phantom image, and the error text lives behind the [+] expander
    // like every other tool result.
    expect(screen.queryByAltText('generated image')).toBeNull()
    fireEvent.click(screen.getByText('[+]'))
    expect(screen.getByText(/HTTP 500/)).toBeTruthy()
  })

  it('leaves non-image generate_image results untouched', () => {
    render(<ToolCallRow tc={tc({ result: { display: 'inline', error: 'nope' } })} />)
    expect(screen.queryByAltText('generated image')).toBeNull()
  })
})
