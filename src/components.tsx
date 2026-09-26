import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import {
  listConversations,
  createConversation,
  getConversation,
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
  queueMessage,
  removeQueued,
  steerAgent,
  getFileTree,
  getFileChildren,
  previewFile,
  deleteFile,
  exportConversationMarkdown,
  deleteConversation,
  moveConversation,
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
  listAgents,
  addAgent,
  updateAgent,
  updateAgentModelEffort,
  deleteAgent,
  runAgentNow,
  getAgentTape,
  setAgentRetry,
  addAgentInstruction,
  updateAgentInstruction,
  deleteAgentInstruction,
  type ScheduledAgent,
  type AgentPolicy,
  type AgentScheduleType,
  type AgentBody,
  type AgentInstruction,
  uploadAttachment,
  transcribeStatus,
  transcribeAudio,
  ttsStatus,
  ttsDownload,
  imageUrl,
  listWorkspaces,
  listLocalWorkspaces,
  addWorkspace,
  getWorkspaceGitBranches,
  checkoutWorkspaceBranch,
  deleteWorkspace,
  discoverHosts,
  localInstanceInfo,
  listRemoteDeviceConversations,
  getRemoteDeviceMessages,
  remoteDeviceImageUrl,
  remoteMediaRef,
  addRemoteDevice,
  connectRemoteDevice,
  disconnectRemoteDevice,
  removeRemoteDevice,
  type RemoteDevice,
  type RemoteHostFound,
  type FileEntry,
  type ProviderPreset,
  type SkillInfo,
  type WorkspaceRow,
  type RemoteConversation,
  type RemoteSnapshot,
  getRemoteDeviceSnapshot,
  acquireRemoteDeviceLease,
  renewRemoteDeviceLease,
  releaseRemoteDeviceLease,
  commitRemoteDeviceSnapshot,
  syncPendingRemoteDeviceCommits,
} from './api'
import { buildMessages, lastAssistantId, tapeQuestionAction, useAgent, useError, useAgentBranch, useStatus, TOOL_OUTPUT_CAP, type AccessMode, type ChatMessage, type Toast, type PendingApproval, type PendingPlanApproval, type PendingQuestion, type ToolCall, type SubAgentRun, type SubAgentToolCall, type AgentBranchInfo } from './store'
import { useUpdateCheck } from './update'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'
import { useTts, splitSentences, liveProse, spokenLine } from './speech'
import { setSoundsEnabled } from './NotificationSounds'
import { useRemote, nsWorkspace, parseNsWorkspace } from './remoteStore'
import { diffLines, langOf, type DiffLine } from './codeview'
import { CodeBlock, AgentMarkdown } from './markdown'
import { VoiceRecorder } from './voice'
import { useStickToBottom } from './useStickToBottom'
import { classifyDrop } from './dropFiles'
import { parseModelScope } from './modelScope'
import { sortWorkspaceGroups } from './workspaceGroupOrder'

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
  if (name.startsWith('memory_')) return '⌘'
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
  if (name.startsWith('memory_')) return 'text-lime-400'
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
  const t = (pick('path', 'file_path', 'query', 'pattern', 'command', 'url', 'question',
    ...(tc.name ?? '').startsWith('memory_') ? ['name'] : []) ?? '')
    .replace(/\s+/g, ' ')
    .trim()
  return t.length > 48 ? t.slice(0, 48) + '…' : t
}

/** One compact chip: glyph + name + target, counting while the call runs. */
type TickerToolCall = Pick<ToolCall, 'id' | 'name' | 'args' | 'result' | 'startedAt' | 'finishedAt'>

function ToolChip({ tc }: { tc: TickerToolCall }) {
  const done = tc.result !== undefined
  // A refused merge-back is an error even though it's "just" a tool result:
  // the turn's work did NOT reach the main tree. Red keeps meaning failure.
  // Exception: zero_commits means the work was already merged mid-turn —
  // a benign no-op, not a failure.
  const mergeFailed =
    tc.name === 'git_merge_back' &&
    (tc.result as { merged?: unknown; zero_commits?: unknown } | undefined)?.merged === false &&
    (tc.result as { zero_commits?: unknown } | undefined)?.zero_commits !== true
  return (
    <span
      className={`inline-flex shrink-0 items-center gap-1.5 rounded px-1.5 py-0.5 font-mono text-[11px] ${
        mergeFailed
          ? 'bg-red-950/60 text-red-300'
          : done
            ? 'bg-zinc-800/70 text-zinc-400'
            : 'bg-zinc-700/60 text-zinc-200'
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
function AgentTelemetry({ tape, compact = false }: { tape: string; compact?: boolean }) {
  const visibleTape = (compact ? tape.slice(-160) : tape).replace(/[\r\n]+/g, '    ')
  const wrapRef = useRef<HTMLDivElement>(null)
  const tapeRef = useRef<HTMLSpanElement>(null)
  const [offset, setOffset] = useState(0)
  useEffect(() => {
    const w = wrapRef.current?.clientWidth ?? 0
    const t = tapeRef.current?.scrollWidth ?? 0
    setOffset(Math.min(0, w - t))
  }, [visibleTape])
  return (
    <div
      ref={wrapRef}
      data-agent-telemetry=""
      className={`overflow-hidden ${compact ? 'mt-1 rounded bg-zinc-950/50 px-1.5 py-0.5' : ''}`}
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
        {visibleTape}
      </span>
    </div>
  )
}

/** The "waiting for <provider>" readout (issue #43): renders in the in-flight
 *  assistant message while a chat call is pending — no tokens back at all,
 *  not even reasoning deltas — so a silent turn is answerable at a glance.
 *  A short grace period (5s) keeps the banner from flashing on for calls the
 *  provider answers quickly; elapsed ticks on the shared 100ms clock and
 *  turns amber past 30s ("still waiting") to distinguish a hung/slow
 *  provider from a paused run. */
const MODEL_CALL_GRACE_MS = 5_000

function ModelCallWaiting() {
  const mc = useAgent((s) => s.modelCallByConv[s.bufferKey()] ?? null)
  const now = useNow()
  if (!mc) return null
  const ms = Math.max(0, now - mc.startedAt)
  if (ms < MODEL_CALL_GRACE_MS) return null
  const slow = ms >= 30_000
  return (
    <div className={`my-1 font-mono text-[10px] ${slow ? 'text-amber-400' : 'text-zinc-500'}`}>
      waiting for {mc.provider || 'provider'}
      {mc.model ? ` · ${mc.model}` : ''} ·{' '}
      <span className="tabular-nums">{formatElapsed(ms)}</span>
      {slow ? ' · still waiting…' : ''}
    </div>
  )
}

/** Collapse a chunk of tool output to one flowing tape line: line endings
 *  become wide separators so the tape never wraps or stacks. */
function oneLine(s: string): string {
  return s.replace(/[\r\n]+/g, '    ').replace(/\t/g, '  ')
}

/** The tape segment for one UI-stream event — shared by the interactive
 *  stream handler and the agent-chat live poll (which feeds the same
 *  events from the backend's tape buffer). `elapsed` is the client-measured
 *  tool duration when known; the polled path omits it. Returns null for
 *  events that carry no tape text. */
function tapeChunkForEvent(ev: AgentEvent, elapsed?: string): string | null {
  if (ev.type === 'thinking') {
    return ev.text ? oneLine(ev.text) + ' ' : null
  }
  if (ev.type === 'tool_start') {
    const a = (ev.args ?? {}) as Record<string, unknown>
    const head =
      typeof a.command === 'string'
        ? `${ev.name} ${a.command}`
        : `${ev.name} ${toolTarget({ id: '', name: ev.name ?? '', args: a } as ToolCall) || JSON.stringify(a).slice(0, 100)}`
    return oneLine(`\n▸ ${head}`) + '    '
  }
  if (ev.type === 'tool_progress') {
    return ev.chunk ? oneLine(ev.chunk) : null
  }
  if (ev.type === 'tool_result') {
    const res = ev.result as { output?: unknown } | null
    let seg = ''
    if (typeof res?.output === 'string') seg += oneLine(res.output).slice(0, 600)
    else if (ev.result !== null && ev.result !== undefined) {
      const r = JSON.stringify(ev.result)
      if (r && r !== '{}') seg += '= ' + oneLine(r).slice(0, 200)
    }
    if (elapsed) seg += `  ✓ ${elapsed}`
    return seg ? oneLine(seg) + '    ' : ''
  }
  return null
}

/** Live, ephemeral stream of calls while the agent works. Newest chip appears
 *  at the left edge and older ones are pushed right, fading out at the right
 *  edge; the row never grows past the chat panel's width. The telemetry tape
 *  runs in a window directly below, whose right edge lines up with the
 *  newest chip's right edge — tape and chip read as one column. Renders even
 *  with zero tool calls (compaction chip / tape only): the tape must not
 *  depend on a tool call existing, or early-turn thinking has no strip. */
function ToolTicker({
  calls,
  telemetry,
  showCompaction = true,
}: {
  calls: ToolCall[]
  /** Optional per-run tape for nested agents; otherwise use the chat tape. */
  telemetry?: string
  showCompaction?: boolean
}) {
  const compaction = useAgent((s) =>
    showCompaction ? s.compactionByConv[s.bufferKey()] : undefined,
  )
  const tape = useAgent((s) => telemetry ?? s.tapeByConv[s.bufferKey()] ?? '')
  const recent = calls.slice(-12)
  const rowRef = useRef<HTMLDivElement>(null)
  const [tapeWidth, setTapeWidth] = useState<number | null>(null)
  useEffect(() => {
    const align = rowRef.current?.querySelector('[data-tape-align]')
    if (align instanceof HTMLElement) setTapeWidth(align.offsetLeft + align.offsetWidth)
  }, [calls])
  const fade =
    'linear-gradient(to right, black 72%, rgba(0,0,0,0.35) 90%, transparent 100%)'
  if (!calls.length && !compaction && !tape) return null
  return (
    <div className="my-1 w-full min-w-0">
      <div
        ref={rowRef}
        data-subagent-tool-ticker={telemetry !== undefined ? '' : undefined}
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
        {compaction && (
          <CompactionChip summarized={compaction.summarized} summary={compaction.summary} />
        )}
      </div>
      {calls.length ? (
        tapeWidth !== null && tapeWidth > 0 && (
          <div className="-mt-px" style={{ width: tapeWidth }}>
            <AgentTelemetry tape={tape} />
          </div>
        )
      ) : (
        <AgentTelemetry tape={tape} />
      )}
    </div>
  )
}

/** Live compaction notice (adr/0004): a chip in the ticker row, expandable
 *  to the continuity summary. The durable transcript divider (rendered from
 *  the persisted system row on history reload) still marks the position. */
function CompactionChip({ summarized, summary }: { summarized?: number; summary: string }) {
  const [open, setOpen] = useState(false)
  return (
    <span className="relative shrink-0">
      <button
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1.5 rounded bg-zinc-800/70 px-1.5 py-0.5 font-mono text-[11px] text-zinc-400 hover:text-zinc-200"
        title={summary}
      >
        <span className="text-zinc-500">✂</span>
        <span>
          context compacted
          {typeof summarized === 'number' && summarized > 0 ? ` (${summarized} msgs)` : ''}
        </span>
      </button>
      {open && (
        <div className="absolute left-0 top-full z-10 mt-1 max-h-72 w-96 overflow-auto whitespace-pre-wrap rounded border border-zinc-800 bg-zinc-900 p-2 font-mono text-[11px] text-zinc-400">
          {summary}
        </div>
      )}
    </span>
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
    // #50: ANY tool result that carries a stored image renders it — not just
    // view_image. The backend stores every computer-use capture (screenshot,
    // the observe-crops from mouse_move/click/drag/scroll) and webtool/MCP
    // downloads the same way ({image: rel} / {images: [rels]}), so the audit
    // trail can show what the agent actually saw. Falls through to the JSON
    // dump when the field is absent so error results keep their text.
    const resultObj = tc.result && typeof tc.result === 'object' ? (tc.result as Record<string, unknown>) : null
    if (resultObj) {
      const rel = resultObj.image
      const rels = Array.isArray(resultObj.images) ? resultObj.images.filter((v): v is string => typeof v === 'string' && !!v) : []
      const images = typeof rel === 'string' && rel ? [rel, ...rels] : rels
      if (images.length > 0) {
        return (
          <div className="flex flex-wrap gap-2">
            {images.map((imgRel, i) => (
              <img
                key={i}
                src={imageSrc(imgRel)}
                alt={`${tc.name} result`}
                title="click to open full size"
                className={`max-h-64 cursor-zoom-in rounded border border-zinc-700`}
                onClick={() => useAgent.getState().setLightboxSrc(imgRel)}
              />
            ))}
          </div>
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
  const [open, setOpen] = useState(false)
  const previewTape = run.preview ?? ''
  const previewRef = useRef<HTMLSpanElement>(null)
  const [previewOffset, setPreviewOffset] = useState(0)
  useEffect(() => {
    const tape = previewRef.current
    const viewport = tape?.parentElement
    if (!tape || !viewport) return
    const updateOffset = () => setPreviewOffset(Math.min(0, viewport.clientWidth - tape.scrollWidth))
    updateOffset()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(updateOffset)
    observer.observe(viewport)
    return () => observer.disconnect()
  }, [previewTape])
  const running = run.status === 'running'
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
    <div data-subagent-card="" className="my-1 rounded border border-zinc-800 bg-zinc-900/40">
      <button
        data-subagent-toggle=""
        aria-expanded={open}
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
      <div className="border-t border-zinc-800/80 px-3 py-1.5">
        {open ? (
          run.text ? (
            <div className="text-sm leading-relaxed text-zinc-200" data-subagent-full-text="">
              <MessageBody content={run.text} />
              {running && <span className="stream-caret" />}
            </div>
          ) : null
        ) : run.preview ? (
          <div className="overflow-hidden text-sm leading-relaxed text-zinc-200" data-subagent-preview>
            <span className="relative block overflow-hidden whitespace-pre">
              <span
                className="block whitespace-pre"
                style={{ transform: `translateX(${previewOffset}px)` }}
                data-subagent-preview-line=""
              >
                {run.preview.replace(/[\r\n]+/g, ' ')}
                {running && <span className="stream-caret" />}
              </span>
              <span ref={previewRef} className="invisible absolute left-0 top-0 whitespace-pre" aria-hidden="true">
                {run.preview.replace(/[\r\n]+/g, ' ')}
              </span>
            </span>
          </div>
        ) : null}
        {running ? (
          <SubAgentToolTicker tools={run.tools} telemetry={run.telemetry} />
        ) : (
          <>
            {run.tools.length > 0 && <SubAgentTraceLine tools={run.tools} />}
            {run.telemetry && <AgentTelemetry tape={run.telemetry} compact />}
          </>
        )}
      </div>
    </div>
  )
}

/** Live nested calls use the normal horizontal ticker; finished calls use the
 *  same expandable trace rows as the parent conversation. */
function SubAgentToolTicker({ tools, telemetry }: { tools: SubAgentToolCall[]; telemetry: string }) {
  return (
    <div className="min-w-0 border-l border-zinc-800 pl-2">
      <ToolTicker calls={tools} telemetry={telemetry} showCompaction={false} />
    </div>
  )
}

/** Finished nested tools stay behind the same expandable summary as a normal chat. */
function SubAgentTraceLine({ tools }: { tools: SubAgentToolCall[] }) {
  return <TraceLine calls={tools} />
}

/** Agent message body: full markdown rendering (see src/markdown.tsx). */
function MessageBody({ content }: { content: string }) {
  return <AgentMarkdown content={content} />
}

/** Collapsible record of a history compaction (adr/0004): marks where the
 *  summarized prefix used to be; expands to the summary itself. */
function CompactionDivider({ summarized, summary }: { summarized?: number; summary: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="my-1 border-l-2 border-zinc-700 pl-2">
      <button
        onClick={() => setOpen(!open)}
        className="font-mono text-[11px] text-zinc-500 hover:text-zinc-300"
      >
        <span className="mr-1.5">{open ? '\u25bc' : '\u25b6'}</span>
        earlier context summarized to stay within the model's window
        {typeof summarized === 'number' && summarized > 0 ? ` (${summarized} messages)` : ''}
        {' — details above this line are condensed'}
      </button>
      {open && (
        <div className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap border-l border-zinc-800 pl-2 font-mono text-[11px] text-zinc-400">
          {summary}
        </div>
      )}
    </div>
  )
}

type FileChange = { path: string; added: number; deleted: number; binary?: boolean }
type FileChangeSummary = { files: FileChange[]; added: number; deleted: number }
type GitActivityOperation = {
  sequence: number
  operation: string
  outcome: string
  source: string
  certainty?: string
  detail?: string
  branch?: string
  remote?: string
  target_branch?: string
  commit?: string
  subject?: string
}
type GitActivityLane = {
  id: string
  label: string
  branch?: string
  base_branch?: string
  branch_action?: string
  commits_ahead?: number | null
  dirty?: boolean | null
  worktree?: string
  integrated?: boolean | null
  operations: GitActivityOperation[]
}
type GitActivitySummary = {
  run_id: string
  outcome: string
  coverage?: string
  lanes: GitActivityLane[]
}

const GIT_STEP_LABELS: Record<string, string> = {
  checkout: 'Checkout / worktree',
  stage: 'Stage changes',
  commit: 'Commit',
  push: 'Push',
  pull: 'Pull',
  merge: 'Merge back',
  rebase: 'Rebase',
  reset: 'Reset',
  stash: 'Stash',
  clean: 'Clean',
  restore: 'Restore',
  remove: 'Remove',
  move: 'Move',
  'cherry-pick': 'Cherry-pick',
  revert: 'Revert',
  worktree: 'Worktree',
  branch: 'Branch',
  tag: 'Tag',
}

function gitOutcomeLabel(outcome: string) {
  switch (outcome) {
    case 'succeeded': return 'completed'
    case 'failed': return 'failed'
    case 'blocked': return 'blocked'
    case 'cancelled': return 'cancelled'
    case 'no-op': return 'no changes'
    case 'skipped': return 'skipped'
    default: return 'outcome unknown'
  }
}

let mermaidLoading: Promise<typeof import('mermaid')> | null = null

function loadMermaid() {
  if (!mermaidLoading) {
    mermaidLoading = import('mermaid').then((module) => {
      module.default.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'dark', flowchart: { htmlLabels: false } })
      return module
    })
  }
  return mermaidLoading
}

function escapeMermaid(value: string): string {
  return value.replace(/["\\[\]{}<>]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 120)
}

export function gitMermaid(summary: GitActivitySummary): string {
  const lines = ['flowchart LR', '  primary["Primary workspace"]']
  const visibleLanes = summary.lanes.filter((lane) => {
    // An explorer that only reused a parent worktree has no isolated changes
    // of its own to show. Likewise, omit a drained, clean isolated worktree
    // whose only recorded operation was the harness creating it.
    if (lane.worktree === 'shared' && lane.operations.length === 0) return false
    const hasNoUnmergedWork = lane.commits_ahead === 0 && lane.dirty === false
    const onlyWorktreeSetup = lane.operations.every(
      (op) => op.operation === 'checkout' && op.source === 'harness',
    )
    return !(hasNoUnmergedWork && onlyWorktreeSetup)
  })
  visibleLanes.forEach((lane, laneIndex) => {
    const id = `lane${laneIndex}`
    const branch = escapeMermaid(lane.branch || 'current branch')
    const base = escapeMermaid(lane.base_branch || 'primary branch')
    const start = `${id}Start`
    const startLabel = lane.branch_action === 'created'
      ? `${lane.label}: created ${branch}`
      : lane.branch_action === 'reused'
        ? `${lane.label}: reused ${branch}`
        : `${lane.label}: ${branch}`
    lines.push(`  ${start}["${escapeMermaid(startLabel)}"]`)
    if (lane.branch_action !== 'none') lines.push(`  primary -. "from ${base}" .-> ${start}`)
    let previous = start
    lane.operations.forEach((op, opIndex) => {
      const node = `${id}Op${opIndex}`
      const operation = GIT_STEP_LABELS[op.operation] || op.operation
      const detail = op.subject ? `${op.commit || ''} ${op.subject}` : op.detail || ''
      const target = op.operation === 'push'
        ? [op.remote, op.target_branch].filter(Boolean).join('/')
        : op.target_branch ? `to ${op.target_branch}` : detail
      lines.push(`  ${node}["${escapeMermaid(`${operation}: ${gitOutcomeLabel(op.outcome)}${target ? ` · ${target}` : ''}`)}"]`)
      lines.push(`  ${previous} --> ${node}`)
      previous = node
    })
    const confirmedMerge = lane.operations.find((op) => op.operation === 'merge' && op.outcome === 'succeeded')
    if (lane.integrated === true && confirmedMerge) {
      lines.push(`  ${previous} --> primary`)
    }
    const hasNoUnmergedWork = lane.commits_ahead === 0 && lane.dirty === false
    if (lane.branch_action !== 'none' && lane.integrated !== true && !hasNoUnmergedWork) {
      const end = `${id}End`
      const lifecycle = lane.worktree === 'removed' ? 'worktree removed' : lane.worktree === 'kept' ? 'worktree retained' : 'worktree status unknown'
      const count = typeof lane.commits_ahead === 'number' && lane.commits_ahead > 0 ? ` · ${lane.commits_ahead} cumulative commits ahead` : ''
      lines.push(`  ${end}["Not merged · ${escapeMermaid(lifecycle + count)}"]`)
      lines.push(`  ${previous} -.-> ${end}`)
    }
  })
  return lines.join('\n')
}

function GitActivityDiagram({ summary }: { summary: GitActivitySummary }) {
  const [svg, setSvg] = useState('')
  const [failed, setFailed] = useState(false)
  const [enlarged, setEnlarged] = useState(false)
  const id = `git-activity-${summary.run_id.replace(/[^A-Za-z0-9_-]/g, '') || 'run'}`
  const graph = gitMermaid(summary)
  useEffect(() => {
    let active = true
    void loadMermaid().then((module) => module.default.render(id, graph)).then(({ svg: rendered }) => {
      if (active) {
        setSvg(rendered)
        setFailed(false)
      }
    }).catch(() => {
      if (active) setFailed(true)
    })
    return () => { active = false }
  }, [id, graph])
  if (failed || !svg) {
    return <p className="text-[10px] text-zinc-500">Branch-flow diagram unavailable; the text timeline below contains the full summary.</p>
  }
  return (
    <button
      type="button"
      aria-label={enlarged ? 'Restore Git branch activity diagram size' : 'Enlarge Git branch activity diagram'}
      aria-pressed={enlarged}
      title={enlarged ? 'Restore diagram size' : 'Enlarge diagram'}
      onClick={() => setEnlarged((current) => !current)}
      className={enlarged
        ? 'fixed inset-[1%] z-40 cursor-zoom-out overflow-auto rounded border border-zinc-700 bg-zinc-950 p-[1.5%] text-left shadow-2xl focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500'
        : 'block w-full cursor-zoom-in overflow-x-auto rounded border border-zinc-800 bg-zinc-950 p-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500'}
    >
      <span className="sr-only">{enlarged ? 'Click to restore the diagram to its default size.' : 'Click to enlarge the diagram.'}</span>
      <span className={enlarged ? 'block min-w-[720px] [&_svg]:h-auto [&_svg]:max-w-full' : 'block min-w-[520px] [&_svg]:h-auto [&_svg]:max-w-full'} aria-hidden="true" dangerouslySetInnerHTML={{ __html: svg }} />
    </button>
  )
}

function GitActivitySummary({ summary }: { summary: GitActivitySummary }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const operations = summary.lanes.flatMap((lane) => lane.operations)
  const failures = operations.filter((op) => ['failed', 'blocked', 'cancelled'].includes(op.outcome)).length
  const label = operations.length
    ? `${operations.length} Git ${operations.length === 1 ? 'operation' : 'operations'}`
    : 'Git branch activity'
  const scopeNote = summary.lanes.some((lane) => lane.branch_action !== 'none')
    ? summary.lanes.map((lane) => lane.integrated === true
      ? 'merged into primary workspace'
      : lane.commits_ahead === 0 && lane.dirty === false
        ? 'no unmerged work'
        : `${lane.branch || 'branch'} remains separate`).join(' · ')
    : 'Git operations recorded'
  return (
    <div className="w-fit max-w-full overflow-hidden rounded-md border border-zinc-700/80 bg-zinc-900/70 font-mono text-[11px]">
      <button
        type="button"
        className="flex min-h-7 max-w-full items-center gap-2 px-2 py-1 text-left text-zinc-300 hover:bg-zinc-800/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="w-2 text-zinc-500" aria-hidden="true">{open ? '\u2304' : '\u203a'}</span>
        <span>{label}</span>
        <span className={failures ? 'text-red-400' : 'text-zinc-500'}>
          {failures ? `${failures} unsuccessful` : scopeNote}
        </span>
      </button>
      {open && (
        <div id={panelId} className="max-w-[min(80vw,760px)] space-y-3 border-t border-zinc-800 p-2">
          <GitActivityDiagram summary={summary} />
          <ol className="space-y-2" aria-label="Git operation timeline">
            {summary.lanes.map((lane) => (
              <li key={lane.id} className="space-y-1">
                <div className="text-zinc-300">{lane.label} <span className="text-zinc-500">{lane.branch && `· ${lane.branch}`}</span></div>
                {lane.branch_action !== 'none' && (
                  <div className="pl-3 text-zinc-400">
                    {lane.branch_action === 'created' ? `Created branch from ${lane.base_branch || 'the primary workspace'}` : lane.branch_action === 'reused' ? 'Reused existing branch/worktree' : 'Used the parent worktree'}
                  </div>
                )}
                <ol className="space-y-1 pl-3">
                  {lane.operations.map((op) => (
                    <li key={`${lane.id}-${op.sequence}`} className="flex flex-wrap gap-x-2 text-zinc-400">
                      <span className={op.outcome === 'succeeded' ? 'text-emerald-400' : ['failed', 'blocked'].includes(op.outcome) ? 'text-red-400' : 'text-amber-300'}>
                        {GIT_STEP_LABELS[op.operation] || op.operation}: {gitOutcomeLabel(op.outcome)}
                      </span>
                      {op.commit && <span className="text-zinc-300">{op.commit} {op.subject}</span>}
                      {op.operation === 'push' && <span>{[op.remote, op.target_branch].filter(Boolean).join('/') || op.detail}</span>}
                      {op.operation === 'merge' && op.target_branch && <span>into {op.target_branch}</span>}
                      {op.detail && op.operation !== 'push' && <span className="break-all">{op.detail}</span>}
                      {op.certainty === 'uncertain' && <span className="text-amber-300">result may be incomplete</span>}
                    </li>
                  ))}
                </ol>
                {lane.branch_action !== 'none' && !(lane.integrated !== true && lane.commits_ahead === 0 && lane.dirty === false) && (
                  <div className="pl-3 text-zinc-500">
                    {lane.integrated === true ? `Merged into ${lane.operations.find((op) => op.operation === 'merge' && op.outcome === 'succeeded')?.target_branch || 'the primary workspace'}` : `Not merged${lane.worktree === 'removed' ? ' · worktree removed' : lane.worktree === 'kept' ? ' · worktree retained' : ' · worktree state unknown'}${typeof lane.commits_ahead === 'number' ? ` · ${lane.commits_ahead} commits ahead (cumulative)` : ''}${lane.dirty ? ' · uncommitted changes remain' : ''}`}
                  </div>
                )}
              </li>
            ))}
          </ol>
          {summary.coverage && <p className="text-[10px] leading-4 text-zinc-500">Coverage: {summary.coverage}</p>}
        </div>
      )}
    </div>
  )
}

function FileChangesSummary({ summary }: { summary: FileChangeSummary }) {
  const [open, setOpen] = useState(false)
  const panelId = useId()
  const count = summary.files.length
  return (
    <div className="w-fit max-w-full overflow-hidden rounded-md border border-zinc-700/80 bg-zinc-900/70 font-mono text-[11px]">
      <button
        type="button"
        className="flex min-h-7 max-w-full items-center gap-2 px-2 py-1 text-left text-zinc-300 hover:bg-zinc-800/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="w-2 text-zinc-500" aria-hidden="true">{open ? '\u2304' : '\u203a'}</span>
        <span className="whitespace-nowrap">{count} {count === 1 ? 'file changed' : 'files changed'}</span>
        <span className="whitespace-nowrap text-emerald-400">+{summary.added}</span>
        <span className="whitespace-nowrap text-red-400">-{summary.deleted}</span>
      </button>
      {open && (
        <div id={panelId} className="border-t border-zinc-800" role="list" aria-label="Changed files">
          {summary.files.map((file) => (
            <div key={file.path} role="listitem" className="flex min-h-7 max-w-full items-center gap-2 border-b border-zinc-800/70 px-2 py-1 last:border-b-0">
              <span className="w-2 shrink-0 text-amber-400" aria-hidden="true">{'{}'}</span>
              <span className="min-w-0 flex-1 truncate text-zinc-300" title={file.path}>{file.path}</span>
              {file.binary ? (
                <span className="shrink-0 text-zinc-500">binary</span>
              ) : (
                <>
                  <span className="shrink-0 text-emerald-400">+{file.added}</span>
                  <span className="shrink-0 text-red-400">-{file.deleted}</span>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

export function MessageView({ msg, live }: { msg: ChatMessage; live?: boolean }) {
  // Persisted failure markers (backend writes role='system' when a turn
  // dies): a slim machine line, not a fake agent message. EXCEPTION — the
  // end-of-turn merge-back handshake (#58 decision 5) persists the same way
  // and must NOT read as an error: a successful merge renders as the same
  // neutral git_merge_back pill the live stream showed; red stays reserved
  // for actual failures (the refusal's reason lives in the expandable
  // detail, exactly like every other tool result).
    if (msg.role === 'system') {
    if (!msg.content) return null
    let merge: { worktree_merge?: Record<string, unknown> } | null = null
    let fileChanges: FileChangeSummary | undefined
    let gitActivity: GitActivitySummary | undefined
    let status: {
      worktree_status?: {
        branch?: string
        base_branch?: string
        worktree_id?: string
        commits?: number
        dirty?: boolean
        worktree?: string
      }
    } | null = null
    try {
      const parsed: unknown = JSON.parse(msg.content)
      if (parsed && typeof parsed === 'object' && 'worktree_merge' in (parsed as object)) {
        merge = parsed as { worktree_merge: Record<string, unknown> }
      } else if (
        parsed &&
        typeof parsed === 'object' &&
        'file_changes' in parsed &&
        parsed.file_changes &&
        typeof parsed.file_changes === 'object'
      ) {
        fileChanges = parsed.file_changes as FileChangeSummary
      } else if (
        parsed &&
        typeof parsed === 'object' &&
        'git_activity' in parsed &&
        parsed.git_activity &&
        typeof parsed.git_activity === 'object'
      ) {
        gitActivity = parsed.git_activity as GitActivitySummary
      } else if (
        parsed &&
        typeof parsed === 'object' &&
        'worktree_status' in (parsed as object)
      ) {
        status = parsed as {
          worktree_status: {
            branch?: string
            base_branch?: string
            worktree_id?: string
            commits?: number
            dirty?: boolean
            worktree?: string
          }
        }
      }
    } catch {
      // not JSON — a genuine failure marker
    }
    if (fileChanges || gitActivity) {
      return (
        <div className="space-y-1 pl-3">
          {fileChanges && <FileChangesSummary summary={fileChanges} />}
          {gitActivity && <GitActivitySummary summary={gitActivity} />}
        </div>
      )
    }
    if (status) {
      // Turn end itself never integrates work. If commits remain, say plainly
      // that the main workspace is still unchanged; the existing merge action
      // is recovery UI, not a request for users to manage branches routinely.
      const s = status.worktree_status ?? {}
      const count = s.commits ?? 0
      const bits: string[] = [
        count > 0
          ? `${count} committed change(s) are not yet integrated into the main workspace`
          : 'No committed changes are waiting to be integrated',
      ]
      if (count > 0 && s.base_branch) bits.push(`target branch ${s.base_branch}`)
      if (s.dirty) bits.push('additional uncommitted changes are not included')
      return (
        <div className="pl-3">
          <div className="font-mono text-[11px] text-amber-300/90">◆ {bits.join(', ')}</div>
        </div>
      )
    }
    if (merge) {
      const r = merge.worktree_merge ?? {}
      const tc: ToolCall = {
        id: `merge-back-${msg.id}`,
        name: 'git_merge_back',
        result: r,
      }
      return (
        <div className="pl-3">
          <ToolCallRow tc={tc} />
        </div>
      )
    }
    // Invoked skill the backend registry doesn't know (chip rendered from the
    // UI list which can drift from the backend cache): say so plainly.
    try {
      const parsed: unknown = JSON.parse(msg.content)
      if (parsed && typeof parsed === 'object' && 'skill_not_found' in (parsed as object)) {
        const names = ((parsed as { skill_not_found: { skills?: string[] } }).skill_not_found.skills) ?? []
        return (
          <div className="pl-3">
            <div className="font-mono text-[11px] text-amber-300/90">
              ◆ Skill not found{names.length > 1 ? 's' : ''}: {names.join(', ')} — the skill was
              not loaded this turn. Refresh skills in Settings if it should exist.
            </div>
          </div>
        )
      }
    } catch {
      // not JSON — fall through
    }
    // Legacy history compaction marker (adr/0004): old versions stored a
    // summary as a system row after deleting the summarized transcript.
    try {
      const parsed: unknown = JSON.parse(msg.content)
      if (parsed && typeof parsed === 'object' && 'compaction' in (parsed as object)) {
        const c = (parsed as { compaction: { summarized_messages?: number; summary: string } }).compaction
        return <CompactionDivider summarized={c.summarized_messages} summary={c.summary} />
      }
    } catch {
      // not JSON — fall through to the failure-marker render
    }
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
        <div
          className={`max-w-[85%] rounded border px-3 py-2 text-sm ${
            msg.queued
              ? 'border-dashed border-amber-600/70 bg-amber-950/20 text-amber-100'
              : 'border-zinc-700/70 bg-zinc-800/60 text-zinc-100'
          }`}
        >
          {msg.queued && (
            <div className="mb-1 text-right font-mono text-[10px] uppercase tracking-widest text-amber-500">
              queued · lands next boundary
            </div>
          )}
          {msg.images?.length ? (
            <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
              {msg.images.map((rel, i) => (
                <img
                  key={i}
                  src={imageSrc(rel)}
                  alt="attachment"
                  title="click to open full size"
                  className="max-h-40 cursor-zoom-in rounded border border-zinc-700"
                  onClick={() => useAgent.getState().setLightboxSrc(rel)}
                />
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
  const inlineSubAgents = (msg.toolCalls ?? []).filter(
    (call) => call.name === 'spawn_agent' && call.subAgent,
  )

  const body = (
    <>
      {msg.content ? (
        <div className="text-sm leading-relaxed text-zinc-200">
          <MessageBody content={msg.content} />
        </div>
      ) : null}
      {!inlineSubAgents.length && live ? (
        <ToolTicker calls={msg.toolCalls ?? []} />
      ) : !inlineSubAgents.length && msg.toolCalls?.length ? (
        <TraceLine calls={msg.toolCalls} />
      ) : null}
      {!msg.content && !msg.toolCalls?.length && (
        <span className="run-pulse font-mono text-sm text-zinc-500">▊</span>
      )}
      {/* Issue #43: live "waiting for <provider> · <elapsed>s" while the
          chat call is pending (the dots above stop being the whole story). */}
      {live && <ModelCallWaiting />}
    </>
  )

  if (planApproved) {
    return <PlanningFold msg={msg} body={body} />
  }

  // Issue #63: answered ask_user questions render as anchor cards in
  // chronological order, interleaved with the turn's emission text at the
  // call's contentOffset (where the question was asked relative to the
  // block's text). Every answered question gets its own card - a turn with
  // two questions renders two cards. Rows recorded before offsets existed
  // fall back to card(s) above the body, preserving the old top-anchor look.
  const allCalls = msg.toolCalls ?? []
  const asks = allCalls.filter((t) => t.name === 'ask_user' && t.result !== undefined)
  const ordered = [...asks].sort(
    (a, b) => (a.contentOffset ?? -1) - (b.contentOffset ?? -1),
  )
  const chronological =
    ordered.length > 0 && ordered.every((t) => (t.contentOffset ?? -1) >= 0)
  const anchorFor = (t: ToolCall) => (
    <QuestionAnchor key={t.id} tc={t} after={allCalls.slice(allCalls.indexOf(t) + 1)} />
  )
  const hasSpawnAnchors = inlineSubAgents.length > 0 && inlineSubAgents.every(
    (call) => typeof call.contentOffset === 'number',
  )
  const textAnchors: Array<{ offset: number; order: number; node: ReactNode }> = [
    ...ordered.map((call, order) => ({
      offset: call.contentOffset ?? 0,
      order,
      node: <QuestionAnchor key={`question-${call.id}`} tc={call} after={allCalls.slice(allCalls.indexOf(call) + 1)} />,
    })),
    ...inlineSubAgents.map((call, order) => ({
      offset: hasSpawnAnchors ? call.contentOffset! : msg.content.length,
      order: ordered.length + order,
      node: <SubAgentBlock key={`subagent-${call.id}`} run={call.subAgent!} />,
    })),
  ].sort((a, b) => a.offset - b.offset || a.order - b.order)
  const interleaved: ReactNode[] = []
  if (
    textAnchors.length > 0 &&
    ordered.every((call) => typeof call.contentOffset === 'number') &&
    (hasSpawnAnchors || ordered.length === 0)
  ) {
    let cursor = 0
    for (const anchor of textAnchors) {
      const cut = Math.min(Math.max(anchor.offset, cursor), msg.content.length)
      if (cut > cursor) {
        interleaved.push(
          <div key={`agent-seg-${cursor}`} data-agent-emission="" className="text-sm leading-relaxed text-zinc-200">
            <MessageBody content={msg.content.slice(cursor, cut)} />
          </div>,
        )
        cursor = cut
      }
      interleaved.push(anchor.node)
    }
    if (cursor < msg.content.length) {
      interleaved.push(
        <div key={`agent-seg-${cursor}`} data-agent-emission="" className="text-sm leading-relaxed text-zinc-200">
          <MessageBody content={msg.content.slice(cursor)} />
        </div>,
      )
    }
  }
  const segments: ReactNode[] = []
  if (chronological) {
    let cursor = 0
    for (const t of ordered) {
      const cut = Math.min(Math.max(t.contentOffset ?? 0, cursor), msg.content.length)
      if (cut > cursor) {
        segments.push(
          <div key={`qa-seg-${cursor}`} className="text-sm leading-relaxed text-zinc-200">
            <MessageBody content={msg.content.slice(cursor, cut)} />
          </div>,
        )
        cursor = cut
      }
      segments.push(anchorFor(t))
    }
    if (cursor < msg.content.length) {
      segments.push(
        <div key={`qa-seg-${cursor}`} className="text-sm leading-relaxed text-zinc-200">
          <MessageBody content={msg.content.slice(cursor)} />
        </div>,
      )
    }
  }

  return (
    <div className="border-l-2 border-zinc-700/70 pl-3">
      <div className="mb-0.5 flex items-center gap-2 select-none font-mono text-[10px] uppercase tracking-widest text-zinc-600">
        agent
        {/* Stop control on the message currently being read aloud. */}
        <MessageStopButton msgId={msg.id} />
      </div>
      {msg.implementsPlan && <PlanBanner plan={msg.implementsPlan} />}
      {textAnchors.length > 0 &&
      (hasSpawnAnchors || ordered.every((call) => typeof call.contentOffset === 'number')) ? (
        <>
          {interleaved}
          {live && (
            <ToolTicker calls={(msg.toolCalls ?? []).filter((call) => call.name !== 'spawn_agent')} />
          )}
          {!live && allCalls.some((call) => call.name !== 'spawn_agent') && (
            <TraceLine calls={allCalls.filter((call) => call.name !== 'spawn_agent')} />
          )}
          {live && <ModelCallWaiting />}
        </>
      ) : chronological ? (
        <>
          {segments}
          {allCalls.some((call) => call.name !== 'spawn_agent') ? (
            live
              ? <ToolTicker calls={allCalls.filter((call) => call.name !== 'spawn_agent')} />
              : <TraceLine calls={allCalls.filter((call) => call.name !== 'spawn_agent')} />
          ) : null}
          {live && <ModelCallWaiting />}
        </>
      ) : (
        <>
          {ordered.map((call) => (
            <QuestionAnchor key={call.id} tc={call} after={allCalls.slice(allCalls.indexOf(call) + 1)} />
          ))}
          {body}
        </>
      )}
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

/** Issue #63: persistent anchor for an answered mid-run ask_user question.
 *  The live AskUserCard above the composer vanishes the moment the answer
 *  arrives, and the Q&A then hides inside the collapsed trace — so every
 *  answered question renders as an inline card at its chronological spot
 *  (the caller slices the turn text at the call's contentOffset): the
 *  question + the chosen answer (AskUserTrace), and a one-line summary of
 *  what the agent did next (the calls after it in the same turn).
 *  Open by default, collapsible; works live and after reload. */
function QuestionAnchor({ tc, after }: { tc: ToolCall; after: ToolCall[] }) {
  const [open, setOpen] = useState(true)
  const byName = new Map<string, number>()
  for (const t of after) byName.set(t.name, (byName.get(t.name) ?? 0) + 1)
  const result = (tc.result ?? {}) as { answer?: string | null }
  const answer =
    typeof result.answer === 'string' && result.answer ? result.answer : null
  return (
    <div className="mb-2 rounded border border-orange-800/60 bg-orange-950/20">
      <button
        className="flex w-full items-center gap-2 px-2.5 py-1 text-left font-mono text-[10px] uppercase tracking-widest text-orange-400"
        onClick={() => setOpen((o) => !o)}
      >
        <span className="text-orange-600">{open ? '▼' : '▸'}</span>
        <span>question · answered</span>
        {answer && (
          <span className="truncate tracking-normal normal-case text-zinc-400">
            {answer}
          </span>
        )}
        <span className="ml-auto shrink-0 tracking-normal text-zinc-600 normal-case">
          {open ? 'hide' : 'show'}
        </span>
      </button>
      {open && (
        <div className="border-t border-orange-800/40 px-2.5 py-2">
          <AskUserTrace tc={tc} />
          {after.length > 0 && (
            <p className="mt-1.5 truncate font-mono text-[11px] text-zinc-500">
              <span className="mr-1.5 text-zinc-600">→ did next:</span>
              {[...byName].map(([n, c], i) => (
                <span key={n}>
                  {i > 0 && <span className="text-zinc-700"> · </span>}
                  <span className={toolGlyphColor(n)}>{toolGlyph(n)}</span> {n}
                  {c > 1 ? ` ×${c}` : ''}
                </span>
              ))}
            </p>
          )}
        </div>
      )}
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
  const { workspace, previewPath, setPreviewPath } = useAgent()
  const status = useStatus()
  const scope = useRemote((s) => s.scope)
  const [tree, setTree] = useState<FileEntry[]>([])
  const [menu, setMenu] = useState<{ entry: FileEntry; x: number; y: number } | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('filesPanelCollapsed') === '1')
  const [deleteTarget, setDeleteTarget] = useState<FileEntry | null>(null)
  const cacheKey = `${parseNsWorkspace(workspace)?.hostId ?? 'local'}|${workspace}`

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

// ---------------------------------------------------------------- image viewer (#50)

/** Zoom factors cycled by the +/- buttons (1x = fit). */
const LIGHTBOX_ZOOMS = [1, 2, 4]

/** Clamp a pan offset to ±max so a zoomed image can't be dragged clean out
 *  of view (max 0 = no overflow on that axis = no panning there). */
const clampPan = (v: number, max: number) => Math.min(max, Math.max(-max, v))

/**
 * Full-resolution image viewer (#50): a lightbox that opens IN-APP instead of
 * the previous external-browser hop. Two entry points feed it via the global
 * `lightboxSrc` store field:
 *  - chat attachment thumbnails (data: URL or stored rel path)
 *  - any tool-result image rendered in the audit trail (screenshot /
 *    observe-crop / view_image / fetch_image / MCP images)
 * One shared instance is mounted in App, exactly like PreviewModal.
 */
export function ImageLightbox() {
  const { lightboxSrc, setLightboxSrc } = useAgent()
  const [zoom, setZoom] = useState(0) // index into LIGHTBOX_ZOOMS; 0 = fit-to-screen
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const drag = useRef<{ sx: number; sy: number; ox: number; oy: number; maxX: number; maxY: number } | null>(null)

  // Any (re)open resets to fit — a stale zoom from a previous image must not
  // carry into the next one.
  useEffect(() => {
    setZoom(0)
    setPan({ x: 0, y: 0 })
  }, [lightboxSrc])

  // Back at fit there is no overflow, so a stale pan offset from a previous
  // zoom level would visibly re-apply on the next zoom-in. Zero it here —
  // covers both the − button reaching 1x and the + cycle wrapping.
  useEffect(() => {
    if (zoom === 0) setPan({ x: 0, y: 0 })
  }, [zoom])

  // Esc closes, matching PreviewModal and the dialog shells.
  useEffect(() => {
    if (!lightboxSrc) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setLightboxSrc(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [lightboxSrc, setLightboxSrc])

  if (!lightboxSrc) return null
  const src = imageSrc(lightboxSrc)
  const zoomed = zoom > 0
  return (
    <div
      className="fixed inset-0 z-50 flex flex-col items-center justify-center overflow-hidden bg-black/90 p-6"
      onPointerDown={(e) => {
        if (e.target === e.currentTarget) setLightboxSrc(null)
        else e.stopPropagation()
      }}
    >
      <div className="absolute right-3 top-3 z-10 flex items-center gap-1 rounded border border-zinc-700 bg-zinc-900/90 px-1 py-0.5">
        <button
          title="Zoom in (at max, cycles back to fit)"
          className="rounded px-1.5 text-sm leading-6 text-zinc-300 hover:bg-zinc-700"
          onClick={() => setZoom((z) => (z + 1) % LIGHTBOX_ZOOMS.length)}
        >
          +
        </button>
        <span className="min-w-10 text-center font-mono text-[11px] text-zinc-400">
          {LIGHTBOX_ZOOMS[zoom]}x
        </span>
        <button
          title="Zoom out"
          className="rounded px-1.5 text-sm leading-6 text-zinc-300 hover:bg-zinc-700"
          onClick={() => setZoom((z) => (z > 0 ? z - 1 : z))}
        >
          −
        </button>
        <button
          title="Reset"
          className="ml-1 rounded px-1.5 font-mono text-[11px] leading-6 text-zinc-400 hover:bg-zinc-700 hover:text-zinc-200"
          onClick={() => {
            setZoom(0)
            setPan({ x: 0, y: 0 })
          }}
        >
          reset
        </button>
      </div>
      {/* The zoom viewport: a definite-size flex box (flex-1 + min-h-0 inside
          the fixed-height column) that the image overflows when zoomed. Pan
          clamping measures against THIS box, and the caption below stays put
          because the oversized image never joins the column layout. */}
      <div className="flex min-h-0 w-full flex-1 items-center justify-center">
        <img
          src={src}
          alt="full-size image"
          draggable={false}
          className={`max-h-full max-w-full select-none rounded ${zoomed ? 'max-w-none cursor-grab' : 'cursor-zoom-in'}`}
          style={
            zoomed
              ? { transform: `translate(${pan.x}px, ${pan.y}px) scale(${LIGHTBOX_ZOOMS[zoom]})` }
              : undefined
          }
          onPointerDown={(e) => {
            e.stopPropagation()
            if (!zoomed) {
              setZoom(1)
              return
            }
            // Pan range: how far the scaled image may translate before its
            // far edge meets the viewport edge (the flex centers it, so each
            // direction gets half the total overflow). translate() runs
            // POST-scale in screen pixels — its argument needs no /factor.
            const factor = LIGHTBOX_ZOOMS[zoom]
            const el = e.currentTarget
            const box = el.parentElement
            const overflow = (axis: 'clientWidth' | 'clientHeight') =>
              box ? Math.max(0, el[axis] * factor - box[axis]) / 2 : 0
            // Pointer capture keeps the drag tracking even when the cursor
            // outruns the image — no janky mid-pan tracking loss. Guarded:
            // jsdom and older webviews may not implement it.
            try {
              el.setPointerCapture(e.pointerId)
            } catch {
              /* per-element drag still works */
            }
            drag.current = {
              sx: e.clientX,
              sy: e.clientY,
              ox: pan.x,
              oy: pan.y,
              maxX: overflow('clientWidth'),
              maxY: overflow('clientHeight'),
            }
          }}
          onPointerMove={(e) => {
            const d = drag.current
            if (!d) return
            setPan({
              x: clampPan(d.ox + (e.clientX - d.sx), d.maxX),
              y: clampPan(d.oy + (e.clientY - d.sy), d.maxY),
            })
          }}
          onPointerUp={() => (drag.current = null)}
          onPointerCancel={() => (drag.current = null)}
        />
      </div>
      <p className="mt-2 shrink-0 text-center font-mono text-[10px] text-zinc-500">
        {zoomed ? 'drag to pan · esc to close' : 'click to zoom · esc to close'}
      </p>
    </div>
  )
}

// ---------------------------------------------------------------- dialogs

/** Shared modal chrome: scrim, Esc, backdrop click. All in-app dialogs
 *  build on this so Esc/backdrop behavior matches PreviewModal. */
function DialogShell({ children, onClose, panelClassName, panelRole, panelLabel }: { children: React.ReactNode; onClose: () => void; panelClassName?: string; panelRole?: 'dialog' | 'alertdialog'; panelLabel?: string }) {
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
        className={panelClassName ?? "w-full max-w-sm rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl"}
        role={panelRole}
        aria-label={panelLabel}
        aria-modal={panelRole ? true : undefined}
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

/** Move chat to another workspace (issue #8). The backend re-files the
 *  conversation row — and every future turn derives its working directory
 *  from that row — so picking a target here genuinely redirects where the
 *  chat's next messages run. Refusals (run active, messages queued) come
 *  back as 409s and surface in the failure NoticeDialog. */
function MoveChatDialog({
  convId,
  chatTitle,
  currentWorkspace,
  onDone,
  onError,
  onCancel,
}: {
  convId: number
  chatTitle: string
  currentWorkspace: string | null
  /** Called after a successful move with the destination path (null =
   *  Default), so the parent can re-adopt the open chat's workspace. */
  onDone: (target: string | null) => void
  /** 409s (run active, messages queued) and network failures land here. */
  onError: (title: string, message: string) => void
  onCancel: () => void
}) {
  const [rows, setRows] = useState<WorkspaceRow[] | null>(null)
  const [picked, setPicked] = useState<string | null>(currentWorkspace)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let alive = true
    listWorkspaces()
      .then((ws) => {
        if (alive) setRows(ws)
      })
      .catch(() => {
        if (alive) setRows([])
      })
    return () => {
      alive = false
    }
  }, [])

  const move = async () => {
    if (picked === currentWorkspace) {
      onCancel()
      return
    }
    setBusy(true)
    try {
      await moveConversation(convId, picked)
      onDone(picked)
    } catch (e) {
      onError(
        'Move failed',
        String((e as { message?: string }).message ?? e),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <DialogShell onClose={onCancel}>
      <div className="p-4">
        <h2 className="mb-1 text-sm font-semibold text-zinc-100">Move “{chatTitle}”</h2>
        <p className="mb-3 text-xs leading-relaxed text-zinc-400">
          The chat keeps its history; its next message will run inside the
          workspace you pick.
        </p>
        <div className="mb-3 max-h-56 space-y-1 overflow-y-auto">
          {rows === null && <p className="px-1 py-2 text-xs text-zinc-500">Loading…</p>}
          {rows?.map((w) => (
            <button
              key={w.id}
              disabled={!w.exists}
              title={w.exists ? w.path ?? 'No root directory' : 'Folder not found on disk'}
              className={`flex w-full items-center justify-between rounded border px-2 py-1.5 text-left text-xs ${
                picked === w.path
                  ? 'border-blue-500 bg-blue-950/40 text-zinc-100'
                  : 'border-zinc-700 bg-zinc-800/40 text-zinc-300 hover:bg-zinc-800'
              } ${!w.exists ? 'cursor-not-allowed opacity-40' : ''}`}
              onClick={() => setPicked(w.path)}
            >
              <span className="truncate">{w.label}</span>
              <span className="ml-2 shrink-0 font-mono text-[9px] text-zinc-500">
                {w.path === null ? 'no directory' : w.exists ? '' : 'missing'}
              </span>
            </button>
          ))}
          {rows !== null && rows.length === 0 && (
            <p className="px-1 py-2 text-xs text-zinc-500">No workspaces registered.</p>
          )}
        </div>
        <div className="flex justify-end gap-2">
          <button
            className="rounded border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            autoFocus
            disabled={busy}
            className="rounded bg-blue-600 px-3 py-1.5 text-xs text-white hover:bg-blue-500 disabled:opacity-50"
            onClick={() => void move()}
          >
            {busy ? 'Moving…' : 'Move'}
          </button>
        </div>
      </div>
    </DialogShell>
  )
}

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

export function RemoteTranscriptDialog({
  hostId,
  conversationId,
  title,
  online,
  deviceName,
  onClose,
}: {
  hostId: string
  conversationId: string
  title: string
  online: boolean
  deviceName: string
  onClose: () => void
}) {
  const messages = useRemoteConversations((state) => state.transcripts[remoteConversationKey(hostId, conversationId)] ?? null)
  const setTranscript = useRemoteConversations((state) => state.setTranscript)
  const [loading, setLoading] = useState(!messages)
  const [error, setError] = useState<string | null>(null)
  const [retry, setRetry] = useState(0)
  const [editing, setEditing] = useState(false)
  const [leaseToken, setLeaseToken] = useState<string | null>(null)
  const [revision, setRevision] = useState<string | null>(null)
  const [draftMessages, setDraftMessages] = useState<ChatMessage[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [syncMessage, setSyncMessage] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getRemoteDeviceMessages(hostId, conversationId)
      .then((rows) => {
        if (!cancelled) setTranscript(hostId, conversationId, buildMessages(rows).map((message) => scopeRemoteMedia(message, hostId)))
      })
      .catch((e) => {
        if (!cancelled) setError(String((e as Error).message ?? e))
      })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [hostId, conversationId, retry, setTranscript])

  useEffect(() => {
    if (!leaseToken) return
    const timer = window.setInterval(() => {
      void renewRemoteDeviceLease(hostId, conversationId, leaseToken).catch((error) => {
        setLeaseToken(null)
        setEditing(false)
        setDraftMessages(null)
        setSyncMessage(`Edit lease lost: ${String((error as Error).message ?? error)}`)
      })
    }, 45_000)
    return () => window.clearInterval(timer)
  }, [hostId, conversationId, leaseToken])

  const startEditing = async () => {
    setBusy(true)
    setSyncMessage(null)
    try {
      const snapshot: RemoteSnapshot = await getRemoteDeviceSnapshot(hostId, conversationId)
      const lease = await acquireRemoteDeviceLease(hostId, conversationId, snapshot.revision, `yaah-${Date.now()}`)
      setRevision(snapshot.revision)
      setLeaseToken(lease.lease_token)
      setDraftMessages(buildMessages(snapshot.messages).map((message) => scopeRemoteMedia(message, hostId)))
      setEditing(true)
    } catch (error) {
      setSyncMessage(`Could not acquire edit lease: ${String((error as Error).message ?? error)}`)
    } finally { setBusy(false) }
  }

  const cancelEditing = async () => {
    const token = leaseToken
    setLeaseToken(null)
    setEditing(false)
    setDraftMessages(null)
    setRevision(null)
    if (token) await releaseRemoteDeviceLease(hostId, conversationId, token).catch(() => {})
  }

  const saveEditing = async () => {
    if (!leaseToken || !revision || !draftMessages) return
    setBusy(true)
    setSyncMessage(null)
    try {
      const commitId = crypto.randomUUID()
      const snapshot = await getRemoteDeviceSnapshot(hostId, conversationId)
      if (snapshot.revision !== revision) throw new Error('stale revision; refresh before editing')
      const stored = draftMessages.map((message) => ({
        id: Number(message.id), role: message.role, content: message.content,
        tool_calls: message.toolCalls?.map((call) => ({ id: call.id, type: 'function', function: { name: call.name, arguments: JSON.stringify(call.args ?? {}) } })) ?? null,
        tool_call_id: null, images: message.images ?? [], sub_agent_transcript: message.subAgent ?? null,
      }))
      const result = await commitRemoteDeviceSnapshot(hostId, conversationId, {
        lease_token: leaseToken, revision, commit_id: commitId,
        conversation: { ...snapshot.conversation, title }, messages: stored,
      })
      setTranscript(hostId, conversationId, draftMessages)
      setRevision(result.revision)
      setSyncMessage('Changes saved to device.')
      await releaseRemoteDeviceLease(hostId, conversationId, leaseToken).catch(() => {})
      setLeaseToken(null)
      setEditing(false)
      setDraftMessages(null)
    } catch (error) {
      setSyncMessage(`Changes remain pending locally. Retry sync when the device reconnects: ${String((error as Error).message ?? error)}`)
      void syncPendingRemoteDeviceCommits(hostId, conversationId).catch(() => {})
    } finally { setBusy(false) }
  }

  const renderedMessages = draftMessages ?? messages

  return (
    <DialogShell onClose={editing ? () => void cancelEditing() : onClose} panelClassName="flex max-h-[85vh] w-full max-w-3xl flex-col rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl" panelRole="dialog" panelLabel={`Remote transcript: ${title}`}>
      <header className="flex items-start justify-between gap-4 border-b border-zinc-800 px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-zinc-100">{title}</h2>
            <p className="mt-1 font-mono text-[10px] text-zinc-500">{deviceName} <span className="px-1 text-zinc-700">·</span> {online ? 'remote transcript · read-only' : 'cached transcript · read-only offline'}</p>
          </div>
          <div className="flex shrink-0 gap-2">
            {!editing && <button disabled={!online || busy} className="rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800 disabled:opacity-40" onClick={() => void startEditing()}>Edit transcript</button>}
            {editing && <><button disabled={busy} className="rounded px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-800" onClick={() => void cancelEditing()}>Cancel edit</button><button disabled={busy} className="rounded bg-blue-600 px-2 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-40" onClick={() => void saveEditing()}>{busy ? 'Saving…' : 'Save changes'}</button></>}
            <button className="rounded px-2 py-1 text-xs text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500" onClick={() => editing ? void cancelEditing() : onClose()} aria-label="Close remote transcript">Close</button>
          </div>
        </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {error && <div role="alert" className="flex items-center gap-2 py-4 text-xs text-red-400"><span>Could not load this transcript. {online ? 'Check the device connection and retry.' : 'Reconnect to this device to refresh its cached copy.'}</span><button className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-[10px] text-zinc-300 hover:bg-zinc-800 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500" onClick={() => setRetry((value) => value + 1)}>Retry</button></div>}
          {(loading && messages === null) && !error && <div aria-label="Loading remote transcript" className="space-y-3 py-2"><div className="h-3 w-1/3 animate-pulse rounded bg-zinc-800"/><div className="h-12 w-2/3 animate-pulse rounded bg-zinc-800/70"/><div className="h-8 w-1/2 animate-pulse rounded bg-zinc-800/50"/></div>}
          {syncMessage && <p role="status" className="mb-2 text-xs text-amber-300">{syncMessage}</p>}
          {editing && draftMessages && <p className="mb-2 font-mono text-[10px] text-emerald-400">EDIT LEASE HELD · transcript-only changes; turns remain unavailable until Phase 6</p>}
          {renderedMessages?.length === 0 && <p className="py-4 text-xs text-zinc-500">This device chat has no messages yet.</p>}
          {renderedMessages && renderedMessages.length > 0 && <div className="space-y-4">{renderedMessages.map((message, index) => editing ? <label key={message.id} className="block"><span className="mb-1 block font-mono text-[10px] text-zinc-500">{message.role}</span><textarea aria-label={`Edit ${message.role} message ${index + 1}`} className="min-h-20 w-full rounded border border-zinc-700 bg-zinc-950 p-2 text-sm text-zinc-200" value={message.content} onChange={(event) => setDraftMessages((current) => current?.map((item, itemIndex) => itemIndex === index ? { ...item, content: event.target.value } : item) ?? null)} /></label> : <MessageView key={message.id} msg={message}/> )}</div>}
        </div>
      <footer className="border-t border-zinc-800 px-4 py-2 font-mono text-[10px] text-zinc-600">{editing ? 'LEASED TRANSCRIPT EDIT' : 'READ ONLY'} <span className="px-1 text-zinc-700">·</span> Remote turns and workspace execution remain Phase 6</footer>
    </DialogShell>
  )
}

/** Bind image paths in cached transcript records to their conversation owner. */
function scopeRemoteMedia<T>(value: T, hostId: string): T {
  if (Array.isArray(value)) return value.map((item) => scopeRemoteMedia(item, hostId)) as T
  if (!value || typeof value !== 'object') return value
  const entries = Object.entries(value as Record<string, unknown>).map(([key, item]) => {
    if (key === 'image' && typeof item === 'string') return [key, remoteMediaRef(hostId, item)]
    if (key === 'images' && Array.isArray(item)) {
      return [key, item.map((image) => typeof image === 'string' ? remoteMediaRef(hostId, image) : scopeRemoteMedia(image, hostId))]
    }
    return [key, scopeRemoteMedia(item, hostId)]
  })
  return Object.fromEntries(entries) as T
}

export function DeviceGroups({
  devices,
  workspaces,
  conversations,
  onChange,
  onOpenConversation,
}: {
  devices: RemoteDevice[]
  workspaces: WorkspaceRow[]
  conversations: Array<{ id: number; title: string; workspace: string | null; updated_at: string }>
  onChange: () => void
  onOpenConversation: (conversation: { id: number; workspace: string | null }) => void
}) {
  const [adding, setAdding] = useState(false)
  const [url, setUrl] = useState('')
  const [passphrase, setPassphrase] = useState('')
  const [scanning, setScanning] = useState(false)
  const [hosts, setHosts] = useState<RemoteHostFound[]>([])
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState<string | null>(null)
  const [passByDevice, setPassByDevice] = useState<Record<string, string>>({})
  const [removeConfirmId, setRemoveConfirmId] = useState<string | null>(null)
  const [disconnectConfirmId, setDisconnectConfirmId] = useState<string | null>(null)
  const [refreshingDevices, setRefreshingDevices] = useState(false)
  const [expandedDevices, setExpandedDevices] = useState<Record<string, boolean>>({})
  const [deviceChats, setDeviceChats] = useState<Record<string, RemoteConversation[]>>({})
  const [deviceChatStatus, setDeviceChatStatus] = useState<Record<string, 'online' | 'cached'>>({})
  const [loadingDeviceChats, setLoadingDeviceChats] = useState<Record<string, boolean>>({})
  const [deviceChatErrors, setDeviceChatErrors] = useState<Record<string, string | null>>({})
  const deviceChatRequestRef = useRef<Record<string, number>>({})
  const [remoteConversation, setRemoteConversation] = useState<{
    hostId: string
    conversationId: string
    title: string
    online: boolean
  } | null>(null)
  const [addingFolderFor, setAddingFolderFor] = useState<string | null>(null)

  const refreshDeviceChats = useCallback(async (hostId: string) => {
    const requestId = (deviceChatRequestRef.current[hostId] ?? 0) + 1
    deviceChatRequestRef.current[hostId] = requestId
    const isCurrent = () => deviceChatRequestRef.current[hostId] === requestId
    setLoadingDeviceChats((current) => ({ ...current, [hostId]: true }))
    setDeviceChatErrors((current) => ({ ...current, [hostId]: null }))
    try {
      const result = await listRemoteDeviceConversations(hostId)
      if (!isCurrent()) return
      setDeviceChats((current) => ({ ...current, [hostId]: result.conversations }))
      setDeviceChatStatus((current) => ({ ...current, [hostId]: result.status }))
    } catch (e) {
      if (isCurrent()) setDeviceChatErrors((current) => ({ ...current, [hostId]: String((e as Error).message ?? e) }))
    } finally {
      if (isCurrent()) setLoadingDeviceChats((current) => ({ ...current, [hostId]: false }))
    }
  }, [])

  useEffect(() => {
    const hostIds = new Set(devices.map((device) => device.host_id))
    for (const device of devices) void refreshDeviceChats(device.host_id)
    for (const hostId of Object.keys(deviceChatRequestRef.current)) {
      if (!hostIds.has(hostId)) deviceChatRequestRef.current[hostId] = (deviceChatRequestRef.current[hostId] ?? 0) + 1
    }
  }, [devices, refreshDeviceChats])
  const [folderPath, setFolderPath] = useState('')
  const askToConnect = async (device: RemoteDevice) => {
    const secret = passByDevice[device.host_id] ?? ''
    setWorking(device.host_id)
    setError(null)
    try {
      await connectRemoteDevice(device.host_id, secret)
      setPassByDevice((current) => ({ ...current, [device.host_id]: '' }))
      await useRemote.getState().refreshDevices()
      onChange()
    } catch (e) {
      setError(String((e as Error).message ?? e).replace(/^\\d+:\\s*/, ''))
    } finally {
      setWorking(null)
    }
  }
  const scan = async () => {
    setScanning(true)
    setError(null)
    try {
      const [found, me] = await Promise.all([discoverHosts(), localInstanceInfo()])
      setHosts(found.hosts.filter((host) => host.iid && host.iid !== me.instance_id))
    } catch (e) {
      setError(String((e as Error).message ?? e))
    } finally {
      setScanning(false)
    }
  }
  const add = async (deviceUrl = url, secret = passphrase) => {
    if (!deviceUrl.trim()) return
    setWorking('add')
    setError(null)
    try {
      await addRemoteDevice(deviceUrl.trim(), secret)
      setPassphrase('')
      setUrl('')
      setAdding(false)
      await useRemote.getState().refreshDevices()
      onChange()
    } catch (e) {
      setError(String((e as Error).message ?? e).replace(/^\\d+:\\s*/, ''))
    } finally {
      setWorking(null)
    }
  }
  const remove = async (device: RemoteDevice) => {
    setWorking(device.host_id)
    try {
      await removeRemoteDevice(device.host_id)
      await useRemote.getState().refreshDevices()
      onChange()
    } catch (e) {
      setError(String((e as Error).message ?? e))
    } finally {
      setWorking(null)
    }
  }
  const refreshDeviceList = async () => {
    if (refreshingDevices) return
    setRefreshingDevices(true)
    setError(null)
    const ok = await useRemote.getState().refreshDevices()
    if (ok) onChange()
    else setError('Could not refresh devices. Check the backend connection and try again.')
    setRefreshingDevices(false)
  }
  const selectDeviceWorkspace = (workspacePath: string) => {
    useAgent.getState().setWorkspace(workspacePath)
    useAgent.getState().newConversation()
    void getWorkspaceGitBranches(workspacePath).catch(() => {})
  }
  return (
    <section className="mb-2 border-b border-zinc-800 pb-2" aria-label="Remote devices">
      <div className="flex items-center justify-between px-1 py-1">
        <h2 className="font-mono text-[10px] font-semibold uppercase tracking-wider text-zinc-500">Remote devices</h2>
        <div className="flex items-center gap-1">
          <button className="rounded px-1.5 py-0.5 text-[10px] text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500 disabled:opacity-50" onClick={() => void refreshDeviceList()} disabled={refreshingDevices || working !== null} aria-label="Refresh devices" title="Refresh device status and workspace lists">
            {refreshingDevices ? 'Refreshing…' : '↻ Refresh'}
          </button>
          <button className="rounded px-1.5 py-0.5 text-[10px] text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500" onClick={() => { setAdding((value) => !value); setError(null) }} aria-expanded={adding}>+ Add</button>
        </div>
      </div>
      {adding && (
        <div className="space-y-1.5 px-1 pb-2">
          <input aria-label="Device URL" className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-[11px] text-zinc-200 placeholder:text-zinc-500 focus:border-blue-500 focus:outline-none" placeholder="http://192.168.1.10:8765" value={url} onChange={(event) => setUrl(event.target.value)} />
          <input aria-label="Device passphrase" type="password" autoComplete="new-password" className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-[11px] text-zinc-200 placeholder:text-zinc-500 focus:border-blue-500 focus:outline-none" placeholder="Passphrase (not saved)" value={passphrase} onChange={(event) => setPassphrase(event.target.value)} />
          <div className="flex items-center justify-between">
            <button className="rounded px-1 py-0.5 text-[10px] text-zinc-500 hover:text-zinc-200 disabled:opacity-50" disabled={scanning || working !== null} onClick={() => void scan()}>{scanning ? 'Scanning…' : 'Scan network'}</button>
            <div className="flex gap-1.5"><button className="rounded px-2 py-1 text-[10px] text-zinc-400 hover:bg-zinc-800" onClick={() => setAdding(false)}>Cancel</button><button className="rounded bg-blue-600 px-2 py-1 text-[10px] text-white hover:bg-blue-500 disabled:opacity-50" disabled={working !== null || !url.trim()} onClick={() => void add()}>{working === 'add' ? 'Verifying…' : 'Save device'}</button></div>
          </div>
          {hosts.length > 0 && <div className="max-h-24 overflow-auto border-t border-zinc-800 pt-1">{hosts.map((host) => <button key={`${host.host}:${host.port}`} className="block w-full truncate px-1 py-1 text-left text-[10px] text-zinc-300 hover:bg-zinc-800" onClick={() => { const discovered = `http://${host.host}:${host.port}`; setUrl(discovered); if (!host.auth) void add(discovered, '') }}>{host.name} · {host.host}:{host.port}</button>)}</div>}
          {error && <p role="alert" className="text-[10px] text-red-400">{error}</p>}
        </div>
      )}
      {devices.map((device) => {
        const deviceWorkspaces = workspaces.filter((row) => row.owner_id === device.host_id)
        const localDeviceConversations = conversations.filter((conversation) => parseNsWorkspace(conversation.workspace)?.hostId === device.host_id).sort((a, b) => b.updated_at.localeCompare(a.updated_at))
        const ownedConversations = deviceChats[device.host_id] ?? []
        const connected = device.status === 'online'
        const statusLabel = device.status === 'online' ? 'online' : device.status === 'error' ? 'connection error' : 'offline · cached'
        const expanded = expandedDevices[device.host_id] ?? true
        const addFolder = async () => {
          const raw = folderPath.trim()
          if (!raw || !connected) return
          setWorking(device.host_id)
          setError(null)
          try {
            const namespaced = nsWorkspace(device.host_id, raw)
            await addWorkspace(namespaced, device.host_id)
            setFolderPath('')
            setAddingFolderFor(null)
            await useRemote.getState().refreshDevices()
            onChange()
          } catch (e) {
            setError(String((e as Error).message ?? e))
          } finally {
            setWorking(null)
          }
        }
        return (
          <div key={device.host_id} className="group/device">
            <div className="flex items-center gap-1 rounded px-1 py-1 hover:bg-zinc-800/50">
              <button className="rounded px-1 text-[10px] text-zinc-600 hover:text-zinc-200" aria-label={`${expanded ? 'Collapse' : 'Expand'} ${device.name}`} aria-expanded={expanded} onClick={() => setExpandedDevices((current) => ({ ...current, [device.host_id]: !expanded }))}>{expanded ? '⌄' : '›'}</button>
              <button className="min-w-0 flex-1 truncate text-left text-xs text-zinc-300" title={`${device.url} · ${statusLabel}`} onClick={() => setExpandedDevices((current) => ({ ...current, [device.host_id]: true }))}>{device.name}</button>
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${connected ? 'bg-emerald-500' : device.status === 'error' ? 'bg-red-500' : 'bg-zinc-600'}`} title={statusLabel} aria-label={statusLabel} />
              <button className="rounded px-1 text-[10px] text-zinc-500 hover:bg-zinc-700 hover:text-zinc-200 disabled:opacity-50" aria-label={connected ? `Disconnect ${device.name}` : `Reconnect ${device.name}`} title={connected ? 'Disconnect device' : 'Reconnect device'} disabled={working !== null || (!connected && !(passByDevice[device.host_id] ?? ''))} onClick={() => connected ? setDisconnectConfirmId(device.host_id) : void askToConnect(device)}>{working === device.host_id ? '…' : connected ? '−' : '↻'}</button>
              {!connected && <input className="w-20 rounded border border-zinc-700 bg-zinc-800 px-1 py-0.5 font-mono text-[9px] text-zinc-300 placeholder:text-zinc-600 focus:border-blue-500 focus:outline-none" type="password" autoComplete="new-password" aria-label={`Passphrase for ${device.name}`} placeholder="passphrase" value={passByDevice[device.host_id] ?? ''} onChange={(event) => setPassByDevice((current) => ({ ...current, [device.host_id]: event.target.value }))} />}
              <button className="rounded px-1 text-[10px] text-zinc-600 opacity-0 hover:text-red-400 group-hover/device:opacity-100 focus:opacity-100" aria-label={`Remove ${device.name}`} title="Remove device profile" disabled={working !== null} onClick={() => setRemoveConfirmId(device.host_id)}>×</button>
            </div>
            {expanded && <>
              {deviceWorkspaces.map((row) => (
                <div key={row.path ?? `${device.host_id}:default`}>
                  <button className="flex w-full items-center gap-1.5 truncate rounded py-1 pl-6 pr-1 text-left font-mono text-[10px] text-zinc-500 hover:bg-zinc-800/60 hover:text-zinc-200 disabled:opacity-50" disabled={!connected} title={connected ? row.path ?? 'Device home folder' : 'Offline — cached workspace name; reconnect to use'} onClick={() => selectDeviceWorkspace(row.path ?? '')}><span className="truncate">{row.label}</span><span className="ml-auto shrink-0 text-[9px] text-zinc-600" aria-hidden="true">›</span></button>
                  {localDeviceConversations.filter((conversation) => conversation.workspace === row.path).slice(0, 3).map((conversation) => (
                    <button key={`local:${conversation.id}`} className="block w-full truncate rounded py-1 pl-10 pr-2 text-left text-[11px] text-zinc-500 hover:bg-zinc-800/60 hover:text-zinc-200" title={`${conversation.title} · locally owned chat using this device's workspace`} onClick={() => onOpenConversation(conversation)}>{conversation.title}<span className="ml-1 font-mono text-[9px] text-zinc-600">local chat</span></button>
                  ))}
                </div>
              ))}
              {!connected && deviceWorkspaces.length === 0 && <p className="px-6 py-1 text-[10px] text-zinc-600">No cached workspaces</p>}
              <div className="mt-1 border-t border-zinc-800/70 pt-1">
                <div className="flex items-center justify-between px-6 py-0.5">
                  <span className="font-mono text-[9px] uppercase tracking-wider text-zinc-600">Device chats</span>
                  <button className="rounded px-1 text-[10px] text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300 disabled:opacity-50" aria-label={`Refresh chats from ${device.name}`} title="Refresh cached transcripts from this device" disabled={loadingDeviceChats[device.host_id]} onClick={() => void refreshDeviceChats(device.host_id)}>{loadingDeviceChats[device.host_id] ? '…' : '↻'}</button>
                </div>
                {loadingDeviceChats[device.host_id] && ownedConversations.length === 0 && <p className="px-6 py-1 text-[10px] text-zinc-600">Loading device chats…</p>}
                {deviceChatErrors[device.host_id] && ownedConversations.length === 0 && <div role="alert" className="flex items-center justify-between gap-2 px-6 py-1 text-[10px] text-red-400"><span>Could not load device chats</span><button className="rounded px-1 text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200" onClick={() => void refreshDeviceChats(device.host_id)}>Retry</button></div>}
                {ownedConversations.map((conversation) => {
                  const transcriptOnline = connected && deviceChatStatus[device.host_id] === 'online'
                  return <button key={`remote:${device.host_id}:${conversation.conversation_id}`} className="block w-full truncate rounded py-1 pl-6 pr-2 text-left text-[11px] text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200" title={`${conversation.title} · ${transcriptOnline ? 'read-only remote transcript' : 'cached transcript · read-only offline'}`} onClick={() => setRemoteConversation({ hostId: device.host_id, conversationId: conversation.conversation_id, title: conversation.title, online: transcriptOnline })}>{conversation.title}<span className={`ml-1 font-mono text-[9px] ${transcriptOnline ? 'text-zinc-600' : 'text-amber-500/80'}`}>{transcriptOnline ? 'remote' : 'cached · read-only'}</span></button>
                })}
                {!loadingDeviceChats[device.host_id] && !deviceChatErrors[device.host_id] && ownedConversations.length === 0 && <p className="px-6 py-1 text-[10px] text-zinc-600">No cached device chats</p>}
              </div>
              {connected && <button className="ml-6 mt-0.5 rounded px-1 py-0.5 text-[10px] text-zinc-600 hover:bg-zinc-800 hover:text-zinc-300" onClick={() => { setAddingFolderFor(addingFolderFor === device.host_id ? null : device.host_id); setFolderPath('') }}>+ Add folder</button>}
              {addingFolderFor === device.host_id && connected && <div className="ml-6 mt-1 border-l border-zinc-800 pl-2"><input autoFocus className="w-full rounded border border-zinc-700 bg-zinc-800 px-1.5 py-1 font-mono text-[10px] text-zinc-200 focus:border-blue-500 focus:outline-none" aria-label={`Folder path on ${device.name}`} placeholder="path on device" value={folderPath} onChange={(event) => setFolderPath(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void addFolder(); if (event.key === 'Escape') setAddingFolderFor(null) }} /><button className="mt-1 rounded bg-blue-600 px-2 py-0.5 text-[10px] text-white hover:bg-blue-500 disabled:opacity-50" disabled={!folderPath.trim() || working !== null} onClick={() => void addFolder()}>{working === device.host_id ? 'Adding…' : 'Add folder'}</button></div>}
            </>}
          </div>
        )
      })}
      {devices.length === 0 && !adding && <p className="px-2 py-1 text-[10px] text-zinc-600">No saved devices. Add one to browse its workspaces here.</p>}
      {!adding && error && <p role="alert" className="px-2 py-1 text-[10px] text-red-400">{error}</p>}
      {disconnectConfirmId && (
        <ConfirmDialog title={`Disconnect ${devices.find((device) => device.host_id === disconnectConfirmId)?.name ?? 'device'}?`} body="Chats and device metadata stay here. Any open remote conversation will stop working until you reconnect with the device passphrase. Local workspaces and chats are unaffected." confirmLabel="Disconnect" onCancel={() => setDisconnectConfirmId(null)} onConfirm={() => {
          const hostId = disconnectConfirmId;
          setDisconnectConfirmId(null);
          void disconnectRemoteDevice(hostId).then(() => { void useRemote.getState().refreshDevices(); onChange() }).catch((e) => setError(String(e)));
        }} />
      )}
      {removeConfirmId && (
        <ConfirmDialog title={`Remove ${devices.find((device) => device.host_id === removeConfirmId)?.name ?? 'device'}?`} body="This removes the saved device and its cached workspace list from this sidebar. It does not delete data on the remote machine." confirmLabel="Remove device" onCancel={() => setRemoveConfirmId(null)} onConfirm={() => {
          const device = devices.find((item) => item.host_id === removeConfirmId);
          setRemoveConfirmId(null);
          if (device) void remove(device);
        }} />
      )}
      {remoteConversation && <RemoteTranscriptDialog key={`${remoteConversation.hostId}:${remoteConversation.conversationId}`} {...remoteConversation} deviceName={devices.find((device) => device.host_id === remoteConversation.hostId)?.name ?? remoteConversation.hostId} onClose={() => setRemoteConversation(null)} />}
    </section>
  )
}
function ConversationList() {
  const { conversationId, setConversationId, loadHistory, setWorkspace, newConversation, workspace } = useAgent()
  // Per-conversation run status: rows with an in-flight turn show a spinner
  // (issue #10). Reference-stable selector — only re-renders on status writes.
  const statusByConv = useAgent((s) => s.statusByConv)
  // Scheduled runs (issue #41) never write statusByConv — they stream inside
  // the backend — so the row spinner also keys off the agents poller's live
  // `running` flag, mapped by pinned conversation.
  const agents = useAgent((s) => s.agents)
  const agentRunningConvs = new Set(
    agents.filter((a) => a.running).map((a) => a.conversation_id),
  )
  // Row start/stop: conversation id -> agent, so the toggle can reach the
  // agent API without a lookup per click.
  const refreshAgents = useAgent((s) => s.refreshAgents)
  const agentByConv = new Map(agents.map((a) => [a.conversation_id, a]))
  // Conversations whose run the user just asked to stop: the row dims its
  // "working" signal right away (the click registered) and the toggle re-polls
  // agents fast until the backend settles, instead of waiting out the 5s poll.
  const [stoppingConvs, setStoppingConvs] = useState<Set<number>>(new Set())
  const clearStopping = (id: number) =>
    setStoppingConvs((prev) => {
      if (!prev.has(id)) return prev
      const next = new Set(prev)
      next.delete(id)
      return next
    })
  // Issue #81: pause/resume the agent's schedule straight from the row menu —
  // same full-record PATCH (agentToBody + enabled flipped) the AgentsDialog's
  // pause/resume button uses, then refresh so the row label flips.
  const toggleAgentEnable = (c: { id: number }) => {
    const a = agentByConv.get(c.id)
    if (!a) return
    updateAgent(a.id, agentToBody(a, !a.enabled))
      .then(() => refreshAgents())
      .catch((e) =>
        setNotice({
          title: `Could not update "${a.name}"`,
          message: String((e as { message?: string }).message ?? e),
        }),
      )
  }
  const toggleAgentRun = (c: { id: number }) => {
    const a = agentByConv.get(c.id)
    if (!a) return
    if (a.running) {
      // Same cancel path as an in-chat Stop: the turn ends after its current
      // step and the run settles normally.
      setStoppingConvs((prev) => new Set(prev).add(c.id))
      cancelAgent(c.id).catch(() => {})
      // The backend settles in well under a second; poll tightly so the row
      // flips to "run now" the moment it does (bounded, then the normal 5s
      // poll takes over as fallback).
      const started = Date.now()
      const settle = async () => {
        await refreshAgents()
        const still = useAgent
          .getState()
          .agents.some((x) => x.conversation_id === c.id && x.running)
        if (!still || Date.now() - started > 10000) clearStopping(c.id)
        else window.setTimeout(() => void settle(), 400)
      }
      window.setTimeout(() => void settle(), 400)
    } else {
      runAgentNow(a.id)
        .then(() => refreshAgents())
        .catch((e) =>
          setNotice({
            title: `Could not run "${a.name}"`,
            message: String((e as { message?: string }).message ?? e),
          }),
        )
    }
  }
  // Issue #25 sidebar signals: needs-you (any user-blocking gate) and the
  // finished-but-unacknowledged map (set by setStatus, cleared on open).
  const pendingQuestions = useAgent((s) => s.pendingQuestions)
  const pendingApprovals = useAgent((s) => s.pendingApprovals)
  const pendingPlanApprovals = useAgent((s) => s.pendingPlanApprovals)
  const finishedByConv = useAgent((s) => s.finishedByConv)
  const agentBranchByConv = useAgent((s) => s.agentBranchByConv)
  const [convs, setConvs] = useState<Array<{ id: number; title: string; workspace: string | null; updated_at: string; chat_type?: 'chat' | 'agent' }>>([])
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
  // Move chat (issue #8): the row being relocated — the picker dialog reads
  // the workspace registry and POSTs /move, then this list refreshes.
  const [moveTarget, setMoveTarget] = useState<{ id: number; title: string; workspace: string | null } | null>(null)
  const [removeWsTarget, setRemoveWsTarget] = useState<WorkspaceRow | null>(null)
  const [menuOpenId, setMenuOpenId] = useState<number | null>(null)
  const devices = useRemote((s) => s.devices)
  const refreshDevices = useRemote((s) => s.refreshDevices)

  const refresh = useCallback(() => {
    listConversations().then((rows) => {
      setConvs(rows)
      useAgent.setState({ titleByConv: {} })
    }).catch(() => setConvs([]))
    listWorkspaces().then(setWorkspaces).catch(() => setWorkspaces([]))
    void refreshDevices()
  }, [refreshDevices])
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

  // Conversations are local-owned even when their selected workspace belongs
  // to a remote. Keep the transcript under that device while preserving the
  // local conversation ID and local read/write APIs.
  const visibleConvs = convs.filter((conversation) => parseNsWorkspace(conversation.workspace) === null)
  const remoteConvs = convs.filter((conversation) => parseNsWorkspace(conversation.workspace) !== null)
  // Scheduled agents (issue #41): each agent chat is pinned under its own
  // workspace, above the workspace's normal chats. The composer gating map
  // comes from the AgentRunWatcher's store slice.
  const agentChatByConv = useAgent((s) => s.agentChatByConv)
  const [agentsDialog, setAgentsDialog] = useState<{ ws: string | null; agentId: string | null } | null>(null)

  /** Open a locally-owned conversation and adopt its workspace. */
  const openConversation = (c: { id: number; workspace: string | null }) => {
    setConversationId(c.id)
    setWorkspace(c.workspace ?? '')
    getMessages(c.id)
      .then((rows) => loadHistory(c.id, rows))
      .catch(() => {})
  }

  // Group rows by workspace; Default (null path) first, then by the most
  // recent conversation activity in each group.
  const localWorkspaces = workspaces.filter((workspace) => !workspace.owner_id)
  const groups: Array<{ ws: WorkspaceRow; items: typeof convs }> = []
  for (const w of localWorkspaces) {
    groups.push({ ws: w, items: visibleConvs.filter((c) => (c.workspace ?? null) === w.path) })
  }
  const knownPaths = new Set(localWorkspaces.map((w) => w.path))
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
  const orderedGroups = sortWorkspaceGroups(groups)
  for (const g of orderedGroups) {
    g.items.sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  }

  const renderRow = (c: typeof convs[number], isAgent: boolean) => (
    <ConversationRow
      key={c.id}
      conv={c}
      active={c.id === conversationId}
      running={
        statusByConv[String(c.id)] === 'thinking' ||
        statusByConv[String(c.id)] === 'running-tool' ||
        agentRunningConvs.has(c.id)
      }
      blocked={Boolean(
        pendingQuestions[String(c.id)] ||
          pendingApprovals[String(c.id)] ||
          pendingPlanApprovals[String(c.id)],
      )}
      finished={finishedByConv[String(c.id)] ?? null}
      pendingMerge={Boolean(agentBranchByConv[String(c.id)]?.pendingMerge)}
      pendingMergeBranch={agentBranchByConv[String(c.id)]?.branch}
      isAgent={isAgent}
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
      onMove={
        isAgent
          ? undefined
          : () => setMoveTarget({ id: c.id, title: c.title, workspace: c.workspace ?? null })
      }
      onToggleRun={isAgent ? () => toggleAgentRun(c) : undefined}
      stopping={stoppingConvs.has(c.id)}
      onAgentSettings={
        isAgent
          ? () => {
              const aid = agentChatByConv[String(c.id)]
              if (aid) setAgentsDialog({ ws: c.workspace, agentId: aid })
            }
          : undefined
      }
      onToggleEnable={isAgent ? () => toggleAgentEnable(c) : undefined}
      agentEnabled={agentByConv.get(c.id)?.enabled}
    />
  )

  return (
    <div className="flex-1 overflow-y-auto">
      <DeviceGroups devices={devices} workspaces={workspaces} conversations={remoteConvs} onChange={refresh} onOpenConversation={openConversation} />
      {orderedGroups.length > 0 && <h2 className="mb-1 px-1 font-mono text-[10px] font-semibold uppercase tracking-wider text-zinc-500">This device</h2>}
      {orderedGroups.map(({ ws, items }) => {
        const key = expandKey(ws.path ?? '')
        const isExpanded = expanded[key] ?? true
        const isActiveWs = (ws.path ?? '') === (workspace || '')
        // Agent chats pin above the workspace's normal chats (issue #41);
        // the 5-cap and show-more stepping apply to normal chats only.
        const wsAgents = items.filter((c) => c.chat_type === 'agent')
        const chats = items.filter((c) => c.chat_type !== 'agent')
        // Capped view: the 5 most recent chats, plus the open conversation
        // appended whenever it ranks older (the list never hides what you're
        // looking at); "show more" steps +5 per click, session-only.
        const activeIdx = chats.findIndex((c) => c.id === conversationId)
        const base = Math.min(5 + (extra[key] ?? 0), chats.length)
        const head = chats.slice(0, base)
        // The active chat sits outside the head block: append it (never a
        // duplicate — only when its index is past the head) so it stays
        // visible directly above the "show more" line.
        const visible =
          activeIdx >= base ? [...head, chats[activeIdx]] : head
        const hidden = chats.length - visible.length
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
              {/* Agents dialogue (issue #41): on-hover silhouette on each
                  workspace — lists the workspace's agents + new agent. */}
              <button
                className="rounded px-1 text-[10px] text-zinc-600 opacity-0 hover:text-zinc-200 group-hover:opacity-100 focus:opacity-100"
                aria-label={`Agents for ${ws.label}`}
                title="Agents — scheduled recurring runs in this workspace"
                onClick={() => setAgentsDialog({ ws: ws.path, agentId: null })}
              >
                <PersonIcon />
              </button>
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
                  {wsAgents.map((c) => renderRow(c, true))}
                  {visible.map((c) => renderRow(c, false))}
                  {hidden > 0 && (
                    <button
                      className="block w-full px-3 py-1 text-left text-[11px] text-zinc-600 hover:text-zinc-300"
                      onClick={() => setExtra((e) => ({ ...e, [key]: (e[key] ?? 0) + 5 }))}
                    >
                      Show more ({hidden} more)
                    </button>
                  )}
                </>
              ) : wsAgents.length > 0 ? (
                <>
                  {wsAgents.map((c) => renderRow(c, true))}
                  <p className="px-3 py-1 text-[10px] text-zinc-600">No conversations yet.</p>
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

      {/* in-app dialogs (replace native confirm/prompt/alert) */}
      {agentsDialog && (
        <AgentsDialog
          wsPath={agentsDialog.ws}
          editAgentId={agentsDialog.agentId}
          onClose={() => {
            setAgentsDialog(null)
            // Renames sync the pinned chat's title server-side; re-pull so
            // the sidebar row shows it without a manual reload.
            refresh()
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
      {moveTarget && (
        <MoveChatDialog
          convId={moveTarget.id}
          chatTitle={moveTarget.title}
          currentWorkspace={moveTarget.workspace}
          onCancel={() => setMoveTarget(null)}
          onError={(title, message) => {
            setMoveTarget(null)
            setNotice({ title, message })
          }}
          onDone={(target) => {
            const movedId = moveTarget?.id
            setMoveTarget(null)
            // The core invariant, both ways: the open conversation's
            // workspace IS the active workspace. If the moved chat is the
            // one on screen, the panel adopts the destination so the very
            // next message (and any queued-drain autosend) streams there —
            // not just the row re-sorting under its new group.
            if (movedId !== undefined && movedId === conversationId) setWorkspace(target ?? '')
            refresh()
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
                // Drop the conversation's per-run state, if any survived.
                useAgent.setState((s) => {
                  const key = String(deleteTarget.id)
                  const statusByConv = { ...s.statusByConv }
                  delete statusByConv[key]
                  const abortByConv = { ...s.abortByConv }
                  abortByConv[key]?.abort()
                  delete abortByConv[key]
                  const errorByConv = { ...s.errorByConv }
                  delete errorByConv[key]
                  // Chat deletion releases the session server-side; the
                  // chip must not keep claiming the agent branch.
                  useAgent.getState().setAgentBranch(key, null)
                  return { statusByConv, abortByConv, errorByConv }
                })
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
            deleteWorkspace(removeWsTarget.id, removeWsTarget.owner_id ?? undefined)
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
  running,
  blocked,
  finished,
  pendingMerge,
  pendingMergeBranch,
  isAgent,
  onAgentSettings,
  onToggleEnable,
  agentEnabled,
  onToggleRun,
  stopping,
  menuOpen,
  setMenuOpen,
  onOpen,
  onExport,
  onSys,
  onDelete,
  onMove,
}: {
  conv: { id: number; title: string; updated_at: string }
  active: boolean
  /** A turn is streaming in this conversation right now (issue #10). */
  running: boolean
  /** The run is paused on the user: ask_user question, tool approval or plan
   *  approval (issue #25, grilling round 1: all three gates, orange beats
   *  the working dots since the run is frozen, not progressing). */
  blocked: boolean
  /** Finished-but-unacknowledged signal: 'ok' (green bar) | 'error' (red
   *  pill). Only set for background chats; cleared when the chat opens. */
  finished: 'ok' | 'error' | null
  /** Session branch still has pending work; this survives opening/switching chats. */
  pendingMerge?: boolean
  pendingMergeBranch?: string
  /** A scheduled agent's pinned chat (issue #41) — silhouette badge. */
  isAgent?: boolean
  /** Open the agent settings dialogue (agent chats only). */
  onAgentSettings?: () => void
  /** Pause/resume the agent's schedule (agent chats only) — issue #81: same
   *  toggle the AgentsDialog row has, reachable without the full dialog. */
  onToggleEnable?: () => void
  /** Current enabled state, for the menu item's label (with onToggleEnable). */
  agentEnabled?: boolean
  /** Start/stop the agent's run (agent chats only). Present = the row shows
   *  the toggle; the icon follows the running state (■ stop / ▶ run now). */
  onToggleRun?: () => void
  /** The user just clicked stop: dim the "working" signals until the
   *  backend settles, so the click visibly registered. */
  stopping?: boolean
  menuOpen: boolean
  setMenuOpen: (open: boolean) => void
  onOpen: () => void
  onExport: () => void
  onSys: () => void
  onDelete: () => void
  /** Offered for normal chats only: a scheduled agent's pinned chat is
   *  welded to the agent's workspace row, so it is not movable. */
  onMove?: () => void
}) {
  const liveTitle = useAgent((s) => s.titleByConv[String(conv.id)])
  return (
    <div className="group relative flex items-center">
      <button
        className={`flex min-w-0 flex-1 items-center rounded px-2 py-1.5 text-left text-xs ${
          active ? 'bg-zinc-800 text-zinc-100' : 'text-zinc-300 hover:bg-zinc-800/60'
        }`}
        onClick={onOpen}
        title={
          pendingMerge
            ? `${liveTitle ?? conv.title} — unmerged session work on ${pendingMergeBranch ?? 'agent branch'}`
            : liveTitle ?? conv.title
        }
        aria-label={
          pendingMerge
            ? `${liveTitle ?? conv.title}, unmerged session work on ${pendingMergeBranch ?? 'agent branch'}`
            : liveTitle ?? conv.title
        }
      >
        {/* Issue #25: one status slot left of the title, same footprint for
            every state so the row never shifts. Precedence: needs-you (orange,
            pulsing) > pending merge > finished (green bar / red pill) > working dots. */}
        {blocked ? (
          <span
            aria-hidden="true"
            className="run-bar run-bar-orange mr-1.5 shrink-0"
            title="Waiting for you — a question or approval is pausing this run"
          />
        ) : pendingMerge ? (
          <span
            aria-hidden="true"
            className="mr-1.5 inline-flex h-3 w-3 shrink-0 items-center justify-center rounded-full border border-amber-500/70 font-mono text-[9px] leading-none text-amber-300"
            title={`Unmerged session work on ${pendingMergeBranch ?? 'agent branch'} — open chat for details`}
          >
            !
          </span>
        ) : finished === 'error' ? (
          <span aria-hidden="true" className="run-bar run-bar-red mr-1.5 shrink-0" title="Run failed" />
        ) : finished === 'ok' ? (
          <span aria-hidden="true" className="run-bar run-bar-green mr-1.5 shrink-0" title="Run finished" />
        ) : running ? (
          <span
            aria-hidden="true"
            className={`run-dots mr-1.5 shrink-0 transition-opacity ${stopping ? 'stopping' : ''}`}
            title={stopping ? 'Stopping…' : 'Working…'}
          >
            <i />
            <i />
            <i />
          </span>
        ) : null}
        {isAgent && (
          <span
            aria-hidden="true"
            className={`mr-1.5 shrink-0 ${active ? 'text-blue-200' : 'text-zinc-500'}`}
            title="Scheduled agent — runs on a repeating schedule"
          >
            <PersonIcon />
          </span>
        )}
        <span className="min-w-0 flex-1 truncate">{liveTitle ?? conv.title}</span>
        <span
          className={`ml-1.5 shrink-0 font-mono text-[9px] ${active ? 'text-blue-200' : 'text-zinc-600'}`}
        >
          {relTime(conv.updated_at)}
        </span>
      </button>
      <div className={`absolute right-1 flex items-center gap-1 ${menuOpen || (onToggleRun && running) ? '' : 'opacity-0 group-hover:opacity-100'}`}>
        {onToggleRun && (
          <button
            className={`flex h-[18px] w-[18px] items-center justify-center rounded transition-opacity ${
              stopping
                ? 'opacity-30'
                : running
                  ? 'text-zinc-300 hover:text-zinc-100'
                  : 'text-zinc-600 hover:text-zinc-300'
            }`}
            aria-label={running ? 'Stop this run' : 'Run now'}
            title={running ? 'Stop this run' : 'Run now'}
            onClick={(e) => {
              e.stopPropagation()
              onToggleRun()
            }}
          >
            {running ? <StopIcon /> : <PlayIcon />}
          </button>
        )}
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
            {onMove && (
              <button
                className="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
                onClick={() => {
                  setMenuOpen(false)
                  onMove()
                }}
              >
                Move to workspace…
              </button>
            )}
            {onAgentSettings && (
              <button
                className="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
                onClick={() => {
                  setMenuOpen(false)
                  onAgentSettings()
                }}
              >
                Agent settings…
              </button>
            )}
            {onToggleEnable && (
              <button
                className="block w-full px-3 py-1.5 text-left text-xs text-zinc-300 hover:bg-zinc-800"
                onClick={() => {
                  setMenuOpen(false)
                  onToggleEnable()
                }}
              >
                {agentEnabled === false ? 'Resume agent' : 'Pause agent'}
              </button>
            )}
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

// ---------------------------------------------------------------- update chip (#16)

/** Sidebar "update available" chip: check-for-update runs inside the hook
 *  (on mount + every 5 min (#36), silent on failure). Clicking downloads the
 *  installer via Rust, then hands off to the shim and closes the app —
 *  the chip's last visible state is "installing…". */
function UpdateChip() {
  const { update, phase, progress, error, install } = useUpdateCheck()
  if (phase === 'idle' || !update) return null
  const pct = Math.round(progress * 100)
  const label =
    phase === 'ready'
      ? `⬆ update to v${update.version}`
      : phase === 'downloading'
        ? `downloading ${pct}%`
        : phase === 'installing'
          ? 'installing… YAAH will close'
          : 'update failed — click to retry'
  return (
    <button
      className="mb-2 w-full overflow-hidden rounded border border-amber-700/60 bg-amber-950/30 px-2 py-1 text-left font-mono text-[10px] text-amber-300 hover:border-amber-500 disabled:opacity-60"
      onClick={() => install()}
      disabled={phase === 'downloading' || phase === 'installing'}
      title={phase === 'error' && error ? `Update failed: ${error}` : `Install YAAH v${update.version} (github.com/elboaf/YAAH/releases/latest)`}
    >
      <span className="block truncate">{label}</span>
      {phase === 'downloading' && (
        <span className="mt-0.5 block h-0.5 w-full rounded bg-zinc-700">
          <span className="block h-0.5 rounded bg-amber-500" style={{ width: `${pct}%` }} />
        </span>
      )}
    </button>
  )
}

/** #51/#76 — shared select-option groups for every model picker. */
export function ModelOptions({
  byProvider,
  value,
}: {
  byProvider: Record<string, ProviderModels>
  value: string
}) {
  const [provider, modelId] = value.split('::')
  return (
    <>
      {Object.entries(byProvider).map(([name, pm]) => (
        <optgroup key={name} label={pm.error ? `${name} (${pm.error})` : name}>
          {pm.models.map((m) => (
            <option key={`${name}::${m}`} value={`${name}::${m}`}>
              {m}
            </option>
          ))}
        </optgroup>
      ))}
      {/* value isn't in any group (provider down / removed): keep it visible+selectable */}
      {!Object.values(byProvider).some((pm) => pm.models.includes(modelId)) && (
        <option value={value}>{modelId}</option>
      )}
      {Object.keys(byProvider).length === 0 && (
        <option value={value}>No provider configured — open Settings</option>
      )}
    </>
  )
}

/** #51 — hook: the provider-grouped model list shared by both pickers. */
export function useModelList() {
  const [byProvider, setByProvider] = useState<Record<string, ProviderModels>>({})
  const refresh = useCallback(() => {
    listAvailableModels()
      .then((r) => setByProvider(r.providers))
      .catch(() => {})
  }, [])
  useEffect(() => {
    refresh()
  }, [refresh])
  return { byProvider, refresh }
}

/** #76 — render the effort levels advertised for the selected model. */
export function EffortOptions({ efforts = [] }: { efforts?: string[] }) {
  return (
    <>
      <option value="">Default</option>
      {efforts.map((effort) => (
        <option key={effort} value={effort}>
          {effort.charAt(0).toUpperCase() + effort.slice(1)}
        </option>
      ))}
    </>
  )
}

export function modelReasoningEfforts(
  byProvider: Record<string, ProviderModels>,
  model: string,
): string[] {
  const parsed = parseModelScope(model)
  const candidates = parsed.provider ? [parsed.provider] : Object.keys(byProvider)
  for (const provider of candidates) {
    const info = byProvider[provider]?.model_info?.find((entry) => entry.id === parsed.model)
    if (info) return info.reasoning_efforts
  }
  return []
}

export function modelSupportsReasoning(
  byProvider: Record<string, ProviderModels>,
  model: string,
): boolean {
  const parsed = parseModelScope(model)
  const candidates = parsed.provider ? [parsed.provider] : Object.keys(byProvider)
  return candidates.some((provider) =>
    byProvider[provider]?.model_info?.some((entry) => entry.id === parsed.model && entry.supports_reasoning),
  )
}

export const EFFORT_HINT = 'Effort options are shown only when advertised by the model provider.'

/** Default reasoning/thought level for new chats. Existing conversations keep
 *  their pinned effort, matching the default-model selector's scope. */
export function DefaultThoughtLevelPicker() {
  const effort = useAgent((s) => s.globalEffort)
  const model = useAgent((s) => s.globalModel)
  const { byProvider } = useModelList()
  const supportedEfforts = modelReasoningEfforts(byProvider, model)
  const refreshGlobals = useAgent((s) => s.refreshGlobals)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const apply = async (value: string) => {
    if (value === effort || saving) return
    setSaving(true)
    setError('')
    try {
      await updateConfig({ reasoning_effort: value })
      await refreshGlobals()
    } catch (e) {
      setError(`Could not save thought level: ${String((e as Error).message ?? e)}`)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="w-24 shrink-0">
      <select
        id="default-thought-level"
        className="w-full rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-200 focus:border-blue-500 focus:outline-none disabled:opacity-60"
        value={effort}
        onChange={(e) => void apply(e.target.value)}
        disabled={saving}
        aria-label="Thought level"
        aria-describedby={error ? 'default-thought-level-status' : undefined}
      >
        <EffortOptions efforts={supportedEfforts} />
      </select>
      {saving && <p className="mt-1 text-[10px] text-zinc-500" role="status">Saving thought level…</p>}
      {error && <p id="default-thought-level-status" className="mt-1 text-[10px] text-red-400" role="alert">{error}</p>}
    </div>
  )
}

export function Sidebar() {
  const { newConversation, workspace, setWorkspace, clearLog, refreshGlobals } = useAgent()
  const globalModel = useAgent((s) => s.globalModel)
  const [activeProvider, setActiveProvider] = useState('')
  // name -> {models, error?} for every configured provider
  const [byProvider, setByProvider] = useState<Record<string, ProviderModels>>({})
  const [savingModel, setSavingModel] = useState(false)
  const [modelError, setModelError] = useState('')
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

  // Registry and device flows are owned by ConversationList/DeviceGroups;
  // the sidebar footer only manages local model defaults.

  // Merged model list: every configured provider, queried in parallel by the
  // backend (keys never reach the browser). Grouped per provider in the dropdown.
  const refreshModels = useCallback(() => {
    listAvailableModels()
      .then((r) => {
        setByProvider(r.providers)
        setActiveProvider(r.active_provider)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    // #51: the sidebar picker is now the DEFAULT for new chats (the store's
    // globalModel drives both this select and fresh drafts' header pickers).
    refreshGlobals()
    refreshModels()
  }, [refreshGlobals, refreshModels])

  // value encoding "provider::model" keeps providers with clashing ids apart
  const pickModel = (value: string) => {
    const idx = value.indexOf('::')
    if (idx < 0) return
    const provider = value.slice(0, idx)
    const m = value.slice(idx + 2)
    const previousProvider = activeProvider
    if (!m || (m === globalModel && provider === activeProvider)) return
    setSavingModel(true)
    setModelError('')
    setActiveProvider(provider)
    const keepEffort = modelReasoningEfforts(byProvider, `${provider}::${m}`).includes(
      useAgent.getState().globalEffort,
    )
    setActiveModel(provider, m)
      .then(() => (keepEffort ? undefined : updateConfig({ reasoning_effort: '' })))
      .then(refreshGlobals)
      .then(refreshModels)
      .catch((e) => {
        setActiveProvider(previousProvider)
        setModelError(`Could not save model: ${String((e as Error).message ?? e)}`)
      })
      .finally(() => setSavingModel(false))
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
      <aside className="flex w-64 min-w-[220px] flex-col border-r border-zinc-800 bg-zinc-900 p-2 text-sm">
        <div className="mb-2 flex items-center gap-1.5">
          <button
            className="min-w-0 flex-1 rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500"
            onClick={() => {
              newConversation()
              clearLog()
            }}
          >
            New chat
          </button>
          <button
            className="flex h-7 w-7 shrink-0 items-center justify-center rounded border border-dashed border-zinc-700 text-zinc-500 hover:border-zinc-500 hover:bg-zinc-800/60 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500"
            aria-label="Add local workspace"
            title="Add local workspace…"
            onClick={() => void browseWorkspace()}
          >
            <FolderPlusIcon />
          </button>
        </div>
        <ConversationList />
        <UpdateChip />
        {/* Compact defaults: existing chats retain their own selections. */}
        <div className="relative mt-auto border-t border-zinc-800 pt-2">
          <div className="flex items-center gap-1.5">
            <select
              id="default-model"
              className="min-w-0 flex-1 truncate rounded border border-zinc-700 bg-zinc-800 px-1.5 py-1 font-mono text-xs text-zinc-200 focus:border-blue-500 focus:outline-none disabled:opacity-60"
              value={`${activeProvider}::${globalModel}`}
              onChange={(e) => pickModel(e.target.value)}
              disabled={savingModel}
              aria-label="Default model"
              aria-describedby={modelError ? 'default-model-status' : undefined}
              title={`${activeProvider} · ${globalModel}`}
            >
              <ModelOptions byProvider={byProvider} value={`${activeProvider}::${globalModel}`} />
            </select>
            <DefaultThoughtLevelPicker />
            <button
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-zinc-500 hover:bg-zinc-800 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500"
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
          {savingModel && <p className="mt-1 text-[10px] text-zinc-500" role="status">Saving model…</p>}
          {modelError && <p id="default-model-status" className="mt-1 text-[10px] text-red-400" role="alert">{modelError}</p>}
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
            getConfig().then((c) => setActiveProvider(c.active_provider)).catch(() => {})
            refreshGlobals()
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

/** Scheduled agents (issue #41): first-class recurring runs in pinned chats.
 *  The dialogue opens from the on-hover silhouette icon on each workspace
 *  (list + new) and as "Agent settings…" from the pinned chat's row menu.
 *  Typed messages in an agent chat become standing instructions (the
 *  Composer routes them to the API); they never trigger a run. */

function PersonIcon({ className = '' }: { className?: string }) {
  return (
    <svg
      className={className}
      width="11"
      height="11"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="8" cy="4.5" r="2.8" />
      <path d="M2.5 14.5c0-3 2.5-5.2 5.5-5.2s5.5 2.2 5.5 5.2" />
    </svg>
  )
}

/** Add-workspace glyph for the sidebar's New chat row: folder with a plus,
 * drawn to match PersonIcon's stroke weight. */
function FolderPlusIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M1.5 4.5a1.5 1.5 0 0 1 1.5-1.5h3l1.5 2h6a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H3a1.5 1.5 0 0 1-1.5-1.5z" />
      <path d="M8 6.8v4.4M5.8 9h4.4" />
    </svg>
  )
}

/** Run-now / stop glyphs for the agent row toggle: drawn, not unicode, so
 *  weight matches the row's icon voice at 8px. */
function PlayIcon() {
  return (
    <svg width="8" height="8" viewBox="0 0 8 8" fill="currentColor" aria-hidden="true">
      <path d="M1.5 0.8 L7 4 L1.5 7.2 Z" />
    </svg>
  )
}

function StopIcon() {
  return (
    <svg width="7" height="7" viewBox="0 0 8 8" fill="currentColor" aria-hidden="true">
      <rect x="0.8" y="0.8" width="6.4" height="6.4" rx="1" />
    </svg>
  )
}

const agentInputCls =
  'rounded border border-zinc-700 bg-zinc-800 px-2 py-1 font-mono text-xs text-zinc-100 focus:border-blue-500 focus:outline-none'

/** One standing instruction row: inline edit + delete (issue #41: the list
 *  is editable and individually deletable). */
function InstructionRow({ agentId, ins }: { agentId: string; ins: AgentInstruction }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(ins.content)
  const [busy, setBusy] = useState(false)

  const save = async () => {
    setBusy(true)
    try {
      await updateAgentInstruction(agentId, ins.id, draft.trim())
      setEditing(false)
    } finally {
      setBusy(false)
    }
  }

  if (editing) {
    return (
      <li className="flex items-center gap-1.5">
        <input
          className={`min-w-0 flex-1 ${agentInputCls}`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void save()
            if (e.key === 'Escape') setEditing(false)
          }}
          autoFocus
        />
        <button
          className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-800"
          disabled={busy}
          onClick={() => void save()}
        >
          save
        </button>
        <button
          className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800"
          onClick={() => {
            setDraft(ins.content)
            setEditing(false)
          }}
        >
          cancel
        </button>
      </li>
    )
  }
  return (
    <li className="flex items-start gap-1.5">
      <span className="min-w-0 flex-1 break-words text-[11px] text-zinc-300">{ins.content}</span>
      <button
        className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800"
        onClick={() => setEditing(true)}
      >
        edit
      </button>
      <button
        className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-red-400 hover:bg-zinc-800"
        onClick={() => void deleteAgentInstruction(agentId, ins.id)}
      >
        ✕
      </button>
    </li>
  )
}

/** Standing instructions block: shown for a saved agent. */
function InstructionsEditor({ agent }: { agent: ScheduledAgent }) {
  const [draft, setDraft] = useState('')
  const refreshAgents = useAgent((s) => s.refreshAgents)
  const add = async () => {
    if (!draft.trim()) return
    await addAgentInstruction(agent.id, draft.trim())
    setDraft('')
    await refreshAgents()
  }
  return (
    <div>
      <p className="mb-1 font-mono text-[10px] uppercase tracking-wider text-zinc-500">
        Standing instructions — appended to the prompt at every fire
      </p>
      <ul className="mb-1.5 space-y-1">
        {agent.instructions.map((ins) => (
          <InstructionRow key={ins.id} agentId={agent.id} ins={ins} />
        ))}
        {agent.instructions.length === 0 && (
          <li className="text-[10px] text-zinc-600">
            None. Messages typed in the agent's chat also become standing instructions.
          </li>
        )}
      </ul>
      <div className="flex gap-1.5">
        <input
          className={`min-w-0 flex-1 ${agentInputCls}`}
          value={draft}
          placeholder="add an instruction…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void add()
          }}
        />
        <button
          className="shrink-0 rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
          onClick={() => void add()}
        >
          Add
        </button>
      </div>
    </div>
  )
}

/** The create/edit form. `agent` null = new agent in workspace `wsPath`. */
function AgentForm({
  agent,
  wsPath,
  onDone,
  onCancel,
}: {
  agent: ScheduledAgent | null
  wsPath: string | null
  onDone: (saved: ScheduledAgent) => void
  onCancel: () => void
}) {
  const [name, setName] = useState(agent?.name ?? '')
  const [prompt, setPrompt] = useState(agent?.prompt ?? '')
  const [scheduleType, setScheduleType] = useState<AgentScheduleType>(agent?.schedule_type ?? 'interval')
  const [minutes, setMinutes] = useState(String(agent?.schedule_spec?.minutes ?? 60))
  const [time, setTime] = useState(agent?.schedule_spec?.time ?? '09:00')
  const [weekday, setWeekday] = useState(String(agent?.schedule_spec?.weekday ?? 0))
  const [policy, setPolicy] = useState<AgentPolicy>(agent?.approval_policy ?? 'sandbox-only')
  const [model, setModel] = useState(agent?.model ?? '')
  const [effort, setEffort] = useState(agent?.effort ?? '')
  const [memory, setMemory] = useState(agent?.memory_enabled ?? true)
  const [allowAsk, setAllowAsk] = useState(agent?.allow_ask_user ?? false)
  const [retention, setRetention] = useState(String(agent?.retention ?? 0))
  const [notify, setNotify] = useState(agent?.notify_on_success ?? false)
  const [enabled, setEnabled] = useState(agent?.enabled ?? true)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  // Model list across every configured provider, grouped per provider for the
  // same provider-grouped select the sidebar footer uses. A per-agent override
  // may be a bare id (= active provider, unchanged behavior) or
  // "provider::model" to route the run at a specific provider — the backend
  // resolves both (model_client.chat swaps base/key/model as needed).
  const [byProvider, setByProvider] = useState<Record<string, ProviderModels>>({})
  const [activeProvider, setActiveProvider] = useState('')
  const refreshAgents = useAgent((s) => s.refreshAgents)

  useEffect(() => {
    let alive = true
    listAvailableModels()
      .then((r) => {
        if (!alive) return
        setByProvider(r.providers)
        setActiveProvider(r.active_provider)
      })
      .catch(() => {})
    return () => {
      alive = false
    }
  }, [])

  const save = async () => {
    setErr(null)
    if (!name.trim() || !prompt.trim()) {
      setErr('name and prompt are required')
      return
    }
    setBusy(true)
    try {
      const body: AgentBody = {
        workspace: wsPath ?? '',
        name: name.trim(),
        prompt: prompt.trim(),
        schedule_type: scheduleType,
        schedule_spec:
          scheduleType === 'interval'
            ? { minutes: Math.max(5, parseInt(minutes, 10) || 60) }
            : scheduleType === 'daily'
              ? { time }
              : { weekday: parseInt(weekday, 10) || 0, time },
        approval_policy: policy,
        model: model.trim(),
        effort,
        memory_enabled: memory,
        allow_ask_user: allowAsk,
        retention: Math.max(0, parseInt(retention, 10) || 0),
        notify_on_success: notify,
        enabled,
      }
      const saved = agent ? await updateAgent(agent.id, body) : await addAgent(body)
      await refreshAgents()
      onDone(saved)
    } catch (e) {
      setErr(String((e as { message?: string }).message ?? e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2.5">
      <div className="flex gap-2">
        <label className="min-w-0 flex-1">
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Name</span>
          <input className={`w-full ${agentInputCls}`} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="w-44 shrink-0">
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Workspace</span>
          <input
            className={`w-full ${agentInputCls} text-zinc-500`}
            value={wsPath ? wsBasename(wsPath) : 'Default (Home)'}
            disabled
          />
        </label>
      </div>
      <label className="block">
        <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">
          Prompt — what the agent does on every run
        </span>
        <textarea
          className={`w-full ${agentInputCls} min-h-[64px] resize-y`}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
        />
      </label>
      <div className="flex flex-wrap items-end gap-2">
        <label>
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Schedule</span>
          <select
            className={agentInputCls}
            value={scheduleType}
            onChange={(e) => setScheduleType(e.target.value as AgentScheduleType)}
          >
            <option value="interval">every N min</option>
            <option value="daily">daily</option>
            <option value="weekly">weekly</option>
          </select>
        </label>
        {scheduleType === 'interval' ? (
          <label>
            <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Minutes (min 5)</span>
            <input className={`w-24 ${agentInputCls}`} value={minutes} onChange={(e) => setMinutes(e.target.value)} />
          </label>
        ) : (
          <>
            <label>
              <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Time</span>
              <input className={`w-24 ${agentInputCls}`} value={time} onChange={(e) => setTime(e.target.value)} placeholder="09:00" />
            </label>
            {scheduleType === 'weekly' && (
              <label>
                <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Day</span>
                <select className={agentInputCls} value={weekday} onChange={(e) => setWeekday(e.target.value)}>
                  {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((d, i) => (
                    <option key={d} value={i}>
                      {d}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </>
        )}
        <label>
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Approval policy</span>
          <select className={agentInputCls} value={policy} onChange={(e) => setPolicy(e.target.value as AgentPolicy)}>
            <option value="sandbox-only">sandbox-only</option>
            <option value="autonomous">autonomous</option>
          </select>
        </label>
      </div>
      <p className="text-[10px] leading-relaxed text-zinc-600">
        {policy === 'sandbox-only'
          ? 'Sandbox-only (default): tools that would need approval are skipped ("skipped: approval required") and the run continues — safe unattended.'
          : 'Autonomous (opt-in): everything auto-approves, including shell commands and file edits — only for agents you trust.'}
      </p>
      <label className="flex items-center gap-1.5">
        <input
          type="checkbox"
          checked={allowAsk}
          onChange={(e) => setAllowAsk(e.target.checked)}
        />
        <span
          className="text-[10px] text-zinc-400"
          title="Leave off for unattended agents: questions are then skipped with a note and the run continues."
        >
          May ask questions — the agent may pause to ask you something and waits for your answer.
        </span>
      </label>
      <div className="flex flex-wrap items-end gap-2">
        <label>
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Model</span>
          <select
            className={agentInputCls}
            value={model}
            onChange={(e) => setModel(e.target.value)}
            aria-label="Model override"
            title={model || 'default (inherit active model)'}
          >
            {/* head option: this field is an override — blank = active model */}
            <option value="">default (inherit active model)</option>
            {Object.entries(byProvider).map(([name, pm]) => (
              <optgroup
                key={name}
                label={pm.error ? `${name} (${pm.error})` : name}
              >
                {pm.models.map((m) => (
                  <option key={`${name}::${m}`} value={name === activeProvider ? m : `${name}::${m}`}>
                    {m}
                  </option>
                ))}
              </optgroup>
            ))}
            {/* saved override isn't in any group (e.g. its provider is down or removed) */}
            {model !== '' && !Object.values(byProvider).some((pm) => pm.models.includes(model)) && (
              <option value={model}>{model}</option>
            )}
            {Object.keys(byProvider).length === 0 && model !== '' && (
              <option value={model}>{model}</option>
            )}
          </select>
        </label>
        <label>
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Effort</span>
          <select className={agentInputCls} value={effort} onChange={(e) => setEffort(e.target.value)}>
            <option value="">default</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select>
        </label>
        <label>
          <span className="mb-0.5 block text-[10px] uppercase tracking-wider text-zinc-500">Retention (runs, 0 = all)</span>
          <input className={`w-24 ${agentInputCls}`} value={retention} onChange={(e) => setRetention(e.target.value)} />
        </label>
      </div>
      <div className="flex flex-wrap gap-4 text-[11px] text-zinc-400">
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={memory} onChange={(e) => setMemory(e.target.checked)} />
          Memory — include transcript history in each fire
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={notify} onChange={(e) => setNotify(e.target.checked)} />
          Notify on completion
        </label>
        <label className="flex items-center gap-1.5">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
          Enabled
        </label>
      </div>
      {agent && <InstructionsEditor agent={agent} />}
      {err && <p className="text-xs text-red-400">{err}</p>}
      <div className="flex justify-end gap-2 border-t border-zinc-800 pt-2.5">
        <button
          className="rounded border border-zinc-700 px-3 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          className="rounded bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50"
          disabled={busy}
          onClick={() => void save()}
        >
          {agent ? 'Save agent' : 'Create agent'}
        </button>
      </div>
      {!agent && (
        <p className="text-[10px] text-zinc-600">
          The first fire is never immediate — an interval agent runs one interval after save, a
          daily/weekly agent at its next clock slot. Use "Run now" to test right away. Missed fires
          while YAAH is closed are skipped.
        </p>
      )}
    </div>
  )
}

const AGENT_DLG_OVERLAY =
  'fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4'

/** The agents dialogue for one workspace: list of that workspace's agents +
 *  "New agent", each with run-now / pause / edit / delete (delete removes the
 *  pinned chat after a confirm). */
function AgentsDialog({
  wsPath,
  editAgentId,
  onClose,
}: {
  wsPath: string | null
  editAgentId: string | null
  onClose: () => void
}) {
  const agents = useAgent((s) => s.agents)
  const refreshAgents = useAgent((s) => s.refreshAgents)
  const pushToast = useAgent((s) => s.pushToast)
  const openConversationId = useAgent((s) => s.setConversationId)
  const loadHistory = useAgent((s) => s.loadHistory)
  const setWorkspace = useAgent((s) => s.setWorkspace)
  // null = list view; 'new' = creating; otherwise the agent id being edited.
  const [editing, setEditing] = useState<string | null>(editAgentId ?? null)
  const [deleteTarget, setDeleteTarget] = useState<ScheduledAgent | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    void refreshAgents()
  }, [refreshAgents])
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !deleteTarget) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, deleteTarget])

  const wsAgents = agents
    .filter((a) => (a.workspace || '') === (wsPath || ''))
    .sort((a, b) => a.name.localeCompare(b.name))
  const editingAgent = editing && editing !== 'new' ? agents.find((a) => a.id === editing) ?? null : null

  const openChat = (a: ScheduledAgent) => {
    setConversationSafe(openConversationId, loadHistory, setWorkspace, a)
    onClose()
  }

  const runNow = async (a: ScheduledAgent) => {
    setBusy(true)
    try {
      await runAgentNow(a.id)
      await refreshAgents()
    } catch (e) {
      pushToast({ kind: 'error', title: `Could not run "${a.name}"`, body: String((e as { message?: string }).message ?? e) })
    } finally {
      setBusy(false)
    }
  }

  const togglePause = async (a: ScheduledAgent) => {
    setBusy(true)
    try {
      await updateAgent(a.id, agentToBody(a, !a.enabled))
      await refreshAgents()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className={AGENT_DLG_OVERLAY} onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="flex max-h-[85vh] w-full max-w-2xl flex-col rounded-lg border border-zinc-700 bg-zinc-900 shadow-2xl">
        <div className="flex shrink-0 items-center justify-between border-b border-zinc-800 px-4 py-3">
          <h2 className="font-mono text-xs uppercase tracking-wider text-zinc-400">
            Agents — {wsPath ? wsBasename(wsPath) : 'Default (Home)'}
          </h2>
          <button className="rounded px-2 text-zinc-500 hover:text-zinc-200" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {editing === null ? (
            <div className="space-y-2">
              {wsAgents.length === 0 && (
                <p className="py-4 text-center text-xs text-zinc-600">
                  No agents in this workspace yet. An agent runs its prompt on a repeating
                  schedule, unattended, appending every run to its own pinned chat.
                </p>
              )}
              {wsAgents.map((a) => (
                <div key={a.id} className="rounded border border-zinc-800 bg-zinc-900/60 p-2.5">
                  <div className="flex items-center gap-2">
                    <span
                      className={`font-mono text-[10px] uppercase ${a.running ? 'text-blue-400' : a.enabled ? 'text-emerald-400' : 'text-zinc-500'}`}
                    >
                      {a.running ? 'running' : a.enabled ? 'on' : 'paused'}
                    </span>
                    <span className="truncate font-mono text-xs text-zinc-200">{a.name}</span>
                    <span className="flex-1 truncate font-mono text-[10px] text-zinc-600">
                      {a.schedule_text} · {a.approval_policy}
                      {a.next_fire_at && a.enabled ? ` · next ${relTime(a.next_fire_at)}` : ''}
                    </span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-[10px] text-zinc-500">{a.prompt}</p>
                  <div className="mt-1.5 flex items-center gap-1.5">
                    <button
                      className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
                      disabled={busy || a.running}
                      onClick={() => void runNow(a)}
                    >
                      run now
                    </button>
                    <button
                      className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800"
                      disabled={busy}
                      onClick={() => void togglePause(a)}
                    >
                      {a.enabled ? 'pause' : 'resume'}
                    </button>
                    <button
                      className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800"
                      onClick={() => setEditing(a.id)}
                    >
                      edit
                    </button>
                    <button
                      className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-800"
                      onClick={() => openChat(a)}
                    >
                      open chat
                    </button>
                    <button
                      className="ml-auto rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-red-400 hover:bg-zinc-800"
                      onClick={() => setDeleteTarget(a)}
                    >
                      delete
                    </button>
                  </div>
                </div>
              ))}
              <button
                className="w-full rounded border border-dashed border-zinc-700 px-3 py-2 text-xs text-zinc-400 hover:border-zinc-500 hover:text-zinc-200"
                onClick={() => setEditing('new')}
              >
                + New agent
              </button>
            </div>
          ) : (
            <AgentForm
              agent={editingAgent}
              wsPath={editingAgent ? editingAgent.workspace || null : wsPath}
              onDone={() => setEditing(null)}
              onCancel={() => setEditing(null)}
            />
          )}
        </div>
      </div>
      {deleteTarget && (
        <ConfirmDialog
          title={`Delete agent "${deleteTarget.name}"?`}
          body="The agent and its pinned chat (the full run transcript) will be removed. This cannot be undone."
          confirmLabel="Delete agent"
          onCancel={() => setDeleteTarget(null)}
          onConfirm={async () => {
            setDeleteTarget(null)
            await deleteAgent(deleteTarget.id, true)
            await refreshAgents()
          }}
        />
      )}
    </div>
  )
}

/** Shared helpers for dialog rows (kept tiny on purpose). */
function agentToBody(a: ScheduledAgent, enabled: boolean): AgentBody {
  return {
    workspace: a.workspace || '',
    name: a.name,
    prompt: a.prompt,
    schedule_type: a.schedule_type,
    schedule_spec: a.schedule_spec,
    approval_policy: a.approval_policy,
    model: a.model,
    effort: a.effort,
    memory_enabled: a.memory_enabled,
    allow_ask_user: a.allow_ask_user,
    retention: a.retention,
    notify_on_success: a.notify_on_success,
    enabled,
  }
}

async function setConversationSafe(
  setConversationId: (id: number) => void,
  loadHistory: (id: number, rows: Awaited<ReturnType<typeof getMessages>>) => void,
  setWorkspace: (ws: string) => void,
  a: ScheduledAgent,
) {
  setConversationId(a.conversation_id)
  setWorkspace(a.workspace || '')
  getMessages(a.conversation_id)
    .then((rows) => loadHistory(a.conversation_id, rows))
    .catch(() => {})
}

/** Bottom-right toast stack (issue #41): failure toasts for scheduled runs
 *  always; success only for agents with notify-on-success. */
function ToastCard({ toast }: { toast: Toast }) {
  const dismiss = useAgent((s) => s.dismissToast)
  useEffect(() => {
    const t = window.setTimeout(() => dismiss(toast.id), 6000)
    return () => window.clearTimeout(t)
  }, [toast.id, dismiss])
  const tone =
    toast.kind === 'error'
      ? 'border-red-800 bg-red-950/90 text-red-200'
      : toast.kind === 'success'
        ? 'border-emerald-800 bg-emerald-950/90 text-emerald-200'
        : 'border-zinc-700 bg-zinc-800/95 text-zinc-200'
  return (
    <div className={`pointer-events-auto rounded border px-3 py-2 shadow-lg ${tone}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium">{toast.title}</p>
          {toast.body && <p className="mt-0.5 text-[10px] opacity-80">{toast.body}</p>}
        </div>
        <button className="shrink-0 opacity-60 hover:opacity-100" onClick={() => dismiss(toast.id)} aria-label="Dismiss">
          ✕
        </button>
      </div>
    </div>
  )
}

export function ToastStack() {
  const toasts = useAgent((s) => s.toasts)
  if (toasts.length === 0) return null
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-[60] flex w-80 flex-col gap-2">
      {toasts.map((t) => (
        <ToastCard key={t.id} toast={t} />
      ))}
    </div>
  )
}

/** Polls /api/agents: keeps the store's agent map fresh (the Composer gates
 *  on it) and raises run toasts — failures always, successes per-agent. The
 *  first pass after boot only marks, so old completions don't toast. */
export function AgentRunWatcher() {
  const refreshAgents = useAgent((s) => s.refreshAgents)
  useEffect(() => {
    let alive = true
    const poll = async () => {
      await refreshAgents()
      if (!alive) return
      const { agents, agentsToastedThrough, setAgentsToastedThrough, pushToast } =
        useAgent.getState()
      for (const a of agents) {
        if (!a.last_finished_at || a.last_status === 'running') continue
        const seen = agentsToastedThrough[a.id]
        if (seen === undefined) {
          setAgentsToastedThrough(a.id, a.last_finished_at)
          continue
        }
        if (a.last_finished_at === seen) continue
        setAgentsToastedThrough(a.id, a.last_finished_at)
        if (a.last_status === 'error') {
          pushToast({
            kind: 'error',
            title: `Agent "${a.name}" run failed`,
            body: 'Open its pinned chat for the transcript.',
          })
        } else if (a.notify_on_success) {
          pushToast({ kind: 'success', title: `Agent "${a.name}" run finished` })
        }
      }
    }
    void poll()
    const t = window.setInterval(() => void poll(), 5000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [refreshAgents])
  return null
}

/**
 * Live follow for agent chats: a scheduled run streams inside the backend —
 * nothing pushes its events to the frontend — so the open chat only updates
 * by reloading history. While the pinned agent of the on-screen chat is
 * mid-run, re-pull the transcript at a fast clip and drain the backend's
 * tape buffer into the live telemetry tape (getAgentTape resumes by offset,
 * so only new events flow). One final pull of each when the run ends, so
 * the closing summary isn't cut off by the interval boundary. loadHistory
 * skips the reload while a user-started run owns the buffer.
 */
export function AgentChatLiveFollow() {
  const conversationId = useAgent((s) => s.conversationId)
  const agents = useAgent((s) => s.agents)
  const running =
    conversationId !== null &&
    agents.some((a) => a.running && a.conversation_id === conversationId)
  useEffect(() => {
    if (!running || conversationId === null) return
    let alive = true
    // Fresh fire: drop the previous run's tape before the new events land.
    useAgent.getState().resetTape(String(conversationId))
    useAgent.getState().clearCompaction(String(conversationId))
    let offset = 0
    const drainTape = async (final = false) => {
      const res = await getAgentTape(conversationId, offset)
      // `final` still lands after cleanup flipped `alive` — the closing
      // tool_result events must reach the tape too.
      if (!alive && !final) return
      offset = res.offset
      const appendTape = useAgent.getState().appendTape
      const syncTapeQuestion = useAgent.getState().syncTapeQuestion
      for (const ev of res.events) {
        const chunk = tapeChunkForEvent(ev as AgentEvent)
        if (chunk) appendTape(String(conversationId), chunk)
        // #93: a taped ask_user opens the live question card (and its
        // tool_result / turn-terminal events close it) — same store the
        // interactive stream writes, so the answer path is identical.
        syncTapeQuestion(tapeQuestionAction(ev as AgentEvent, String(conversationId)))
      }
      // Run over: the final pull must still have cleared any card the run
      // left behind (the tape's closing events do this; belt-and-braces).
      if (!res.running) {
        useAgent.getState().setPendingQuestion((q) =>
          q && q.convKey === String(conversationId) ? null : q,
        )
      }
    }
    const pull = () =>
      getMessages(conversationId)
        .then((rows) => {
          if (alive) useAgent.getState().loadHistory(conversationId, rows)
        })
        .catch(() => {})
    void pull()
    void drainTape().catch(() => {})
    const t = window.setInterval(pull, 2500)
    const tapeTimer = window.setInterval(() => void drainTape().catch(() => {}), 500)
    return () => {
      alive = false
      window.clearInterval(t)
      window.clearInterval(tapeTimer)
      getMessages(conversationId)
        .then((rows) => useAgent.getState().loadHistory(conversationId, rows))
        .catch(() => {})
      void drainTape(true).catch(() => {})
    }
  }, [running, conversationId])
  return null
}

/** Settings card: the GLOBAL scheduled-run retry preference (issue #41). */
function AgentsSettingsSection() {
  const [rc, setRc] = useState('2')
  const [rb, setRb] = useState('5')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    listAgents()
      .then((p) => {
        setRc(String(p.retry.retry_count))
        setRb(String(p.retry.retry_backoff_minutes))
      })
      .catch(() => {})
  }, [])

  const save = async () => {
    await setAgentRetry(parseInt(rc, 10) || 0, parseInt(rb, 10) || 5)
    setSaved(true)
    window.setTimeout(() => setSaved(false), 1500)
  }

  return (
    <>
      <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        Scheduled agents
      </h3>
      <p className="mb-2 text-[10px] leading-relaxed text-zinc-600">
        Agents are created from the silhouette icon on each workspace in the sidebar. Failed runs
        retry with backoff, globally:
      </p>
      <div className="flex items-center gap-1.5">
        <label className="text-[10px] text-zinc-500">
          retry
          <input
            className={`ml-1 w-12 ${agentInputCls}`}
            value={rc}
            onChange={(e) => setRc(e.target.value)}
            aria-label="Retry count"
          />
        </label>
        <label className="text-[10px] text-zinc-500">
          times, backoff
          <input
            className={`ml-1 w-12 ${agentInputCls}`}
            value={rb}
            onChange={(e) => setRb(e.target.value)}
            aria-label="Backoff minutes"
          />
        </label>
        <span className="text-[10px] text-zinc-500">min</span>
        <button
          className="ml-auto rounded border border-zinc-700 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
          onClick={() => void save()}
        >
          {saved ? 'Saved ✓' : 'Save'}
        </button>
      </div>
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
  const [maxSteps, setMaxSteps] = useState<number | ''>('')
  const [activeTab, setActiveTab] = useState<'general' | 'providers' | 'voice' | 'mcp'>('general')
  // Per-model context-window overrides (model id -> tokens); blank = auto.
  const [ctxOverrides, setCtxOverrides] = useState<Record<string, number>>({})
  const [ctxModelDraft, setCtxModelDraft] = useState('')
  const [ctxTokensDraft, setCtxTokensDraft] = useState<number | ''>('')
  // History compaction: on/off + absolute trigger threshold in k tokens
  // (the field holds 250 for 250k; 0/blank = fraction-of-window only).
  const [compactionEnabled, setCompactionEnabled] = useState(true)
  const [compactionTriggerK, setCompactionTriggerK] = useState<number | ''>('')
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
  // Notification chimes (#29): mute toggle, default ON.
  const [soundsEnabled, setSoundsUi] = useState(true)
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
          setMaxSteps(c.max_steps ?? '')
          setCtxOverrides(c.context_window_overrides ?? {})
          setCompactionEnabled(c.compaction?.enabled !== false)
          setCompactionTriggerK(c.compaction?.trigger_tokens ? c.compaction.trigger_tokens / 1000 : '')
          setUiScale(Number(c.ui_scale) || 1.0)
          const v = c.voice
          setVoiceEngine(v?.engine === 'cloud' ? 'cloud' : 'local')
          setCloudEndpoint(v?.cloud_endpoint ?? '')
          setCloudKeySaved(v?.cloud_api_key === 'set')
          setCloudModel(v?.cloud_model || '')
          setPttHotkeyDraft(v?.ptt_hotkey ?? '')
          setTtsVoiceDraft(v?.tts_voice || 'af_heart')
          setTtsSpeedDraft(v?.tts_speed ?? 1.0)
          setSoundsUi(v?.sounds_enabled !== false)
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
        max_steps: maxSteps === '' ? undefined : Number(maxSteps),
        context_window_overrides: ctxOverrides,
        compaction: {
          enabled: compactionEnabled,
          trigger_tokens: compactionTriggerK === '' ? 0 : Number(compactionTriggerK) * 1000,
        },
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
          sounds_enabled: soundsEnabled,
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
      // Provider/model changes can affect the defaults inherited by new chats;
      // refresh the sidebar's model and thought-level controls.
      useAgent.getState().refreshGlobals()
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

        <div
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-zinc-800 px-4"
          role="tablist"
          aria-label="Settings sections"
        >
          {([
            ['general', 'General'],
            ['providers', 'Providers'],
            ['voice', 'Voice'],
            ['mcp', 'MCP'],
          ] as const).map(([tab, label]) => (
            <button
              key={tab}
              type="button"
              role="tab"
              id={`settings-tab-${tab}`}
              aria-selected={activeTab === tab}
              aria-controls="settings-panel"
              tabIndex={activeTab === tab ? 0 : -1}
              onClick={() => setActiveTab(tab)}
              onKeyDown={(e) => {
                const tabs = ['general', 'providers', 'voice', 'mcp'] as const
                const index = tabs.indexOf(tab)
                const next = e.key === 'ArrowRight'
                  ? (index + 1) % tabs.length
                  : e.key === 'ArrowLeft'
                    ? (index - 1 + tabs.length) % tabs.length
                    : e.key === 'Home'
                      ? 0
                      : e.key === 'End'
                        ? tabs.length - 1
                        : -1
                if (next >= 0) {
                  e.preventDefault()
                  const nextTab = tabs[next]
                  setActiveTab(nextTab)
                  document.getElementById(`settings-tab-${nextTab}`)?.focus()
                }
              }}
              className={`shrink-0 border-b-2 px-3 py-2.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-blue-500 ${
                activeTab === tab
                  ? 'border-blue-500 text-zinc-100'
                  : 'border-transparent text-zinc-500 hover:text-zinc-300'
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        <div
          className="min-h-0 flex-1 overflow-y-auto p-4"
          role="tabpanel"
          id="settings-panel"
          aria-labelledby={`settings-tab-${activeTab}`}
          tabIndex={0}
        >
          <div className="grid grid-cols-4 gap-3">
            {/* providers: collapsed rows, active first; fields behind one open row */}
            {activeTab === 'providers' && (
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
            )}

            {activeTab === 'general' && (
              <SettingsCard title="Agent & context" className="col-span-4">
              <div className="max-w-sm">
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

              <div className="mt-2.5 border-t border-zinc-800 pt-2.5">
                <label className="mb-1 block text-[10px] text-zinc-500">History compaction</label>
                <div className="flex items-center gap-2.5">
                  <button
                    type="button"
                    role="switch"
                    aria-checked={compactionEnabled}
                    aria-label="Enable history compaction"
                    className={`relative h-4 w-8 shrink-0 rounded-full transition-colors ${
                      compactionEnabled ? 'bg-blue-600' : 'bg-zinc-700'
                    }`}
                    onClick={() => setCompactionEnabled((v) => !v)}
                  >
                    <span
                      className={`absolute top-0.5 h-3 w-3 rounded-full bg-zinc-100 transition-all ${
                        compactionEnabled ? 'left-4.5' : 'left-0.5'
                      }`}
                    />
                  </button>
                  <span className="text-[10px] text-zinc-400">
                    {compactionEnabled ? 'On' : 'Off'} — fold old history into a summary when the prompt grows past
                  </span>
                  <input
                    type="number"
                    min="0"
                    aria-label="Compaction trigger threshold in thousands of tokens"
                    className={`${settingsInputCls} w-20 shrink-0`}
                    value={compactionTriggerK}
                    onChange={(e) => setCompactionTriggerK(e.target.value === '' ? '' : Number(e.target.value))}
                  />
                  <span className="text-[10px] text-zinc-600">k tokens (0 = 70% of the model's window)</span>
                </div>
                <p className="mt-1 text-[10px] text-zinc-600">
                  An absolute threshold never fires before the model's window allows: the trigger is the smaller of
                  the threshold and 70% of the window, so small-window models still compact before overflowing.
                </p>
              </div>
            </SettingsCard>
            )}

            {activeTab === 'voice' && (
              <>
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

            <SettingsCard title="Notification sounds" className="col-span-2">
              <label className="flex items-center gap-2 text-xs text-zinc-300">
                <input
                  type="checkbox"
                  className="accent-blue-600"
                  checked={soundsEnabled}
                  onChange={(e) => {
                    setSoundsEnabled(e.target.checked)
                    setSoundsUi(e.target.checked)
                  }}
                />
                Chime when a run finishes and when a question needs an answer
              </label>
              <p className="mt-1.5 text-[10px] leading-relaxed text-zinc-600">
                Run-finished chimes for every chat, background ones included. The
                question chime only plays while the window is unfocused — when
                focused, the card itself is the signal.
              </p>
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
            </>
            )}

            {activeTab === 'general' && (
              <>
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

            <SettingsCard title="Scheduled agents" className="col-span-4">
              <AgentsSettingsSection />
            </SettingsCard>
            </>
            )}

            {activeTab === 'mcp' && (
              <SettingsCard title="MCP tool servers" className="col-span-4">
              <McpSection />
            </SettingsCard>
            )}
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
const imageSrc = (img: string) => {
  if (img.startsWith('data:')) return img
  if (img.startsWith('remote-image:')) {
    const [, hostId, ...parts] = img.split(':')
    return remoteDeviceImageUrl(hostId, parts.join(':'))
  }
  return imageUrl(img)
}

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
 *  a compact Git control that opens detailed sync state and Git commands.
 *  Hidden entirely for non-repos.
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
  agentBranch,
}: {
  info: GitInfo | null
  streaming: boolean
  conversationId: number | null
  onCommandDone: () => void
  agentBranch: AgentBranchInfo | null
}) {
  const [menuOpen, setMenuOpen] = useState(false)
  const [detailsOpen, setDetailsOpen] = useState(false)
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
    setDetailsOpen(false)
  }, [conversationId])

  // Click-outside closes any open popover/menu.
  useEffect(() => {
    if (!menuOpen && !detailsOpen && !commitOpen && !confirmAction) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
        setDetailsOpen(false)
        setCommitOpen(false)
        setConfirmAction(null)
      }
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [menuOpen, detailsOpen, commitOpen, confirmAction])

  if (!info) return null

  const busy = busyAction !== null
  const lockMutations = streaming || busy

  const openMenu = () => {
    setDetailsOpen(false)
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

  // Mid-run AND between-turns isolation (adr/0003 revised): the primary
  // selector continues to represent the primary tree, while this separate
  // indicator reports the conversation's agent checkout. Sub-agents are
  // excluded — the parent turn owns the binding.
  const showAgentBranch = Boolean(agentBranch)
  // An explicit git_merge_back landed the agent branch in the primary tree;
  // the status indicator remains visible and switches to a neutral merged state.
  const agentMerged = Boolean(agentBranch?.merged)
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
      {/* Primary branch selector: always represents the user's working tree. */}
      <button
        className="flex shrink-0 items-center gap-1 rounded border border-zinc-700 bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300 hover:border-zinc-500"
        title={
          info.dirty
            ? `Primary working-tree branch. ${info.changed} changed file${info.changed === 1 ? '' : 's'} (${info.untracked} untracked). Click to switch branch.`
            : 'Primary working-tree branch — click to switch'
        }
        aria-label="Primary branch; switch branch"
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

      {showAgentBranch && (
        <span
          className={`flex min-w-0 items-center gap-1 rounded border px-1.5 py-0.5 font-mono text-[10px] ${
            agentMerged
              ? 'border-zinc-700 bg-zinc-800/40 text-zinc-400'
              : 'border-amber-500/50 bg-amber-500/10 text-amber-300'
          }`}
          title={
            `Agent checkout branch ${agentBranch!.branch}` +
            (agentBranch!.baseBranch ? `, based on ${agentBranch!.baseBranch}` : '') +
            `, worktree ${agentBranch!.worktreeId ?? conversationId ?? 'unknown'}` +
            (agentMerged ? `, merged into ${info.branch}; checkout remains active` : `, not yet merged into ${info.branch}`)
          }
          aria-label={`Agent checkout: branch ${agentBranch!.branch}, based on ${agentBranch!.baseBranch ?? 'unknown'}, worktree ${agentBranch!.worktreeId ?? conversationId ?? 'unknown'}${agentMerged ? ', merged' : ', unmerged'}`}
        >
          <span className={`inline-block h-1.5 w-1.5 shrink-0 rounded-full ${agentMerged ? 'bg-zinc-500' : 'run-pulse bg-amber-400'}`} aria-hidden="true" />
          <span className="shrink-0 text-zinc-500">AGENT</span>
          <span className="min-w-0 max-w-[9rem] truncate font-semibold">{agentBranch!.branch}</span>
          {agentMerged && <span className="shrink-0 text-zinc-300">merged</span>}
        </span>
      )}

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

      <button
        className="shrink-0 rounded border border-zinc-700 px-1.5 py-0.5 font-mono text-[10px] text-zinc-500 hover:border-zinc-500 hover:text-zinc-200"
        title="Git details and actions"
        aria-label="Git details and actions"
        aria-expanded={detailsOpen}
        onClick={() => {
          setMenuOpen(false)
          setDetailsOpen((open) => !open)
        }}
      >
        git{info.ahead > 0 && <span className="ml-1 text-amber-400">↑{info.ahead}</span>}{info.behind > 0 && <span className="ml-0.5 text-amber-400">↓{info.behind}</span>}
      </button>

      {detailsOpen && (
        <div className="absolute bottom-full right-0 z-30 mb-1 w-80 max-w-[calc(100vw-2rem)] rounded border border-zinc-700 bg-zinc-900 p-2 shadow-[0_20px_25px_-5px_rgba(0,0,0,0.1),0_8px_10px_-6px_rgba(0,0,0,0.1)]">
          <div className="mb-2 flex items-center justify-between border-b border-zinc-800 pb-1.5">
            <span className="font-mono text-[10px] uppercase tracking-wider text-zinc-400">Git details</span>
            <span className="font-mono text-[10px] text-zinc-500">{info.branch}</span>
          </div>
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[10px]">
            {info.added + info.deleted > 0 && (
              <span title={`${info.added} added / ${info.deleted} deleted lines (uncommitted, tracked files)`}>
                <span className="text-emerald-500/90">+{info.added}</span>{' '}
                <span className="text-red-400/90">−{info.deleted}</span>
              </span>
            )}
            {info.local_hash && (
              <span className={pairColor} title={pairTitle}>
                <button className="hover:text-zinc-200" title={copied === 'local' ? 'copied' : `copy ${info.local_hash}`} onClick={() => copyHash(info.local_hash!, 'local')}>
                  {copied === 'local' ? '✓' : info.local_hash}
                </button>
                <span className="text-zinc-700">·</span>
                <button className="hover:text-zinc-200" title={copied === 'remote' ? 'copied' : info.remote_hash ? `copy ${info.remote_hash}` : 'no upstream'} onClick={() => info.remote_hash && copyHash(info.remote_hash, 'remote')}>
                  {copied === 'remote' ? '✓' : info.remote_hash ?? '—'}
                </button>
                {info.ahead > 0 && <span> ↑{info.ahead}</span>}
                {info.behind > 0 && <span> ↓{info.behind}</span>}
              </span>
            )}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1 border-t border-zinc-800 pt-1.5">
            <button className={cmdBtn} disabled={busyAction !== null} title="git status" onClick={() => run('status')}>
              status{busyAction === 'status' && <span className="run-pulse text-amber-300"> ●</span>}
            </button>
            <button className={cmdBtn} disabled={lockMutations} title={streaming ? 'agent is working — wait for the turn to end' : 'stage all + commit'} onClick={() => { setConfirmAction(null); setCommitOpen(true) }}>
              commit{busyAction === 'commit' && <span className="run-pulse text-amber-300"> ●</span>}
            </button>
            {confirmAction === 'push' ? (
              <span className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">
                push to {info.upstream ?? `origin/${info.branch}`}?{' '}
                <button className="text-blue-400 hover:text-blue-300" title="confirm push" onClick={() => run('push')}>✓</button>
                <button className="text-zinc-500 hover:text-zinc-300" title="cancel" onClick={() => setConfirmAction(null)}>✕</button>
              </span>
            ) : (
              <button className={cmdBtn} disabled={lockMutations} title={streaming ? 'agent is working — wait for the turn to end' : info.upstream ? `push to ${info.upstream}` : 'push (sets upstream on first push)'} onClick={() => setConfirmAction('push')}>
                push{busyAction === 'push' && <span className="run-pulse text-amber-300"> ●</span>}
              </button>
            )}
            {confirmAction === 'pull' ? (
              <span className="flex items-center gap-1 rounded border border-zinc-700 bg-zinc-800/60 px-1.5 py-0.5 font-mono text-[10px] text-zinc-300">
                pull {info.upstream ? `from ${info.upstream}` : '(no upstream)'}?{' '}
                <button className="text-blue-400 hover:text-blue-300" title="confirm pull" onClick={() => run('pull')}>✓</button>
                <button className="text-zinc-500 hover:text-zinc-300" title="cancel" onClick={() => setConfirmAction(null)}>✕</button>
              </span>
            ) : (
              <button className={cmdBtn} disabled={lockMutations} title={streaming ? 'agent is working — wait for the turn to end' : info.upstream ? `pull from ${info.upstream}` : 'pull (no upstream set)'} onClick={() => setConfirmAction('pull')}>
                pull{busyAction === 'pull' && <span className="run-pulse text-amber-300"> ●</span>}
              </button>
            )}
          </div>
        </div>
      )}


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

export /**
 * Draft destination picker (issue #90): keep the next chat's destination
 * directly selectable while preserving its draft-local pin until first send.
 * Disappears once the chat is saved — the sidebar grouping takes over.
 */
function DraftDestinationCard() {
  const workspace = useAgent((s) => s.workspace)
  const draftDestination = useAgent((s) => s.draftDestination)
  const pinDraftDestination = useAgent((s) => s.pinDraftDestination)
  const devices = useRemote((s) => s.devices)
  const scope = useRemote((s) => s.scope)
  const [rows, setRows] = useState<WorkspaceRow[]>([])
  const [remoteAdd, setRemoteAdd] = useState(false)
  const [remotePath, setRemotePath] = useState('')
  const [err, setErr] = useState<string | null>(null)
  const [gitBranch, setGitBranch] = useState<string | null>(null)
  const [gitBranches, setGitBranches] = useState<string[]>([])
  const [gitMenuOpen, setGitMenuOpen] = useState(false)
  const [gitLoading, setGitLoading] = useState(false)
  const [gitBusy, setGitBusy] = useState(false)
  const [gitError, setGitError] = useState<string | null>(null)

  // The live destination: pinned value, else the active workspace ('' =
  // Default, the no-root pseudo-workspace).
  const dest = draftDestination ?? workspace
  const destRef = useRef(dest)
  destRef.current = dest

  useEffect(() => {
    let cancelled = false
    setGitBranch(null)
    setGitBranches([])
    setGitMenuOpen(false)
    setGitLoading(false)
    setGitBusy(false)
    setGitError(null)
    if (!dest || dest.startsWith('remote:')) return
    getWorkspaceGitBranches(dest)
      .then((result) => {
        if (!cancelled) {
          setGitBranch(result.branch)
          setGitBranches(result.branches)
        }
      })
      .catch(() => {
        if (!cancelled) {
          setGitBranch(null)
          setGitBranches([])
        }
      })
    return () => { cancelled = true }
  }, [dest])

  useEffect(() => {
    let cancelled = false
    const load = parseNsWorkspace(dest) ? listWorkspaces() : listLocalWorkspaces()
    load
      .then((r) => {
        if (!cancelled) setRows(r)
      })
      .catch(() => {
        if (!cancelled) setRows([])
      })
    return () => {
      cancelled = true
    }
  }, [dest, devices])

  const pick = (path: string) => {
    if (path.startsWith('remote:')) {
      const hostId = parseNsWorkspace(path)?.hostId
      const device = devices.find((item) => item.host_id === hostId)
      if (!device || device.status !== 'online') {
        setErr('Reconnect this device in the sidebar before using its workspace.')
        return
      }
    }
    if (path === dest) {
      pinDraftDestination(path)
      return
    }
    pinDraftDestination(path)
    setErr(null)
  }

  const addFolder = async () => {
    if (parseNsWorkspace(dest)) {
      setRemoteAdd(true)
      return
    }
    try {
      const { invoke } = await import('@tauri-apps/api/core')
      const picked = await invoke<string | null>('pick_workspace')
      if (picked) {
        await addWorkspace(picked).catch(() => null)
        pick(picked)
        setRows((current) => current.some((w) => w.path === picked)
          ? current
          : [...current, { id: -1, path: picked, label: wsBasename(picked), last_opened_at: null, exists: true, conversation_count: 0 }])
      }
    } catch {
      setErr('Folder picking needs the desktop app.')
    }
  }

  const addRemote = async () => {
    const path = remotePath.trim()
    const ownerId = parseNsWorkspace(dest)?.hostId
    const device = devices.find((item) => item.host_id === ownerId)
    if (!path || !ownerId || device?.status !== 'online') {
      setErr('Reconnect this device before adding a workspace to it.')
      return
    }
    const namespacedPath = nsWorkspace(ownerId, path)
    try {
      await addWorkspace(namespacedPath, ownerId)
      pick(namespacedPath)
      setRows((current) => current.some((w) => w.path === namespacedPath)
        ? current
        : [...current, { id: -1, path: namespacedPath, label: wsBasename(path), last_opened_at: null, exists: true, conversation_count: 0, owner_id: ownerId, device_status: 'online' }])
      setRemoteAdd(false)
      setRemotePath('')
    } catch (e) {
      setErr(String((e as Error).message ?? e).replace(/^\d+:\s*/, ''))
    }
  }

  const destinationInRows = rows.some((w) => w.path === dest)

  const openGitBranches = async () => {
    if (!dest) return
    setGitMenuOpen(true)
    setGitLoading(true)
    setGitError(null)
    try {
      const result = await getWorkspaceGitBranches(dest)
      if (destRef.current !== dest) return
      setGitBranch(result.branch)
      setGitBranches(result.branches)
    } catch (e) {
      if (destRef.current === dest) {
        setGitError(String((e as Error).message ?? e).replace(/^\d+:\s*/, ''))
        setGitBranches([])
      }
    } finally {
      if (destRef.current === dest) setGitLoading(false)
    }
  }

  const checkoutDraftBranch = async (branch: string) => {
    if (!dest || gitBusy) return
    setGitBusy(true)
    setGitError(null)
    try {
      const result = await checkoutWorkspaceBranch(dest, branch)
      if (destRef.current !== dest) return
      if (!result.ok) {
        setGitError(result.error || 'Branch checkout failed')
        return
      }
      setGitBranch(branch)
      setGitMenuOpen(false)
    } catch (e) {
      if (destRef.current === dest) {
        setGitError(String((e as Error).message ?? e).replace(/^\d+:\s*/, ''))
      }
    } finally {
      if (destRef.current === dest) setGitBusy(false)
    }
  }

  return (
    <div className="mx-auto mt-3 w-full max-w-md rounded border border-zinc-800 bg-zinc-900/60 px-3 py-2">
      <div className="flex items-center gap-2">
        <label htmlFor="draft-destination" className="shrink-0 text-xs text-zinc-400">
          This chat will be saved to
        </label>
        <select
          id="draft-destination"
          aria-label="Save this chat to"
          className="min-w-0 flex-1 truncate rounded border border-zinc-700 bg-zinc-800 px-2 py-1 text-xs text-zinc-200 focus:border-blue-500 focus:outline-none"
          value={dest}
          onChange={(e) => pick(e.target.value)}
          title={dest || 'Default (no folder)'}
        >
          <option value="">Default (no folder)</option>
          {dest && !destinationInRows && (
            <option value={dest}>{wsBasename(parseNsWorkspace(dest)?.path ?? dest)}</option>
          )}
          {rows.filter((w) => w.path !== null && (!w.owner_id || w.device_status === 'online')).map((w) => (
            <option key={w.path} value={w.path!}>{w.owner_id ? `${devices.find((d) => d.host_id === w.owner_id)?.name ?? 'Device'} · ${w.label}` : w.label}</option>
          ))}
        </select>
        {gitBranch && !parseNsWorkspace(dest) && (
          <div className="relative shrink-0">
            <button
              type="button"
              aria-label={`Branch ${gitBranch}`}
              title="This switches the branch for every chat sharing this workspace"
              disabled={gitBusy}
              onClick={() => gitMenuOpen ? setGitMenuOpen(false) : void openGitBranches()}
              className="rounded border border-zinc-700 px-2 py-1 font-mono text-[10px] text-zinc-300 hover:bg-zinc-800 disabled:opacity-50"
            >
              ⎇ {gitBranch}
            </button>
            {gitMenuOpen && (
              <div role="menu" aria-label="Git branches" className="absolute right-0 top-full z-30 mt-1 max-h-48 min-w-36 overflow-auto rounded border border-zinc-700 bg-zinc-900 p-1 shadow-xl">
                {gitLoading ? <div className="px-2 py-1 text-[10px] text-zinc-500">Loading branches…</div> :
                  gitBranches.map((branch) => (
                    <button
                      key={branch}
                      type="button"
                      role="menuitem"
                      disabled={gitBusy || branch === gitBranch}
                      onClick={() => void checkoutDraftBranch(branch)}
                      className="flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left font-mono text-[10px] text-zinc-200 hover:bg-zinc-800 disabled:opacity-50"
                    >
                      <span>{branch}</span><span>{branch === gitBranch ? '✓' : ''}</span>
                    </button>
                  ))}
                {!gitLoading && gitBranches.length === 0 && <div className="px-2 py-1 text-[10px] text-zinc-500">No local branches</div>}
              </div>
            )}
          </div>
        )}
        {!remoteAdd && (
          <button
            className="shrink-0 rounded border border-dashed border-zinc-700 px-2 py-1 text-xs text-zinc-400 hover:border-zinc-500 hover:bg-zinc-800/60 hover:text-zinc-200"
            onClick={() => void addFolder()}
            aria-label={parseNsWorkspace(dest) ? `Add folder on ${devices.find((d) => d.host_id === parseNsWorkspace(dest)?.hostId)?.name ?? 'remote device'}` : 'Add workspace'}
            title={parseNsWorkspace(dest) ? 'Add folder on selected remote device' : 'Add workspace'}
          >
            <span aria-hidden="true" className="text-sm leading-none">+</span>
          </button>
        )}
      </div>
      {remoteAdd && (
        <div className="mt-1.5 rounded border border-zinc-700 bg-zinc-800 p-2">
          <input
            autoFocus
            className="mb-1.5 w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 font-mono text-xs"
            placeholder="folder path on the host, e.g. C:/repos/proj"
            value={remotePath}
            onChange={(e) => setRemotePath(e.target.value)}
            onKeyDown={(e) => e.key === 'Escape' && setRemoteAdd(false)}
            aria-label="Folder path on the host"
          />
          <div className="flex justify-end gap-1.5">
            <button
              className="rounded border border-zinc-700 px-2 py-0.5 text-[10px] text-zinc-400 hover:bg-zinc-900"
              onClick={() => setRemoteAdd(false)}
            >
              Cancel
            </button>
            <button
              className="rounded bg-blue-600 px-2 py-0.5 text-[10px] text-white hover:bg-blue-500 disabled:opacity-50"
              disabled={!remotePath.trim()}
              onClick={() => void addRemote()}
            >
              Add
            </button>
          </div>
        </div>
      )}
      {err && <p className="mt-1 text-[10px] text-red-400">{err}</p>}
      {gitError && <p role="alert" className="mt-1 text-[10px] text-red-400">{gitError}</p>}
    </div>
  )
}

/** #51/#76 — the per-chat model + effort pickers, rendered in the ChatPanel
 *  header. One component for drafts and saved chats alike:
 *  - draft (conversationId null): edits go to the store's draftScope, which
 *    the first send writes into the new conversation row.
 *  - normal chat: edits PATCH /api/conversations/{id} (model/effort columns).
 *  - agent-pinned chat: edits write through to the owning agent
 *    (PATCH /api/agents/{id}/model-effort) — the chat cannot desync.
 *  Selectors disable while this chat streams (a turn always finishes on what
 *  it started with) or while a write is in flight. Provider-down turns fail
 *  visibly in the transcript; the down notes show right in the picker row. */
export function ChatScopePickers() {
  const conversationId = useAgent((s) => s.conversationId)
  const status = useStatus()
  const streaming = status === 'thinking' || status === 'running-tool'
  const agents = useAgent((s) => s.agents)
  const refreshAgents = useAgent((s) => s.refreshAgents)
  const owner = conversationId === null ? null : agents.find((a) => a.conversation_id === conversationId && a.id) ?? null
  const isAgentChat = owner !== null
  // Agent chats stay editable (write-through) — only a LIVE agent run locks.
  const agentRunLive = isAgentChat && Boolean(owner?.running)
  const locked = streaming || agentRunLive

  const draftScope = useAgent((s) => s.draftScope)
  const setDraftScope = useAgent((s) => s.setDraftScope)
  const globalModel = useAgent((s) => s.globalModel)
  const globalEffort = useAgent((s) => s.globalEffort)

  const { byProvider, refresh } = useModelList()

  // The conversation row's pinned scope, refreshed on chat switch and after
  // each turn (an agent run may have written through the agent).
  const [rowScope, setRowScope] = useState<{ model: string; effort: string } | null>(null)
  useEffect(() => {
    setRowScope(null)
    if (conversationId === null) return
    let cancelled = false
    getConversation(conversationId)
      .then((c) => {
        if (!cancelled) setRowScope({ model: c.model || '', effort: c.effort || '' })
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [conversationId, streaming])

  const model =
    conversationId === null
      ? draftScope?.model ?? globalModel
      : isAgentChat
        ? owner?.model ?? ''
        : rowScope?.model ?? globalModel
  const effort =
    conversationId === null
      ? draftScope?.effort ?? globalEffort
      : isAgentChat
        ? owner?.effort ?? ''
        : rowScope?.effort ?? globalEffort

  const activeProviderFor = (m: string) => {
    for (const [name, pm] of Object.entries(byProvider)) {
      if (pm.models.includes(m)) return name
    }
    return ''
  }
  const parsedModel = parseModelScope(model)
  const shownProvider = parsedModel.provider || activeProviderFor(model)
  const shownModel = parsedModel.model

  const [savingModel, setSavingModel] = useState(false)
  const [savingEffort, setSavingEffort] = useState(false)

  const applyModel = (value: string) => {
    const idx = value.indexOf('::')
    if (idx < 0) return
    const provider = value.slice(0, idx)
    const m = value.slice(idx + 2)
    if (!m || (m === shownModel && provider === shownProvider)) return
    const newModel = `${provider}::${m}`
    const nextEffort = modelReasoningEfforts(byProvider, newModel).includes(effort) ? effort : ''
    if (conversationId === null) {
      setDraftScope({ model: newModel, effort: nextEffort })
      return
    }
    setSavingModel(true)
    const write = isAgentChat
      ? updateAgentModelEffort(owner.id, newModel, nextEffort)
      : updateConversation(conversationId, { model: newModel, effort: nextEffort })
    write
      .then(() => {
        if (isAgentChat) refreshAgents()
      })
      .catch(() => {})
      .finally(() => setSavingModel(false))
  }

  const applyEffort = (e: string) => {
    if (e === effort) return
    if (conversationId === null) {
      setDraftScope({ effort: e })
      return
    }
    setSavingEffort(true)
    const write = isAgentChat
      ? updateAgentModelEffort(owner.id, model, e)
      : updateConversation(conversationId, { effort: e })
    write
      .then(() => {
        if (isAgentChat) refreshAgents()
      })
      .catch(() => {})
      .finally(() => setSavingEffort(false))
  }

  const saving = savingModel || savingEffort
  const noProvider = Object.keys(byProvider).length === 0
  const downNotes = Object.entries(byProvider).filter(([, pm]) => pm.error)

  return (
    <div className="flex flex-col gap-0.5 border-b border-zinc-800 bg-zinc-900/60 px-4 py-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wider text-zinc-600">model</span>
        <select
          className="min-w-0 max-w-[16rem] flex-1 truncate rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-zinc-200 focus:border-blue-500 focus:outline-none disabled:opacity-50"
          value={`${shownProvider}::${shownModel}`}
          onChange={(e) => applyModel(e.target.value)}
          disabled={locked || saving || noProvider}
          aria-label="Chat model"
          title={
            isAgentChat
              ? "This chat's model (writes through to the owning agent)"
              : conversationId === null
                ? "This chat's model (pinned at first send)"
                : 'Model for this chat — other chats are unaffected'
          }
        >
          <ModelOptions byProvider={byProvider} value={`${shownProvider}::${shownModel}`} />
        </select>
        <span className="ml-1 text-[10px] uppercase tracking-wider text-zinc-600">effort</span>
        <select
          className="rounded border border-zinc-700 bg-zinc-800 px-1.5 py-0.5 text-[11px] text-zinc-200 focus:border-blue-500 focus:outline-none disabled:opacity-50"
          value={effort}
          onChange={(e) => applyEffort(e.target.value)}
          disabled={locked || saving || !modelSupportsReasoning(byProvider, model)}
          aria-label="Chat reasoning effort"
          title={`${EFFORT_HINT}${isAgentChat ? ' (writes through to the owning agent)' : ''}`}
        >
          <EffortOptions efforts={modelReasoningEfforts(byProvider, model)} />
        </select>
        {isAgentChat && (
          <span
            className="rounded border border-zinc-700 px-1.5 py-0.5 text-[10px] text-zinc-400"
            title="This chat belongs to a scheduled agent — changes apply to the agent"
          >
            agent
          </span>
        )}
        {(savingModel || savingEffort) && (
          <span className="text-[10px] text-zinc-500">saving…</span>
        )}
      </div>
      {/* Provider-state affordances (#51): moved here from the sidebar so
          they sit next to the picker that needs them. */}
      {noProvider && (
        <p className="text-[10px] leading-relaxed text-amber-400">
          No model provider configured — add one in Settings to start.
        </p>
      )}
      {downNotes.map(([name, pm]) => (
        <p key={name} className="text-[10px] leading-relaxed text-amber-400">
          {name}: {pm.error}
        </p>
      ))}
    </div>
  )
}

export function ChatPanel() {
  const conversationId = useAgent((s) => s.conversationId)
  const messages = useAgent(
    (s) => s.messagesByConv[s.conversationId === null ? 'draft' : String(s.conversationId)] ?? [],
  )
  const status = useStatus()
  const error = useError()
  const pendingQuestion = useAgent((s) => {
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    // A question belongs to the turn that asked it: only render when its
    // conversation is on screen (a hidden turn's ask must not leak here).
    return s.pendingQuestions[key] ?? null
  })
  const pendingApproval = useAgent((s) => {
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    // Same screen-scoping as questions: a hidden turn's approval request
    // must not render over an unrelated conversation.
    return s.pendingApprovals[key] ?? null
  })
  const pendingPlanApproval = useAgent((s) => {
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    return s.pendingPlanApprovals[key] ?? null
  })
  // #52: the transcript scrolls in THIS container (the bottomRef div is its
  // last child), so the scroll listener lives here. Force-snap when input is
  // required (question / approval / plan) — that is when the user's eyes are.
  const { containerRef: transcriptRef, onScroll: onTranscriptScroll } = useStickToBottom(
    Boolean(pendingQuestion || pendingApproval || pendingPlanApproval),
    [messages],
  )
  const streaming = status === 'thinking' || status === 'running-tool'
  const agentBranch = useAgentBranch()
  // A scheduled agent run streams inside the backend — no live buffer, the
  // messages arrive by history reload — but its ticker/tape should still
  // show on the newest message while the run is going.
  const agents = useAgent((s) => s.agents)
  const agentRunLive =
    conversationId !== null &&
    agents.some((a) => a.running && a.conversation_id === conversationId)
  // Only the in-flight assistant message shows the ephemeral ticker; every
  // finished turn collapses to the one-line trace. The steer path (#steer)
  // splits after an injected user message, so the last message can be an
  // assistant emission or a user row while the turn keeps running — keying
  // liveness on the absolute tail would flip the active ticker to a finished
  // TraceLine summary mid-run. Key it on the last ASSISTANT message instead.
  const bufKey = conversationId === null ? 'draft' : String(conversationId)
  const liveId = (() => {
    if (!(streaming || agentRunLive) || messages.length === 0) return null
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') return messages[i].id
    }
    return messages[messages.length - 1].id
  })()

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
  // Briefing-first narration (#66 + #75): while an emission streams, its
  // completed sentences are HELD (counted, not appended). When the backend's
  // per-emission `say` briefing arrives, the held verbatim sentences are
  // discarded and only the briefing is appended — the voice channel is a
  // briefing per emission, not a read-aloud. If an emission completes
  // without a usable tag, the held sentences flush verbatim at the swap
  // (before the next emission's utterance claims the player) or at run end
  // — never silence. The emission lane is serialized (#83): each emission
  // is one stream utterance queued on the player's process queue — an
  // emission arriving mid-briefing waits for the current utterance to
  // drain instead of cutting it off mid-word. Only a hard stop (mute /
  // per-message) kills current audio and drops queued emissions.
  const lastMsg = messages.length ? messages[messages.length - 1] : null
  const lastAssistantId = lastMsg && lastMsg.role === 'assistant' ? lastMsg.id : null
  const lastAssistantContent = lastMsg && lastMsg.role === 'assistant' ? lastMsg.content : ''
  const wasStreamingRef = useRef(false)
  const lastSpokenRef = useRef<string | null>(null)
  const narrationRef = useRef<{
    msgId: string
    spoken: number
    said: boolean
    feed: { append: (chunk: string) => void; end: () => void }
  } | null>(null)
  const beginNarration = useTts((s) => s.beginNarration)
  useEffect(() => {
    if (!streaming) return
    wasStreamingRef.current = true
    if (!ttsEnabled || !ttsReady || !lastAssistantId || !lastAssistantContent) return
    let n = narrationRef.current
    if (!n || n.msgId !== lastAssistantId) {
      // Emission swap: flush the previous emission's held sentences (it
      // ended without a usable tag) BEFORE the new utterance is announced.
      // beginStream no longer supersedes (#83): the player queues the new
      // emission until the current utterance drains, so the flushed
      // sentences play through and the new voice starts right after.
      const prev = n
      if (prev) {
        const prevMsg = messages.find((m) => m.id === prev.msgId)
        const chunks = splitSentences(liveProse(prevMsg?.content ?? ''))
        for (let i = prev.spoken; i < chunks.length; i++) prev.feed.append(chunks[i])
      }
      const feed = beginNarration(lastAssistantId)
      if (!feed) return
      n = { msgId: lastAssistantId, spoken: 0, said: false, feed }
      narrationRef.current = n
      lastSpokenRef.current = lastAssistantId
    }
    if (n.said) return
    const msgSay = messages.find((m) => m.id === lastAssistantId)?.say
    if (msgSay != null) {
      // Briefing arrived: drop the held verbatim sentences, speak the
      // briefing alone (spokenLine clamps it to the cap; markdown-free).
      n.said = true
      n.feed.append(spokenLine(msgSay, lastAssistantContent))
      return
    }
    // No tag yet: hold. Only the COUNT tracks here - chunks stay unappended
    // so they can be discarded wholesale when the briefing lands.
    const chunks = splitSentences(liveProse(lastAssistantContent))
    n.spoken = chunks.length
  }, [streaming, lastAssistantId, lastAssistantContent, messages, ttsEnabled, ttsReady, beginNarration])
  // Run finished → close the live narration; fall back to the classic
  // end-of-run read ONLY when nothing was narrated live (e.g. TTS was
  // enabled mid-run). Fires on the streaming→idle transition only (a
  // history load or conversation switch also lands here with
  // streaming=false, but lastSpokenRef guards against re-speaking
  // anything that already played).
  useEffect(() => {
    if (streaming) {
      wasStreamingRef.current = true
      return
    }
    if (!wasStreamingRef.current) return // idle at mount / history load: stay silent
    wasStreamingRef.current = false
    const n = narrationRef.current
    if (n) {
      if (!n.said) {
        // The last emission ended without a briefing: flush its held
        // sentences so the fallback stays verbatim, never silence.
        const msg = messages.find((m) => m.id === n.msgId)
        const chunks = splitSentences(liveProse(msg?.content ?? ''))
        for (let i = n.spoken; i < chunks.length; i++) n.feed.append(chunks[i])
      }
      n.feed.end() // playback drains; nothing new is fed
      narrationRef.current = null
      return
    }
    if (!ttsEnabled || !ttsReady || !lastAssistantId) return
    if (lastSpokenRef.current === lastAssistantId) return
    lastSpokenRef.current = lastAssistantId
    speakMessage(
      lastAssistantId,
      lastAssistantContent,
      messages.find((m) => m.id === lastAssistantId)?.say,
    )
  }, [streaming, lastAssistantId, lastAssistantContent, messages, ttsEnabled, ttsReady, speakMessage])
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

  return (
    <main className="flex min-w-0 flex-1 flex-col">
      {/* #51/#76: per-chat model + effort pickers — the chat's own scope. */}
      <ChatScopePickers />
      <div
        ref={transcriptRef}
        onScroll={onTranscriptScroll}
        className="min-w-0 flex-1 space-y-4 overflow-y-auto p-4"
      >
        {conversationId === null && (
          <DraftDestinationCard />
        )}
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
          agentBranch={agentBranch}
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

/** Execution is selected by each chat workspace, not by a global host toggle. */
function HostSwitcher({ disabled }: { disabled: boolean }) {
  return (
    <div
      className="flex items-center gap-1.5 rounded px-2 py-1.5 text-xs text-zinc-400"
      title="Workspace selection determines where workspace tools run. Local paths always stay on this device."
      aria-label="Workspace-bound execution target"
    >
      <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" aria-hidden="true">
        <rect x="2" y="2.5" width="10" height="4" rx="1" />
        <rect x="2" y="8.5" width="10" height="4" rx="1" />
      </svg>
      Workspace target
      {disabled && <span className="sr-only">A conversation is running.</span>}
    </div>
  )
}

function Composer() {
  const {
    conversationId,
    workspace,
    appendUserMessage,
    appendAssistantPlaceholder,
    appendTextDelta,
    setSay,
    startToolCall,
    appendToolOutput,
    finishToolCall,
    splitAtPlanApproval,
    appendTape,
    startSubAgent,
    subAgentTextDelta,
    subAgentToolStart,
    subAgentToolProgress,
    subAgentToolResult,
    appendSubAgentTelemetry,
    finishSubAgent,
    settleSubAgents,
    setStatus,
    setModelCall,
    setAgentBranch,
    setError,
    setConversationId,
    adoptDraft,
    setPendingQuestion,
    setPendingApproval,
    setPendingPlanApproval,
    setContext,
    pushLog,
    appendRawMessage,
    setCompaction,
    setAbortController,
    removeMessage,
    clearCompaction,
  } = useAgent()
  const status = useStatus()
  // Live ask_user card, for PTT question routing (mirror kept in a ref below
  // so the global-hotkey handlers never go stale).
  const pendingQuestion = useAgent((s) => {
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    return s.pendingQuestions[key] ?? null
  })
  // Same gate pattern for the access-mode approval and plan-approval cards:
  // a pending gate in THIS conversation must block steering/queuing (the
  // composer input is the gate's answer channel).
  const pendingApproval = useAgent((s) => {
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    return s.pendingApprovals[key] ?? null
  })
  const pendingPlanApproval = useAgent((s) => {
    const key = s.conversationId === null ? 'draft' : String(s.conversationId)
    return s.pendingPlanApprovals[key] ?? null
  })
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  // Issue #7 queue: echoes of messages queued during this run (transcript
  // rows marked queued) + pill open state + steer-in-flight flag.
  const bufKeyForQueue = conversationId === null ? 'draft' : String(conversationId)
  const queueEchoes = useAgent((s) => s.queueEchoByConv[bufKeyForQueue])
  const setQueueEcho = useAgent((s) => s.setQueueEcho)
  const dropQueuedEcho = useAgent((s) => s.dropQueuedEcho)
  const markQueuedAsNormal = useAgent((s) => s.markQueuedAsNormal)
  const steering = useAgent((s) => s.steerByConv[bufKeyForQueue] ?? false)
  const setSteerFlag = useAgent((s) => s.setSteer)
  const [queueOpen, setQueueOpen] = useState(false)
  const pendingQueueAutosendRef = useRef<Record<string, Array<{ id: number; text: string; skills?: string[]; images?: string[] }>>>({})
  // Buffer key of the conversation with a send closure in flight (issue #10:
  // several chats can run at once — gates and the Stop button are scoped to
  // the conversation they belong to, not to the whole app).
  const [sendingKey, setSendingKey] = useState<string | null>(null)
  const [attachments, setAttachments] = useState<Attachment[]>([])
  const [images, setImages] = useState<ImageAttachment[]>([])
  const [dragOver, setDragOver] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const streaming = status === 'thinking' || status === 'running-tool'
  // Agent chats (issue #41): the composer files standing instructions, it
  // never starts a run.
  const agentChatByConv = useAgent((s) => s.agentChatByConv)
  const isAgentChat =
    conversationId !== null && Boolean(agentChatByConv[String(conversationId)])

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
  const pttTargetRef = useRef<{ conversationId: number | null; workspace: string } | null>(null)
  const pttBusyRef = useRef(false) // a release is still transcribing/sending
  const prevTitleRef = useRef('')
  const voiceStateRef = useRef<'idle' | 'recording' | 'transcribing'>('idle')
  // PTT routes dictated text to the pending gate rather than steering it.
  // The refs stay live while the global hotkey handler remains mounted.
  const pendingQuestionRef = useRef<PendingQuestion | null>(null)
  const anyQuestionRef = useRef<PendingQuestion | null>(null)
  const anyQuestion = useAgent((s) => Object.values(s.pendingQuestions)[0] ?? null)
  useEffect(() => {
    anyQuestionRef.current = anyQuestion
  }, [anyQuestion])
  const anyApprovalRef = useRef<PendingApproval | null>(null)
  const anyApproval = useAgent((s) => Object.values(s.pendingApprovals)[0] ?? null)
  useEffect(() => {
    anyApprovalRef.current = anyApproval
  }, [anyApproval])
  const anyPlanRef = useRef<PendingPlanApproval | null>(null)
  const anyPlan = useAgent((s) => Object.values(s.pendingPlanApprovals)[0] ?? null)
  useEffect(() => {
    anyPlanRef.current = anyPlan
  }, [anyPlan])
  // Live approval card (same ref pattern: the stream handler owns the store
  // copy; PTT reads a ref so the hotkey handler never goes stale). Unscoped:
  // an approval in a background conversation must still shield its turn from
  // the interrupt below.
  // Live exit_plan card (same ref pattern): a plan waiting for approval must
  // shield its turn from the PTT interrupt, and a dictated answer resolves it.
  // Assigned after `send`/`stop` are declared below (TDZ-safe via refs).
  const sendRef = useRef<(
    text?: string,
    opts?: {
      interrupt?: boolean
      queueHandoff?: boolean
      target?: { conversationId: number | null; workspace: string }
      images?: string[]
      skills?: string[]
    },
  ) => Promise<void>>(
    async () => {},
  )

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
    const targetState = useAgent.getState()
    const pttTarget = {
      conversationId: targetState.conversationId,
      workspace:
        targetState.conversationId === null
          ? (targetState.draftDestination ?? targetState.workspace)
          : targetState.workspace,
    }
    if (voiceStateRef.current === 'transcribing') return
    // Mid-run PTT queues and steers only after a non-empty transcript is
    // available. A gate remains protected and receives dictated answers via
    // the existing release-time routing below.
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
    pttTargetRef.current = pttTarget
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
    // The release is the commit point: capture the destination AND target
    // conversation now, before transcription. A subsequent chat switch must
    // not redirect this recording into whichever chat happens to be visible
    // when transcription finishes.
    const releaseTarget = pttTargetRef.current ?? {
      conversationId: useAgent.getState().conversationId,
      workspace: useAgent.getState().workspace,
    }
    pttTargetRef.current = null
    // Release-time snapshot for the cleanup paths: only a release that was
    // committed on an UNFILED draft owns the pin — clearing must never
    // stomp a pin made later on a different draft (or an adopted chat).
    const ownedUnfiledDraft = releaseTarget.conversationId === null
    const clearOrphanedDraftPin = () => {
      if (!ownedUnfiledDraft) return
      useAgent.getState().clearOrphanedDraftPin()
    }
    setVoiceState('transcribing')
    pttBusyRef.current = true
    try {
      const blob = await rec.stop()
      const { text, language } = await transcribeAudio(blob)
      if (!text) {
        // Empty transcript after real speech: whisper heard noise — stay
        // quiet. The release-time pin must not survive (#88): no draft
        // was filed, so the next typed draft follows the live workspace.
        useAgent.getState().clearOrphanedDraftPin()
        return
      }
      const q = pendingQuestionRef.current
      if (q) {
        const picked = matchOptionLabel(text, q.options.map((o) => o.label), language)
        if (picked === false) {
          clearOrphanedDraftPin()
          pushReject(
            `Heard "${text.trim().slice(0, 40)}" — dictated in a different language than the options; use one of the labels or Something else…`,
          )
          return
        }
        if (picked !== null) {
          clearOrphanedDraftPin()
          window.dispatchEvent(
            new CustomEvent('yaah-answer-ask', { detail: { callId: q.callId, answer: picked } }),
          )
          return
        }
        // No option matched: the transcript IS the free-text answer.
        clearOrphanedDraftPin()
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
        clearOrphanedDraftPin()
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
        clearOrphanedDraftPin()
        window.dispatchEvent(
          new CustomEvent('yaah-answer-plan', { detail: { callId: pl.callId, answer: decision } }),
        )
        return
      }
      const targetKey = releaseTarget.conversationId === null ? 'draft' : String(releaseTarget.conversationId)
      const targetStatus = useAgent.getState().statusByConv[targetKey]
      if ((targetStatus === 'thinking' || targetStatus === 'running-tool') && releaseTarget.conversationId !== null) {
        await steerInput(text.trim(), releaseTarget.conversationId)
      } else {
        void sendRef.current(text, { target: releaseTarget })
      }
    } catch (e) {
      // Transcription failed: nothing was filed, so the release-time pin
      // must not leak into the next typed draft (#88).
      clearOrphanedDraftPin()
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
    // Already bound to exactly this accelerator: re-registering would churn
    // the OS shortcut and toast on every unrelated settings save.
    if (hk === (registeredHotkeyRef.current ?? '')) return
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
  // turn server-side, no duplicate user message) and dismiss. Keyed by the
  // failing conversation's buffer (issue #39): the banner belongs to the chat
  // that failed, so switching chats must hide it — and Resume must target the
  // failed conversation, not whichever chat happens to be on screen.
  const [turnErrorByConv, setTurnErrorByConv] = useState<Record<string, string>>({})
  const screenKey = conversationId === null ? 'draft' : String(conversationId)
  const turnError = turnErrorByConv[screenKey] ?? null
  const setTurnError = (key: string, message: string | null) =>
    setTurnErrorByConv((m) => {
      if (message === null) {
        if (!(key in m)) return m
        const next = { ...m }
        delete next[key]
        return next
      }
      return { ...m, [key]: message }
    })
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
    let awaitingPostSteerEmission = false
    // True while text is allowed to flow without an emission separator: the
    // stream starts mid-emission (first emission of a fresh message), and a
    // tool event closes the emission — the next text opens a new one (#17).
    let textSinceTool = true
    const startPostSteerEmission = (firstText?: string) => {
      if (!awaitingPostSteerEmission) return false
      const nextId = useAgent.getState().startAssistantEmissionAfterUser(bufKey, firstText)
      if (nextId) curId = nextId
      awaitingPostSteerEmission = false
      textSinceTool = true
      return nextId !== null
    }
    return (ev: AgentEvent) => {
    if (ev.type === 'skill_not_found') {
      // The user invoked a skill the backend registry doesn't know (the chip
      // renders from the UI list, which can drift from the backend cache):
      // surface it as a visible system row instead of a silent prompt note.
      appendRawMessage(bufKey, {
        id: `skill-not-found-${(ev.skills ?? []).join('-')}-${Date.now()}`,
        role: 'system',
        content: JSON.stringify({
          skill_not_found: { skills: ev.skills ?? [] },
        }),
      })
    } else if (ev.type === 'text') {
      setStatus(bufKey, 'thinking')
      if (ev.text) {
        const text = textSinceTool ? ev.text : '\n' + ev.text
        if (!startPostSteerEmission(text)) appendTextDelta(bufKey, curId, text)
        textSinceTool = true
      }
    } else if (ev.type === 'say') {
      // Spoken briefing (#66): captured on its message for read-aloud,
      // never rendered.
      startPostSteerEmission()
      setSay(bufKey, curId, ev.say ?? '')
    } else if (ev.type === 'thinking') {
      setStatus(bufKey, 'thinking')
      // Model reasoning flows onto the tape (UI-only; never stored).
      const chunk = tapeChunkForEvent(ev)
      if (chunk) appendTape(bufKey, chunk)
    } else if (ev.type === 'tool_start') {
      startPostSteerEmission()
      setStatus(bufKey, 'running-tool')
      textSinceTool = false
      startToolCall(bufKey, curId, ev.call_id ?? '', ev.name ?? 'tool', ev.args)
      pushLog({ kind: 'tool', name: ev.name, args: ev.args })
      // Telemetry tape: every tool event of the turn flows into one
      // per-conversation line that survives gaps and turn boundaries.
      appendTape(bufKey, tapeChunkForEvent(ev) ?? '')
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
    } else if (ev.type === 'worktree_bound') {
      // The turn isolated into its session worktree: the branch chip
      // shows the agent branch — and keeps showing it after the turn
      // (adr/0003 revised: the binding persists until drain/delete).
      if (ev.branch) {
        const existing = useAgent.getState().agentBranchByConv[bufKey]
        setAgentBranch(
          bufKey,
          existing?.branch === ev.branch
            ? {
                ...existing,
                baseBranch: ev.base_branch || existing.baseBranch,
                worktreeId: ev.worktree_id || existing.worktreeId || String(conversationId),
                boundAt: existing.boundAt,
              }
            : {
                branch: ev.branch,
                baseBranch: ev.base_branch || undefined,
                worktreeId: ev.worktree_id || String(conversationId),
                boundAt: Date.now(),
              },
        )
      }
    } else if (ev.type === 'git_activity') {
      appendRawMessage(bufKey, {
        id: `git-activity-${ev.run_id ?? Date.now()}`,
        role: 'system',
        content: JSON.stringify({
          git_activity: {
            version: 1,
            run_id: ev.run_id ?? '',
            outcome: ev.outcome ?? 'unknown',
            coverage: ev.coverage ?? '',
            lanes: ev.lanes ?? [],
          },
        }),
      })
    } else if (ev.type === 'file_changes') {
      appendRawMessage(bufKey, {
        id: `file-changes-${Date.now()}`,
        role: 'system',
        content: JSON.stringify({
          file_changes: {
            files: ev.files ?? [],
            added: ev.added ?? 0,
            deleted: ev.deleted ?? 0,
          },
        }),
      })
    } else if (ev.type === 'worktree_status') {
      // Turn-end settlement confirms commits remain on the agent branch.
      appendRawMessage(bufKey, {
        id: `worktree-status-${Date.now()}`,
        role: 'system',
        content: JSON.stringify({
          worktree_status: {
            branch: ev.branch ?? '',
            base_branch: ev.base_branch ?? '',
            worktree_id: ev.worktree_id ?? String(conversationId),
            worktree: ev.worktree ?? '',
            commits: ev.commits ?? 0,
            dirty: ev.dirty ?? false,
          },
        }),
      })
      const currentBranch = useAgent.getState().agentBranchByConv[bufKey]
      if (currentBranch) {
        setAgentBranch(bufKey, { ...currentBranch, pendingMerge: true, merged: false })
      }
    } else if (ev.type === 'worktree_released') {
      // Only fires when the session actually released (drained, chat
      // deleted) — not every turn end.
      setAgentBranch(bufKey, null)
      pushLog({
        kind: 'system',
        name: 'worktree',
        result: { released: true, note: 'session worktree removed' },
      })
    } else if (ev.type === 'tool_progress') {
      if (ev.chunk) {
        appendToolOutput(bufKey, curId, ev.call_id ?? '', ev.chunk)
        appendTape(bufKey, tapeChunkForEvent(ev) ?? '')
      }
    } else if (ev.type === 'tool_result') {
      finishToolCall(bufKey, curId, ev.call_id ?? '', ev.result)
      pushLog({ kind: 'tool', name: ev.name, result: ev.result })
      // Close the call's tape segment: response summary + client-measured time.
      {
        const callId = ev.call_id ?? ''
        const msg = (useAgent.getState().messagesByConv[bufKey] ?? []).find((m) => m.id === curId)
        const tc = msg?.toolCalls?.slice().reverse().find((t) => t.id === callId)
        const elapsed =
          tc?.startedAt && tc?.finishedAt
            ? formatElapsed(tc.finishedAt - tc.startedAt)
            : undefined
        appendTape(bufKey, tapeChunkForEvent(ev, elapsed) ?? '')
      }
      if (ev.name === 'git_merge_back') {
        // An explicit merge landed the agent branch in the primary tree:
        // keep its separate indicator visible, but switch it to neutral.
        const r = ev.result as { merged?: boolean } | undefined
        const cur = useAgent.getState().agentBranchByConv[bufKey]
        if (r?.merged && cur) {
          setAgentBranch(bufKey, { ...cur, merged: true, pendingMerge: false })
        }
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
      startPostSteerEmission()
      setStatus(bufKey, 'running-tool')
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
      setStatus(bufKey, 'running-tool')
      startSubAgent(
        bufKey,
        curId,
        ev.call_id ?? '',
        ev.agent_id ?? 0,
        ev.agent_type ?? 'sub-agent',
        ev.prompt ?? '',
      )
      appendTape(bufKey, `\n▸ spawned ${ev.agent_type ?? 'sub-agent'}    `)
      pushLog({ kind: 'tool', name: 'spawn_agent', args: { agent_type: ev.agent_type, prompt: ev.prompt } })
    } else if (ev.type === 'sub_agent_progress') {
      const spawnCallId = ev.call_id ?? ''
      const tapeEvent: AgentEvent = { ...ev, type: ev.kind as AgentEvent['type'] }
      if (ev.kind !== 'tool_result') {
        const tapeChunk = tapeChunkForEvent(tapeEvent)
        if (tapeChunk) appendSubAgentTelemetry(bufKey, curId, spawnCallId, tapeChunk)
      }
      if (ev.kind === 'text' && ev.text) {
        subAgentTextDelta(bufKey, curId, spawnCallId, ev.text)
      }
      if (ev.kind === 'tool_start') {
        subAgentToolStart(bufKey, curId, spawnCallId, ev.tool_call_id ?? '', ev.name ?? 'tool', ev.args)
        appendSubAgentTelemetry(bufKey, curId, spawnCallId, `\n▸ ${ev.name ?? 'tool'}    `)
      } else if (ev.kind === 'tool_progress') {
        if (ev.chunk) subAgentToolProgress(bufKey, curId, spawnCallId, ev.tool_call_id ?? '', ev.chunk)
      } else if (ev.kind === 'tool_result') {
        const msg = (useAgent.getState().messagesByConv[bufKey] ?? []).find((m) => m.id === curId)
        const childTool = msg?.toolCalls?.find((call) => call.id === spawnCallId)?.subAgent?.tools.find((tool) => tool.id === ev.tool_call_id)
        const elapsed = childTool?.startedAt ? formatElapsed(Date.now() - childTool.startedAt) : undefined
        const resultChunk = tapeChunkForEvent(tapeEvent, elapsed)
        if (resultChunk) appendSubAgentTelemetry(bufKey, curId, spawnCallId, resultChunk)
        subAgentToolResult(bufKey, curId, spawnCallId, ev.tool_call_id ?? '', ev.result)
      } else if (ev.kind === 'approval_request') {
        setStatus(bufKey, 'running-tool')
        setPendingApproval({
          // Approval requests carry their separate namespaced gate ID.
          callId: ev.approval_call_id ?? '',
          tool: ev.name ?? 'tool',
          args: (ev.args ?? {}) as Record<string, unknown>,
          convKey: bufKey,
        })
      } else if (ev.kind === 'approval_decision') {
        setPendingApproval((a) => (a && a.callId === ev.approval_call_id ? null : a))
      }
    } else if (ev.type === 'sub_agent_done') {
      finishSubAgent(bufKey, curId, ev.call_id ?? '', ev.status ?? 'completed', ev.turns ?? 0, ev.note)
      appendSubAgentTelemetry(
        bufKey,
        curId,
        ev.call_id ?? '',
        `\n${ev.status === 'completed' ? '✓' : ev.status === 'cancelled' ? '■' : '!'} ${ev.status ?? 'completed'} · ${ev.turns ?? 0} turns    `,
      )
      pushLog({ kind: 'tool', name: 'spawn_agent', result: { status: ev.status, turns: ev.turns } })
    } else if (ev.type === 'error') {
      setStatus(bufKey, 'error')
      setError(bufKey, ev.message ?? 'Unknown agent error')
      setTurnError(bufKey, ev.message ?? 'Unknown agent error')
      settleSubAgents(bufKey, curId)
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      setPendingApproval((a) => (a && a.convKey === bufKey ? null : a))
      setPendingPlanApproval((p) => (p && p.convKey === bufKey ? null : p))
    } else if (ev.type === 'stopped') {
      setStatus(bufKey, 'idle')
      setSteerFlag(bufKey, false)
      appendTextDelta(bufKey, curId, '\n[stopped]')
      settleSubAgents(bufKey, curId)
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      setPendingApproval((a) => (a && a.convKey === bufKey ? null : a))
      setPendingPlanApproval((p) => (p && p.convKey === bufKey ? null : p))
      // Hard Stop holds the queue (#7): the queued echoes stay in the
      // transcript marked queued until the user sends or discards them.
      // No count state - the pill renders from queueEchoByConv.
    } else if (ev.type === 'done') {
      setStatus(bufKey, 'idle')
      setSteerFlag(bufKey, false)
      // A completed turn must leave no block pulsing: settle anything the
      // stream ended without a sub_agent_done for (defensive; the backend
      // always emits done events in the normal path).
      settleSubAgents(bufKey, curId)
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      setPendingApproval((a) => (a && a.convKey === bufKey ? null : a))
      setPendingPlanApproval((p) => (p && p.convKey === bufKey ? null : p))
      // Successful completion drains the backend queue. Any echo left after
      // injected/autosend events is stale and would expose invalid remove ids.
      if (!pendingQueueAutosendRef.current[bufKey]) setQueueEcho(bufKey, [])
    } else if (ev.type === 'title') {
      const convId = Number(bufKey)
      if (ev.title && Number.isInteger(convId) && convId > 0) {
        useAgent.getState().setTitle(convId, ev.title)
      }
    } else if (ev.type === 'user_injected') {
      // Soft injection landed (#7): promote the optimistic echo (matched
      // by the backend's queued-item id). Later assistant text must not keep
      // appending to the earlier emission above this user message.
      awaitingPostSteerEmission = true
      const echoes = useAgent.getState().queueEchoByConv[bufKey] ?? []
      const echo = echoes.find((e) => e.id === ev.id)
      if (echo) {
        useAgent.getState().reconcileInjected(bufKey, echo.tempId, ev.images ?? echo.images, ev.skills ?? echo.skills)
      }
      setSteerFlag(bufKey, false)
      setQueueEcho(
        bufKey,
        echoes.filter((e) => e.id !== ev.id),
      )
    } else if (ev.type === 'queued_autosend') {
      // The run ended (naturally or on error) with messages still queued
      // (#7): the backend hands them back - fire them as fresh turns.
      pendingQueueAutosendRef.current[bufKey] = ev.items ?? []
    } else if (ev.type === 'model_call') {
      // Issue #43: a chat call is dispatched and nothing has come back yet.
      // The waiting readout is driven by streamAgentTurn's onModelCall hook;
      // this branch only keeps the status dot honest.
      setStatus(bufKey, 'thinking')
    } else if (ev.type === 'compacted') {
      // Prompt compaction (adr/0004) ran before the first model call:
      // surface it as a chip in the ticker row (store-driven), not a
      // transcript message. The full transcript remains untouched; only
      // prompt-summary state and its replay watermark are updated.
      setCompaction(bufKey, {
        summarized: ev.summarized_messages,
        summary: ev.summary ?? '',
      })
    } else if (ev.type === 'compaction_failed') {
      // Soft-fail surfacing: the turn proceeds on the full history.
      pushLog({
        kind: 'system',
        name: 'compaction',
        result: { skipped: true, error: ev.error ?? 'unknown error' },
      })
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

  const send = async (
    pttText?: string,
    opts?: {
      interrupt?: boolean
      queueHandoff?: boolean
      target?: { conversationId: number | null; workspace: string }
      images?: string[]
      skills?: string[]
    },
  ) => {
    // Push-to-talk passes explicit text: it sends as its own message and
    // must not touch (or clear) whatever draft is sitting in the composer.
    // Its target is captured at release, not read from this render after
    // asynchronous transcription.
    const isPtt = pttText !== undefined
    const target = isPtt ? opts?.target : undefined
    const targetConversationId = target ? target.conversationId : conversationId
    const targetWorkspace = target?.workspace ?? workspace
    const handedOffPayload = opts?.images !== undefined || opts?.skills !== undefined
    const text = (pttText ?? input).trim()
    if (
      (!text && (isPtt || (attachments.length === 0 && images.length === 0))) ||
      (sendingKey === (targetConversationId === null ? 'draft' : String(targetConversationId)) && !opts?.queueHandoff)
    )
      return
    // Agent chat (issue #41): typed messages NEVER trigger a run — each one
    // becomes a standing instruction the agent sees at every scheduled fire.
    if (conversationId !== null && !isPtt) {
      const agentId = useAgent.getState().agentChatByConv[String(conversationId)]
      if (agentId) {
        if (!text) return
        try {
          await addAgentInstruction(agentId, text)
          appendUserMessage(String(conversationId), text)
          useAgent.getState().pushToast({
            kind: 'info',
            title: 'Standing instruction saved',
            body: 'Messages in an agent chat never start a run — this rides along at every fire.',
          })
        } catch (e) {
          useAgent.getState().pushToast({
            kind: 'error',
            title: 'Could not save standing instruction',
            body: String((e as Error).message ?? e),
          })
        }
        setInput('')
        return
      }
    }
    setSending(true)
    setSendError(null)

    // Inline small attachments as fenced blocks; large staged files as
    // workspace path pointers the agent can read_file. PTT skips this —
    // the attachments belong to the untouched draft.
    let fullText = text
    if (!isPtt && !handedOffPayload) {
      for (const a of attachments) {
        fullText += attachmentText(a)
      }
      if (images.length) {
        fullText += `\n\n[${images.length} image${images.length === 1 ? '' : 's'} attached]`
      }
    }
    const imageDataUrls = opts?.images ?? images.map((i) => i.dataUrl)
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
    const invokedSkills = opts?.skills ?? [...pickedSkills.map((s) => s.name), ...dollarNames]
    // Captured draft: if the turn fails before the agent answers, the
    // composer gets it back — a failed send must not cost the prompt.
    const draft = { input, attachments, images, pickedSkills }
    if (!isPtt && !handedOffPayload) {
      setInput('')
      setAttachments([])
      setImages([])
      setPickedSkills([])
    }
    // Capture the turn's target buffer now: everything this turn writes —
    // optimistic messages, stream deltas, tool traces — goes there, even if
    // the user switches to another conversation mid-stream (Q11: free).
    // `let` because adopting a newly created conversation re-keys the
    // buffer: events before adoption target 'draft', after it the real id.
    let bufKey = targetConversationId === null ? 'draft' : String(targetConversationId)
    const entryKey = bufKey
    setError(bufKey, null)
    // A new send supersedes a failed turn: drop the stale mid-stream banner
    // (#39) so it doesn't linger above the composer during the replacement
    // turn. (Resume remains for when the user wants the same turn continued.)
    setTurnError(bufKey, null)
    setSendingKey(bufKey)
    // Fresh turn: the previous run's compaction chip is stale — clear it
    // alongside the tape so the ticker row starts clean.
    clearCompaction(bufKey)
    const userId = appendUserMessage(bufKey, fullText, imageDataUrls, invokedSkills.length ? invokedSkills : undefined)
    const asstId = appendAssistantPlaceholder(bufKey)
    const ac = new AbortController()
    setAbortController(bufKey, ac)
    try {
      let cid: number
      // The effective destination for a first send (#94): the pinned draft
      // destination — which newConversation pins and the card's Change…
      // picker can override — else the workspace captured by the send. Both
      // the row and turn use the same destination.
      const dest = targetWorkspace
      if (targetConversationId === null) {
        // #51/#76: the draft's header pickers pin the new chat's scope —
        // written into the row at creation so the first turn already
        // resolves through the conversation (the header values ARE what
        // runs). Falls back to the current defaults when untouched.
        const ds = useAgent.getState().draftScope
        const created = await createConversation(fullText.slice(0, 40) || 'New chat', dest, {
          model: ds?.model ?? useAgent.getState().globalModel,
          effort: ds?.effort ?? useAgent.getState().globalEffort,
        })
        cid = created.id
        // Atomic: re-key the draft buffer (optimistic messages included).
        // If this PTT belongs to a draft the user has since left, keep that
        // draft filed but don't steal focus from the currently selected chat.
        const preserveSelection = target && useAgent.getState().conversationId !== targetConversationId
        adoptDraft(cid, preserveSelection ? { preserveSelection: true } : undefined)
        bufKey = String(cid)
        // Move the run's abort handle and in-flight marker to the new key.
        setAbortController(bufKey, ac)
        setAbortController(entryKey, null)
        setSendingKey(bufKey)
      } else {
        cid = targetConversationId
      }
      setStatus(bufKey, 'thinking')
      await streamAgentTurn(
        cid,
        fullText,
        dest,
        handleStreamEvent(bufKey, asstId),
        ac.signal,
        imageDataUrls,
        invokedSkills,
        false,
        (mc) => setModelCall(bufKey, mc),
      )
      if (useAgent.getState().statusByConv[bufKey] !== 'error') setStatus(bufKey, 'idle')
    } catch (e) {
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      if ((e as Error).name === 'AbortError') {
        setStatus(bufKey, 'idle')
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
        if (!handedOffPayload) {
          setInput(isPtt ? (draft.input ? `${draft.input.trimEnd()} ${text}` : text) : draft.input)
          setAttachments(draft.attachments)
          setImages(draft.images)
          setPickedSkills(draft.pickedSkills)
        }
        setStatus(bufKey, 'error')
        setSendError(
          `Message not sent — the agent could not be reached. Your draft was restored.`,
        )
        // A first send that never started filed nothing (#88): drop the
        // release-time pin so the restored draft follows the live
        // workspace again instead of freezing on the old one.
        useAgent.getState().clearOrphanedDraftPin()
        textareaRef.current?.focus()
      }
    } finally {
      setSending(false)
      setAbortController(bufKey, null)
      setSendingKey((k) => (k === entryKey || k === bufKey ? null : k))
      const autosend = pendingQueueAutosendRef.current[bufKey]
      if (autosend) {
        delete pendingQueueAutosendRef.current[bufKey]
        void drainQueueOnEnd(bufKey, autosend, send)
      }
    }
  }
  // Keep the PTT handlers pointed at the latest send (stale-closure shield).
  sendRef.current = send

  /** Continue a turn that died mid-stream: same conversation, same prompt,
   *  no duplicate user message (backend resume flag). The banner only renders
   *  for the conversation that failed (#39), so the key captured here IS the
   *  failed chat — the guard keeps a mid-click chat switch from resuming a
   *  turn into the wrong buffer. */
  const resumeTurn = async () => {
    if (conversationId === null || sending) return
    const bufKey = String(conversationId)
    if (turnErrorByConv[bufKey] === undefined) return
    setSending(true)
    setSendingKey(bufKey)
    setSendError(null)
    setTurnError(bufKey, null)
    setError(bufKey, null)
    const asstId = appendAssistantPlaceholder(bufKey)
    const ac = new AbortController()
    setAbortController(bufKey, ac)
    try {
      setStatus(bufKey, 'thinking')
      await streamAgentTurn(
        conversationId,
        'resume',
        workspace,
        handleStreamEvent(bufKey, asstId),
        ac.signal,
        [],
        [],
        true,
        (mc) => setModelCall(bufKey, mc),
      )
      if (useAgent.getState().statusByConv[bufKey] !== 'error') setStatus(bufKey, 'idle')
    } catch (e) {
      setPendingQuestion((q) => (q && q.convKey === bufKey ? null : q))
      if ((e as Error).name === 'AbortError') {
        setStatus(bufKey, 'idle')
        const tailId = lastAssistantId(bufKey) ?? asstId
        appendTextDelta(bufKey, tailId, '\n[stopped]')
        settleSubAgents(bufKey, tailId)
      } else {
        setStatus(bufKey, 'error')
        setTurnError(bufKey, String((e as Error).message ?? e))
        settleSubAgents(bufKey, lastAssistantId(bufKey) ?? asstId)
      }
    } finally {
      setSending(false)
      setAbortController(bufKey, null)
      setSendingKey((k) => (k === bufKey ? null : k))
    }
  }

  const stop = () => {
    // Cancel server-side (mid-loop) and abort the client stream — both
    // scoped to the conversation on screen (issue #10: a background chat's
    // run must keep running).
    if (conversationId !== null) void cancelAgent(conversationId).catch(() => {})
    const key = conversationId === null ? 'draft' : String(conversationId)
    useAgent.getState().abortByConv[key]?.abort()
  }

  /** Queue the current composer draft into the running turn (#7): POST it
   *  to the server queue (persisted with the run), echo it optimistically
   *  into the transcript marked queued, and clear the composer. */
  const queueInput = async (
    messageOverride?: string,
    targetConversationId: number | null = conversationId,
  ): Promise<boolean> => {
    const fromComposer = messageOverride === undefined
    const text = messageOverride ?? input.trim()
    if ((!text && (fromComposer ? attachments.length === 0 && images.length === 0 : true)) || targetConversationId === null) return false
    const fullText = text + (fromComposer ? attachments.map(attachmentText).join('') : '')
    const skillNames = fromComposer ? [...pickedSkills.map((s) => s.name)] : []
    for (const match of fullText.matchAll(/\$([A-Za-z0-9_-]+)/g)) {
      if (fromComposer && skills.some((skill) => skill.name === match[1]) && !skillNames.includes(match[1])) {
        skillNames.push(match[1])
      }
    }
    if (fromComposer && images.length > 4) {
      useAgent.getState().pushToast({
        kind: 'error',
        title: 'Too many images',
        body: 'A message can include up to four images. Remove the extras before queuing.',
      })
      return false
    }
    const imageDataUrls = fromComposer ? images.map((image) => image.dataUrl) : []
    try {
      const targetKey = String(targetConversationId)
      const res = await queueMessage(targetConversationId, fullText || '[Image attachment]', skillNames, imageDataUrls)
      const tempId = appendUserMessage(
        targetKey,
        fullText || '[Image attachment]',
        imageDataUrls,
        skillNames,
        true,
      )
      const echoes = [
        ...(useAgent.getState().queueEchoByConv[targetKey] ?? []),
        { id: res.item.id, tempId, text: fullText || '[Image attachment]', images: imageDataUrls, skills: skillNames },
      ]
      setQueueEcho(targetKey, echoes)
      if (fromComposer && targetKey === bufKeyForQueue) {
        setInput('')
        setImages([])
        setAttachments([])
        setPickedSkills([])
      }
      return true
    } catch (e) {
      useAgent.getState().pushToast({
        kind: 'error',
        title: 'Could not queue message',
        body: `${String((e as Error).message ?? e)}. Your draft was kept; send it normally if the run has ended.`,
      })
      return false
    }
  }

  /** Steer (#7): interrupt the in-flight step so queued messages land at
   *  the next boundary now; the run continues with full context. */
  const steerNow = async (targetConversationId: number | null = conversationId) => {
    if (targetConversationId === null) return
    const targetKey = String(targetConversationId)
    if (useAgent.getState().pendingQuestions[targetKey] || useAgent.getState().pendingApprovals[targetKey] || useAgent.getState().pendingPlanApprovals[targetKey]) return
    setSteerFlag(targetKey, true)
    try {
      await steerAgent(targetConversationId)
    } catch (e) {
      setSteerFlag(targetKey, false)
      useAgent.getState().pushToast({
        kind: 'error',
        title: 'Could not steer',
        body: `${String((e as Error).message ?? e)}. The message remains queued; use Stop to end the run, or leave it for the next boundary.`,
      })
    } finally {
      // The flag clears when the injection lands (user_injected) or when
      // the run ends; this is just a safety reset if the POST failed.
      setTimeout(() => setSteerFlag(targetKey, false), 3000)
    }
  }

  const steerInput = async (
    messageOverride?: string,
    targetConversationId: number | null = conversationId,
  ) => {
    if (pendingQuestion || pendingApproval || pendingPlanApproval || targetConversationId === null) return
    if (await queueInput(messageOverride, targetConversationId)) {
      await steerNow(targetConversationId)
    }
  }

  /** Fire queued messages as fresh turns after the run ended with them
   *  still queued (#7: auto-send on natural completion or error). */
  const drainQueueOnEnd = async (
    key: string,
    items: Array<{ id: number; text: string; skills?: string[]; images?: string[] }>,
    sendQueued: (text?: string, opts?: { queueHandoff?: boolean; images?: string[]; skills?: string[] }) => Promise<void>,
  ) => {
    for (const it of items) {
      const echoes = useAgent.getState().queueEchoByConv[key] ?? []
      const echo = echoes.find((e) => e.id === it.id)
      if (echo) {
        markQueuedAsNormal(key, echo.tempId)
        dropQueuedEcho(key, echo.tempId)
      }
      await sendQueued(it.text, { queueHandoff: true, images: it.images ?? [], skills: it.skills ?? [] })
    }
    // Any echo without a matching backend item is stale: the backend's
    // drained list is authoritative after the run-end handoff.
    setQueueEcho(key, [])
  }

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
        // #48: a drop advertising files with no readable File objects is the
        // WebView2 silent-failure signature (native OLE handler intercepting
        // the drag) — surface it, never swallow it.
        const outcome = classifyDrop(e.dataTransfer)
        if (outcome.kind === 'files') {
          addFiles(outcome.files)
        } else if (outcome.kind === 'files-but-inaccessible') {
          pushReject('The dropped file(s) could not be read by the app — attach them with the + button instead')
        }
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
              onClick={() => setTurnError(screenKey, null)}
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
          placeholder={
            isAgentChat
              ? 'This is an agent chat — messages become standing instructions (they never start a run)'
              : 'Describe a task... (drop/paste/attach images or text files; type / to load a skill)'
          }
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
                  if (streaming && conversationId !== null) {
                    if (!pendingQuestion && !pendingApproval && !pendingPlanApproval) void steerInput()
                  } else {
                    void send()
                  }
                }
                return
              }
              if (e.key === 'Escape') {
                e.preventDefault()
                setSkillMenuOpen(false)
                return
              }
            }
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && streaming && conversationId !== null) {
              e.preventDefault()
              if (!pendingQuestion && !pendingApproval && !pendingPlanApproval) void queueInput()
              return
            }
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              if (streaming && conversationId !== null) {
                if (!pendingQuestion && !pendingApproval && !pendingPlanApproval) void steerInput()
                return
              }
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
        {/* Queued messages pill (#7): sits above the composer while a run
            streams. Shows the count; expands to per-message rows with
            remove buttons; Steer interrupts the current step so the queue
            lands now. Rendered only while there is something queued. */}
        {queueEchoes?.length ? (
          <div className="mb-2 rounded border border-amber-700/60 bg-amber-950/20">
            <button
              className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left font-mono text-[10px] uppercase tracking-widest text-amber-400"
              onClick={() => setQueueOpen((o) => !o)}
            >
              <span className="text-amber-600">{queueOpen ? '▼' : '▸'}</span>
              <span>
                {queueEchoes.length} queued message{queueEchoes.length === 1 ? '' : 's'}
              </span>
              <span className="ml-auto flex items-center gap-2 tracking-normal normal-case">
                {streaming && conversationId !== null && !pendingQuestion && !pendingApproval && !pendingPlanApproval && (
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation()
                      void steerNow()
                    }}
                    className="rounded bg-amber-600/80 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-widest text-zinc-950 hover:bg-amber-500"
                  >
                    {steering ? 'steering…' : 'steer'}
                  </span>
                )}
                <span className="text-zinc-600">{queueOpen ? 'hide' : 'show'}</span>
              </span>
            </button>
            {queueOpen && (
              <div className="border-t border-amber-800/40 px-2.5 py-1.5">
                {queueEchoes.map((q) => (
                  <div key={q.tempId} className="flex items-center gap-2 py-0.5">
                    <span className="min-w-0 flex-1 truncate text-xs text-amber-100">
                      {q.text}
                    </span>
                    <button
                      title="Discard queued message"
                      className="rounded px-1 text-amber-700 hover:bg-amber-900/40 hover:text-amber-300"
                      onClick={() => {
                        dropQueuedEcho(bufKeyForQueue, q.tempId)
                        setQueueEcho(
                          bufKeyForQueue,
                          (queueEchoes ?? []).filter((x) => x.id !== q.id),
                        )
                        void removeQueued(conversationId ?? 0, q.id).catch(() => {})
                      }}
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : null}
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
            {streaming && conversationId !== null ? (
              <>
                <button
                  className="rounded border border-amber-700 px-2.5 py-1.5 text-sm text-amber-200 hover:bg-amber-950 disabled:opacity-40"
                  title="Interrupt the current step and inject this message now"
                  disabled={Boolean(pendingQuestion || pendingApproval || pendingPlanApproval || (!input.trim() && !attachments.length && !images.length))}
                  onClick={() => void steerInput()}
                >
                  Steer
                </button>
                <button
                  className="rounded border border-zinc-600 px-2.5 py-1.5 text-sm text-zinc-200 hover:bg-zinc-700 disabled:opacity-40"
                  title="Queue until the next natural boundary (Ctrl/Cmd+Enter)"
                  disabled={Boolean(pendingQuestion || pendingApproval || pendingPlanApproval || (!input.trim() && !attachments.length && !images.length))}
                  onClick={() => void queueInput()}
                >
                  Queue
                </button>
                <button
                  className="rounded border border-red-700 px-3 py-1.5 text-sm text-red-300 hover:bg-red-950"
                  onClick={stop}
                >
                  Stop
                </button>
              </>
            ) : streaming || sendingKey === (conversationId === null ? 'draft' : String(conversationId)) ? (
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
