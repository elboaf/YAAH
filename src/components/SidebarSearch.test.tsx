import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

const { searchConversations } = vi.hoisted(() => ({
  searchConversations: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    searchConversations,
  }
})

import { SidebarSearch } from './SidebarSearch'

const mockMatches = [
  { kind: 'chat', conversation_id: 1, conversation_title: 'Chat A', message_id: 10, excerpt: 'schema design chat', score: 1.0 },
  { kind: 'chat', conversation_id: 2, conversation_title: 'Chat B', message_id: 20, excerpt: 'auth flow chat', score: 0.9 },
  { kind: 'tool', conversation_id: 3, conversation_title: 'Tool Chat', message_id: 30, excerpt: 'grep output found', score: 0.8 },
  { kind: 'sub-agent', conversation_id: 4, conversation_title: 'Agent Chat', message_id: 40, excerpt: 'explore codebase', score: 0.7 },
]

describe('SidebarSearch (#676)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows search input on mount', () => {
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    expect(screen.getByPlaceholderText('Search conversations…')).toBeInTheDocument()
  })

  it('shows no results until query is typed', () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({ count: 1, matches: mockMatches.slice(0, 1), truncated: false })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    expect(searchConversations).not.toHaveBeenCalled()
    expect(screen.queryByRole('list')).not.toBeInTheDocument()
  })

  it('triggers search after debounce', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({ count: 2, matches: mockMatches.slice(0, 2), truncated: false })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'schema' } })
    await waitFor(() => expect(searchConversations).toHaveBeenCalledWith('schema', 10), { timeout: 1000 })
  })

  it('renders classified chat results with kind badge', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({
      count: 2,
      matches: mockMatches.filter((m) => m.kind === 'chat'),
      truncated: false,
    })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'test' } })
    await waitFor(() => expect(screen.getByText('Chats')).toBeInTheDocument(), { timeout: 1000 })
    expect(screen.getByText('Chat A')).toBeInTheDocument()
    expect(screen.getAllByText('chat').length).toBeGreaterThanOrEqual(1)
  })

  it('renders tool output in separate section', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({
      count: 1,
      matches: mockMatches.filter((m) => m.kind === 'tool'),
      truncated: false,
    })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'grep' } })
    await waitFor(() => expect(screen.getByText('Tool output')).toBeInTheDocument(), { timeout: 1000 })
    expect(screen.getByText('tool')).toBeInTheDocument()
  })

  it('renders sub-agent results in separate section', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({
      count: 1,
      matches: mockMatches.filter((m) => m.kind === 'sub-agent'),
      truncated: false,
    })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'explore' } })
    await waitFor(() => expect(screen.getByText('Sub-agents')).toBeInTheDocument(), { timeout: 1000 })
    expect(screen.getByText('agent')).toBeInTheDocument()
  })

  it('navigates to conversation on result click', async () => {
    const onOpen = vi.fn()
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({
      count: 1,
      matches: [{ kind: 'chat', conversation_id: 42, conversation_title: 'Target', message_id: 100, excerpt: 'found it', score: 1.0 }],
      truncated: false,
    })
    render(<SidebarSearch onOpenConversation={onOpen} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'found' } })
    await waitFor(() => expect(screen.getByText('Target')).toBeInTheDocument(), { timeout: 1000 })
    fireEvent.click(screen.getByText('Target'))
    expect(onOpen).toHaveBeenCalledWith(42)
  })

  it('shows loading state while search is in progress', async () => {
    let resolvePromise: () => void
    const neverResolves = new Promise<void>((resolve) => { resolvePromise = resolve })
    ;(searchConversations as ReturnType<typeof vi.fn>).mockImplementation(async () => {
      await neverResolves
      return { count: 0, matches: [], truncated: false }
    })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'loading' } })
    // Wait for debounce + loading state to appear
    await waitFor(() => expect(screen.getByText('Searching…')).toBeInTheDocument(), { timeout: 1000 })
    resolvePromise!()
  })

  it('shows no-results state', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({ count: 0, matches: [], truncated: false })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'nothing' } })
    await waitFor(() => expect(screen.getByText('No matches for "nothing"')).toBeInTheDocument(), { timeout: 1000 })
  })

  it('shows error state', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Backend unreachable'))
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'error' } })
    await waitFor(() => expect(screen.getByText('Backend unreachable')).toBeInTheDocument(), { timeout: 1000 })
  })
})
