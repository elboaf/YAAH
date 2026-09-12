// Active connection scope, shared by the host switcher, sidebar conversation
// list, and workspace picker. The backend owns the session; this mirrors its
// /api/remote/status shape so the UI can scope (and grey out) what it shows.
import { create } from 'zustand'
import { remoteStatus } from './api'

export interface RemoteScope {
  connected: boolean
  url?: string
  name?: string
  hostId?: string
  os?: string
}

interface RemoteState {
  scope: RemoteScope
  setScope: (s: RemoteScope) => void
  /** Re-read the backend's session (e.g. after a launch auto-reconnect). */
  refreshScope: () => Promise<void>
}

export const useRemote = create<RemoteState>((set) => ({
  scope: { connected: false },
  setScope: (scope) => set({ scope }),
  refreshScope: async () => {
    try {
      const s = await remoteStatus()
      set({
        scope: s.connected
          ? { connected: true, url: s.url, name: s.name, hostId: s.host_id, os: s.os }
          : { connected: false },
      })
    } catch {
      /* backend still starting; stays local until told otherwise */
    }
  },
}))

/** Namespace helpers mirroring backend/agent/remote.py. */
export const nsWorkspace = (hostId: string, path: string | null) =>
  `remote:${hostId}:${path ?? ''}`

export const parseNsWorkspace = (
  ws: string | null | undefined,
): { hostId: string; path: string } | null => {
  if (ws && ws.startsWith('remote:')) {
    const idx = ws.indexOf(':', 'remote:'.length)
    if (idx >= 0) return { hostId: ws.slice('remote:'.length, idx), path: ws.slice(idx + 1) }
  }
  return null
}
