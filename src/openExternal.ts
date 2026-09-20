import { IS_TAURI } from './api'

/**
 * Open a link in the OS default browser (#31). The Tauri webview swallows
 * target="_blank" navigation, so under Tauri the click is intercepted and
 * the href handed to tauri-plugin-opener; plain-browser dev (vite) and the
 * vitest DOM keep the native <a target="_blank"> behavior.
 *
 * Callers must gate on the same scheme allowlist as before (safeHref:
 * https?/mailto) — this helper decides HOW to open, never WHETHER.
 */
export function openExternal(href: string, event: { preventDefault(): void }): void {
  if (!IS_TAURI) return
  event.preventDefault()
  void import('@tauri-apps/plugin-opener')
    .then(({ openUrl }) => openUrl(href))
    .catch(() => {})
}
