import { create } from 'zustand'

export type Role = 'user' | 'assistant' | 'tool'

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
  messages: ChatMessage[]
  status: AgentStatus
  error: string | null
  workspace: string
  log: LogEntry[]
  /** File currently open in the preview side panel (Q44). */
  previewPath: string | null
  setPreviewPath: (p: string | null) => void

  setWorkspace: (ws: string) => void
  newConversation: () => void
  setConversationId: (id: number) => void
  setStatus: (s: AgentStatus) => void
  setError: (e: string | null) => void
  pushLog: (e: Omit<LogEntry, 'id' | 'time'>) => void
  clearLog: () => void
  abortController: AbortController | null
  setAbortController: (c: AbortController | null) => void

  appendUserMessage: (text: string, images?: string[]) => void
  appendAssistantPlaceholder: () => string
  appendTextDelta: (msgId: string, text: string) => void
  startToolCall: (msgId: string, callId: string, name: string, args: unknown) => void
  finishToolCall: (msgId: string, callId: string, result: unknown) => void

  /** Load a conversation's persisted history into the UI. */
  loadHistory: (
    rows: Array<{
      id: number
      role: string
      content: string
      images?: string[] | null
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
const WORKSPACE_KEY = 'agent.workspace'
const DEFAULT_WORKSPACE = '.'

function loadStoredWorkspace(): string {
  if (typeof localStorage === 'undefined') return DEFAULT_WORKSPACE
  try {
    const ws = localStorage.getItem(WORKSPACE_KEY)
    return ws && ws !== DEFAULT_WORKSPACE ? ws : DEFAULT_WORKSPACE
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

export const useAgent = create<AgentState>((set) => ({
  conversationId: null,
  messages: [],
  status: 'idle',
  error: null,
  workspace: loadStoredWorkspace(),
  log: [],
  previewPath: null,
  setPreviewPath: (previewPath) => set({ previewPath }),

  setWorkspace: (ws) => {
    set({ workspace: ws })
    if (ws && ws !== DEFAULT_WORKSPACE) persistWorkspace(ws)
  },

  newConversation: () =>
    set({ conversationId: null, messages: [], status: 'idle', error: null }),

  setConversationId: (id) => set({ conversationId: id }),
  setStatus: (status) => set({ status }),
  setError: (error) => set({ error }),

  pushLog: (e) =>
    set((s) => ({
      log: [...s.log, { ...e, id: nextLogId++, time: now() }].slice(-200),
    })),

  clearLog: () => set({ log: [] }),

  abortController: null,
  setAbortController: (c) => set({ abortController: c }),

  appendUserMessage: (text, images) =>
    set((s) => ({
      messages: [
        ...s.messages,
        { id: genId(), role: 'user', content: text, images },
      ],
    })),

  appendAssistantPlaceholder: () => {
    const id = genId()
    set((s) => ({
      messages: [...s.messages, { id, role: 'assistant', content: '' }],
    }))
    return id
  },

  appendTextDelta: (msgId, text) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === msgId ? { ...m, content: m.content + text } : m,
      ),
    })),

  startToolCall: (msgId, callId, name, args) =>
    set((s) => ({
      messages: s.messages.map((m) =>
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
    })),

  finishToolCall: (msgId, callId, result) =>
    set((s) => ({
      messages: s.messages.map((m) => {
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
    })),

  loadHistory: (rows) =>
    set({
      messages: rows.map((r) => {
        const fn = r.tool_calls?.[0]?.function
        return {
          id: `db${r.id}`,
          role: r.role as Role,
          content: r.content,
          images: r.images ?? undefined,
          toolCalls:
            r.role === 'tool' && fn?.name
              ? [
                  {
                    id: r.tool_calls![0].id ?? '',
                    name: fn.name,
                    args: safeParse(fn.arguments),
                    result: safeParse(r.content),
                  },
                ]
              : undefined,
        }
      }),
      status: 'idle',
      error: null,
    }),
}))

function safeParse(s?: string): unknown {
  if (!s) return undefined
  try {
    return JSON.parse(s)
  } catch {
    return s
  }
}
