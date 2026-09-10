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
  created_at: string
}

export const listConversations = () =>
  api<ConversationRow[]>('/api/conversations')

export const createConversation = (title: string) =>
  api<{ id: number }>('/api/conversations', {
    method: 'POST',
    body: JSON.stringify({ title }),
  })

export const getMessages = (id: number) =>
  api<Array<{ id: number; role: string; content: string; tool_calls: any }>>(
    `/api/conversations/${id}/messages`,
  )

export interface AgentEvent {
  type: 'text' | 'tool_start' | 'tool_result' | 'done' | 'error'
  text?: string
  name?: string
  args?: unknown
  result?: unknown
  message?: string
}

/** POST to the agent endpoint and parse the NDJSON event stream. */
export async function streamAgentTurn(
  conversationId: number,
  message: string,
  workspace: string,
  onEvent: (ev: AgentEvent) => void,
): Promise<void> {
  const res = await fetch(`${BASE}/api/agent/${conversationId}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, workspace }),
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
    let idx
    while ((idx = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, idx).trim()
      buf = buf.slice(idx + 1)
      if (line) onEvent(JSON.parse(line))
    }
  }
}
