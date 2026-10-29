// #564 regression: clicking ANY chat greyed the whole window ("Maximum
// update depth exceeded" → root unmount). Root cause: ChatPanel's inline
// zustand selector ended in `?? []` — for a conversation whose messages
// aren't cached yet (the exact state right after the click, before
// loadHistory lands) it returned a FRESH empty array on every store check,
// so useSyncExternalStore saw "changed" on every render and React looped
// until it tore the tree down. The React 19.3.1 bump turned the latent
// instability into a hard crash.
//
// These tests pin: (1) the selector returns a STABLE reference across calls
// for an uncached conversation, (2) ChatPanel renders against an uncached
// conversation without the infinite-update crash.

import { describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import { useAgent, selectMessages, EMPTY_MESSAGES } from './store'
import { ChatPanel } from './components'

describe('selectMessages reference stability (#564)', () => {
  it('returns the SAME reference for an uncached conversation on every call', () => {
    useAgent.setState({ conversationId: 424242, messagesByConv: {} })
    const a = selectMessages(useAgent.getState())
    const b = selectMessages(useAgent.getState())
    expect(a).toBe(b)
    expect(Array.isArray(a)).toBe(true)
  })

  it('EMPTY_MESSAGES is module-stable — selectors must share it, not manufacture arrays', () => {
    expect(selectMessages(useAgent.getState())).toBe(EMPTY_MESSAGES)
    useAgent.setState({ messagesByConv: {} })
    expect(selectMessages(useAgent.getState())).toBe(EMPTY_MESSAGES)
  })

  it('still returns the live cached array when one exists (no behavior change)', () => {
    const cached = [{ id: 'm1', role: 'user', content: 'hello' }] as never[]
    useAgent.setState({ conversationId: 7, messagesByConv: { '7': cached } })
    expect(selectMessages(useAgent.getState())).toBe(cached)
  })
})

describe('ChatPanel renders an uncached conversation without the infinite loop (#564)', () => {
  it('clicking a chat whose messages are not yet loaded renders empty, not grey', () => {
    // The exact post-click state: conversation selected, nothing cached.
    useAgent.setState({ conversationId: 31337, messagesByConv: {} })
    // Pre-fix this throws "Maximum update depth exceeded" (React tears the
    // tree down); post-fix it renders the panel's empty state.
    render(<ChatPanel />)
    expect(document.getElementById('root') ?? document.body).toBeTruthy()
    expect(screen.queryByText(/maximum update depth/i)).toBeNull()
    useAgent.setState({ conversationId: null, messagesByConv: {} })
  })
})
