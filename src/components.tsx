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
  cancelAgent,
  getFileTree,
  previewFile,
  deleteFile,
  exportConversationMarkdown,
  deleteConversation,
  updateConversation,
  submitAnswer,
  imageUrl,
  type FileEntry,
  type ProviderPreset,
} from './api'
import { useAgent, type ChatMessage, type PendingQuestion, type ToolCall } from './store'
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
      .then(() => setPendingQuestion(null))
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
        <span className="ml-auto shrink-0 text-zinc-600">{open ? '[-]' : '[+]'}</span>
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
        <div className="text-sm leading-relaxed text-zinc-200">
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
              <button
                title="Export as Markdown (Q39)"
                className="rounded px-1 py-1.5 text-[10px] text-zinc-400 opacity-0 hover:text-zinc-200 group-hover:opacity-100"
                onClick={() => {
                  exportConversationMarkdown(c.id, c.title).catch((e) =>
                    alert(`Export failed: ${e.message ?? e}`),
                  )
                }}
              >
                md↓
              </button>
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
              <button
                title="Delete conversation"
                className="rounded px-1 py-1.5 text-[10px] text-zinc-400 opacity-0 hover:text-red-400 group-hover:opacity-100"
                onClick={() => {
                  if (!confirm(`Delete "${c.title}" and all its messages? This cannot be undone.`)) return
                  deleteConversation(c.id)
                    .then(() => {
                      if (c.id === conversationId) {
                        newConversation()
                      } else {
                        refresh()
                      }
                    })
                    .catch((e) => alert(`Delete failed: ${e.message ?? e}`))
                }}
              >
                ✕
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
  const [activeProvider, setActiveProvider] = useState('')
  // name -> {models, error?} for every configured provider
  const [byProvider, setByProvider] = useState<Record<string, ProviderModels>>({})
  const [savingModel, setSavingModel] = useState(false)
  const [showSettings, setShowSettings] = useState(false)

  // Keep the input in sync with the store (conversation switch, restore below).
  useEffect(() => setWsInput(workspace), [workspace])

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
          title="Remembered across app restarts"
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
              <option value="">No models available — check Settings</option>
            )}
          </select>
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
      })
      .catch((e) => setErr(String(e)))
    getProviders().then(setPresets).catch(() => {})
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
  }

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

        {/* configured providers: add / edit / remove (Q9) */}
        <label className="mb-1 block text-xs text-zinc-500">Providers</label>
        {Object.entries(providers).map(([name, p]) => (
          <div key={name} className="mb-3 rounded border border-zinc-700 p-2">
            <div className="mb-1 flex items-center gap-2">
              <input
                type="radio"
                name="active-provider"
                checked={active === name}
                onChange={() => setActive(name)}
                title="Make active"
              />
              <span className="font-mono text-xs text-zinc-200">{name}</span>
              <button
                className="ml-auto text-[10px] text-red-400 hover:text-red-300"
                onClick={() => removeProvider(name)}
              >
                remove
              </button>
            </div>
            <input
              className="mb-1 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
              value={p.api_base}
              onChange={(e) => patchProvider(name, { api_base: e.target.value })}
              placeholder="https://api.openai.com/v1"
            />
            <div className="mb-1 flex gap-1">
              <select
                className="w-full rounded border border-zinc-700 bg-zinc-800 px-1 py-1 text-[10px] text-zinc-300"
                value=""
                onChange={(e) => applyPreset(name, e.target.value)}
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
              className="mb-1 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
              placeholder={p.savedKey ? 'key saved' : 'sk-... (optional for local)'}
              value={p.apiKeyInput}
              onChange={(e) => patchProvider(name, { apiKeyInput: e.target.value })}
            />
          </div>
        ))}

        {/* add a provider: preset templates or a custom OpenAI-compatible URL */}
        <div className="mb-3 flex gap-1">
          <input
            className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            placeholder="new provider name (or 'custom')"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
          />
          <button
            className="rounded border border-zinc-700 px-2 text-[11px] text-zinc-300 hover:bg-zinc-800"
            onClick={() => addProvider(newName)}
          >
            + custom
          </button>
        </div>
        <div className="mb-3 flex flex-wrap gap-1">
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

/** Message images are data URLs while live (just sent) and backend rel
 *  paths once loaded from history — render either. */
const imageSrc = (img: string) => (img.startsWith('data:') ? img : imageUrl(img))

export function ChatPanel() {
  const { messages, status, error, pendingQuestion } = useAgent()
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
          <p className="mt-10 text-center text-sm text-zinc-600">
            Start a conversation. Work in progress streams as a live ticker, then
            collapses to a one-line trace.
          </p>
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

interface Attachment {
  name: string
  content: string
}

/** Image staged for sending; dataUrl doubles as the thumbnail src. */
interface ImageAttachment {
  name: string
  dataUrl: string
}

const MAX_IMAGE_BYTES = 5_000_000

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
    setPendingQuestion,
    pushLog,
    setAbortController,
  } = useAgent()
  const abortController = useAgent((s) => s.abortController)
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [images, setImages] = useState<ImageAttachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const addImageFile = (f: File) => {
    if (!f.type.startsWith('image/') || f.size > MAX_IMAGE_BYTES) return
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result)
      if (dataUrl.startsWith('data:image/')) {
        setImages((imgs) =>
          imgs.length < 4 ? [...imgs, { name: f.name, dataUrl }] : imgs,
        )
      }
    }
    reader.readAsDataURL(f)
  }

  const readDroppedFiles = (files: FileList) => {
    void (async () => {
      const added: Attachment[] = []
      for (const f of Array.from(files)) {
        if (f.type.startsWith('image/')) {
          addImageFile(f)
          continue
        }
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
    if ((!text && attachments.length === 0 && images.length === 0) || sending) return
    setSending(true)

    // Inline attachments as fenced blocks (Q33)
    let fullText = text
    for (const a of attachments) {
      fullText += `\n\n--- attached file: ${a.name} ---\n\`\`\`\n${a.content}\n\`\`\``
    }
    if (images.length) {
      fullText += `\n\n[${images.length} image${images.length === 1 ? '' : 's'} attached]`
    }
    const imageDataUrls = images.map((i) => i.dataUrl)

    setInput('')
    setAttachments([])
    setImages([])
    setError(null)
    appendUserMessage(fullText, imageDataUrls)
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
            if (ev.name === 'ask_user') {
              const a = (ev.args ?? {}) as {
                question?: string
                options?: Array<{ label: string; description?: string }>
              }
              setPendingQuestion({
                callId: ev.call_id ?? '',
                question: a.question ?? '',
                options: a.options ?? [],
              })
            }
          } else if (ev.type === 'tool_result') {
            finishToolCall(asstId, ev.call_id ?? '', ev.result)
            pushLog({ kind: 'tool', name: ev.name, result: ev.result })
            if (ev.name === 'ask_user') setPendingQuestion(null)
          } else if (ev.type === 'error') {
            setStatus('error')
            setError(ev.message ?? 'Unknown agent error')
            setPendingQuestion(null)
          } else if (ev.type === 'stopped') {
            setStatus('idle')
            appendTextDelta(asstId, '\n[stopped]')
            setPendingQuestion(null)
          } else if (ev.type === 'done') {
            setStatus('idle')
            setPendingQuestion(null)
          }
        },
        ac.signal,
        imageDataUrls,
      )
      if (useAgent.getState().status !== 'error') setStatus('idle')
    } catch (e) {
      setPendingQuestion(null)
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
          placeholder="Describe a task... (Enter to send, Shift+Enter for newline, drop/paste/attach images or text files)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onPaste={(e) => {
            const files = e.clipboardData?.files
            if (files?.length) {
              const imgs = Array.from(files).filter((f) => f.type.startsWith('image/'))
              if (imgs.length) {
                e.preventDefault()
                imgs.forEach(addImageFile)
              }
            }
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              void send()
            }
          }}
        />
        <input
          ref={fileInputRef}
          type="file"
          accept="image/*"
          multiple
          className="hidden"
          onChange={(e) => {
            Array.from(e.target.files ?? []).forEach(addImageFile)
            e.target.value = ''
          }}
        />
        <button
          title="Attach images"
          className="self-end rounded border border-zinc-700 px-3 py-2 text-sm text-zinc-300 hover:bg-zinc-800"
          onClick={() => fileInputRef.current?.click()}
        >
          📎
        </button>
        <button
          className="self-end rounded bg-blue-600 px-3 py-2 text-sm text-white hover:bg-blue-500 disabled:opacity-50"
          onClick={() => void send()}
          disabled={sending || (!input.trim() && attachments.length === 0 && images.length === 0)}
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
