---
name: computer-use
description: Playbook for driving and testing GUI apps with YAAH's computer-use tools (screenshot, list_windows, mouse/keyboard). Use when a task involves launching, controlling, or verifying a desktop application's UI on Windows.
---

# Computer use: driving and testing a GUI app

The tools (`screenshot`, `list_windows`, `focus_window`, `read_ui_tree`,
`mouse_move`, `mouse_click`, `mouse_scroll`, `type_text`, `press_key`,
`wait`) are real input on the user's desktop. Use them only for the app
under test.

## Structured first, pixels second

`read_ui_tree(hwnd=...)` returns every element of a window — type, name,
current value, center coordinates — with no vision involved. It beats
screenshots at everything: exact click targets, exact text values,
nothing misread. The loop becomes: read tree → find the element → click
its center → read the tree again to verify.

Reach for `screenshot` only when the tree fails you: empty or useless
trees (games, custom-drawn controls, remote streams like Parsec — their
content is pixels on this machine, the real UI lives elsewhere), or
when you need the visual layout to interpret the tree. A hybrid read is
normal: screenshot once to understand what you're looking at, then
drive and verify via the tree.

## The loop

Every GUI interaction is: **look → decide → act → look again**.

1. Launch the app via `bash`/`powershell` (`Start-Process`, never a
   trailing `&`). Redirect its stdout/stderr to a log file you can `read_file`
   — app logs are your fastest failure signals.
2. `list_windows` to find the window (match on process name, exact title).
   If it's missing, it may still be painting: `wait` 1–2s and re-list,
   don't retry-storm.
3. `focus_window` before typing — keystrokes go to the FOCUSED window.
4. `screenshot` to see the state. Locate the click target by a visual
   anchor (label text, distinctive color, relative position to a stable
   edge), then click.
5. Verify with a fresh `screenshot` (or `observe: true` on the action to
   get the post-action shot in the same result). Never assume a click
   landed; never assume text was typed.

## Discipline

- **Wait for paint.** After launch, after clicks that open dialogs, after
  navigation: `wait` then screenshot. Clicking before the window paints
  is the #1 failure mode.
- **Retry with information, not repetition.** Max ~2 blind retries. After
  that, screenshot, read the app's log, and change the approach (keyboard
  shortcut instead of menu, tab navigation, a different anchor).
- **Coordinates come from the latest screenshot.** If anything moved
  between screenshot and click (window opened, layout shifted), re-screenshot.
- **Prefer keyboard over mouse** when a shortcut exists (`press_key` with
  `ctrl+s`): no coordinates to go stale, no focus fights.
- **Modal dialogs:** they take focus and block the main window. Screenshot
  after any action that could open one; dismiss with its own buttons or `esc`.
- **The user-activity pause is not an error to fight.** It means the user
  is touching the machine. Screenshot, `wait`, retry when idle.

## Multi-monitor

The desktop may span several monitors. `list_windows` reports each
window's `monitor` (1-based) — use it. To look at a window on a
non-primary screen, never take a bare `screenshot()`; call
`screenshot(hwnd=<the window's hwnd>)`, which captures the monitor that
window lives on. If the shot doesn't show the window you expected, you
screened the wrong monitor: recheck `monitor` in the `list_windows`
output and retry with the hwnd.

## Verification

- "Looks right" in a screenshot is the assertion for UI tests; state the
  expected outcome first, then check the screenshot against it.
- For values you can reach without pixels (a saved file, an exit code, a
  log line), verify through the shell/file tools instead — cheaper and
  more reliable than vision.

## Privacy

Screenshots go to the configured model provider, and they capture the
whole screen — keep them purposeful and only when the task needs the screen.
