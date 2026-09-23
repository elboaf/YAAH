# Computer vision & accessibility for computer use

Research: what Windows accessibility layers let the model act without
pixel-guessing, whether screenshots are still needed at all, how to see
inside the Windows Sandbox window, and what to build next. Every
load-bearing claim is cited, and the claims marked **[verified live]** were
probed on this machine during this research session (2026-09-19).

## Recommendations, ranked

| # | Build | Why | Effort |
|---|-------|-----|--------|
| 1 | **Pattern-based invocation** — a `ui_element_action` tool (Invoke / Toggle / SelectItem / ExpandCollapse / Value / Scroll on a UIA element) | Buttons, checkboxes, menu items, tabs and list items can be actuated *directly through UIA patterns* — no mouse move, no coordinates, no screenshot, no focus steal. This is the single biggest efficiency win and yaah already ships the dependency. | ~1–2 days |
| 2 | **In-VM UIA bridge** — run UIA *inside* the sandbox via `sandbox_run`, return JSON over the existing mapped-logs channel | The host cannot see inside the VM (**[verified live]**), and in-VM UIA works with zero installs via PowerShell 5.1 + the .NET UIAutomation assemblies (**[verified live]**). It is the only structured-UI path into the sandbox. | ~2–3 days |
| 3 | **Element-wait helpers** — wait until an element with name/id appears or vanishes, with timeout | Kills the sleep-and-rescreenshot retry loop that burns round-trips on slow apps. | ~1 day |
| 4 | **Windows.Graphics.Capture capture** for occluded / window-targeted shots | `PrintWindow` fails on the sandbox client (**[verified live]** — returns success but a solid color); WGC is the API that reads DWM-composited windows. Also useful for any occluded host window. | ~2–4 days |
| 5 | **OCR fallback** — Windows.Media.Ocr via PowerShell 5.1 (zero-install), Tesseract-in-toolkit as the heavyweight option | Third leg for pixel-only UIs (canvas, games, remote streams): OCR word boxes give approximate text positions when no tree exists. | ~1 day |
| 6 | **Grid overlay option on screenshots** (`grid_size` param) | Cheap, but low value: SoM boxes + coordinate rulers already exist and are strictly better when a tree is available. Only worth it for pixel-only surfaces. | ~½ day |

**Do not build:** UIA remote operations (WinUI/UWP-focused, needs WinRT
interop, no Python story — see Q1); MSAA directly (legacy, reachable via
`LegacyIAccessible` when needed); a pywinauto migration (yaah's
`uiautomation` stack already covers it); stitched multi-monitor captures
(rejected in PLAN-computer-use.md Q11); training a GUI-specialized VLM
(CogAgent-style — model-side, not yaah's layer).

## What yaah already has (grounded in code)

- `read_ui_tree` — `backend/agent/computer.py:770`. Walks a window's UIA
  tree via the `uiautomation` package (lazy import): flat element list with
  tree path, control type, name, AutomationId, disabled flag, center
  coordinates, and Value-pattern text. Caps: depth ≤ 12, nodes ≤ 400.
  On failure returns "this window may be pixel-only; use screenshot".
- Set-of-marks overlay — `computer.py:581` (`_som_overlay`): numbered boxes
  drawn over a window screenshot from the UIA rects, returned as
  `id -> name/type/center` so `mouse_click` can act on an element's center.
- Coordinate-ruler annotation — `computer.py:477` (`_annotate`): every
  screenshot carries labeled rulers; the system prompt tells the model to
  click ruler values, never visually estimated positions.
- Region crops — `computer.py:464` (`x/y/w/h`), observe crops —
  `computer.py:450` (400 px around the action point).
- Input: SendInput-based mouse/keyboard with the injected-event filter and
  user-activity pause (`computer.py:853–990`), panic hotkey
  (`computer.py:1313+`), `list_windows` / `focus_window`.
- Dependencies (`requirements.txt`): `pynput`, `mss`, `uiautomation` —
  all Windows-only, PyInstaller-bundled.

The gap is not perception — it is **action**: the tree is read-only today.
The model must still route every action through pixel coordinates.

## Q1 — The accessibility stack: what works without pixels

UI Automation (UIA) is the modern Windows accessibility API; its **control
patterns** expose a control's functionality independent of its appearance
([Control patterns overview](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/ui-automation-control-patterns-overview)):

- **Invoke** — "Invokes the action of a control, such as a button click"
  ([IUIAutomationInvokePattern::Invoke](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationinvokepattern-invoke)).
  This is the headline: a button press without the mouse.
- **Toggle** (checkboxes), **SelectionItem** (list items, radio), **ExpandCollapse**
  (combos, tree nodes), **Value** (`SetValue` — text fields without typing),
  **Scroll**, **RangeValue** (sliders), **LegacyIAccessible** (bridge to
  MSAA-era apps).

So: **with a tree + patterns, screenshots are unnecessary** for standard
controls. Locate by name/AutomationId → act via pattern → verify via the
tree (or one screenshot at the end). Pixels remain necessary for:
canvas/game-rendered UIs, apps with broken trees, and *visual* verification
(did the dialog actually render?).

Framework coverage (who exposes a tree):

| Framework | UIA tree | Notes |
|---|---|---|
| Win32 / WinForms / WPF / WinUI 3 / UWP | Yes | First-class providers |
| Qt5+ | Yes | Via Qt's UIA support |
| Chromium / Electron | Yes | Chromium's UIA provider is newer and was long behind its MSAA support — see the [Chromium UIA docs](https://chromium.googlesource.com/chromium/src/+/HEAD/docs/accessibility/browser/uiautomation.md) and the [MSEdgeExplainers provider-mappings explainer](https://microsoftedge.github.io/MSEdgeExplainers/Accessibility/UIA/explainer.html). Expect richer trees in recent versions; verify per app. |
| Java AWT/Swing | Via Java Access Bridge | Separate bridge, not UIA-native |
| Games / SDL / custom canvas | **No** | Pixel-only; OCR + SoM-from-vision territory |

Practical limits: trees can be huge (yaah caps nodes at 400 — right call),
COM round-trips are per-element (walks are slow on giant trees), names are
sometimes empty, and UIA does not cross **security boundaries** — different
integrity levels, different sessions, different desktops. That last one is
exactly why the sandbox is opaque (Q5).

**UIA remote operations**
([CoreAutomationRemoteOperation](https://learn.microsoft.com/en-us/uwp/api/windows.ui.uiautomation.core.coreautomationremoteoperation?view=winrt-28000),
[Microsoft-UI-UIAutomation](https://github.com/microsoft/Microsoft-UI-UIAutomation)):
a batched bytecode VM for UIA calls, aimed at WinUI/UWP apps. Requires WinRT
interop; no practical Python story today. Skip.

## Q2 — Python options for the backend

- **`uiautomation` (yinkaisheng)** — what yaah uses
  ([PyPI](https://pypi.org/project/uiautomation/),
  [repo](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)).
  Pure ctypes over the native UIA client COM API; no compiled deps, so it
  PyInstaller-bundles cleanly. Supports the patterns: `GetInvokePattern().Invoke()`,
  `GetTogglePattern().Toggle()`, etc. (usage shown in
  [issue #149](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows/issues/149)).
  **This is the right choice — keep it.**
- **pywinauto** ([docs](https://pywinauto.readthedocs.io/en/latest/getting_started.html))
  — richer control wrappers, `wait_chains`, two backends (`win32`/`uia`).
  Heavier walks; would duplicate what yaah already has. Not needed.
- **comtypes direct IUIAutomation** — what `uiautomation` effectively does
  under the hood. No reason to drop a level.
- **FlaUI** (C#) — irrelevant to the backend, but its .NET cousin
  (`UIAutomationClient` assemblies) is exactly what makes the *in-VM
  PowerShell* route work with zero installs (Q5).

## Q3 — Smart screenshots

- **Set-of-Mark** ([arXiv 2310.11441](https://arxiv.org/abs/2310.11441),
  [microsoft/SoM](https://github.com/microsoft/SoM)): overlaying numbered,
  spatially-grounded marks on a screenshot dramatically improves VLM
  grounding — zero-shot GPT-4V with SoM beat fine-tuned referring models on
  RefCOCOg. SoM's own pipeline needed SAM/segmentation because web pages
  have no tree; **on Windows, UIA hands you exact rects for free**, which is
  what yaah's `_som_overlay` already exploits. Keep; this is the
  state-of-the-art shape.
- **Grid overlays**: a labeled grid helps models read coordinates on
  pixel-only surfaces. yaah's rulers already do the load-bearing work; a
  full `grid_size` overlay is a cheap add-on for canvas-only windows, not a
  priority.
- **Window-targeted / occluded capture**:
  - `PrintWindow` with `PW_RENDERFULLCONTENT` (Win 8.1+) captures most
    occluded windows ([SO: capturing hidden windows](https://stackoverflow.com/questions/830359/capturing-a-window-that-is-hidden-or-minimized),
    [PW_RENDERFULLCONTENT flag](https://stackoverflow.com/questions/78427508/the-third-parameter-of-printwindow-api)).
    **[Verified live]**: on the Windows Sandbox client it returns success
    but produces a single solid color — GDI cannot read its D3D/DWM
    surface. Do not rely on it for the VM window.
  - **Windows.Graphics.Capture**
    ([namespace docs](https://learn.microsoft.com/en-us/uwp/api/windows.graphics.capture?view=winrt-28000),
    [screen capture guide](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture),
    [robmikh/Win32CaptureSample](https://github.com/robmikh/Win32CaptureSample))
    captures individual windows through DWM, including occluded ones
    (Win10 1903+ for window capture). This is the correct tool for the
    sandbox window and for occluded host windows. Cost: WinRT interop from
    Python (a small compiled helper or a PowerShell Add-Type WinRT shim).
  - DWM thumbnails (`DwmRegisterThumbnail`) composite a live preview but
    give **no pixel access** — not useful for the model.
- **OCR**: **Windows.Media.Ocr** is free, offline, per-language, and gives
  per-word bounding rects
  ([OcrEngine](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine?view=winrt-28000)).
  Usable from **Windows PowerShell 5.1** with zero installs
  ([TobiasPSP/PsOcr](https://github.com/TobiasPSP/PsOcr) — note its README:
  PowerShell 7 cannot load these WinRT projections the same way; use
  `powershell.exe`, not `pwsh`). The MSIX "package identity" caveat on the
  namespace docs applies to Store-app usage; desktop PowerShell usage is
  proven by PsOcr. Tesseract (via the toolkit) is the fallback for
  Windows-Ocr-unfriendly text. Word rects make OCR a *position* source, not
  just a text source.
- **Change detection**: hash/diff the previous crop before spending a full
  screenshot — cheap "did anything change?" gate. Small win, easy add.

## Q4 — What GUI agents actually do (consensus)

- **UFO** (Microsoft, [arXiv 2402.07939](https://arxiv.org/abs/2402.07939),
  [github](https://github.com/microsoft/UFO)) — the closest prior art to
  yaah's shape. Dual-agent (HostAgent picks the app, AppAgent drives it);
  each step observes **the app window screenshot with all controls
  annotated** plus **per-control info records** from the UIA tree; a
  control-interaction module grounds the chosen action. I.e., the
  state of the art on Windows is exactly the yaah shape: *tree + annotated
  pixels together*, not one or the other.
- **Set-of-Mark** (above) — numbered marks are the grounding mechanism of
  choice for general VLMs; coordinate regression from raw pixels is
  strictly worse for models without GUI-specific training.
- **MobileAgent** (OCR-augmented mobile agent, cited in UFO's related
  work) — OCR augmentation raises completion rates to near-human on mobile
  benchmarks. Supports OCR as the third leg.
- **CogAgent** — a VLM *trained* for GUI grounding; not applicable to yaah
  (which uses general models), but its existence marks the ceiling of
  pixel-only approaches.
- **Consensus**: use the accessibility tree when present (cheaper, exact,
  actionable via patterns); pixels + SoM when it's missing; OCR for text on
  canvas. Screenshots are for *verification* and *pixel-only surfaces*, not
  for locating standard controls.

## Q5 — The sandbox case (all **[verified live]** this session)

The VM renders on the host as one opaque window
(`WindowsSandboxClient.exe`):

1. **Host UIA is blind across the boundary.** `read_ui_tree` on the client
   hwnd returns only the client's own chrome — nested unnamed panes and a
   title bar — and zero VM-internal elements. `list_windows` likewise shows
   nothing from inside the VM. UIA does not cross the VM session/desktop
   boundary. (Consistent with the security-boundary limits in Q1; the
   [Windows Sandbox docs](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/)
   describe the VM as a fully isolated environment.)
2. **`PrintWindow` cannot read the client's surface.** Called with
   `PW_RENDERFULLCONTENT` on the occluded client window: `ok=True`,
   2048×1133 capture, **1 unique sampled color**. GDI never sees the
   D3D content. Host-side occluded capture of the VM window must go
   through Windows.Graphics.Capture (untested here — flagged as the next
   probe).
3. **In-VM UIA works with zero installs.** Inside the VM (Windows 10
   19041), `sandbox_run` + PowerShell 5.1 + `Add-Type -AssemblyName
   UIAutomationClient` found a running Notepad window, enumerated elements
   with exact bounding rects, and reported pattern availability — no
   installs, no toolkit provisioning.
   **Wrinkle found live:** the tree for classic Notepad came back shallow
   (2 unnamed panes) and the File menu reported no Invoke pattern. The
   managed `System.Windows.Automation` client is the *legacy* UIA client;
   some apps expose richer trees to the native client. Two robust in-VM
   options, in order:
   - provision **python + `uiautomation` into the toolkit** (yaah's own
     proven stack, and the toolkit persists across sandboxes by design), or
   - drive the **native UIA COM API from PowerShell** via an `Add-Type`
     C# wrapper (still zero-install, more code).
4. **The right architecture**: a `vm_ui_tree` tool = `sandbox_run` a UIA
   walker inside the VM, return the same flat-JSON shape `read_ui_tree`
   already produces, over the existing `cmd.<n>.ps1` / `done.<n>` channel.
   Actions inside the VM then go through in-VM SendInput (PowerShell can
   inject input) or in-VM patterns — the VM agent acts *where UIA is
   authoritative*. Host-side screenshots of the VM window remain the
   visual verification layer (WGC for occluded capture).
5. **Clipboard**: mapped folders already carry files both ways; the VM
   clipboard (`Set-Clipboard` / `Get-Clipboard` in-VM) is still handy for
   pasting text into VM apps without typing.

## Q6 — Existing projects: what fits, what to reference, what to skip

| Project | What it is | License | Verdict |
|---|---|---|---|
| [`uiautomation`](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows) | ctypes wrapper over native UIA; patterns included | MIT | **Already adopted** — yaah's dep. The "own stack" is a thin tool layer on top of it, not a new stack. |
| [`RPA.Windows`](https://rpaframework.org/libraries/windows/) ([source](https://github.com/robocorp/rpaframework/blob/master/packages/windows/src/RPA/Windows/keywords/action.py), [PyPI](https://pypi.org/project/rpaframework-windows/)) | Higher-level keywords over `uiautomation`: locator strings (`name:`, `id:`, `class:`) → find → click/set-value/Invoke | Apache-2.0 | **Reference, don't depend.** It already built the exact "locator → element → pattern action" layer the proposed `ui_element_action` tool needs — copy the API shape, skip the Robot Framework orientation and the extra dependency. |
| [UFO / UFO² / UFO³](https://github.com/microsoft/UFO) ([paper](https://arxiv.org/abs/2402.07939), [UFO² publication](https://www.microsoft.com/en-us/research/publication/ufo2-the-desktop-agentos/)) | Microsoft's Windows GUI agent; MIT; control-interaction module + UIA control extraction (pywinauto-based) | MIT | **Steal code/patterns, not the framework.** It is an *agent* — it owns the LLM loop, which yaah already is. Its control-extraction and action-grounding modules are the reusable parts. |
| [OmniParser](https://github.com/microsoft/OmniParser) ([models](https://huggingface.co/microsoft/OmniParser-v2.0)) | Vision model that parses screenshots into structured elements with boxes (the "SoM from pixels" complement to UIA) | CC-BY-4.0 | **Optional fallback layer.** Right tool for canvas/game UIs where no tree exists — but it needs torch + model weights (GBs), so it belongs in the toolkit or a sidecar, not the PyInstaller backend. Windows.Media.Ocr covers the cheap end. |
| [WindowsAgentArena](https://microsoft.github.io/WindowsAgentArena/) ([paper](https://arxiv.org/html/2409.08264v1)) | Microsoft's benchmark: agents run **inside** a Windows 11 VM (client process in the VM) | MIT | **Architectural validation, not a dependency.** It is a benchmark harness with its own Docker/Hyper-V provisioning — but it confirms the in-VM-agent architecture yaah's sandbox bridge uses. |
| [WinAppDriver](https://learn.microsoft.com/en-us/answers/questions/1455246/is-the-tool-winappdriver-dead-or-not) / Appium Windows driver | WebDriver server for Windows apps | — | **Dead.** Microsoft stopped maintaining it (~2021–2022); the [Appium README](https://github.com/appium/appium-windows-driver/blob/master/README.md) warns about it. Avoid. |
| pywinauto, FlaUI, Accessibility Insights | Mature UIA tooling | — | **No change.** pywinauto duplicates what `uiautomation` gives yaah; FlaUI is C#; Accessibility Insights is a manual-debugging tool (useful for humans, not for the model). |

**Bottom line:** for the host-side UIA layer we are *not* on our own —
`uiautomation` is maintained, MIT, ctypes-pure, and already in the bundle;
the missing piece (pattern invocation + locator-based element addressing)
is a ~1–2 day tool layer with two good design references (RPA.Windows,
UFO's control module). For the vision fallback, OmniParser exists if the
cheap options (UIA, Windows OCR) ever prove insufficient. The one place we
are genuinely on our own is the **in-VM sandbox bridge** — no published
project drives UIA inside Windows Sandbox over a mapped-folder channel —
but it is also the smallest piece: a walker script (same `uiautomation`
lib) plus the channel yaah already operates, with WindowsAgentArena as
prior art for the architecture.

## Q7 — AutoHotkey inside the VM as the input layer

The idea: run AHK *inside* the sandbox so all GUI input is injected in the
VM's own input session — the host's mouse/keyboard are never touched, and
the host user can keep working. **[Verified live]** on 2026-09-19.

**The premise holds.** The VM has its own isolated input session; input
injected in-VM (SendInput, ControlClick, ControlSend) never surfaces on the
host. AHK v2.0.28 is a 3.1 MB portable zip, actively maintained (Sept 2026
release), GPL-2.0 — fine as an *external tool* in the toolkit (same model
as MinGit; nothing GPL is linked into yaah).

**Live results (Notepad ×2 in the VM):**

| Technique | Result |
|---|---|
| `SendEvent` typing into the focused window | ✅ text landed exactly |
| `ControlClick "x300 y200", hwnd, , "Left", 1, "NA"` on a **background** window | ✅ click delivered, no activation (NA = no-activate) |
| `ControlSend` to a background **window** (no control target) | ❌ silent no-op — the documented caveat |
| `ControlSend keys, "Edit1", hwnd` to a background window with **explicit control** | ✅ text landed, no focus steal |
| `ControlGetText` read-back | ✅ trivially read window contents (where the managed .NET UIA Value pattern threw "Unsupported Pattern") |

**Bonus find:** [UIA-v2 by Descolada](https://github.com/Descolada/UIA-v2)
— a full UIA wrapper for AHK v2 (element finding, patterns, browser
helpers). AHK alone can therefore be the in-VM *everything*: structured UI
discovery + pattern actions + background-safe input + state reads. Python +
`uiautomation` remains preferable for running the workspace's own
tests/code, but for *driving the VM's GUI*, AHK is the more complete
single dependency.

**Operational gotchas (all hit live — these belong in a skill/nudge if
AHK is adopted):**

1. Always run `AutoHotkey64.exe /ErrorStdOut <script.ahk>` — without it,
   script errors become **modal dialogs that hang the session forever**
   (cost two timeouts during testing).
2. Launch via `Start-Process -Wait`, not direct `&` invocation (direct
   invocation produced empty output/hangs under the bootstrap's wrapper).
3. AHK v2 reserved-name traps: variables may not shadow function names
   (`log`, `WinGetList` both fail with "This Func cannot be used as an
   output variable").
4. `ControlSend` v2 signature is `(Keys, Control, WinTitle)` — flipped
   args either no-op or error.
5. Background input **requires an explicit control target**; window-level
   `ControlSend` silently does nothing.

**Architecture implication:** with AHK (+UIA-v2) or Python
(+`uiautomation`) inside the toolkit, the VM agent can find elements
(UIA), click and type without stealing focus (ControlClick/ControlSend),
and read state (ControlGetText/UIA) — entirely inside the VM. The host's
role shrinks to: boot the VM, dispatch commands, and screenshot the
sandbox window for visual verification. That is the "strong toolkit for
GUI stuff" from the original hypothesis, and it holds.

## Verification log (live probes, 2026-09-19)

| Probe | Result |
|---|---|
| Host `read_ui_tree` on `WindowsSandboxClient.exe` hwnd | Only client chrome (nested opaque panes + title bar); zero VM elements |
| Host `list_windows` while VM ran Notepad | VM apps invisible; one opaque `WindowsSandboxClient.exe` window |
| `PrintWindow(hwnd, PW_RENDERFULLCONTENT)` on the client, occluded | `ok=True`, 2048×1133, **1 unique color** — GDI cannot read the surface |
| In-VM `Add-Type UIAutomationClient` + tree walk of Notepad | Window found; elements enumerated with exact rects; pattern availability reported |
| In-VM Notepad tree depth / Invoke availability | Shallow (2 panes); File menu Invoke pattern unavailable — legacy managed UIA client limitation |
| In-VM OS | Windows 10 19041 (build 19041), PowerShell 5.1 |
| AHK v2.0.28 in toolkit: focused `SendEvent` typing | Text landed exactly |
| AHK `ControlClick … "NA"` on a background window | Click delivered, no activation |
| AHK `ControlSend` to background window (no control target) | Silent no-op (documented caveat) |
| AHK `ControlSend keys, "Edit1", hwnd` (explicit control) | Text landed in background window, no focus steal |
| AHK `ControlGetText` read-back vs .NET UIA Value pattern | AHK read trivially; managed UIA threw "Unsupported Pattern" |
