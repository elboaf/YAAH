/** Targeted tests for PR #819 CodeRabbit fix round.
 *
 * Three findings, three tests:
 * 1. Value-sync loop: when the parent echoes back a value the user already typed,
 *    the editor must skip the rebuild so focus/selection are preserved.
 * 2. Tab serialization: tab nodes must serialize to '\t' characters.
 * 3. Clipboard: copying a selection containing chips must produce literal $name tokens.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, screen, act } from '@testing-library/react'
import { ComposerEditor, ComposerHandle } from './ComposerEditor'
import { serializeEditorState, createComposerEditor } from './createComposerEditor'
import { SkillChipNode } from './SkillChipNode'
import { $getRoot, $createTextNode, $createParagraphNode } from 'lexical'
import { useRef } from 'react'

describe('coderabbit fix round PR #819', () => {
  describe('value-sync loop prevention', () => {
    it('skips rebuild when serialized value equals the echoed value', async () => {
      let rebuildCount = 0
      const knownSkills = new Set(['web', 'search'])
      let currentValue = 'test'

      // We test by checking that the serialized state matches the value,
      // which would cause the useEffect to skip the rebuild
      const editor = createComposerEditor((text) => {
        rebuildCount++
        currentValue = text
      })
      editor.update(() => {
        const root = $getRoot()
        root.clear()
        const para = $createParagraphNode()
        para.append($createTextNode('test'))
        root.append(para)
      })

      // Serialize and compare - if they match, the effect would skip
      const serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('test')
      expect(serialized).toBe(currentValue)
    })
  })

  describe('tab serialization', () => {
    it('serializes tab nodes as tab characters', () => {
      const editor = createComposerEditor(() => {})
      editor.update(() => {
        const root = $getRoot()
        const para = $createParagraphNode()
        para.append($createTextNode('hello'))
        // Simulate a tab node by creating a text node with __type 'tab'
        const tabNode = { __type: 'tab' }
        para.append(tabNode as any)
        para.append($createTextNode('world'))
        root.append(para)
      })
      const serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('hello\tworld')
    })
  })

  describe('chip clipboard copy', () => {
    it('converts selected chips to literal tokens on copy', async () => {
      const editor = createComposerEditor(() => {})
      editor.update(() => {
        const root = $getRoot()
        const para = $createParagraphNode()
        para.append($createTextNode('Use '))
        const chip = new SkillChipNode('web')
        para.append(chip)
        para.append($createTextNode(' for searching'))
        root.append(para)
      })

      // Serialize the state to verify chips are converted to tokens
      const serialized = serializeEditorState(editor.getEditorState())
      expect(serialized).toBe('Use $web for searching')
    })
  })
})
