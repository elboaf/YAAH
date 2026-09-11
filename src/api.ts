// Detect the Tauri webview via its IPC internals — the origin alone is not
// reliable: macOS uses tauri:// but Windows/Linux use http://tauri.localhost,
// which looks like a normal http origin to a naive protocol check.
export const IS_TAURI =
  typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window
export const BASE = IS_TAURI ? 'http://127.0.0.1:8765' : ''
const url = (p: string) => `${BASE}${p}`

// Listen for backend lifecycle events reported by the Tauri shell
// (supervisor thread: down | up | error).
export let backendStartupError: string | null = null
if (typeof window !== 'undefined') {
  window.addEventListener('backend-status', (e) => {
    const { status, message } = (e as CustomEvent<{ status?: string; message?: string }>).detail
    if (status === 'error') backendStartupError = message ?? 'backend failed to start'
  })
}

/** Fire the UI-wide "backend unreachable" signal used by the recovery banner. */
export function signalBackendDown() {
  window.dispatchEvent(new CustomEvent('backend-down'))
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(url(path), {
      headers: { 'Content-Type': 'application/json' },
      ...init,
    })
  } catch (e) {
    // Network-level failure (ECONNREFUSED etc.) means the backend process
    // is down; the Tauri supervisor respawns it and the banner reloads the
    // app once /api/health answers again.
    if ((e as Error).name === 'AbortError') throw e
    signalBackendDown()
    throw new Error(`Backend is unreachable (restarting): ${url(path)}`)
  }
  const text = await res.text()
  // A JSON parse failure here almost always means an HTML page came back
  // (SPA fallback / static server) because the API backend isn't reachable.
  const contentType = res.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    signalBackendDown()
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

export const createConversation = (title: string, workspace?: string | null) =>
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
      images?: string[] | null
      tool_calls: Array<{
        id?: string
        type?: string
        function?: { name?: string; arguments?: string }
      }> | null
    }>
  >(`/api/conversations/${id}/messages`)

/** URL for a stored image (rel path under backend/data/images/). */
export const imageUrl = (rel: string) => `${BASE}/api/images/${rel}`

// ---------------------------------------------------------------- config

export interface ProviderConfig {
  api_base: string
  api_key: string // masked from server: 'set' or ''
  model: string
}

export interface AgentConfig {
  providers: Record<string, ProviderConfig>
  active_provider: string
  api_base: string // derived from active provider
  api_key: string // masked, derived
  model: string // derived
  temperature?: number
  max_tokens?: number
  max_steps?: number
  /** Workspace used last, restored into the sidebar on startup. */
  last_workspace?: string
}

export const getConfig = () => api<AgentConfig>('/api/config')

/** Remember the workspace for the next app launch (stored in config.json). */
export const updateLastWorkspace = (workspace: string) =>
  api<{ ok: boolean }>('/api/config/last-workspace', {
    method: 'POST',
    body: JSON.stringify({ workspace }),
  })
export const updateConfig = (
  patch: Partial<{
    providers: Record<string, Partial<ProviderConfig>>
    active_provider: string
    temperature: number
    max_tokens: number
    max_steps: number
  }>,
) =>
  api<{ ok: boolean }>('/api/config', {
    method: 'PUT',
    body: JSON.stringify(patch),
  })

/** Pick a model from a provider's group: activates that provider. */
export const setActiveModel = (provider: string, model: string) =>
  api<{ ok: boolean }>('/api/config/active-model', {
    method: 'POST',
    body: JSON.stringify({ provider, model }),
  })

export interface ProviderPreset {
  api_base: string
  model: string
  needs_api_key: boolean
  supports_tools: boolean | null
}

export const getProviders = () =>
  api<Record<string, ProviderPreset>>('/api/providers')

/** Per-provider model listing, with the error when a provider is unreachable. */
export interface ProviderModels {
  models: string[]
  error?: string
}

/**
 * Models offered by every configured provider, queried in parallel
 * server-side. API keys never leave the backend.
 */
export const listAvailableModels = () =>
  api<{
    providers: Record<string, ProviderModels>
    active_provider: string
    model: string
  }>('/api/models/available')

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
  fields: { title?: string; workspace?: string | null; system_prompt_override?: string | null },
) =>
  api<{ ok: boolean }>(`/api/conversations/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(fields),
  })

export const deleteConversation = (id: number) =>
  api<{ ok: boolean }>(`/api/conversations/${id}`, { method: 'DELETE' })

// ---------------------------------------------------------------- workspaces

export interface WorkspaceRow {
  id: number
  /** null = the Default pseudo-workspace (no root directory). */
  path: string | null
  label: string
  last_opened_at: string | null
  exists: boolean
  conversation_count: number
}

export const listWorkspaces = () => api<WorkspaceRow[]>('/api/workspaces')

export const addWorkspace = (path: string) =>
  api<WorkspaceRow>('/api/workspaces', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })

export const deleteWorkspace = (id: number) =>
  api<{ ok: boolean; relocated: number }>(`/api/workspaces/${id}`, {
    method: 'DELETE',
  })

// The export endpoint sets Content-Disposition: attachment, but the `download`
// attribute on an anchor is ignored cross-origin (tauri.localhost -> 127.0.0.1),
// so we fetch the body ourselves and trigger the download from a blob URL.
export async function exportConversationMarkdown(id: number, title: string) {
  const res = await fetch(url(`/api/conversations/${id}/export`))
  if (!res.ok) throw new Error(`${res.status}: export failed`)
  const blob = await res.blob()
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `${title.replace(/[^\w -]/g, '').trim() || 'conversation'}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(a.href)
}

export const cancelAgent = (id: number) =>
  api<{ ok: boolean }>(`/api/agent/${id}/cancel`, { method: 'POST' })

/** Answer a pending ask_user question; the blocked agent loop resumes. */
export const submitAnswer = (id: number, callId: string, answer: string) =>
  api<{ ok: boolean }>(`/api/conversations/${id}/answer`, {
    method: 'POST',
    body: JSON.stringify({ call_id: callId, answer }),
  })

// ---------------------------------------------------------------- skills

export interface SkillInfo {
  name: string
  description: string
  disable_model_invocation: boolean
  path: string
}

export const listSkills = () => api<{ skills: SkillInfo[] }>('/api/skills')

export const refreshSkills = () =>
  api<{ skills: SkillInfo[] }>('/api/skills/refresh', { method: 'POST' })

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
  images: string[] = [],
  skills: string[] = [],
  resume = false,
): Promise<void> {
  let res: Response
  try {
    res = await fetch(url(`/api/agent/${conversationId}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, workspace, images, skills, resume }),
      signal,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    signalBackendDown()
    throw e
  }
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
