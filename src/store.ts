import { create } from 'zustand'

export type Role = 'user' | 'assistant' | 'tool' | 'system'

export interface ToolCall {
  id: string
  name: string
  args?: unknown
  result?: unknown
  /** Live sub-agent run state (spawn_agent calls only). */
  subAgent?: SubAgentRun
}

/** A live sub-agent run (spawn_agent tool call in flight). */
export interface SubAgentRun {
  agentId: number
  agentType: string
  prompt: string
  status: 'running' | 'completed' | 'error' | 'cancelled' | 'max_turns'
  /** Streamed text deltas from the sub-agent's own turns. */
  text: string
  /** Tool chips inside the sub-agent's block. */
  tools: Array<{ id: string; name: string; args?: unknown; result?: unknown }>
}

export interface ChatMessage {
  id: string
  role: Role
  content: string
  /** Stored image rel paths (backend/data/images/...), rendered via imageUrl(). */
  images?: string[]
  /** Skills invoked for this turn via $name in the text (live display only). */
  skills?: string[]
  toolCalls?: ToolCall[]
  /** Sub-agent run snapshot (persisted spawn_agent result, history load). */
  subAgent?: SubAgentRun
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

/** Access modes for the global tool-approval gate (PLAN-access-modes.md). */
export type AccessMode = 'ask' | 'plan' | 'full'

/** A tool call waiting for the user's approve/deny under ask mode. */
export interface PendingApproval {
  callId: string
  tool: string
  args: Record<string, unknown>
  /** Buffer of the turn that asked; only rendered when on screen. */
  convKey: string
}

/** The agent's exit_plan call waiting for approval under plan mode. The run
 *  is blocked on it; approving ends plan mode and resumes the SAME turn. */
export interface PendingPlanApproval {
  callId: string
  plan: string
  /** Buffer of the turn that asked; only rendered when on screen. */
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

  /** Tool call awaiting approve/deny under the access-mode gate. */
  pendingApproval: PendingApproval | null
  setPendingApproval: (a: PendingApproval | null | ((prev: PendingApproval | null) => PendingApproval | null)) => void

  /** exit_plan call awaiting approve/revise under plan mode. */
  pendingPlanApproval: PendingPlanApproval | null
  setPendingPlanApproval: (p: PendingPlanApproval | null | ((prev: PendingPlanApproval | null) => PendingPlanApproval | null)) => void

  /** Live copy of the configured access mode (loaded at startup, updated by
   *  the header control and by plan-mode's approve-plan flow). */
  accessMode: AccessMode
  setAccessMode: (m: AccessMode) => void

  /**
   * Per-conversation context-size readout: exact usage.prompt_tokens of the
   * latest model call, plus the resolved context window it fills. Written
   * from the stream's usage event; cleared when the conversation is deleted.
   */
  contextByConv: Record<string, { tokens: number; window: number | null; model: string | null }>
  setContext: (convId: number, tokens: number, window: number | null, model: string | null) => void

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

  appendUserMessage: (key: string, text: string, images?: string[], skills?: string[]) => string
  appendAssistantPlaceholder: (key: string) => string
  appendTextDelta: (key: string, msgId: string, text: string) => void
  /** Remove one optimistic message (failed-send rollback). */
  removeMessage: (key: string, msgId: string) => void
  startToolCall: (key: string, msgId: string, callId: string, name: string, args: unknown) => void
  finishToolCall: (key: string, msgId: string, callId: string, result: unknown) => void

  /** Sub-agent live state (spawn_agent calls). */
  startSubAgent: (key: string, msgId: string, callId: string, agentId: number, agentType: string, prompt: string) => void
  subAgentTextDelta: (key: string, msgId: string, callId: string, text: string) => void
  subAgentToolStart: (key: string, msgId: string, callId: string, name: string, args: unknown) => void
  subAgentToolResult: (key: string, msgId: string, callId: string, result: unknown) => void
  finishSubAgent: (key: string, msgId: string, callId: string, status: string, turns: number) => void
  /** Mark every still-running sub-agent on a message as interrupted and
   *  settle its unfinished tool chips — called when the stream ends
   *  (done, stopped, error, or abort) so no block pulses forever. */
  settleSubAgents: (key: string, msgId: string) => void

  /** Load a conversation's persisted history into its buffer. */
  loadHistory: (
    convId: number,
    rows: Array<{
      id: number
      role: string
      content: string
      images?: string[] | null
      sub_agent_transcript?: {
        agent_type?: string
        status?: string
        turns?: number
        output?: string
        transcript?: Array<{ role: string; content: string; name?: string }>
      } | null
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
    // Legacy value '.' meant "no workspace" too. A remote-namespaced value
    // is only meaningful while connected to that host; a fresh launch
    // starts local.
    if (!ws || ws === '.' || ws.startsWith('remote:')) return DEFAULT_WORKSPACE
    return ws
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

// The open conversation is remembered across reloads: the recovery banner
// reloads the whole app when the backend comes back, and without this the
// reload silently lands on a fresh "draft" — the next send then creates a
// brand-new conversation, which looks like the app switched chats on its own.
// localStorage only (the DB is the durable copy; a stale id simply misses).
const CONV_KEY = 'agent.conversationId'

export function persistConversationId(id: number | null): void {
  if (typeof localStorage === 'undefined') return
  try {
    if (id === null) localStorage.removeItem(CONV_KEY)
    else localStorage.setItem(CONV_KEY, String(id))
  } catch {
    /* non-persistent storage is fine */
  }
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
  pendingApproval: null,
  setPendingApproval: (a) =>
    set((s) => ({
      pendingApproval:
        typeof a === 'function' ? a(s.pendingApproval) : a,
    })),
  pendingPlanApproval: null,
  setPendingPlanApproval: (p) =>
    set((s) => ({
      pendingPlanApproval:
        typeof p === 'function' ? p(s.pendingPlanApproval) : p,
    })),
  accessMode: 'ask',
  setAccessMode: (m) => set({ accessMode: m }),

  contextByConv: {},
  setContext: (convId, tokens, window, model) =>
    set((s) => ({
      contextByConv: {
        ...s.contextByConv,
        [String(convId)]: { tokens, window, model },
      },
    })),
  setWorkspace: (ws) => {
    const norm = ws === '.' ? '' : ws
    set({ workspace: norm })
    if (norm) persistWorkspace(norm)
    else clearStoredWorkspace()
  },

  newConversation: () => {
    set((s) => ({
      conversationId: null,
      messagesByConv: { ...s.messagesByConv, draft: [] },
      status: 'idle',
      error: null,
      pendingQuestion: null,
      pendingApproval: null,
      pendingPlanApproval: null,
    }))
    persistConversationId(null)
  },

  setConversationId: (id) => {
    persistConversationId(id)
    set({ conversationId: id })
  },

  adoptDraft: (id) => {
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
    })
    persistConversationId(id)
  },

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

  appendUserMessage: (key, text, images, skills) => {
    const id = genId()
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: [
          ...(s.messagesByConv[key] ?? []),
          { id, role: 'user', content: text, images, skills },
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

  // ---- sub-agent live state ----
  // All five helpers locate the spawn_agent ToolCall by (msgId, callId) and
  // mutate its subAgent field. Events carry call_id so parallel agents in
  // one parent turn route to the right block.

  startSubAgent: (key, msgId, callId, agentId, agentType, prompt) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && !tcs[i].subAgent) {
              tcs[i] = {
                ...tcs[i],
                subAgent: {
                  agentId,
                  agentType,
                  prompt,
                  status: 'running',
                  text: '',
                  tools: [],
                },
              }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  subAgentTextDelta: (key, msgId, callId, text) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && tcs[i].subAgent) {
              const sa = tcs[i].subAgent!
              tcs[i] = { ...tcs[i], subAgent: { ...sa, text: sa.text + text } }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  subAgentToolStart: (key, msgId, callId, name, args) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && tcs[i].subAgent) {
              const sa = tcs[i].subAgent!
              tcs[i] = {
                ...tcs[i],
                subAgent: {
                  ...sa,
                  tools: [...sa.tools, { id: `sat${sa.tools.length + 1}`, name, args }],
                },
              }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  subAgentToolResult: (key, msgId, callId, result) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && tcs[i].subAgent) {
              const sa = tcs[i].subAgent!
              const tools = [...sa.tools]
              for (let j = tools.length - 1; j >= 0; j--) {
                if (tools[j].result === undefined) {
                  tools[j] = { ...tools[j], result }
                  break
                  }
              }
              tcs[i] = { ...tcs[i], subAgent: { ...sa, tools } }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  finishSubAgent: (key, msgId, callId, status, turns) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && tcs[i].subAgent) {
              const sa = tcs[i].subAgent!
              tcs[i] = {
                ...tcs[i],
                subAgent: { ...sa, status: status as SubAgentRun['status'] },
              }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  settleSubAgents: (key, msgId) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          let changed = false
          const tcs = m.toolCalls.map((tc) => {
            if (!tc.subAgent || tc.subAgent.status !== 'running') return tc
            changed = true
            const sa = tc.subAgent
            const tools = sa.tools.map((t) =>
              t.result === undefined ? { ...t, result: null } : t,
            )
            return {
              ...tc,
              // The tool result settles too, so the parent chip shows done.
              result: tc.result ?? {
                status: 'cancelled',
                output: '',
                note: 'run interrupted',
              },
              subAgent: { ...sa, status: 'cancelled' as const, tools },
            }
          })
          return changed ? { ...m, toolCalls: tcs } : m
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
  const subAgentById = new Map<string, SubAgentRun>()
  for (const r of rows) {
    if (r.role !== 'tool') continue
    const id = r.tool_call_id ?? r.tool_calls?.[0]?.id ?? ''
    if (!id) continue
    resultById.set(id, safeParse(r.content))
    const tc = r.tool_calls?.[0]
    const name =
      tc?.function?.name ?? (tc as { name?: string } | undefined)?.name
    if (name) nameById.set(id, name)
    // Rehydrate a spawn_agent run snapshot into the same live shape the
    // stream builds, so a reloaded turn renders the nested transcript.
    const snap = r.sub_agent_transcript
    if (name === 'spawn_agent' && snap && typeof snap === 'object') {
      const entries = Array.isArray(snap.transcript) ? snap.transcript : []
      let text = ''
      const tools: SubAgentRun['tools'] = []
      for (const e of entries) {
        if (e.role === 'assistant' && typeof e.content === 'string') {
          text = e.content // last assistant text wins (the final message)
        } else if (e.role === 'tool' && e.name) {
          tools.push({ id: `sat${tools.length + 1}`, name: e.name, result: e.content })
        }
      }
      subAgentById.set(id, {
        agentId: 0,
        agentType: snap.agent_type ?? 'sub-agent',
        prompt: '',
        status: (snap.status as SubAgentRun['status']) ?? 'completed',
        text,
        tools,
      })
    }
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
          subAgent: c.id ? subAgentById.get(c.id) : undefined,
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
