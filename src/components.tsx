import { useEffect, useRef, useState } from 'react'
import { useAgent, type ChatMessage } from './store'

const API = 'http://localhost:8765'

// ---------------------------------------------------------------- icons
const roleIcon: Record<string, string> = {
  user: '🧑',
  assistant: '🤖',
  tool: '🔧',
}

function ToolCallBlock({ tc }: { tc: NonNullable<ChatMessage['toolCalls']>[number] }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="my-1 rounded border border-zinc-700 bg-zinc-800/60 text-xs">
      <button
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-zinc-300 hover:bg-zinc-700/40"
        onClick={() => setOpen((o) => !o)}
      >
        <span>{tc.result !== undefined ? '✅' : '⏳'}</span>
        <span className="font-mono font-semibold text-amber-300">{tc.name}</span>
        <span className="ml-auto text-zinc-500">{open ? '▾' : '▸'}</span>
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
      {!isUser && <span className="mt-1 select-none">{roleIcon[msg.role]}</span>}
      <div
        className={`max-w-[85%] rounded-lg px-3 py-2 text-sm ${
          isUser
            ? 'bg-blue-600 text-white'
            : 'bg-zinc-800 text-zinc-100'
        }`}
      >
        {msg.content && (
          <div className="whitespace-pre-wrap break-words">{msg.content}</div>
        )}
        {msg.toolCalls?.map((tc) => <ToolCallBlock key={tc.id} tc={tc} />)}
        {!msg.content && !msg.toolCalls?.length && (
          <span className="animate-pulse text-zinc-500">…</span>
        )}
      </div>
      {isUser && <span className="mt-1 select-none">🧑</span>}
    </div>
  )
}

// ---------------------------------------------------------------- panels

interface ConversationRow {
  id: number
  title: string
}

export function Sidebar() {
  const {
    conversationId,
    newConversation,
    setConversationId,
    loadHistoryFromApi,
    workspace,
    setWorkspace,
  } = useAgent()
  const [convs, setConvs] = useState<ConversationRow[]>([])
  const [model, setModel] = useState('…')
  const [wsInput, setWsInput] = useState(workspace)

  const refresh = () => {
    fetch(`${API}/api/conversations`)
      .then((r) => r.json())
      .then(setConvs)
      .catch(() => {})
  }

  useEffect(refresh, [])
  useEffect(() => {
    fetch(`${API}/api/config`)
      .then((r) => r.json())
      .then((c) => setModel(c.model))
      .catch(() => {})
  }, [])

  return (
    <aside className="flex w-64 min-w-[220px] flex-col border-r border-zinc-800 bg-zinc-900 p-3 text-sm">
      <h1 className="mb-3 font-semibold text-zinc-200">AI Coding Agent</h1>

      <button
        className="mb-3 rounded bg-blue-600 px-3 py-1.5 text-white hover:bg-blue-500"
        onClick={() => {
          newConversation()
          refresh()
        }}
      >
        + New chat
      </button>

      <div className="mb-3">
        <label className="mb-1 block text-xs text-zinc-500">Workspace</label>
        <input
          className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200"
          value={wsInput}
          onChange={(e) => setWsInput(e.target.value)}
          onBlur={() => setWorkspace(wsInput || '.')}
          placeholder="/path/to/project"
        />
      </div>

      <div className="mb-3 text-xs text-zinc-500">
        Model: <span className="font-mono text-zinc-300">{model}</span>
      </div>

      <div className="flex-1 overflow-auto">
        <div className="mb-1 text-xs text-zinc-500">History</div>
        {convs.map((c) => (
          <button
            key={c.id}
            className={`block w-full truncate rounded px-2 py-1.5 text-left text-xs hover:bg-zinc-800 ${
              c.id === conversationId ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-400'
            }`}
            onClick={async () => {
              setConversationId(c.id)
              await loadHistoryFromApi(c.id)
            }}
          >
            {c.title}
          </button>
        ))}
      </div>
    </aside>
  )
}

export function ChatPanel() {
  const { messages, status, error } = useAgent()
  const bottomRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  return (
    <main className="flex flex-1 flex-col overflow-hidden">
      <div className="flex-1 space-y-3 overflow-auto p-4">
        {messages.length === 0 && (
          <div className="flex h-full items-center justify-center text-zinc-600">
            Ask the agent to do something in your workspace…
          </div>
        )}
        {messages.map((m) => (
          <MessageView key={m.id} msg={m} />
        ))}
        {error && (
          <div className="rounded border border-red-800 bg-red-900/40 px-3 py-2 text-sm text-red-300">
            {error}
          </div>
        )}
        <div ref={bottomRef} />
      </div>
      {status === 'running-tool' && (
        <div className="px-4 pb-1 text-xs text-amber-400">🔧 running tool…</div>
      )}
      {status === 'thinking' && (
        <div className="px-4 pb-1 text-xs text-zinc-500">🤖 thinking…</div>
      )}
      <Composer />
    </main>
  )
}

function Composer() {
  const [text, setText] = useState('')
  const { status, conversationId, workspace, appendUserMessage, setStatus, setError } =
    useAgent()
  const {
    appendAssistantPlaceholder,
    appendTextDelta,
    startToolCall,
    finishToolCall,
  } = useAgent.getState()
  const busy = status === 'thinking' || status === 'running-tool'

  async function send() {
    const msg = text.trim()
    if (!msg || busy) return
    setText('')
    setError(null)
    appendUserMessage(msg)

    let cid = conversationId
    if (cid === null) {
      const created = await fetch(`${API}/api/conversations`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: msg.slice(0, 40) }),
      }).then((r) => r.json())
      cid = created.id
      useAgent.getState().setConversationId(created.id)
      fetch(`${API}/api/conversations`).catch(() => {})
    }

    const asstId = appendAssistantPlaceholder()
    setStatus('thinking')

    try {
      await import('./api').then(({ streamAgentTurn }) =>
        streamAgentTurn(cid!, msg, workspace, (ev) => {
          switch (ev.type) {
            case 'text':
              appendTextDelta(asstId, ev.text ?? '')
              break
            case 'tool_start':
              setStatus('running-tool')
              startToolCall(asstId, ev.name!, ev.args)
              break
            case 'tool_result':
              finishToolCall(asstId, ev.name!, ev.result)
              break
            case 'done':
              setStatus('idle')
              break
            case 'error':
              setError(ev.message ?? 'Unknown error')
              setStatus('error')
              break
          }
        }),
      )
      if (useAgent.getState().status !== 'error') setStatus('idle')
    } catch (e) {
      setError(String(e))
      setStatus('error')
    }
  }

  return (
    <div className="border-t border-zinc-800 p-3">
      <div className="flex gap-2">
        <textarea
          className="flex-1 resize-none rounded border border-zinc-700 bg-zinc-800 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-blue-500"
          rows={2}
          value={text}
          placeholder="Message the agent… (Enter to send, Shift+Enter for newline)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              send()
            }
          }}
        />
        <button
          className="rounded bg-blue-600 px-4 py-2 text-sm text-white hover:bg-blue-500 disabled:opacity-40"
          disabled={busy || !text.trim()}
          onClick={send}
        >
          Send
        </button>
      </div>
    </div>
  )
}
