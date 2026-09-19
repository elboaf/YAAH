import { useCallback, useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
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
  getFileChildren,
  previewFile,
  deleteFile,
  exportConversationMarkdown,
  deleteConversation,
  updateConversation,
  submitAnswer,
  listSkills,
  refreshSkills,
  getContext,
  getGitBranch,
  getGitInfo,
  getGitBranches,
  runGitCommand,
  type GitInfo,
  type GitAction,
  listMcpServers,
  addMcpServer,
  removeMcpServer,
  reloadMcpServers,
  type McpServerInfo,
  uploadAttachment,
  transcribeStatus,
  transcribeAudio,
  ttsStatus,
  ttsDownload,
  imageUrl,
  listWorkspaces,
  listLocalWorkspaces,
  addWorkspace,
  deleteWorkspace,
  discoverHosts,
  localInstanceInfo,
  remoteStatus,
  connectRemote,
  disconnectRemote,
  type RemoteHostFound,
  type RemoteStatus,
  type FileEntry,
  type ProviderPreset,
  type SkillInfo,
  type WorkspaceRow,
} from './api'
import { lastAssistantId, useAgent, type AccessMode, type ChatMessage, type PendingApproval, type PendingPlanApproval, type PendingQuestion, type ToolCall, type SubAgentRun } from './store'
import { useTts } from './speech'
import { useRemote, nsWorkspace, parseNsWorkspace } from './remoteStore'
import { diffLines, langOf, type DiffLine } from './codeview'
import { CodeBlock, AgentMarkdown } from './markdown'
import { VoiceRecorder } from './voice'

// ---------------------------------------------------------------- code views

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

/** Route a dictated answer to an ask_user question. Returns the matched
 *  option label, null when nothing matches (transcript is the free-text
 *  answer), or false when the transcript is in a different language than
 *  the labels (a match would not mean what the user thinks it means).
 *
 *  Matching is deliberately forgiving — dictation is lossy: case and
 *  punctuation are ignored, a leading "the" is dropped, and a transcript
 *  that merely CONTAINS an option label counts ("I think option B" → B).
 *  A bare letter ("b", "option b") matches the option at that position.
 *  No substring matching: "no" must never match "Not now".
 *
 *  `whisperLang` (BCP-47-ish code from /api/transcribe) sharpens the gate:
 *  labels are UI strings in one script, so a transcript dictated in a
 *  clearly different language is rejected up front. When absent, the
 *  transcript's own character script stands in. */
export function matchOptionLabel(
  transcript: string,
  labels: string[],
  whisperLang?: string | null,
): string | null | false {
  const t = transcript.trim().toLowerCase().replace(/[.!?,:;]+$/g, '').trim()
  if (!t || labels.length === 0) return null
  const langOf = (s: string) => {
    const letters = s.match(/\p{L}/gu) ?? []
    if (letters.length === 0) return null
    const latin = letters.filter((ch) => /[\u0000-\u024F\u1E00-\u1EFF]/u.test(ch)).length
    return latin / letters.length >= 0.6 ? 'latin' : 'other'
  }
  // Languages whose script is decisively not Latin — a transcript in one of
  // these cannot be answering labels written in a Latin script.
  const NON_LATIN = /^(zh|ja|ko|ru|uk|be|bg|sr|mk|ar|he|fa|ur|hi|bn|ta|te|th|km|my|el|ka|hy|yi)/
  let tLang: string | null = null
  if (whisperLang) tLang = NON_LATIN.test(whisperLang) ? 'other' : 'latin'
  const labelLangs = labels.map(langOf)
  const labelsLatin = labelLangs.some((l) => l === 'latin')
  if (tLang === null) tLang = langOf(t) // fall back to the transcript's script
  if (tLang && ((tLang === 'other' && labelsLatin) || (tLang === 'latin' && labelLangs.every((l) => l === 'other'))))
    return false
  const norm = (s: string) =>
    s
      .trim()
      .toLowerCase()
      .replace(/[^\p{L}\p{N}\s]/gu, '')
      .replace(/\s+/g, ' ')
  const normed = labels.map((l) => ({ raw: l, norm: norm(l) }))
  // 1. Exact (normalized) match.
  const exact = normed.find((x) => x.norm === t)
  if (exact) return exact.raw
  // 2. Bare letter / ordinal: "b", "option b", "the second one" → position.
  const ordinals = ['first', 'second', 'third', 'fourth', 'fifth', 'sixth']
  const mLetter = t.match(/^(?:option\s+)?([a-z])$/)
  if (mLetter) {
    const idx = mLetter[1].charCodeAt(0) - 97
    if (idx >= 0 && idx < normed.length) return normed[idx].raw
  }
  const mOrd = t.match(/^(?:the\s+)?(first|second|third|fourth|fifth|sixth)(?:\s+one)?$/)
  if (mOrd) {
    const idx = ordinals.indexOf(mOrd[1])
    if (idx >= 0 && idx < normed.length) return normed[idx].raw
  }
  // 3. Transcript contains a label (≥ 4 chars so "no" can't ride along).
  const contains = normed.find((x) => x.norm.length >= 4 && t.includes(x.norm))
  if (contains) return contains.raw
  // 4. Levenshtein ≤ 1 per word for a single-word label ("Continue" →
  //    "continue" misheard as "continues").
  for (const x of normed) {
    if (x.norm.split(' ').length !== 1) continue
    for (const w of t.split(' ')) {
      if (levenshtein(w, x.norm) <= 1) return x.raw
    }
  }
  return null
}

function levenshtein(a: string, b: string): number {
  if (a === b) return 0
  const m = a.length
  const n = b.length
  if (!m || !n) return Math.max(m, n)
  let prev = Array.from({ length: n + 1 }, (_, i) => i)
  for (let i = 1; i <= m; i++) {
    const cur = [i]
    for (let j = 1; j <= n; j++) {
      cur[j] = Math.min(
        prev[j] + 1,
        cur[j - 1] + 1,
        prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1),
      )
    }
    prev = cur
  }
  return prev[n]
}

function AskUserCard({ pending }: { pending: PendingQuestion }) {
  const conversationId = useAgent((s) => s.conversationId)
  const setPendingQuestion = useAgent((s) => s.setPendingQuestion)
  const [customOpen, setCustomOpen] = useState(false)
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Set when push-to-talk routes a transcript into the free-text box, so the
  // field opens pre-filled and visibly active instead of silently eating it.
  const [voiceSeeded, setVoiceSeeded] = useState(false)

  const answer = (text: string) => {
    if (conversationId === null || submitting) return
    setSubmitting(true)
    setErr(null)
    submitAnswer(conversationId, pending.callId, text)
      .then(() => {
        // Clear only if this is still the same question (a newer ask in
        // another conversation may have replaced it meanwhile).
        setPendingQuestion((q) => (q && q.callId === pending.callId ? null : q))
        // Reset the in-flight flag unconditionally: if a newer question
        // already replaced this one in the slot, the .then still fires with
        // this call's closure. Leaving `submitting` true here is what wedged
        // every follow-up question into an all-buttons-disabled card.
        setSubmitting(false)
      })
      .catch((e) => {
        setErr(String(e))
        setSubmitting(false)
      })
  }

  // Push-to-talk answer routing: the PTT release dispatches 'yaah-answer-ask'
  // (window event, because the hotkey fires while the card is unmounted or
  // the webview unfocused). An exact/fuzzy option match answers directly;
  // anything else becomes the free-text answer. PTT transcription is slow
  // relative to a click, so a stale event for an already-answered question
  // must be dropped, not submitted to whatever ask came next.
  useEffect(() => {
    const onVoiceAnswer = (e: Event) => {
      const { callId, answer: ans } = (e as CustomEvent<{ callId: string; answer: string }>).detail
      if (callId !== pending.callId || !ans || submitting) return
      const labels = pending.options.map((o) => o.label)
      const picked = matchOptionLabel(ans, labels)
      if (picked) {
        answer(picked)
      } else {
        setCustomOpen(true)
        setCustom(ans)
        setVoiceSeeded(true)
      }
    }
    window.addEventListener('yaah-answer-ask', onVoiceAnswer)
    return () => window.removeEventListener('yaah-answer-ask', onVoiceAnswer)
  })

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
              className={`flex-1 rounded border bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-orange-500 focus:outline-none ${
                voiceSeeded ? 'border-orange-500/70' : 'border-zinc-700'
              }`}
              placeholder={voiceSeeded ? 'Voice answer staged — edit or Send' : 'Type your own answer…'}
              value={custom}
              onChange={(e) => {
                setCustom(e.target.value)
                setVoiceSeeded(false)
              }}
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

/** Live approval card: a tool call blocked by the access-mode gate (ask
 *  mode). Approve executes it, Deny returns an error to the model; typed
 *  text denies with that text as guidance. Plan mode reuses this card with
 *  the plan-exit chip. Voice answers ride the same 'yaah-answer-ask' event
 *  (labels are option rows like any ask_user card). */
function ApprovalCard({ approval }: { approval: PendingApproval }) {
  const conversationId = useAgent((s) => s.conversationId)
  const setPendingApproval = useAgent((s) => s.setPendingApproval)
  const setAccessMode = useAgent((s) => s.setAccessMode)
  const [customOpen, setCustomOpen] = useState(false)
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const respond = (text: string) => {
    if (conversationId === null || submitting) return
    setSubmitting(true)
    setErr(null)
    submitAnswer(conversationId, approval.callId, text)
      .then(() => {
        setPendingApproval((a) => (a && a.callId === approval.callId ? null : a))
        setSubmitting(false)
      })
      .catch((e) => {
        setErr(String(e))
        setSubmitting(false)
      })
  }

  // Plan-mode exit lives in the status strip (PlanExitButton): plan mode
  // never holds a pending approval — blocked calls fail immediately and the
  // model presents its plan as text, so there is no card to hang the exit on.
  useEffect(() => {
    const onVoiceAnswer = (e: Event) => {
      const { callId, answer: ans } = (e as CustomEvent<{ callId: string; answer: string }>).detail
      if (callId !== approval.callId || !ans || submitting) return
      const norm = ans.trim().toLowerCase()
      if (norm === 'approve' || norm === 'yes' || norm === 'ok' || norm === 'approve it' || norm === 'allow') {
        respond('approve')
      } else if (norm === 'deny' || norm === 'no' || norm === 'deny it' || norm === 'reject' || norm === 'stop') {
        respond('deny')
      } else {
        // Anything else is guidance: deny, telling the model why.
        respond(ans)
      }
    }
    window.addEventListener('yaah-answer-ask', onVoiceAnswer)
    return () => window.removeEventListener('yaah-answer-ask', onVoiceAnswer)
  })

  // One-line arg summary per tool, mono-styled, so the card reads like a
  // trace chip rather than prose.
  const summary = (() => {
    const a = approval.args ?? {}
    const pick = (...keys: string[]) => {
      for (const k of keys) {
        const v = a[k]
        if (typeof v === 'string' && v.trim()) return v
      }
      return ''
    }
    switch (approval.tool) {
      case 'bash':
      case 'powershell':
        return pick('command')
      case 'write_file':
      case 'create_file':
        return pick('path', 'file_path')
      case 'edit_file':
        return pick('path', 'file_path')
      case 'delete_file':
      case 'move_file':
        return pick('path', 'src', 'dst')
      case 'git_commit':
        return pick('message')
      case 'git_push':
      case 'git_pull':
        return `${approval.tool === 'git_push' ? 'push' : 'pull'} ${pick('remote') || 'origin'}`
      default: {
        const kv = Object.entries(a)
          .filter(([, v]) => typeof v === 'string' && v.length < 80)
          .slice(0, 2)
          .map(([k, v]) => `${k}=${v}`)
          .join(' ')
        return kv
      }
    }
  })()

  const commandLike = approval.tool === 'bash' || approval.tool === 'powershell'

  return (
    <div className="rounded-lg border border-amber-700/60 bg-zinc-900 p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-amber-400">
        <span className="run-pulse">!</span> approval needed
      </div>
      <p className="mb-1.5 flex items-center gap-2 font-mono text-xs text-zinc-100">
        <span className="text-amber-300">{approval.tool}</span>
        {summary && <span className="truncate text-zinc-400">{summary}</span>}
      </p>
      {commandLike && (
        <pre className="mb-2 max-h-32 overflow-y-auto whitespace-pre-wrap rounded border border-zinc-800 bg-zinc-950 p-2 font-mono text-[11px] text-zinc-300">
          {String(approval.args?.command ?? '')}
        </pre>
      )}
      <div className="flex gap-1.5">
        <button
          className="rounded bg-emerald-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
          disabled={submitting}
          onClick={() => respond('approve')}
        >
          {submitting ? '…' : 'Approve'}
        </button>
        <button
          className="rounded border border-zinc-600 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
          disabled={submitting}
          onClick={() => respond('deny')}
        >
          Deny
        </button>
      </div>
      {!customOpen ? (
        <button
          className="mt-1.5 block w-full rounded border border-dashed border-zinc-600 px-2.5 py-1.5 text-left text-xs text-zinc-400 hover:border-amber-500/60 hover:text-zinc-200"
          disabled={submitting}
          onClick={() => setCustomOpen(true)}
        >
          Deny with a note…
        </button>
      ) : (
        <div className="mt-1.5 flex gap-1.5">
          <input
            autoFocus
            className="flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-amber-500 focus:outline-none"
            placeholder="Why deny? Sent to the model as guidance…"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && custom.trim()) {
                e.preventDefault()
                respond(custom.trim())
              }
            }}
          />
          <button
            className="rounded bg-amber-600 px-3 py-1.5 text-xs text-white hover:bg-amber-500 disabled:opacity-50"
            disabled={submitting || !custom.trim()}
            onClick={() => respond(custom.trim())}
          >
            {submitting ? '…' : 'Send'}
          </button>
        </div>
      )}
      {err && <p className="mt-2 text-[11px] text-red-400">{err}</p>}
    </div>
  )
}

/** Live exit_plan card: the agent's plan blocked the run under plan mode.
 *  Approve & run flips the access mode to full (the backend does the same
 *  save before resuming) and the SAME turn continues into execution; typed
 *  text is a change request — plan mode stays on and the model revises. */
function PlanApprovalCard({ pending }: { pending: PendingPlanApproval }) {
  const conversationId = useAgent((s) => s.conversationId)
  const setPendingPlanApproval = useAgent((s) => s.setPendingPlanApproval)
  const setAccessMode = useAgent((s) => s.setAccessMode)
  const [customOpen, setCustomOpen] = useState(false)
  const [custom, setCustom] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  const respond = (text: string) => {
    if (conversationId === null || submitting) return
    setSubmitting(true)
    setErr(null)
    submitAnswer(conversationId, pending.callId, text)
      .then(() => {
        if (text === 'approve') {
          // The backend already saved access_mode=full before unblocking the
          // run; mirror it into the store so the UI follows immediately.
          setAccessMode('full')
          window.dispatchEvent(
            new CustomEvent('yaah-access-mode-changed', { detail: { mode: 'full' } }),
          )
        }
        setPendingPlanApproval((p) => (p && p.callId === pending.callId ? null : p))
        setSubmitting(false)
      })
      .catch((e) => {
        setErr(String(e))
        setSubmitting(false)
      })
  }

  // Push-to-talk routing: PTT release dispatches 'yaah-answer-plan' (its own
  // event — the ask/approval cards answer a different vocabulary). "Approve"
  // wording approves; anything else becomes change-request feedback.
  useEffect(() => {
    const onVoiceAnswer = (e: Event) => {
      const { callId, answer: ans } = (e as CustomEvent<{ callId: string; answer: string }>).detail
      if (callId !== pending.callId || !ans || submitting) return
      if (ans === 'approve') respond('approve')
      else respond(ans)
    }
    window.addEventListener('yaah-answer-plan', onVoiceAnswer)
    return () => window.removeEventListener('yaah-answer-plan', onVoiceAnswer)
  })

  return (
    <div className="rounded-lg border border-sky-700/60 bg-zinc-900 p-3 shadow-lg">
      <div className="mb-2 flex items-center gap-2 font-mono text-[10px] uppercase tracking-widest text-sky-400">
        <span className="run-pulse">▸</span> plan ready — approve to run
      </div>
      <div className="mb-2.5 max-h-64 overflow-y-auto text-sm text-zinc-100">
        <AgentMarkdown content={pending.plan} />
      </div>
      <div className="flex gap-1.5">
        <button
          className="rounded bg-emerald-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-emerald-600 disabled:opacity-50"
          disabled={submitting}
          onClick={() => respond('approve')}
        >
          {submitting ? '…' : 'Approve & run'}
        </button>
        {!customOpen && (
          <button
            className="rounded border border-zinc-600 px-3 py-1.5 text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
            disabled={submitting}
            onClick={() => setCustomOpen(true)}
          >
            Request changes
          </button>
        )}
      </div>
      {customOpen && (
        <div className="mt-1.5 flex gap-1.5">
          <input
            autoFocus
            className="flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1.5 text-xs text-zinc-100 focus:border-sky-500 focus:outline-none"
            placeholder="What should change? Sent to the model as feedback…"
            value={custom}
            onChange={(e) => setCustom(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && custom.trim()) {
                e.preventDefault()
                respond(custom.trim())
              }
            }}
          />
          <button
            className="rounded bg-sky-600 px-3 py-1.5 text-xs text-white hover:bg-sky-500 disabled:opacity-50"
            disabled={submitting || !custom.trim()}
            onClick={() => respond(custom.trim())}
          >
            {submitting ? '…' : 'Send'}
          </button>
        </div>
      )}
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
  if (name === 'exit_plan') return '▸'
  if (name === 'spawn_agent') return '⧉'
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
  if (name === 'exit_plan') return 'text-sky-400'
  if (name === 'spawn_agent') return 'text-fuchsia-400'
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

/** One compact chip: glyph + name + target, counting while the call runs. */
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
      {!done && <ElapsedBadge startedAt={tc.startedAt} />}
      {!done && <span className="run-pulse text-amber-300">●</span>}
    </span>
  )
}

// ---- liveness clock --------------------------------------------------------
// One shared 100ms interval for every elapsed counter on screen. The timer
// exists only while at least one subscriber is mounted, so a settled UI
// (everything finished) costs zero timers.

const tickerListeners = new Set<() => void>()
let tickerTimer: ReturnType<typeof setInterval> | null = null

function subscribeTicker(cb: () => void) {
  tickerListeners.add(cb)
  if (!tickerTimer) tickerTimer = setInterval(() => tickerListeners.forEach((l) => l()), 100)
  return () => {
    tickerListeners.delete(cb)
    if (tickerListeners.size === 0 && tickerTimer) {
      clearInterval(tickerTimer)
      tickerTimer = null
    }
  }
}

/** Re-renders the caller on the shared 100ms clock. Only mount it under
 *  something that is actually running. */
function useNow(): number {
  return useSyncExternalStore(
    subscribeTicker,
    () => Date.now(),
    () => 0,
  )
}

function formatElapsed(ms: number): string {
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)}s`
  if (ms < 60_000) return `${Math.floor(ms / 1000)}s`
  return `${Math.floor(ms / 60_000)}:${String(Math.floor((ms % 60_000) / 1000)).padStart(2, '0')}`
}

/** Live elapsed time since `startedAt`, tabular so digits don't jitter. */
function ElapsedBadge({ startedAt, className = 'text-zinc-500' }: { startedAt?: number; className?: string }) {
  const now = useNow()
  const ms = Math.max(0, now - (startedAt ?? now))
  return <span className={`tabular-nums ${className}`}>{formatElapsed(ms)}</span>
}

/** The telemetry tape: one borderless terminal line per conversation where
 *  every tool event of the session flows by — call, arguments, streamed
 *  output, response, timing — newest at the right edge, old text pushing
 *  out through a left fade. Lives in the store, so it survives tool calls,
 *  thinking gaps, and turn boundaries. Rendered in a narrow window under
 *  the ticker chips, right edge aligned with the newest chip's right edge.
 *  Not meant to be read; it is proof that output is occurring. */
function LiveTelemetry() {
  const tape = useAgent((s) => s.tapeByConv[s.bufferKey()] ?? '')
  const wrapRef = useRef<HTMLDivElement>(null)
  const tapeRef = useRef<HTMLSpanElement>(null)
  const [offset, setOffset] = useState(0)
  useEffect(() => {
    const w = wrapRef.current?.clientWidth ?? 0
    const t = tapeRef.current?.scrollWidth ?? 0
    setOffset(Math.min(0, w - t))
  }, [tape])
  return (
    <div
      ref={wrapRef}
      className="overflow-hidden"
      style={{
        maskImage:
          'linear-gradient(to right, transparent 0%, black 14%, black 100%)',
        WebkitMaskImage:
          'linear-gradient(to right, transparent 0%, black 14%, black 100%)',
      }}
    >
      <span
        ref={tapeRef}
        className="block whitespace-pre font-mono text-[10px] leading-4 text-zinc-400"
        style={{ transform: `translateX(${offset}px)` }}
      >
        {tape}
      </span>
    </div>
  )
}

/** Collapse a chunk of tool output to one flowing tape line: line endings
 *  become wide separators so the tape never wraps or stacks. */
function oneLine(s: string): string {
  return s.replace(/[\r\n]+/g, '    ').replace(/\t/g, '  ')
}

/** Live, ephemeral stream of calls while the agent works. Newest chip appears
 *  at the left edge and older ones are pushed right, fading out at the right
 *  edge; the row never grows past the chat panel's width. The telemetry tape
 *  runs in a window directly below, whose right edge lines up with the
 *  newest chip's right edge — tape and chip read as one column. */
function ToolTicker({ calls }: { calls: ToolCall[] }) {
  const recent = calls.slice(-12)
  const rowRef = useRef<HTMLDivElement>(null)
  const [tapeWidth, setTapeWidth] = useState<number | null>(null)
  useEffect(() => {
    const align = rowRef.current?.querySelector('[data-tape-align]')
    if (align instanceof HTMLElement) setTapeWidth(align.offsetLeft + align.offsetWidth)
  }, [calls])
  const fade =
    'linear-gradient(to right, black 72%, rgba(0,0,0,0.35) 90%, transparent 100%)'
  return (
    <div className="my-1 w-full min-w-0">
      <div
        ref={rowRef}
        className="relative flex items-center gap-1.5 overflow-hidden"
        style={{ maskImage: fade, WebkitMaskImage: fade }}
      >
        <span className="shrink-0 font-mono text-[10px] text-zinc-600">
          {calls.length > recent.length ? `${calls.length} calls` : 'working…'}
        </span>
        {[...recent].reverse().map((tc, i) => (
          <span
            key={tc.id}
            data-tape-align={i === 0 ? '' : undefined}
            className={`shrink-0 ${i === 0 ? 'chip-in' : ''}`}
          >
            <ToolChip tc={tc} />
          </span>
        ))}
      </div>
      {tapeWidth !== null && tapeWidth > 0 && (
        <div className="-mt-px" style={{ width: tapeWidth }}>
          <LiveTelemetry />
        </div>
      )}
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
    if (tc.name === 'exit_plan') {
      const result = (tc.result ?? {}) as { decision?: string; feedback?: string; note?: string }
      return (
        <div className="rounded border border-sky-800/60 bg-zinc-900/60 p-2">
          <p className="mb-1.5 whitespace-pre-wrap font-sans text-xs text-zinc-200">{String(args.plan ?? '')}</p>
          <p className="font-mono text-[11px] text-sky-300">
            {result.decision === 'approved'
              ? '✓ approved — plan mode off, executing'
              : result.decision === 'revised'
                ? `↻ changes requested: ${result.feedback ?? ''}`
                : (result.note ?? 'no decision')}
          </p>
        </div>
      )
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

  if (tc.subAgent) {
    return <SubAgentBlock run={tc.subAgent} />
  }

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

/** Live nested transcript for one spawn_agent call: the sub-agent's own
 *  text deltas and tool chips, indented under the parent turn. Collapses
 *  to a status line when the run finishes; expands on click. The running
 *  transcript carries the streaming caret. */
function SubAgentBlock({ run }: { run: SubAgentRun }) {
  const [open, setOpen] = useState(true)
  const textRef = useRef<HTMLDivElement>(null)
  const running = run.status === 'running'
  useEffect(() => {
    const el = textRef.current
    if (el && running) el.scrollTop = el.scrollHeight
  }, [run.text, running])
  const statusLabel =
    run.status === 'running'
      ? 'running'
      : run.status === 'completed'
        ? 'done'
        : run.status === 'error'
          ? 'error'
          : run.status === 'cancelled'
            ? 'stopped'
            : 'max turns'
  const statusColor = running
    ? 'text-amber-300'
    : run.status === 'error'
      ? 'text-red-400'
      : 'text-emerald-400'
  return (
    <div className="my-1 rounded border border-zinc-800 bg-zinc-900/40">
      <button
        className="flex w-full items-center gap-2 px-2 py-1 text-left font-mono text-[10px]"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-fuchsia-400">{'\u29c9'}</span>
        <span className="text-zinc-300">{run.agentType}</span>
        <span className="truncate text-zinc-600">{run.prompt}</span>
        <span className={`ml-auto shrink-0 ${statusColor}`}>
          {running && <span className="run-pulse mr-1">{'\u25cf'}</span>}
          {statusLabel}
        </span>
        <span className="shrink-0 text-zinc-600">{open ? '\u25be' : '\u25b8'}</span>
      </button>
      {open && (
        <div className="border-t border-zinc-800/80 px-3 py-1.5">
          {run.tools.length > 0 && (
            <div className="mb-1 flex flex-wrap gap-1">
              {run.tools.map((t) => (
                <span
                  key={t.id}
                  className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[10px] ${
                    t.result !== undefined
                      ? 'bg-zinc-800/70 text-zinc-400'
                      : 'bg-zinc-700/60 text-zinc-200'
                  }`}
                >
                  <span className={toolGlyphColor(t.name)}>{toolGlyph(t.name)}</span>
                  <span>{t.name}</span>
                  {t.result === undefined && (
                    <span className="run-pulse text-amber-300">{'\u25cf'}</span>
                  )}
                </span>
              ))}
            </div>
          )}
          {run.text && (
            <div
              ref={textRef}
              className={`whitespace-pre-wrap break-words font-mono text-[11px] leading-4 text-zinc-400 ${
                running ? 'max-h-40 overflow-auto' : ''
              }`}
            >
              {run.text}
              {running && <span className="stream-caret" />}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/** Agent message body: full markdown rendering (see src/markdown.tsx). */
function MessageBody({ content }: { content: string }) {
  return <AgentMarkdown content={content} />
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
          {msg.skills?.length ? (
            <div className="mt-1.5 flex flex-wrap justify-end gap-1">
              {msg.skills.map((name) => (
                <span
                  key={name}
                  title={`Skill loaded for this turn: ${name}`}
                  className="rounded bg-indigo-900/60 px-1.5 py-0.5 font-mono text-[10px] text-indigo-200"
                >
                  ${name}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      </div>
    )
  }

  // Plan-approval boundary: this message is the planning emission when it
  // carries an APPROVED exit_plan call (a revised/rejected one keeps the
  // same message going — planning continues there). Once approved it folds
  // to a one-line header; the execution runs in the next message.
  const exitCalls = msg.toolCalls?.filter((t) => t.name === 'exit_plan') ?? []
  const planApproved = exitCalls.some(
    (c) => (c.result as { decision?: string } | undefined)?.decision === 'approved',
  )

  const body = (
    <>
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
    </>
  )

  if (planApproved) {
    return <PlanningFold msg={msg} body={body} />
  }

  return (
    <div className="border-l-2 border-zinc-700/70 pl-3">
      <div className="mb-0.5 flex items-center gap-2 select-none font-mono text-[10px] uppercase tracking-widest text-zinc-600">
        agent
        {/* Stop control on the message currently being read aloud. */}
        <MessageStopButton msgId={msg.id} />
      </div>
      {msg.implementsPlan && <PlanBanner plan={msg.implementsPlan} />}
      {body}
    </div>
  )
}

/** Collapsed planning emission: everything the model said/did before its
 *  exit_plan call was approved, behind one sky header. Expandable for the
 *  full reasoning trail (text + trace, exactly the normal message body). */
function PlanningFold({ msg, body }: { msg: ChatMessage; body: ReactNode }) {
  const [open, setOpen] = useState(false)
  const n = msg.toolCalls?.length ?? 0
  return (
    <div className="border-l-2 border-sky-800/50 pl-3">
      <button
        className="flex w-full items-center gap-2 py-0.5 text-left font-mono text-[10px] uppercase tracking-widest text-sky-400 hover:text-sky-300"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-sky-600">{open ? '▾' : '▸'}</span>
        <span>planning</span>
        {n > 0 && (
          <span className="tracking-normal text-zinc-600 normal-case">
            {n} call{n === 1 ? '' : 's'}
          </span>
        )}
        <span className="ml-auto tracking-normal text-zinc-600 normal-case">
          ✓ approved — {open ? 'hide' : 'show'}
        </span>
      </button>
      {open && <div className="mt-0.5">{body}</div>}
    </div>
  )
}

/** Header on the execution half of a plan turn: the approved plan sits
 *  above the tool calls so what the model is implementing stays visible.
 *  Plan body collapsible; open by default, scrolled if long. */
function PlanBanner({ plan }: { plan: string }) {
  const [open, setOpen] = useState(true)
  return (
    <div className="mb-2 rounded border border-sky-800/60 bg-sky-950/30">
      <button
        className="flex w-full items-center gap-2 px-2.5 py-1 text-left font-mono text-[10px] uppercase tracking-widest text-sky-400"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-sky-600">{open ? '▾' : '▸'}</span>
        <span>implementing approved plan</span>
        <span className="ml-auto tracking-normal text-zinc-600 normal-case">
          {open ? 'hide plan' : 'show plan'}
        </span>
      </button>
      {open && (
        <div className="max-h-64 overflow-y-auto border-t border-sky-800/40 px-3 py-2 text-sm text-zinc-100">
          <AgentMarkdown content={plan} />
        </div>
      )}
    </div>
  )
}

/** Stop control for the message currently being read aloud: a small square
 *  next to the "agent" label while that message's audio plays. */
function MessageStopButton({ msgId }: { msgId: string }) {
  const speakingMsgId = useTts((s) => s.speakingMsgId)
  const stop = useTts((s) => s.stop)
  if (speakingMsgId !== msgId) return null
  return (
    <button
      className="rounded border border-amber-600/70 px-1 leading-none text-amber-300 hover:bg-zinc-800"
      title="Stop reading aloud"
      aria-label="Stop reading aloud"
      onClick={stop}
    >
      <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor" aria-hidden="true">
        <rect x="1" y="1" width="6" height="6" rx="1" />
      </svg>
    </button>
  )
}

// ---------------------------------------------------------------- file tree (Q8/Q12/Q40)

function TreeRow({
  entry,
  depth,
  onOpen,
  onContext,
  loadChildren,
}: {
  entry: FileEntry
  depth: number
  onOpen: (e: FileEntry) => void
  onContext: (e: FileEntry, x: number, y: number) => void
  loadChildren: (e: FileEntry) => Promise<FileEntry[]>
}) {
  const [openDir, setOpenDir] = useState(depth < 1)
  // Eager entries carry children; lazy ones fetch on first expand.
  const [kids, setKids] = useState<FileEntry[] | null>(entry.children ?? null)
  const [loadingKids, setLoadingKids] = useState(false)
  const toggleDir = () => {
    if (!openDir && kids === null) {
      setLoadingKids(true)
      loadChildren(entry)
        .then((entries) => setKids(entries))
        .catch(() => setKids([]))
        .finally(() => setLoadingKids(false))
    }
    setOpenDir((o) => !o)
  }
  return (
    <>
      <button
        className="block w-full truncate rounded px-1 py-0.5 text-left text-[11px] hover:bg-zinc-800"
        style={{ paddingLeft: `${depth * 12 + 4}px` }}
        onClick={() => {
          if (entry.type === 'dir') toggleDir()
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
      {entry.type === 'dir' && openDir && (
        <>
          {loadingKids ? (
            <p
              className="py-0.5 font-mono text-[10px] text-zinc-600"
              style={{ paddingLeft: `${(depth + 1) * 12 + 4}px` }}
            >
              loading…
            </p>
          ) : (
            (kids ?? []).map((c) => (
              <TreeRow
                key={c.path}
                entry={c}
                depth={depth + 1}
                onOpen={onOpen}
                onContext={onContext}
                loadChildren={loadChildren}
              />
            ))
          )}
        </>
      )}
    </>
  )
}

// Session cache of fetched trees, keyed by scope+workspace: flipping between
// This device and a host repaints instantly from here instead of refetching.
// The backend now returns a depth-limited tree, so a fetch is cheap anyway.
const treeCache = new Map<string, FileEntry[]>()

/** Replace one entry's children in a tree (lazy expansion patch), returning
 *  the new tree. Depth-first by path — paths are unique in a workspace. */
function patchChildren(tree: FileEntry[], path: string, entries: FileEntry[]): FileEntry[] {
  return tree.map((e) => {
    if (e.path === path) return { ...e, children: entries, lazy: false }
    if (e.children) return { ...e, children: patchChildren(e.children, path, entries) }
    return e
  })
}

export function FilesPanel() {
  const { workspace, previewPath, setPreviewPath, status } = useAgent()
  const scope = useRemote((s) => s.scope)
  const [tree, setTree] = useState<FileEntry[]>([])
  const [menu, setMenu] = useState<{ entry: FileEntry; x: number; y: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('filesPanelCollapsed') === '1')
  const [deleteTarget, setDeleteTarget] = useState<FileEntry | null>(null)
  const cacheKey = `${scope.connected ? scope.url : 'local'}|${workspace}`

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
    // Paint the cached tree immediately when we have one — a scope flip or a
    // turn-end refresh must not blank the panel while the (cheap, shallow)
    // fetch is in flight.
    const cached = treeCache.get(cacheKey)
    if (cached) setTree(cached)
    setLoading(!cached)
    getFileTree(workspace)
      .then((r) => {
        treeCache.set(cacheKey, r.tree)
        setTree(r.tree)
        setErr(null)
      })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false))
  }, [cacheKey, workspace])

  useEffect(refresh, [refresh])

  /** Fetch one directory's children (lazy tree) and patch tree + cache. */
  const loadChildren = useCallback(
    (entry: FileEntry) =>
      getFileChildren(workspace, entry.path).then((r) => {
        setTree((cur) => {
          const next = patchChildren(cur, entry.path, r.entries)
          treeCache.set(cacheKey, next)
          return next
        })
        return r.entries
      }),
    [cacheKey, workspace],
  )

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
              loadChildren={loadChildren}
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

/** localStorage key for a group's expanded (chats visible) state. */
const expandKey = (path: string | null) =>
  `yaah.group.expanded.${path ?? 'default'}`

/** Groups for the greyed "This device" section shown while connected. */
function buildLocalGroups(
  localWorkspaces: WorkspaceRow[],
  localConvs: Array<{ id: number; title: string; workspace: string | null }>,
) {
  const groups: Array<{ ws: WorkspaceRow; items: typeof localConvs }> = localWorkspaces.map(
    (ws) => ({ ws, items: localConvs.filter((c) => (c.workspace ?? null) === ws.path) }),
  )
  const known = new Set(localWorkspaces.map((w) => w.path))
  for (const c of localConvs) {
    if (!known.has(c.workspace ?? null)) {
      groups.push({
        ws: {
          id: -1,
          path: c.workspace ?? null,
          label: c.workspace === null ? 'Default (Home)' : wsBasename(c.workspace),
          last_opened_at: null,
          exists: true,
          conversation_count: 0,
        },
        items: [],
      })
    }
  }
  return groups.filter((g) => g.items.length > 0 || g.ws.path === null)
}

function ConversationList() {
  const { conversationId, setConversationId, loadHistory, setWorkspace, newConversation, workspace } = useAgent()
  const [convs, setConvs] = useState<Array<{ id: number; title: string; workspace: string | null; updated_at: string }>>([])
  const [workspaces, setWorkspaces] = useState<WorkspaceRow[]>([])
  // Expanded groups show their chats (capped, with show-more stepping);
  // collapsed groups show the header only. Persisted per workspace.
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  // Session-only show-more stepping: +5 visible chats per click, resets on
  // restart (deliberate tightness persists; casual browsing doesn't).
  const [extra, setExtra] = useState<Record<string, number>>({})
  const [notice, setNotice] = useState<{ title: string; message: string } | null>(null)
  const [sysTarget, setSysTarget] = useState<{ id: number; title: string } | null>(null)
  const [deleteTarget, setDeleteTarget] = useState<{ id: number; title: string } | null>(null)
  const [removeWsTarget, setRemoveWsTarget] = useState<WorkspaceRow | null>(null)
  const [menuOpenId, setMenuOpenId] = useState<number | null>(null)
  // Active connection scope decides which registry/chats are shown; local
  // rows render greyed while a host is connected.
  const scope = useRemote((s) => s.scope)
  const [localWorkspaces, setLocalWorkspaces] = useState<WorkspaceRow[]>([])

  const refresh = useCallback(() => {
    listConversations().then(setConvs).catch(() => setConvs([]))
    // Scope-aware: the backend returns the HOST's registry (namespaced)
    // while connected, this machine's otherwise.
    listWorkspaces()
      .then(setWorkspaces)
      .catch(() => setWorkspaces([]))
    if (scope.connected) {
      listLocalWorkspaces().then(setLocalWorkspaces).catch(() => setLocalWorkspaces([]))
    } else {
      setLocalWorkspaces([])
    }
  }, [scope.connected, scope.hostId])
  useEffect(() => {
    refresh()
    // workspace too: adding a workspace (Sidebar) flips the active workspace,
    // and the new registry row must appear without any other refresh trigger.
  }, [conversationId, workspace, refresh])

  // Expanded state persists per workspace (Q14); a group with no remembered
  // state starts expanded.
  useEffect(() => {
    const next: Record<string, boolean> = {}
    for (const w of workspaces) {
      const key = expandKey(w.path ?? '')
      try {
        next[key] = localStorage.getItem(key) !== '0'
      } catch {
        next[key] = true
      }
    }
    setExpanded(next)
  }, [workspaces])

  /** Name click: collapsed -> expand + start a new draft chat in this
   *  workspace; expanded -> collapse. No chevron — the name is the toggle. */
  const toggleGroup = (path: string | null) => {
    const key = expandKey(path ?? '')
    const wasExpanded = expanded[key] ?? true
    setExpanded((c) => ({ ...c, [key]: !wasExpanded }))
    // Re-expansion starts fresh at 5+active: show-more stepping is
    // session-scoped browsing state, dropped on collapse.
    setExtra((e) => ({ ...e, [key]: 0 }))
    try {
      localStorage.setItem(key, wasExpanded ? '0' : '1')
    } catch {
      /* non-persistent toggle is fine */
    }
    if (!wasExpanded) {
      setWorkspace(path ?? '')
      newConversation()
    }
  }

  // Esc closes an open row menu — the app-wide dialog contract (dialogs,
  // settings, preview all dismiss on Escape; the row menu is no exception).
  useEffect(() => {
    if (menuOpenId === null) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpenId(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpenId])

  // Chats visible in the active scope: while connected, only conversations
  // namespaced to THIS host; otherwise only non-remote ones. A chat from a
  // different host stays hidden entirely (it would be unopenable anyway).
  const inScope = (ws: string | null) => {
    const ns = parseNsWorkspace(ws)
    return scope.connected ? ns?.hostId === scope.hostId : ns === null
  }
  const visibleConvs = convs.filter((c) => inScope(c.workspace))
  const localConvs = convs.filter((c) => parseNsWorkspace(c.workspace) === null)

  /** Open a conversation and adopt its workspace (the core invariant: the
   *  open conversation's workspace IS the active workspace, both ways).
   *  Scope guard: a local chat must never open while connected. */
  const openConversation = (c: { id: number; workspace: string | null }) => {
    if (!inScope(c.workspace)) return
    setConversationId(c.id)
    setWorkspace(c.workspace ?? '')
    getMessages(c.id)
      .then((rows) => loadHistory(c.id, rows))
      .catch(() => {})
  }

  // Group rows by workspace; Default (null path) first, then by the most
  // recent conversation activity in each group.
  const groups: Array<{ ws: WorkspaceRow; items: typeof convs }> = []
  for (const w of workspaces) {
    groups.push({ ws: w, items: visibleConvs.filter((c) => (c.workspace ?? null) === w.path) })
  }
  const knownPaths = new Set(workspaces.map((w) => w.path))
  for (const c of visibleConvs) {
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
        const key = expandKey(ws.path ?? '')
        const isExpanded = expanded[key] ?? true
        const isActiveWs = (ws.path ?? '') === (workspace || '')
        // Capped view: the 5 most recent chats, plus the open conversation
        // appended whenever it ranks older (the list never hides what you're
        // looking at); "show more" steps +5 per click, session-only.
        const activeIdx = items.findIndex((c) => c.id === conversationId)
        const base = Math.min(5 + (extra[key] ?? 0), items.length)
        const head = items.slice(0, base)
        // The active chat sits outside the head block: append it (never a
        // duplicate — only when its index is past the head) so it stays
        // visible directly above the "show more" line.
        const visible =
          activeIdx >= base ? [...head, items[activeIdx]] : head
        const hidden = items.length - visible.length
        return (
          <div key={ws.path ?? 'default'} className="mb-3">
            {/* Workspace section: bold header, hairline top rule, chat count,
                remove menu. The name IS the toggle: click expands the group
                and starts a new chat there; click again collapses. The active
                workspace (the open conversation's workspace) carries the
                state marker — it survives collapse so a hidden active chat
                stays findable. */}
            <div className="border-t border-zinc-800 pt-2 first:border-t-0 first:pt-0">
            <div className="group flex items-center gap-0.5 rounded px-1 py-1 hover:bg-zinc-800/60">
              <button
                className={`min-w-0 flex-1 truncate text-left font-mono text-[11px] font-semibold uppercase tracking-wider ${
                  isActiveWs ? 'text-zinc-100' : 'text-zinc-400'
                } hover:text-zinc-200`}
                title={
                  ws.path === null
                    ? 'No root directory — conversations without a workspace'
                    : parseNsWorkspace(ws.path)?.path || ws.path
                }
                aria-expanded={isExpanded}
                onClick={() => toggleGroup(ws.path)}
              >
                {ws.label}
                {ws.path !== null && !ws.exists && (
                  <span className="ml-1 text-amber-500" title="Folder not found on disk">
                    ⚠
                  </span>
                )}
              </button>
              <span
                className="mr-1 font-mono text-[10px] text-zinc-600"
                title={`${items.length} conversation${items.length === 1 ? '' : 's'} in this workspace`}
              >
                {items.length}
              </span>
              {ws.path !== null && (
                <button
                  className="rounded px-1 text-[10px] text-zinc-600 opacity-0 hover:text-red-400 group-hover:opacity-100 focus:opacity-100"
                  aria-label={`Remove workspace ${ws.label}`}
                  title="Remove this workspace (its conversations move to Default)"
                  onClick={() => setRemoveWsTarget(ws)}
                >
                  ✕
                </button>
              )}
              {isActiveWs && (
                <span
                  aria-hidden="true"
                  className="h-1.5 w-1.5 shrink-0 rounded-full bg-blue-500"
                  title="Active workspace"
                />
              )}
            </div>
            {isExpanded &&
              (items.length > 0 ? (
                <>
                  {visible.map((c) => (
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
                  ))}
                  {hidden > 0 && (
                    <button
                      className="block w-full px-3 py-1 text-left text-[11px] text-zinc-600 hover:text-zinc-300"
                      onClick={() => setExtra((e) => ({ ...e, [key]: (e[key] ?? 0) + 5 }))}
                    >
                      Show more ({hidden} more)
                    </button>
                  )}
                </>
              ) : (
                <p className="px-3 py-1 text-[10px] text-zinc-600">No conversations yet.</p>
              ))}
            </div>
          </div>
        )
      })}
      {convs.length === 0 && groups.length === 0 && (
        <p className="px-2 py-3 text-center text-[11px] text-zinc-600">No conversations yet.</p>
      )}
      {scope.connected && (
        <div
          className="mb-3 border-t border-zinc-800 pt-2 opacity-40 select-none"
          title="Local chats — switch back to “This device” to open them"
          aria-disabled="true"
        >
          <p className="px-2 pb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
            💻 This device — view only
          </p>
          {buildLocalGroups(localWorkspaces, localConvs).map(({ ws, items }) => {
            // Same 5-cap + show-more as live groups (session-only stepping,
            // keyed under local: to stay apart from workspace keys); no
            // toggle — the section is view-only.
            const lkey = `local:${ws.path ?? 'default'}`
            const lbase = Math.min(5 + (extra[lkey] ?? 0), items.length)
            const lvisible = items.slice(0, lbase)
            return (
              <div key={ws.path ?? 'default'} className="mb-1 px-1">
                <p className="truncate py-0.5 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
                  {ws.label}
                </p>
                {lvisible.map((c) => (
                  <p key={c.id} className="truncate rounded px-2 py-1 text-xs text-zinc-600">
                    {c.title}
                  </p>
                ))}
                {items.length > lvisible.length && (
                  <button
                    className="block w-full px-2 py-1 text-left text-[11px] text-zinc-600 hover:text-zinc-400"
                    onClick={() => setExtra((e) => ({ ...e, [lkey]: (e[lkey] ?? 0) + 5 }))}
                  >
                    Show more ({items.length - lvisible.length} more)
                  </button>
                )}
              </div>
            )
          })}
        </div>
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
                const moved =
                  conversationId !== null &&
                  !!convs.find(
                    (c) => c.id === conversationId && c.workspace === removeWsTarget.path,
                  )
                if (conversationId !== null) {
                  getMessages(conversationId)
                    .then((rows) => loadHistory(conversationId, rows))
                    .catch(() => {})
                }
                // Removing the selected workspace clears the selection: keeping
                // the stale path would re-persist it as last_workspace on the
                // next turn, and the backend re-seeds the deleted row from
                // that (issue #11).
                if (moved || workspace === removeWsTarget.path) setWorkspace('')
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
        className={`flex min-w-0 flex-1 items-center rounded px-2 py-1.5 text-left text-xs ${
          active ? 'bg-blue-600 text-white' : 'text-zinc-300 hover:bg-zinc-800'
        }`}
        onClick={onOpen}
        title={conv.title}
      >
        <span className="min-w-0 flex-1 truncate">{conv.title}</span>
        <span
          className={`ml-1.5 shrink-0 font-mono text-[9px] ${active ? 'text-blue-200' : 'text-zinc-600'}`}
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
  const { newConversation, workspace, setWorkspace, clearLog } = useAgent()
  const [model, setModel] = useState('...')
  const [activeProvider, setActiveProvider] = useState('')
  // name -> {models, error?} for every configured provider
  const [byProvider, setByProvider] = useState<Record<string, ProviderModels>>({})
  const [savingModel, setSavingModel] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [notice, setNotice] = useState<{ title: string; message: string } | null>(null)

  // The workspace is remembered across restarts: the store seeds itself from
  // localStorage, and config.json is the durable fallback for a fresh install,
  // cleared storage, or a first run on a new machine. A remote-namespaced
  // last workspace is meaningless locally — the store already rejects it.
  useEffect(() => {
    if (workspace && workspace !== '.') return
    getConfig()
      .then((c) => {
        if (c.last_workspace && !parseNsWorkspace(c.last_workspace)) setWorkspace(c.last_workspace)
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Registry refresh is owned by ConversationList; the Sidebar only needs
  // model/config state for the footer plus the add-workspace flows.
  const scope = useRemote((s) => s.scope)

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
  }, [refreshModels])

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

  // While connected there is no client-side folder picker for host paths:
  // the folder dialog can only open on the machine running the UI. Adding a
  // host workspace is a free-text path, validated by the host on register.
  const [showRemoteAdd, setShowRemoteAdd] = useState(false)
  const [remoteAddPath, setRemoteAddPath] = useState('')

  const addRemoteWorkspace = async () => {
    const path = remoteAddPath.trim()
    if (!path) return
    try {
      await addWorkspace(path)
      setWorkspace(path)
      newConversation()
      setShowRemoteAdd(false)
      setRemoteAddPath('')
    } catch (e) {
      setNotice({
        title: 'Could not add folder on host',
        message: String((e as Error).message ?? e).replace(/^\d+:\s*/, ''),
      })
    }
  }

  /** Open a workspace from the list: its most recent conversation, or a
   *  fresh chat when it has none (Q2: the list is the conversation switcher). */
  const browseWorkspace = async () => {
    // Native folder picker when running inside Tauri (Q28)
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      const picked = await invoke<string | null>('pick_workspace')
      if (picked) {
        await addWorkspace(picked).catch(() => null)
        setWorkspace(picked)
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
        <ConversationList />
        {/* Add workspace: a persistent, labeled action row — the affordance
            the old dropdown buried as a pseudo-option. */}
        <button
          className="mb-2 flex w-full items-center gap-1.5 rounded border border-dashed border-zinc-700 px-2 py-1.5 text-left text-xs text-zinc-400 hover:border-zinc-500 hover:bg-zinc-800/60 hover:text-zinc-200"
          onClick={scope.connected ? () => setShowRemoteAdd(true) : () => void browseWorkspace()}
        >
          <span aria-hidden="true" className="text-sm leading-none text-zinc-500">+</span>
          {scope.connected ? 'Add folder on host…' : 'Add workspace…'}
        </button>
        {scope.connected && showRemoteAdd && (
          <div className="mb-3 rounded border border-zinc-700 bg-zinc-800 p-2">
            <input
              autoFocus
              className="mb-1.5 w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs"
              placeholder="folder path on the host, e.g. C:/repos/proj"
              value={remoteAddPath}
              onChange={(e) => setRemoteAddPath(e.target.value)}
              onKeyDown={(e) => e.key === 'Escape' && setShowRemoteAdd(false)}
              aria-label="Folder path on the host"
            />
            <div className="flex justify-end gap-1.5">
              <button
                className="rounded border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-900"
                onClick={() => setShowRemoteAdd(false)}
              >
                Cancel
              </button>
              <button
                className="rounded bg-blue-600 px-2 py-0.5 text-[10px] text-white hover:bg-blue-500"
                onClick={() => void addRemoteWorkspace()}
              >
                Add on host
              </button>
            </div>
          </div>
        )}
        {/* Footer strip: configuration lives at the bottom, pinned — the
            conversation list owns the column. Model readout in mono (the
            machine's voice), gear for Settings. */}
        <div className="mt-auto border-t border-zinc-800 pt-2">
          <div className="flex items-center gap-1">
            <select
              className="min-w-0 flex-1 truncate rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-200 focus:border-blue-500 focus:outline-none"
              value={`${activeProvider}::${model}`}
              onChange={(e) => pickModel(e.target.value)}
              aria-label="Model"
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
            <button
              className="shrink-0 rounded border border-zinc-700 p-1.5 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
              aria-label="Settings"
              title="Settings"
              onClick={() => setShowSettings(true)}
            >
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="3" />
                <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z" />
              </svg>
            </button>
          </div>
          {Object.keys(byProvider).length === 0 && (
            <button
              className="mt-1.5 w-full rounded border border-amber-700/60 bg-amber-950/30 px-2 py-1 text-left text-[10px] leading-relaxed text-amber-300 hover:border-amber-500"
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

/** MCP tool servers (Settings panel section). Each registered server is a
 *  local program the backend launches; its tools appear to the model as
 *  mcp_<server>_<tool>. Registration is trust — no per-call confirmations. */
function McpSection() {
  const [servers, setServers] = useState<McpServerInfo[]>([])
  const [name, setName] = useState('')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      setServers((await listMcpServers()).servers)
    } catch {
      /* transient backend hiccup — the poll retries */
    }
  }, [])

  // Poll while any server is still starting, so status/ticks arrive live.
  useEffect(() => {
    void refresh()
    const t = setInterval(() => {
      setServers((cur) => {
        if (cur.some((s) => s.status === 'starting')) void refresh()
        return cur
      })
    }, 1500)
    return () => clearInterval(t)
  }, [refresh])

  const add = async () => {
    setErr(null)
    if (!name.trim() || !command.trim()) {
      setErr('name and command are required')
      return
    }
    setBusy(true)
    try {
      const argList = args
        .split(/\s+/)
        .map((a) => a.trim())
        .filter(Boolean)
      setServers((await addMcpServer({ name: name.trim(), command: command.trim(), args: argList })).servers)
      setName('')
      setCommand('')
      setArgs('')
    } catch (e) {
      setErr(String((e as { message?: string }).message ?? e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (server: string) => {
    setBusy(true)
    try {
      setServers((await removeMcpServer(server)).servers)
    } catch (e) {
      setErr(String((e as { message?: string }).message ?? e))
    } finally {
      setBusy(false)
    }
  }

  const statusColor = (s: McpServerInfo['status']) =>
    s === 'connected'
      ? 'text-emerald-400'
      : s === 'failed'
        ? 'text-red-400'
        : s === 'stopped'
          ? 'text-zinc-500'
          : 'text-amber-400'

  return (
    <>
      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        MCP tool servers
      </h3>
      <div className="mb-2 space-y-1.5">
        {servers.length === 0 && (
          <p className="text-[10px] text-zinc-600">
            No servers registered. An MCP server is a local tool program (browser control, git,
            databases...) whose tools the agent can call directly — more reliable than GUI
            automation.
          </p>
        )}
        {servers.map((s) => (
          <div key={s.name} className="rounded border border-zinc-800 bg-zinc-900/60 p-2">
            <div className="flex items-center gap-2">
              <span className={`font-mono text-[10px] uppercase ${statusColor(s.status)}`}>
                {s.status}
              </span>
              <span className="font-mono text-xs text-zinc-200">{s.name}</span>
              <span className="flex-1 truncate font-mono text-[10px] text-zinc-600">
                {s.command} {s.args.join(' ')}
              </span>
              <button
                className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800"
                onClick={() => setExpanded(expanded === s.name ? null : s.name)}
              >
                {s.tools.length} tool{s.tools.length === 1 ? '' : 's'}
              </button>
              <button
                className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-red-400 hover:bg-zinc-800"
                disabled={busy}
                onClick={() => void remove(s.name)}
              >
                remove
              </button>
            </div>
            {s.status === 'failed' && s.error && (
              <p className="mt-1 text-[10px] text-red-400">{s.error}</p>
            )}
            {expanded === s.name && (
              <ul className="mt-1.5 space-y-0.5">
                {s.tools.map((t) => (
                  <li key={t.name} className="text-[10px] text-zinc-400">
                    <span className="font-mono text-zinc-300">{t.name}</span>
                    {t.description ? ` — ${t.description}` : ''}
                  </li>
                ))}
                {s.tools.length === 0 && (
                  <li className="text-[10px] text-zinc-600">no tools discovered yet</li>
                )}
              </ul>
            )}
          </div>
        ))}
        <div className="flex gap-1.5">
          <input
            className="w-24 shrink-0 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="name"
            aria-label="Server name"
          />
          <input
            className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            value={command}
            onChange={(e) => setCommand(e.target.value)}
            placeholder='command, e.g. npx -y @modelcontextprotocol/server-filesystem ~'
            aria-label="Server command"
          />
          <input
            className="w-40 shrink-0 rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
            value={args}
            onChange={(e) => setArgs(e.target.value)}
            placeholder="args (space-separated)"
            aria-label="Server args"
          />
          <button
            className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
            disabled={busy}
            onClick={() => void add()}
          >
            Add
          </button>
        </div>
        <p className="text-[10px] text-zinc-600">
          Runs locally with your permissions — registering a server trusts it. Its tools appear to
          the agent as mcp_&lt;name&gt;_&lt;tool&gt;. Config is stored in config.json (mcpServers).
        </p>
      </div>
      {err && <p className="mb-2 text-xs text-red-400">{err}</p>}
    </>
  )
}

/** One settings group: mono micro-label header, hairline frame, compact
 *  body. Cards are flat (no fill, no shadow) — hairlines do the grouping. */
function SettingsCard({
  title,
  className = '',
  children,
}: {
  title: string
  className?: string
  children: React.ReactNode
}) {
  return (
    <section className={`rounded-lg border border-zinc-800 p-3 ${className}`}>
      <h3 className="mb-2.5 font-mono text-[10px] font-medium uppercase tracking-[0.1em] text-zinc-500">
        {title}
      </h3>
      {children}
    </section>
  )
}

/** Shared field chrome: raised fill, hairline border, blue focus border.
 *  Width is set per-use (w-full / flex-1 / fixed). */
const settingsInputCls =
  'rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-100 focus:border-blue-500 focus:outline-none'

function SettingsModal({ onClose }: { onClose: () => void }) {
  // Local working copy of the providers map: blank key field = keep saved key
  const [providers, setProviders] = useState<Record<string, { api_base: string; model: string; apiKeyInput: string; savedKey: boolean }>>({})
  const [active, setActive] = useState('')
  const [newName, setNewName] = useState('')
  const [temperature, setTemperature] = useState<number | ''>('')
  const [maxTokens, setMaxTokens] = useState<number | ''>('')
  const [maxSteps, setMaxSteps] = useState<number | ''>('')
  // Per-model context-window overrides (model id -> tokens); blank = auto.
  const [ctxOverrides, setCtxOverrides] = useState<Record<string, number>>({})
  const [ctxModelDraft, setCtxModelDraft] = useState('')
  const [ctxTokensDraft, setCtxTokensDraft] = useState<number | ''>('')
  // Interface scale draft (1.0 / 1.1 / 1.25 / 1.5) — applied live on save.
  const [uiScale, setUiScale] = useState(1.0)
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
  // Push-to-talk hotkey (Tauri accelerator string); captured live from the
  // keyboard when the user clicks "record".
  const [pttHotkeyDraft, setPttHotkeyDraft] = useState('')
  const [capturingHotkey, setCapturingHotkey] = useState(false)
  // Read-aloud (TTS): voice + speed drafts; model download state.
  const [ttsVoiceDraft, setTtsVoiceDraft] = useState('af_heart')
  const [ttsSpeedDraft, setTtsSpeedDraft] = useState(1.0)
  const [ttsModelReady, setTtsModelReady] = useState(false)
  const [ttsDownloading, setTtsDownloading] = useState(false)
  const [ttsDlPct, setTtsDlPct] = useState<number | null>(null)
  const [ttsDlErr, setTtsDlErr] = useState<string | null>(null)
  const previewVoice = useTts((s) => s.previewVoice)
  // Playback failures (preview or chat) surface here too — the toggle
  // tooltip is invisible when the user is inside Settings.
  const ttsUiError = useTts((s) => s.error)
  // LAN hosting: on by default; passphrase gates remote tool execution.
  const [remoteHost, setRemoteHost] = useState(true)
  const [remotePass, setRemotePass] = useState('')
  const [remoteName, setRemoteName] = useState('')
  // Config load state: Save stays disabled until a load SUCCEEDED — saving
  // the empty initial copy would wipe every stored provider (the merge
  // treats an absent provider as deleted).
  const [loaded, setLoaded] = useState(false)

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
    // Retry the config load: the backend can be momentarily busy (or still
    // starting), and a failed load that looks like "no providers" is a
    // wipe-in-waiting.
    const loadConfig = async () => {
      for (let attempt = 0; attempt < 4; attempt++) {
        try {
          const c = await getConfig()
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
          setCtxOverrides(c.context_window_overrides ?? {})
          setUiScale(Number(c.ui_scale) || 1.0)
          const v = c.voice
          setVoiceEngine(v?.engine === 'cloud' ? 'cloud' : 'local')
          setCloudEndpoint(v?.cloud_endpoint ?? '')
          setCloudKeySaved(v?.cloud_api_key === 'set')
          setCloudModel(v?.cloud_model || '')
          setPttHotkeyDraft(v?.ptt_hotkey ?? '')
          setTtsVoiceDraft(v?.tts_voice || 'af_heart')
          setTtsSpeedDraft(v?.tts_speed ?? 1.0)
          // Passphrase is stored plaintext by design (like provider keys),
          // so Settings can show and edit it directly.
          setRemoteHost(c.remote?.hosting_enabled ?? true)
          setRemotePass(c.remote?.passphrase ?? '')
          setRemoteName(c.remote?.display_name ?? '')
          setLoaded(true)
          setErr(null)
          return
        } catch (e) {
          if (attempt === 3) {
            setErr(
              `Could not load settings: ${(e as Error).message}. Save is disabled so your saved providers cannot be wiped by accident.`,
            )
          } else {
            await new Promise((r) => setTimeout(r, 800 * (attempt + 1)))
          }
        }
      }
    }
    void loadConfig()
    getProviders().then(setPresets).catch(() => {})
    transcribeStatus()
      .then((s) => {
        setVoiceLocalReady(s.local_available)
        setVoiceLocalModel(s.local_model)
      })
      .catch(() => {})
    ttsStatus()
      .then((s) => {
        setTtsModelReady(s.available)
        setTtsDownloading(s.downloading)
        if (s.available) setTtsVoiceDraft(s.tts_voice || s.default_voice)
      })
      .catch(() => {})
  }, [])

  const patchProvider = (name: string, patch: Partial<{ api_base: string; model: string; apiKeyInput: string }>) =>
    setProviders((ps) => ({ ...ps, [name]: { ...ps[name], ...patch } }))

  // Live hotkey capture: the next non-modifier keydown becomes the
  // accelerator. Capture-phase listener so Esc cancels the capture instead
  // of closing Settings.
  useEffect(() => {
    if (!capturingHotkey) return
    const onKey = (e: KeyboardEvent) => {
      e.preventDefault()
      e.stopPropagation()
      const key = e.key
      if (key === 'Escape') {
        setCapturingHotkey(false)
        return
      }
      if (['Control', 'Shift', 'Alt', 'Meta'].includes(key)) return // modifier alone
      const parts: string[] = []
      if (e.ctrlKey) parts.push('Ctrl')
      if (e.altKey) parts.push('Alt')
      if (e.shiftKey) parts.push('Shift')
      if (e.metaKey) parts.push('Super')
      let main: string | null = null
      if (/^[a-z0-9]$/i.test(key)) main = key.toUpperCase()
      else if (/^F\d{1,2}$/.test(key)) main = key
      else if (key === ' ') main = 'Space'
      else if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(key)) main = key
      else if (/^[`\-=[\];'",./\\]$/.test(key)) main = key.toUpperCase()
      if (!main) return // unrepresentable key — wait for another
      parts.push(main)
      setPttHotkeyDraft(parts.join('+'))
      setCapturingHotkey(false)
    }
    window.addEventListener('keydown', onKey, true)
    return () => window.removeEventListener('keydown', onKey, true)
  }, [capturingHotkey])

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
        context_window_overrides: ctxOverrides,
        ui_scale: uiScale,
        voice: {
          engine: voiceEngine,
          cloud_endpoint: cloudEndpoint,
          // Typed key replaces; empty/kept field is dropped server-side so
          // the saved key survives.
          ...(cloudKey ? { cloud_api_key: cloudKey } : {}),
          cloud_model: cloudModel,
          ptt_hotkey: pttHotkeyDraft,
          tts_voice: ttsVoiceDraft,
          tts_speed: ttsSpeedDraft,
        },
        remote: {
          hosting_enabled: remoteHost,
          passphrase: remotePass,
          display_name: remoteName,
        },
      })
      // Hot-swap the live registration in the Composer; it reports failure
      // (combo taken by another app) through the same reject toast system.
      window.dispatchEvent(new CustomEvent('ptt-hotkey-changed', { detail: pttHotkeyDraft }))
      // Live-apply the interface scale (App's UiScale listens and re-zooms).
      window.dispatchEvent(new CustomEvent('ui-scale-changed', { detail: { scale: uiScale } }))
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
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      {/* Percentage sizing only: the app root is zoomed (UiScale), so viewport
          units would double-zoom. Header/footer pinned, body scrolls. */}
      <div
        className="flex max-h-[90%] w-[92%] max-w-4xl flex-col overflow-hidden rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-zinc-800 px-4 py-3">
          <h2 className="text-sm font-semibold text-zinc-100">Settings</h2>
          <button
            type="button"
            className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200"
            onClick={onClose}
            aria-label="Close settings"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              <path d="M18 6 6 18M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <div className="grid grid-cols-4 gap-3">
            {/* providers: collapsed rows, active first; fields behind one open row */}
            <SettingsCard title="Providers" className="col-span-4">
              {providerOrder.length === 0 && (
                <p className="text-[11px] text-zinc-600">
                  No providers yet — add one below to start using the agent.
                </p>
              )}
              <div className="space-y-1.5">
                {providerOrder.map((name) => {
                  const p = providers[name]
                  const isOpen = expanded === name
                  const summary = p.savedKey
                    ? 'key saved'
                    : p.apiKeyInput
                      ? 'key entered'
                      : 'no key'
                  return (
                    <div
                      key={name}
                      className={`rounded border ${active === name ? 'border-zinc-700' : 'border-zinc-800'}`}
                    >
                      <div className="flex items-center gap-2.5 px-2.5 py-2">
                        <input
                          type="radio"
                          name="active-provider"
                          checked={active === name}
                          onChange={() => setActive(name)}
                          title="Make active"
                          aria-label={`Make ${name} the active provider`}
                          className="h-3 w-3 shrink-0 accent-blue-600"
                        />
                        <button
                          type="button"
                          className="flex min-w-0 flex-1 items-center gap-2 text-left"
                          onClick={() => setExpanded(isOpen ? null : name)}
                          aria-expanded={isOpen}
                          aria-label={`Toggle ${name} settings`}
                        >
                          <span className={`truncate font-mono text-xs ${active === name ? 'text-zinc-100' : 'text-zinc-200'}`}>
                            {name}
                          </span>
                          {!isOpen && (
                            <span className="truncate font-mono text-[10px] text-zinc-600">{summary}</span>
                          )}
                          <span className="ml-auto shrink-0 text-[10px] text-zinc-500">
                            {isOpen ? '▾' : '▸'}
                          </span>
                        </button>
                      </div>
                      {isOpen && (
                        <div className="border-t border-zinc-800 p-2.5">
                          <div className="mb-1.5 flex gap-1.5">
                            <input
                              className={`${settingsInputCls} min-w-0 flex-1`}
                              value={p.api_base}
                              onChange={(e) => patchProvider(name, { api_base: e.target.value })}
                              placeholder="https://api.openai.com/v1"
                              aria-label={`${name} API base URL`}
                            />
                            <select
                              className={`${settingsInputCls} w-36 shrink-0 px-1 text-[10px] text-zinc-300`}
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
                            className={`${settingsInputCls} w-full`}
                            placeholder={p.savedKey ? 'key saved' : 'sk-... (optional for local)'}
                            value={p.apiKeyInput}
                            onChange={(e) => patchProvider(name, { apiKeyInput: e.target.value })}
                            aria-label={`${name} API key`}
                          />
                          <div className="mt-2 flex justify-end">
                            <button
                              type="button"
                              className="text-[10px] text-red-400 hover:text-red-300"
                              onClick={() => setRemoveTarget(name)}
                            >
                              remove provider
                            </button>
                          </div>
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>

              {/* add a provider: preset templates or a custom OpenAI-compatible URL */}
              <div className="mt-2.5 flex gap-1.5">
                <input
                  className={`${settingsInputCls} min-w-0 flex-1`}
                  placeholder="new provider name (or 'custom')"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  aria-label="New provider name"
                />
                <button
                  type="button"
                  className="shrink-0 rounded border border-zinc-700 px-2 text-[11px] text-zinc-300 hover:bg-zinc-800"
                  onClick={() => addProvider(newName)}
                >
                  + custom
                </button>
              </div>
              <div className="mt-1.5 flex flex-wrap gap-1">
                {Object.keys(presets).map((preset) => (
                  <button
                    key={preset}
                    type="button"
                    className="rounded border border-zinc-700 px-2 py-1 font-mono text-[10px] text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
                    onClick={() => addProvider(providers[preset] ? `${preset}-2` : preset, preset)}
                  >
                    + {preset}
                  </button>
                ))}
              </div>
            </SettingsCard>

            <SettingsCard title="Generation" className="col-span-2">
              <div className="grid grid-cols-3 gap-2.5">
                <div>
                  <label className="mb-1 block text-[10px] text-zinc-500">Temperature</label>
                  <input
                    type="number"
                    step="0.1"
                    min="0"
                    max="2"
                    className={`${settingsInputCls} w-full`}
                    value={temperature}
                    onChange={(e) => setTemperature(e.target.value === '' ? '' : Number(e.target.value))}
                  />
                </div>
                <div>
                  <label className="mb-1 block text-[10px] text-zinc-500">Max tokens</label>
                  <input
                    type="number"
                    min="0"
                    className={`${settingsInputCls} w-full`}
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(e.target.value === '' ? '' : Number(e.target.value))}
                  />
                  <p className="mt-1 text-[10px] text-zinc-600">0 = provider default</p>
                </div>
                <div>
                  <label className="mb-1 block text-[10px] text-zinc-500">Max steps</label>
                  <input
                    type="number"
                    min="0"
                    className={`${settingsInputCls} w-full`}
                    value={maxSteps}
                    onChange={(e) => setMaxSteps(e.target.value === '' ? '' : Number(e.target.value))}
                  />
                  <p className="mt-1 text-[10px] text-zinc-600">0 = unlimited (Stop still works)</p>
                </div>
              </div>

              <div className="mt-2.5 border-t border-zinc-800 pt-2.5">
                <label className="mb-1 block text-[10px] text-zinc-500">Context window overrides</label>
                <p className="mb-1.5 text-[10px] text-zinc-600">
                  Tokens per model id — wins over the provider-reported value and the built-in
                  table (powers the context readout in the chat panel).
                </p>
                <div className="flex gap-1.5">
                  <input
                    type="text"
                    placeholder="model id"
                    aria-label="Model id for the context window override"
                    className={`${settingsInputCls} min-w-0 flex-1`}
                    value={ctxModelDraft}
                    onChange={(e) => setCtxModelDraft(e.target.value)}
                  />
                  <input
                    type="number"
                    min="0"
                    placeholder="tokens"
                    aria-label="Context window in tokens"
                    className={`${settingsInputCls} w-24 shrink-0`}
                    value={ctxTokensDraft}
                    onChange={(e) => setCtxTokensDraft(e.target.value === '' ? '' : Number(e.target.value))}
                  />
                  <button
                    type="button"
                    className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
                    onClick={() => {
                      const id = ctxModelDraft.trim()
                      if (!id || !ctxTokensDraft || ctxTokensDraft <= 0) return
                      setCtxOverrides((o) => ({ ...o, [id]: ctxTokensDraft as number }))
                      setCtxModelDraft('')
                      setCtxTokensDraft('')
                    }}
                  >
                    Set
                  </button>
                </div>
                {Object.keys(ctxOverrides).length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {Object.entries(ctxOverrides).map(([id, win]) => (
                      <span
                        key={id}
                        className="flex items-center gap-1 rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300"
                      >
                        {id}: {win.toLocaleString()}
                        <button
                          aria-label={`Remove override for ${id}`}
                          className="text-zinc-500 hover:text-red-400"
                          onClick={() =>
                            setCtxOverrides((o) => {
                              const n = { ...o }
                              delete n[id]
                              return n
                            })
                          }
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </SettingsCard>

            <SettingsCard title="Voice dictation" className="col-span-2">
              <div className="mb-2.5 flex gap-1.5" role="radiogroup" aria-label="Transcription engine">
                {(['local', 'cloud'] as const).map((engine) => (
                  <button
                    key={engine}
                    type="button"
                    role="radio"
                    aria-checked={voiceEngine === engine}
                    className={`flex-1 rounded border px-2 py-1.5 font-mono text-xs ${
                      voiceEngine === engine
                        ? 'border-blue-600 bg-blue-600/15 text-zinc-100'
                        : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'
                    }`}
                    onClick={() => setVoiceEngine(engine)}
                  >
                    {engine === 'local' ? 'local whisper' : 'cloud (BYOK)'}
                  </button>
                ))}
              </div>
              {voiceEngine === 'local' ? (
                <p className="text-[10px] leading-relaxed text-zinc-600">
                  {voiceLocalReady ? (
                    <>
                      On-device engine ready — model{' '}
                      <span className="font-mono text-zinc-500">{voiceLocalModel}</span>. Audio never
                      leaves this machine.
                    </>
                  ) : (
                    <>
                      No local whisper engine found (packaged installs bundle one; this looks like a
                      dev run). Use cloud, or place a whisper.cpp CLI + ggml model under backend/.
                    </>
                  )}
                </p>
              ) : (
                <div className="space-y-1.5">
                  <input
                    className={`${settingsInputCls} w-full`}
                    value={cloudEndpoint}
                    onChange={(e) => setCloudEndpoint(e.target.value)}
                    placeholder="https://api.openai.com/v1  or  http://192.168.1.10:8080/inference"
                    aria-label="Cloud transcription endpoint"
                  />
                  <div className="flex gap-1.5">
                    <input
                      type="password"
                      className={`${settingsInputCls} min-w-0 flex-1`}
                      value={cloudKey}
                      onChange={(e) => setCloudKey(e.target.value)}
                      placeholder={cloudKeySaved ? 'key saved (optional)' : 'API key (optional)'}
                      aria-label="Cloud transcription API key"
                    />
                    <input
                      className={`${settingsInputCls} w-28 shrink-0`}
                      value={cloudModel}
                      onChange={(e) => setCloudModel(e.target.value)}
                      placeholder="model (optional)"
                      aria-label="Cloud transcription model"
                    />
                  </div>
                  <p className="text-[10px] leading-relaxed text-zinc-600">
                    OpenAI-compatible /audio/transcriptions endpoint (base URL is fine) or a
                    whisper.cpp server /inference URL. Key and model are optional. Recordings are
                    sent to that server.
                  </p>
                </div>
              )}

              <div className="mt-2.5 border-t border-zinc-800 pt-2.5">
                <div className="mb-1.5 flex items-center gap-1.5">
                  <span className="text-[10px] text-zinc-500">Push-to-talk</span>
                  <button
                    type="button"
                    className={`shrink-0 rounded border px-2 py-1 font-mono text-xs ${
                      capturingHotkey
                        ? 'border-amber-600 bg-amber-950/40 text-amber-200'
                        : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'
                    }`}
                    onClick={() => setCapturingHotkey(true)}
                    aria-label="Record push-to-talk hotkey"
                  >
                    {capturingHotkey ? 'press keys…' : pttHotkeyDraft || 'set hotkey'}
                  </button>
                  <button
                    type="button"
                    className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-800"
                    onClick={() => setPttHotkeyDraft('')}
                    aria-label="Disable push-to-talk hotkey"
                  >
                    off
                  </button>
                </div>
                <p className="text-[10px] leading-relaxed text-zinc-600">
                  Hold the key to record, release to transcribe and send — works system-wide, even
                  when YAAH is in the background. Pressing it stops a running turn first; with a
                  question card up, the transcript answers it. Esc cancels capture; "off" disables.
                </p>
              </div>
            </SettingsCard>

            <SettingsCard title="Read aloud" className="col-span-2">
              {!ttsModelReady ? (
                <div>
                  <p className="mb-1.5 text-[10px] leading-relaxed text-zinc-600">
                    The agent can read its responses aloud with an on-device voice (Kokoro —
                    nothing leaves this machine). One-time download:
                  </p>
                  {ttsDownloading ? (
                    <div className="rounded border border-zinc-800 bg-zinc-800/40 px-2 py-1.5">
                      <div className="mb-1 flex justify-between font-mono text-[10px] text-zinc-400">
                        <span>downloading voice model…</span>
                        <span>{ttsDlPct !== null ? `${ttsDlPct}%` : ''}</span>
                      </div>
                      <div className="h-1 overflow-hidden rounded bg-zinc-700">
                        <div
                          className="h-full bg-blue-500 transition-all"
                          style={{ width: `${ttsDlPct ?? 0}%` }}
                        />
                      </div>
                    </div>
                  ) : (
                    <button
                      type="button"
                      className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
                      onClick={() => {
                        setTtsDownloading(true)
                        setTtsDlErr(null)
                        ttsDownload((p) => {
                          if (p.stage === 'download' && p.total) {
                            setTtsDlPct(Math.round(((p.received ?? 0) / p.total) * 100))
                          } else if (p.stage === 'extract') {
                            setTtsDlPct(null)
                          } else if (p.stage === 'done') {
                            setTtsModelReady(true)
                            setTtsDownloading(false)
                            useTts.getState().setReady(true) // un-hide the header toggle
                          } else if (p.stage === 'error') {
                            setTtsDownloading(false)
                            setTtsDlErr(p.detail || 'download failed')
                          }
                        })
                          .then(() => setTtsDownloading(false))
                          .catch((e) => {
                            setTtsDownloading(false)
                            setTtsDlErr(String((e as Error).message ?? e))
                          })
                      }}
                    >
                      Download voice model (~126 MB)
                    </button>
                  )}
                  {ttsDlErr && <p className="mt-1 text-[10px] text-red-400">{ttsDlErr}</p>}
                </div>
              ) : (
                <div className="space-y-1.5">
                  <div className="flex gap-1.5">
                    <select
                      className={`${settingsInputCls} w-full`}
                      value={ttsVoiceDraft}
                      onChange={(e) => setTtsVoiceDraft(e.target.value)}
                      aria-label="Read-aloud voice"
                    >
                      <optgroup label="American English — female">
                        {['af_alloy', 'af_aoede', 'af_bella', 'af_heart', 'af_jessica', 'af_kore', 'af_nicole', 'af_nova', 'af_river', 'af_sarah', 'af_sky'].map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </optgroup>
                      <optgroup label="American English — male">
                        {['am_adam', 'am_echo', 'am_eric', 'am_fenrir', 'am_liam', 'am_michael', 'am_onyx', 'am_puck', 'am_santa'].map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </optgroup>
                      <optgroup label="British English — female">
                        {['bf_alice', 'bf_emma', 'bf_isabella', 'bf_lily'].map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </optgroup>
                      <optgroup label="British English — male">
                        {['bm_daniel', 'bm_fable', 'bm_george', 'bm_lewis'].map((v) => (
                          <option key={v} value={v}>{v}</option>
                        ))}
                      </optgroup>
                    </select>
                    <button
                      type="button"
                      className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
                      title="Preview this voice"
                      aria-label="Preview voice"
                      onClick={() => previewVoice(ttsVoiceDraft, ttsSpeedDraft)}
                    >
                      ▶
                    </button>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="shrink-0 text-[10px] text-zinc-500">speed</span>
                    <input
                      type="range"
                      min="0.5"
                      max="2"
                      step="0.05"
                      value={ttsSpeedDraft}
                      onChange={(e) => setTtsSpeedDraft(Number(e.target.value))}
                      className="flex-1 accent-blue-500"
                      aria-label="Speaking rate"
                    />
                    <span className="w-10 shrink-0 text-right font-mono text-[10px] text-zinc-400">
                      {ttsSpeedDraft.toFixed(2)}×
                    </span>
                  </div>
                  <p className="text-[10px] leading-relaxed text-zinc-600">
                    Reads each finished response aloud (prose only — code is skipped). Toggle
                    anytime with the speaker button under the chat. The voice model stays on this
                    machine.
                  </p>
                </div>
              )}
              {ttsUiError && (
                <p className="mt-2 text-[10px] text-red-400">Read-aloud error: {ttsUiError}</p>
              )}
            </SettingsCard>

            <SettingsCard title="Remote hosting" className="col-span-2">
              <div className="space-y-1.5">
                <label className="flex items-center gap-2 text-xs text-zinc-300">
                  <input
                    type="checkbox"
                    className="accent-blue-600"
                    checked={remoteHost}
                    onChange={(e) => setRemoteHost(e.target.checked)}
                  />
                  Let other YAAH instances on this network use this machine
                </label>
                <div className="flex gap-1.5">
                  <input
                    className={`${settingsInputCls} min-w-0 flex-1`}
                    type="password"
                    value={remotePass}
                    onChange={(e) => setRemotePass(e.target.value)}
                    placeholder="passphrase (required to accept remote tools)"
                    aria-label="Hosting passphrase"
                  />
                  <input
                    className={`${settingsInputCls} w-32 shrink-0`}
                    value={remoteName}
                    onChange={(e) => setRemoteName(e.target.value)}
                    placeholder="display name (optional)"
                    aria-label="Host display name"
                  />
                </div>
                <p className="text-[10px] leading-relaxed text-zinc-600">
                  Workspace tools (shell, files, git) of a connected client run on this machine's
                  home directory — set a passphrase before accepting the firewall prompt. Other
                  devices appear next to the chatbox automatically.
                </p>
              </div>
            </SettingsCard>

            <SettingsCard title="Interface" className="col-span-4">
              <div className="flex items-center justify-between gap-3" role="radiogroup" aria-label="Interface scale">
                <p className="text-[10px] text-zinc-600">
                  Zoom for the whole app — larger text at the same layout, applied live
                </p>
                <div className="flex shrink-0 gap-1.5">
                  {([1.0, 1.1, 1.25, 1.5] as const).map((s) => (
                    <button
                      key={s}
                      type="button"
                      role="radio"
                      aria-checked={uiScale === s}
                      className={`rounded border px-2.5 py-1 font-mono text-xs ${
                        uiScale === s
                          ? 'border-blue-600 bg-blue-600/15 text-zinc-100'
                          : 'border-zinc-700 text-zinc-400 hover:bg-zinc-800'
                      }`}
                      onClick={() => setUiScale(s)}
                    >
                      {s === 1.0 ? '100%' : s === 1.1 ? '110%' : s === 1.25 ? '125%' : '150%'}
                    </button>
                  ))}
                </div>
              </div>
            </SettingsCard>

            <SettingsCard title="MCP tool servers" className="col-span-4">
              <McpSection />
            </SettingsCard>
          </div>
        </div>

        {/* Pinned footer: errors and the save state never scroll away */}
        <div className="flex shrink-0 items-center justify-between gap-3 border-t border-zinc-800 px-4 py-3">
          <div className="min-w-0 flex-1">
            {err && <p className="text-xs leading-relaxed text-red-400">{err}</p>}
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              type="button"
              className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              type="button"
              className="rounded bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              disabled={saving || !loaded}
              title={loaded ? undefined : 'Settings are still loading'}
              onClick={save}
            >
              {saved ? 'Saved!' : saving ? 'Saving…' : 'Save'}
            </button>
          </div>
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

/** Compact token readout: 43,251 -> "43.3k" (sub-k values stay exact). */
function fmtTok(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`
  return String(n)
}

/** Color ramp for the context bar as the window fills. */
function ctxColor(frac: number): string {
  if (frac >= 0.9) return 'bg-red-500'
  if (frac >= 0.7) return 'bg-amber-500'
  return 'bg-emerald-500'
}

/** Access-mode dropdown for the composer toolbar: ask / plan / full, each
 *  with a one-line description. Writes through to config so the backend's
 *  next tool call is gated under the new mode (the gate re-reads config
 *  per call — live for running turns). Ask is the default; plan blocks
 *  edits; full runs unsandboxed. */
function AccessModeControl() {
  const accessMode = useAgent((s) => s.accessMode)
  const setAccessMode = useAgent((s) => s.setAccessMode)
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  const meta: Record<AccessMode, { short: string; label: string; desc: string; dot: string }> = {
    ask: {
      short: 'ask',
      label: 'Ask first',
      desc: 'Edits and shell commands prompt before running',
      dot: 'bg-amber-400',
    },
    plan: {
      short: 'plan',
      label: 'Plan',
      desc: 'Read-only — the agent plans, never edits',
      dot: 'bg-sky-400',
    },
    full: {
      short: 'full',
      label: 'Full access',
      desc: 'Tools run without confirmation',
      dot: 'bg-zinc-400',
    },
  }
  const m = meta[accessMode]
  const save = (mode: AccessMode) => {
    setAccessMode(mode)
    setOpen(false)
    updateConfig({ access_mode: mode })
      .then(() => {
        // Other windows follow live changes via this event (App.tsx).
        window.dispatchEvent(
          new CustomEvent('yaah-access-mode-changed', { detail: { mode } }),
        )
      })
      .catch(() => {
        // Revert on failure so the chip never lies about the real mode.
        setAccessMode(accessMode)
      })
  }
  // Click-outside closes the menu.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [open])
  return (
    <div ref={ref} className="relative">
      <button
        className="flex items-center gap-1.5 rounded px-2 py-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-400 hover:bg-zinc-700/50 hover:text-zinc-200"
        title="Access mode — what the agent may do without asking"
        aria-label="Access mode"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span className={`inline-block h-1.5 w-1.5 rounded-full ${m.dot}`} />
        mode: {m.short}
        <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
          <path d="M1.5 3l2.5 2.5L6.5 3" />
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-9 left-0 z-20 w-64 rounded border border-zinc-700 bg-zinc-900 py-1 shadow-lg">
          {(Object.keys(meta) as AccessMode[]).map((mode) => (
            <button
              key={mode}
              className="flex w-full items-start gap-2 px-3 py-1.5 text-left hover:bg-zinc-800/60"
              onClick={() => save(mode)}
            >
              <span className={`mt-1 inline-block h-1.5 w-1.5 shrink-0 rounded-full ${meta[mode].dot}`} />
              <span className="min-w-0">
                <span className="block text-xs text-zinc-200">{meta[mode].label}</span>
                <span className="block text-[10px] leading-snug text-zinc-500">{meta[mode].desc}</span>
              </span>
              {mode === accessMode && (
                <svg className="ml-auto mt-1 shrink-0 text-blue-400" width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M1.5 5.5l2.5 2.5 4.5-5.5" />
                </svg>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** Git cluster for the status strip: branch chip (click = checkout dropdown),
 *  +N −N uncommitted line counts, local·upstream hash pair with ↑N ↓N, and
 *  the status/commit/push/pull commands. Hidden entirely for non-repos.
 *
 *  Commands run directly against git (no agent turn, no tokens) and land in
 *  the conversation as synthetic tool rows; mutating actions are disabled
 *  while the agent is mid-turn (status stays readable), push/pull confirm
 *  inline first, commit opens a small popover with a visible file count. */
function GitChipCluster({
  info,
  streaming,
  conversationId,
  onCommandDone,
}: {
  info: GitInfo | null
  streaming: boolean
  conversationId: number | null
  onCommandDone: () => void
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [branches, setBranches] = useState<string[]>([])
  const [branchesLoaded, setBranchesLoaded] = useState(false)
  const [confirmAction, setConfirmAction] = useState<'push' | 'pull' | null>(null)
  const [commitOpen, setCommitOpen] = useState(false)
  const [commitMsg, setCommitMsg] = useState('')
  const [busyAction, setBusyAction] = useState<GitAction | null>(null)
  const [copied, setCopied] = useState<'local' | 'remote' | null>(null)
  const wrapRef = useRef<HTMLSpanElement>(null)
  const appendRawMessage = useAgent((s) => s.appendRawMessage)

  // Fresh branch list per conversation; never reuse across sessions.
  useEffect(() => {
    setBranches([])
    setBranchesLoaded(false)
    setMenuOpen(false)
  }, [conversationId])

  // Click-outside closes any open popover/menu.
  useEffect(() => {
    if (!menuOpen && !commitOpen && !confirmAction) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
        setCommitOpen(false)
        setConfirmAction(null)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menuOpen, commitOpen, confirmAction])

  if (!info) return null

  const busy = busyAction !== null
  const lockMutations = streaming || busy

  const openMenu = () => {
    setCommitOpen(false)
    setConfirmAction(null)
    setMenuOpen((o) => !o)
    if (!branchesLoaded && conversationId !== null) {
      getGitBranches(conversationId)
        .then((r) => {
          setBranches(r.branches)
          setBranchesLoaded(true)
        })
        .catch(() => {})
    }
  }

  const run = async (action: GitAction, opts?: { message?: string; branch?: string }) => {
    if (conversationId === null || busyAction !== null) return
    setBusyAction(action)
    try {
      const res = await runGitCommand(conversationId, action, opts)
      // Live trace row (the backend persists the same row for reloads — the
      // live path never refetches history, so no duplicates can form).
      const callId = `ui-${action}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`
      appendRawMessage(String(conversationId), {
        id: callId,
        role: 'tool',
        content: JSON.stringify(res),
        toolCalls: [{ id: callId, name: `git ${action}`, args: opts ?? {}, result: res }],
      })
    } catch {
      // HTTP-level failure (backend down/restarting): the banner owns that.
    } finally {
      setBusyAction(null)
      setCommitOpen(false)
      setCommitMsg('')
      setConfirmAction(null)
      onCommandDone()
    }
  }

  const copyHash = (h: string, which: 'local' | 'remote') => {
    navigator.clipboard
      ?.writeText(h)
      .then(() => {
        setCopied(which)
        window.setTimeout(() => setCopied(null), 1200)
      })
      .catch(() => {})
  }

  const pairAway = info.ahead > 0 || info.behind > 0
  const pairDiverged = info.ahead > 0 && info.behind > 0
  const pairColor = pairDiverged ? 'text-red-400' : pairAway ? 'text-amber-400' : 'text-zinc-500'
  const pairTitle =
    `local ${info.local_hash ?? '?'} · upstream ${info.upstream ?? '(none)'} ${info.remote_hash ?? '—'}` +
    (pairAway ? ` — ↑${info.ahead} ahead ↓${info.behind} behind` : ' — in sync')

  const cmdBtn =
    'rounded px-1 py-0.5 font-mono text-[10px] text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 ' +
    'disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-zinc-500'

  return (
    <span ref={wrapRef} className="relative flex min-w-0 items-center gap-2">
      {/* branch chip: dirty dot + name + chevron */}
      <button
        className="flex shrink-0 items-center gap-1 rounded border border-zinc-700 bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 hover:border-zinc-500"
        title="Current git branch — click to switch"
        aria-label="Switch git branch"
        aria-expanded={menuOpen}
        onClick={openMenu}
      >
        <span
          className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${info.dirty ? 'bg-amber-400' : 'bg-transparent'}`}
          title={info.dirty ? `${info.changed} changed file${info.changed === 1 ? '' : 's'} (${info.untracked} untracked)` : undefined}
        />
        <span className="min-w-0 max-w-[10rem] truncate">{info.branch}</span>
        <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
          <path d="M1.5 3l2.5 2.5L6.5 3" />
        </svg>
      </button>

      {/* checkout dropdown (opens upward — the strip is the floor) */}
      {menuOpen && (
        <div className="absolute bottom-full left-0 z-30 mb-1 w-56 rounded border border-zinc-700 bg-zinc-900 py-1 shadow-[0_20px_25px_-5px_rgba(0,0,0,0.1),0_8px_10px_-6px_rgba(0,0,0,0.1)]">
          <div className="px-3 pb-1 pt-1.5 font-mono text-[10px] uppercase tracking-wider text-zinc-600">
            branches
          </div>
          <div className="max-h-56 overflow-auto">
            {branches.length === 0 && (
              <div className="px-3 py-1.5 font-mono text-[11px] text-zinc-600">
                {branchesLoaded ? 'no local branches' : 'loading…'}
              </div>
            )}
            {branches.map((b) => (
              <button
                key={b}
                disabled={lockMutations}
                className={`flex w-full items-center gap-2 px-3 py-1 text-left font-mono text-[11px] hover:bg-zinc-800/60 ${
                  b === info.branch ? 'text-zinc-200' : 'text-zinc-400'
                } ${lockMutations ? 'cursor-not-allowed opacity-40' : ''}`}
                onClick={() => {
                  setMenuOpen(false)
                  if (b !== info.branch) run('checkout', { branch: b })
                }}
              >
                <span className="w-3 shrink-0 text-blue-400">{b === info.branch ? '✓' : ''}</span>
                <span className="truncate">{b}</span>
              </button>
            ))}
          </div>
          {lockMutations && (
            <div className="border-t border-zinc-800 px-3 py-1 font-mono text-[10px] text-zinc-600">
              {busy ? 'git is running…' : 'agent is working — wait for the turn to end'}
            </div>
          )}
        </div>
      )}

      {/* uncommitted line counts (tracked changes) */}
      {info.added + info.deleted > 0 && (
        <span
          className="shrink-0 font-mono text-[10px]"
          title={`${info.added} added / ${info.deleted} deleted lines (uncommitted, tracked files)`}
        >
          <span className="text-emerald-500/90">+{info.added}</span>{' '}
          <span className="text-red-400/90">−{info.deleted}</span>
        </span>
      )}

      {/* local·upstream hash pair (click a hash to copy) */}
      {info.local_hash && (
        <span className={`shrink-0 font-mono text-[10px] ${pairColor}`} title={pairTitle}>
          <button
            className="hover:text-zinc-200"
            title={copied === 'local' ? 'copied' : `copy ${info.local_hash}`}
            onClick={() => copyHash(info.local_hash!, 'local')}
          >
            {copied === 'local' ? '✓' : info.local_hash}
          </button>
          <span className="text-zinc-700">·</span>
          <button
            className="hover:text-zinc-200"
            title={copied === 'remote' ? 'copied' : info.remote_hash ? `copy ${info.remote_hash}` : 'no upstream'}
            onClick={() => info.remote_hash && copyHash(info.remote_hash, 'remote')}
          >
            {copied === 'remote' ? '✓' : info.remote_hash ?? '—'}
          </button>
          {info.ahead > 0 && <span> ↑{info.ahead}</span>}
          {info.behind > 0 && <span> ↓{info.behind}</span>}
        </span>
      )}

      {/* commands: status · commit · push · pull */}
      <span className="flex shrink-0 items-center gap-0.5">
        <button className={cmdBtn} disabled={busyAction !== null} title="git status" onClick={() => run('status')}>
          status{busyAction === 'status' && <span className="run-pulse text-amber-300"> ●</span>}
        </button>
        <button
          className={cmdBtn}
          disabled={lockMutations}
          title={streaming ? 'agent is working — wait for the turn to end' : 'stage all + commit'}
          onClick={() => {
            setConfirmAction(null)
            setCommitOpen(true)
          }}
        >
          commit{busyAction === 'commit' && <span className="run-pulse text-amber-300"> ●</span>}
        </button>
        {confirmAction === 'push' ? (
          <span className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">
            push to {info.upstream ?? `origin/${info.branch}`}?{' '}
            <button className="text-blue-400 hover:text-blue-300" title="confirm push" onClick={() => run('push')}>
              ✓
            </button>
            <button className="text-zinc-500 hover:text-zinc-300" title="cancel" onClick={() => setConfirmAction(null)}>
              ✕
            </button>
          </span>
        ) : (
          <button
            className={cmdBtn}
            disabled={lockMutations}
            title={streaming ? 'agent is working — wait for the turn to end' : info.upstream ? `push to ${info.upstream}` : 'push (sets upstream on first push)'}
            onClick={() => setConfirmAction('push')}
          >
            push{busyAction === 'push' && <span className="run-pulse text-amber-300"> ●</span>}
          </button>
        )}
        {confirmAction === 'pull' ? (
          <span className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">
            pull {info.upstream ? `from ${info.upstream}` : '(no upstream)'}?{' '}
            <button className="text-blue-400 hover:text-blue-300" title="confirm pull" onClick={() => run('pull')}>
              ✓
            </button>
            <button className="text-zinc-500 hover:text-zinc-300" title="cancel" onClick={() => setConfirmAction(null)}>
              ✕
            </button>
          </span>
        ) : (
          <button
            className={cmdBtn}
            disabled={lockMutations}
            title={streaming ? 'agent is working — wait for the turn to end' : info.upstream ? `pull from ${info.upstream}` : 'pull (no upstream set)'}
            onClick={() => setConfirmAction('pull')}
          >
            pull{busyAction === 'pull' && <span className="run-pulse text-amber-300"> ●</span>}
          </button>
        )}
      </span>

      {/* commit popover: message + visible stage-all sweep */}
      {commitOpen && (
        <div className="absolute bottom-full right-0 z-30 mb-1 w-72 rounded border border-zinc-700 bg-zinc-900 p-2 shadow-[0_20px_25px_-5px_rgba(0,0,0,0.1),0_8px_10px_-6px_rgba(0,0,0,0.1)]">
          <input
            autoFocus
            value={commitMsg}
            onChange={(e) => setCommitMsg(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') run('commit', { message: commitMsg })
              else if (e.key === 'Escape') {
                setCommitOpen(false)
                setCommitMsg('')
              }
            }}
            placeholder="commit message"
            className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-[11px] text-zinc-200 placeholder-zinc-600 focus:border-blue-500 focus:outline-none"
          />
          <div className="mt-1.5 flex items-center justify-between gap-2">
            <span className="font-mono text-[10px] text-zinc-500">
              stage all (git add -A) · {info.changed} file{info.changed === 1 ? '' : 's'}
            </span>
            <button
              className="rounded bg-blue-600 px-2 py-1 text-[11px] text-white hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
              disabled={!commitMsg.trim() || lockMutations}
              onClick={() => run('commit', { message: commitMsg })}
            >
              {busyAction === 'commit' ? 'committing…' : `commit ${info.changed} file${info.changed === 1 ? '' : 's'}`}
            </button>
          </div>
        </div>
      )}
    </span>
  )
}

/** Context-size readout: exact tokens + % + fill bar. Nothing renders until
 *  the first turn completes (the count comes from the API's usage report). */
function ContextChip({ info }: { info: { tokens: number; window: number | null; model: string | null } | undefined }) {
  if (!info) return null
  const frac = info.window ? Math.min(1, info.tokens / info.window) : null
  return (
    <span
      className="flex items-center gap-1.5 font-mono text-[10px] text-zinc-400"
      title={
        info.window
          ? `${info.tokens.toLocaleString()} / ${info.window.toLocaleString()} tokens`
          : `${info.tokens.toLocaleString()} tokens (unknown context window — set an override in Settings)`
      }
    >
      {frac !== null && (
        <span className="relative inline-block h-1 w-14 overflow-hidden rounded bg-zinc-700">
          <span
            className={`absolute inset-y-0 left-0 rounded ${ctxColor(frac)}`}
            style={{ width: `${Math.max(2, frac * 100)}%` }}
          />
        </span>
      )}
      <span>
        {fmtTok(info.tokens)}
        {info.window ? ` / ${fmtTok(info.window)}` : ''} tok
      </span>
    </span>
  )
}

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
  const pendingApproval = useAgent((s) => {
    if (s.pendingApproval === null) return null
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    // Same screen-scoping as questions: a hidden turn's approval request
    // must not render over an unrelated conversation.
    return s.pendingApproval.convKey === key ? s.pendingApproval : null
  })
  const pendingPlanApproval = useAgent((s) => {
    if (s.pendingPlanApproval === null) return null
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    return s.pendingPlanApproval.convKey === key ? s.pendingPlanApproval : null
  })
  const bottomRef = useRef<HTMLDivElement>(null)
  const streaming = status === 'thinking' || status === 'running-tool'
  // Only the in-flight assistant message shows the ephemeral ticker; every
  // finished turn collapses to the one-line trace.
  const liveId = streaming && messages.length > 0 ? messages[messages.length - 1].id : null

  // ---- session metadata: context size + git branch (status strip) ----
  const setContext = useAgent((s) => s.setContext)
  const contextInfo = useAgent((s) =>
    s.conversationId === null ? undefined : s.contextByConv[String(s.conversationId)],
  )
  const [gitInfo, setGitInfo] = useState<GitInfo | null>(null)
  useEffect(() => {
    setGitInfo(null)
    if (conversationId === null) return
    let cancelled = false
    // Exact context readout: persisted by the backend at every model call.
    getContext(conversationId)
      .then((c) => {
        if (cancelled || c.context_tokens === null || c.context_tokens <= 0) return
        setContext(conversationId, c.context_tokens, c.context_window, c.context_model)
      })
      .catch(() => {})
    // Git cluster readout: TTL-cached backend read (branch, dirty, counts,
    // hashes), re-polled every 2s while this session is on screen so
    // terminal activity reflects without any push channel.
    const tick = () => {
      getGitInfo(conversationId)
        .then((r) => {
          if (!cancelled) setGitInfo(r.info)
        })
        .catch(() => {})
    }
    tick()
    const poll = window.setInterval(tick, 2000)
    return () => {
      cancelled = true
      window.clearInterval(poll)
    }
  }, [conversationId, setContext])
  // After a UI-driven git command, refresh the readouts immediately (the
  // backend already invalidated its caches) instead of waiting for the poll.
  const refreshGitInfo = useCallback(() => {
    if (conversationId === null) return
    getGitInfo(conversationId)
      .then((r) => setGitInfo(r.info))
      .catch(() => {})
  }, [conversationId])

  // ---- read-aloud (TTS) ----
  const ttsEnabled = useTts((s) => s.enabled)
  const ttsReady = useTts((s) => s.ready)
  const ttsSpeaking = useTts((s) => s.speaking)
  const ttsError = useTts((s) => s.error)
  const speakMessage = useTts((s) => s.speakMessage)
  const speakQuestion = useTts((s) => s.speakQuestion)
  const ttsStop = useTts((s) => s.stop)
  const ttsSetEnabled = useTts((s) => s.setEnabled)
  const ttsSync = useTts((s) => s.syncFromServer)
  // Hydrate readiness + persisted enabled flag once at mount.
  useEffect(() => {
    ttsStatus()
      .then((s) => ttsSync({ available: s.available, tts_enabled: s.tts_enabled }))
      .catch(() => {})
  }, [ttsSync])
  // Turn finished → speak its prose. Fires on the streaming→idle transition
  // only (a history load or conversation switch also lands here with
  // streaming=false, but lastSpokenRef guards against re-speaking anything
  // that already played). A turn that ended in an ask_user speaks the
  // question instead — the answer text is still streaming when the card
  // appears, and the question is what the user is waiting on.
  const lastMsg = messages.length ? messages[messages.length - 1] : null
  const lastAssistantId = lastMsg && lastMsg.role === 'assistant' ? lastMsg.id : null
  const lastAssistantContent = lastMsg && lastMsg.role === 'assistant' ? lastMsg.content : ''
  const wasStreamingRef = useRef(false)
  const lastSpokenRef = useRef<string | null>(null)
  useEffect(() => {
    if (streaming) {
      wasStreamingRef.current = true
      return
    }
    if (!wasStreamingRef.current) return // idle at mount / history load: stay silent
    wasStreamingRef.current = false
    if (!ttsEnabled || !ttsReady || !lastAssistantId) return
    if (lastSpokenRef.current === lastAssistantId) return
    lastSpokenRef.current = lastAssistantId
    speakMessage(lastAssistantId, lastAssistantContent)
  }, [streaming, lastAssistantId, lastAssistantContent, ttsEnabled, ttsReady, speakMessage])
  // ask_user appears → speak the question (options stay visual).
  const spokenQuestionRef = useRef<string | null>(null)
  useEffect(() => {
    if (pendingQuestion && ttsEnabled && ttsReady) {
      if (spokenQuestionRef.current !== pendingQuestion.callId) {
        spokenQuestionRef.current = pendingQuestion.callId
        speakQuestion(pendingQuestion.callId, pendingQuestion.question)
      }
    }
  }, [pendingQuestion, ttsEnabled, ttsReady, speakQuestion])
  // Persist the toggle; the backend merges it into voice.tts_enabled.
  const toggleTts = () => {
    const next = !ttsEnabled
    ttsSetEnabled(next)
    updateConfig({ voice: { tts_enabled: next } }).catch(() => {})
  }

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
                <span className="font-mono text-indigo-300">/</span> to load a skill's instructions.
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
          {/* key: each question mounts a FRESH card. Without it React reuses
              the instance across consecutive questions and any stuck local
              state (submitting, custom text) wedges every later ask. */}
          <AskUserCard key={pendingQuestion.callId} pending={pendingQuestion} />
        </div>
      )}
      {pendingApproval && pendingApproval.convKey === (conversationId === null ? 'draft' : String(conversationId)) && (
        <div className="border-t border-amber-800/60 px-4 pb-3 pt-3">
          <ApprovalCard key={pendingApproval.callId} approval={pendingApproval} />
        </div>
      )}
      {pendingPlanApproval && pendingPlanApproval.convKey === (conversationId === null ? 'draft' : String(conversationId)) && (
        <div className="border-t border-sky-800/60 px-4 pb-3 pt-3">
          <PlanApprovalCard key={pendingPlanApproval.callId} pending={pendingPlanApproval} />
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
        {/* Session metadata: git cluster + exact context fill. */}
        <GitChipCluster
          info={gitInfo}
          streaming={streaming}
          conversationId={conversationId}
          onCommandDone={refreshGitInfo}
        />
        <ContextChip info={contextInfo} />
        {/* Access mode lives in the composer toolbar now. Plan approval is a
            live card above the composer (exit_plan), not a status-strip chip. */}
        {/* Read-aloud toggle: one click to mute/unmute the agent's voice.
            Hidden while the model isn't downloaded — Settings owns that. */}
        {ttsReady && (
          <button
            className={`ml-auto rounded border px-1.5 py-0.5 ${
              ttsError
                ? 'border-red-700 text-red-300'
                : ttsSpeaking
                  ? 'border-amber-600/70 text-amber-300'
                  : ttsEnabled
                    ? 'border-zinc-600 text-zinc-200 hover:bg-zinc-800'
                    : 'border-zinc-800 text-zinc-600 hover:text-zinc-400'
            }`}
            title={
              ttsError
                ? `Read-aloud error: ${ttsError}`
                : ttsEnabled
                  ? 'Read-aloud on — click to mute'
                  : 'Read-aloud off — click to hear responses'
            }
            aria-pressed={ttsEnabled}
            aria-label={ttsEnabled ? 'Mute read-aloud' : 'Unmute read-aloud'}
            onClick={toggleTts}
          >
            {ttsSpeaking ? (
              <span className="run-pulse inline-block text-[10px] leading-[12px]">●</span>
            ) : (
              <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
                <path d="M2 5.5h2L7 3v8L4 8.5H2z" />
                <path d="M9.5 5a3 3 0 0 1 0 4" />
              </svg>
            )}
          </button>
        )}
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

/** Remembered passphrases per host URL, and the last-connected URL for
 *  auto-reconnect on launch. localStorage only — the backend keeps the
 *  session in memory alone, so a restart reconnects from here. */
const REMOTE_PASS_KEY = 'yaah.remote.pass'
const REMOTE_LAST_KEY = 'yaah.remote.last'

function loadPassMap(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(REMOTE_PASS_KEY) ?? '{}')
  } catch {
    return {}
  }
}

function savePassMap(map: Record<string, string>) {
  try {
    localStorage.setItem(REMOTE_PASS_KEY, JSON.stringify(map))
  } catch {
    /* storage unavailable */
  }
}

/**
 * Host switcher: sits left of the chatbox. "This device" is the local
 * backend; any discovered YAAH host on the LAN can be selected instead —
 * the agent's workspace tools then execute over there (conversations and
 * provider keys stay here). Switching is locked while a turn is running.
 */
function HostSwitcher({ disabled }: { disabled: boolean }) {
  const [status, setStatus] = useState<RemoteStatus | null>(null)
  const [open, setOpen] = useState(false)
  const [hosts, setHosts] = useState<RemoteHostFound[] | null>(null)
  const [scanning, setScanning] = useState(false)
  const [askingPass, setAskingPass] = useState<RemoteHostFound | null>(null)
  const [pass, setPass] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const remember = (url: string, passphrase: string) => {
    const map = loadPassMap()
    if (passphrase) map[url] = passphrase
    else delete map[url]
    savePassMap(map)
    try {
      if (url) localStorage.setItem(REMOTE_LAST_KEY, url)
      else localStorage.removeItem(REMOTE_LAST_KEY)
    } catch {
      /* storage unavailable */
    }
  }

  const connect = async (url: string, passphrase: string) => {
    setBusy(true)
    setErr(null)
    try {
      const s = await connectRemote(url, passphrase)
      setStatus(s)
      remember(url, passphrase)
      setAskingPass(null)
      setOpen(false)
      // Scope switch: stash the local workspace so disconnect restores it,
      // and land on the host's Default workspace.
      if (s.host_id) {
        const st = useAgent.getState()
        if (!parseNsWorkspace(st.workspace)) {
          try {
            localStorage.setItem('yaah.ws.stash', st.workspace)
          } catch {
            /* storage unavailable */
          }
        }
        st.setWorkspace(nsWorkspace(s.host_id, null))
        useRemote.setState({
          scope: { connected: true, url: s.url, name: s.name, hostId: s.host_id, os: s.os },
        })
      }
    } catch (e) {
      const msg = String((e as Error).message).replace(/^\d+:\s*/, '')
      // A rejected passphrase must never be a permanent dead end: forget
      // the remembered value for this host so the next pick prompts fresh.
      if (/wrong passphrase|no passphrase set/i.test(msg)) {
        remember(url, '')
        // Re-prompt with the real host/port parsed from the URL, so the
        // form's submit rebuilds the same address.
        try {
          const u = new URL(url)
          setAskingPass({
            name: u.hostname,
            host: u.hostname,
            port: Number(u.port) || 80,
            protocol: 0,
            iid: '',
            os: '',
            auth: true,
          })
        } catch {
          setAskingPass(null)
        }
        setErr('The host rejected the remembered passphrase — enter the current one.')
      } else {
        setErr(msg)
      }
    } finally {
      setBusy(false)
    }
  }

  const goLocal = async () => {
    setBusy(true)
    try {
      await disconnectRemote()
      remember('', '')
      setStatus({ connected: false })
      setOpen(false)
      // Restore the stashed local workspace (stashed at connect time).
      let stash = ''
      try {
        stash = localStorage.getItem('yaah.ws.stash') ?? ''
        localStorage.removeItem('yaah.ws.stash')
      } catch {
        /* storage unavailable */
      }
      useAgent.getState().setWorkspace(stash)
      useRemote.setState({ scope: { connected: false } })
    } catch (e) {
      setErr(String(e))
    } finally {
      setBusy(false)
    }
  }

  const scan = () => {
    setScanning(true)
    setErr(null)
    // Hide this machine itself from the list (its beacon arrives like any
    // other host's, seen via its LAN IP) and match by instance id.
    Promise.all([discoverHosts(), localInstanceInfo()])
      .then(([r, me]) =>
        setHosts(r.hosts.filter((h) => h.iid && h.iid !== me.instance_id)),
      )
      .catch((e) => setErr(String(e)))
      .finally(() => setScanning(false))
  }

  // Restore the last host on launch; silent failure just means local mode.
  useEffect(() => {
    remoteStatus()
      .then((s) => {
        if (s.connected) {
          setStatus(s)
          useRemote.setState({
            scope: { connected: true, url: s.url, name: s.name, hostId: s.host_id, os: s.os },
          })
          return
        }
        let last = ''
        try {
          last = localStorage.getItem(REMOTE_LAST_KEY) ?? ''
        } catch {
          /* storage unavailable */
        }
        const passMap = loadPassMap()
        if (last && passMap[last] !== undefined) void connect(last, passMap[last])
      })
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const pickHost = (h: RemoteHostFound) => {
    const url = `http://${h.host}:${h.port}`
    const known = loadPassMap()[url]
    if (h.auth && known === undefined) {
      setAskingPass(h)
      setPass('')
      return
    }
    void connect(url, known ?? '')
  }

  const connected = status?.connected === true

  return (
    <div className="relative">
      <button
        title={
          connected
            ? `Connected to ${status?.name} — tools run there (click to switch)`
            : 'Running on this device (click to pick a remote host)'
        }
        aria-label="Host switcher"
        aria-expanded={open}
        disabled={disabled}
        className={`flex items-center gap-1.5 whitespace-nowrap rounded px-2 py-1.5 text-xs hover:bg-zinc-700/50 ${
          connected ? 'text-emerald-300' : 'text-zinc-300'
        } disabled:opacity-50`}
        onClick={() => {
          setOpen(!open)
          if (!open && hosts === null) scan()
        }}
      >
        <svg
          width="13"
          height="13"
          viewBox="0 0 14 14"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          aria-hidden="true"
        >
          <rect x="2" y="2.5" width="10" height="4" rx="1" />
          <rect x="2" y="8.5" width="10" height="4" rx="1" />
        </svg>
        {connected ? status?.name : 'This device'}
        <svg width="8" height="8" viewBox="0 0 8 8" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
          <path d="M1.5 3l2.5 2.5L6.5 3" />
        </svg>
      </button>
      {open && (
        <div className="absolute bottom-9 left-0 z-20 w-64 rounded border border-zinc-700 bg-zinc-900 py-1 shadow-lg">
          {askingPass ? (
            <form
              className="p-3"
              onSubmit={(e) => {
                e.preventDefault()
                if (askingPass && askingPass.port > 0) {
                  void connect(`http://${askingPass.host}:${askingPass.port}`, pass)
                }
              }}
            >
              <p className="mb-2 text-xs text-zinc-300">
                Passphrase for <span className="font-mono text-zinc-100">{askingPass.name}</span>
              </p>
              <input
                type="password"
                autoFocus
                className="mb-2 w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs"
                value={pass}
                onChange={(e) => setPass(e.target.value)}
                aria-label="Host passphrase"
              />
              <div className="flex justify-end gap-1.5">
                <button
                  type="button"
                  className="rounded border border-zinc-700 px-2 py-1 text-[10px] text-zinc-400 hover:bg-zinc-800"
                  onClick={() => setAskingPass(null)}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  disabled={busy}
                  className="rounded bg-blue-600 px-2 py-1 text-[10px] text-white hover:bg-blue-500 disabled:opacity-50"
                >
                  Connect
                </button>
              </div>
            </form>
          ) : (
            <>
              <button
                className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs ${
                  connected ? 'hover:bg-zinc-800/60' : 'bg-zinc-800/80 text-zinc-400'
                }`}
                disabled={busy}
                onClick={() => void goLocal()}
              >
                <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
                  <rect x="2" y="2.5" width="10" height="4" rx="1" />
                  <rect x="2" y="8.5" width="10" height="4" rx="1" />
                </svg>
                <span className="text-zinc-200">This device</span>
                {!connected && <span className="ml-auto text-[10px] text-zinc-500">current</span>}
              </button>
              <div className="border-t border-zinc-800" />
              {(hosts ?? []).map((h) => (
                <button
                  key={`${h.host}:${h.port}`}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-zinc-800/60"
                  disabled={busy}
                  title={`${h.host}:${h.port} · ${h.os}`}
                  onClick={() => pickHost(h)}
                >
                  {h.auth ? (
                    <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <rect x="3" y="6" width="8" height="6" rx="1" />
                      <path d="M5 6V4.5a2 2 0 0 1 4 0V6" />
                    </svg>
                  ) : (
                    <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
                      <circle cx="7" cy="7" r="5" />
                      <path d="M2 7h10M7 2c1.8 1.5 1.8 8.5 0 10M7 2c-1.8 1.5-1.8 8.5 0 10" />
                    </svg>
                  )}
                  <span className="min-w-0 flex-1 truncate text-zinc-200">{h.name}</span>
                </button>
              ))}
              {hosts !== null && hosts.length === 0 && (
                <p className="px-3 py-2 text-[11px] leading-snug text-zinc-500">
                  No hosts found on this network. Install YAAH on the other machine (hosting is on
                  by default) — first launches may need the Windows firewall prompt accepted.
                </p>
              )}
              <button
                className="block w-full border-t border-zinc-800 px-3 py-1.5 text-left text-[10px] text-zinc-500 hover:text-zinc-300"
                disabled={scanning || busy}
                onClick={scan}
              >
                {scanning ? 'Scanning…' : '↻ Scan network'}
              </button>
            </>
          )}
          {err && <p className="border-t border-red-900 px-3 py-1.5 text-[10px] text-red-300">{err}</p>}
        </div>
      )}
    </div>
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
    appendToolOutput,
    finishToolCall,
    splitAtPlanApproval,
    appendTape,
    startSubAgent,
    subAgentTextDelta,
    subAgentToolStart,
    subAgentToolResult,
    finishSubAgent,
    settleSubAgents,
    setStatus,
    setError,
    setConversationId,
    adoptDraft,
    setPendingQuestion,
    setPendingApproval,
    setPendingPlanApproval,
    setContext,
    pushLog,
    setAbortController,
    removeMessage,
  } = useAgent()
  const abortController = useAgent((s) => s.abortController)
  // Live ask_user card, for PTT question routing (mirror kept in a ref below
  // so the global-hotkey handlers never go stale).
  const pendingQuestion = useAgent((s) => {
    if (s.pendingQuestion === null) return null
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    return s.pendingQuestion.convKey === key ? s.pendingQuestion : null
  })
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

  // ---- skills (/ autocomplete + chips, $ inline invocation) ----
  const [skills, setSkills] = useState<SkillInfo[]>([])
  const [skillMenuOpen, setSkillMenuOpen] = useState(false)
  const [skillQuery, setSkillQuery] = useState('')
  const [skillIndex, setSkillIndex] = useState(0)
  // True only after the user points at a row (arrows or hover). Enter commits
  // a skill solely on this explicit selection; otherwise Enter sends the
  // literal text — typing a message that starts with "/" stays possible.
  const [skillNavigated, setSkillNavigated] = useState(false)
  const [pickedSkills, setPickedSkills] = useState<SkillInfo[]>([])
  /** Which character opened the menu: '/' adds a chip, '$' completes inline. */
  const [skillTrigger, setSkillTrigger] = useState<'/' | '$'>('/')
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
        const { text } = await transcribeAudio(blob)
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
    } catch (e) {
      const err = e as DOMException
      const reason =
        err?.name === 'NotAllowedError'
          ? 'permission denied'
          : err?.name === 'NotFoundError'
            ? 'no microphone found'
            : err?.message || 'unknown error'
      pushReject(`Microphone unavailable — ${reason}`)
      return
    }
    recorderRef.current = rec
    setVoiceState('recording')
  }

  // Auto-stop on silence (VAD): once you've spoken and stayed quiet for
  // ~1.6s, finish the recording and transcribe. Manual click still wins.
  // Never fires for push-to-talk: the hotkey release is the only stop.
  useEffect(() => {
    if (voiceState !== 'recording' || pttHeldRef.current) return
    const id = window.setInterval(() => {
      const rec = recorderRef.current
      if (!rec) return
      const m = rec.metrics()
      if (m.speechStarted && m.silenceMs >= 1600) void toggleDictation()
    }, 200)
    return () => window.clearInterval(id)
  }, [voiceState])

  // ---- push-to-talk: system-wide hotkey, hold = record, release = send ----
  // Spec (2026-09-13): global accelerator via tauri-plugin-global-shortcut;
  // release transcribes and sends as its own message (same path as Enter,
  // from the background, no focus steal). Empty/silent recordings are a
  // silent no-op. PTT takes the mic from click-dictation (which is
  // cancelled and discarded). Registration failure → warning toast, PTT
  // stays off until re-picked in Settings.
  //
  // Interrupt (2026-09-13): pressing the hotkey while a turn is running
  // cancels that turn first (same path as the Stop button), so a new
  // dictation never queues behind a long run.
  //
  // Question routing (2026-09-13): while an ask_user card is up, the PTT
  // transcript is routed to that question instead of the composer — a fuzzy
  // match on an option label answers it directly, anything else fills the
  // free-text "Something else…" box. Dictated in a different language than
  // the labels? Rejected with a hint (a match would not mean what the user
  // thinks it means).
  const [pttHotkey, setPttHotkey] = useState('') // currently registered accelerator
  const pttHeldRef = useRef(false)
  const pttBusyRef = useRef(false) // a release is still transcribing/sending
  const prevTitleRef = useRef('')
  const voiceStateRef = useRef<'idle' | 'recording' | 'transcribing'>('idle')
  // Live ask_user card (the stream handler owns the store copy; PTT reads it
  // from a ref so the hotkey handlers never go stale). `anyQuestion` is the
  // unfiltered store value: a question in a background conversation must
  // still shield its turn from the interrupt below.
  const pendingQuestionRef = useRef<PendingQuestion | null>(null)
  const anyQuestionRef = useRef<PendingQuestion | null>(null)
  const anyQuestion = useAgent((s) => s.pendingQuestion)
  useEffect(() => {
    anyQuestionRef.current = anyQuestion
  }, [anyQuestion])
  // Live approval card (same ref pattern: the stream handler owns the store
  // copy; PTT reads a ref so the hotkey handler never goes stale). Unscoped:
  // an approval in a background conversation must still shield its turn from
  // the interrupt below.
  const anyApprovalRef = useRef<PendingApproval | null>(null)
  const anyApproval = useAgent((s) => s.pendingApproval)
  useEffect(() => {
    anyApprovalRef.current = anyApproval
  }, [anyApproval])
  // Live exit_plan card (same ref pattern): a plan waiting for approval must
  // shield its turn from the PTT interrupt, and a dictated answer resolves it.
  const anyPlanRef = useRef<PendingPlanApproval | null>(null)
  const anyPlan = useAgent((s) => s.pendingPlanApproval)
  useEffect(() => {
    anyPlanRef.current = anyPlan
  }, [anyPlan])
  // Assigned after `send`/`stop` are declared below (TDZ-safe via refs).
  const sendRef = useRef<(text?: string, opts?: { interrupt?: boolean }) => Promise<void>>(
    async () => {},
  )
  const stopRef = useRef<() => void>(() => {})

  useEffect(() => {
    voiceStateRef.current = voiceState
  }, [voiceState])

  const setPttTitle = (on: boolean) => {
    if (on) {
      prevTitleRef.current = document.title
      document.title = '● Recording — release to dictate'
    } else if (prevTitleRef.current) {
      document.title = prevTitleRef.current
      prevTitleRef.current = ''
    }
  }

  const pttPress = async () => {
    if (pttBusyRef.current) return // previous release is still in flight
    if (voiceStateRef.current === 'transcribing') return
    if (status === 'thinking' || status === 'running-tool') {
      // A turn is running: stop it (Stop-button path, server + client) so
      // the dictation lands now instead of queueing behind the run — unless
      // the turn is blocked on an ask_user question, an access-mode
      // approval, or a plan waiting for approval, which the dictated
      // answer is about to resolve; cancelling would destroy the thing
      // being answered.
      if (!anyQuestionRef.current && !anyApprovalRef.current && !anyPlanRef.current) stopRef.current()
    }
    if (voiceStateRef.current === 'recording') {
      // Take the mic over from click-dictation; discard its audio.
      try {
        await recorderRef.current?.stop()
      } catch {
        /* already dead — proceed */
      }
      recorderRef.current = null
      setVoiceState('idle')
    }
    const rec = new VoiceRecorder()
    try {
      await rec.start()
    } catch (e) {
      const err = e as DOMException
      const reason =
        err?.name === 'NotAllowedError'
          ? 'permission denied'
          : err?.name === 'NotFoundError'
            ? 'no microphone found'
            : err?.message || 'unknown error'
      pushReject(`Microphone unavailable — ${reason}`)
      return
    }
    recorderRef.current = rec
    pttHeldRef.current = true
    setVoiceState('recording')
    setPttTitle(true)
  }

  const pttRelease = async () => {
    pttHeldRef.current = false
    setPttTitle(false)
    const rec = recorderRef.current
    if (voiceStateRef.current !== 'recording' || !rec) return
    // No speech at all (accidental tap): silently stop, send nothing.
    if (!rec.metrics().speechStarted) {
      recorderRef.current = null
      setVoiceState('idle')
      void rec.stop().catch(() => {})
      return
    }
    setVoiceState('transcribing')
    pttBusyRef.current = true
    try {
      const blob = await rec.stop()
      const { text, language } = await transcribeAudio(blob)
      if (!text) {
        // Empty transcript after real speech: whisper heard noise — stay quiet.
        return
      }
      const q = pendingQuestionRef.current
      if (q) {
        const picked = matchOptionLabel(text, q.options.map((o) => o.label), language)
        if (picked === false) {
          pushReject(
            `Heard "${text.trim().slice(0, 40)}" — dictated in a different language than the options; use one of the labels or Something else…`,
          )
          return
        }
        if (picked !== null) {
          window.dispatchEvent(
            new CustomEvent('yaah-answer-ask', { detail: { callId: q.callId, answer: picked } }),
          )
          return
        }
        // No option matched: the transcript IS the free-text answer.
        window.dispatchEvent(
          new CustomEvent('yaah-answer-ask', { detail: { callId: q.callId, answer: text.trim() } }),
        )
        return
      }
      // Approval routing: "approve/deny (it)" or yes/no resolves the gate;
      // anything else is a denial carrying the transcript as guidance.
      const ap = anyApprovalRef.current
      if (ap) {
        const t = text.trim().toLowerCase()
        if (/^(approve|approve it|allow|yes|ok|go ahead|confirmed)\b/.test(t)) {
          window.dispatchEvent(
            new CustomEvent('yaah-answer-ask', { detail: { callId: ap.callId, answer: 'approve' } }),
          )
        } else {
          window.dispatchEvent(
            new CustomEvent('yaah-answer-ask', { detail: { callId: ap.callId, answer: text.trim() } }),
          )
        }
        return
      }
      // Plan routing: "approve/go ahead" approves the plan; anything else is
      // a change request for the model to incorporate.
      const pl = anyPlanRef.current
      if (pl) {
        const t = text.trim().toLowerCase()
        const decision = /^(approve|approve it|approved|allow|yes|ok|go ahead|confirmed|looks good)\b/.test(t)
          ? 'approve'
          : text.trim()
        window.dispatchEvent(
          new CustomEvent('yaah-answer-plan', { detail: { callId: pl.callId, answer: decision } }),
        )
        return
      }
      void sendRef.current(text)
    } catch (e) {
      pushReject(`Dictation failed: ${(e as Error).message}`)
    } finally {
      recorderRef.current = null
      pttBusyRef.current = false
      setVoiceState('idle')
    }
  }

  // Register the configured accelerator once at startup; re-registered live
  // by Settings via the 'ptt-hotkey-changed' event. Pressed/Released are also
  // bridged onto window events so tests (and the capture UI) can drive the
  // handlers without the Tauri plugin.
  // Tauri invoke rejections are plain strings, not Errors — `(e as Error)
  // .message` would print "undefined" and hide the real (permission) cause.
  const pttErrMsg = (e: unknown) => (e instanceof Error ? e.message : String(e))

  const registeredHotkeyRef = useRef<string | null>(null)
  const pttHandlerRef = useRef<(e: { state: string }) => void>(() => {})
  pttHandlerRef.current = (event) => {
    if (event.state === 'Pressed') void pttPress()
    else if (event.state === 'Released') void pttRelease()
  }

  const applyPttHotkey = async (hk: string): Promise<void> => {
    const gss = await import('@tauri-apps/plugin-global-shortcut')
    if (registeredHotkeyRef.current) {
      const old = registeredHotkeyRef.current
      registeredHotkeyRef.current = null
      setPttHotkey('')
      await gss.unregister(old).catch(() => {})
    }
    if (!hk) return
    // Handlers are read through pttHandlerRef, so re-registration never
    // goes stale on press/release closures.
    await gss.register(hk, (e) => pttHandlerRef.current(e as { state: string }))
    registeredHotkeyRef.current = hk
    setPttHotkey(hk)
  }

  useEffect(() => {
    let disposed = false
    // Route through pttHandlerRef (reassigned every render), not the
    // mount-time closures: pttPress reads `status`, and a closure captured
    // at mount would see 'idle' forever — the interrupt would never fire.
    const press = () => pttHandlerRef.current({ state: 'Pressed' })
    const release = () => pttHandlerRef.current({ state: 'Released' })
    ;(async () => {
      try {
        const cfg = await getConfig()
        const hk = cfg.voice?.ptt_hotkey ?? ''
        if (!hk || disposed) return
        await applyPttHotkey(hk)
      } catch (e) {
        if (!disposed) {
          pushReject(
            `Push-to-talk hotkey could not be registered (${pttErrMsg(e)}) — pick another in Settings`,
          )
        }
      }
    })()
    const onHotkeyChanged = (e: Event) => {
      const hk = (e as CustomEvent<string>).detail ?? ''
      applyPttHotkey(hk)
        .then(() => {
          if (hk) pushReject(`Push-to-talk bound to ${hk}`)
        })
        .catch((err) => {
          pushReject(
            `Hotkey ${hk || '(disabled)'} could not be registered (${pttErrMsg(err)})`,
          )
        })
    }
    window.addEventListener('ptt-press', press)
    window.addEventListener('ptt-release', release)
    window.addEventListener('ptt-hotkey-changed', onHotkeyChanged)
    return () => {
      disposed = true
      window.removeEventListener('ptt-press', press)
      window.removeEventListener('ptt-release', release)
      window.removeEventListener('ptt-hotkey-changed', onHotkeyChanged)
      setPttTitle(false)
      if (registeredHotkeyRef.current) {
        void import('@tauri-apps/plugin-global-shortcut').then((m) =>
          m.unregister(registeredHotkeyRef.current!).catch(() => {}),
        )
      }
    }
    // Handlers read state through refs; register once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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

  // The menu opens when the input is exactly "/" or starts with "/" —
  // the query is whatever follows, and the list narrows as it grows. The
  // same menu serves "$": the query is the unfinished $name at the very end
  // of the input; picking one adds a chip and strips the token, same as /.
  useEffect(() => {
    if (input === '/') {
      setSkillMenuOpen(true)
      setSkillTrigger('/')
      setSkillQuery('')
      setSkillIndex(0)
      setSkillNavigated(false)
      return
    }
    if (input.startsWith('/')) {
      setSkillMenuOpen(true)
      setSkillTrigger('/')
      setSkillQuery(input.slice(1))
      setSkillIndex(0)
      setSkillNavigated(false)
      return
    }
    const dollar = /\$([A-Za-z0-9_-]*)$/.exec(input)
    if (dollar) {
      setSkillMenuOpen(true)
      setSkillTrigger('$')
      setSkillQuery(dollar[1])
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
    if (skillTrigger === '$') {
      // $ picks a chip too, stripping the raw $name from the text —
      // identical to /, just without clearing the rest of the prompt.
      setPickedSkills((p) => (p.some((x) => x.name === s.name) ? p : [...p, s]))
      setInput((prev) => prev.replace(/\$[A-Za-z0-9_-]*$/, ''))
      setSkillMenuOpen(false)
      setSkillNavigated(false)
      textareaRef.current?.focus()
      return
    }
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
   *  captured at send time — a stream never writes to "what's on screen".
   *  curId advances past an approved exit_plan (splitAtPlanApproval), so
   *  the execution half of the turn streams into its own message. */
  const handleStreamEvent = (bufKey: string, asstId: string) => {
    let curId = asstId
    // True while text is allowed to flow without an emission separator: the
    // stream starts mid-emission (first emission of a fresh message), and a
    // tool event closes the emission — the next text opens a new one (#17).
    let textSinceTool = true
    return (ev: AgentEvent) => {
    if (ev.type === 'text') {
      setStatus('thinking')
      if (ev.text) {
        const text = textSinceTool ? ev.text : '\n' + ev.text
        textSinceTool = true
        appendTextDelta(bufKey, curId, text)
      }
    } else if (ev.type === 'thinking') {
      setStatus('thinking')
      // Model reasoning flows onto the tape (UI-only; never stored).
      if (ev.text) appendTape(bufKey, oneLine(ev.text) + ' ')
    } else if (ev.type === 'tool_start') {
      setStatus('running-tool')
      textSinceTool = false
      startToolCall(bufKey, curId, ev.call_id ?? '', ev.name ?? 'tool', ev.args)
      pushLog({ kind: 'tool', name: ev.name, args: ev.args })
      // Telemetry tape: every tool event of the turn flows into one
      // per-conversation line that survives gaps and turn boundaries.
      {
        const a = (ev.args ?? {}) as Record<string, unknown>
        const head =
          typeof a.command === 'string'
            ? `${ev.name} ${a.command}`
            : `${ev.name} ${toolTarget({ id: '', name: ev.name ?? '', args: a } as ToolCall) || JSON.stringify(a).slice(0, 100)}`
        appendTape(bufKey, oneLine(`\n▸ ${head}`) + '    ')
      }
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
      if (ev.name === 'exit_plan') {
        const a = (ev.args ?? {}) as { plan?: string }
        setPendingPlanApproval({
          callId: ev.call_id ?? '',
          plan: a.plan ?? '',
          convKey: bufKey,
        })
      }
    } else if (ev.type === 'tool_progress') {
      if (ev.chunk) {
        appendToolOutput(bufKey, curId, ev.call_id ?? '', ev.chunk)
        appendTape(bufKey, oneLine(ev.chunk))
      }
    } else if (ev.type === 'tool_result') {
      finishToolCall(bufKey, curId, ev.call_id ?? '', ev.result)
      pushLog({ kind: 'tool', name: ev.name, result: ev.result })
      // Close the call's tape segment: response summary + client-measured time.
      {
        const callId = ev.call_id ?? ''
        const msg = (useAgent.getState().messagesByConv[bufKey] ?? []).find((m) => m.id === curId)
        const tc = msg?.toolCalls?.slice().reverse().find((t) => t.id === callId)
        const res = ev.result as { output?: unknown } | null
        let seg = ''
        if (typeof res?.output === 'string') seg += oneLine(res.output).slice(0, 600)
        else if (ev.result !== null && ev.result !== undefined) {
          const r = JSON.stringify(ev.result)
          if (r && r !== '{}') seg += '= ' + oneLine(r).slice(0, 200)
        }
        if (tc?.startedAt && tc?.finishedAt) seg += `  ✓ ${formatElapsed(tc.finishedAt - tc.startedAt)}`
        appendTape(bufKey, (seg ? oneLine(seg) + '    ' : ''))
      }
      if (ev.name === 'ask_user') {
        setPendingQuestion((q) => (q && q.callId === ev.call_id ? null : q))
      }
      if (ev.name === 'exit_plan') {
        setPendingPlanApproval((p) => (p && p.callId === ev.call_id ? null : p))
        // Approved -> the rest of the turn is implementation: split the live
        // message so the pre-plan emission stays its own (foldable) block.
        const nid = splitAtPlanApproval(bufKey, curId)
        if (nid) {
          curId = nid
          // Fresh block: its first emission needs no separator line.
          textSinceTool = true
        }
      }
    } else if (ev.type === 'approval_request') {
      setStatus('running-tool')
      startToolCall(bufKey, curId, ev.call_id ?? '', ev.name ?? 'tool', ev.args)
      pushLog({ kind: 'tool', name: ev.name, args: ev.args })
      setPendingApproval({
        callId: ev.call_id ?? '',
        tool: ev.name ?? 'tool',
        args: (ev.args ?? {}) as Record<string, unknown>,
        convKey: bufKey,
      })
    } else if (ev.type === 'approval_decision') {
      setPendingApproval((a) => (a && a.callId === ev.call_id ? null : a))
    } else if (ev.type === 'sub_agent_spawned') {
      setStatus('running-tool')
      startSubAgent(
        bufKey,
        curId,
        ev.call_id ?? '',
        ev.agent_id ?? 0,
        ev.agent_type ?? 'sub-agent',
        ev.prompt ?? '',
      )
      pushLog({ kind: 'tool', name: 'spawn_agent', args: { agent_type: ev.agent_type, prompt: ev.prompt } })
    } else if (ev.type === 'sub_agent_progress') {
      if (ev.text) subAgentTextDelta(bufKey, curId, ev.call_id ?? '', ev.text)
      if (ev.kind === 'tool_start') {
        subAgentToolStart(bufKey, curId, ev.call_id ?? '', ev.name ?? 'tool', ev.args)
      } else if (ev.kind === 'tool_result') {
        subAgentToolResult(bufKey, curId, ev.call_id ?? '', ev.result)
      } else if (ev.kind === 'approval_request') {
        setStatus('running-tool')
        setPendingApproval({
          // The forwarded event's call_id is the namespaced gate key
          // (spawnCallId:toolCallId) the /answer endpoint must echo.
          callId: ev.call_id ?? '',
          tool: ev.name ?? 'tool',
          args: (ev.args ?? {}) as Record<string, unknown>,
          convKey: bufKey,
        })
      } else if (ev.kind === 'approval_decision') {
        setPendingApproval((a) => (a && a.callId === ev.call_id ? null : a))
      }
    } else if (ev.type === 'sub_agent_done') {
      finishSubAgent(bufKey, curId, ev.call_id ?? '', ev.status ?? 'completed', ev.turns ?? 0)
      pushLog({ kind: 'tool', name: 'spawn_agent', result: { status: ev.status, turns: ev.turns } })
    } else if (ev.type === 'error') {
      setStatus('error')
      setError(ev.message ?? 'Unknown agent error')
      setTurnError(ev.message ?? 'Unknown agent error')
      settleSubAgents(bufKey, curId)
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      setPendingApproval((a) => (a && a.convKey === bufKey ? null : a))
      setPendingPlanApproval((p) => (p && p.convKey === bufKey ? null : p))
    } else if (ev.type === 'stopped') {
      setStatus('idle')
      appendTextDelta(bufKey, curId, '\n[stopped]')
      settleSubAgents(bufKey, curId)
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      setPendingApproval((a) => (a && a.convKey === bufKey ? null : a))
      setPendingPlanApproval((p) => (p && p.convKey === bufKey ? null : p))
    } else if (ev.type === 'done') {
      setStatus('idle')
      // A completed turn must leave no block pulsing: settle anything the
      // stream ended without a sub_agent_done for (defensive; the backend
      // always emits done events in the normal path).
      settleSubAgents(bufKey, curId)
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      setPendingApproval((a) => (a && a.convKey === bufKey ? null : a))
      setPendingPlanApproval((p) => (p && p.convKey === bufKey ? null : p))
    } else if (ev.type === 'usage') {
      // Exact context size of the turn's final model call, straight from
      // the provider's usage report. Numeric conversation ids only — the
      // draft buffer has no row yet; the open-time fetch covers it.
      const convId = Number(bufKey)
      if (Number.isInteger(convId) && convId > 0) {
        setContext(convId, ev.usage_tokens ?? 0, null, ev.model ?? null)
        // Resolve the window (override -> provider -> table) for the bar.
        getContext(convId)
          .then((c) => {
            setContext(convId, ev.usage_tokens ?? 0, c.context_window, c.context_model)
          })
          .catch(() => {})
      }
    }
    }
  }

  const send = async (pttText?: string, opts?: { interrupt?: boolean }) => {
    // Push-to-talk passes explicit text: it sends as its own message and
    // must not touch (or clear) whatever draft is sitting in the composer.
    const isPtt = pttText !== undefined
    const interrupting = isPtt && opts?.interrupt === true
    const text = (pttText ?? input).trim()
    if ((!text && (isPtt || (attachments.length === 0 && images.length === 0))) || (sending && !interrupting))
      return
    // PTT interrupt: the hotkey press already cancelled the running turn
    // (stopRef → Stop-button path). That turn's stream is still winding down
    // in its own send closure — the one that owns setSending(false) and
    // setAbortController(null) — so wait for it to release the store's
    // AbortController before touching any shared state. Bounded at 5s; the
    // abort makes the in-flight fetch throw immediately, so this is fast.
    if (interrupting) {
      for (let i = 0; i < 100; i++) {
        if (!useAgent.getState().abortController) break
        await new Promise<void>((r) => setTimeout(r, 50))
      }
    }
    setSending(true)
    setSendError(null)

    // Inline small attachments as fenced blocks; large staged files as
    // workspace path pointers the agent can read_file. PTT skips this —
    // the attachments belong to the untouched draft.
    let fullText = text
    if (!isPtt) {
      for (const a of attachments) {
        fullText += attachmentText(a)
      }
      if (images.length) {
        fullText += `\n\n[${images.length} image${images.length === 1 ? '' : 's'} attached]`
      }
    }
    const imageDataUrls = images.map((i) => i.dataUrl)
    // $name anywhere in the prompt loads the skill for this turn (unknown
    // names are literal text; the message is sent exactly as written). Menu
    // picks — / or $ — become chips; this scan is the fallback for hand-typed
    // $names that never went through the menu. All merge into one list.
    const knownSkillNames = new Set(skills.map((s) => s.name))
    const dollarNames: string[] = []
    for (const m of fullText.matchAll(/\$([A-Za-z0-9_-]+)/g)) {
      const name = m[1]
      if (
        knownSkillNames.has(name) &&
        !pickedSkills.some((s) => s.name === name) &&
        !dollarNames.includes(name)
      ) {
        dollarNames.push(name)
      }
    }
    const invokedSkills = [...pickedSkills.map((s) => s.name), ...dollarNames]
    // Captured draft: if the turn fails before the agent answers, the
    // composer gets it back — a failed send must not cost the prompt.
    const draft = { input, attachments, images, pickedSkills }

    if (!isPtt) {
      setInput('')
      setAttachments([])
      setImages([])
      setPickedSkills([])
    }
    setError(null)
    // Capture the turn's target buffer now: everything this turn writes —
    // optimistic messages, stream deltas, tool traces — goes there, even if
    // the user switches to another conversation mid-stream (Q11: free).
    // `let` because adopting a newly created conversation re-keys the
    // buffer: events before adoption target 'draft', after it the real id.
    let bufKey = conversationId === null ? 'draft' : String(conversationId)
    const userId = appendUserMessage(bufKey, fullText, imageDataUrls, invokedSkills.length ? invokedSkills : undefined)
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
        const tailId = lastAssistantId(bufKey) ?? asstId
        appendTextDelta(bufKey, tailId, '\n[stopped]')
        // Stop pressed mid-delegation: settle any still-running sub-agent
        // blocks so nothing keeps pulsing after the stream is gone.
        settleSubAgents(bufKey, tailId)
      } else {
        // The turn never started (network, bad key, server down): roll back
        // the optimistic messages and restore the draft so nothing is lost.
        removeMessage(bufKey, asstId)
        removeMessage(bufKey, userId)
        // PTT has no input to clear, so its dictated text must come back
        // too — a failed send must not cost the dictation either.
        setInput(isPtt ? (draft.input ? `${draft.input.trimEnd()} ${text}` : text) : draft.input)
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
  // Keep the PTT handlers pointed at the latest send (stale-closure shield).
  sendRef.current = send

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
        const tailId = lastAssistantId(bufKey) ?? asstId
        appendTextDelta(bufKey, tailId, '\n[stopped]')
        settleSubAgents(bufKey, tailId)
      } else {
        setStatus('error')
        setTurnError(String((e as Error).message ?? e))
        settleSubAgents(bufKey, lastAssistantId(bufKey) ?? asstId)
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
  // Keep the PTT handlers pointed at the latest stop (stale-closure shield).
  stopRef.current = stop
  // Keep the PTT handlers pointed at the latest stop (stale-closure shield).
  stopRef.current = stop

  return (
    <div
      className="p-3"
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
                  <div className="font-mono text-xs text-indigo-300">
                    {skillTrigger === '$' ? '$' : '/'}
                    {s.name}
                  </div>
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
      <div
        className={`rounded border bg-zinc-800/50 ${
          dragOver
            ? 'border-blue-500'
            : streaming
              ? 'border-amber-600/70 focus-within:border-amber-500'
              : 'border-zinc-700 focus-within:border-blue-500'
        }`}
      >
        {/* Staged content lives inside the card: everything the message is
            made of sits in one bordered container. */}
        {images.length > 0 && (
          <div className="flex flex-wrap gap-2 px-3 pt-3">
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
          <div className="flex flex-wrap gap-1 px-3 pt-3">
            {attachments.map((a, i) => (
              <span
                key={i}
                title={a.savedPath ?? a.name}
                className="flex items-center gap-1 rounded bg-zinc-800 px-2 py-0.5 font-mono text-[10px] text-zinc-300"
              >
                {a.name}
                {a.content !== undefined && <span className="text-zinc-500">· inline</span>}
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
          <div className="flex flex-wrap gap-1 px-3 pt-3">
            {pickedSkills.map((s) => (
              <span
                key={s.name}
                title={s.description || s.path}
                className="flex items-center gap-1 rounded bg-indigo-900/60 px-2 py-0.5 font-mono text-[10px] text-indigo-200"
              >
                /{s.name}
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
          <div className="space-y-1 px-3 pt-3" aria-live="polite">
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
            className="mx-3 mt-3 flex items-center justify-between gap-2 rounded border border-red-800/60 bg-red-950/40 px-2 py-1.5"
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
            className="mx-3 mt-3 flex items-center justify-between gap-2 rounded border border-red-800/60 bg-red-950/40 px-2 py-1.5"
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
        <textarea
          ref={textareaRef}
          className="block w-full resize-none bg-transparent px-3 py-2 text-sm text-zinc-100 focus:outline-none"
          rows={2}
          style={{ height: 'auto', minHeight: '3.25rem', maxHeight: '16rem' }}
          placeholder="Describe a task... (drop/paste/attach images or text files; type / to load a skill)"
          aria-label="Message the agent"
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
        {/* Unified toolbar: host + mode on the left, attach/mic/send on the
            right — one hairline-separated row inside the composer card. */}
        <div className="flex items-center gap-1 border-t border-zinc-700/70 px-1.5 py-1.5">
          <HostSwitcher disabled={streaming || sending} />
          <AccessModeControl />
          <div className="ml-auto flex items-center gap-1">
            <button
              title="Attach files"
              aria-label="Attach files"
              className="rounded p-1.5 text-zinc-400 hover:bg-zinc-700/50 hover:text-zinc-200"
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
                className={`rounded p-1.5 ${
                  voiceState === 'recording'
                    ? 'text-red-400'
                    : voiceState === 'transcribing'
                      ? 'text-amber-300'
                      : 'text-zinc-400 hover:bg-zinc-700/50 hover:text-zinc-200'
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
                className="rounded border border-red-700 px-3 py-1.5 text-sm text-red-300 hover:bg-red-950"
                onClick={stop}
              >
                Stop
              </button>
            ) : (
              <button
                className="rounded bg-blue-600 px-3 py-1.5 text-sm text-white hover:bg-blue-500 disabled:opacity-50"
                onClick={() => void send()}
                disabled={!input.trim() && attachments.length === 0 && images.length === 0}
              >
                Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
