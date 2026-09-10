const BASE = 'http://localhost:8765'

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`)
  return res.json()
}

export interface ConversationRow {
  id: number
  title: string
  workspace: string | null
  created_at: string
  updated_at: string
}

export const listConversations = () =>
  api<ConversationRow[]>('/api/conversations')

export const createConversation = (title: string, workspace?: string) =>
  api<{ id: number }>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title, workspace: workspace ?? null }),
  })

export const getMessages = (id: number) =>
  api<
    Array<{
      id: number
      role: string
      content: string
      tool_calls: Array<{
        id?: string
        type?: string
        function?: { name?: string; arguments?: string }
      }> | null
    }>
  >(`/api/conversations/${id}/messages`)

// ---------------------------------------------------------------- config

export interface AgentConfig {
  api_base: string
  api_key: string // masked from server
  model: string
}

export const getConfig = () => api<AgentConfig>('/api/config')
export const updateConfig = (patch: Partial<AgentConfig>) =>
  api<{ ok: boolean }>('/api/config', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

// ---------------------------------------------------------------- agent stream

export interface AgentEvent {
  type: 'text' | 'tool_start' | 'tool_result' | 'done' | 'error'
  text?: string
  name?: string
  args?: unknown
  result?: unknown
  message?: string
  call_id?: string
}

export type AgentEventHandler = (ev: AgentEvent) => void

/** POST to the agent endpoint and parse the NDJSON event stream. */
export async function streamAgentTurn(
  conversationId: number,
  message: string,
  workspace: string,
  onEvent: AgentEventHandler,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${BASE}/api/agent/${conversationId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, workspace }),
    signal,
  })
  if (!res.ok || !res.body) {
    throw new Error(`Agent error ${res.status}: ${await res.text()}`)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 1)
      if (line) onEvent(JSON.parse(line))
    }
  }
}
