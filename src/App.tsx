import { useEffect, useState } from 'react'
import { BASE, IS_TAURI, getConfig, getMessages } from './api'
import { quantizeUiScale } from './jitter'
import { AgentChatLiveFollow, AgentRunWatcher, AgentSayWatcher, ChatPanel, ImageLightbox, PreviewModal, Sidebar, ToastStack } from './components'
import { NativeMenuGate } from './nativeMenu'
import { useAgent, persistConversationId } from './store'
import { NotificationSounds } from './NotificationSounds'

/**
 * Recovery banner: when any API call finds the backend unreachable, poll
 * /api/health until it answers again, then reload so every panel refetches
 * clean state. The banner never restarts the backend on its own (issue
 * #254): a cold PyInstaller start or a loaded VM can legitimately stay
 * silent past any fixed threshold, and auto-killing murdered healthy,
 * still-starting backends in a restart ping-pong. The kill is manual —
 * a "Restart backend" button (Tauri only) for hangs and the parked
 * crash-loop state — and a count-up timer shows how long it's been dark.
 */
export function BackendRecoveryBanner() {
  // How often to poll /api/health while it's down. 8s: responsive enough
  // for the timer, cheap enough to never matter.
  const POLL_INTERVAL_MS = 8000
  const [down, setDown] = useState(false)
  const [downMs, setDownMs] = useState(0)
  const [restarting, setRestarting] = useState(false)
  useEffect(() => {
    let poll: number | undefined
    let tick: number | undefined
    let firstFailure = 0
    const check = async () => {
      try {
        // Abort after one poll interval: a fetch that hangs forever (rather
        // than failing fast) would otherwise stall this check loop and mask
        // the downtime the timer is meant to show.
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
        // Down. Record the first failure time; the timer reads it. No kill
        // here — see the component docstring (#254): the user decides.
        if (firstFailure === 0) {
          firstFailure = Date.now()
          setDownMs(0)
        } else {
          setDownMs(Date.now() - firstFailure)
        }
      }
    }
    const start = () => {
      if (poll !== undefined) return
      setDown(true)
      setRestarting(false)
      firstFailure = 0
      poll = window.setInterval(check, POLL_INTERVAL_MS)
      // Cheap 1s tick keeps the count-up timer moving between polls.
      tick = window.setInterval(() => {
        if (firstFailure !== 0) setDownMs(Date.now() - firstFailure)
      }, 1000)
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
      if (tick !== undefined) window.clearInterval(tick)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const restart = () => {
    if (!IS_TAURI) return
    setRestarting(true)
    import('@tauri-apps/api/core')
      .then(({ invoke }) => invoke('restart_backend'))
      .catch(() => {})
  }
  if (!down) return null
  return (
    <div className="absolute inset-x-0 top-0 z-50 bg-red-950/90 px-4 py-1.5 text-center text-sm text-red-200">
      Backend is unreachable — {Math.round(downMs / 1000)} s{' '}
      {restarting ? '— restarting it' : '— waiting for it to come back'} …{' '}
      {IS_TAURI && (
        <button
          type="button"
          onClick={restart}
          className="ml-2 rounded border border-red-400/50 px-2 py-0.5 text-xs font-semibold text-red-100 hover:bg-red-800/70"
        >
          Restart backend
        </button>
      )}
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
 * #207: hydrate the "show <say> emissions in chat" flag from the saved
 * voice config at startup, and follow live changes (Settings saved in this
 * or another window) via the providers-changed event — the same shape as
 * AccessMode above.
 */
function SayInChatSync() {
  useEffect(() => {
    const apply = () => {
      getConfig()
        .then((c) => {
          useAgent.getState().setSayInChat(c.voice?.say_in_chat === true)
        })
        .catch(() => {})
    }
    apply()
    window.addEventListener('providers-changed', apply)
    return () => window.removeEventListener('providers-changed', apply)
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
      {/* #287: suppress the WebView2 default context menu outside editables
          and images — Back/Refresh/Save as/Print is browser chrome, not Yaah
          UI. Mount-first so the capture listener registers before anything
          else cares. */}
      <NativeMenuGate />
      <UiScale />
      <AccessMode />
      <SayInChatSync />
      <BackendRecoveryBanner />
      <RestoreSession />
      <NotificationSounds />
      {/* Scheduled agents (issue #41): poller (toasts + store map) + toast stack */}
      <AgentRunWatcher />
      <AgentChatLiveFollow />
      {/* #296: speaks scheduled fires' spoken briefings (per-agent say_mode). */}
      <AgentSayWatcher />
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
