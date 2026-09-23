/// <reference types="vite/client" />
import { describe, expect, it } from 'vitest'
// Vite-native raw imports (no node:fs — this repo does not ship @types/node).
import tauriConf from '../src-tauri/tauri.conf.json'
import componentsSource from './components.tsx?raw'

/**
 * Issue #48 guard. The composer attaches files via HTML5 drag & drop
 * (onDrop -> addFiles). On Windows, Tauri's native drag-drop handling is ON
 * by default and installs an exclusive OLE drop target over WebView2 — every
 * OS file drag is consumed before the webview sees it, so the HTML5 `drop`
 * event never fires and dragging a file onto the chat window does nothing.
 *
 * The two sides of that coupling live in different layers: `dragDropEnabled:
 * false` in tauri.conf.json, and the HTML5 handlers in components.tsx. This
 * test pins both, so neither can regress silently again.
 *
 * History: 4d8fcc0 added the flag for #42, but that branch never merged to
 * master — the flag vanished with it and #48 is the same bug resurfacing on
 * the file-attach path.
 */

describe('tauri window config keeps HTML5 drag & drop usable (#48)', () => {
  it('disables the native drag-drop handler (dragDropEnabled: false)', () => {
    const windows = tauriConf.app?.windows ?? []
    expect(windows.length).toBeGreaterThan(0)
    // Every declared webview window opts out of the native OLE drop target;
    // a single forgotten window resurrects the silent swallow.
    for (const w of windows) {
      expect(w).toHaveProperty('dragDropEnabled', false)
    }
  })

  it('the composer keeps an HTML5 drop handler that stages dropped files', () => {
    // The handler must actually ingest the dropped files...
    expect(componentsSource).toMatch(/onDrop=\{\(e\) => \{/)
    expect(componentsSource).toMatch(/classifyDrop\(e\.dataTransfer\)/)
    expect(componentsSource).toMatch(/addFiles\(outcome\.files\)/)
    // ...and must never silently ignore an unreadable file payload (#48's
    // "no response to drop events" is exactly what this prevents).
    expect(componentsSource).toMatch(/files-but-inaccessible/)
  })
})
