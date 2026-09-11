import { create } from 'zustand'

export type Role = 'user' | 'assistant' | 'tool' | 'system'

export interface ToolCall {
  id: string
  name: string
  args?: unknown
  result?: unknown
}

export interface ChatMessage {
  id: string
  role: Role
  content: string
  /** Stored image rel paths (backend/data/images/...), rendered via imageUrl(). */
  images?: string[]
  toolCalls?: ToolCall[]
}

export type AgentStatus = 'idle' | 'thinking' | 'running-tool' | 'error'

/** A live ask_user question waiting for the user's answer. */
export interface PendingQuestion {
  callId: string
  question: string
  options: Array<{ label: string; description?: string }>
  /** Buffer the asking turn streams into; the card renders only when that
   *  conversation is on screen (a hidden turn's question must not leak). */
  convKey: string
}

/** One line in the right-panel activity log. */
export interface LogEntry {
  id: number
  kind: 'tool' | 'system'
  time: string
  name?: string
  args?: unknown
  result?: unknown
}

interface AgentState {
  conversationId: number | null
  /**
   * Per-conversation message buffers, keyed by conversation id with 'draft'
   * for the unsaved new chat. A stream in flight writes into the buffer it
   * captured at send time, so switching conversations mid-turn is safe:
   * the hidden turn keeps streaming into its own buffer (Q11: free switching).
   */
  messagesByConv: Record<string, ChatMessage[]>
  /** Key of the buffer on screen: conversationId ?? 'draft'. */
  bufferKey: () => string
  status: AgentStatus
  error: string | null
  workspace: string
  log: LogEntry[]
  /** File currently open in the preview side panel (Q44). */
  previewPath: string | null
  setPreviewPath: (p: string | null) => void

  /** Question the agent is currently waiting on (null = none). */
  pendingQuestion: PendingQuestion | null
  setPendingQuestion: (q: PendingQuestion | null | ((prev: PendingQuestion | null) => PendingQuestion | null)) => void

  setWorkspace: (ws: string) => void
  newConversation: () => void
  setConversationId: (id: number) => void
  /** First send of a new chat: re-key the live 'draft' buffer to the real
   * conversation id and follow it on screen, in one atomic update. */
  adoptDraft: (id: number) => void
  setStatus: (s: AgentStatus) => void
  setError: (e: string | null) => void
  pushLog: (e: Omit<LogEntry, 'id' | 'time'>) => void
  clearLog: () => void
  abortController: AbortController | null
  setAbortController: (c: AbortController | null) => void

  appendUserMessage: (key: string, text: string, images?: string[]) => string
  appendAssistantPlaceholder: (key: string) => string
  appendTextDelta: (key: string, msgId: string, text: string) => void
  /** Remove one optimistic message (failed-send rollback). */
  removeMessage: (key: string, msgId: string) => void
  startToolCall: (key: string, msgId: string, callId: string, name: string, args: unknown) => void
  finishToolCall: (key: string, msgId: string, callId: string, result: unknown) => void

  /** Load a conversation's persisted history into its buffer. */
  loadHistory: (
    convId: number,
    rows: Array<{
      id: number
      role: string
      content: string
      images?: string[] | null
      tool_call_id?: string | null
      tool_calls: Array<{
        id?: string
        function?: { name?: string; arguments?: string }
      }> | null
    }>,
  ) => void
}

let nextId = 1
const genId = () => `m${nextId++}`
let nextLogId = 1

const now = () =>
  new Date().toLocaleTimeString([], { hour12: false })

// The workspace is remembered across restarts: seeded synchronously from
// localStorage so the field is correct on first paint, then mirrored to the
// backend's config.json (the durable copy) whenever it changes.
// '' is the Default pseudo-workspace (no root directory).
const WORKSPACE_KEY = 'agent.workspace'
const DEFAULT_WORKSPACE = ''

function loadStoredWorkspace(): string {
  if (typeof localStorage === 'undefined') return DEFAULT_WORKSPACE
  try {
    const ws = localStorage.getItem(WORKSPACE_KEY)
    // Legacy value '.' meant "no workspace" too.
    return ws && ws !== '.' ? ws : ''
  } catch {
    return DEFAULT_WORKSPACE
  }
}

/** Persist the workspace: localStorage immediately, backend config.json async. */
export function persistWorkspace(ws: string): void {
  if (typeof localStorage !== 'undefined') {
    try {
      localStorage.setItem(WORKSPACE_KEY, ws)
    } catch {
      /* storage unavailable (private mode etc.): backend copy still saves */
    }
  }
  void import('./api').then(({ updateLastWorkspace }) =>
    updateLastWorkspace(ws).catch(() => {}),
  )
}

/** Forget the stored workspace (the user switched to Default). */
function clearStoredWorkspace(): void {
  if (typeof localStorage !== 'undefined') {
    try {
      localStorage.removeItem(WORKSPACE_KEY)
    } catch {
      /* nothing to clear */
    }
  }
  void import('./api').then(({ updateLastWorkspace }) =>
    updateLastWorkspace('').catch(() => {}),
  )
}

export const useAgent = create<AgentState>((set, get) => ({
  conversationId: null,
  messagesByConv: { draft: [] },
  bufferKey: () => {
    const id = get().conversationId
    return id === null ? 'draft' : String(id)
  },
  status: 'idle',
  error: null,
  workspace: loadStoredWorkspace(),
  log: [],
  previewPath: null,
  setPreviewPath: (previewPath) => set({ previewPath }),
  pendingQuestion: null,
  setPendingQuestion: (q) =>
    set((s) => ({
      pendingQuestion:
        typeof q === 'function' ? q(s.pendingQuestion) : q,
    })),
  setWorkspace: (ws) => {
    const norm = ws === '.' ? '' : ws
    set({ workspace: norm })
    if (norm) persistWorkspace(norm)
    else clearStoredWorkspace()
  },

  newConversation: () =>
    set((s) => ({
      conversationId: null,
      messagesByConv: { ...s.messagesByConv, draft: [] },
      status: 'idle',
      error: null,
      pendingQuestion: null,
    })),

  setConversationId: (id) => set({ conversationId: id }),

  adoptDraft: (id) =>
    set((s) => {
      const draft = s.messagesByConv.draft ?? []
      const rest = { ...s.messagesByConv }
      delete rest.draft
      return {
        conversationId: id,
        messagesByConv: {
          ...rest,
          // Merge rather than stomp: nothing should be under a fresh id,
          // but a rematch must never drop messages either way.
          [String(id)]: [...(rest[String(id)] ?? []), ...draft],
          draft: [],
        },
      }
    }),

  setStatus: (status) => set({ status }),
  setError: (error) => set({ error }),

  pushLog: (e) =>
    set((s) => ({
      log: [...s.log, { ...e, id: nextLogId++, time: now() }].slice(-200),
    })),

  clearLog: () => set({ log: [] }),

  abortController: null,
  setAbortController: (c) => set({ abortController: c }),

  // ---- buffer mutation helpers ----
  // Every mutation takes an explicit buffer key: the send path captures its
  // target at send time, so a turn streams into its own conversation's
  // buffer even when the user is looking at another one.

  appendUserMessage: (key, text, images) => {
    const id = genId()
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: [
          ...(s.messagesByConv[key] ?? []),
          { id, role: 'user', content: text, images },
        ],
      },
    }))
    return id
  },

  removeMessage: (key, msgId) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).filter((m) => m.id !== msgId),
      },
    }))
  },

  appendAssistantPlaceholder: (key) => {
    const id = genId()
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: [...(s.messagesByConv[key] ?? []), { id, role: 'assistant', content: '' }],
      },
    }))
    return id
  },

  appendTextDelta: (key, msgId, text) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) =>
          m.id === msgId ? { ...m, content: m.content + text } : m,
        ),
      },
    }))
  },

  startToolCall: (key, msgId, callId, name, args) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) =>
          m.id === msgId
            ? {
                ...m,
                toolCalls: [
                  ...(m.toolCalls ?? []),
                  { id: callId || `t${(m.toolCalls?.length ?? 0) + 1}`, name, args },
                ],
              }
            : m,
        ),
      },
    }))
  },

  finishToolCall: (key, msgId, callId, result) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && tcs[i].result === undefined) {
              tcs[i] = { ...tcs[i], result }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  loadHistory: (convId, rows) =>
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [String(convId)]: buildMessages(rows),
      },
    })),
}))

/**
 * Build a rendered message list from persisted rows. Tool rows carry one
 * result each, linked to their call by tool_call_id (the call name is
 * duplicated in tool_calls[0].name). Results merge into the owning assistant
 * turn's calls so a reloaded turn renders exactly like a finished live turn:
 * one collapsed trace, results inside it - not a second wall of tool blocks.
 */
function buildMessages(
  rows: Parameters<AgentState['loadHistory']>[1],
): ChatMessage[] {
  const resultById = new Map<string, unknown>()
  const nameById = new Map<string, string>()
  for (const r of rows) {
    if (r.role !== 'tool') continue
    const id = r.tool_call_id ?? r.tool_calls?.[0]?.id ?? ''
    if (!id) continue
    resultById.set(id, safeParse(r.content))
    const tc = r.tool_calls?.[0]
    const name =
      tc?.function?.name ?? (tc as { name?: string } | undefined)?.name
    if (name) nameById.set(id, name)
  }

  const out: ChatMessage[] = []
  for (const r of rows) {
    if (r.role === 'tool') {
      const id = r.tool_call_id ?? r.tool_calls?.[0]?.id ?? ''
      // Already absorbed into the assistant turn's trace; render
      // standalone only when orphaned (no matching call row).
      if (id && resultById.has(id)) continue
      out.push({
        id: `db${r.id}`,
        role: 'tool',
        content: r.content,
        images: r.images ?? undefined,
        toolCalls: [
          {
            id,
            name: nameById.get(id) ?? 'tool',
            args: undefined,
            result: safeParse(r.content),
          },
        ],
      })
      continue
    }
    if (r.role === 'assistant' && r.tool_calls?.length) {
      out.push({
        id: `db${r.id}`,
        role: 'assistant',
        content: r.content,
        images: r.images ?? undefined,
        toolCalls: r.tool_calls.map((c, i) => ({
          id: c.id ?? `t${r.id}-${i}`,
          name: c.function?.name ?? nameById.get(c.id ?? '') ?? 'tool',
          args: safeParse(c.function?.arguments),
          result: c.id ? resultById.get(c.id) : undefined,
        })),
      })
      continue
    }
    out.push({
      id: `db${r.id}`,
      role: r.role as Role,
      content: r.content,
      images: r.images ?? undefined,
    })
  }
  return out
}

function safeParse(s?: string): unknown {
  if (!s) return undefined
  try {
    return JSON.parse(s)
  } catch {
    return s
  }
}
