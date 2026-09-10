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
  toolCalls?: ToolCall[]
}

export type AgentStatus = 'idle' | 'thinking' | 'running-tool' | 'error'

interface AgentState {
  conversationId: number | null
  messages: ChatMessage[]
  status: AgentStatus
  error: string | null
  workspace: string

  setWorkspace: (ws: string) => void
  newConversation: () => void
  setConversationId: (id: number) => void
  setStatus: (s: AgentStatus) => void
  setError: (e: string | null) => void

  appendUserMessage: (text: string) => void
  appendAssistantPlaceholder: () => string
  appendTextDelta: (msgId: string, text: string) => void
  startToolCall: (msgId: string, name: string, args: unknown) => void
  finishToolCall: (msgId: string, name: string, result: unknown) => void

  loadHistoryFromApi: (conversationId: number) => Promise<void>
}

let nextId = 1
const genId = () => `m${nextId++}`

export const useAgent = create<AgentState>((set) => ({
  conversationId: null,
  messages: [],
  status: 'idle',
  error: null,
  workspace: '.',

  setWorkspace: (ws) => set({ workspace: ws }),

  newConversation: () =>
    set({ conversationId: null, messages: [], status: 'idle', error: null }),

  setConversationId: (id) => set({ conversationId: id }),
  setStatus: (status) => set({ status }),
  setError: (error) => set({ error }),

  appendUserMessage: (text) =>
    set((s) => ({
      messages: [...s.messages, { id: genId(), role: 'user', content: text }],
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

  startToolCall: (msgId, name, args) =>
    set((s) => ({
      messages: s.messages.map((m) =>
        m.id === msgId
          ? {
              ...m,
              toolCalls: [
                ...(m.toolCalls ?? []),
                { id: `t${(m.toolCalls?.length ?? 0) + 1}`, name, args },
              ],
            }
          : m,
      ),
    })),

  finishToolCall: (msgId, name, result) =>
    set((s) => ({
      messages: s.messages.map((m) => {
        if (m.id !== msgId || !m.toolCalls?.length) return m
        const tcs = [...m.toolCalls]
        for (let i = tcs.length - 1; i >= 0; i--) {
          if (tcs[i].name === name && tcs[i].result === undefined) {
            tcs[i] = { ...tcs[i], result }
            break
          }
        }
        return { ...m, toolCalls: tcs }
      }),
    })),

  loadHistoryFromApi: async (conversationId) => {
    const res = await fetch(`/api/conversations/${conversationId}/messages`)
    if (!res.ok) return
    const rows: Array<{
      id: number
      role: Role
      content: string
      tool_calls: Array<{ id?: string; name?: string }> | null
    }> = await res.json()
    const messages: ChatMessage[] = rows.map((r) => ({
      id: `db${r.id}`,
      role: r.role,
      content: r.content,
      toolCalls:
        r.role === 'tool' && r.tool_calls?.[0]?.name
          ? [{ id: r.tool_calls[0].id ?? '', name: r.tool_calls[0].name! }]
          : undefined,
    }))
    set({ messages })
  },
}))
