import { useEffect, useRef, useState } from 'react'
import { useAgent, type ChatMessage } from './store'
import {
  listConversations,
  createConversation,
  getMessages,
  getConfig,
  updateConfig,
  streamAgentTurn,
} from './api'

function ToolCallBlock({ tc }: { tc: NonNullable<ChatMessage['toolCalls']>[number] }) {
  const [open, setOpen] = useState(false)
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
          {tc.result !== undefined && (
            <div className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all">
              result: {JSON.stringify(tc.result, null, 2)}
            </div>
          )}
        </div>
      )}
    </div>
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
        {msg.content && (
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        )}
        {msg.toolCalls?.map((tc) => (
          <ToolCallBlock key={tc.id} tc={tc} />
        ))}
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

function ConversationList() {
  const { conversationId, setConversationId, loadHistory, setWorkspace } = useAgent()
  const [convs, setConvs] = useState<Array<{ id: number; title: string; workspace: string | null }>>([])

  useEffect(() => {
    listConversations().then(setConvs).catch(() => setConvs([]))
  }, [conversationId])

  return (
    <div className="flex-1 overflow-y-auto">
      {convs.map((c) => (
        <button
          key={c.id}
          className={`block w-full truncate rounded px-2 py-1.5 text-left text-xs ${
            c.id === conversationId
              ? 'bg-blue-600 text-white'
              : 'text-zinc-300 hover:bg-zinc-800'
          }`}
          onClick={() => {
            setConversationId(c.id)
            if (c.workspace) setWorkspace(c.workspace)
            getMessages(c.id).then(loadHistory).catch(() => {})
          }}
        >
          {c.title}
        </button>
      ))}
    </div>
  )
}

export function Sidebar() {
  const { newConversation, workspace, setWorkspace, clearLog } = useAgent()
  const [wsInput, setWsInput] = useState(workspace)
  const [model, setModel] = useState('...')
  const [showSettings, setShowSettings] = useState(false)

  useEffect(() => {
    getConfig().then((c) => setModel(c.model)).catch(() => {})
  }, [])

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
          className="mb-3 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200"
          value={wsInput}
          onChange={(e) => setWsInput(e.target.value)}
          onBlur={() => setWorkspace(wsInput || '.')}
          placeholder="/path/to/project"
        />
        <button
          className="mb-3 rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
          onClick={() => setShowSettings(true)}
        >
          Settings
        </button>
        <div className="mb-3 truncate text-xs text-zinc-500">
          Model: <span className="font-mono text-zinc-300">{model}</span>
        </div>
        <ConversationList />
      </aside>
      {showSettings && <SettingsModal onClose={() => setShowSettings(false)} />}
    </>
  )
}

function SettingsModal({ onClose }: { onClose: () => void }) {
  const [apiKey, setApiKey] = useState('')
  const [apiBase, setApiBase] = useState('')
  const [model, setModel] = useState('')
  const [maskedKey, setMaskedKey] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    getConfig()
      .then((c) => {
        setApiBase(c.api_base ?? '')
        setModel(c.model ?? '')
        setMaskedKey(c.api_key ?? '')
      })
      .catch((e) => setErr(String(e)))
  }, [])

  const save = async () => {
    setSaving(true)
    setErr(null)
    try {
      await updateConfig({
        api_key: apiKey || undefined,
        api_base: apiBase || undefined,
        model: model || undefined,
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
        className="w-96 rounded-lg border border-zinc-700 bg-zinc-900 p-4 text-sm text-zinc-200"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-3 font-semibold">Settings</h2>

        <label className="mb-1 block text-xs text-zinc-500">API key</label>
        <input
          type="password"
          className="mb-1 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
          placeholder={maskedKey ? 'key saved' : 'sk-...'}
          value={apiKey}
          onChange={(e) => setApiKey(e.target.value)}
        />
        <p className="mb-3 text-[10px] text-zinc-600">
          Leave blank to keep the existing key.
        </p>

        <label className="mb-1 block text-xs text-zinc-500">API base URL</label>
        <input
          className="mb-3 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
          value={apiBase}
          onChange={(e) => setApiBase(e.target.value)}
          placeholder="https://api.openai.com/v1"
        />

        <label className="mb-1 block text-xs text-zinc-500">Model</label>
        <input
          className="mb-3 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="gpt-4o-mini"
        />

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
  } = useAgent()
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const { abortController, setAbortController } = useAgent()

  const send = async () => {
    const text = input.trim()
    if (!text || sending) return
    setSending(true)
    setInput('')
    setError(null)
    appendUserMessage(text)
    const asstId = appendAssistantPlaceholder()
    const ac = new AbortController()
    setAbortController(ac)
    try {
      let cid: number
      if (conversationId === null) {
        const created = await createConversation('New chat', workspace)
        cid = created.id
        setConversationId(cid)
      } else {
        cid = conversationId
      }
      setStatus('thinking')
      await streamAgentTurn(
        cid,
        text,
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
    abortController?.abort()
  }

  return (
    <div className="border-t border-zinc-800 p-3">
      <div className="flex gap-2">
        <textarea
          className="flex-1 resize-none rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 focus:border-blue-500 focus:outline-none"
          rows={2}
          placeholder="Describe a task... (Enter to send, Shift+Enter for newline)"
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
          disabled={sending || !input.trim()}
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

function ActivityPanel() {
  const { log, clearLog } = useAgent()
  return (
    <aside className="hidden w-[22rem] min-w-[260px] flex-col border-l border-zinc-800 bg-zinc-900/60 lg:flex">
      <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
        <h2 className="text-xs font-semibold tracking-wide text-zinc-400">
          ACTIVITY
        </h2>
        <button
          onClick={clearLog}
          className="text-[10px] text-zinc-500 hover:text-zinc-300"
        >
          clear
        </button>
      </div>
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
    </aside>
  )
}

export { ActivityPanel }
