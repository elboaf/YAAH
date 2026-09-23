// Issue #50: images surface in-app instead of leaving the window.
// Two gaps closed here:
//  1. Chat attachment thumbnails used to shell out to the system browser
//     (openExternal) — now they open a full-res in-app lightbox.
//  2. Computer-use screenshots (and any tool result carrying a stored image
//     rel path) used to fall through to a raw JSON dump in the audit trail —
//     now they render the actual picture, clickable into the same viewer.

import { describe, expect, it, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { MessageView, ImageLightbox } from './components'
import { useAgent, type ChatMessage, type ToolCall } from './store'

const PNG_DATA_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='

const userMsg = (images: string[]): ChatMessage => ({
  id: 'u1',
  role: 'user',
  content: 'look at this',
  images,
})

const toolMsg = (tc: ToolCall): ChatMessage => ({
  id: 'a1',
  role: 'assistant',
  content: '',
  toolCalls: [tc],
})

/** Expand the finished turn's collapsed trace line AND its tool row so
 *  ToolCallRow bodies render (assistant messages render ToolCallRow nested
 *  inside TraceLine, both collapsed by default). */
function openTrace(container: HTMLElement) {
  fireEvent.click(screen.getByText(/1 call/))
  fireEvent.click(screen.getByText('[+]'))
  expect(container.textContent).toContain('args:')
}

beforeEach(() => {
  useAgent.setState({ lightboxSrc: null })
})

afterEach(() => {
  cleanup()
  useAgent.setState({ lightboxSrc: null })
})

describe('trace tool results with stored images (#50)', () => {
  it('a screenshot result renders the stored image, not a JSON dump', () => {
    const { container } = render(
      <MessageView
        msg={toolMsg({
          id: 't1',
          name: 'screenshot',
          result: { image: 'screenshots/abc.png', monitor: 1, origin: { x: 0, y: 0 } },
        })}
      />,
    )
    openTrace(container)
    const img = container.querySelector('img')
    expect(img).toBeTruthy()
    expect(img!.getAttribute('src')).toContain('/api/images/screenshots/abc.png')
    // The old failure mode: the raw result JSON as the only rendering.
    expect(container.textContent).not.toContain('"image"')
  })

  it('view_image keeps rendering (generalized branch, same mechanism)', () => {
    const { container } = render(
      <MessageView
        msg={toolMsg({
          id: 't2',
          name: 'view_image',
          result: { image: 'agent/pic.jpg', url: 'https://x/y.jpg' },
        })}
      />,
    )
    openTrace(container)
    const img = container.querySelector('img')
    expect(img).toBeTruthy()
    expect(img!.getAttribute('src')).toContain('/api/images/agent/pic.jpg')
  })

  it('an MCP result with images[] renders every stored image', () => {
    const { container } = render(
      <MessageView
        msg={toolMsg({
          id: 't3',
          name: 'mcp__vision__read',
          result: { result: 'two charts', images: ['mcp/1.png', 'mcp/2.png'] },
        })}
      />,
    )
    openTrace(container)
    const imgs = container.querySelectorAll('img')
    expect(imgs.length).toBe(2)
    expect(imgs[0].getAttribute('src')).toContain('/api/images/mcp/1.png')
    expect(imgs[1].getAttribute('src')).toContain('/api/images/mcp/2.png')
  })

  it('a result with image AND images[] renders both', () => {
    const { container } = render(
      <MessageView
        msg={toolMsg({
          id: 't4',
          name: 'weird_tool',
          result: { image: 'screenshots/main.png', images: ['screenshots/extra.png'] },
        })}
      />,
    )
    openTrace(container)
    expect(container.querySelectorAll('img').length).toBe(2)
  })

  it('a result without an image field still falls through to the JSON dump', () => {
    const { container } = render(
      <MessageView
        msg={toolMsg({
          id: 't5',
          name: 'bash',
          result: { error: 'command failed' },
        })}
      />,
    )
    openTrace(container)
    expect(container.querySelector('img')).toBeNull()
    expect(container.textContent).toContain('command failed')
  })

  it('an error result that happens to carry an image still renders the picture', () => {
    // The image check precedes the JSON dump regardless of other fields —
    // a failed capture that still stored a partial png stays inspectable.
    const { container } = render(
      <MessageView
        msg={toolMsg({
          id: 't6',
          name: 'screenshot',
          result: { image: 'screenshots/partial.png', error: 'stale monitor' },
        })}
      />,
    )
    openTrace(container)
    expect(container.querySelector('img')).toBeTruthy()
  })

  it('clicking a trace image opens the in-app lightbox', () => {
    const { container } = render(
      <>
        <MessageView
          msg={toolMsg({
            id: 't7',
            name: 'screenshot',
            result: { image: 'screenshots/click.png', monitor: 1 },
          })}
        />
        <ImageLightbox />
      </>,
    )
    openTrace(container)
    fireEvent.click(container.querySelector('img')!)
    const viewer = container.querySelector('img[alt="full-size image"]')
    expect(viewer).toBeTruthy()
    expect(viewer!.getAttribute('src')).toContain('/api/images/screenshots/click.png')
    expect(useAgent.getState().lightboxSrc).toBe('screenshots/click.png')
  })
})

describe('attached image thumbnails (#50)', () => {
  it('a stored attachment opens the lightbox in-app — no external hop', () => {
    const { container } = render(
      <>
        <MessageView msg={userMsg(['1/att.png'])} />
        <ImageLightbox />
      </>,
    )
    // The old implementation wrapped the thumbnail in <a target="_blank">.
    expect(container.querySelector('a')).toBeNull()
    fireEvent.click(container.querySelector('img')!)
    const viewer = container.querySelector('img[alt="full-size image"]')
    expect(viewer).toBeTruthy()
    expect(viewer!.getAttribute('src')).toContain('/api/images/1/att.png')
  })

  it('an optimistic (data URL) attachment opens the lightbox with the data URL', () => {
    const { container } = render(
      <>
        <MessageView msg={userMsg([PNG_DATA_URL])} />
        <ImageLightbox />
      </>,
    )
    fireEvent.click(container.querySelector('img')!)
    const viewer = container.querySelector('img[alt="full-size image"]')
    expect(viewer).toBeTruthy()
    expect(viewer!.getAttribute('src')).toBe(PNG_DATA_URL)
  })
})

describe('the lightbox itself (#50)', () => {
  it('esc closes it', () => {
    useAgent.setState({ lightboxSrc: 'screenshots/esc.png' })
    const { container } = render(<ImageLightbox />)
    expect(container.querySelector('img[alt="full-size image"]')).toBeTruthy()
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(useAgent.getState().lightboxSrc).toBeNull()
    expect(container.querySelector('img[alt="full-size image"]')).toBeNull()
  })

  it('clicking the backdrop closes it', () => {
    useAgent.setState({ lightboxSrc: 'screenshots/backdrop.png' })
    const { container } = render(<ImageLightbox />)
    const scrim = container.firstElementChild as HTMLElement
    fireEvent.pointerDown(scrim, { target: scrim })
    expect(useAgent.getState().lightboxSrc).toBeNull()
  })

  it('clicking the image zooms instead of closing', () => {
    useAgent.setState({ lightboxSrc: 'screenshots/zoom.png' })
    const { container } = render(<ImageLightbox />)
    const img = container.querySelector('img[alt="full-size image"]') as HTMLElement
    fireEvent.pointerDown(img, { target: img })
    // Zoomed to 2x: the zoom indicator shows it and the image stays open.
    expect(container.textContent).toContain('2x')
    expect(useAgent.getState().lightboxSrc).toBe('screenshots/zoom.png')
  })

  it('renders nothing when no image is open', () => {
    const { container } = render(<ImageLightbox />)
    expect(container.firstElementChild).toBeNull()
  })
})
