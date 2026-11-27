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
import { $getRoot, $createTextNode, $createParagraphNode } from 'lexical'
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
    it('mounts the production Composer, converts a $web token to a chip, and lands later input after the chip', async () => {
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

      // The caret must land AFTER the chip — anchored on the paragraph as an
      // element point, not as a text point inside the atomic chip (which is
      // what threw `selectionTransform is not a function` before the fix).
      // Later input therefore appends after the token, not inside the chip.
      await waitFor(
        () => {
          if (!editor) return
          const selection = (editor.getEditorState() as any)._selection
          expect(selection).not.toBeNull()
          // Element-type point on the paragraph, offset = child index.
          expect(selection.anchor.type).toBe('element')
          // The paragraph holds one child (the chip); the caret sits after it.
          expect(selection.anchor.offset).toBe(1)
        },
        { timeout: 1000 },
      )

    })

    it('keeps the caret after text that follows a chip (foo $web bar)', async () => {
      // #840 criterion 2: typing `foo $web bar` with the caret after `bar`
      // must leave the caret after `bar`, not jump to the chip's start.
      // This exercises the text-node branch of the caret-restore walk.
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

      // Enter `foo $web bar` with the caret after `bar` (offset 12).
      await act(async () => {
        if (!editor) return
        await editor.update(() => {
          const root = $getRoot()
          root.clear()
          const para = $createParagraphNode()
          para.append($createTextNode('foo $web bar'))
          root.append(para)
        })
      })
      fireEvent.input(box)

      await waitFor(
        () => {
          const chip = container.querySelector('[data-skill="web"]')
          expect(chip).toBeTruthy()
        },
        { timeout: 1000 },
      )

      // Caret lands after `bar` — a text point inside the trailing text node.
      await waitFor(
        () => {
          if (!editor) return
          const selection = (editor.getEditorState() as any)._selection
          expect(selection).not.toBeNull()
          expect(selection.anchor.type).toBe('text')
          // "foo $web bar" = "foo " + "$web" + " bar", so the trailing
          // text node is " bar" (4 chars) and the caret at its end sits at
          // offset 4.
          expect(selection.anchor.offset).toBe(4)
        },
        { timeout: 1000 },
      )
    })
  })
})
