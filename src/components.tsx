import { useCallback, useEffect, useRef, useState } from 'react'
import {
  listConversations,
  createConversation,
  getMessages,
  getConfig,
  updateConfig,
  setActiveModel,
  getProviders,
  listAvailableModels,
  type ProviderModels,
  streamAgentTurn,
  type AgentEvent,
  cancelAgent,
  getFileTree,
  previewFile,
  deleteFile,
  exportConversationMarkdown,
  deleteConversation,
  updateConversation,
  submitAnswer,
  listSkills,
  refreshSkills,
  uploadAttachment,
  transcribeStatus,
  transcribeAudio,
  imageUrl,
  listWorkspaces,
  addWorkspace,
  deleteWorkspace,
  type FileEntry,
  type ProviderPreset,
  type SkillInfo,
  type WorkspaceRow,
} from './api'
import { useAgent, type ChatMessage, type PendingQuestion, type ToolCall } from './store'
import { diffLines, highlightLine, langOf, type DiffLine } from './codeview'
import { VoiceRecorder } from './voice'

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

/** One option row of an ask_user card: label + trade-off description. */
function AskOptionRow({
  label,
  description,
  chosen,
  disabled,
  onClick,
}: {
  label: string
  description?: string
  chosen?: boolean
  disabled?: boolean
  onClick?: () => void
}) {
  return (
    <button
      className={`block w-full rounded border px-2.5 py-1.5 text-left text-xs ${
        chosen
          ? 'border-orange-500 bg-orange-950/40 text-orange-200'
          : 'border-zinc-700 bg-zinc-800/60 text-zinc-200 enabled:hover:border-orange-500/60 enabled:hover:bg-zinc-800'
      } disabled:cursor-default`}
      disabled={disabled}
      onClick={onClick}
    >
      <span className="font-medium">{label}</span>
      {description && <span className="block text-[11px] text-zinc-400">{description}</span>}
    </button>
  )
}

/** Live question waiting for the user's answer; rendered above the composer
 *  while the agent blocks on ask_user. Options submit directly; 'Something
 *  else…' reveals a free-text field. */
function AskUserCard({ pending }: { pending: PendingQuestion }) {
  const conversationId = useAgent((s) => s.conversationId)
  const setPendingQuestion = useAgent((s) => s.setPendingQuestion)
  const [customOpen, setCustomOpen] = useState(false)
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const answer = (text: string) => {
    if (conversationId === null || submitting) return
    setSubmitting(true)
    setErr(null)
    submitAnswer(conversationId, pending.callId, text)
      .then(() => {
        // Clear only if this is still the same question (a newer ask in
        // another conversation may have replaced it meanwhile).
        setPendingQuestion((q) => (q && q.callId === pending.callId ? null : q))
      })
      .catch((e) => {
        setErr(String(e))
        setSubmitting(false)
      })
  }

  return (
    <div className="rounded-lg border border-orange-700/60 bg-zinc-900 p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-orange-400">
        <span className="run-pulse">?</span> agent asks — pick an answer
      </div>
      <p className="mb-2.5 whitespace-pre-wrap text-sm text-zinc-100">{pending.question}</p>
      <div className="space-y-1.5">
        {pending.options.map((o, i) => (
          <AskOptionRow
            key={i}
            label={o.label}
            description={o.description}
            disabled={submitting}
            onClick={() => answer(o.label)}
          />
        ))}
        {!customOpen ? (
          <button
            className="block w-full rounded border border-dashed border-zinc-600 px-2.5 py-1.5 text-left text-xs text-zinc-400 hover:border-orange-500/60 hover:text-zinc-200"
            disabled={submitting}
            onClick={() => setCustomOpen(true)}
          >
            Something else…
          </button>
        ) : (
          <div className="flex gap-1.5">
            <input
              autoFocus
              className="flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-orange-500 focus:outline-none"
              placeholder="Type your own answer…"
              value={custom}
              onChange={(e) => setCustom(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && custom.trim()) {
                  e.preventDefault()
                  answer(custom.trim())
                }
              }}
            />
            <button
              className="rounded bg-orange-600 px-3 py-1.5 text-xs text-white hover:bg-orange-500 disabled:opacity-50"
              disabled={submitting || !custom.trim()}
              onClick={() => answer(custom.trim())}
            >
              {submitting ? '…' : 'Send'}
            </button>
          </div>
        )}
      </div>
      {err && <p className="mt-2 text-[11px] text-red-400">{err}</p>}
    </div>
  )
}

/** Answered ask_user call as it appears in the trace/history. */
function AskUserTrace({ tc }: { tc: ToolCall }) {
  const args = (tc.args ?? {}) as {
    question?: string
    options?: Array<{ label: string; description?: string }>
  }
  const result = (tc.result ?? {}) as { answer?: string | null; note?: string }
  const answer = typeof result.answer === 'string' && result.answer ? result.answer : null
  return (
    <div className="rounded border border-zinc-700 bg-zinc-900/60 p-2">
      <p className="mb-1.5 whitespace-pre-wrap font-sans text-xs text-zinc-200">
        {args.question ?? ''}
      </p>
      <div className="space-y-1">
        {(args.options ?? []).map((o, i) => (
          <AskOptionRow
            key={i}
            label={o.label}
            description={o.description}
            chosen={answer !== null && o.label === answer}
            disabled
          />
        ))}
        {answer !== null && !(args.options ?? []).some((o) => o.label === answer) && (
          <AskOptionRow label={answer} chosen disabled />
        )}
      </div>
      {answer === null && (
        <p className="mt-1 font-sans text-[11px] text-zinc-500">
          {result.note ?? 'not answered'}
        </p>
      )}
    </div>
  )
}

/** Icon + color identity per tool, so rows read at a glance. */
function toolGlyph(name: string): string {
  if (name === 'ask_user') return '?'
  if (name === 'read_file') return '▤'
  if (name === 'search_files') return '⌕'
  if (name === 'bash' || name === 'powershell') return '❯'
  if (name === 'edit_file') return '✎'
  if (name === 'write_file') return '✚'
  if (name === 'web_search') return '⌕'
  if (name === 'web_fetch') return '☰'
  if (name === 'view_image') return '▣'
  return '⚙'
}

function toolGlyphColor(name: string): string {
  if (name === 'ask_user') return 'text-orange-400'
  if (name === 'read_file') return 'text-sky-400'
  if (name === 'search_files') return 'text-violet-400'
  if (name === 'bash') return 'text-emerald-400'
  if (name === 'powershell') return 'text-blue-400'
  if (name === 'web_search' || name === 'web_fetch') return 'text-cyan-400'
  if (name === 'view_image') return 'text-pink-400'
  if (name === 'edit_file' || name === 'write_file') return 'text-amber-400'
  return 'text-zinc-400'
}

/** Short human target for a call: the file, query, or command it acts on. */
function toolTarget(tc: ToolCall): string {
  const a = (tc.args ?? {}) as Record<string, unknown>
  const pick = (...keys: string[]) => {
    for (const k of keys) {
      const v = a[k]
      if (typeof v === 'string' && v) return v
    }
    return undefined
  }
  const t = (pick('path', 'file_path', 'query', 'pattern', 'command', 'url', 'question') ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  return t.length > 48 ? t.slice(0, 48) + '…' : t
}

/** One compact chip: glyph + name + target, pulsing while the call runs. */
function ToolChip({ tc }: { tc: ToolCall }) {
  const done = tc.result !== undefined
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded px-1.5 py-0.5 font-mono text-[11px] ${
        done ? 'bg-zinc-800/70 text-zinc-400' : 'bg-zinc-700/60 text-zinc-200'
      }`}
    >
      <span className={toolGlyphColor(tc.name)}>{toolGlyph(tc.name)}</span>
      <span>{tc.name}</span>
      {toolTarget(tc) && <span className="text-zinc-500">{toolTarget(tc)}</span>}
      {!done && <span className="run-pulse text-amber-300">●</span>}
    </span>
  )
}

/** Live, ephemeral stream of calls while the agent works. Newest chip appears
 *  at the left edge and older ones are pushed right, fading out at the right
 *  edge; the row never grows past the chat panel's width. */
function ToolTicker({ calls }: { calls: ToolCall[] }) {
  const recent = calls.slice(-12)
  const fade =
    'linear-gradient(to right, black 72%, rgba(0,0,0,0.35) 90%, transparent 100%)'
  return (
    <div
      className="my-1 flex w-full min-w-0 items-center gap-1.5 overflow-hidden"
      style={{ maskImage: fade, WebkitMaskImage: fade }}
    >
      <span className="shrink-0 font-mono text-[10px] text-zinc-600">
        {calls.length > recent.length ? `${calls.length} calls` : 'working…'}
      </span>
      {[...recent].reverse().map((tc, i) => (
        <span key={tc.id} className={`shrink-0 ${i === 0 ? 'chip-in' : ''}`}>
          <ToolChip tc={tc} />
        </span>
      ))}
    </div>
  )
}

/** Finished turn's collapsed trace: one line, expandable to full detail. */
function TraceLine({ calls }: { calls: ToolCall[] }) {
  const [open, setOpen] = useState(false)
  const byName = new Map<string, number>()
  for (const tc of calls) byName.set(tc.name, (byName.get(tc.name) ?? 0) + 1)
  return (
    <div className="my-1">
      <button
        className="flex max-w-full items-center gap-2 font-mono text-[11px] text-zinc-500 hover:text-zinc-300"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-zinc-600">{open ? '▾' : '▸'}</span>
        <span className="shrink-0">
          {calls.length} call{calls.length === 1 ? '' : 's'}
        </span>
        <span className="truncate">
          {[...byName].map(([n, c], i) => (
            <span key={n}>
              {i > 0 && <span className="text-zinc-700"> · </span>}
              <span className={toolGlyphColor(n)}>{toolGlyph(n)}</span> {n}
              {c > 1 ? ` ×${c}` : ''}
            </span>
          ))}
        </span>
      </button>
      {open && (
        <div className="mt-1 space-y-0.5 border-l border-zinc-800 pl-2">
          {calls.map((tc) => (
            <ToolCallRow key={tc.id} tc={tc} />
          ))}
        </div>
      )}
    </div>
  )
}

/** A single call: chip row, expandable to args/result detail. */
function ToolCallRow({ tc }: { tc: ToolCall }) {
  const [open, setOpen] = useState(false)
  const args = (tc.args ?? {}) as Record<string, unknown>

  // Rich rendering per tool (Q43)
  const body = (() => {
    if (tc.name === 'ask_user') {
      return <AskUserTrace tc={tc} />
    }
    if (tc.name === 'edit_file' && typeof args.old_text === 'string' && typeof args.new_text === 'string') {
      return <DiffBlock oldText={args.old_text} newText={args.new_text} />
    }
    if (tc.name === 'view_image' && tc.result && typeof tc.result === 'object') {
      const rel = (tc.result as { image?: string }).image
      if (typeof rel === 'string' && rel) {
        return (
          <img
            src={imageUrl(rel)}
            alt="view_image result"
            className="max-h-64 rounded border border-zinc-700"
          />
        )
      }
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
    <div className="font-mono text-[11px]">
      <button
        className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-zinc-800/60"
        onClick={() => setOpen((o) => !o)}
      >
        <ToolChip tc={tc} />
        <span className="shrink-0 text-zinc-600">{open ? '[-]' : '[+]'}</span>
      </button>
      {open && (
        <div className="border-l border-zinc-800 px-2 py-1 text-zinc-400">
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

function MessageView({ msg, live }: { msg: ChatMessage; live?: boolean }) {
  // Persisted failure markers (backend writes role='system' when a turn
  // dies): a slim machine line, not a fake agent message.
  if (msg.role === 'system') {
    if (!msg.content) return null
    return (
      <div className="font-mono text-[11px] text-red-400/90">
        <span className="mr-1.5 text-zinc-600">⚠</span>
        {msg.content}
      </div>
    )
  }

  // Persisted tool-role messages (history load) render as one slim call row.
  if (msg.role === 'tool') {
    const tc = msg.toolCalls?.[0]
    if (!tc) return null
    return (
      <div className="pl-3">
        <ToolCallRow tc={tc} />
      </div>
    )
  }

  const isUser = msg.role === 'user'
  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] rounded border border-zinc-700/70 bg-zinc-800/60 px-3 py-2 text-sm text-zinc-100">
          {msg.images?.length ? (
            <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
              {msg.images.map((rel, i) => (
                <a key={i} href={imageSrc(rel)} target="_blank" rel="noreferrer">
                  <img
                    src={imageSrc(rel)}
                    alt="attachment"
                    className="max-h-40 rounded border border-zinc-700"
                  />
                </a>
              ))}
            </div>
          ) : null}
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        </div>
      </div>
    )
  }

  return (
    <div className="border-l-2 border-zinc-700/70 pl-3">
      <div className="mb-0.5 select-none font-mono text-[10px] uppercase tracking-widest text-zinc-600">
        agent
      </div>
      {msg.content ? (
        <div className="max-w-prose text-sm leading-relaxed text-zinc-200">
          <MessageBody content={msg.content} />
        </div>
      ) : null}
      {msg.toolCalls?.length ? (
        live ? (
          <ToolTicker calls={msg.toolCalls} />
        ) : (
          <TraceLine calls={msg.toolCalls} />
        )
      ) : null}
      {!msg.content && !msg.toolCalls?.length && (
        <span className="run-pulse font-mono text-sm text-zinc-500">▊</span>
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
        onKeyDown={(e) => {
          // Keyboard equivalent of right-click (Windows convention).
          if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
            e.preventDefault()
            const r = e.currentTarget.getBoundingClientRect()
            onContext(entry, r.left + 8, r.bottom + 4)
          }
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
  const { workspace, previewPath, setPreviewPath, status } = useAgent()
  const [tree, setTree] = useState<FileEntry[]>([])
  const [menu, setMenu] = useState<{ entry: FileEntry; x: number; y: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('filesPanelCollapsed') === '1')
  const [deleteTarget, setDeleteTarget] = useState<FileEntry | null>(null)

  const toggleCollapsed = () =>
    setCollapsed((c) => {
      localStorage.setItem('filesPanelCollapsed', c ? '0' : '1')
      return !c
    })

  const refresh = useCallback(() => {
    if (workspace === '.') {
      setTree([])
      setErr(null)
      return
    }
    setLoading(true)
    getFileTree(workspace)
      .then((r) => {
        setTree(r.tree)
        setErr(null)
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [workspace])

  useEffect(refresh, [refresh])

  // The panel exists to watch the agent's workspace: refetch when a turn
  // ends (running-tool -> idle), so files it wrote appear without a manual
  // refresh. Collapsed while the workspace is empty still skips the fetch.
  const prevStatus = useRef(status)
  useEffect(() => {
    if (prevStatus.current === 'running-tool' && status === 'idle') refresh()
    prevStatus.current = status
  }, [status, refresh])

  // Esc closes the context menu, matching every other floating surface.
  useEffect(() => {
    if (!menu) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenu(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menu])

  const openPreview = (entry: FileEntry) => setPreviewPath(entry.path)

  if (collapsed) {
    return (
      <aside className="hidden w-7 min-w-[28px] flex-col items-center border-r border-zinc-800 bg-zinc-900/40 py-2 xl:flex">
        <button
          className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300"
          title="Show files"
          aria-label="Show files panel"
          onClick={toggleCollapsed}
        >
          ▸
        </button>
        <button
          className="mt-2 flex-1 text-[10px] font-semibold tracking-wide text-zinc-500 hover:text-zinc-300"
          style={{ writingMode: 'vertical-rl' }}
          aria-label="Show files panel"
          onClick={toggleCollapsed}
        >
          FILES
        </button>
      </aside>
    )
  }

  return (
    <aside className="hidden w-60 min-w-[200px] flex-col border-r border-zinc-800 bg-zinc-900/40 xl:flex">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <button
          className="rounded p-0.5 text-zinc-500 hover:text-zinc-300"
          title="Hide files"
          aria-label="Hide files panel"
          onClick={toggleCollapsed}
        >
          ▾
        </button>
        <h2 className="flex-1 text-xs font-semibold tracking-wide text-zinc-400">FILES</h2>
        <button
          className="text-[10px] text-zinc-500 hover:text-zinc-300"
          aria-label="Refresh file tree"
          onClick={refresh}
        >
          refresh
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-1">
        {err ? (
          <div className="p-2">
            <p className="text-[10px] leading-relaxed text-red-400">{err}</p>
            <button
              className="mt-1 text-[10px] text-zinc-500 hover:text-zinc-300"
              onClick={refresh}
            >
              retry
            </button>
          </div>
        ) : loading ? (
          <p className="mt-4 px-2 text-center font-mono text-[11px] text-zinc-600">
            loading…
          </p>
        ) : tree.length === 0 ? (
          <p className="mt-4 px-2 text-center text-[11px] text-zinc-600">
            No files in the workspace.
          </p>
        ) : (
          tree.map((e) => (
            <TreeRow
              key={e.path}
              entry={e}
              depth={0}
              onOpen={openPreview}
              onContext={(entry, x, y) => setMenu({ entry, x, y })}
            />
          ))
        )}
      </div>

      {/* context menu (Q40) */}
      {menu && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setMenu(null)} onContextMenu={(e) => { e.preventDefault(); setMenu(null) }} />
          <div
            className="fixed z-50 w-40 rounded border border-zinc-700 bg-zinc-900 py-1 text-xs shadow-xl"
            role="menu"
            aria-label={`Actions for ${menu.entry.path}`}
            style={{ left: Math.min(menu.x, window.innerWidth - 170), top: Math.min(menu.y, window.innerHeight - 120) }}
          >
            {menu.entry.type === 'file' && (
              <button
                className="block w-full px-3 py-1 text-left text-zinc-300 hover:bg-zinc-800"
                role="menuitem"
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
              role="menuitem"
              onClick={() => {
                setDeleteTarget(menu.entry)
                setMenu(null)
              }}
            >
              Delete
            </button>
          </div>
        </>
      )}

      {/* in-app delete confirmation (replaces native confirm) */}
      {deleteTarget && (
        <ConfirmDialog
          title="Delete file?"
          body={`${deleteTarget.path} will be removed from disk. This cannot be undone.`}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            deleteFile(workspace, deleteTarget.path)
              .then(refresh)
              .catch((e) => setErr(String(e)))
            setDeleteTarget(null)
          }}
        />
      )}
    </aside>
  )
}

// ---------------------------------------------------------------- preview modal (Q44)

/** File preview as a modal overlay over the chat; Esc or backdrop closes. */
export function PreviewModal() {
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
    setErr(null)
    previewFile(workspace, previewPath)
      .then(setFile)
      .catch((e) => setErr(String(e)))
  }, [workspace, previewPath])

  useEffect(() => {
    if (!previewPath) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPreviewPath(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [previewPath, setPreviewPath])

  if (!previewPath) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-8"
      onClick={() => setPreviewPath(null)}
    >
      <div
        className="flex max-h-full w-full max-w-3xl flex-col overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
          <h2 className="truncate font-mono text-xs text-zinc-300">{previewPath}</h2>
          <button
            className="ml-2 text-[10px] text-zinc-500 hover:text-zinc-300"
            onClick={() => setPreviewPath(null)}
          >
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
    </div>
  )
}

// ---------------------------------------------------------------- dialogs

/** Shared modal chrome: scrim, Esc, backdrop click. All in-app dialogs
 *  build on this so Esc/backdrop behavior matches PreviewModal. */
function DialogShell({ children, onClose }: { children: React.ReactNode; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-8"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  )
}

/** Destructive confirmation, replacing native confirm(). */
function ConfirmDialog({
  title,
  body,
  confirmLabel = 'Delete',
  onConfirm,
  onCancel,
}: {
  title: string
  body: string
  confirmLabel?: string
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <DialogShell onClose={onCancel}>
      <div className="p-4">
        <h2 className="mb-1 text-sm font-semibold text-zinc-100">{title}</h2>
        <p className="mb-4 break-words text-xs leading-relaxed text-zinc-400">{body}</p>
        <div className="flex justify-end gap-2">
          <button
            className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            autoFocus
            className="rounded border border-red-700 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950"
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </DialogShell>
  )
}

/** Single-value input dialog, replacing native prompt() — multi-line, since
 *  a system prompt is prose, not one line. */
function PromptDialog({
  title,
  label,
  initial,
  placeholder,
  onOK,
  onCancel,
}: {
  title: string
  label: string
  initial: string
  placeholder?: string
  onOK: (value: string) => void
  onCancel: () => void
}) {
  const [value, setValue] = useState(initial)
  return (
    <DialogShell onClose={onCancel}>
      <div className="p-4">
        <h2 className="mb-1 text-sm font-semibold text-zinc-100">{title}</h2>
        <label className="mb-1 block text-[10px] text-zinc-500">{label}</label>
        <textarea
          rows={5}
          autoFocus
          value={value}
          placeholder={placeholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
              e.preventDefault()
              onOK(value)
            }
          }}
          className="w-full resize-y rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 font-mono text-xs text-zinc-100 focus:border-blue-500 focus:outline-none"
        />
        <div className="mt-3 flex justify-end gap-2">
          <button
            className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500"
            onClick={() => onOK(value)}
          >
            Save
          </button>
        </div>
      </div>
    </DialogShell>
  )
}

/** Failure notice, replacing native alert(). */
function NoticeDialog({
  title,
  message,
  onClose,
}: {
  title: string
  message: string
  onClose: () => void
}) {
  return (
    <DialogShell onClose={onClose}>
      <div className="p-4">
        <h2 className="mb-1 text-sm font-semibold text-red-300">{title}</h2>
        <p className="mb-4 break-all text-xs leading-relaxed text-zinc-400">{message}</p>
        <div className="flex justify-end">
          <button
            autoFocus
            className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            onClick={onClose}
          >
            OK
          </button>
        </div>
      </div>
    </DialogShell>
  )
}

// ---------------------------------------------------------------- sidebar

/** Relative timestamp for a conversation row ("2h", "3d", "May 2"). */
function relTime(iso: string | null): string {
  if (!iso) return ''
  const t = new Date(iso.includes('T') ? iso : iso.replace(' ', 'T') + 'Z').getTime()
  if (Number.isNaN(t)) return ''
  const mins = Math.round((Date.now() - t) / 60000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h`
  const days = Math.round(hours / 24)
  if (days < 30) return `${days}d`
  return new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Basename of a workspace path for group headers. */
const wsBasename = (path: string) => {
  const norm = path.replace(/[\\/]+$/, '')
  const idx = Math.max(norm.lastIndexOf('\\'), norm.lastIndexOf('/'))
  return idx >= 0 ? norm.slice(idx + 1) : norm
}

/** localStorage key for a group's collapsed state. */
const collapseKey = (path: string | null) =>
  `yaah.group.collapsed.${path ?? 'default'}`

function ConversationList() {
  const { conversationId, setConversationId, loadHistory, setWorkspace, newConversation } = useAgent()
  const [convs, setConvs] = useState<Array<{ id: number; title: string; workspace: string | null; updated_at: string }>>([])
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([])
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({})
  const [notice, setNotice] = useState<{ title: string; message: string } | null>(null)
  const [sysTarget, setSysTarget] = useState<{ id: number; title: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{ id: number; title: string } | null>(null)
  const [removeWsTarget, setRemoveWsTarget] = useState<WorkspaceRow | null>(null)
  const [menuOpenId, setMenuOpenId] = useState<number | null>(null)

  const refresh = useCallback(() => {
    listConversations().then(setConvs).catch(() => setConvs([]))
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
  }, [])
  useEffect(() => {
    refresh()
  }, [conversationId, refresh])

  // Collapse state persists per workspace (Q14); groups start expanded.
  useEffect(() => {
    const next: Record<string, boolean> = {}
    for (const w of workspaces) {
      const key = collapseKey(w.path ?? '')
      try {
        next[key] = localStorage.getItem(key) === '1'
      } catch {
        next[key] = false
      }
    }
    setCollapsed(next)
  }, [workspaces])

  const toggleCollapsed = (path: string | null) => {
    const key = collapseKey(path ?? '')
    setCollapsed((c) => {
      const v = !c[key]
      try {
        localStorage.setItem(key, v ? '1' : '0')
      } catch {
        /* non-persistent collapse is fine */
      }
      return { ...c, [key]: v }
    })
  }

  /** Open a conversation and adopt its workspace (the core invariant: the
   *  open conversation's workspace IS the active workspace, both ways). */
  const openConversation = (c: { id: number; workspace: string | null }) => {
    setConversationId(c.id)
    setWorkspace(c.workspace ?? '')
    getMessages(c.id)
      .then((rows) => loadHistory(c.id, rows))
      .catch(() => {})
  }

  /** Header body click = open that workspace's most recent conversation,
   *  or a fresh chat in it when the group is empty (same as the dropdown). */
  const openWorkspace = (w: WorkspaceRow) => {
    const latest = convs.find((c) => (c.workspace ?? null) === w.path)
    if (latest) {
      openConversation(latest)
    } else {
      setWorkspace(w.path ?? '')
      newConversation()
    }
  }

  // Group rows by workspace; Default (null path) first, then by the most
  // recent conversation activity in each group.
  const groups: Array<{ ws: WorkspaceRow; items: typeof convs }> = []
  for (const w of workspaces) {
    groups.push({ ws: w, items: convs.filter((c) => (c.workspace ?? null) === w.path) })
  }
  const knownPaths = new Set(workspaces.map((w) => w.path))
  for (const c of convs) {
    const p = c.workspace ?? null
    if (!knownPaths.has(p)) {
      // A conversation filed under a path the registry doesn't know yet
      // (created between registry refreshes) still gets its group.
      groups.push({
        ws: {
          id: -1,
          path: p,
          label: p === null ? 'Default (Home)' : wsBasename(p),
          last_opened_at: null,
          exists: p === null || true,
          conversation_count: 0,
        },
        items: [],
      })
      knownPaths.add(p)
    }
  }
  groups.sort((a, b) => {
    if ((a.ws.path ?? null) === null) return -1
    if ((b.ws.path ?? null) === null) return 1
    const at = a.items[0]?.updated_at ?? a.ws.last_opened_at ?? ''
    const bt = b.items[0]?.updated_at ?? b.ws.last_opened_at ?? ''
    return bt.localeCompare(at)
  })
  for (const g of groups) {
    g.items.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  }

  return (
    <div className="flex-1 overflow-y-auto">
      {groups.map(({ ws, items }) => {
        const key = collapseKey(ws.path ?? '')
        const isCollapsed = collapsed[key] ?? false
        return (
          <div key={ws.path ?? 'default'} className="mb-1">
            <div className="group flex items-center gap-0.5 rounded px-1 py-1 hover:bg-zinc-800/60">
              <button
                className="rounded px-0.5 text-[10px] text-zinc-500 hover:text-zinc-200"
                aria-label={isCollapsed ? 'Expand group' : 'Collapse group'}
                aria-expanded={!isCollapsed}
                onClick={() => toggleCollapsed(ws.path)}
              >
                {isCollapsed ? '▸' : '▾'}
              </button>
              <button
                className="min-w-0 flex-1 truncate text-left font-mono text-[10px] uppercase tracking-wider text-zinc-400 hover:text-zinc-200"
                title={ws.path ?? 'No root directory — conversations without a workspace'}
                onClick={() => openWorkspace(ws)}
              >
                {ws.label}
                {ws.path !== null && !ws.exists && (
                  <span className="ml-1 text-amber-500" title="Folder not found on disk">
                    ⚠
                  </span>
                )}
              </button>
              {ws.path !== null && (
                <button
                  className="rounded px-1 text-[10px] text-zinc-600 opacity-0 hover:text-red-400 group-hover:opacity-100"
                  title="Remove this workspace (its conversations move to Default)"
                  onClick={() => setRemoveWsTarget(ws)}
                >
                  ✕
                </button>
              )}
            </div>
            {!isCollapsed &&
              (items.length > 0 ? (
                items.map((c) => (
                  <ConversationRow
                    key={c.id}
                    conv={c}
                    active={c.id === conversationId}
                    menuOpen={menuOpenId === c.id}
                    setMenuOpen={(open) => setMenuOpenId(open ? c.id : null)}
                    onOpen={() => openConversation(c)}
                    onExport={() =>
                      exportConversationMarkdown(c.id, c.title).catch((e) =>
                        setNotice({ title: 'Export failed', message: String(e?.message ?? e) }),
                      )
                    }
                    onSys={() => setSysTarget({ id: c.id, title: c.title })}
                    onDelete={() => setDeleteTarget({ id: c.id, title: c.title })}
                  />
                ))
              ) : (
                <p className="px-3 py-1 text-[10px] text-zinc-600">No conversations yet.</p>
              ))}
          </div>
        )
      })}
      {convs.length === 0 && groups.length === 0 && (
        <p className="px-2 py-3 text-center text-[11px] text-zinc-600">No conversations yet.</p>
      )}

      {/* in-app dialogs (replace native confirm/prompt/alert) */}
      {notice && (
        <NoticeDialog
          title={notice.title}
          message={notice.message}
          onClose={() => setNotice(null)}
        />
      )}
      {sysTarget && (
        <PromptDialog
          title="System prompt override"
          label={`Applies to "${sysTarget.title}" only. Empty saves the default.`}
          initial=""
          placeholder="Leave empty to use the default system prompt…"
          onCancel={() => setSysTarget(null)}
          onOK={(value) => {
            updateConversation(sysTarget.id, { system_prompt_override: value || null }).catch(
              (e) => setNotice({ title: 'Save failed', message: String(e?.message ?? e) }),
            )
            setSysTarget(null)
          }}
        />
      )}
      {deleteTarget && (
        <ConfirmDialog
          title="Delete conversation?"
          body={`"${deleteTarget.title}" and all its messages will be removed. This cannot be undone.`}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={() => {
            deleteConversation(deleteTarget.id)
              .then(() => {
                if (deleteTarget.id === conversationId) {
                  newConversation()
                } else {
                  refresh()
                }
              })
              .catch((e) =>
                setNotice({ title: 'Delete failed', message: String(e?.message ?? e) }),
              )
            setDeleteTarget(null)
          }}
        />
      )}
      {removeWsTarget && (
        <ConfirmDialog
          title={`Remove workspace "${removeWsTarget.label}"?`}
          body={
            removeWsTarget.conversation_count > 0
              ? `${removeWsTarget.conversation_count} conversation${removeWsTarget.conversation_count === 1 ? '' : 's'} will move to Default. The folder on disk is not touched.`
              : 'The folder on disk is not touched.'
          }
          confirmLabel="Remove"
          onCancel={() => setRemoveWsTarget(null)}
          onConfirm={() => {
            deleteWorkspace(removeWsTarget.id)
              .then((r) => {
                // If the open conversation was relocated, follow it to Default.
                if (conversationId !== null) {
                  getMessages(conversationId)
                    .then((rows) => loadHistory(conversationId, rows))
                    .catch(() => {})
                  const moved = convs.find(
                    (c) => c.id === conversationId && c.workspace === removeWsTarget.path,
                  )
                  if (moved) setWorkspace('')
                }
                refresh()
              })
              .catch((e) =>
                setNotice({ title: 'Remove failed', message: String(e?.message ?? e) }),
              )
            setRemoveWsTarget(null)
          }}
        />
      )}
    </div>
  )
}

/** One conversation row: title + relative timestamp, hover-revealed ⋯ menu
 *  with labeled actions (replaces the bare md↓/sys/✕ glyph strip). */
function ConversationRow({
  conv,
  active,
  menuOpen,
  setMenuOpen,
  onOpen,
  onExport,
  onSys,
  onDelete,
}: {
  conv: { id: number; title: string; updated_at: string }
  active: boolean
  menuOpen: boolean
  setMenuOpen: (open: boolean) => void
  onOpen: () => void
  onExport: () => void
  onSys: () => void
  onDelete: () => void
}) {
  return (
    <div className="group relative flex items-center">
      <button
        className={`min-w-0 flex-1 truncate rounded px-2 py-1.5 text-left text-xs ${
          active ? 'bg-blue-600 text-white' : 'text-zinc-300 hover:bg-zinc-800'
        }`}
        onClick={onOpen}
        title={conv.title}
      >
        {conv.title}
        <span
          className={`ml-1.5 font-mono text-[9px] ${active ? 'text-blue-200' : 'text-zinc-600'}`}
        >
          {relTime(conv.updated_at)}
        </span>
      </button>
      <div className={`absolute right-1 ${menuOpen ? '' : 'opacity-0 group-hover:opacity-100'}`}>
        <button
          className="rounded bg-zinc-800 px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-700"
          aria-label="Conversation actions"
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen(!menuOpen)}
        >
          ⋯
        </button>
        {menuOpen && (
          <div className="absolute right-0 top-6 z-20 w-44 rounded border border-zinc-700 bg-zinc-900 py-1 shadow-xl">
            <button
              className="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
              onClick={() => {
                setMenuOpen(false)
                onExport()
              }}
            >
              Export as Markdown
            </button>
            <button
              className="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
              onClick={() => {
                setMenuOpen(false)
                onSys()
              }}
            >
              System prompt override
            </button>
            <button
              className="block w-full px-3 py-1.5 text-left text-xs text-red-400 hover:bg-zinc-800"
              onClick={() => {
                setMenuOpen(false)
                onDelete()
              }}
            >
              Delete conversation
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

export function Sidebar() {
  const { newConversation, workspace, setWorkspace, clearLog, conversationId, setConversationId, loadHistory } = useAgent()
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([])
  const [model, setModel] = useState('...')
  const [activeProvider, setActiveProvider] = useState('')
  // name -> {models, error?} for every configured provider
  const [byProvider, setByProvider] = useState<Record<string, ProviderModels>>({})
  const [savingModel, setSavingModel] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [notice, setNotice] = useState<{ title: string; message: string } | null>(null)

  // The workspace is remembered across restarts: the store seeds itself from
  // localStorage, and config.json is the durable fallback for a fresh install,
  // cleared storage, or a first run on a new machine.
  useEffect(() => {
    if (workspace && workspace !== '.') return
    getConfig()
      .then((c) => {
        if (c.last_workspace) setWorkspace(c.last_workspace)
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Registry rows for the dropdown; refreshed when the conversation changes
  // (a new conversation may have filed a workspace the list hasn't seen).
  const refreshWorkspaces = useCallback(() => {
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
  }, [])
  useEffect(refreshWorkspaces, [refreshWorkspaces, conversationId])

  // Merged model list: every configured provider, queried in parallel by the
  // backend (keys never reach the browser). Grouped per provider in the dropdown.
  const refreshModels = useCallback(() => {
    listAvailableModels()
      .then((r) => {
        setByProvider(r.providers)
        setActiveProvider(r.active_provider)
        setModel(r.model)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    getConfig()
      .then((c) => {
        setModel(c.model)
        setActiveProvider(c.active_provider)
      })
      .catch(() => {})
    refreshModels()
  }, [conversationId, refreshModels])

  // value encoding "provider::model" keeps providers with clashing ids apart
  const pickModel = (value: string) => {
    const idx = value.indexOf('::')
    if (idx < 0) return
    const provider = value.slice(0, idx)
    const m = value.slice(idx + 2)
    if (!m || (m === model && provider === activeProvider)) return
    setSavingModel(true)
    setModel(m)
    setActiveProvider(provider)
    setActiveModel(provider, m)
      .then(refreshModels)
      .finally(() => setSavingModel(false))
  }

  /** Open a workspace from the dropdown: its most recent conversation, or a
   *  fresh chat when it has none (Q2: the dropdown is a conversation switcher). */
  const pickWorkspace = (value: string) => {
    if (value === '__add__') {
      void browseWorkspace()
      return
    }
    const ws = workspaces.find((w) => (w.path ?? '') === value)
    if (!ws) return
    setWorkspace(ws.path ?? '')
    if (conversationId !== null) {
      // Is the open conversation filed in the newly selected workspace?
      listConversations()
        .then((convs) => {
          const open = convs.find((c) => c.id === conversationId)
          if (open && (open.workspace ?? null) !== ws.path) {
            // Different workspace: switch to its most recent conversation.
            const latest = convs.find((c) => (c.workspace ?? null) === ws.path)
            if (latest) {
              setConversationId(latest.id)
              getMessages(latest.id)
                .then((rows) => loadHistory(latest.id, rows))
                .catch(() => {})
            } else {
              newConversation()
            }
          }
        })
        .catch(() => {})
    } else {
      // Fresh draft: jump to the workspace's most recent conversation if any.
      listConversations()
        .then((convs) => {
          const latest = convs.find((c) => (c.workspace ?? null) === ws.path)
          if (latest) {
            setConversationId(latest.id)
            getMessages(latest.id)
              .then((rows) => loadHistory(latest.id, rows))
              .catch(() => {})
          }
        })
        .catch(() => {})
    }
  }

  const browseWorkspace = async () => {
    // Native folder picker when running inside Tauri (Q28)
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      const picked = await invoke<string | null>('pick_workspace')
      if (picked) {
        const ws = await addWorkspace(picked).catch(() => null)
        setWorkspaces((list) =>
          ws && !list.some((w) => w.id === ws.id) ? [...list, ws] : list,
        )
        setWorkspace(ws?.path ?? picked)
        newConversation()
      }
    } catch {
      setNotice({
        title: 'Add workspace unavailable',
        message: 'Folder picking needs the desktop app. Run YAAH via its installer to add workspaces.',
      })
    }
  }

  return (
    <>
      <aside className="flex w-64 min-w-[220px] flex-col border-r border-zinc-800 bg-zinc-900 p-3 text-sm">
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
        <select
          className="mb-3 w-full truncate rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200 focus:border-blue-500 focus:outline-none"
          value={workspace || ''}
          onChange={(e) => pickWorkspace(e.target.value)}
          aria-label="Workspace"
          title={
            workspaces.find((w) => (w.path ?? '') === (workspace || ''))?.path ??
            'No root directory — conversations without a workspace'
          }
        >
          <option value="">Default (Home)</option>
          {workspaces
            .filter((w) => w.path !== null)
            .map((w) => (
              <option key={w.id} value={w.path ?? ''}>
                {w.label}
                {w.exists ? '' : '  (missing)'}
              </option>
            ))}
          <option value="__add__">+ Add workspace…</option>
        </select>
        <div className="mb-3">
          <label className="mb-1 block text-xs text-zinc-500">
            Model{savingModel ? ' (saving...)' : ''}
          </label>
          <select
            className="w-full truncate rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200 focus:border-blue-500 focus:outline-none"
            value={`${activeProvider}::${model}`}
            onChange={(e) => pickModel(e.target.value)}
            title={model}
          >
            {Object.entries(byProvider).map(([name, pm]) => (
              <optgroup
                key={name}
                label={pm.error ? `${name} (${pm.error})` : name}
              >
                {pm.models.map((m) => (
                  <option key={`${name}::${m}`} value={`${name}::${m}`}>
                    {m}
                  </option>
                ))}
              </optgroup>
            ))}
            {/* active model isn't in any group (e.g. its provider is down) */}
            {!Object.values(byProvider).some((pm) => pm.models.includes(model)) && (
              <option value={`${activeProvider}::${model}`}>{model}</option>
            )}
            {Object.keys(byProvider).length === 0 && (
              <option value={`${activeProvider}::${model}`}>
                No provider configured — open Settings
              </option>
            )}
          </select>
          {Object.keys(byProvider).length === 0 && (
            <button
              className="mt-1 w-full rounded border border-amber-700/60 bg-amber-950/30 px-2 py-1 text-left text-[10px] leading-relaxed text-amber-300 hover:border-amber-500"
              onClick={() => setShowSettings(true)}
            >
              No model provider configured — add one in Settings to start.
            </button>
          )}
          {/* per-provider failure notes (Q10) */}
          {Object.entries(byProvider)
            .filter(([, pm]) => pm.error)
            .map(([name, pm]) => (
              <p key={name} className="mt-1 text-[10px] text-amber-400">
                {name}: {pm.error}
              </p>
            ))}
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
            getConfig().then((c) => { setModel(c.model); setActiveProvider(c.active_provider) }).catch(() => {})
            refreshModels()
          }}
        />
      )}
      {notice && (
        <NoticeDialog
          title={notice.title}
          message={notice.message}
          onClose={() => setNotice(null)}
        />
      )}
    </>
  )
}

// ---------------------------------------------------------------- settings (Q4/Q10/Q31/Q35/Q41)

function SettingsModal({ onClose }: { onClose: () => void }) {
  // Local working copy of the providers map: blank key field = keep saved key
  const [providers, setProviders] = useState<Record<string, { api_base: string; model: string; apiKeyInput: string; savedKey: boolean }>>({})
  const [active, setActive] = useState('')
  const [newName, setNewName] = useState('')
  const [temperature, setTemperature] = useState<number | ''>('')
  const [maxTokens, setMaxTokens] = useState<number | ''>('')
  const [maxSteps, setMaxSteps] = useState<number | ''>('')
  const [presets, setPresets] = useState<Record<string, ProviderPreset>>({})
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // The one provider whose fields are open; collapsed rows show a summary.
  const [expanded, setExpanded] = useState<string | null>(null)
  // Provider awaiting removal confirmation (its saved key dies with it).
  const [removeTarget, setRemoveTarget] = useState<string | null>(null)
  // Voice dictation: engine choice + cloud (BYOK) credentials.
  const [voiceEngine, setVoiceEngine] = useState<'local' | 'cloud'>('local')
  const [voiceLocalReady, setVoiceLocalReady] = useState(false)
  const [voiceLocalModel, setVoiceLocalModel] = useState<string | null>(null)
  const [cloudEndpoint, setCloudEndpoint] = useState('')
  const [cloudKey, setCloudKey] = useState('')
  const [cloudKeySaved, setCloudKeySaved] = useState(false)
  const [cloudModel, setCloudModel] = useState('')

  // Esc closes, matching PreviewModal and the dialog shells — but the
  // removal confirm consumes Esc first, so it never dismisses two layers.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (removeTarget) setRemoveTarget(null)
        else onClose()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, removeTarget])

  useEffect(() => {
    getConfig()
      .then((c) => {
        const next: typeof providers = {}
        for (const [name, p] of Object.entries(c.providers)) {
          next[name] = {
            api_base: p.api_base,
            model: p.model,
            apiKeyInput: '',
            savedKey: p.api_key === 'set',
          }
        }
        setProviders(next)
        setActive(c.active_provider)
        setTemperature(c.temperature ?? '')
        setMaxTokens(c.max_tokens ? c.max_tokens : '')
        setMaxSteps(c.max_steps ?? '')
        const v = c.voice
        setVoiceEngine(v?.engine === 'cloud' ? 'cloud' : 'local')
        setCloudEndpoint(v?.cloud_endpoint ?? '')
        setCloudKeySaved(v?.cloud_api_key === 'set')
        setCloudModel(v?.cloud_model || '')
      })
      .catch((e) => setErr(String(e)))
    getProviders().then(setPresets).catch(() => {})
    transcribeStatus()
      .then((s) => {
        setVoiceLocalReady(s.local_available)
        setVoiceLocalModel(s.local_model)
      })
      .catch(() => {})
  }, [])

  const patchProvider = (name: string, patch: Partial<{ api_base: string; model: string; apiKeyInput: string }>) =>
    setProviders((ps) => ({ ...ps, [name]: { ...ps[name], ...patch } }))

  const applyPreset = (target: string, presetName: string) => {
    const p = presets[presetName]
    if (!p) return
    patchProvider(target, { api_base: p.api_base })
  }

  const addProvider = (name: string, fromPreset?: string) => {
    const key = name.trim()
    if (!key || providers[key]) return
    const p = fromPreset ? presets[fromPreset] : undefined
    setProviders((ps) => ({
      ...ps,
      [key]: {
        api_base: p?.api_base ?? '',
        model: p?.model ?? '',
        apiKeyInput: '',
        savedKey: false,
      },
    }))
    setNewName('')
  }

  const removeProvider = (name: string) => {
    setProviders((ps) => {
      const next = { ...ps }
      delete next[name]
      return next
    })
    if (active === name) {
      const rest = Object.keys(providers).filter((n) => n !== name)
      setActive(rest[0] ?? '')
    }
    if (expanded === name) setExpanded(null)
  }

  // Active provider first, then the rest alphabetically — the row the user
  // needs to verify is always the first thing they see.
  const providerOrder = (Object.keys(providers) as string[]).sort((a, b) => {
    if (a === active) return -1
    if (b === active) return 1
    return a.localeCompare(b)
  })

  const save = async () => {
    setSaving(true)
    setErr(null)
    try {
      // Send only providers that still exist; blank key fields keep saved keys
      const out: Record<string, { api_base: string; model: string; api_key?: string }> = {}
      for (const [name, p] of Object.entries(providers)) {
        out[name] = {
          api_base: p.api_base,
          model: p.model,
          ...(p.apiKeyInput ? { api_key: p.apiKeyInput } : {}),
        }
      }
      await updateConfig({
        providers: out,
        active_provider: active || undefined,
        temperature: temperature === '' ? undefined : Number(temperature),
        max_tokens: maxTokens === '' ? 0 : Number(maxTokens),
        max_steps: maxSteps === '' ? undefined : Number(maxSteps),
        voice: {
          engine: voiceEngine,
          cloud_endpoint: cloudEndpoint,
          // Typed key replaces; empty/kept field is dropped server-side so
          // the saved key survives.
          ...(cloudKey ? { cloud_api_key: cloudKey } : {}),
          cloud_model: cloudModel,
        },
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

        {/* providers: collapsed rows, active first; fields behind one open row */}
        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Providers
        </h3>
        {providerOrder.length === 0 && (
          <p className="mb-2 text-[11px] text-zinc-600">
            No providers yet — add one below to start using the agent.
          </p>
        )}
        {providerOrder.map((name) => {
          const p = providers[name]
          const isOpen = expanded === name
          const summary = p.savedKey
            ? 'key saved'
            : p.apiKeyInput
              ? 'key entered'
              : 'no key'
          return (
            <div key={name} className="mb-1.5 rounded border border-zinc-700">
              <button
                className="flex w-full items-center gap-2 px-2 py-1.5 text-left"
                onClick={() => setExpanded(isOpen ? null : name)}
                aria-expanded={isOpen}
              >
                <input
                  type="radio"
                  name="active-provider"
                  checked={active === name}
                  onChange={() => setActive(name)}
                  onClick={(e) => e.stopPropagation()}
                  title="Make active"
                  aria-label={`Make ${name} the active provider`}
                />
                <span className="font-mono text-xs text-zinc-200">{name}</span>
                {!isOpen && (
                  <span className="truncate text-[10px] text-zinc-600">{summary}</span>
                )}
                <span className="ml-auto text-[10px] text-zinc-600">
                  {isOpen ? '▾' : '▸'}
                </span>
              </button>
              {isOpen && (
                <div className="border-t border-zinc-800 p-2">
                  <input
                    className="mb-1 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
                    value={p.api_base}
                    onChange={(e) => patchProvider(name, { api_base: e.target.value })}
                    placeholder="https://api.openai.com/v1"
                    aria-label={`${name} API base URL`}
                  />
                  <div className="mb-1 flex gap-1">
                    <select
                      className="w-full rounded border border-zinc-700 bg-zinc-800 px-1 py-1 text-[10px] text-zinc-300"
                      value=""
                      onChange={(e) => applyPreset(name, e.target.value)}
                      aria-label={`Apply a preset to ${name}`}
                    >
                      <option value="">use preset…</option>
                      {Object.keys(presets).map((preset) => (
                        <option key={preset} value={preset}>
                          {preset}
                        </option>
                      ))}
                    </select>
                  </div>
                  <input
                    type="password"
                    className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
                    placeholder={p.savedKey ? 'key saved' : 'sk-... (optional for local)'}
                    value={p.apiKeyInput}
                    onChange={(e) => patchProvider(name, { apiKeyInput: e.target.value })}
                    aria-label={`${name} API key`}
                  />
                  <button
                    className="mt-1.5 text-[10px] text-red-400 hover:text-red-300"
                    onClick={() => setRemoveTarget(name)}
                  >
                    remove provider
                  </button>
                </div>
              )}
            </div>
          )
        })}

        {/* add a provider: preset templates or a custom OpenAI-compatible URL */}
        <div className="mb-1 flex gap-1">
          <input
            className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            placeholder="new provider name (or 'custom')"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            aria-label="New provider name"
          />
          <button
            className="rounded border border-zinc-700 px-2 text-[11px] text-zinc-300 hover:bg-zinc-800"
            onClick={() => addProvider(newName)}
          >
            + custom
          </button>
        </div>
        <div className="mb-4 flex flex-wrap gap-1">
          {Object.keys(presets).map((preset) => (
            <button
              key={preset}
              className="rounded border border-zinc-700 px-2 py-1 text-[11px] text-zinc-300 hover:bg-zinc-800"
              onClick={() => addProvider(providers[preset] ? `${preset}-2` : preset, preset)}
            >
              + {preset}
            </button>
          ))}
        </div>

        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Generation
        </h3>
        <div className="mb-3 flex gap-2">
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
              min="0"
              className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
              value={maxTokens}
              onChange={(e) => setMaxTokens(e.target.value === '' ? '' : Number(e.target.value))}
            />
            <p className="mt-1 text-[10px] text-zinc-600">0 or blank = no limit (provider default)</p>
          </div>
        </div>

        <div className="mb-3">
          <label className="mb-1 block text-xs text-zinc-500">Max steps</label>
          <input
            type="number"
            min="0"
            className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            value={maxSteps}
            onChange={(e) => setMaxSteps(e.target.value === '' ? '' : Number(e.target.value))}
          />
          <p className="mt-1 text-[10px] text-zinc-600">
            Tool-call rounds per turn before the agent gives up; 0 = unlimited (Stop still works)
          </p>
        </div>

        <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Voice dictation
        </h3>
        <div className="mb-2 flex gap-2" role="radiogroup" aria-label="Transcription engine">
          {(['local', 'cloud'] as const).map((engine) => (
            <button
              key={engine}
              role="radio"
              aria-checked={voiceEngine === engine}
              className={`flex-1 rounded border px-2 py-1.5 font-mono text-xs ${
                voiceEngine === engine
                  ? 'border-blue-600 bg-blue-950/40 text-blue-200'
                  : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'
              }`}
              onClick={() => setVoiceEngine(engine)}
            >
              {engine === 'local' ? 'local whisper' : 'cloud (BYOK)'}
            </button>
          ))}
        </div>
        {voiceEngine === 'local' ? (
          <p className="mb-3 text-[10px] text-zinc-600">
            {voiceLocalReady ? (
              <>
                On-device engine ready — model{' '}
                <span className="font-mono text-zinc-500">{voiceLocalModel}</span>. Audio never
                leaves this machine.
              </>
            ) : (
              <>
                No local whisper engine found (packaged installs bundle one; this looks like a dev
                run). Use cloud, or place a whisper.cpp CLI + ggml model under backend/.
              </>
            )}
          </p>
        ) : (
          <div className="mb-3 space-y-1.5">
            <input
              className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
              value={cloudEndpoint}
              onChange={(e) => setCloudEndpoint(e.target.value)}
              placeholder="https://api.openai.com/v1  or  http://192.168.1.10:8080/inference"
              aria-label="Cloud transcription endpoint"
            />
            <div className="flex gap-1.5">
              <input
                type="password"
                className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
                value={cloudKey}
                onChange={(e) => setCloudKey(e.target.value)}
                placeholder={cloudKeySaved ? 'key saved (optional)' : 'API key (optional)'}
                aria-label="Cloud transcription API key"
              />
              <input
                className="w-28 shrink-0 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
                value={cloudModel}
                onChange={(e) => setCloudModel(e.target.value)}
                placeholder="model (optional)"
                aria-label="Cloud transcription model"
              />
            </div>
            <p className="text-[10px] text-zinc-600">
              OpenAI-compatible /audio/transcriptions endpoint (base URL is fine) or a whisper.cpp
              server /inference URL. API key and model are optional — local model servers usually
              need neither. Recordings are sent to that server.
            </p>
          </div>
        )}

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
      {removeTarget && (
        <ConfirmDialog
          title="Remove provider?"
          body={`"${removeTarget}" will be removed when you Save, and its saved API key will be deleted from config.json. You would need to re-enter the key to use it again.`}
          confirmLabel="Remove"
          onCancel={() => setRemoveTarget(null)}
          onConfirm={() => {
            removeProvider(removeTarget)
            setRemoveTarget(null)
          }}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------- chat

/** Message images are data URLs while live (just sent) and backend rel
 *  paths once loaded from history — render either. */
const imageSrc = (img: string) => (img.startsWith('data:') ? img : imageUrl(img))

export function ChatPanel() {
  const conversationId = useAgent((s) => s.conversationId)
  const messages = useAgent(
    (s) => s.messagesByConv[s.conversationId === null ? 'draft' : String(s.conversationId)] ?? [],
  )
  const { status, error } = useAgent()
  const pendingQuestion = useAgent((s) => {
    if (s.pendingQuestion === null) return null
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    // A question belongs to the turn that asked it: only render when its
    // conversation is on screen (a hidden turn's ask must not leak here).
    return s.pendingQuestion.convKey === key ? s.pendingQuestion : null
  })
  const bottomRef = useRef<HTMLDivElement>(null)
  const streaming = status === 'thinking' || status === 'running-tool'
  // Only the in-flight assistant message shows the ephemeral ticker; every
  // finished turn collapses to the one-line trace.
  const liveId = streaming && messages.length > 0 ? messages[messages.length - 1].id : null

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <main className="flex min-w-0 flex-1 flex-col">
      <div className="min-w-0 flex-1 space-y-4 overflow-y-auto p-4">
        {messages.length === 0 && (
          <div className="mt-12 text-center">
            <p className="text-sm text-zinc-300">
              Point me at a project and describe a task.
            </p>
            <ol className="mt-4 inline-block space-y-1.5 text-left text-xs text-zinc-500">
              <li>
                <span className="mr-1.5 text-zinc-700">1.</span>
                Pick a workspace in the sidebar — choose one or add a folder.
              </li>
              <li>
                <span className="mr-1.5 text-zinc-700">2.</span>
                Add a model provider in Settings (your own API key), if you haven't.
              </li>
              <li>
                <span className="mr-1.5 text-zinc-700">3.</span>
                Describe your task below — type{' '}
                <span className="font-mono text-indigo-300">/s</span> to load a skill's instructions.
              </li>
            </ol>
            <p className="mt-5 text-[11px] text-zinc-600">
              Tool calls stream live as chips, then collapse to an expandable trace.
            </p>
          </div>
        )}
        {messages.map((m) => (
          <MessageView key={m.id} msg={m} live={m.id === liveId} />
        ))}
        <div ref={bottomRef} />
      </div>
      {error && (
        <div className="border-t border-red-900 bg-red-950/60 px-4 py-2 text-xs text-red-300">
          {error}
        </div>
      )}
      {pendingQuestion && (
        <div className="border-t border-orange-800/60 px-4 pb-3 pt-3">
          <AskUserCard pending={pendingQuestion} />
        </div>
      )}
      <Composer />
      <div className="flex items-center gap-2 px-4 pb-1 pt-0.5 font-mono text-[10px] text-zinc-600">
        <span
          className={`inline-block h-1.5 w-1.5 rounded-full ${
            streaming ? 'run-pulse bg-amber-400' : status === 'error' ? 'bg-red-500' : 'bg-emerald-600'
          }`}
        />
        {streaming ? 'working' : status}
      </div>
    </main>
  )
}

/** A staged text file. Small files ride inline (`content`); larger ones are
 *  copied into <workspace>/.yaah-attachments and referenced by `savedPath`,
 *  which the agent's workspace-sandboxed read_file tool can open on demand. */
interface Attachment {
  name: string
  content?: string
  savedPath?: string
  size: number
}

/** Image staged for sending; dataUrl doubles as the thumbnail src. */
interface ImageAttachment {
  name: string
  dataUrl: string
}

const MAX_IMAGE_BYTES = 5_000_000
// Files at or under this ride inline in the message as a fenced block;
// anything bigger is staged in the workspace and referenced by path.
const INLINE_LIMIT_BYTES = 100_000
const MAX_TEXT_BYTES = 2_000_000

/** The text an attachment contributes to the outgoing message: small files
 *  inline so the model sees them with no tool call; large ones as a path
 *  pointer it can read_file (possibly in chunks) across turns. */
const attachmentText = (a: Attachment): string => {
  if (a.content !== undefined) {
    return `\n\n--- attached file: ${a.name} ---\n\`\`\`\n${a.content}\n\`\`\``
  }
  const kb = Math.max(1, Math.round(a.size / 1_000))
  return `\n\n--- attached file: ${a.name} (${kb} KB) ---\nSaved to ${a.savedPath} in the workspace. Read it with read_file (use offset/limit for large files).`
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
    adoptDraft,
    setPendingQuestion,
    pushLog,
    setAbortController,
    removeMessage,
  } = useAgent()
  const abortController = useAgent((s) => s.abortController)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [images, setImages] = useState<ImageAttachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const streaming = status === 'thinking' || status === 'running-tool'

  // Collapse the auto-grown textarea back to its resting height whenever the
  // draft empties (send, skill pick, restore-on-error keeps content so no reset).
  useEffect(() => {
    if (input === '' && textareaRef.current) {
      textareaRef.current.style.height = ''
    }
  }, [input])

  // ---- skills (/s autocomplete + chips) ----
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [skillMenuOpen, setSkillMenuOpen] = useState(false)
  const [skillQuery, setSkillQuery] = useState('')
  const [skillIndex, setSkillIndex] = useState(0)
  // True only after the user points at a row (arrows or hover). Enter commits
  // a skill solely on this explicit selection; otherwise Enter sends the
  // literal text — typing a message that starts with "/s" stays possible.
  const [skillNavigated, setSkillNavigated] = useState(false)
  const [pickedSkills, setPickedSkills] = useState<SkillInfo[]>([])
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // ---- voice dictation (click-to-toggle; text lands in the input) ----
  const [voiceState, setVoiceState] = useState<'idle' | 'recording' | 'transcribing'>('idle')
  const [micAvailable, setMicAvailable] = useState<boolean | null>(null) // null = checking
  const recorderRef = useRef<VoiceRecorder | null>(null)

  useEffect(() => {
    // Retry: on a fresh launch this request races backend startup (the
    // supervisor may still be cycling), and giving up on the first failure
    // hid the mic button for the whole session. Re-check whenever the
    // backend reports it's back up.
    let cancelled = false
    const check = (attempt = 0) => {
      transcribeStatus()
        .then((s) => {
          if (!cancelled) setMicAvailable(s.engine === 'cloud' ? s.cloud_configured : s.local_available)
        })
        .catch(() => {
          if (cancelled || attempt >= 8) return
          window.setTimeout(() => check(attempt + 1), 1500 * (attempt + 1))
        })
    }
    check()
    const onBackendStatus = () => check()
    window.addEventListener('backend-status', onBackendStatus)
    return () => {
      cancelled = true
      window.removeEventListener('backend-status', onBackendStatus)
    }
  }, [])

  const toggleDictation = async () => {
    if (voiceState === 'transcribing') return
    if (voiceState === 'recording') {
      setVoiceState('transcribing')
      try {
        const blob = await recorderRef.current!.stop()
        const text = await transcribeAudio(blob)
        if (text) {
          setInput((cur) => (cur ? `${cur.trimEnd()} ${text}` : text))
        } else {
          pushReject('Heard nothing — try speaking closer to the mic')
        }
      } catch (e) {
        pushReject(`Dictation failed: ${(e as Error).message}`)
      } finally {
        recorderRef.current = null
        setVoiceState('idle')
        textareaRef.current?.focus()
      }
      return
    }
    // idle → recording
    const rec = new VoiceRecorder()
    try {
      await rec.start()
    } catch {
      pushReject('Microphone unavailable — check permission for this app')
      return
    }
    recorderRef.current = rec
    setVoiceState('recording')
  }

  // Auto-stop on silence (VAD): once you've spoken and stayed quiet for
  // ~1.6s, finish the recording and transcribe. Manual click still wins.
  useEffect(() => {
    if (voiceState !== 'recording') return
    const id = window.setInterval(() => {
      const rec = recorderRef.current
      if (!rec) return
      const m = rec.metrics()
      if (m.speechStarted && m.silenceMs >= 1600) void toggleDictation()
    }, 200)
    return () => window.clearInterval(id)
  }, [voiceState])

  // ---- attachment rejection feedback (harden: silent drops are a trust bug) ----
  const [rejects, setRejects] = useState<string[]>([])
  const [sendError, setSendError] = useState<string | null>(null)
  // A turn that died mid-stream: the banner offers Resume (continue the same
  // turn server-side, no duplicate user message) and dismiss.
  const [turnError, setTurnError] = useState<string | null>(null)
  const pushReject = useCallback((msg: string) => {
    setRejects((r) => [...r.slice(-3), msg])
    // Auto-clear after 6s; each new rejection resets the timer.
    window.setTimeout(() => {
      setRejects((r) => (r.includes(msg) ? r.filter((x) => x !== msg) : r))
    }, 6000)
  }, [])

  const loadSkills = useCallback(() => {
    listSkills()
      .then((r) => setSkills(r.skills))
      .catch(() => setSkills([]))
  }, [])

  useEffect(loadSkills, [loadSkills])

  // The menu opens when the input is exactly "/s" or starts with "/s " —
  // the query is whatever follows, and the list narrows as it grows.
  useEffect(() => {
    if (input === '/s') {
      setSkillMenuOpen(true)
      setSkillQuery('')
      setSkillIndex(0)
      setSkillNavigated(false)
      return
    }
    if (input.startsWith('/s ')) {
      setSkillMenuOpen(true)
      setSkillQuery(input.slice(3))
      setSkillIndex(0)
      setSkillNavigated(false)
      return
    }
    setSkillMenuOpen(false)
  }, [input])

  const filteredSkills = skills.filter((s) =>
    skillQuery ? s.name.toLowerCase().startsWith(skillQuery.toLowerCase()) : true,
  )

  const pickSkill = (s: SkillInfo) => {
    setPickedSkills((p) => (p.some((x) => x.name === s.name) ? p : [...p, s]))
    setSkillMenuOpen(false)
    setSkillNavigated(false)
    setInput('')
    textareaRef.current?.focus()
  }

  const removeSkill = (name: string) =>
    setPickedSkills((p) => p.filter((s) => s.name !== name))

  const addImageFile = (f: File) => {
    if (!f.type.startsWith('image/')) {
      pushReject(`"${f.name}" skipped — not an image file`)
      return
    }
    if (f.size > MAX_IMAGE_BYTES) {
      pushReject(
        `"${f.name}" skipped — ${(f.size / 1_000_000).toFixed(1)} MB exceeds the 5 MB limit`,
      )
      return
    }
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result)
      if (dataUrl.startsWith('data:image/')) {
        setImages((imgs) => {
          if (imgs.length >= 4) {
            pushReject(`"${f.name}" skipped — 4 images max per message`)
            return imgs
          }
          return [...imgs, { name: f.name, dataUrl }]
        })
      } else {
        pushReject(`"${f.name}" skipped — unreadable image data`)
      }
    }
    reader.onerror = () => pushReject(`"${f.name}" skipped — could not be read`)
    reader.readAsDataURL(f)
  }

  /** Unified entry for every file that enters the composer — picker, drag,
   *  or paste. Images go to the vision path; everything else is read as
   *  text: small files stage inline, large ones are copied into the
   *  workspace so the agent can read_file them. Binary content is rejected
   *  with an explanation, never silently dropped. */
  const addFiles = (files: FileList | File[]) => {
    void (async () => {
      const added: Attachment[] = []
      for (const f of Array.from(files)) {
        if (f.type.startsWith('image/')) {
          addImageFile(f)
          continue
        }
        if (f.size > MAX_TEXT_BYTES) {
          pushReject(
            `"${f.name}" skipped — ${(f.size / 1_000_000).toFixed(1)} MB exceeds the 2 MB text limit`,
          )
          continue
        }
        let content: string
        try {
          content = await f.text()
        } catch {
          pushReject(`"${f.name}" skipped — could not be read`)
          continue
        }
        if (content.includes('\u0000')) {
          pushReject(
            `"${f.name}" skipped — binary file (only images and text/code files are supported)`,
          )
          continue
        }
        if (f.size <= INLINE_LIMIT_BYTES) {
          added.push({ name: f.name, content, size: f.size })
          continue
        }
        // Large file: stage a copy in the workspace for read_file. The Default
        // workspace resolves to the home directory, so staging always has a
        // sandboxed target.
        try {
          const { path } = await uploadAttachment(workspace, f.name, content)
          added.push({ name: f.name, savedPath: path, size: f.size })
        } catch (e) {
          pushReject(`"${f.name}" skipped — could not stage file: ${(e as Error).message}`)
        }
      }
      if (added.length) setAttachments((a) => [...a, ...added])
    })()
  }

  /** One shared stream-event handler for both a fresh send and a resume:
   *  everything keys off the in-flight assistant message id and the buffer
   *  captured at send time — a stream never writes to "what's on screen". */
  const handleStreamEvent = (bufKey: string, asstId: string) => (ev: AgentEvent) => {
    if (ev.type === 'text') {
      setStatus('thinking')
      if (ev.text) appendTextDelta(bufKey, asstId, ev.text)
    } else if (ev.type === 'tool_start') {
      setStatus('running-tool')
      startToolCall(bufKey, asstId, ev.call_id ?? '', ev.name ?? 'tool', ev.args)
      pushLog({ kind: 'tool', name: ev.name, args: ev.args })
      if (ev.name === 'ask_user') {
        const a = (ev.args ?? {}) as {
          question?: string
          options?: Array<{ label: string; description?: string }>
        }
        setPendingQuestion({
          callId: ev.call_id ?? '',
          question: a.question ?? '',
          options: a.options ?? [],
          convKey: bufKey,
        })
      }
    } else if (ev.type === 'tool_result') {
      finishToolCall(bufKey, asstId, ev.call_id ?? '', ev.result)
      pushLog({ kind: 'tool', name: ev.name, result: ev.result })
      if (ev.name === 'ask_user') {
        setPendingQuestion((q) => (q && q.callId === ev.call_id ? null : q))
      }
    } else if (ev.type === 'error') {
      setStatus('error')
      setError(ev.message ?? 'Unknown agent error')
      setTurnError(ev.message ?? 'Unknown agent error')
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
    } else if (ev.type === 'stopped') {
      setStatus('idle')
      appendTextDelta(bufKey, asstId, '\n[stopped]')
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
    } else if (ev.type === 'done') {
      setStatus('idle')
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
    }
  }

  const send = async () => {
    const text = input.trim()
    if ((!text && attachments.length === 0 && images.length === 0) || sending) return
    setSending(true)
    setSendError(null)

    // Inline small attachments as fenced blocks; large staged files as
    // workspace path pointers the agent can read_file.
    let fullText = text
    for (const a of attachments) {
      fullText += attachmentText(a)
    }
    if (images.length) {
      fullText += `\n\n[${images.length} image${images.length === 1 ? '' : 's'} attached]`
    }
    const imageDataUrls = images.map((i) => i.dataUrl)
    const invokedSkills = pickedSkills.map((s) => s.name)
    // Captured draft: if the turn fails before the agent answers, the
    // composer gets it back — a failed send must not cost the prompt.
    const draft = { input, attachments, images, pickedSkills }

    setInput('')
    setAttachments([])
    setImages([])
    setPickedSkills([])
    setError(null)
    // Capture the turn's target buffer now: everything this turn writes —
    // optimistic messages, stream deltas, tool traces — goes there, even if
    // the user switches to another conversation mid-stream (Q11: free).
    // `let` because adopting a newly created conversation re-keys the
    // buffer: events before adoption target 'draft', after it the real id.
    let bufKey = conversationId === null ? 'draft' : String(conversationId)
    const userId = appendUserMessage(bufKey, fullText, imageDataUrls)
    const asstId = appendAssistantPlaceholder(bufKey)
    const ac = new AbortController()
    setAbortController(ac)
    try {
      let cid: number
      if (conversationId === null) {
        const created = await createConversation(fullText.slice(0, 40) || 'New chat', workspace)
        cid = created.id
        // Atomic: re-key the draft buffer (optimistic messages included)
        // to the new id and move the panel onto it. bufKey follows so the
        // stream keeps writing where the panel is now looking.
        adoptDraft(cid)
        bufKey = String(cid)
      } else {
        cid = conversationId
      }
      setStatus('thinking')
      await streamAgentTurn(
        cid,
        fullText,
        workspace,
        handleStreamEvent(bufKey, asstId),
        ac.signal,
        imageDataUrls,
        invokedSkills,
      )
      if (useAgent.getState().status !== 'error') setStatus('idle')
    } catch (e) {
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      if ((e as Error).name === 'AbortError') {
        setStatus('idle')
        appendTextDelta(bufKey, asstId, '\n[stopped]')
      } else {
        // The turn never started (network, bad key, server down): roll back
        // the optimistic messages and restore the draft so nothing is lost.
        removeMessage(bufKey, asstId)
        removeMessage(bufKey, userId)
        setInput(draft.input)
        setAttachments(draft.attachments)
        setImages(draft.images)
        setPickedSkills(draft.pickedSkills)
        setStatus('error')
        setSendError(
          `Message not sent — the agent could not be reached. Your draft was restored.`,
        )
        textareaRef.current?.focus()
      }
    } finally {
      setSending(false)
      setAbortController(null)
    }
  }

  /** Continue a turn that died mid-stream: same conversation, same prompt,
   *  no duplicate user message (backend resume flag). */
  const resumeTurn = async () => {
    if (conversationId === null || sending) return
    setSending(true)
    setSendError(null)
    setTurnError(null)
    setError(null)
    const bufKey = String(conversationId)
    const asstId = appendAssistantPlaceholder(bufKey)
    const ac = new AbortController()
    setAbortController(ac)
    try {
      setStatus('thinking')
      await streamAgentTurn(
        conversationId,
        'resume',
        workspace,
        handleStreamEvent(bufKey, asstId),
        ac.signal,
        [],
        [],
        true,
      )
      if (useAgent.getState().status !== 'error') setStatus('idle')
    } catch (e) {
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      if ((e as Error).name === 'AbortError') {
        setStatus('idle')
        appendTextDelta(bufKey, asstId, '\n[stopped]')
      } else {
        setStatus('error')
        setTurnError(String((e as Error).message ?? e))
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
        if (e.dataTransfer?.files?.length) addFiles(e.dataTransfer.files)
      }}
    >
      {images.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {images.map((img, i) => (
            <span key={i} className="relative">
              <img
                src={img.dataUrl}
                alt={img.name}
                title={img.name}
                className="h-16 rounded border border-zinc-700"
              />
              <button
                className="absolute -right-1.5 -top-1.5 h-4 w-4 rounded-full bg-zinc-700 text-[10px] leading-4 text-zinc-300 hover:bg-red-600 hover:text-white"
                onClick={() => setImages((arr) => arr.filter((_, j) => j !== i))}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {attachments.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1">
          {attachments.map((a, i) => (
            <span
              key={i}
              title={a.savedPath ?? a.name}
              className="flex items-center gap-1 rounded bg-zinc-800 px-2 py-0.5 font-mono text-[10px] text-zinc-300"
            >
              {a.name}
              {a.savedPath && (
                <span className="text-zinc-500">
                  · {Math.max(1, Math.round(a.size / 1_000))} KB · staged
                </span>
              )}
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
      {pickedSkills.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1">
          {pickedSkills.map((s) => (
            <span
              key={s.name}
              title={s.description || s.path}
              className="flex items-center gap-1 rounded bg-indigo-900/60 px-2 py-0.5 font-mono text-[10px] text-indigo-200"
            >
              /s {s.name}
              <button
                className="text-indigo-400 hover:text-red-400"
                onClick={() => removeSkill(s.name)}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}
      {rejects.length > 0 && (
        <div className="mb-2 space-y-1" aria-live="polite">
          {rejects.map((msg, i) => (
            <p
              key={`${msg}-${i}`}
              className="rounded border border-amber-800/60 bg-amber-950/40 px-2 py-1 text-[11px] text-amber-300"
            >
              {msg}
            </p>
          ))}
        </div>
      )}
      {sendError && (
        <div
          className="mb-2 flex items-center justify-between gap-2 rounded border border-red-800/60 bg-red-950/40 px-2 py-1.5"
          role="alert"
        >
          <p className="text-[11px] text-red-300">{sendError}</p>
          <button
            className="shrink-0 text-[10px] text-red-400 hover:text-red-200"
            onClick={() => setSendError(null)}
          >
            dismiss
          </button>
        </div>
      )}
      {turnError && (
        <div
          className="mb-2 flex items-center justify-between gap-2 rounded border border-red-800/60 bg-red-950/40 px-2 py-1.5"
          role="alert"
        >
          <p className="min-w-0 flex-1 truncate text-[11px] text-red-300" title={turnError}>
            The turn failed mid-stream — {turnError}
          </p>
          <button
            className="shrink-0 rounded border border-red-700 px-2 py-0.5 text-[10px] text-red-300 hover:bg-red-950"
            disabled={sending}
            onClick={() => void resumeTurn()}
          >
            Resume turn
          </button>
          <button
            className="shrink-0 text-[10px] text-red-400 hover:text-red-200"
            onClick={() => setTurnError(null)}
          >
            dismiss
          </button>
        </div>
      )}
      {skillMenuOpen && (
        <div className="relative">
          <div className="absolute bottom-1 left-0 z-10 max-h-56 w-80 overflow-y-auto rounded border border-zinc-700 bg-zinc-900 shadow-lg">
            {filteredSkills.length === 0 ? (
              <div className="px-3 py-2 text-xs text-zinc-500">No matching skills</div>
            ) : (
              filteredSkills.map((s, i) => (
                <button
                  key={s.name}
                  className={`block w-full px-3 py-1.5 text-left ${
                    i === skillIndex ? 'bg-zinc-800' : 'hover:bg-zinc-800/60'
                  }`}
                  onMouseEnter={() => {
                    setSkillIndex(i)
                    setSkillNavigated(true)
                  }}
                  onMouseDown={(e) => {
                    e.preventDefault() // keep textarea focus
                    pickSkill(s)
                  }}
                >
                  <div className="font-mono text-xs text-indigo-300">/s {s.name}</div>
                  {s.description && (
                    <div className="truncate text-[10px] text-zinc-500">{s.description}</div>
                  )}
                </button>
              ))
            )}
            <div className="border-t border-zinc-800 px-3 py-1 text-[10px] text-zinc-600">
              ↑↓ navigate · Tab adds · Esc closes — Enter sends your text
            </div>
            <button
              className="block w-full px-3 py-1.5 text-left text-[10px] text-zinc-500 hover:text-zinc-300"
              onMouseDown={(e) => {
                e.preventDefault()
                refreshSkills().then(loadSkills)
              }}
            >
              ↻ Rescan skills folder
            </button>
          </div>
        </div>
      )}
      <div className="flex gap-2">
        <textarea
          ref={textareaRef}
          className={`flex-1 resize-none rounded border bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:outline-none ${
            dragOver
              ? 'border-blue-500'
              : streaming
                ? 'border-amber-600/70 focus:border-amber-500'
                : 'border-zinc-700 focus:border-blue-500'
          }`}
          rows={2}
          style={{ height: 'auto', minHeight: '3.25rem', maxHeight: '16rem' }}
          placeholder="Describe a task... (drop/paste/attach images or text files; type /s to load a skill)"          aria-label="Message the agent"
          value={input}
          onChange={(e) => {
            setInput(e.target.value)
            // Auto-grow with content, capped at ~8 rows; resets on send.
            const el = e.target
            el.style.height = 'auto'
            el.style.height = `${Math.min(el.scrollHeight, 256)}px`
          }}
          onPaste={(e) => {
            const files = e.clipboardData?.files
            if (files?.length) {
              e.preventDefault()
              addFiles(files)
            }
          }}
          onKeyDown={(e) => {
            // IME safety: Enter confirming a CJK composition must never send
            // or pick a skill (isComposing is true for the whole composition).
            if (e.nativeEvent.isComposing || e.keyCode === 229) return
            if (skillMenuOpen && filteredSkills.length > 0) {
              if (e.key === 'ArrowDown') {
                e.preventDefault()
                setSkillIndex((i) => (i + 1) % filteredSkills.length)
                setSkillNavigated(true)
                return
              }
              if (e.key === 'ArrowUp') {
                e.preventDefault()
                setSkillIndex((i) => (i - 1 + filteredSkills.length) % filteredSkills.length)
                setSkillNavigated(true)
                return
              }
              if (e.key === 'Tab') {
                // Tab completes the highlighted skill but never sends.
                e.preventDefault()
                pickSkill(filteredSkills[skillIndex] ?? filteredSkills[0])
                return
              }
              if (e.key === 'Enter') {
                e.preventDefault()
                // Enter commits only an explicitly selected row; with no
                // selection it closes the menu so the literal text sends.
                if (skillNavigated) {
                  pickSkill(filteredSkills[skillIndex] ?? filteredSkills[0])
                } else {
                  setSkillMenuOpen(false)
                  void send()
                }
                return
              }
              if (e.key === 'Escape') {
                e.preventDefault()
                setSkillMenuOpen(false)
                return
              }
            }
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            addFiles(e.target.files ?? [])
            e.target.value = ''
          }}
        />
        <button
          title="Attach files"
          aria-label="Attach files"
          className="self-end rounded border border-zinc-700 px-3 py-2 text-zinc-300 hover:bg-zinc-800"
          onClick={() => fileInputRef.current?.click()}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 14 14"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <path d="M7 2v10M2 7h10" />
          </svg>
        </button>
        {/* Only render once confirmed available: an optimistic show/hide
            flashed the button before the backend could answer. */}
        {micAvailable === true && (
          <button
            title={
              voiceState === 'recording'
                ? 'Stop and transcribe (auto-stops ~1.5s after you stop talking)'
                : voiceState === 'transcribing'
                  ? 'Transcribing…'
                  : 'Dictate (voice to text; stops automatically on silence)'
            }
            aria-label={
              voiceState === 'recording' ? 'Stop and transcribe' : 'Dictate (voice to text)'
            }
            aria-pressed={voiceState === 'recording'}
            className={`self-end rounded border px-3 py-2 ${
              voiceState === 'recording'
                ? 'border-red-600 text-red-400'
                : voiceState === 'transcribing'
                  ? 'border-amber-600/70 text-amber-300'
                  : 'border-zinc-700 text-zinc-300 hover:bg-zinc-800'
            }`}
            onClick={() => void toggleDictation()}
          >
            {voiceState === 'transcribing' ? (
              <span className="run-pulse inline-block text-[10px] leading-[14px]">●</span>
            ) : (
              <svg
                width="14"
                height="14"
                viewBox="0 0 14 14"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
                strokeLinecap="round"
                aria-hidden="true"
              >
                <rect x="5" y="1.25" width="4" height="7" rx="2" />
                <path d="M2.75 6.5a4.25 4.25 0 0 0 8.5 0M7 10.75v2" />
              </svg>
            )}
          </button>
        )}
        {sending ? (
          <button
            className="self-end rounded border border-red-700 px-3 py-2 text-sm text-red-300 hover:bg-red-950"
            onClick={stop}
          >
            Stop
          </button>
        ) : (
          <button
            className="self-end rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-500 disabled:opacity-50"
            onClick={() => void send()}
            disabled={!input.trim() && attachments.length === 0 && images.length === 0}
          >
            Send
          </button>
        )}
      </div>
    </div>
  )
}
