// Issue #258: local, workspace-relative file paths in assistant chat render
// as clickable links that open the file in the existing PreviewModal.
//
// Detection is two-stage (syntactic + existence via the containment-checked
// /api/files/exists probe). These tests pin both stages, the suffix
// tolerance, the exclusion rules, and the acceptance cases named in the
// issue. The AgentMarkdown tests mock the existence probe so the linkified
// set is deterministic; the click handler is asserted through the real
// store's setPreviewPath.
//
// Run: npx vitest run src/pathLinks.test.tsx

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { AgentMarkdown } from './markdown'
import {
  tokenizePathCandidates,
  collectPathCandidates,
  resolveExistingPaths,
  clearPathCache,
} from './pathLinks'
import { useAgent } from './store'
import { checkFileExists } from './api'

vi.mock('./api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('./api')>()
  return { ...orig, checkFileExists: vi.fn() }
})

const existsMock = vi.mocked(checkFileExists)

beforeEach(() => {
  clearPathCache()
  existsMock.mockReset()
  useAgent.setState({ workspace: 'C:/tmp/ws', previewPath: null })
})

// ------------------------------------------------------------- stage 1: syntax

describe('tokenizePathCandidates (syntactic stage)', () => {
  it('tokenizes a plain relative path in prose', () => {
    const toks = tokenizePathCandidates('Fixed the bug in src/app.ts today')
    expect(toks).toHaveLength(1)
    expect(toks[0].path).toBe('src/app.ts')
  })

  it('peels trailing sentence punctuation off the path', () => {
    for (const s of ['edit src/app.ts.', 'edit src/app.ts,', '(edit src/app.ts)']) {
      const toks = tokenizePathCandidates(s)
      expect(toks).toHaveLength(1)
      expect(toks[0].path).toBe('src/app.ts')
    }
  })

  it('parses :line and :line:col suffixes', () => {
    const toks = tokenizePathCandidates('look at src/app.ts:42 and src/main.tsx:7:3')
    expect(toks.map((t) => t.path)).toEqual(['src/app.ts', 'src/main.tsx'])
    expect(toks[0].line).toBe(42)
    expect(toks[1].line).toBe(7)
  })

  it('parses a #L42 suffix', () => {
    const toks = tokenizePathCandidates('see src/app.ts#L42')
    expect(toks).toHaveLength(1)
    expect(toks[0].path).toBe('src/app.ts')
    expect(toks[0].line).toBe(42)
  })

  it('rejects URLs, bare domains and host:port shapes', () => {
    for (const s of [
      'see https://example.com/x/y.ts here',
      'go to www.example.com/a/b.ts now',
      'open example.com/a/b.ts please',
      'at localhost:8765/a/b.ts now',
    ]) {
      expect(tokenizePathCandidates(s)).toHaveLength(0)
    }
  })

  it('rejects absolute paths (deferred scope)', () => {
    expect(tokenizePathCandidates('open C:\\code\\app.ts now')).toHaveLength(0)
    expect(tokenizePathCandidates('open /usr/local/app.ts now')).toHaveLength(0)
    expect(tokenizePathCandidates('open \\\\server\\share\\app.ts now')).toHaveLength(0)
  })

  it('rejects separator-less tokens and bare numbers', () => {
    expect(tokenizePathCandidates('the Makefile is fine')).toHaveLength(0)
    expect(tokenizePathCandidates('version 1.2.3 shipped')).toHaveLength(0)
  })

  it('never re-matches interior segments of an accepted token', () => {
    const toks = tokenizePathCandidates('src/app.ts')
    expect(toks).toHaveLength(1)
    expect(toks[0].path).toBe('src/app.ts')
  })
})

describe('collectPathCandidates (markdown-aware scan)', () => {
  it('skips fenced code blocks entirely', () => {
    const raw = '```ts\nconst p = "src/app.ts"\n```\nplain src/app.ts mention'
    expect(collectPathCandidates(raw)).toEqual(['src/app.ts'])
  })

  it('masks inline code spans', () => {
    const raw = 'keep `src/hidden.ts` but linkify src/app.ts'
    expect(collectPathCandidates(raw)).toEqual(['src/app.ts'])
  })

  it('does not treat markdown link targets or labels as candidates', () => {
    expect(collectPathCandidates('[label](src/app.ts)')).toEqual([])
  })

  it('returns the union of candidates across lines', () => {
    const raw = 'first src/a.ts\n\nthen src/b.ts:3 then src/c.ts'
    expect(collectPathCandidates(raw)).toEqual(['src/a.ts', 'src/b.ts', 'src/c.ts'])
  })
})

// ------------------------------------------------------- stage 2: existence

describe('resolveExistingPaths (existence stage)', () => {
  it('keeps only paths the probe confirms', async () => {
    existsMock.mockImplementation((ws, p) =>
      Promise.resolve(p === 'src/app.ts'),
    )
    const got = await resolveExistingPaths('C:/tmp/ws', ['src/app.ts', 'src/missing.ts'])
    expect(got.has('src/app.ts')).toBe(true)
    expect(got.has('src/missing.ts')).toBe(false)
    expect(existsMock).toHaveBeenCalledWith('C:/tmp/ws', 'src/missing.ts')
  })

  it('treats probe failures (backend down) as "not a link"', async () => {
    existsMock.mockRejectedValue(new Error('Backend is unreachable'))
    const got = await resolveExistingPaths('C:/tmp/ws', ['src/app.ts'])
    expect(got.size).toBe(0)
  })

  it('does not probe when there is no workspace', async () => {
    const got = await resolveExistingPaths(null, ['src/app.ts'])
    expect(got.size).toBe(0)
    expect(existsMock).not.toHaveBeenCalled()
  })

  it('caches results per workspace (TTL) so repaints do not refetch', async () => {
    existsMock.mockResolvedValue(true)
    await resolveExistingPaths('C:/tmp/ws', ['src/app.ts'])
    await resolveExistingPaths('C:/tmp/ws', ['src/app.ts'])
    expect(existsMock).toHaveBeenCalledTimes(1)
    // A different workspace is a different cache key.
    await resolveExistingPaths('C:/tmp/other', ['src/app.ts'])
    expect(existsMock).toHaveBeenCalledTimes(2)
  })
})

// -------------------------------------------- render + click (acceptance)

function renderMd(content: string) {
  return render(<AgentMarkdown content={content} />)
}

describe('AgentMarkdown path linkification (#258 acceptance)', () => {
  it('renders an existing workspace-relative path as a link that opens the preview', async () => {
    existsMock.mockResolvedValue(true)
    const { container } = renderMd('Fixed the import in src/app.ts now')
    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'src/app.ts' })).toBeInTheDocument()
    })
    const link = screen.getByRole('link', { name: 'src/app.ts' })
    expect(link).toHaveAttribute('data-chat-file-link', 'src/app.ts')
    fireEvent.click(link)
    await waitFor(() => {
      expect(useAgent.getState().previewPath).toBe('src/app.ts')
    })
    void container
  })

  it('keeps the :42 / #L42 suffix visible on the label, opens the bare path', async () => {
    existsMock.mockResolvedValue(true)
    renderMd('see src/app.ts:42 and src/other.ts#L7')
    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'src/app.ts:42' })).toBeInTheDocument()
    })
    expect(screen.getByRole('link', { name: 'src/other.ts#L7' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'src/app.ts:42' }))
    await waitFor(() => {
      expect(useAgent.getState().previewPath).toBe('src/app.ts')
    })
  })

  it('renders a path-shaped token that does not exist as plain text', async () => {
    existsMock.mockResolvedValue(false)
    renderMd('planned refactor of src/app.ts soon')
    await waitFor(() => {
      expect(existsMock).toHaveBeenCalled()
    })
    expect(screen.queryByRole('link', { name: /src\/app\.ts/ })).not.toBeInTheDocument()
    expect(screen.getByText(/src\/app\.ts/)).toBeInTheDocument()
  })

  it('never linkifies inside fenced code blocks or inline code spans', async () => {
    existsMock.mockResolvedValue(true)
    renderMd('```\nsrc/app.ts\n```\n\nand `src/app.ts` inline')
    // The scan never yields a candidate from fence or span interiors, so no
    // probe fires and both occurrences stay plain text.
    await waitFor(() => {
      expect(document.querySelector('code')).toBeInTheDocument()
    })
    expect(existsMock).not.toHaveBeenCalled()
    expect(screen.queryByRole('link', { name: 'src/app.ts' })).not.toBeInTheDocument()
    expect(screen.getAllByText('src/app.ts').length).toBe(2)
  })

  it('leaves markdown http links and javascript: neutralization untouched', async () => {
    existsMock.mockResolvedValue(true)
    renderMd('[docs](https://example.com/x) and [bad](javascript:alert(1))')
    const link = screen.getByRole('link', { name: 'docs' })
    expect(link).toHaveAttribute('href', 'https://example.com/x')
    expect(link).toHaveAttribute('target', '_blank')
    expect(screen.queryByRole('link', { name: 'bad' })).not.toBeInTheDocument()
  })

  it('does not upgrade the link after a workspace switch resolves empty', async () => {
    existsMock.mockReset()
    existsMock.mockResolvedValue(false)
    const { rerender } = render(<AgentMarkdown content="look at src/app.ts" />)
    await waitFor(() => expect(existsMock).toHaveBeenCalled())
    useAgent.setState({ workspace: 'C:/tmp/other' })
    rerender(<AgentMarkdown content="look at src/app.ts" />)
    await waitFor(() => expect(existsMock).toHaveBeenCalledTimes(2))
    expect(screen.queryByRole('link', { name: 'src/app.ts' })).not.toBeInTheDocument()
    cleanup()
  })

  it('strips <say> briefings before scanning (no phantom candidates)', async () => {
    existsMock.mockResolvedValue(true)
    renderMd('Answer. <say>open src/secret-plan.ts quietly</say>')
    await waitFor(() => {
      // The briefing's path was never probed: it is stripped pre-scan.
      expect(existsMock).not.toHaveBeenCalled()
    })
    expect(screen.queryByText(/secret-plan/)).not.toBeInTheDocument()
  })

  it('links the :42 occurrence once the bare path is confirmed (probes stay stripped)', async () => {
    existsMock.mockImplementation((_ws, p) => Promise.resolve(p === 'src/app.ts'))
    renderMd('edited src/app.ts earlier; see src/app.ts:42 now')
    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'src/app.ts:42' })).toBeInTheDocument()
    })
    // Candidates are existence-probed by their stripped path only — a
    // "src/app.ts:42" file never exists, so probing the raw form would
    // leave every suffixed mention unlinked.
    expect(existsMock.mock.calls.map((c) => c[1])).toEqual(['src/app.ts'])
    // Both occurrences linkify; each opens the bare, suffix-free path.
    expect(screen.getByRole('link', { name: 'src/app.ts' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'src/app.ts:42' }))
    await waitFor(() => {
      expect(useAgent.getState().previewPath).toBe('src/app.ts')
    })
  })
})
