import { create } from 'zustand'
import { listAgents, type ScheduledAgent } from './api'

let toastSeq = 0

export type Role = 'user' | 'assistant' | 'tool' | 'system'

export interface ToolCall {
  id: string
  name: string
  args?: unknown
  result?: unknown
  /** Live output tail while the tool runs (tool_progress chunks). */
  output?: string
  /** Client clock ms when the call started / finished (liveness UI). */
  startedAt?: number
  finishedAt?: number
  /** Live sub-agent run state (spawn_agent calls only). */
  subAgent?: SubAgentRun
}

/** Cap on the streamed-output tail kept per tool call, so a chatty build
 *  can't grow memory unbounded. */
export const TOOL_OUTPUT_CAP = 8_000

/** Cap on the telemetry tape per conversation (a tail is kept). */
export const TAPE_CAP = 16_000

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
  /** The approved plan this message implements (set at the exit_plan
   *  approval boundary; rendered as a header above the execution). */
  implementsPlan?: string
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

/** Transient toast (issue #41: scheduled-run failure/success notices). */
export interface Toast {
  id: number
  kind: 'error' | 'success' | 'info'
  title: string
  body?: string
}

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
  /**
   * Per-conversation run status. Multiple chats can run at once (issue #10);
   * everything that means "the run I'm looking at" derives from the on-screen
   * conversation's entry via useStatus(). A stream writes its own
   * conversation's entry, so a hidden turn never clobbers the visible one.
   */
  statusByConv: Record<string, AgentStatus>
  setStatus: (key: string, s: AgentStatus) => void
  /**
   * Live model-call state per conversation (issue #43): set when the backend
   * emits `model_call` (a chat call dispatched, no response yet), cleared on
   * the first non-thinking stream activity. The status strip renders
   * "waiting for <provider> · <elapsed>s" from it so a silent turn is
   * answerable — network vs provider vs quietly working — at a glance.
   */
  modelCallByConv: Record<string, { provider: string; model: string; startedAt: number } | null>
  setModelCall: (key: string, mc: { provider: string; model: string; startedAt: number } | null) => void
  /**
   * Sidebar finish signal, keyed by convKey (issue #25): 'ok' | 'error'. Set
   * by setStatus when a turn ends (running -> idle/error) while its chat is
   * in the background; ConversationRow renders a green bar / red pill until
   * the chat is opened. Not persisted - a finish signal from a previous
   * session is stale the moment the app restarts.
   */
  finishedByConv: Record<string, 'ok' | 'error'>
  clearFinished: (key: string) => void
  /** Per-conversation stream/failed-send error (rendered by the owning chat). */
  errorByConv: Record<string, string | null>
  setError: (key: string, e: string | null) => void
  /**
   * Spoken briefing of the last finished turn, per conversation (#66): the
   * backend's `say` event text, captured by the stream handler and consumed
   * by the read-aloud trigger. Speech-only — never rendered.
   */
  sayByConv: Record<string, string | undefined>
  setSay: (key: string, say: string | undefined) => void
  workspace: string
  /**
   * Draft destination (issue #32): where the next first-send will file the
   * draft chat. null = follow the live active workspace (open conversation,
   * expand group, add workspace all update what the card shows); a string =
   * the user pinned a destination via the card's Change… picker, which
   * survives active-workspace churn until first send. Cleared on adopt
   * (the chat is filed) and on newConversation (a fresh draft follows the
   * active workspace again).
   */
  draftDestination: string | null
  pinDraftDestination: (ws: string | null) => void
  log: LogEntry[]
  /** File currently open in the preview side panel (Q44). */
  previewPath: string | null
  setPreviewPath: (p: string | null) => void

  /** Question the agent is currently waiting on, per conversation (keyed by
   *  convKey). Multiple chats can each have one waiting. */
  pendingQuestions: Record<string, PendingQuestion>
  setPendingQuestion: (q: PendingQuestion | null | ((prev: PendingQuestion | null) => PendingQuestion | null)) => void

  /** Tool call awaiting approve/deny under the access-mode gate, per conv. */
  pendingApprovals: Record<string, PendingApproval>
  setPendingApproval: (a: PendingApproval | null | ((prev: PendingApproval | null) => PendingApproval | null)) => void

  /** exit_plan call awaiting approve/revise under plan mode, per conv. */
  pendingPlanApprovals: Record<string, PendingPlanApproval>
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

  /**
   * Per-conversation telemetry tape: one ever-growing line that every tool
   * event of the session appends into (call, arguments, streamed output,
   * response, timing). Survives individual tool calls, thinking gaps, and
   * turn boundaries; capped to a tail so it can't grow unbounded.
   */
  tapeByConv: Record<string, string>
  appendTape: (key: string, chunk: string) => void
  resetTape: (key: string) => void

  // ---- scheduled agents (issue #41) ----
  /** All agents, refreshed from /api/agents by the watcher + CRUD callers. */
  agents: ScheduledAgent[]
  /** conversation id (string key) -> agent id, for agent-chat composer gating. */
  agentChatByConv: Record<string, string>
  refreshAgents: () => Promise<void>
  /** Per-agent last_finished_at already toasted, so the poller fires once. */
  agentsToastedThrough: Record<string, string>
  setAgentsToastedThrough: (agentId: string, iso: string) => void

  toasts: Toast[]
  pushToast: (t: Omit<Toast, 'id'>) => void
  dismissToast: (id: number) => void

  setWorkspace: (ws: string) => void
  newConversation: () => void
  setConversationId: (id: number) => void
  /** First send of a new chat: re-key the live 'draft' buffer to the real
   * conversation id and follow it on screen, in one atomic update. */
  adoptDraft: (id: number) => void
  /** Abort controller per in-flight run, keyed by buffer key (issue #10:
   *  several conversations can stream at once). */
  abortByConv: Record<string, AbortController>
  setAbortController: (key: string, c: AbortController | null) => void
  pushLog: (e: Omit<LogEntry, 'id' | 'time'>) => void
  clearLog: () => void

  appendUserMessage: (key: string, text: string, images?: string[], skills?: string[]) => string
  appendAssistantPlaceholder: (key: string) => string
  /** UI-generated rows outside the streaming protocol (git command trace
   *  rows). Persisted by the backend; live list only. */
  appendRawMessage: (key: string, msg: ChatMessage) => void
  appendTextDelta: (key: string, msgId: string, text: string) => void
  /** Remove one optimistic message (failed-send rollback). */
  removeMessage: (key: string, msgId: string) => void
  startToolCall: (key: string, msgId: string, callId: string, name: string, args: unknown) => void
  finishToolCall: (key: string, msgId: string, callId: string, result: unknown) => void
  appendToolOutput: (key: string, msgId: string, callId: string, chunk: string) => void
  /** At an approved exit_plan tool_result: close the planning message and
   *  open a fresh assistant message (flagged with the plan) for everything
   *  the rest of the turn emits. Returns the new message id, or null when
   *  the call was not on msgId or was not approved (plan continues). */
  splitAtPlanApproval: (key: string, msgId: string) => string | null

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

/**
 * Update one of the per-conversation pending-gate maps (ask_user question /
 * access-mode approval / plan approval). Callers use the same shapes as
 * before the multi-run change: an object (keyed by its convKey), null
 * (clear all), or a reducer applied to each existing entry — a null return
 * removes the entry, so the old `q && q.callId === x ? null : q` filters
 * keep working unchanged.
 */
function applyPending<T extends { convKey: string }>(
  map: Record<string, T>,
  q: T | null | ((prev: T | null) => T | null),
): Record<string, T> {
  if (typeof q === 'function') {
    const next = { ...map }
    for (const k of Object.keys(next)) {
      const r = q(next[k])
      if (r === null || r === undefined) delete next[k]
      else next[k] = r
    }
    return next
  }
  if (q === null) return {}
  return { ...map, [q.convKey]: q }
}

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
  statusByConv: {},
  modelCallByConv: {},
  setModelCall: (key, mc) =>
    set((s) => ({ modelCallByConv: { ...s.modelCallByConv, [key]: mc } })),
  draftDestination: null,
  pinDraftDestination: (ws) => set({ draftDestination: ws }),
  setStatus: (key, status) =>
    set((s) => {
      const prev = s.statusByConv[key]
      // Issue #25: a turn that ends (running -> idle/error) while its chat is
      // NOT on screen leaves a sidebar signal (green bar / red pill). A run
      // watched in its own chat doesn't signal - the transcript IS the
      // signal, and Q4/Q8: no retroactive bar on switch-away. Draft and
      // non-numeric keys have no sidebar row, so they never signal.
      const wasRunning = prev === 'thinking' || prev === 'running-tool'
      const ended = status === 'idle' || status === 'error'
      const idNum = Number(key)
      if (wasRunning && ended && Number.isFinite(idNum) && s.conversationId !== idNum) {
        return {
          statusByConv: { ...s.statusByConv, [key]: status },
          finishedByConv: { ...s.finishedByConv, [key]: status === 'error' ? 'error' : 'ok' },
        }
      }
      return { statusByConv: { ...s.statusByConv, [key]: status } }
    }),
  finishedByConv: {},
  clearFinished: (key) =>
    set((s) => {
      if (!(key in s.finishedByConv)) return s
      const finishedByConv = { ...s.finishedByConv }
      delete finishedByConv[key]
      return { finishedByConv }
    }),
  errorByConv: {},
  sayByConv: {},
  setSay: (key, say) => set((s) => ({ sayByConv: { ...s.sayByConv, [key]: say } })),
  setError: (key, error) =>
    set((s) => ({ errorByConv: { ...s.errorByConv, [key]: error } })),
  workspace: loadStoredWorkspace(),
  log: [],
  previewPath: null,
  setPreviewPath: (previewPath) => set({ previewPath }),
  pendingQuestions: {},
  setPendingQuestion: (q) =>
    set((s) => ({ pendingQuestions: applyPending(s.pendingQuestions, q) })),
  pendingApprovals: {},
  setPendingApproval: (a) =>
    set((s) => ({ pendingApprovals: applyPending(s.pendingApprovals, a) })),
  pendingPlanApprovals: {},
  setPendingPlanApproval: (p) =>
    set((s) => ({ pendingPlanApprovals: applyPending(s.pendingPlanApprovals, p) })),
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

  tapeByConv: {},
  appendTape: (key, chunk) =>
    set((s) => {
      const merged = (s.tapeByConv[key] ?? '') + chunk
      return {
        tapeByConv: {
          ...s.tapeByConv,
          [key]: merged.length > TAPE_CAP ? merged.slice(-TAPE_CAP) : merged,
        },
      }
    }),
  resetTape: (key) =>
    set((s) => {
      if (!(key in s.tapeByConv)) return s
      const next = { ...s.tapeByConv }
      delete next[key]
      return { tapeByConv: next }
    }),

  // ---- scheduled agents (issue #41) ----
  agents: [],
  agentChatByConv: {},
  refreshAgents: async () => {
    try {
      const { agents } = await listAgents()
      const byConv: Record<string, string> = {}
      for (const a of agents) byConv[String(a.conversation_id)] = a.id
      useAgent.setState({ agents, agentChatByConv: byConv })
    } catch {
      /* transient backend hiccup — the poller retries */
    }
  },
  agentsToastedThrough: {},
  setAgentsToastedThrough: (agentId, iso) =>
    set((s) => ({ agentsToastedThrough: { ...s.agentsToastedThrough, [agentId]: iso } })),

  toasts: [],
  pushToast: (t) => {
    const id = ++toastSeq
    set((s) => ({ toasts: [...s.toasts.slice(-4), { ...t, id }] }))
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((x) => x.id !== id) })),
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
      // A fresh draft follows the active workspace again (#32): any pinned
      // destination belonged to the previous draft.
      draftDestination: null,
    }))
    persistConversationId(null)
  },

  setConversationId: (id) => {
    persistConversationId(id)
    // Opening a chat acknowledges its sidebar finish signal (issue #25).
    if (id !== null) get().clearFinished(String(id))
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
        // The draft is filed now (#32): the destination card's job is done.
        draftDestination: null,
      }
    })
    persistConversationId(id)
  },

  pushLog: (e) =>
    set((s) => ({
      log: [...s.log, { ...e, id: nextLogId++, time: now() }].slice(-200),
    })),

  clearLog: () => set({ log: [] }),

  abortByConv: {},
  setAbortController: (key, c) =>
    set((s) => {
      const next = { ...s.abortByConv }
      if (c === null) delete next[key]
      else next[key] = c
      return { abortByConv: next }
    }),

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

  appendRawMessage: (key, msg) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: [...(s.messagesByConv[key] ?? []), msg],
      },
    }))
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
                  {
                    id: callId || `t${(m.toolCalls?.length ?? 0) + 1}`,
                    name,
                    args,
                    startedAt: Date.now(),
                  },
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
              tcs[i] = { ...tcs[i], result, finishedAt: Date.now() }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  appendToolOutput: (key, msgId, callId, chunk) => {
    set((s) => ({
      messagesByConv: {
        ...s.messagesByConv,
        [key]: (s.messagesByConv[key] ?? []).map((m) => {
          if (m.id !== msgId || !m.toolCalls?.length) return m
          const tcs = [...m.toolCalls]
          for (let i = tcs.length - 1; i >= 0; i--) {
            if (tcs[i].id === callId && tcs[i].result === undefined) {
              const merged = (tcs[i].output ?? '') + chunk
              // Keep only the tail; a chatty build must not grow unbounded.
              const output =
                merged.length > TOOL_OUTPUT_CAP ? merged.slice(-TOOL_OUTPUT_CAP) : merged
              tcs[i] = { ...tcs[i], output }
              break
            }
          }
          return { ...m, toolCalls: tcs }
        }),
      },
    }))
  },

  splitAtPlanApproval: (key, msgId) => {
    let newId: string | null = null
    set((s) => {
      const msgs = s.messagesByConv[key] ?? []
      const idx = msgs.findIndex((m) => m.id === msgId)
      if (idx === -1) return {}
      const call = msgs[idx].toolCalls?.find((t) => t.name === 'exit_plan')
      const decision = (call?.result ?? null) as { decision?: string } | null
      if (decision?.decision !== 'approved') return {}
      const plan = (call?.args ?? {}) as { plan?: unknown }
      newId = genId()
      const next = msgs.slice()
      next.splice(idx + 1, 0, {
        id: newId,
        role: 'assistant',
        content: '',
        implementsPlan: typeof plan.plan === 'string' ? plan.plan : undefined,
      })
      return { messagesByConv: { ...s.messagesByConv, [key]: next } }
    })
    return newId
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
    set((s) => {
      const key = String(convId)
      // A live run owns this buffer right now (issue #10): the in-flight
      // message isn't persisted yet, so a wholesale reload would drop it and
      // orphan every later delta. Skip — the live buffer is the truth.
      const st = s.statusByConv[key]
      if (st === 'thinking' || st === 'running-tool') return {}
      return {
        messagesByConv: {
          ...s.messagesByConv,
          [key]: buildMessages(rows),
        },
      }
    }),
}))

/**
 * The on-screen conversation's run status — the replacement for the old
 * single global `status` now that several chats can run at once (issue #10).
 */
export function useStatus(): AgentStatus {
  const key = useAgent(
    (s) => (s.conversationId === null ? 'draft' : String(s.conversationId)),
  )
  return useAgent((s) => s.statusByConv[key] ?? 'idle')
}

/** The on-screen conversation's last stream/failed-send error, if any. */
export function useError(): string | null {
  const key = useAgent(
    (s) => (s.conversationId === null ? 'draft' : String(s.conversationId)),
  )
  return useAgent((s) => s.errorByConv[key] ?? null)
}

/** Id of the last assistant message in a buffer — after a plan-approval
 *  split, turn-end writes ([stopped], sub-agent settle) belong to the tail,
 *  not the id captured at send time. */
export function lastAssistantId(key: string): string | null {
  const msgs = useAgent.getState().messagesByConv[key] ?? []
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === 'assistant') return msgs[i].id
  }
  return null
}

/**
 * Build a rendered message list from persisted rows. Tool rows carry one
 * result each, linked to their call by tool_call_id (the call name is
 * duplicated in tool_calls[0].name). Results merge into the owning assistant
 * turn's calls so a reloaded turn renders exactly like a finished live turn:
 * one collapsed trace, results inside it - not a second wall of tool blocks.
 *
 * Issue #17: one turn renders as ONE assistant block. Each model call's row
 * is an emission; emissions join with single line feeds (live parity) and
 * their tool calls pool into the block's trace. A message stays open across
 * emissions until a user row (or a plan-approval boundary) closes it.
 */
export function buildMessages(
  rows: Parameters<AgentState['loadHistory']>[1],
): ChatMessage[] {
  const resultById = new Map<string, unknown>()
  const nameById = new Map<string, string>()
  const subAgentById = new Map<string, SubAgentRun>()
  // Ids that a persisted assistant tool_calls row actually called. UI rows
  // (git commands run from the strip) carry a result but have no assistant
  // caller — they must render standalone, not be treated as absorbed.
  const calledIds = new Set<string>()
  for (const r of rows) {
    if (r.role === 'assistant' && r.tool_calls?.length) {
      for (const c of r.tool_calls) if (c.id) calledIds.add(c.id)
    }
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
  // Issue #17 coalescing: an assistant turn block stays open across model
  // calls until a user/system row (or a rendered standalone tool row) closes
  // it. Emissions join with a single line feed (live parity); each call's
  // tool calls pool into the block's trace. Plan-approval boundaries force
  // a split (the plan message folds to a one-line header).
  let open: ChatMessage | null = null
  const closeOpen = () => {
    open = null
  }
  /** Append an emission's text (+ images) to the open block, or open a new
   *  one when forced (plan boundary) or none is open. */
  const coalesce = (r: (typeof rows)[number], calls?: ChatMessage['toolCalls']) => {
    if (!open) {
      out.push({
        id: `db${r.id}`,
        role: 'assistant',
        content: r.content,
        images: r.images ?? undefined,
        toolCalls: calls,
        implementsPlan: pendingPlanText !== null ? pendingPlanText : undefined,
      })
      open = out[out.length - 1]
      return
    }
    open.content = open.content ? `${open.content}\n${r.content}` : r.content
    if (calls?.length) open.toolCalls = [...(open.toolCalls ?? []), ...calls]
    if (r.images?.length) open.images = [...(open.images ?? []), ...r.images]
  }
  // Plan-approval boundary: when an assistant row carries an exit_plan call
  // whose persisted result says approved, the NEXT assistant row (the rest
  // of the same turn) implements that plan — flag it so it renders with the
  // plan header, mirroring the live splitAtPlanApproval behavior.
  let pendingPlanText: string | null = null
  for (const r of rows) {
    if (r.role === 'tool') {
      const id = r.tool_call_id ?? r.tool_calls?.[0]?.id ?? ''
      // Already absorbed into the assistant turn's trace; render
      // standalone only when orphaned (no matching call row) — e.g. the
      // git commands the user ran from the status strip. An absorbed row
      // leaves the block open (its tools belong to this turn's trace).
      if (id && resultById.has(id) && calledIds.has(id)) continue
      closeOpen()
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
      const calls = r.tool_calls.map((c, i) => ({
        id: c.id ?? `t${r.id}-${i}`,
        name: c.function?.name ?? nameById.get(c.id ?? '') ?? 'tool',
        args: safeParse(c.function?.arguments),
        result: c.id ? resultById.get(c.id) : undefined,
        subAgent: c.id ? subAgentById.get(c.id) : undefined,
      }))
      // A plan boundary always opens a fresh block; close the old one.
      if (pendingPlanText !== null) closeOpen()
      coalesce(r, calls)
      pendingPlanText = null
      for (const c of calls) {
        if (c.name !== 'exit_plan') continue
        const decision = (c.result ?? {}) as { decision?: string }
        const plan = (c.args ?? {}) as { plan?: unknown }
        if (decision.decision === 'approved' && typeof plan.plan === 'string') {
          pendingPlanText = plan.plan
        }
      }
      continue
    }
    if (r.role === 'assistant') {
      if (pendingPlanText !== null) closeOpen()
      coalesce(r)
      pendingPlanText = null
      continue
    }
    // User/system rows close the open turn block.
    closeOpen()
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
