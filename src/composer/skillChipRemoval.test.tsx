/** Tests for PR #860 CodeRabbit Major finding: chip removal.
 *
 * The chip-removal handler must resolve the clicked DOM element to its
 * Lexical node and remove that specific node, not replace the first matching
 * `$name` token in the serialized text. A regex on the text matches the
 * prefix of a differently-named sibling chip (`$ask-matt $ask` -> `-matt
 * $ask`) and always removes the first occurrence, so duplicate-named chips
 * could never be removed in turn.
 *
 * The removal control must be a real, focusable <button> so it is
 * keyboard-operable out of the box (Tab to focus, Enter/Space to activate)
 * with no nested button roles.
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

describe('composer: skill chip removal (PR #860)', () => {
  beforeEach(() => {
    streamAgentTurn.mockReset()
    getConfig.mockResolvedValue({})
    createConversation.mockResolvedValue({ id: 42 })
    listSkills.mockResolvedValue({ skills: [{ name: 'ask', description: '' }, { name: 'ask-matt', description: '' }] })
    useAgent.setState({
      conversationId: null,
      statusByConv: {},
      messagesByConv: {},
      pendingQuestions: {},
      pendingApprovals: {},
      pendingPlanApprovals: {},
    })
  })

  it('removes only the clicked chip when two chips share a name', async () => {
    const { container } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as HTMLElement

    const editor = (box as unknown as { __editor?: { update: (fn: () => void) => void } }).__editor
    await act(async () => {
      if (!editor) return
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        // Two chips with the same name: the first removal must not nuke the
        // other one.
        para.append($createTextNode('$ask $ask'))
        root.append(para)
      })
    })
    fireEvent.input(box)

    await waitFor(
      () => {
        const chips = container.querySelectorAll('[data-skill="ask"]')
        expect(chips.length).toBe(2)
      },
      { timeout: 1000 },
    )

    // Click the remove button on the SECOND chip.
    const secondChip = container.querySelectorAll('.skill-chip')[1]
    const secondRemove = secondChip?.querySelector('button.skill-chip__remove') as HTMLButtonElement | null
    expect(secondRemove).toBeTruthy()
    secondRemove?.click()

    // Exactly one chip remains.
    await waitFor(
      () => {
        const chips = container.querySelectorAll('[data-skill="ask"]')
        expect(chips.length).toBe(1)
      },
      { timeout: 1000 },
    )
  })

  it('removes the correct, differently-named chip (not the first match)', async () => {
    const { container } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as HTMLElement

    const editor = (box as unknown as { __editor?: { update: (fn: () => void) => void } }).__editor
    await act(async () => {
      if (!editor) return
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        // `$ask-matt $ask`: a naive regex on the serialized text would match
        // the prefix of `$ask-matt` and damage the wrong token.
        para.append($createTextNode('$ask-matt $ask'))
        root.append(para)
      })
    })
    fireEvent.input(box)

    await waitFor(
      () => {
        expect(container.querySelectorAll('[data-skill="ask-matt"]').length).toBe(1)
        expect(container.querySelectorAll('[data-skill="ask"]').length).toBe(1)
      },
      { timeout: 1000 },
    )

    // Click the remove button on the `$ask` chip (the second one).
    const askChip = container.querySelectorAll('.skill-chip')[1]
    const askRemove = askChip?.querySelector('button.skill-chip__remove') as HTMLButtonElement | null
    expect(askRemove).toBeTruthy()
    askRemove?.click()

    // `$ask` is gone; `$ask-matt` survives intact.
    await waitFor(
      () => {
        expect(container.querySelector('[data-skill="ask"]')).toBeNull()
        expect(container.querySelector('[data-skill="ask-matt"]')).toBeTruthy()
      },
      { timeout: 1000 },
    )
  })

  it('renders the removal control as a real, focusable button', async () => {
    const { container } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as HTMLElement

    const editor = (box as unknown as { __editor?: { update: (fn: () => void) => void } }).__editor
    await act(async () => {
      if (!editor) return
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode('$ask'))
        root.append(para)
      })
    })
    fireEvent.input(box)

    await waitFor(
      () => {
        const chip = container.querySelector('[data-skill="ask"]') as HTMLElement | null
        // The removal control is a real <button>, which is focusable and
        // keyboard-operable by default (no nested button roles).
        const remove = chip?.querySelector('button.skill-chip__remove') as HTMLButtonElement | null
        expect(remove).toBeTruthy()
        expect(remove?.tagName).toBe('BUTTON')
        expect(remove?.getAttribute('tabindex')).not.toBe('-1')
      },
      { timeout: 1000 },
    )
  })

  it('stops Enter keydown from propagating to the composer send handler', async () => {
    const { container } = render(<Composer />)
    const box = screen.getByLabelText('Message the agent') as HTMLElement

    // Spy on keydown events bubbling up to the composer root (the container).
    let reachedRoot = false
    const spy = vi.fn((e: KeyboardEvent) => {
      if (e.key === 'Enter') reachedRoot = true
    })
    container.addEventListener('keydown', spy)

    const editor = (box as unknown as { __editor?: { update: (fn: () => void) => void } }).__editor
    await act(async () => {
      if (!editor) return
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode('$ask'))
        root.append(para)
      })
    })
    fireEvent.input(box)

    await waitFor(
      () => {
        const chip = container.querySelector('[data-skill="ask"]') as HTMLElement | null
        expect(chip).toBeTruthy()
      },
      { timeout: 1000 },
    )

    // Focus the removal button and press Enter.
    const remove = container.querySelector('button.skill-chip__remove') as HTMLButtonElement | null
    expect(remove).toBeTruthy()
    act(() => {
      remove?.focus()
    })
    act(() => {
      fireEvent.keyDown(remove!, { key: 'Enter' })
    })

    // The keydown must NOT have bubbled up to the composer root, so the
    // send handler never sees it.
    expect(reachedRoot).toBe(false)

    container.removeEventListener('keydown', spy)
  })
})
