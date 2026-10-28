// Connected and saved remote devices. Device membership is metadata owned by
// the backend; credentials are supplied only for the current connection and
// never persisted by the browser.
import { create } from 'zustand'
import { listRemoteDevices, type RemoteDevice } from './api'

interface RemoteState {
  devices: RemoteDevice[]
  refreshDevices: () => Promise<boolean>
}

export const useRemote = create<RemoteState>((set) => ({
  devices: [],
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
