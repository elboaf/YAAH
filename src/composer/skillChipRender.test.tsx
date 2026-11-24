/** Regression: a `$name` token already in the editor as plain text must
 *  still become a real SkillChipNode once the known-skill set is populated.
 *
 *  The composer's value-sync effect skipped a rebuild whenever the editor's
 *  serialized text already matched the parent value. But serialization is
 *  identical whether `$ask-matt` is a chip or a plain TextNode, so a token
 *  typed before skills load (or restored from a draft whose chips serialized
 *  back to `$name`) stayed as spell-checked `$name` text forever — the chip
 *  never rendered. This pins the fix.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { useAgent } from '../store'
import { Composer } from '../components'
import { $getRoot, $createTextNode, $createParagraphNode } from 'lexical'

const { streamAgentTurn, getConfig, createConversation, listSkills } = vi.hoisted(() => ({
  streamAgentTurn: vi.fn(),
  getConfig: vi.fn(),
  createConversation: vi.fn(),
  listSkills: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  streamAgentTurn,
  getConfig,
  createConversation,
  listSkills,
}))

describe('composer: skill chip renders from in-editor text', () => {
  beforeEach(() => {
    streamAgentTurn.mockReset()
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 42 })
    // Skills load AFTER the composer mounts and the token is already in the
    // editor as plain text — the exact race that regressed.
    listSkills.mockResolvedValue({ skills: [{ name: 'ask-matt', description: '' }] })
    useAgent.setState({
      conversationId: null,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
    })
  })

  it('converts a `$name` token that is already plain text into a chip', async () => {
    const { container } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as HTMLElement

    // Drive the editor directly (jsdom can't set a contentEditable value),
    // then fire input so the parent's `input` state picks up `$ask-matt`.
    const editor = (box as unknown as { __editor?: { update: (fn: () => void) => void } }).__editor
    await act(async () => {
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode('$ask-matt'))
        root.append(para)
      })
    })
    fireEvent.input(box)

    // The chip is a real node: a span with data-skill, not spell-checked text.
    await waitFor(
      () => {
        const chip = container.querySelector('[data-skill="ask-matt"]')
        expect(chip).toBeTruthy()
        expect(chip?.textContent).toBe('$ask-matt')
      },
      { timeout: 1000 },
    )
  })
})
