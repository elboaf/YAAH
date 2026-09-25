import { beforeEach, describe, expect, it } from 'vitest'
import { remoteConversationKey, useRemoteConversations } from './remoteConversationStore'

describe('owner-scoped remote conversation transcripts', () => {
  beforeEach(() => useRemoteConversations.setState({ transcripts: {} }))

  it('keys transcripts by owner and opaque conversation ID', () => {
    expect(remoteConversationKey('host:a', '7')).not.toBe(remoteConversationKey('host', 'a:7'))
    useRemoteConversations.getState().setTranscript('host-a', '7', [{ id: 'a', role: 'user', content: 'A' }])
    useRemoteConversations.getState().setTranscript('host-b', '7', [{ id: 'b', role: 'user', content: 'B' }])
    expect(useRemoteConversations.getState().getTranscript('host-a', '7')?.[0].content).toBe('A')
    expect(useRemoteConversations.getState().getTranscript('host-b', '7')?.[0].content).toBe('B')
  })

  it('does not collide with the local numeric transcript buffer', () => {
    useRemoteConversations.getState().setTranscript('host-a', '7', [{ id: 'remote', role: 'user', content: 'remote' }])
    expect(useRemoteConversations.getState().transcripts['7']).toBeUndefined()
  })

  it('clears only one host cache', () => {
    useRemoteConversations.getState().setTranscript('host-a', '1', [])
    useRemoteConversations.getState().setTranscript('host-b', '1', [])
    useRemoteConversations.getState().clearHost('host-a')
    expect(useRemoteConversations.getState().getTranscript('host-a', '1')).toBeUndefined()
    expect(useRemoteConversations.getState().getTranscript('host-b', '1')).toEqual([])
  })
})
