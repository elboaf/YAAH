// Chip expansion tests (#143): clicking a text-attachment chip toggles an
// inline, monospace, height-capped expansion; inline-content chips render
// from stored data with no network call; staged-path chips fetch lazily on
// first expand and a missing file shows a graceful not-found state.
// Run: npx vitest run src/attachmentChipExpand.test.tsx
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MessageView } from './components'

const { previewFile } = vi.hoisted(() => ({ previewFile: vi.fn() }))
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  previewFile,
}))

afterEach(() => {
  cleanup()
  previewFile.mockReset()
})

describe('attachment chip expand/collapse (#143)', () => {
  const inlineMsg = {
    id: 'm1',
    role: 'user' as const,
    content: 'please review',
    attachments: [{ name: 'notes.md', size: 8, content: '# hello\n' }],
  }
  const stagedMsg = {
    id: 'm2',
    role: 'user' as const,
    content: 'big one',
    attachments: [{ name: 'big.log', size: 204_800, path: '.yaah-attachments/big.log' }],
  }

  it('expands inline content on click with no fetch, and collapses on second click', () => {
    const { container } = render(<MessageView msg={inlineMsg} />)
    const chip = screen.getByText('notes.md')
    fireEvent.click(chip)
    const block = container.querySelector('[data-attachment-expand]')
    expect(block).toBeTruthy()
    expect(block!.textContent).toContain('# hello')
    expect(previewFile).not.toHaveBeenCalled()
    fireEvent.click(chip)
    expect(container.querySelector('[data-attachment-expand]')).toBeNull()
  })

  it('expanded block is monospace and height-capped with internal scroll', () => {
    const { container } = render(<MessageView msg={inlineMsg} />)
    fireEvent.click(screen.getByText('notes.md'))
    const block = container.querySelector('[data-attachment-expand]') as HTMLElement
    expect(block.className).toContain('font-mono')
    expect(block.className).toContain('overflow-y-auto')
    expect(block.className).toContain('max-h-')
  })

  it('fetches staged content lazily on first expand and caches it', async () => {
    previewFile.mockResolvedValue({
      path: '.yaah-attachments/big.log',
      total_lines: 3,
      start_line: 1,
      end_line: 3,
      content: 'line one\nline two\nline three\n',
      truncated: false,
    })
    const { container } = render(<MessageView msg={stagedMsg} />)
    const chip = screen.getByText('big.log')
    fireEvent.click(chip)
    expect(previewFile).toHaveBeenCalledTimes(1)
    await waitFor(() => {
      expect(container.querySelector('[data-attachment-expand]')!.textContent).toContain('line two')
    })
    // collapse + re-expand serves from cache: still exactly one fetch
    fireEvent.click(chip)
    expect(container.querySelector('[data-attachment-expand]')).toBeNull()
    fireEvent.click(chip)
    await waitFor(() => {
      expect(container.querySelector('[data-attachment-expand]')).toBeTruthy()
    })
    expect(previewFile).toHaveBeenCalledTimes(1)
  })

  it('shows a graceful not-found state when the staged file is gone', async () => {
    previewFile.mockRejectedValue(new Error('404'))
    const { container } = render(<MessageView msg={stagedMsg} />)
    fireEvent.click(screen.getByText('big.log'))
    await waitFor(() => {
      expect(container.textContent).toContain('file no longer exists')
    })
    expect(container.querySelector('[data-attachment-expand]')).toBeNull()
  })
})
