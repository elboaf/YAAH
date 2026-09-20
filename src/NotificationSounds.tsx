import { useEffect, useRef } from 'react'
import { useAgent, type AgentStatus } from './store'
import { getConfig, updateConfig } from './api'
import { playQuestionPending, playRunFinished } from './sounds'

/**
 * Notification chimes (#29). Mounted once in App; owns both triggers:
 *
 * - Run finished: a store subscription over statusByConv catches every
 *   conversation's running -> idle/error transition, background chats
 *   included (#10). Fires even when the window is focused — the chime is
 *   the "done" cue, the transcript is the detail. A run watched in its own
 *   chat chimes too; muting is Settings' job, not the UI's.
 * - Question pending: a subscription over pendingQuestions fires when an
 *   ask_user card appears while the window is unfocused (document.hidden
 *   or no window focus). Focused windows stay silent — the card itself is
 *   the signal. Once per call_id: the ref remembers the last callId seen
 *   per conversation, so re-renders and conversation switches can't
 *   re-chime; history loads never repopulate pendingQuestions, so they
 *   can't re-chime either.
 *
 * Mute: voice.sounds_enabled in config.json (default true), persisted via
 * the existing voice-block merge in PUT /api/config — no backend change.
 */

const RUNNING: AgentStatus[] = ['thinking', 'running-tool']

/** Pure transition decision, unit-tested in store.test.ts. */
export function shouldChimeFinish(
  prev: AgentStatus | undefined,
  next: AgentStatus | undefined,
): boolean {
  const wasRunning = prev === 'thinking' || prev === 'running-tool'
  const ended = next === 'idle' || next === 'error'
  return wasRunning && ended
}

/** Pure unfocused check, unit-tested. */
export function isUnfocused(doc: Document, win: Window): boolean {
  return doc.hidden || !win.document.hasFocus()
}

export function NotificationSounds() {
  // Mute flag: hydrated once from config; Settings flips it live via the
  // window event, and persists through updateConfig like tts_enabled.
  const soundsEnabledRef = useRef(true)
  useEffect(() => {
    let cancelled = false
    getConfig()
      .then((c) => {
        if (!cancelled) soundsEnabledRef.current = c.voice?.sounds_enabled !== false
      })
      .catch(() => {})
    const onToggle = (e: Event) => {
      const next = (e as CustomEvent<{ enabled: boolean }>).detail.enabled
      soundsEnabledRef.current = next
    }
    window.addEventListener('sounds-enabled-changed', onToggle)
    return () => {
      cancelled = true
      window.removeEventListener('sounds-enabled-changed', onToggle)
    }
  }, [])

  // Run-finish chime: subscribe to the whole statusByConv map.
  useEffect(() => {
    let prevStatuses: Record<string, AgentStatus> = {}
    let hydrated = false
    const unsub = useAgent.subscribe((s) => {
      const next = s.statusByConv
      if (next === prevStatuses) return
      // First observed snapshot is the baseline: a mount that lands mid-run
      // (or on history load) must not chime for transitions it never saw.
      if (!hydrated) {
        prevStatuses = next
        hydrated = true
        return
      }
      for (const key of Object.keys(next)) {
        if (shouldChimeFinish(prevStatuses[key], next[key])) {
          prevStatuses = next
          if (soundsEnabledRef.current) playRunFinished()
          return
        }
      }
      prevStatuses = next
    })
    return unsub
  }, [])

  // Question-pending chime: subscribe to the pendingQuestions map. A Set of
  // already-chimed ids keeps it once-per-event even with several
  // conversations waiting at once; ids whose conversation cleared its
  // question leave the Set, so a later question there chimes again.
  const chimedQuestionsRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const unsub = useAgent.subscribe((s) => {
      const map = s.pendingQuestions
      const keys = Object.keys(map)
      // Forget ids whose question is gone (answered/cleared) so a later
      // question in the same conversation chimes again.
      for (const id of chimedQuestionsRef.current) {
        if (!map[id.split(':')[0]]) chimedQuestionsRef.current.delete(id)
      }
      if (keys.length === 0) return
      if (!isUnfocused(document, window)) return
      for (const key of keys) {
        const q = map[key]
        if (!q) continue
        const id = `${key}:${q.callId}`
        if (chimedQuestionsRef.current.has(id)) continue
        chimedQuestionsRef.current.add(id)
        if (soundsEnabledRef.current) playQuestionPending()
        break // one chime per batch, even if two land in the same tick
      }
    })
    return unsub
  }, [])

  return null
}

/** Settings helper: flip the mute flag live + persist it. */
export function setSoundsEnabled(enabled: boolean): void {
  window.dispatchEvent(
    new CustomEvent('sounds-enabled-changed', { detail: { enabled } }),
  )
  updateConfig({ voice: { sounds_enabled: enabled } }).catch(() => {})
}
