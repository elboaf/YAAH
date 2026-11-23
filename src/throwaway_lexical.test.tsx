import { describe, it, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor, act } from '@testing-library/react'
import { useAgent } from './store'
import { Composer } from './components'
import { $getRoot, $createTextNode, $createParagraphNode } from 'lexical'

const { streamAgentTurn, getConfig, createConversation } = vi.hoisted(() => ({
  streamAgentTurn: vi.fn(), getConfig: vi.fn(), createConversation: vi.fn(),
}))
vi.mock('./api', async (importOriginal) => ({
  ...await importOriginal<typeof import('./api')>(),
  streamAgentTurn, getConfig, createConversation,
}))

describe('throwaway: input event', () => {
  beforeEach(() => {
    streamAgentTurn.mockReset()
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 42 })
    useAgent.setState({ conversationId: null, statusByConv: {}, messagesByConv: {}, pendingQuestions: {}, pendingApprovals: {}, pendingPlanApprovals: {} })
  })

  it('fire input event after update', async () => {
    streamAgentTurn.mockImplementation(async (_cid, _msg, _ws, onEvent: any) => {
      onEvent({ type: 'text', text: 'Visible answer.' })
      onEvent({ type: 'done' })
    })
    const { rerender } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as any
    const editor = box.__editor as any

    await act(async () => {
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode('do the thing'))
        root.append(para)
      })
    })
    fireEvent.input(box)
    await act(async () => { await new Promise(r => setTimeout(r, 100)) })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => {
      const msgs = useAgent.getState().messagesByConv['42'] ?? []
      console.log('messagesByConv[42]:', JSON.stringify(msgs.map(m => ({ role: m.role, content: m.content, id: m.id }))))
    }, { timeout: 5000 })
  })
})
