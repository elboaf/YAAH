// Connected and saved remote devices. Device membership is metadata owned by
// the backend; credentials are supplied only for the current connection and
// never persisted by the browser.
import { create } from 'zustand'
import { listRemoteDevices, remoteStatus, type RemoteDevice, type RemoteStatus } from './api'

export interface RemoteScope {
  connected: boolean
  url?: string
  name?: string
  hostId?: string
  os?: string
}

interface RemoteState {
  scope: RemoteScope
  devices: RemoteDevice[]
  setScope: (s: RemoteScope) => void
  setDevices: (devices: RemoteDevice[]) => void
  refreshScope: () => Promise<void>
  refreshDevices: () => Promise<boolean>
}

const asScope = (s: RemoteStatus): RemoteScope =>
  s.connected
    ? { connected: true, url: s.url, name: s.name, hostId: s.host_id, os: s.os }
    : { connected: false }

export const useRemote = create<RemoteState>((set) => ({
  scope: { connected: false },
  devices: [],
  setScope: (scope) => set({ scope }),
  setDevices: (devices) => set({ devices }),
  refreshScope: async () => {
    try {
      set({ scope: asScope(await remoteStatus()) })
    } catch {
      /* backend still starting */
    }
  },
  refreshDevices: async () => {
    try {
      const { devices } = await listRemoteDevices()
      set({ devices })
      return true
    } catch {
      /* transient backend error; preserve cached UI state */
      return false
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
