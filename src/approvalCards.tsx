import { useState } from 'react'
import { submitAnswer } from './api'
import { useAgent } from './store'

/**
 * Shared card shell: ledger band (design sign-off 2026-11-05, live session
 * 74694c80 v3) — ruled top/bottom hairlines on a deep well, mono micro-label
 * header, role hue carried by VALUES inside the band, never by chrome.
 * `color` keeps naming the role (orange=ask/gate, sky=plan) for callers and
 * for the accent-specific input/send treatments; the shell itself is hue-free.
 */
export function ApprovalCardShell({ color, icon, title, sub, children, err }: {
  color: 'orange' | 'sky'
  icon: React.ReactNode
  title: string
  /** Faint mono suffix after the title (e.g. "#live · pick an answer"). */
  sub?: string
  children: React.ReactNode
  err: string | null
}) {
  return (
    <div className="lg-card mb-2" role="status">
      <div className="lg-head">
        <span aria-hidden="true" className="lg-caret">{icon}</span>
        <span className="lg-title">{title}</span>
        {sub && <span className="lg-sub">{sub}</span>}
      </div>
      {children}
      {err && <p className="lg-danger">{err}</p>}
    </div>
  )
}

/**
 * Shared free-text answer input + send button: ledger input row (ruled top
 * hairline, ✎ key, underline input, mono SEND). sendColor picks which role
 * hue the focus underline + button carry.
 */
export function FreeTextAnswer({ custom, setCustom, submitting, submit, placeholder, sendColor = 'blue', inputClassName = '', onEdit }: {
  custom: string
  setCustom: (v: string) => void
  submitting: boolean
  submit: (text: string) => void
  placeholder: string
  sendColor?: string
  /** Extra classes for the input (e.g. the voiceSeeded underline). */
  inputClassName?: string
  /** Fired when the user edits the input, after setCustom. */
  onEdit?: () => void
}) {
  const inputAccent =
    sendColor === 'sky' ? 'lg-input--sky' : sendColor === 'orange' ? '' : 'lg-input--blue'
  return (
    <div className="lg-inputrow">
      <span aria-hidden="true" className="lg-key lg-key--faint">✎</span>
      <input
        autoFocus
        className={`lg-input ${inputAccent} ${inputClassName}`}
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
        className={`lg-send lg-send--${sendColor}`}
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
