import { useEffect, useState } from 'react'
import { BASE, IS_TAURI, getConfig, getMessages } from './api'
import { quantizeUiScale } from './jitter'
import { AgentChatLiveFollow, AgentRunWatcher, ChatPanel, ImageLightbox, PreviewModal, Sidebar, ToastStack } from './components'
import { useAgent, persistConversationId } from './store'
import { NotificationSounds } from './NotificationSounds'

/**
 * Recovery banner: when any API call finds the backend unreachable, poll
 * /api/health until it answers again, then reload so every panel refetches
 * clean state. If the backend stays dark for a few seconds (hung rather
 * than crashed — the Rust supervisor only respawns on process exit), ask
 * the shell to kill and restart it.
 */
function BackendRecoveryBanner() {
  // How often to poll /api/health while it's down, and how long without a
  // response before assuming the backend is hung (vs. briefly busy) and
  // poking restart_backend to kill it. 8s poll / ~33s kill: short enough to
  // recover reasonably fast, long enough that a momentary event-loop stall
  // during an agent run is never mistaken for a hang.
  const POLL_INTERVAL_MS = 8000
  const KILL_AFTER_MS = 33000
  // failures at which the first poke fires (ceil(33s / 8s) = 5), then every
  // REPOKE_EVERY failures (~40s) after that.
  const KILL_FAILURES = Math.ceil(KILL_AFTER_MS / POLL_INTERVAL_MS)
  const REPOKE_EVERY = 5
  const [down, setDown] = useState(false)
  const [restarting, setRestarting] = useState(false)
  useEffect(() => {
    let poll: number | undefined
    let failures = 0
    const check = async () => {
      try {
        // Abort after one poll interval: a fetch that hangs forever (rather
        // than failing fast) would otherwise stall this check loop and mask
        // the hang the poke exists to detect.
        const res = await fetch(`${BASE}/api/health`, {
          cache: 'no-store',
          signal: AbortSignal.timeout(POLL_INTERVAL_MS),
        })
        if (res.ok) {
          // Backend is back: reload so all panels refetch fresh state.
          window.location.reload()
          return
        }
        throw new Error(String(res.status))
      } catch {
        failures += 1
        // The shell's supervisor auto-respawns crashes, parks itself if the
        // backend dies instantly several times in a row, and needs a poke
        // (restart_backend) both for the hung case and to leave the parked
        // state. Pokes are throttled so a stuck situation can't ping-pong.
        //
        // The kill threshold is deliberately generous (33s of no response at
        // 8s poll intervals): /api/health is trivial, so a failure here means
        // the process is dead OR the event loop is briefly blocked (e.g. a
        // busy agent run). Killing on the first few seconds of unresponsiveness
        // murdered healthy mid-run processes — the "backend crashed and I lost
        // my runs" class of bug. A genuinely dead backend is respawned by the
        // supervisor regardless; the poke is only for the hung case.
        const downFor = failures * POLL_INTERVAL_MS
        if (downFor >= KILL_AFTER_MS && (failures - KILL_FAILURES) % REPOKE_EVERY === 0) {
          setRestarting(true)
          if (IS_TAURI) {
            // Re-check health immediately before pulling the trigger: the
            // backend may have come up since the last poll, and killing it
            // now would restart a healthy process and ping-pong this banner.
            try {
              const fresh = await fetch(`${BASE}/api/health`, { cache: 'no-store' })
              if (fresh.ok) {
                window.location.reload()
                return
              }
            } catch {
              /* still down — proceed with the poke */
            }
            import('@tauri-apps/api/core')
              .then(({ invoke }) => invoke('restart_backend'))
              .catch(() => {})
          }
        }
      }
    }
    const start = () => {
      if (poll !== undefined) return
      setDown(true)
      failures = 0
      poll = window.setInterval(check, POLL_INTERVAL_MS)
      void check()
    }
    const onDown = () => start()
    const onStatus = (e: Event) => {
      const detail = (e as CustomEvent<{ status?: string }>).detail
      // 'down' = process exited (supervisor handles respawn); 'error' =
      // startup failure or parked crash-loop — the banner watches either.
      if (detail?.status === 'down' || detail?.status === 'error') start()
    }
    window.addEventListener('backend-down', onDown)
    window.addEventListener('backend-status', onStatus)
    return () => {
      window.removeEventListener('backend-down', onDown)
      window.removeEventListener('backend-status', onStatus)
      if (poll !== undefined) window.clearInterval(poll)
    }
    // restarting is read once via the guard above; keep it out of the deps
    // so the listeners are attached exactly once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  if (!down) return null
  return (
    <div className="absolute inset-x-0 top-0 z-50 bg-red-950/90 px-4 py-1.5 text-center text-sm text-red-200">
      Backend is unreachable — {restarting ? 'restarting it' : 'waiting for it to come back'}
      …
    </div>
  )
}

/**
 * Interface scale: applies the persisted `ui_scale` config value as CSS zoom
 * on the app root and re-applies it live when Settings saves a new one.
 * Zoom keeps the terminal-grade density identity intact at 1.0 while letting
 * the user choose a larger, crisper reading size (100–150%). Layout is
 * computed in device pixels, so panes reflow instead of clipping.
 */function UiScale() {
  useEffect(() => {
    const apply = (scale: number) => {
      // Issue #133: quantize before it touches the DOM — a fractional zoom
      // (hand-edited config, fp drift from a stored value) makes WebView2
      // round device pixels differently frame-to-frame, reading as a ~1px
      // dance of the whole client area inside a still window frame.
      // Garbage (NaN/0/negative) quantizes back to 1.0.
      const q = quantizeUiScale(Number(scale) || 1.0)
      // Inline styles only: WebView2's legacy zoom rejects var() in
      // stylesheets, but honors element.style.zoom. Zoom on #root with a
      // full-size box lands exactly on the visual viewport — no box
      // compensation wanted (zoom scales laid-out content, not the box's
      // own percentage-resolved size). Viewport units inside the zoomed
      // subtree get zoomed a second time, so overlays use percentages.
      const rootEl = document.getElementById('root')
      if (!rootEl) return
      rootEl.style.zoom = String(q)
    }
    // Restore the persisted scale (backend config; blank = 1.0 default).
    getConfig()
      .then((c) => apply(Number(c.ui_scale) || 1.0))
      .catch(() => {})
    // Settings saved: apply immediately, no reload needed.
    const onChange = (e: Event) =>
      apply(Number((e as CustomEvent<{ scale?: number }>).detail?.scale) || 1.0)
    window.addEventListener('ui-scale-changed', onChange)
    return () => window.removeEventListener('ui-scale-changed', onChange)
  }, [])
  return null
}

/**
 * Access mode: loads the persisted `access_mode` config value into the store
 * at startup and follows live changes (plan-mode exit, other windows) via a
 * window event, mirroring the ui-scale pattern.
 */
function AccessMode() {
  useEffect(() => {
    getConfig()
      .then((c) => {
        const m = c.access_mode
        if (m === 'ask' || m === 'plan' || m === 'full') {
          useAgent.getState().setAccessMode(m)
        }
      })
      .catch(() => {})
    const onChange = (e: Event) => {
      const mode = (e as CustomEvent<{ mode?: string }>).detail?.mode
      if (mode === 'ask' || mode === 'plan' || mode === 'full') {
        useAgent.getState().setAccessMode(mode)
      }
    }
    window.addEventListener('yaah-access-mode-changed', onChange)
    return () => window.removeEventListener('yaah-access-mode-changed', onChange)
  }, [])
  return null
}

/**
 * Session restore: the recovery banner reloads the whole app when the backend
 * comes back, and a plain F5 does too. Without this, a reload silently lands
 * on a fresh "draft" — the next send then creates a brand-new conversation,
 * which looks like the app switched chats on its own. Mirrors the workspace
 * restore pattern: localStorage id → reopen + load history (async), and if
 * the conversation no longer exists, just clear the stale id.
 */
function RestoreSession() {
  useEffect(() => {
    let stored: string | null = null
    try {
      stored = localStorage.getItem('agent.conversationId')
    } catch {
      stored = null
    }
    const id = stored ? Number(stored) : NaN
    if (!Number.isInteger(id) || id <= 0) return
    getMessages(id)
      .then((rows) => {
        useAgent.getState().setConversationId(id)
        useAgent.getState().loadHistory(id, rows)
      })
      .catch(() => persistConversationId(null))
  }, [])
  return null
}

export default function App() {
  return (
    <div className="relative flex h-full w-full bg-zinc-900 text-zinc-100">
      <UiScale />
      <AccessMode />
      <BackendRecoveryBanner />
      <RestoreSession />
      <NotificationSounds />
      {/* Scheduled agents (issue #41): poller (toasts + store map) + toast stack */}
      <AgentRunWatcher />
      <AgentChatLiveFollow />
      <Sidebar />
      {/* FilesPanel is GUI-removed (see DESIGN.md Layout): the component and
          its wiring stay in components.tsx — restore by re-adding the mount:
          <FilesPanel /> */}
      <ChatPanel />
      <PreviewModal />
      <ImageLightbox />
      <ToastStack />
    </div>
  )
}
