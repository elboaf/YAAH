/**
 * Issue #48: dragging a file or image from Explorer onto the chat window did
 * nothing — the only attach path was the + picker. The composer's HTML5
 * onDrop handler was correct; the drop event never reached it.
 *
 * Root cause: Tauri's native drag-drop handling is ON by default, and on
 * Windows it installs an exclusive OLE drop target over the WebView2 window.
 * That handler consumes every OS file drag before the webview sees it, so
 * HTML5 dragover/drop never fire (dragstart still does — the trap that made
 * this look like a frontend bug). Disabling it (`dragDropEnabled: false` in
 * tauri.conf.json) is required for HTML5 file DnD on Windows; macOS and
 * Linux allow both to coexist, so the flag is harmless there.
 *
 * This module keeps the drop path honest: it classifies a drop payload and
 * reports the one signature that would otherwise vanish silently — a drag
 * that carried files but yielded no File objects. That shape is the known
 * WebView2 partial-failure mode (the OLE layer intercepts the drop but the
 * file payload never materializes), and it is exactly the "no response to
 * drop events" the issue reports. Surfacing it turns any future regression
 * of the config coupling into a visible message instead of silence.
 */

export type DropOutcome =
  | { kind: 'files'; files: File[] }
  | { kind: 'empty' }
  | { kind: 'files-but-inaccessible' }

/** True when the drag payload advertised files, even if none are readable. */
export function dropAdvertisesFiles(dt: DataTransfer | null): boolean {
  if (!dt) return false
  if (dt.files && dt.files.length > 0) return true
  try {
    return Array.from(dt.types ?? []).includes('Files')
  } catch {
    return false
  }
}

/**
 * Classify a drop event's payload. `files` is the happy path; `empty` means
 * the drag carried nothing this app accepts; `files-but-inaccessible` is the
 * silent-failure signature that must never be swallowed (#48).
 */
export function classifyDrop(dt: DataTransfer | null): DropOutcome {
  const files = dt?.files ? Array.from(dt.files) : []
  if (files.length > 0) return { kind: 'files', files }
  if (dropAdvertisesFiles(dt)) return { kind: 'files-but-inaccessible' }
  return { kind: 'empty' }
}
