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
  /** 'agent' = a scheduled agent's pinned chat (issue #41). */
  chat_type?: 'chat' | 'agent'
}

export interface ContextInfo {
  /** Exact tokens (usage.prompt_tokens) of the session's latest model call. */
  context_tokens: number | null
  /** Model id the count was measured with (may differ from the active model). */
  context_model: string | null
  /** Currently active model id. */
  model: string | null
  /** Resolved context window (override -> provider -> table), null = unknown. */
  context_window: number | null
}

export const getContext = (id: number) =>
  api<ContextInfo>(`/api/conversations/${id}/context`)

export interface GitBranchInfo {
  branch: string | null
}

export const getGitBranch = (id: number) =>
  api<GitBranchInfo>(`/api/conversations/${id}/git-branch`)

/** Everything the status strip's git cluster reads: branch, dirty state,
 *  +N −N line counts, local vs upstream hashes, ahead/behind, file counts. */
export interface GitInfo {
  branch: string
  upstream: string | null
  local_hash: string | null
  remote_hash: string | null
  ahead: number
  behind: number
  added: number
  deleted: number
  dirty: boolean
  untracked: number
  changed: number
}

export const getGitInfo = (id: number) =>
  api<{ info: GitInfo | null }>(`/api/conversations/${id}/git-info`)

export const getGitBranches = (id: number) =>
  api<{ branches: string[] }>(`/api/conversations/${id}/git-branches`)

export type GitAction = 'status' | 'commit' | 'push' | 'pull' | 'checkout'

export interface GitCommandResult {
  ok: boolean
  output?: string
  error?: string
  note?: string
}

export const runGitCommand = (id: number, action: GitAction, opts?: { message?: string; branch?: string }) =>
  api<GitCommandResult>(`/api/conversations/${id}/git-command`, {
    method: 'POST',
    body: JSON.stringify({ action, message: opts?.message, branch: opts?.branch }),
  })

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
  /** Reasoning effort (#6): "" = don't send the param; low | medium | high. */
  reasoning_effort?: string
  /** Workspace used last, restored into the sidebar on startup. */
  last_workspace?: string
  /** Access mode gating tool execution: ask | plan | full. */
  access_mode?: 'ask' | 'plan' | 'full'
  /** Interface scale (CSS zoom on the app root); 1.0 = default ramp. */
  ui_scale?: number
  /** Per-model context-window overrides (model id -> tokens). */
  context_window_overrides?: Record<string, number>
  /** Voice dictation; cloud_api_key arrives masked ("set" | ""). */
  voice?: {
    engine: 'local' | 'cloud'
    cloud_endpoint: string
    cloud_api_key: string
    cloud_model: string
    /** System-wide push-to-talk hotkey (Tauri accelerator, "" disables). */
    ptt_hotkey: string
    /** Read-aloud (TTS): on/off, voice name, speaking rate. */
    tts_enabled?: boolean
    tts_voice?: string
    tts_speed?: number
    /** Notification chimes (#29): run-finished + question-pending sounds. */
    sounds_enabled?: boolean
  }
  /** LAN hosting (this instance as a host). */
  remote?: {
    hosting_enabled: boolean
    passphrase: string
    display_name: string
  }
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
    reasoning_effort: string
    ui_scale: number
    access_mode: 'ask' | 'plan' | 'full'
    context_window_overrides: Record<string, number | null>
    voice: {
      engine?: 'local' | 'cloud'
      cloud_endpoint?: string
      cloud_api_key?: string
      cloud_model?: string
      ptt_hotkey?: string
      tts_enabled?: boolean
      tts_voice?: string
      tts_speed?: number
      sounds_enabled?: boolean
    }
    remote?: {
      hosting_enabled?: boolean
      passphrase?: string
      display_name?: string
    }
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
  /** True when children were not loaded yet (fetch via getFileChildren). */
  lazy?: boolean
}

export const getFileTree = (workspace: string) =>
  api<{ root: string; tree: FileEntry[] }>(
    `/api/files?workspace=${encodeURIComponent(workspace)}`,
  )

/** Children of one directory (lazy tree expansion; dirs come back lazy). */
export const getFileChildren = (workspace: string, path: string) =>
  api<{ entries: FileEntry[] }>(
    `/api/files/children?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`,
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

/** This machine's registry only (the sidebar greys it while connected). */
export const listLocalWorkspaces = () =>
  api<WorkspaceRow[]>('/api/workspaces/local')

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

// ---- Message queue + steering (issue #7) ----

export interface QueuedItem {
  id: number
  text: string
  skills: string[]
}

export const queueMessage = (id: number, message: string, skills: string[] = []) =>
  api<{ ok: boolean; item: QueuedItem }>(`/api/agent/${id}/queue`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, skills }),
  })

export const fetchQueue = (id: number) =>
  api<{ items: QueuedItem[] }>(`/api/agent/${id}/queue`)

export const removeQueued = (id: number, itemId: number) =>
  api<{ ok: boolean }>(`/api/agent/${id}/queue/${itemId}`, { method: 'DELETE' })

export const steerAgent = (id: number) =>
  api<{ ok: boolean }>(`/api/agent/${id}/steer`, { method: 'POST' })

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

// ---- MCP tool servers ----

export interface McpToolInfo {
  name: string
  description: string
}

export interface McpServerInfo {
  name: string
  status: 'starting' | 'connected' | 'failed' | 'stopped'
  error: string
  command: string
  args: string[]
  tools: McpToolInfo[]
}

export const listMcpServers = () =>
  api<{ servers: McpServerInfo[] }>('/api/mcp/servers')

export const addMcpServer = (body: { name: string; command: string; args: string[] }) =>
  api<{ servers: McpServerInfo[] }>('/api/mcp/servers', {
    method: 'POST',
    body: JSON.stringify(body),
  })

export const removeMcpServer = (name: string) =>
  api<{ servers: McpServerInfo[] }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
    method: 'DELETE',
  })

export const reloadMcpServers = () =>
  api<{ servers: McpServerInfo[] }>('/api/mcp/reload', { method: 'POST' })

// ---- Scheduled agents (issue #41) ----

export type AgentPolicy = 'sandbox-only' | 'autonomous'
export type AgentScheduleType = 'interval' | 'daily' | 'weekly'
export interface AgentScheduleSpec {
  minutes?: number
  time?: string
  weekday?: number
}

export interface AgentInstruction {
  id: number
  agent_id: string
  content: string
  created_at: string
}

export interface ScheduledAgent {
  id: string
  workspace: string
  name: string
  prompt: string
  schedule_type: AgentScheduleType
  schedule_spec: AgentScheduleSpec
  schedule_text: string
  approval_policy: AgentPolicy
  model: string
  effort: string
  memory_enabled: boolean
  retention: number
  notify_on_success: boolean
  enabled: boolean
  conversation_id: number
  next_fire_at: string
  last_fired_at: string
  last_finished_at: string
  last_status: string
  running: boolean
  instructions: AgentInstruction[]
  chat_title: string
}

export interface AgentsPayload {
  agents: ScheduledAgent[]
  retry: { retry_count: number; retry_backoff_minutes: number }
}

/** The edit/create form payload — the dialogue edits the whole record. */
export type AgentBody = Omit<
  ScheduledAgent,
  'id' | 'schedule_spec' | 'schedule_text' | 'running' | 'instructions' | 'chat_title' |
    'conversation_id' | 'next_fire_at' | 'last_fired_at' | 'last_finished_at' | 'last_status'
> & { schedule_spec: AgentScheduleSpec }

export const listAgents = (workspace?: string) =>
  api<AgentsPayload>(
    '/api/agents' + (workspace !== undefined ? `?workspace=${encodeURIComponent(workspace)}` : ''),
  )

/** One live UI-stream event of a scheduled agent run (see getAgentTape). */
export interface AgentTapeEvent {
  type: string
  text?: string
  name?: string
  command?: string
  chunk?: string
  result?: string
  message?: string
}

/** Poll the in-backend event buffer of the agent run in this conversation:
 * pass back `offset` from the previous call to get only new events. */
export const getAgentTape = (conversationId: number, after: number) =>
  api<{ running: boolean; offset: number; events: AgentTapeEvent[] }>(
    `/api/agents/tape?conversation_id=${conversationId}&after=${after}`,
  )

export const addAgent = (body: AgentBody) =>
  api<ScheduledAgent>('/api/agents', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const updateAgent = (id: string, body: AgentBody) =>
  api<ScheduledAgent>(`/api/agents/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })

export const deleteAgent = (id: string, deleteChat = true) =>
  api<{ ok: boolean }>(
    `/api/agents/${encodeURIComponent(id)}?delete_chat=${deleteChat ? 'true' : 'false'}`,
    { method: 'DELETE' },
  )

export const runAgentNow = (id: string) =>
  api<{ ok: boolean }>(`/api/agents/${encodeURIComponent(id)}/run`, { method: 'POST' })

// Standing instructions: typed messages in an agent chat become these; they
// never trigger a run — they ride along with the prompt at every fire.
export const addAgentInstruction = (agentId: string, content: string) =>
  api<AgentInstruction>(`/api/agents/${encodeURIComponent(agentId)}/instructions`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content }),
  })

export const updateAgentInstruction = (agentId: string, instructionId: number, content: string) =>
  api<{ ok: boolean }>(
    `/api/agents/${encodeURIComponent(agentId)}/instructions/${instructionId}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    },
  )

export const deleteAgentInstruction = (agentId: string, instructionId: number) =>
  api<{ ok: boolean }>(
    `/api/agents/${encodeURIComponent(agentId)}/instructions/${instructionId}`,
    { method: 'DELETE' },
  )

export const setAgentRetry = (retry_count: number, retry_backoff_minutes: number) =>
  api<{ ok: boolean }>('/api/agents/retry', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ retry_count, retry_backoff_minutes }),
  })

/** Stage an attached text file inside the workspace; returns the
 *  workspace-relative path the agent's read_file tool can open. */
export const uploadAttachment = (workspace: string, name: string, content: string) =>
  api<{ path: string }>('/api/attachments', {
    method: 'POST',
    body: JSON.stringify({ workspace, name, content }),
  })

export interface TranscribeStatus {
  engine: 'local' | 'cloud'
  local_available: boolean
  local_model: string | null
  cloud_configured: boolean
}

export const transcribeStatus = () => api<TranscribeStatus>('/api/transcribe/status')

/** Transcribe a WAV blob (raw body — keeps the sidecar multipart-free).
 *  `language` is the whisper-detected source language (null when the engine
 *  doesn't report one) — the PTT answer router uses it to reject answers
 *  dictated in a language the option labels aren't written in. */
export async function transcribeAudio(wav: Blob): Promise<{ text: string; language: string | null }> {
  let res: Response
  try {
    res = await fetch(url('/api/transcribe'), {
      method: 'POST',
      headers: { 'Content-Type': 'audio/wav' },
      body: wav,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    signalBackendDown()
    throw new Error('Backend is unreachable (restarting)')
  }
  const body = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(body.detail || `transcription failed (${res.status})`)
  return { text: body.text ?? '', language: body.language ?? null }
}

// ---------------------------------------------------------------- tts (read-aloud)

export interface TtsStatus {
  available: boolean
  model: string
  model_bytes: number
  voices: string[]
  default_voice: string
  tts_enabled: boolean
  tts_voice: string
  tts_speed: number
  downloading: boolean
}
export const ttsStatus = () => api<TtsStatus>('/api/tts/status')

/** Fire-and-forget stop handshake: raises the backend's supersede floor to
 *  `floor` (the frontend's current utterance generation), so any in-flight
 *  chunk belonging to an older-or-equal generation aborts at its next
 *  sentence boundary instead of holding the engine. */
export const ttsStop = (floor: number) => {
  if (floor <= 0) return
  fetch(url('/api/tts/stop'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ floor }),
  }).catch(() => {})
}

/** Synthesize one prose chunk; resolves to a WAV blob. 409 = either the
 *  utterance was superseded (body detail "superseded" — benign, drop it) or
 *  the model is missing (offer the Settings download). voice/speed/epoch
 *  override the stored settings (Settings preview); omitted = server default. */
export async function ttsSynthesize(
  text: string,
  signal?: AbortSignal,
  opts?: { voice?: string; speed?: number; epoch?: number },
): Promise<Blob> {
  let res: Response
  try {
    res = await fetch(url('/api/tts/synthesize'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        voice: opts?.voice,
        speed: opts?.speed,
        epoch: opts?.epoch,
      }),
      signal,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw e
    signalBackendDown()
    throw new Error('Backend is unreachable (restarting)')
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const err = new Error(body.detail || `synthesis failed (${res.status})`) as Error & { status?: number }
    err.status = res.status
    throw err
  }
  return res.blob()
}

/** Download the TTS model; onLine receives each progress JSON line. */
export async function ttsDownload(onLine: (p: { stage: string; received?: number; total?: number; detail?: string }) => void): Promise<void> {
  let res: Response
  try {
    res = await fetch(url('/api/tts/download'), { method: 'POST' })
  } catch {
    signalBackendDown()
    throw new Error('Backend is unreachable (restarting)')
  }
  if (!res.ok || !res.body) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `download failed (${res.status})`)
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
      if (line) onLine(JSON.parse(line))
    }
  }
}


export const deleteFile = (workspace: string, path: string) =>
  api<{ ok: boolean }>(
    `/api/files?workspace=${encodeURIComponent(workspace)}&path=${encodeURIComponent(path)}`,
    { method: 'DELETE' },
  )

// ---------------------------------------------------------------- remote hosting

export interface RemoteHostFound {
  name: string
  host: string
  port: number
  protocol: number
  /** Per-process instance id — used to hide this machine itself. */
  iid: string
  /** Stable per-host id — scopes conversations across restarts. */
  hid?: string
  os: string
  /** Host requires a passphrase. */
  auth: boolean
}

export interface RemoteStatus {
  connected: boolean
  url?: string
  name?: string
  host_id?: string
  os?: string
  app_version?: string
  workspace_root?: string
}

/** mDNS sweep (~2.5s) for YAAH hosts on this LAN. */
export const discoverHosts = () =>
  api<{ hosts: RemoteHostFound[] }>('/api/remote/discover')

/** This instance's own handshake info (instance id, hostname, …). */
export const localInstanceInfo = () =>
  api<{ instance_id: string; hostname: string }>('/api/remote/info')

export const remoteStatus = () => api<RemoteStatus>('/api/remote/status')

export const connectRemote = (url: string, passphrase: string) =>
  api<RemoteStatus>('/api/remote/connect', {
    method: 'POST',
    body: JSON.stringify({ url, passphrase }),
  })

export const disconnectRemote = () =>
  api<{ ok: boolean }>('/api/remote/disconnect', { method: 'POST' })

// ---------------------------------------------------------------- agent stream

export interface AgentEvent {
  type:
    | 'text'
    | 'say'
    | 'user_injected'
    | 'queued_autosend'
    | 'thinking'
    | 'tool_start'
    | 'tool_progress'
    | 'tool_result'
    | 'approval_request'
    | 'approval_decision'
    | 'sub_agent_spawned'
    | 'sub_agent_progress'
    | 'sub_agent_done'
    | 'model_call'
    | 'done'
    | 'error'
    | 'stopped'
  text?: string
  name?: string
  args?: unknown
  result?: unknown
  message?: string
  call_id?: string
  /** Spoken briefing for read-aloud (#66) — speech-only, never rendered. */
  say?: string
  /** Soft injection landed (#7): the queued message is now a real turn. */
  user_injected_id?: number
  /** Run ended with messages still queued (#7): auto-send them. */
  queued_autosend_items?: Array<{ id: number; text: string }>
  /** Live output chunk while a shell tool runs (tool_progress). */
  chunk?: string
  /** Sub-agent identity (sub_agent_* events). */
  agent_id?: number
  /** Provider + model a pending chat call is waiting on (model_call, #43). */
  provider?: string
  agent_type?: string
  prompt?: string
  status?: string
  turns?: number
  /** Inner event type wrapped by sub_agent_progress (text | tool_start | tool_result). */
  kind?: string
  /** Exact context size (usage.prompt_tokens) of the turn's final model call. */
  usage_tokens?: number
  /** The provider+model this turn's chat call is waiting on (model_call).
   *  Unset between the response arriving and the next call of the turn. */
  modelCall?: { provider: string; model: string; startedAt: number }
  model?: string
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
  /** Fires as soon as the POST is dispatched and each time a model_call
   *  event arrives — feeds the "waiting for <provider>" elapsed readout,
   *  which must tick between events, not just on them. */
  onModelCall?: (mc: { provider: string; model: string; startedAt: number } | null) => void,
): Promise<void> {
  onModelCall?.(null)
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
      if (!line) continue
      const ev = JSON.parse(line) as AgentEvent
      if (ev.type === 'model_call') {
        onModelCall?.({
          provider: ev.provider ?? '',
          model: ev.model ?? '',
          startedAt: Date.now(),
        })
      } else if (ev.type !== 'thinking') {
        // Any other stream activity means the call is no longer pending —
        // thinking deltas still mean "the call hasn't spoken yet", so the
        // waiting readout stays up while reasoning streams.
        onModelCall?.(null)
      }
      onEvent(ev)
    }
  }
}
