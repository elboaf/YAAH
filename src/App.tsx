import { useEffect, useState } from 'react'
import { BASE, IS_TAURI } from './api'
import { ChatPanel, FilesPanel, PreviewModal, Sidebar } from './components'

/**
 * Recovery banner: when any API call finds the backend unreachable, poll
 * /api/health until it answers again, then reload so every panel refetches
 * clean state. If the backend stays dark for a few seconds (hung rather
 * than crashed — the Rust supervisor only respawns on process exit), ask
 * the shell to kill and restart it.
 */
function BackendRecoveryBanner() {
  const [down, setDown] = useState(false)
  const [restarting, setRestarting] = useState(false)
  useEffect(() => {
    let poll: number | undefined
    let failures = 0
    const check = async () => {
      try {
        const res = await fetch(`${BASE}/api/health`, { cache: 'no-store' })
        if (res.ok) {
          // Backend is back: reload so all panels refetch fresh state.
          window.location.reload()
          return
        }
        throw new Error(String(res.status))
      } catch {
        failures += 1
        // ~3s of continued darkness means the process is alive but wedged
        // (the supervisor would already be respawning a crashed one): poke
        // the shell's restart command, which force-kills and respawns.
        if (failures === 4 && !restarting) {
          setRestarting(true)
          if (IS_TAURI) {
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
      poll = window.setInterval(check, 700)
      void check()
    }
    const onDown = () => start()
    const onStatus = (e: Event) => {
      const detail = (e as CustomEvent<{ status?: string }>).detail
      if (detail?.status === 'down') start()
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

export default function App() {
  return (
    <div className="relative flex h-screen w-screen bg-zinc-900 text-zinc-100">
      <BackendRecoveryBanner />
      <Sidebar />
      <FilesPanel />
      <ChatPanel />
      <PreviewModal />
    </div>
  )
}
