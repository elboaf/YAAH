import { useState } from 'react'
import { submitAnswer } from './api'
import { useAgent } from './store'

/**
 * Shared card shell: bordered container with header icon, title, and error display.
 */
const SHELL_COLORS = {
  orange: { border: 'border-orange-700/60', text: 'text-orange-400' },
  sky: { border: 'border-sky-700/60', text: 'text-sky-400' }
}

export function ApprovalCardShell({ color, icon, title, children, err }: {
  color: 'orange' | 'sky'
  icon: React.ReactNode
  title: string
  children: React.ReactNode
  err: string | null
}) {
  const c = SHELL_COLORS[color]
  return (
    <div className={`rounded-lg border ${c.border} bg-zinc-900 p-3 shadow-lg`}>
      <div className={`mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest ${c.text}`}>
        {icon} {title}
      </div>
      {children}
      {err && <p className="mt-2 text-[11px] text-red-400">{err}</p>}
    </div>
  )
}

/**
 * Shared free-text answer input + send button.
 */
export function FreeTextAnswer({ custom, setCustom, submitting, submit, placeholder, sendColor = 'blue', inputClassName = '', onEdit }: {
  custom: string
  setCustom: (v: string) => void
  submitting: boolean
  submit: (text: string) => void
  placeholder: string
  sendColor?: string
  /** Extra classes for the input (e.g. the voiceSeeded highlight ring). */
  inputClassName?: string
  /** Fired when the user edits the input, after setCustom. */
  onEdit?: () => void
}) {
  return (
    <div className="flex gap-1.5">
      <input
        autoFocus
        className={`flex-1 rounded bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-zinc-500 focus:outline-none ${inputClassName}`}
        placeholder={placeholder}
        value={custom}
        onChange={(e) => {
          setCustom(e.target.value)
          onEdit?.()
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && custom.trim()) {
            e.preventDefault()
            submit(custom.trim())
          }
        }}
      />
      <button
        className={`rounded px-3 py-1.5 text-xs text-white disabled:opacity-50 ${
          sendColor === 'orange' ? 'bg-orange-600 hover:bg-orange-500' :
          sendColor === 'sky' ? 'bg-sky-600 hover:bg-sky-500' :
          'bg-blue-600 hover:bg-blue-500'
        }`}
        disabled={submitting || !custom.trim()}
        onClick={() => submit(custom.trim())}
      >
        {submitting ? '…' : 'Send'}
      </button>
    </div>
  )
}

/**
 * Shared submit state management for approval-type cards.
 * Returns { customOpen, setCustomOpen, custom, setCustom, submitting, err, respond }.
 * @param callId - the call ID for this pending item
 * @param onSubmitted - callback fired after submit succeeds (cards use this to clear their pending state)
 * @param customSend - optional custom submission function; defaults to submitAnswer(conversationId, callId, text)
 */
export function useCardSubmit(callId: string, onSubmitted?: (answer: string) => void, customSend?: (text: string) => Promise<unknown>) {
  const conversationId = useAgent((s) => s.conversationId)
  const [customOpen, setCustomOpen] = useState(false)
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const respond = (text: string) => {
    if (submitting) return
    if (conversationId === null && !customSend) return
    setSubmitting(true)
    setErr(null)
    const send = customSend
      ? customSend(text)
      : conversationId === null
        ? Promise.reject(new Error('no conversation'))
        : submitAnswer(conversationId, callId, text)
    send
      .then(() => {
        setSubmitting(false)
        if (onSubmitted) onSubmitted(text)
      })
      .catch((e) => {
        setErr(String(e))
        setSubmitting(false)
      })
  }

  return { customOpen, setCustomOpen, custom, setCustom, submitting, err, respond }
}
