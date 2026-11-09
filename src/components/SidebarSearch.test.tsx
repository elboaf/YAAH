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
    vi.useFakeTimers()
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.useRealTimers()
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
    await vi.advanceTimersByTimeAsync(350)
    expect(searchConversations).toHaveBeenCalledWith('schema', 10)
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
    await vi.advanceTimersByTimeAsync(350)
    await waitFor(() => expect(screen.getByText('Chats')).toBeInTheDocument())
    expect(screen.getByText('Chat A')).toBeInTheDocument()
    expect(screen.getByText('chat')).toBeInTheDocument()
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
    await vi.advanceTimersByTimeAsync(350)
    await waitFor(() => expect(screen.getByText('Tool output')).toBeInTheDocument())
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
    await vi.advanceTimersByTimeAsync(350)
    await waitFor(() => expect(screen.getByText('Sub-agents')).toBeInTheDocument())
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
    await vi.advanceTimersByTimeAsync(350)
    await waitFor(() => expect(screen.getByText('Target')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Target'))
    expect(onOpen).toHaveBeenCalledWith(42)
  })

  it('shows loading state', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise(() => {}))
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'loading' } })
    await vi.advanceTimersByTimeAsync(350)
    expect(screen.getByText('Searching…')).toBeInTheDocument()
  })

  it('shows no-results state', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockResolvedValue({ count: 0, matches: [], truncated: false })
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'nothing' } })
    await vi.advanceTimersByTimeAsync(350)
    await waitFor(() => expect(screen.getByText('No matches for "nothing"')).toBeInTheDocument())
  })

  it('shows error state', async () => {
    ;(searchConversations as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Backend unreachable'))
    render(<SidebarSearch onOpenConversation={vi.fn()} />)
    const input = screen.getByPlaceholderText('Search conversations…')
    fireEvent.change(input, { target: { value: 'error' } })
    await vi.advanceTimersByTimeAsync(350)
    await waitFor(() => expect(screen.getByText('Backend unreachable')).toBeInTheDocument())
  })
})
