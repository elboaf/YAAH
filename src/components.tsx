import { useCallback, useEffect, useRef, useState } from 'react'
import { useAgent, type ChatMessage, type ToolCall } from './store'
import {
  listConversations,
  createConversation,
  getMessages,
  getConfig,
  updateConfig,
  getProviders,
  listAvailableModels,
  streamAgentTurn,
  cancelAgent,
  getFileTree,
  previewFile,
  deleteFile,
  exportConversationUrl,
  updateConversation,
  type FileEntry,
  type ProviderPreset,
} from './api'
import { diffLines, highlightLine, langOf, type DiffLine } from './codeview'

// ---------------------------------------------------------------- code views

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      className="rounded px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1200)
        })
      }}
    >
      {copied ? 'copied!' : 'copy'}
    </button>
  )
}

/** Syntax-highlighted code with line numbers (Q43). */
function CodeBlock({ code, lang, startLine = 1 }: { code: string; lang?: string; startLine?: number }) {
  const lines = code.replace(/\n$/, '').split('\n')
  return (
    <div className="my-1 overflow-hidden rounded border border-zinc-700 bg-zinc-950">
      <div className="flex items-center justify-between border-b border-zinc-800 bg-zinc-900 px-2 py-1">
        <span className="font-mono text-[10px] text-zinc-500">{lang ?? 'text'}</span>
        <CopyButton text={code} />
      </div>
      <pre className="max-h-96 overflow-auto p-1 font-mono text-[11px] leading-4">
        {lines.map((line, i) => (
          <div key={i} className="flex">
            <span className="w-10 shrink-0 select-none pr-2 text-right text-zinc-600">
              {startLine + i}
            </span>
            <span className="whitespace-pre-wrap break-all text-zinc-300">
              {highlightLine(line).map((t, j) => (
                <span key={j} className={t.cls}>{t.text}</span>
              ))}
            </span>
          </div>
        ))}
      </pre>
    </div>
  )
}

/** Old/new diff rendering for edit_file calls (Q43 diff view). */
function DiffBlock({ oldText, newText }: { oldText: string; newText: string }) {
  const lines: DiffLine[] = diffLines(oldText, newText)
  return (
    <div className="my-1 overflow-hidden rounded border border-zinc-700 bg-zinc-950">
      <div className="border-b border-zinc-800 bg-zinc-900 px-2 py-1 font-mono text-[10px] text-zinc-500">
        diff
      </div>
      <pre className="max-h-72 overflow-auto p-1 font-mono text-[11px] leading-4">
        {lines.map((l, i) => (
          <div
            key={i}
            className={
              l.kind === 'add'
                ? 'bg-emerald-950/60 text-emerald-300'
                : l.kind === 'del'
                  ? 'bg-red-950/60 text-red-300'
                  : 'text-zinc-400'
            }
          >
            <span className="select-none opacity-60">{l.kind === 'add' ? '+ ' : l.kind === 'del' ? '- ' : '  '}</span>
            {l.text}
          </div>
        ))}
      </pre>
    </div>
  )
}

// ---------------------------------------------------------------- tool calls

function ToolCallBlock({ tc }: { tc: ToolCall }) {
  const [open, setOpen] = useState(false)
  const args = (tc.args ?? {}) as Record<string, unknown>

  // Rich rendering per tool (Q43)
  const body = (() => {
    if (tc.name === 'edit_file' && typeof args.old_text === 'string' && typeof args.new_text === 'string') {
      return <DiffBlock oldText={args.old_text} newText={args.new_text} />
    }
    if (tc.name === 'read_file' && tc.result && typeof tc.result === 'object') {
      const content = (tc.result as { content?: string }).content
      const path = (tc.result as { path?: string }).path
      if (typeof content === 'string' && content) {
        return <CodeBlock code={content} lang={langOf(String(path ?? ''))} />
      }
    }
    return (
      <div className="whitespace-pre-wrap break-all text-zinc-300">
        {JSON.stringify(tc.result, null, 2)}
      </div>
    )
  })()

  return (
    <div className="my-1 rounded border border-zinc-700 bg-zinc-800/60 text-xs">
      <button
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-zinc-300 hover:bg-zinc-700/40"
        onClick={() => setOpen((o) => !o)}
      >
        <span>{tc.result !== undefined ? 'OK' : 'RUN'}</span>
        <span className="font-mono font-semibold text-amber-300">{tc.name}</span>
        <span className="ml-auto text-zinc-500">{open ? '[-]' : '[+]'}</span>
      </button>
      {open && (
        <div className="border-t border-zinc-700 px-2 py-1.5 font-mono text-[11px] text-zinc-400">
          <div className="whitespace-pre-wrap break-all text-zinc-300">
            args: {JSON.stringify(tc.args ?? {}, null, 2)}
          </div>
          {tc.result !== undefined && <div className="mt-1 max-h-96 overflow-auto">{body}</div>}
        </div>
      )}
    </div>
  )
}

/** Markdown-lite assistant rendering: fenced code blocks become CodeBlocks. */
function MessageBody({ content }: { content: string }) {
  const parts = content.split(/```/)
  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 1) {
          const nl = part.indexOf('\n')
          const lang = nl > 0 ? part.slice(0, nl).trim() : ''
          const code = nl > 0 ? part.slice(nl + 1) : part
          return <CodeBlock key={i} code={code} lang={lang || undefined} />
        }
        return (
          part.trim() && (
            <div key={i} className="whitespace-pre-wrap break-words">
              {part}
            </div>
          )
        )
      })}
    </>
  )
}

function MessageView({ msg }: { msg: ChatMessage }) {
  const isUser = msg.role === 'user'
  return (
    <div className={`flex gap-2 ${isUser ? 'justify-end' : ''}`}>
      {!isUser && (
        <span className="mt-1 select-none font-mono text-[10px] text-zinc-500">
          [{msg.role}]
        </span>
      )}
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          isUser ? 'bg-blue-600 text-white' : 'bg-zinc-800 text-zinc-100'
        }`}
      >
        {msg.content ? (
          isUser ? (
            <div className="whitespace-pre-wrap break-words">{msg.content}</div>
          ) : (
            <MessageBody content={msg.content} />
          )
        ) : null}
        {msg.toolCalls?.map((tc) => <ToolCallBlock key={tc.id} tc={tc} />)}
        {!msg.content && !msg.toolCalls?.length && (
          <span className="animate-pulse text-zinc-500">...</span>
        )}
      </div>
      {isUser && (
        <span className="mt-1 select-none font-mono text-[10px] text-zinc-500">
          [you]
        </span>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- file tree (Q8/Q12/Q40)

function TreeRow({
  entry,
  depth,
  onOpen,
  onContext,
}: {
  entry: FileEntry
  depth: number
  onOpen: (e: FileEntry) => void
  onContext: (e: FileEntry, x: number, y: number) => void
}) {
  const [openDir, setOpenDir] = useState(depth < 1)
  return (
    <>
      <button
        className="block w-full truncate rounded px-1 py-0.5 text-left text-[11px] hover:bg-zinc-800"
        style={{ paddingLeft: `${depth * 12 + 4}px` }}
        onClick={() => {
          if (entry.type === 'dir') setOpenDir((o) => !o)
          else onOpen(entry)
        }}
        onContextMenu={(e) => {
          e.preventDefault()
          onContext(entry, e.clientX, e.clientY)
        }}
      >
        <span className="mr-1 text-zinc-500">{entry.type === 'dir' ? (openDir ? '▾' : '▸') : '•'}</span>
        <span className={entry.type === 'dir' ? 'text-zinc-300' : 'text-zinc-400'}>{entry.name}</span>
      </button>
      {entry.type === 'dir' &&
        openDir &&
        entry.children?.map((c) => (
          <TreeRow key={c.path} entry={c} depth={depth + 1} onOpen={onOpen} onContext={onContext} />
        ))}
    </>
  )
}

export function FilesPanel() {
  const { workspace, previewPath, setPreviewPath } = useAgent()
  const [tree, setTree] = useState<FileEntry[]>([])
  const [menu, setMenu] = useState<{ entry: FileEntry; x: number; y: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  const refresh = useCallback(() => {
    if (!workspace || workspace === '.') {
      setTree([])
      return
    }
    getFileTree(workspace).then((r) => setTree(r.tree)).catch((e) => setErr(String(e)))
  }, [workspace])

  useEffect(refresh, [refresh])

  const openPreview = (entry: FileEntry) => setPreviewPath(entry.path)

  return (
    <aside className="hidden w-60 min-w-[200px] flex-col border-r border-zinc-800 bg-zinc-900/40 xl:flex">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-400">FILES</h2>
        <button className="text-[10px] text-zinc-500 hover:text-zinc-300" onClick={refresh}>
          refresh
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-1">
        {err && <p className="p-2 text-[10px] text-red-400">{err}</p>}
        {!err && tree.length === 0 && (
          <p className="mt-4 px-2 text-center text-[11px] text-zinc-600">
            Set a workspace to browse files.
          </p>
        )}
        {tree.map((e) => (
          <TreeRow
            key={e.path}
            entry={e}
            depth={0}
            onOpen={openPreview}
            onContext={(entry, x, y) => setMenu({ entry, x, y })}
          />
        ))}
      </div>

      {/* context menu (Q40) */}
      {menu && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setMenu(null)} onContextMenu={(e) => { e.preventDefault(); setMenu(null) }} />
          <div
            className="fixed z-50 w-40 rounded border border-zinc-700 bg-zinc-900 py-1 text-xs shadow-xl"
            style={{ left: Math.min(menu.x, window.innerWidth - 170), top: Math.min(menu.y, window.innerHeight - 120) }}
          >
            {menu.entry.type === 'file' && (
              <button
                className="block w-full px-3 py-1 text-left text-zinc-300 hover:bg-zinc-800"
                onClick={() => {
                  openPreview(menu.entry)
                  setMenu(null)
                }}
              >
                Preview
              </button>
            )}
            <button
              className="block w-full px-3 py-1 text-left text-red-400 hover:bg-zinc-800"
              onClick={() => {
                if (confirm(`Delete ${menu.entry.path}?`)) {
                  deleteFile(workspace, menu.entry.path)
                    .then(refresh)
                    .catch((e) => setErr(String(e)))
                }
                setMenu(null)
              }}
            >
              Delete
            </button>
          </div>
        </>
      )}

      {/* click a file row also previews; highlight the open one */}
      <style>{`button[style] { cursor: default; }`}</style>
      {previewPath === null && tree.length > 0 && null}
    </aside>
  )
}

// ---------------------------------------------------------------- right panel: activity + preview (Q44)

function PreviewPane() {
  const { workspace, previewPath, setPreviewPath } = useAgent()
  const [file, setFile] = useState<{
    path: string
    content: string
    total_lines: number
    end_line: number
    truncated: boolean
  } | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!previewPath) {
      setFile(null)
      return
    }
    previewFile(workspace, previewPath)
      .then(setFile)
      .catch((e) => setErr(String(e)))
  }, [workspace, previewPath])

  if (!previewPath) return null
  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <h2 className="truncate font-mono text-xs text-zinc-300">{previewPath}</h2>
        <button className="ml-2 text-[10px] text-zinc-500 hover:text-zinc-300" onClick={() => setPreviewPath(null)}>
          close
        </button>
      </div>
      {err && <p className="p-2 text-[10px] text-red-400">{err}</p>}
      {file && (
        <div className="flex-1 overflow-auto">
          <CodeBlock code={file.content} lang={langOf(file.path)} />
          {file.truncated && (
            <p className="px-2 pb-2 text-[10px] text-zinc-500">
              Showing lines 1-{file.end_line} of {file.total_lines}.
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function ActivityPanel() {
  const { log, clearLog, previewPath } = useAgent()
  return (
    <aside className="hidden w-[22rem] min-w-[260px] flex-col border-l border-zinc-800 bg-zinc-900/60 lg:flex">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-400">
          {previewPath ? 'PREVIEW' : 'ACTIVITY'}
        </h2>
        {!previewPath && (
          <button onClick={clearLog} className="text-[10px] text-zinc-500 hover:text-zinc-300">
            clear
          </button>
        )}
      </div>
      {previewPath ? (
        <PreviewPane />
      ) : (
        <div className="flex-1 overflow-y-auto p-2 font-mono text-[11px]">
          {log.length === 0 && (
            <p className="mt-6 text-center text-zinc-600">No tool activity yet.</p>
          )}
          {log.map((e) => (
            <div key={e.id} className="mb-2 rounded border border-zinc-800 bg-zinc-900 p-1.5">
              <div className="flex justify-between text-zinc-500">
                <span className="text-amber-300">{e.name ?? 'system'}</span>
                <span>{e.time}</span>
              </div>
              {e.args !== undefined && (
                <pre className="mt-1 max-h-24 overflow-auto whitespace-pre-wrap break-all text-zinc-300">
                  {typeof e.args === 'string' ? e.args : JSON.stringify(e.args, null, 2)}
                </pre>
              )}
              {e.result !== undefined && (
                <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all text-emerald-300/80">
                  {typeof e.result === 'string' ? e.result : JSON.stringify(e.result, null, 2)}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}
    </aside>
  )
}

export { ActivityPanel }

// ---------------------------------------------------------------- sidebar

function ConversationList() {
  const { conversationId, setConversationId, loadHistory, setWorkspace, newConversation } = useAgent()
  const [convs, setConvs] = useState<Array<{ id: number; title: string; workspace: string | null }>>([])

  const refresh = useCallback(() => {
    listConversations().then(setConvs).catch(() => setConvs([]))
  }, [])
  useEffect(() => {
    refresh()
  }, [conversationId, refresh])

  return (
    <div className="flex-1 overflow-y-auto">
      {convs.map((c) => (
        <div key={c.id} className="group flex items-center gap-1">
          <button
            className={`min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left text-xs ${
              c.id === conversationId ? 'bg-blue-600 text-white' : 'text-zinc-300 hover:bg-zinc-800'
            }`}
            onClick={() => {
              setConversationId(c.id)
              if (c.workspace) setWorkspace(c.workspace)
              getMessages(c.id).then(loadHistory).catch(() => {})
            }}
          >
            {c.title}
          </button>
          {c.id === conversationId && (
            <>
              <a
                title="Export as Markdown (Q39)"
                href={exportConversationUrl(c.id)}
                download
                className="rounded px-1 py-1.5 text-[10px] text-zinc-400 opacity-0 hover:text-zinc-200 group-hover:opacity-100"
              >
                md↓
              </a>
              <button
                title="System prompt override (Q17)"
                className="rounded px-1 py-1.5 text-[10px] text-zinc-400 opacity-0 hover:text-zinc-200 group-hover:opacity-100"
                onClick={() => {
                  const p = prompt('System prompt override for this conversation (empty = default):')
                  if (p !== null) {
                    updateConversation(c.id, { system_prompt_override: p || null }).catch(() => {})
                  }
                }}
              >
                sys
              </button>
            </>
          )}
        </div>
      ))}
      {convs.length === 0 && (
        <p className="px-2 py-3 text-center text-[11px] text-zinc-600">No conversations yet.</p>
      )}
      <button className="mt-2 w-full rounded px-2 py-1 text-left text-[10px] text-zinc-600 hover:text-zinc-400" onClick={refresh}>
        refresh
      </button>
    </div>
  )
}

export function Sidebar() {
  const { newConversation, workspace, setWorkspace, clearLog, conversationId } = useAgent()
  const [wsInput, setWsInput] = useState(workspace)
  const [model, setModel] = useState('...')
  const [models, setModels] = useState<string[]>([])
  const [modelErr, setModelErr] = useState<string | null>(null)
  const [savingModel, setSavingModel] = useState(false)
  const [showSettings, setShowSettings] = useState(false)

  // Current model plus the list served by the configured endpoint. The key
  // never reaches the browser, so the backend fills it in from saved config.
  const refreshModels = useCallback(() => {
    setModelErr(null)
    listAvailableModels()
      .then((r) => {
        if (r.error) setModelErr(r.error)
        setModels(r.models)
      })
      .catch((e) => setModelErr(String(e)))
  }, [])

  useEffect(() => {
    getConfig().then((c) => setModel(c.model)).catch(() => {})
    refreshModels()
  }, [conversationId, refreshModels])

  const pickModel = (m: string) => {
    if (!m || m === model) return
    setSavingModel(true)
    setModel(m)
    updateConfig({ model: m })
      .then(refreshModels)
      .catch((e) => setModelErr(String(e)))
      .finally(() => setSavingModel(false))
  }

  const browseWorkspace = async () => {
    // Native folder picker when running inside Tauri (Q28)
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      const picked = await invoke<string | null>('pick_workspace')
      if (picked) {
        setWsInput(picked)
        setWorkspace(picked)
      }
    } catch {
      /* not running in Tauri; keep manual input */
    }
  }

  return (
    <>
      <aside className="flex w-64 min-w-[220px] flex-col border-r border-zinc-800 bg-zinc-900 p-3 text-sm">
        <h1 className="mb-3 font-semibold text-zinc-200">AI Coding Agent</h1>
        <button
          className="mb-3 rounded bg-blue-600 px-3 py-1.5 text-white hover:bg-blue-500"
          onClick={() => {
            newConversation()
            clearLog()
          }}
        >
          + New chat
        </button>
        <label className="mb-1 block text-xs text-zinc-500">Workspace</label>
        <input
          className="mb-1 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200"
          value={wsInput}
          onChange={(e) => setWsInput(e.target.value)}
          onBlur={() => setWorkspace(wsInput || '.')}
          placeholder="/path/to/project"
        />
        <button
          className="mb-3 self-start rounded px-1 text-[10px] text-zinc-500 hover:text-zinc-300"
          onClick={() => void browseWorkspace()}
        >
          browse...
        </button>
        <div className="mb-3">
          <label className="mb-1 block text-xs text-zinc-500">
            Model{savingModel ? ' (saving...)' : ''}
          </label>
          <select
            className="w-full truncate rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200"
            value={models.includes(model) ? model : ''}
            onChange={(e) => pickModel(e.target.value)}
            disabled={models.length === 0}
            title={models.length === 0 ? 'No models available — check Settings' : model}
          >
            {/* placeholder row unless the active model is one of the options */}
            {!models.includes(model) && (
              <option value="">{model || 'Select a model'}</option>
            )}
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          {modelErr && <p className="mt-1 text-[10px] text-amber-400">{modelErr}</p>}
        </div>
        <button
          className="mb-3 rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
          onClick={() => setShowSettings(true)}
        >
          Settings
        </button>
        <ConversationList />
      </aside>
      {showSettings && (
        <SettingsModal
          onClose={() => {
            setShowSettings(false)
            getConfig().then((c) => setModel(c.model)).catch(() => {})
            refreshModels()
          }}
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------- settings (Q4/Q10/Q31/Q35/Q41)

function SettingsModal({ onClose }: { onClose: () => void }) {
  const [apiKey, setApiKey] = useState('')
  const [apiBase, setApiBase] = useState('')
  const [maskedKey, setMaskedKey] = useState('')
  const [temperature, setTemperature] = useState<number | ''>('')
  const [maxTokens, setMaxTokens] = useState<number | ''>('')
  const [presets, setPresets] = useState<Record<string, ProviderPreset>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getConfig()
      .then((c) => {
        setApiBase(c.api_base ?? '')
        setMaskedKey(c.api_key ?? '')
        setTemperature(c.temperature ?? '')
        setMaxTokens(c.max_tokens ?? '')
      })
      .catch((e) => setErr(String(e)))
    getProviders().then(setPresets).catch(() => {})
  }, [])

  const applyPreset = (name: string) => {
    const p = presets[name]
    if (!p) return
    setApiBase(p.api_base)
  }

  const save = async () => {
    setSaving(true)
    setErr(null)
    try {
      await updateConfig({
        api_key: apiKey || undefined,
        api_base: apiBase || undefined,
        temperature: temperature === '' ? undefined : Number(temperature),
        max_tokens: maxTokens === '' ? undefined : Number(maxTokens),
      })
      setSaved(true)
      setTimeout(onClose, 600)
    } catch (e) {
      setErr(String(e))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={onClose}
    >
      <div
        className="max-h-[90vh] w-96 overflow-y-auto rounded-lg border border-zinc-700 bg-zinc-900 p-4 text-sm text-zinc-200"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-3 font-semibold">Settings</h2>

        {/* provider presets (Q31) */}
        <label className="mb-1 block text-xs text-zinc-500">Provider preset</label>
        <div className="mb-2 flex flex-wrap gap-1">
          {Object.keys(presets).map((name) => (
            <button
              key={name}
              className={`rounded px-2 py-1 text-[11px] ${
                presets[name].api_base === apiBase
                  ? 'bg-blue-600 text-white'
                  : 'border border-zinc-700 text-zinc-300 hover:bg-zinc-800'
              }`}
              onClick={() => applyPreset(name)}
            >
              {name}
            </button>
          ))}
        </div>

        <label className="mb-1 block text-xs text-zinc-500">API base URL</label>
        <input
          className="mb-3 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
          value={apiBase}
          onChange={(e) => setApiBase(e.target.value)}
          placeholder="https://api.openai.com/v1"
        />

        <label className="mb-1 block text-xs text-zinc-500">API key</label>
        <input
          type="password"
          className="mb-1 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
          placeholder={maskedKey ? 'key saved' : 'sk-...'}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <p className="mb-3 text-[10px] text-zinc-600">
          Leave blank to keep the existing key. Models are chosen from the dropdown in the sidebar.
        </p>

        <div className="mb-3 mt-2 flex gap-2">
          <div className="flex-1">
            <label className="mb-1 block text-xs text-zinc-500">Temperature</label>
            <input
              type="number"
              step="0.1"
              min="0"
              max="2"
              className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
              value={temperature}
              onChange={(e) => setTemperature(e.target.value === '' ? '' : Number(e.target.value))}
            />
          </div>
          <div className="flex-1">
            <label className="mb-1 block text-xs text-zinc-500">Max tokens</label>
            <input
              type="number"
              min="1"
              className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
              value={maxTokens}
              onChange={(e) => setMaxTokens(e.target.value === '' ? '' : Number(e.target.value))}
            />
          </div>
        </div>

        {err && <p className="mb-2 text-xs text-red-400">{err}</p>}

        <div className="flex justify-end gap-2">
          <button
            className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:opacity-50"
            disabled={saving}
            onClick={save}
          >
            {saved ? 'Saved!' : saving ? 'Saving...' : 'Save'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- chat

export function ChatPanel() {
  const { messages, status, error } = useAgent()
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <main className="flex flex-1 flex-col">
      <div className="flex-1 space-y-3 overflow-y-auto p-4">
        {messages.length === 0 && (
          <p className="mt-10 text-center text-sm text-zinc-600">
            Start a conversation. Tool calls will appear inline.
          </p>
        )}
        {messages.map((m) => (
          <MessageView key={m.id} msg={m} />
        ))}
        <div ref={bottomRef} />
      </div>
      {error && (
        <div className="border-t border-red-900 bg-red-950/60 px-4 py-2 text-xs text-red-300">
          {error}
        </div>
      )}
      <Composer />
      <div className="px-4 pb-1 text-[10px] text-zinc-600">status: {status}</div>
    </main>
  )
}

interface Attachment {
  name: string
  content: string
}

function Composer() {
  const {
    conversationId,
    status,
    workspace,
    appendUserMessage,
    appendAssistantPlaceholder,
    appendTextDelta,
    startToolCall,
    finishToolCall,
    setStatus,
    setError,
    setConversationId,
    pushLog,
    setAbortController,
  } = useAgent()
  const abortController = useAgent((s) => s.abortController)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [dragOver, setDragOver] = useState(false)

  const readDroppedFiles = (files: FileList) => {
    void (async () => {
      const added: Attachment[] = []
      for (const f of Array.from(files)) {
        // text attachments only (Q33); skip anything that looks binary
        if (f.size > 200_000) continue
        try {
          const content = await f.text()
          if (content.includes('\u0000')) continue
          added.push({ name: f.name, content })
        } catch {
          /* unreadable file: skip */
        }
      }
      if (added.length) setAttachments((a) => [...a, ...added])
    })()
  }

  const send = async () => {
    const text = input.trim()
    if ((!text && attachments.length === 0) || sending) return
    setSending(true)

    // Inline attachments as fenced blocks (Q33)
    let fullText = text
    for (const a of attachments) {
      fullText += `\n\n--- attached file: ${a.name} ---\n\`\`\`\n${a.content}\n\`\`\``
    }

    setInput('')
    setAttachments([])
    setError(null)
    appendUserMessage(fullText)
    const asstId = appendAssistantPlaceholder()
    const ac = new AbortController()
    setAbortController(ac)
    try {
      let cid: number
      if (conversationId === null) {
        const created = await createConversation(fullText.slice(0, 40) || 'New chat', workspace)
        cid = created.id
        setConversationId(cid)
      } else {
        cid = conversationId
      }
      setStatus('thinking')
      await streamAgentTurn(
        cid,
        fullText,
        workspace,
        (ev) => {
          if (ev.type === 'text') {
            setStatus('thinking')
            if (ev.text) appendTextDelta(asstId, ev.text)
          } else if (ev.type === 'tool_start') {
            setStatus('running-tool')
            startToolCall(asstId, ev.call_id ?? '', ev.name ?? 'tool', ev.args)
            pushLog({ kind: 'tool', name: ev.name, args: ev.args })
          } else if (ev.type === 'tool_result') {
            finishToolCall(asstId, ev.call_id ?? '', ev.result)
            pushLog({ kind: 'tool', name: ev.name, result: ev.result })
          } else if (ev.type === 'error') {
            setStatus('error')
            setError(ev.message ?? 'Unknown agent error')
          } else if (ev.type === 'stopped') {
            setStatus('idle')
            appendTextDelta(asstId, '\n[stopped]')
          } else if (ev.type === 'done') {
            setStatus('idle')
          }
        },
        ac.signal,
      )
      if (useAgent.getState().status !== 'error') setStatus('idle')
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setStatus('idle')
        appendTextDelta(asstId, '\n[stopped]')
      } else {
        setStatus('error')
        setError(String(e))
      }
    } finally {
      setSending(false)
      setAbortController(null)
    }
  }

  const stop = () => {
    // Cancel server-side (mid-loop) and abort the client stream
    if (conversationId !== null) void cancelAgent(conversationId).catch(() => {})
    abortController?.abort()
  }

  return (
    <div
      className="border-t border-zinc-800 p-3"
      onDragOver={(e) => {
        e.preventDefault()
        setDragOver(true)
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setDragOver(false)
        if (e.dataTransfer?.files?.length) readDroppedFiles(e.dataTransfer.files)
      }}
    >
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1">
          {attachments.map((a, i) => (
            <span
              key={i}
              className="flex items-center gap-1 rounded bg-zinc-800 px-2 py-0.5 font-mono text-[10px] text-zinc-300"
            >
              {a.name}
              <button
                className="text-zinc-500 hover:text-red-400"
                onClick={() => setAttachments((arr) => arr.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <textarea
          className={`flex-1 resize-none rounded border bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none ${
            dragOver ? 'border-blue-500' : 'border-zinc-700'
          }`}
          rows={2}
          placeholder="Describe a task... (Enter to send, Shift+Enter for newline, drop text files to attach)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <button
          className="self-end rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-500 disabled:opacity-50"
          onClick={() => void send()}
          disabled={sending || (!input.trim() && attachments.length === 0)}
        >
          Send
        </button>
        {sending && (
          <button
            className="self-end rounded border border-red-700 px-3 py-2 text-sm text-red-300 hover:bg-red-950"
            onClick={stop}
          >
            Stop
          </button>
        )}
      </div>
    </div>
  )
}
