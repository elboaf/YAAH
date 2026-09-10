// In a browser, relative URLs go through the Vite dev proxy. Inside the
// Tauri webview (tauri:// origin) there is no proxy, so hit the embedded
// backend directly.
const IS_TAURI =
  typeof window !== 'undefined' && !window.location.protocol.startsWith('http')
const BASE = IS_TAURI ? 'http://127.0.0.1:8765' : ''
const url = (p: string) => `${BASE}${p}`

// Listen for backend spawn failures reported by the Tauri shell.
export let backendStartupError: string | null = null
if (typeof window !== 'undefined') {
  window.addEventListener('backend-error', (e) => {
    backendStartupError = (e as CustomEvent<{ message?: string }>).detail?.message ?? 'backend failed to start'
  })
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url(path), {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await res.text()
  // A JSON parse failure here almost always means an HTML page came back
  // (SPA fallback / static server) because the API backend isn't reachable.
  const contentType = res.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    throw new Error(
      `${res.status}: expected JSON from ${url(path)} but got '${contentType || 'unknown'}'. ` +
        (backendStartupError ??
          `Is the API backend running on ${BASE || 'the vite proxy target (localhost:8765)'}?`),
    )
  }
  if (!res.ok) throw new Error(`${res.status}: ${text}`)
  return JSON.parse(text) as T
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
  temperature?: number
  max_tokens?: number
}

export const getConfig = () => api<AgentConfig>('/api/config')
export const updateConfig = (patch: Partial<AgentConfig>) =>
  api<{ ok: boolean }>('/api/config', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

export interface ProviderPreset {
  api_base: string
  model: string
  needs_api_key: boolean
  supports_tools: boolean | null
}

export const getProviders = () =>
  api<Record<string, ProviderPreset>>('/api/providers')

export const listModels = (api_base: string, api_key?: string) =>
  api<{ models: string[]; error?: string }>('/api/models', {
    method: 'POST',
    body: JSON.stringify({ api_base, api_key: api_key || null }),
  })

export const probeTools = (api_base: string, model: string, api_key?: string) =>
  api<{ supports_tools: boolean }>('/api/models/probe-tools', {
    method: 'POST',
    body: JSON.stringify({ api_base, model, api_key: api_key || null }),
  })

// ---------------------------------------------------------------- files

export interface FileEntry {
  name: string
  path: string
  type: 'file' | 'dir'
  children?: FileEntry[]
}

export const getFileTree = (workspace: string) =>
  api<{ root: string; tree: FileEntry[] }>(
    `/api/files?workspace=${encodeURIComponent(workspace)}`,
  )

export const previewFile = (
  workspace: string,
  path: string,
  startLine?: number,
  endLine?: number,
) =>
  api<{
    path: string
    total_lines: number
    start_line: number
    end_line: number
    content: string
    truncated: boolean
  }>('/api/files/preview', {
    method: 'POST',
    body: JSON.stringify({
      workspace,
      path,
      start_line: startLine ?? null,
      end_line: endLine ?? null,
    }),
  })

export const exportConversationUrl = (id: number) =>
  url(`/api/conversations/${id}/export`)

export const updateConversation = (
  id: number,
  patch: { title?: string; workspace?: string; system_prompt_override?: string | null },
) =>
  api<{ ok: boolean }>(`/api/conversations/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(patch),
  })

export const cancelAgent = (id: number) =>
  api<{ ok: boolean }>(`/api/agent/${id}/cancel`, { method: 'POST' })

export const deleteFile = (workspace: string, path: string) =>
  api<{ ok: boolean }>(
    `/api/files?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`,
    { method: 'DELETE' },
  )

// ---------------------------------------------------------------- agent stream

export interface AgentEvent {
  type: 'text' | 'tool_start' | 'tool_result' | 'done' | 'error' | 'stopped'
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
  const res = await fetch(url(`/api/agent/${conversationId}`), {
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
