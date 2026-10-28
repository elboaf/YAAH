import { create } from 'zustand'
import type { ChatMessage } from './store'

/** Collision-safe key for a transcript owned by a remote device. */
export const remoteConversationKey = (hostId: string, conversationId: string) =>
  `remote:${encodeURIComponent(hostId)}:${encodeURIComponent(conversationId)}`

interface RemoteConversationState {
  transcripts: Record<string, ChatMessage[]>
  setTranscript: (hostId: string, conversationId: string, messages: ChatMessage[]) => void
  clearHost: (hostId: string) => void
}

/** In-memory cache only; the backend remains the durable, offline transcript cache. */
export const useRemoteConversations = create<RemoteConversationState>((set) => ({
  transcripts: {},
  setTranscript: (hostId, conversationId, messages) =>
    set((state) => ({
      transcripts: {
        ...state.transcripts,
        [remoteConversationKey(hostId, conversationId)]: messages,
      },
    })),
  clearHost: (hostId) =>
    set((state) => {
      const prefix = `remote:${encodeURIComponent(hostId)}:`
      return {
        transcripts: Object.fromEntries(
          Object.entries(state.transcripts).filter(([key]) => !key.startsWith(prefix)),
        ),
      }
    }),
}))
