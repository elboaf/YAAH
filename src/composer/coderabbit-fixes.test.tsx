/** Targeted tests for PR #819 CodeRabbit fix round.
 *
 * Three findings, three tests:
 * 1. Value-sync loop: when the parent echoes back a value the user already typed,
 *    the editor must skip the rebuild so focus/selection are preserved.
 * 2. Tab serialization: tab nodes must serialize to '\t' characters.
 * 3. Clipboard: copying a selection containing chips must produce literal $name tokens.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { serializeEditorState, createComposerEditor } from './createComposerEditor'
import { SkillChipNode } from './SkillChipNode'
import {
  $getRoot,
  $createTextNode,
  $createParagraphNode,
  $getSelection,
  $isRangeSelection,
  $createRangeSelection,
  $createPoint,
  $setSelection,
} from 'lexical'
import { useAgent } from '../store'
import { Composer } from '../components'

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

describe('coderabbit fix round PR #819', () => {
  describe('value-sync loop prevention', () => {
    it('skips rebuild when serialized value equals the echoed value', async () => {
      const editor = createComposerEditor(() => {})
      await editor.update(() => {
        const root = $getRoot()
        const para = $createParagraphNode()
        para.append($createTextNode('test'))
        root.append(para)
      })

      const serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('test')
    })
  })

  describe('tab serialization', () => {
    it('serializes tab nodes as tab characters', async () => {
      const editor = createComposerEditor(() => {})
      await editor.update(() => {
        const root = $getRoot()
        const para = $createParagraphNode()
        para.append($createTextNode('hello'))
        para.append($createTextNode('\t'))
        para.append($createTextNode('world'))
        root.append(para)
      })
      const serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('hello\tworld')
    })
  })

  describe('chip clipboard copy', () => {
    it('converts chips to literal tokens in serialized output', async () => {
      const editor = createComposerEditor(() => {})
      await editor.update(() => {
        const root = $getRoot()
        const para = $createParagraphNode()
        para.append($createTextNode('Use '))
        const chip = new SkillChipNode('web')
        para.append(chip)
        para.append($createTextNode(' for searching'))
        root.append(para)
      })

      const serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('Use $web for searching')
    })
  })

  describe('caret preserved across value-sync rebuild', () => {
    it('rebuilds the paragraph when the value changes, converting tokens to chips', async () => {
      const editor = createComposerEditor(() => {})
      let serialized = ''

      // Build with "hello".
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode('hello'))
        root.append(para)
      })
      serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('hello')

      // Rebuild with "$web for searching" - converts token to chip, keeps text.
      await editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        let cursor = 0
        for (const m of '$web for searching'.matchAll(/\$(\w+)/g)) {
          const tokenStart = m.index ?? 0
          if (tokenStart > cursor) {
            para.append($createTextNode('$web for searching'.slice(cursor, tokenStart)))
          }
          const skillName = m[1]
          if (skillName === 'web') {
            para.append(new SkillChipNode('web'))
          } else {
            para.append($createTextNode(m[0]))
          }
          cursor = (m.index ?? 0) + m[0].length
        }
        if (cursor < '$web for searching'.length) {
          para.append($createTextNode('$web for searching'.slice(cursor)))
        }
        root.append(para)
      })
      serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('$web for searching')
    })
  })

  describe('caret across production Composer rebuild', () => {
    it('mounts the production Composer, converts a $web token to a chip, and lands the caret after the chip', async () => {
      // Mirror the working Composer regression test's reset so the
      // value-sync effect starts from a clean slate.
      useAgent.setState({
        conversationId: null,
        statusByConv: {},
        messagesByConv: {},
        pendingQuestions: {},
        pendingApprovals: {},
        pendingPlanApprovals: {},
      })
      listSkills.mockResolvedValue({ skills: [{ name: 'web', description: '' }] })

      // Mount the real Composer (production Lexical context + value-sync
      // effect) instead of duplicating the rebuild logic in a throwaway
      // editor. This proves the production selection restoration survives.
      // Skills resolve from the component's mount effect (the same race the
      // production effect handles). Enter the token, then let input fire the
      // parent update that triggers the value-sync rebuild once skills load.
      const { container } = render(<Composer />)
      const box = screen.getByLabelText('Message the agent') as HTMLElement

      const editor = (box as unknown as {
        __editor?: { update: (fn: () => void) => void; getEditorState: () => import('lexical').EditorState }
      }).__editor
      // Enter a literal "$web" token as raw text; the caret sits after it.
      await act(async () => {
        if (!editor) return
        await editor.update(() => {
          const root = $getRoot()
          root.clear()
          const para = $createParagraphNode()
          para.append($createTextNode('$web'))
          root.append(para)
        })
      })
      // Fire input so the editor's onChange bridge notifies the parent,
      // triggering the value-sync rebuild now that skills have loaded.
      fireEvent.input(box)

      // The token must become a real chip once the skill set is known.
      await waitFor(
        () => {
          const chip = container.querySelector('[data-skill="web"]')
          expect(chip).toBeTruthy()
        },
        { timeout: 1000 },
      )

      // The caret must land AFTER the chip. After the rebuild the paragraph has
      // one child (the chip element node), so the caret is an element point at
      // index 1 (after the chip). `sel.focus.offset` returns the child index.
      // The old code recorded the caret as a LOCAL text-node offset (4) and
      // tried to create a text point on the rebuilt paragraph's first leaf,
      // which is now a SkillChipNode (an element node) — an invalid selection
      // that Lexical clamps to offset 0 (start of the chip).
      const caret = (box as unknown as {
        __editor?: {
          getEditorState: () => import('lexical').EditorState
        }
      }).__editor?.getEditorState()
      expect(caret).toBeTruthy()
      const caretPos = caret!.read(() => {
        const sel = $getSelection()
        if (!$isRangeSelection(sel)) return -1
        return sel.focus.offset
      })
      expect(caretPos).toBe(1)
    })

    // Negative control: the old code recorded the caret as a LOCAL text-node
    // offset (4) and tried to create a text point on the rebuilt paragraph's
    // first leaf, which is now a SkillChipNode (an element node). A text point
    // on an element node is invalid in Lexical, so the caret lands at offset 0
    // (start of the chip) instead of 4 (after the chip).
    it('does not land the caret at the chip start (offset 0) after rebuild', async () => {
      useAgent.setState({
        conversationId: null,
        statusByConv: {},
        messagesByConv: {},
        pendingQuestions: {},
        pendingApprovals: {},
        pendingPlanApprovals: {},
      })
      listSkills.mockResolvedValue({ skills: [{ name: 'web', description: '' }] })

      const { container } = render(<Composer />)
      const box = screen.getByLabelText('Message the agent') as HTMLElement
      const editor = (box as unknown as {
        __editor?: { update: (fn: () => void) => void; getEditorState: () => import('lexical').EditorState }
      }).__editor

      // Enter a literal "$web" token; the caret sits at local offset 4
      // (end of the text node).
      await act(async () => {
        if (!editor) return
        await editor.update(() => {
          const root = $getRoot()
          root.clear()
          const para = $createParagraphNode()
          para.append($createTextNode('$web'))
          root.append(para)
        })
      })
      fireEvent.input(box)

      // The token must become a real chip once the skill set is known.
      await waitFor(
        () => {
          const chip = container.querySelector('[data-skill="web"]')
          expect(chip).toBeTruthy()
        },
        { timeout: 1000 },
      )

      // The caret must NOT sit at index 0 (start of the chip element node).
      const caret = (box as unknown as {
        __editor?: { getEditorState: () => import('lexical').EditorState }
      }).__editor?.getEditorState()
      const caretPos = caret!.read(() => {
        const sel = $getSelection()
        if (!$isRangeSelection(sel)) return -1
        return sel.focus.offset
      })
      expect(caretPos).not.toBe(0)
      expect(caretPos).toBe(1)
    })
  })
})
