import { useEffect, useRef, useState, useCallback } from 'react'
import { searchConversations, type SearchMatch } from '../api'

/**
 * Sidebar search box with classified results (#676).
 *
 * Queries the global search endpoint, classifies results by kind
 * (chat / tool / sub-agent), and lets the user open matching
 * conversations. Chat results are primary; tool and sub-agent
 * matches are grouped below with a muted style so the user can
 * opt into digging through them.
 */
export function SidebarSearch({
  onOpenConversation,
}: {
  onOpenConversation: (id: number) => void
}) {
  const [query, setQuery] = useState('')
  const [matches, setMatches] = useState<SearchMatch[]>([])
  const [loading, setLoading] = useState(false)
  const [searched, setSearched] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [error, setError] = useState<string | null>(null)

  const doSearch = useCallback(async (q: string) => {
    const trimmed = q.trim()
    if (!trimmed) {
      setMatches([])
      setSearched(false)
      setLoading(false)
      setError(null)
      return
    }

    setLoading(true)
    setError(null)
    try {
      const result = await searchConversations(trimmed, 10)
      setMatches(result.matches)
      setSearched(true)
    } catch (e) {
      setError(String((e as Error).message ?? e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => {
      doSearch(query)
    }, 300)
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
    }
  }, [query, doSearch])

  // Split results by kind for distinct sections
  const chatMatches = matches.filter((m) => m.kind === 'chat')
  const toolMatches = matches.filter((m) => m.kind === 'tool')
  const subagentMatches = matches.filter((m) => m.kind === 'sub-agent')

  return (
    <div className="lg-search border-b border-zinc-700/50 bg-zinc-900/50 px-1.5 py-1">
      {/* Search input */}
      <div className="flex items-center gap-1.5 border border-zinc-700 rounded px-2 py-1.5 focus-within:border-blue-500 bg-zinc-800">
        <svg
          width="14"
          height="14"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          aria-hidden="true"
          className="text-zinc-500 shrink-0"
        >
          <circle cx="7" cy="7" r="5" />
          <path d="M11 11l3 3" />
        </svg>
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search conversations…"
          aria-label="Search conversations"
          className="flex-1 bg-transparent text-xs text-zinc-200 outline-none placeholder:text-zinc-600"
        />
        {loading && (
          <span className="run-dots shrink-0 text-blue-400">
            <i /><i /><i />
          </span>
        )}
      </div>

      {/* Results */}
      {query.trim() && (
        <div className="mt-1.5 max-h-72 overflow-y-auto">
        {error && (
          <p className="px-2 py-1.5 text-xs text-red-400 bg-red-950/30 rounded">
            {error}
          </p>
        )}

        {loading && !searched && (
          <p className="px-2 py-3 text-xs text-zinc-500">Searching…</p>
        )}

        {searched && matches.length === 0 && !loading && (
          <p className="px-2 py-3 text-xs text-zinc-500">
            No matches for "{query}"
          </p>
        )}

        {chatMatches.length > 0 && (
          <div className="mb-1">
            <div className="px-2 py-0.5 text-[10px] font-semibold uppercase text-zinc-500">
              Chats
            </div>
            {chatMatches.map((match) => (
              <button
                key={`chat-${match.conversation_id}-${match.message_id}`}
                onClick={() => onOpenConversation(match.conversation_id)}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-zinc-800/60 transition-colors"
              >
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] font-semibold px-1 py-0.5 rounded bg-blue-900/60 text-blue-300 shrink-0">
                    chat
                  </span>
                  <span className="text-xs text-zinc-200 font-medium truncate">
                    {match.conversation_title}
                  </span>
                </div>
                <div className="mt-0.5 pl-6 text-[11px] text-zinc-500 line-clamp-2">
                  {match.excerpt}
                </div>
              </button>
            ))}
          </div>
        )}

        {toolMatches.length > 0 && (
          <div className="mb-1">
            <div className="px-2 py-0.5 text-[10px] font-semibold uppercase text-zinc-600">
              Tool output
            </div>
            {toolMatches.map((match) => (
              <button
                key={`tool-${match.conversation_id}-${match.message_id}`}
                onClick={() => onOpenConversation(match.conversation_id)}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-zinc-800/60 transition-colors"
              >
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] font-semibold px-1 py-0.5 rounded bg-amber-900/60 text-amber-300 shrink-0">
                    tool
                  </span>
                  <span className="text-xs text-zinc-400 truncate">
                    {match.conversation_title}
                  </span>
                </div>
                <div className="mt-0.5 pl-6 text-[11px] text-zinc-600 line-clamp-2">
                  {match.excerpt}
                </div>
              </button>
            ))}
          </div>
        )}

        {subagentMatches.length > 0 && (
          <div className="mb-1">
            <div className="px-2 py-0.5 text-[10px] font-semibold uppercase text-zinc-600">
              Sub-agents
            </div>
            {subagentMatches.map((match) => (
              <button
                key={`subagent-${match.conversation_id}-${match.message_id}`}
                onClick={() => onOpenConversation(match.conversation_id)}
                className="w-full text-left px-2 py-1.5 rounded hover:bg-zinc-800/60 transition-colors"
              >
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] font-semibold px-1 py-0.5 rounded bg-purple-900/60 text-purple-300 shrink-0">
                    agent
                  </span>
                  <span className="text-xs text-zinc-400 truncate">
                    {match.conversation_title}
                  </span>
                </div>
                <div className="mt-0.5 pl-6 text-[11px] text-zinc-600 line-clamp-2">
                  {match.excerpt}
                </div>
              </button>
            ))}
          </div>
        )}
      </div>
      )}
    </div>
  )
}
