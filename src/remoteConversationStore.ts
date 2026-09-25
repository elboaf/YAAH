import { create } from 'zustand'
import type { ChatMessage } from './store'

/** Collision-safe key for a transcript owned by a remote device. */
export const remoteConversationKey = (hostId: string, conversationId: string) =>
  `remote:${encodeURIComponent(hostId)}:${encodeURIComponent(conversationId)}`

interface RemoteConversationState {
  transcripts: Record<string, ChatMessage[]>
  getTranscript: (hostId: string, conversationId: string) => ChatMessage[] | undefined
  setTranscript: (hostId: string, conversationId: string, messages: ChatMessage[]) => void
  clearTranscript: (hostId: string, conversationId: string) => void
  clearHost: (hostId: string) => void
}

/** In-memory cache only; the backend remains the durable, offline transcript cache. */
export const useRemoteConversations = create<RemoteConversationState>((set, get) => ({
  transcripts: {},
  getTranscript: (hostId, conversationId) =>
    get().transcripts[remoteConversationKey(hostId, conversationId)],
  setTranscript: (hostId, conversationId, messages) =>
    set((state) => ({
      transcripts: {
        ...state.transcripts,
        [remoteConversationKey(hostId, conversationId)]: messages,
      },
    })),
  clearTranscript: (hostId, conversationId) =>
    set((state) => {
      const transcripts = { ...state.transcripts }
      delete transcripts[remoteConversationKey(hostId, conversationId)]
      return { transcripts }
    }),
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
