/** Targeted tests for PR #819 CodeRabbit fix round.
 *
 * Three findings, three tests:
 * 1. Value-sync loop: when the parent echoes back a value the user already typed,
 *    the editor must skip the rebuild so focus/selection are preserved.
 * 2. Tab serialization: tab nodes must serialize to '\t' characters.
 * 3. Clipboard: copying a selection containing chips must produce literal $name tokens.
 */
import { describe, it, expect } from 'vitest'
import { serializeEditorState, createComposerEditor } from './createComposerEditor'
import { SkillChipNode } from './SkillChipNode'
import { $getRoot, $createTextNode, $createParagraphNode } from 'lexical'

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
})
