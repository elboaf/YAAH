import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, fireEvent, screen, waitFor } from '@testing-library/react'
import { useAgent } from '../store'
import { Composer } from '../components'

const { streamAgentTurn, getConfig, createConversation } = vi.hoisted(() => ({
  streamAgentTurn: vi.fn(),
  getConfig: vi.fn(),
  createConversation: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  streamAgentTurn,
  getConfig,
  createConversation,
}))

/** Build a FileList from an array of File objects for a synthetic paste.
 *  jsdom lacks DataTransfer, so just reuse the File[] array directly;
 *  addFiles accepts FileList | File[]. */
function makeFileList(files: File[]): FileList {
  return files as unknown as FileList
}

describe('composer: paste image files', () => {
  beforeEach(() => {
    streamAgentTurn.mockReset()
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 42 })
    useAgent.setState({
      conversationId: null,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
    })
  })

  it('captures a pasted image file (regression: PR #819 broke file paste)', async () => {
    const { container } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as HTMLElement

    // A pasted PNG file. The native paste listener must catch it even though
    // registerRichText registers its own PASTE_COMMAND that would consume it.
    const png = new File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], 'test.png', {
      type: 'image/png',
    })
    const files = makeFileList([png])

    fireEvent.paste(box, {
      clipboardData: { files, getData: () => '' },
    })

    // The image thumbnail must render inside the composer card.
    await waitFor(
      () => {
        const imgs = container.querySelectorAll('img')
        expect(imgs.length).toBeGreaterThan(0)
        expect(imgs[0].getAttribute('src')).toContain('data:image/')
      },
      { timeout: 1000 },
    )
  })
})
