# eve-o-preview — implementation reference (from source)

Repo: https://github.com/Proopai/eve-o-preview
Revision inspected: branch `unified-source-build`, commit `3febbe503de8cf85abcbba31bc2ab89f76f7b492` (2025-11-23), cloned shallow and read locally.
Clone left at `_eve-o-preview/` during research; all paths below are relative to it.

> **Correction to the task premise:** this is **not a C++ app**. It is a **C# WinForms** desktop app
> (`net8.0-windows8.0`, `src/Eve-O-Preview/Eve-O-Preview.csproj`), built on .NET 8 with Win32 interop.
> Everything below is verified in the C# source; the mechanics translate 1:1 to raw Win32/ctypes.

## Architecture map

| Concern | File |
|---|---|
| Thumbnail window (Form) + gesture handlers | `src/Eve-O-Preview/View/Implementation/ThumbnailView.cs` |
| Thumbnail window designer defaults | `src/Eve-O-Preview/View/Implementation/ThumbnailView.Designer.cs` |
| Mouse-capture overlay window (click surface) | `src/Eve-O-Preview/View/Implementation/ThumbnailOverlay.cs`, `ThumbnailOverlay.Designer.cs` |
| Live DWM thumbnail wrapper | `src/Eve-O-Preview/Services/Implementation/DwmThumbnail.cs` |
| Destination-rect math (live) | `src/Eve-O-Preview/View/Implementation/LiveThumbnailView.cs` |
| Orchestration / refresh timer / hide logic | `src/Eve-O-Preview/Services/Implementation/ThumbnailManager.cs` |
| Config + defaults + clamps | `src/Eve-O-Preview/Configuration/Implementation/ThumbnailConfiguration.cs` |
| Win32/DWM interop | `src/Eve-O-Preview/Services/Interop/*.cs` (`DwmNativeMethods.cs`, `DWM_THUMBNAIL_PROPERTIES.cs`, `DWM_TNP_CONSTANTS.cs`, `User32NativeMethods.cs`, `InteropConstants.cs`) |
| Window activation/minimize helpers | `src/Eve-O-Preview/Services/Implementation/WindowManager.cs` |
| Source-process enumeration | `src/Eve-O-Preview/Services/Implementation/ProcessMonitor.cs` |

Two windows exist **per source client**:
1. `ThumbnailView` (Form) — hosts the DWM thumbnail, has the border/highlight color.
2. `ThumbnailOverlay` (Form, owned by #1, transparent, sits exactly on top of #1's client area) — **receives all mouse input** for label painting, highlight framing and the gesture handlers.

`ThumbnailView.IsKnownHandle` (`ThumbnailView.cs:229-233`) treats three handles as "mine": source client HWND (`Id`), view HWND, overlay HWND — the manager uses this to ignore its own windows when tracking foreground.

---

## 1. Window creation

### 1.1 Thumbnail window

Designer defaults (`ThumbnailView.Designer.cs:21-44`):

```csharp
this.AutoScaleMode = System.Windows.Forms.AutoScaleMode.None;
this.BackColor = System.Drawing.Color.FromArgb(255,0,0,1);   // near-black, off-by-1 transparency key
this.BackgroundImageLayout = System.Windows.Forms.ImageLayout.Stretch;
this.ClientSize = new System.Drawing.Size(153, 89);
this.ControlBox = false;
this.DoubleBuffered = true;
this.FormBorderStyle = System.Windows.Forms.FormBorderStyle.SizableToolWindow;
this.MaximizeBox = false;
this.MinimizeBox = false;
this.MinimumSize = new System.Drawing.Size(20, 20);
this.Opacity = 0.1D;
this.ShowIcon = false;
this.ShowInTaskbar = false;
this.Text = "Preview";
this.TopMost = true;
```

Extended style injection (`ThumbnailView.cs:516-524`):

```csharp
protected override CreateParams CreateParams
{
    get
    {
        var Params = base.CreateParams;
        Params.ExStyle |= (int)InteropConstants.WS_EX_TOOLWINDOW;   // 0x00000080
        return Params;
    }
}
```

Borderless toggle (`ThumbnailView.cs:272-285`, `SetFrames`): `FormBorderStyle.SizableToolWindow` (framed) ↔ `FormBorderStyle.None` (= `WS_POPUP`, no caption, no sizing border). Idempotence check avoids re-creating the window handle.

**Notes on styles:**
- `WS_EX_TOOLWINDOW` keeps thumbnails out of Alt-Tab and the taskbar. `ShowInTaskbar=false` also excludes from taskbar.
- **`WS_EX_LAYERED` is never set manually** — setting `Form.Opacity` makes WinForms add `WS_EX_LAYERED` + `SetLayeredWindowAttributes` itself (`SetOpacity`, `ThumbnailView.cs:241-270`). Initial opacity 0.1, config applied every tick.
- **`WS_EX_NOACTIVATE` is NOT used.** The thumbnail window *does* take activation; the app relies on being foreground-tracked (see §6). Foreground of the overlay/view is how it detects "user is hovering/interacting".
- Topmost: `Form.TopMost = true` (designer) and re-applied per tick through `SetTopMost` (`ThumbnailView.cs:294-305`), which sets it on **both** view and overlay. WinForms maps this to `SetWindowPos(HWND_TOPMOST)`.

### 1.2 Overlay window (the actual input surface)

`ThumbnailOverlay.Designer.cs:91-108`:

```csharp
AutoScaleMode = None;
BackColor = System.Drawing.Color.FromArgb(0, 0, 1);      // (0,0,1)
ControlBox = false;
FormBorderStyle = System.Windows.Forms.FormBorderStyle.None;
MaximizeBox = false; MinimizeBox = false;
ShowIcon = false; ShowInTaskbar = false;
SizeGripStyle = System.Windows.Forms.SizeGripStyle.Hide;
Text = "PreviewOverlay";
TransparencyKey = System.Drawing.Color.FromArgb(0, 0, 1); // (0,0,1) pixels become click-through-transparent
```

- Created as an **owned form** of the thumbnail (`this.Owner = owner;` `ThumbnailOverlay.cs:31`), `WS_EX_TOOLWINDOW` via the same `CreateParams` override (`ThumbnailOverlay.cs:254-262`).
- Its `PictureBox` (`OverlayAreaPictureBox`) is `Dock=Fill`, `Cursor=Hand`, and gets **all five mouse handlers** (Designer `ThumbnailOverlay.Designer.cs:49-54`), which are forwarded to ThumbnailView callbacks (`ThumbnailOverlay.cs:41-60`; wiring at `ThumbnailView.cs:78-84`). The `OverlayLabel` and `CycleGroupIndicator` child controls re-wire the same five events (Designer lines 68-72, 85-89) so gestures work anywhere.
- "Prevent previews" fake-preview mode makes the PictureBox opaque (`EnableFakePreview`, `ThumbnailOverlay.cs:198-236`).
- **Why a separate overlay window:** the transparency key makes pixels outside painted text click-through to *the thumbnail window beneath* only for unpainted (0,0,1) areas; the PictureBox and label receive mouse events for the whole rect. It also allows painting the title label and highlight frame without fighting the DWM thumbnail rendering.

### 1.3 Overlay geometry

Repositioned whenever the view moves/resizes (`RefreshOverlay`, `ThumbnailView.cs:473-506`):

```csharp
Size overlaySize = this.ClientSize;
Point overlayLocation = this.Location;
int borderWidth = (this.Size.Width - this.ClientSize.Width) / 2;
overlayLocation.X += borderWidth;
overlayLocation.Y += (this.Size.Height - this.ClientSize.Height) - borderWidth;
this._overlay.Size = overlaySize;
this._overlay.Location = overlayLocation;
```

i.e. overlay client area == thumbnail client area, offset by left border / top border (caption height). Shown once, then only repositioned (`_isOverlayVisible` guard, lines 484-490).

### 1.4 DWM thumbnail registration & destination rect

`DwmThumbnail.Register` (`DwmThumbnail.cs:21-55`):

```csharp
this._properties = new DWM_THUMBNAIL_PROPERTIES();
this._properties.dwFlags = DWM_TNP_CONSTANTS.DWM_TNP_VISIBLE
                            + DWM_TNP_CONSTANTS.DWM_TNP_OPACITY
                            + DWM_TNP_CONSTANTS.DWM_TNP_RECTDESTINATION
                            + DWM_TNP_CONSTANTS.DWM_TNP_SOURCECLIENTAREAONLY;
this._properties.opacity = 255;
this._properties.fVisible = true;
this._properties.fSourceClientAreaOnly = true;
...
this._handle = DwmNativeMethods.DwmRegisterThumbnail(destination, source);
```

- `destination` = thumbnail Form's HWND, `source` = the EVE client HWND (`LiveThumbnailView.cs:56-61`: `WindowManager.GetLiveThumbnail(this.Handle, this.Id)`).
- **`DWM_TNP_RECTSOURCE` is never set** → the thumbnail shows the client's full client area. `fSourceClientAreaOnly = true` excludes the client's own window frame.
- **Aspect ratio is left to DWM.** The app never computes a letterboxed destination rect for display. DWM scales the source into `rcDestination` preserving aspect (letterboxing inside the rect if the ratios differ); the app simply gives it the whole client rect (minus highlight insets, §3-adjacent).
- Exceptions during register/update/unregister are deliberately swallowed (`ArgumentException` = source died mid-call; `COMException` = DWM unavailable) — `DwmThumbnail.cs:37-54, 89-100`.
- Composition gate: `WindowManager.cs:32-36` — assumed enabled on Win8+/Win10+, `DwmIsCompositionEnabled()` only for Win7.
- Re-registration: `RefreshThumbnail` (`LiveThumbnailView.cs:25-36`) re-registers the thumbnail **every forced refresh cycle** (every 2nd timer tick ≈ 1 s; `FORCED_REFRESH_CYCLE_THRESHOLD = 2`, `ThumbnailManager.cs:27, 457-468`), unregistering the old one *after* creating the new one ("To prevent flickering the old broken thumbnail is removed AFTER the new shiny one is created", line 27).

Destination-rect update (`LiveThumbnailView.cs:38-54`):

```csharp
protected override void ResizeThumbnail(int baseWidth, int baseHeight, int highlightWidthTop,
                                        int highlightWidthRight, int highlightWidthBottom, int highlightWidthLeft)
{
    var left = 0 + highlightWidthLeft;
    var top = 0 + highlightWidthTop;
    var right = baseWidth - highlightWidthRight;
    var bottom = baseHeight - highlightWidthBottom;
    if ((this._startLocation.X == left) && (this._startLocation.Y == top)
        && (this._endLocation.X == right) && (this._endLocation.Y == bottom))
        return; // No update required
    this._startLocation = new Point(left, top);
    this._endLocation = new Point(right, bottom);
    this._thumbnail.Move(left, top, right, bottom);
    this._thumbnail.Update();     // -> DwmUpdateThumbnailProperties
}
```

`DwmThumbnail.Move` only mutates a cached `DWM_THUMBNAIL_PROPERTIES.rcDestination`; `Update()` calls `DwmUpdateThumbnailProperties` (`DwmThumbnail.cs:77-101`). Coordinates are **client-area coordinates of the preview window** (DWM requirement), equal to `ClientSize` unless the active-client highlight is drawn (then the highlight border pixels are excluded from the thumbnail rect; inset math at `ThumbnailView.cs:440-471` — it computes left/right insets from the base aspect ratio to keep the visible client square-ish inside the highlight frame; this is the *only* place aspect math appears).

The refresh loop (`ThumbnailManager.RefreshThumbnails`, `ThumbnailManager.cs:399-554`) per tick: track foreground window, maybe hide all (config), then per view: set location/size from config, `SetOpacity`, `SetTopMost`, `SetHighlight`, then `Show()` or `Refresh(forceRefresh)`.

---

## 2. Mouse gestures

**All gestures live in `ThumbnailView.cs` and are driven by the overlay's PictureBox events** — there is **no `SetCapture`, no `WM_NCLBUTTONDOWN`/`HTCAPTION` trick, no `DefWindowProc` move loop**. `WM_NCLBUTTONDOWN`/`HTCAPTION` constants are defined (`InteropConstants.cs:75-76`) and imported `ReleaseCapture` exists (`User32NativeMethods.cs:26-27`) but **are never used** — dead code.

### 2.1 Dispatch table — `MouseDownEventHandler` (`ThumbnailView.cs:660-686`)

```csharp
protected virtual void MouseDownEventHandler(MouseButtons mouseButtons, Keys modifierKeys)
{
    switch (mouseButtons)
    {
        case MouseButtons.Left when modifierKeys == Keys.Control:
            this.ThumbnailDeactivated?.Invoke(this.Id, false);        // minimize client
            break;
        case MouseButtons.Left when modifierKeys == Keys.Shift:
            this.ThumbnailToggleCycleGroup?.Invoke(this.Id);          // exclude/include from cycle groups
            break;
        case MouseButtons.Left when modifierKeys == (Keys.Control | Keys.Shift):
            this.ThumbnailDeactivated?.Invoke(this.Id, true);         // switch to last external app
            break;
        case MouseButtons.Left:
            var oldWindow = this._thumbnailManager.GetActiveClient();
            this.ThumbnailActivated?.Invoke(this.Id);                 // activate EVE client
            this.SetHighlight();
            this.Refresh(true);
            oldWindow?.ClearBorder();
            break;
        case MouseButtons.Right:
        case MouseButtons.Left | MouseButtons.Right:
            this.EnterCustomMouseMode();                              // begin move/resize gesture mode
            break;
    }
}
```

Effects (manager side):
- `ThumbnailActivated` → `ThumbnailManager.cs:676-695`: `Task.Run(ActivateWindow)` → `SetForegroundWindow(source)` + `SetFocus(source)`, and if the source is minimized `ShowWindowAsync(SW_RESTORE)` with min-animation suppressed via `SystemParametersInfo(SPI_SETANIMATION)` around it (`WindowManager.cs:200-221`).
- `ThumbnailDeactivated(id, false)` → `ThumbnailManager.cs:707-716`: `MinimizeWindow(source)` via `WM_SYSCOMMAND/SC_MINIMIZE` (animation-dependent, `WindowManager.cs:223-257`).
- `ThumbnailDeactivated(id, true)` → activates `_externalApplication` — the last foreground HWND that was neither a client nor one of its own windows, captured every tick (`ThumbnailManager.cs:426-429`).
- **Activation fires on button-down, not click-release.**

### 2.2 Gesture mode state machine (`ThumbnailView.cs:599-657`)

Enter (`EnterCustomMouseMode`, 620-626): restore any hover-zoom, set `_isCustomMouseModeActive = true`, snapshot `_baseMousePosition = Control.MousePosition` (screen coords).

Per-move (`MouseMove_Handler` 562-568 → `ProcessCustomMouseMode` 628-651) — **verbatim**:

```csharp
private void ProcessCustomMouseMode(bool leftButton, bool rightButton)
{
    Point mousePosition = Control.MousePosition;
    int offsetX = mousePosition.X - this._baseMousePosition.X;
    int offsetY = mousePosition.Y - this._baseMousePosition.Y;
    this._baseMousePosition = mousePosition;      // re-anchor every WM_MOUSEMOVE (incremental deltas)

    if (!_config.LockThumbnailLocation)
    {
        // Left + Right buttons trigger thumbnail resize
        // Right button only trigger thumbnail movement
        if (leftButton && rightButton)
        {
            this.Size = new Size(this.Size.Width + offsetX, this.Size.Height + offsetY);
            this._baseZoomSize = this.Size;
        }
        else
        {
            this.Location = new Point(this.Location.X + offsetX, this.Location.Y + offsetY);
            this._baseZoomLocation = this.Location;
            this.WindowMoved = true;
        }
    }
}
```

Exit (`MouseUp_Handler` 570-588): on **right-button-up** → `_isCustomMouseModeActive = false`, then optional **snap-to-grid** (verbatim):

```csharp
if (_config.ThumbnailSnapToGrid && this.WindowMoved)
{
    var x = (int)Math.Round((double)this.Location.X / (double)_config.ThumbnailSnapToGridSizeX) * _config.ThumbnailSnapToGridSizeX;
    var y = (int)Math.Round((double)this.Location.Y / (double)_config.ThumbnailSnapToGridSizeY) * _config.ThumbnailSnapToGridSizeY;
    this.Location = new Point(x, y);
    this._baseZoomLocation = this.Location;
    this.WindowMoved = false;
}
```

**Mechanics summary:**
- Gesture truth is polled from `Control.MousePosition` (=`GetCursorPos`, **screen coordinates**) on every `WM_MOUSEMOVE`; deltas are **per-event incremental** with the base re-anchored each time. There is **no drag-distance threshold** — the first pixel of movement moves/resizes.
- In WinForms the implicit mouse capture during button-down keeps `MouseMove` flowing to the overlay; a raw Win32 port must `SetCapture` on button-down and `ReleaseCapture` on up (or rely on `GetCapture` semantics of your toolkit).
- **Which drag does what:** right-only drag = **move**; left+right (both buttons) drag = **resize** (width from X delta, height from Y delta); plain left drag = *nothing* (left already activated the client on down).
- If both buttons are held and one is released mid-drag, the next move event re-evaluates flags → the gesture silently switches to move.
- Plain right-click (press+release, no movement): enters and exits the mode with no effect; no context menu is ever shown.
- Both move and resize are disabled entirely while `LockThumbnailLocation` is set (config option, `ThumbnailConfiguration.cs:135`).
- The resize uses the **outer window size** (`Form.Size`), not `ClientSize`; WinForms `MinimumSize`/`MaximumSize` (§4) clamp it automatically because the assignment goes through `Form.Size`.
- `MouseEnter` (`ThumbnailView.cs:544-550`) always `ExitCustomMouseMode()`s + snapshots the window size/location before hover-zoom — so a stale zoom can't be mutated.

---

## 3. Aspect ratio during resize

**There is none.** The resize math is the two-liner above (`ThumbnailView.cs:641`): `Size += (offsetX, offsetY)` — width follows X, height follows Y, **top-left corner stays fixed** (right/bottom edges grow/shrink), no anchoring options, no ratio derivation, no clamp of its own (only the Form min/max limits of §4).

The app deliberately lets **DWM** preserve the source aspect ratio visually: with `rcSource` unset and `DWM_TNP_SOURCECLIENTAREAONLY=true`, DWM scales the client image into `rcDestination` preserving aspect ratio, letterboxing inside the destination rect. A thumbnail "resized" to a non-native ratio still shows an undistorted image with dead space.

The only aspect calculations in the codebase are:
- Active-client highlight inset (`HighlightThumbnail`, `ThumbnailView.cs:440-471`): keeps the *thumbnail image* at the client's aspect ratio inside a highlighted border:

```csharp
double baseAspectRatio = ((double)baseWidth) / baseHeight;   // clientWidth / clientHeight of the thumbnail view
int actualHeight = baseHeight - 2 * this._highlightWidth;
double desiredWidth = actualHeight * baseAspectRatio;
int actualWidth = (int)Math.Round(desiredWidth, MidpointRounding.AwayFromZero);
int highlightWidthLeft = (baseWidth - actualWidth) / 2;
int highlightWidthRight = baseWidth - actualWidth - highlightWidthLeft;
```

- Hover-zoom (`ZoomIn`, `ThumbnailView.cs:340-390`) scales both dimensions by `ThumbnailZoomFactor` (config default 2, clamped 2..10, `ThumbnailConfiguration.cs:451`) and repositions the window so the configured anchor (NW…SE, 9 values) stays fixed; comment at 353-355: "First change size, THEN move the window. Otherwise there is a chance to fail in a loop."

If you want the *window* to keep the source aspect while resizing, you must add that yourself — derive the ratio from `GetClientRect(source)` (or `DwmQueryThumbnailSourceSize`) and pick which dimension the drag delta drives.

---

## 4. Min/max size clamping

Config properties (`ThumbnailConfiguration.cs`):

| Property | Default (code, line 121-123) | README claim |
|---|---|---|
| `ThumbnailSize` | `new Size(384, 216)` | — (GUI: width 100-640, height 80-400) |
| `ThumbnailMinimumSize` | `new Size(192, 108)` | "100, 80" |
| `ThumbnailMaximumSize` | `new Size(960, 540)` | "640, 400" |

**The README's "100, 80"/"640, 400" numbers are stale** — current code defaults are 192×108 / 960×540 (`ThumbnailConfiguration.cs:121-123`). (The README's 100..640 / 80..400 ranges match the GUI spin-edit ranges, not the config defaults.)

Applied in three places, **width and height independently** in all of them:

1. **On config load** — `ApplyRestrictions` (`ThumbnailConfiguration.cs:440-453`):

```csharp
this.ThumbnailSize = new Size(
    ThumbnailConfiguration.ApplyRestrictions(this.ThumbnailSize.Width,  this.ThumbnailMinimumSize.Width,  this.ThumbnailMaximumSize.Width),
    ThumbnailConfiguration.ApplyRestrictions(this.ThumbnailSize.Height, this.ThumbnailMinimumSize.Height, this.ThumbnailMaximumSize.Height));
```

   with `ApplyRestrictions(value, min, max)` = inclusive clamp (455-468). Also clamps: refresh period 300..1000 ms (10..1000 on Linux), resize timeout 200..5000 ms, opacity 20..100 %, zoom factor 2..10, highlight thickness 1..6.
2. **On every thumbnail window** — `ThumbnailManager.cs:316-318` (applied **after** `SetFrames`, with comment "Max/Min size limitations should be set AFTER the frames are disabled / Otherwise thumbnail window will be unnecessary resized"):

```csharp
view.SetFrames(this._configuration.ShowThumbnailFrames);
// Max/Min size limitations should be set AFTER the frames are disabled
view.SetSizeLimitations(this._configuration.ThumbnailMinimumSize, this._configuration.ThumbnailMaximumSize);
```

   → `ThumbnailView.SetSizeLimitations` (`ThumbnailView.cs:235-239`) assigns `Form.MinimumSize` / `Form.MaximumSize`. WinForms enforces these **per-dimension** on every `Size` set — including the gesture resize — so the right-drag resize is clamped by the OS-framework layer, not by gesture math. Designer floor is 20×20 (`ThumbnailView.Designer.cs:31`).
3. **On the settings GUI** — `MainForm.cs:480-497` `ThumbnailSizeChanged_Handler` clamps the spin-edit values: `Math.Min(Math.Max(w, _minimumSize.Width), _maximumSize.Width)` (and same for height), independently.

Caveat: hover-zoom **temporarily clears** `MaximumSize` (`this.MaximumSize = new Size(0, 0);` `ThumbnailView.cs:356`) and `RestoreWindowSizeAndLocation` puts the original back (613-618) — a re-implementer that clamps manually must remember to allow zoom to exceed the max.

---

## 5. Pinning and other gestures/actions

- **No per-thumbnail pin gesture exists.** There is no way to pin a single thumbnail via mouse. Topmost is global: `ShowThumbnailsAlwaysOnTop` (default **true**, `ThumbnailConfiguration.cs:114`), re-applied every tick (`ThumbnailManager.cs:534`) through `SetTopMost` (`ThumbnailView.cs:294-305`, sets both view and overlay).
- **Hover temporarily pins**: `ThumbnailViewFocused` (`ThumbnailManager.cs:637-655`) — on mouse-enter: `SetTopMost(true)`, `SetOpacity(1.0)`, and if `ThumbnailZoomEnabled`, zoom in. `ThumbnailViewLostFocus` (657-674) reverses it on mouse-leave. Hover also suspends the per-tick layout/size re-application (`_isHoverEffectActive`, lines 474, 524) so the zoom isn't fought by the timer.
- **Close-thumbnail gesture:** none. Thumbnails are removed only when the source process disappears (`UpdateThumbnailsList` removed-loop, `ThumbnailManager.cs:371-391` → `view.Close()`, which closes overlay + view, `ThumbnailView.cs:220-227`). The Active-Clients tab checkbox hides a thumbnail persistently-for-the-session via `DisableThumbnail`/`IsThumbnailDisabled` (`ThumbnailConfiguration.cs:427-435`; enforced in the refresh loop, `ThumbnailManager.cs:496-503`).
- **Minimize client**: Ctrl+Click (§2.1). **Switch to last non-EVE app**: Ctrl+Shift+Click. **Toggle cycle-group exclusion**: Shift+Click (indicator icon rendered by overlay, `ThumbnailOverlay.SetCycleGroupIndicator`, lines 66-126).
- **Per-client activation hotkeys** (config `ClientHotkey`) and **cycle-group hotkeys** (F13/F14/F15/F16… defaults) via `RegisterHotKey` + `Application.AddMessageFilter` (`HotkeyHandler.cs:61-110`; registration at `ThumbnailManager.cs:83-98, 238-277`), plus `MinimizeAllClientsHotkeys` (default `Control+F22`).
- **Preventing input from reaching the source window:** there is *no forwarding* to prevent — the overlay simply swallows every mouse message (the whole thumbnail surface belongs to the overlay), and the app's contract (README lines 5, 11-16) is that it never sends input to EVE. A click *activates* the client instead of being relayed.
- **Client-side chrome manipulation**: `ApplyCaptionBar` (`ThumbnailManager.cs:904-914`, with `SetWindowStyle` 886-903) strips/adds `WS_CAPTION` and `WS_THICKFRAME` on the **source client window** (`GWL_STYLE` via `GetWindowLong`/`SetWindowLong`, `User32NativeMethods.cs:32-36`) — not on the thumbnail.
- **Thumbnail-vs-thumbnail snapping** (`SnapThumbnailView`, `ThumbnailManager.cs:807-885`): 4 corner points per view, thresholds `max(20, dimension/10)`, 9 hand-picked corner-pair combinations (`TestViewPoints`, 861-885) — applies to borderless thumbnails only and uses the **global** `ThumbnailSize` for all views (quirk: ignores per-client sizes, lines 821-822).
- **Wine/compat mode**: `StaticThumbnailView` replaces the DWM thumbnail with a `BitBlt`-copied static image (`WindowManager.GetStaticThumbnail`, 295-325; factory `ThumbnailViewFactory.cs:18-29`); its PictureBox returns `HTTRANSPARENT` from `WM_NCHITTEST` so clicks pass through to the form (`StaticThumbnailImage.cs:8-21`).

---

## 6. Surprises / quirks a re-implementer will trip over

1. **Polling, not events.** Everything is refreshed by a `DispatcherTimer` at `ThumbnailRefreshPeriod` (default 500 ms, clamped 300..1000; `ThumbnailConfiguration.cs:97,445`; `ThumbnailManager.cs:77-79, 291-295`). Source windows are discovered by diffing `Process.GetProcesses()` on `Process.MainWindowHandle` (`ProcessMonitor.cs:72-115`) → **one thumbnail per process** (secondary windows never appear), and removal detection is "process gone from the list", not `WM_DESTROY`.
2. **Periodic re-registration of the DWM thumbnail** every 2nd tick (§1.4). This is a deliberate workaround for thumbnails going stale/black; order matters (new registered before old unregistered).
3. **Resize-event suppression window** (`ThumbnailView.cs:508-513, 532-542`): `Resize` events are ignored until `DateTime.UtcNow` passes `_suppressResizeEventsTimestamp` (now + `ThumbnailResizeTimeoutPeriod`, default 500 ms), set before programmatic Show/Close/SetFrames — a workaround for WinForms firing `Resize` with inconsistent `ClientSize`. If you resize preview windows programmatically in your own app and react to `WM_SIZE`, you'll want an equivalent guard.
4. **Opacity snapping & exception swallowing** (`ThumbnailView.cs:241-270`): opacity ≥ 0.9 snaps to 1.0; deltas < 0.1 are skipped; `Win32Exception` from WinForms internals is swallowed and retried next tick. Overlay opacity = 1.0 if thumbnail opacity > 0.8, else `1 - (1-op)/2` (line 261).
5. **DPI**: `Application.SetHighDpiMode(HighDpiMode.PerMonitorV2)` in code (`Program.cs:74-76`); the manifest's `dpiAware`/`dpiAwareness` blocks are **commented out** (`app.manifest:48-83`). All coordinates are physical pixels; there is no manual per-monitor DPI math in the thumbnail code.
6. **Transparency-key hack**: the magic color `RGB(0,0,1)` is the transparency key for the overlay (`ThumbnailOverlay.Designer.cs:94,108`) and a near-black `BackColor (255,0,0,1)` for the view (`ThumbnailView.Designer.cs:23`); the README warns not to pick `#000001` as the "prevent previews" background or you'll fight the transparency key.
7. **Hide-on-lost-focus debounce**: `HideThumbnailsOnLostFocus` waits `HideThumbnailsDelay` (default 2 ticks ≈ 1 s) before hiding all thumbnails (`ThumbnailManager.cs:117-119, 437-455`) to survive Alt-Tab flicker; a `null` foreground window skips the whole refresh tick (402-409).
8. **Window-position sanity thresholds** for saved client layouts: `WINDOW_POSITION_THRESHOLD_LOW = -10_000`, `HIGH = 31_000`, `WINDOW_SIZE_THRESHOLD = 10` (`ThumbnailManager.cs:24-26, 1050-1055`) — filters minimized/off-screen garbage.
9. **Title mapping**: `Title` setter strips `"EVE - "` and maps `"EVE Frontier - "` → `"*"` for the overlay label (`ThumbnailView.cs:102`); em-dash `—` in titles is replaced with `-` (`ProcessMonitor.cs:94`).
10. **Dead Win32 imports**: `ReleaseCapture`, `WM_NCLBUTTONDOWN`, `HTCAPTION`, `WM_SIZE` are declared but unused (`User32NativeMethods.cs:26-30`; `InteropConstants.cs:64,75-76`) — evidence an earlier implementation used the classic `HTCAPTION` drag trick; the shipped code does manual math instead.
11. **Stale README defaults** for min/max thumbnail size (§4) and the README gesture table omits that the left button alone does nothing while dragging.
12. **Activation-on-press**: the client is activated on `MouseDown` (not on click-up) — feel matters if you re-implement.

---

## 7. Translation notes → Python + ctypes Win32

Target: preview window via `CreateWindowEx`, DWM thumbnails via `dwmapi`.

1. **Preview window styles.** `CreateWindowEx` with:
   - `dwStyle = WS_POPUP | WS_VISIBLE` for borderless (matches `FormBorderStyle.None`); add `WS_THICKFRAME` (+ `WS_CAPTION` if you want a titlebar) for the framed variant.
   - `dwExStyle = WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_LAYERED`. `WS_EX_TOOLWINDOW` = no Alt-Tab/taskbar; `WS_EX_TOPMOST` = the app's default pin (`ShowThumbnailsAlwaysOnTop=true`); `WS_EX_LAYERED` + `SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA)` reproduces `Form.Opacity` (0-255; the code snaps ≥0.9→1.0 and skips <0.1 deltas).
   - Do **not** use `WS_EX_NOACTIVATE` blindly: this app lets its windows activate and uses foreground tracking. If you prefer previews that never steal focus, add `WS_EX_NOACTIVATE` and return `MA_NOACTIVATE` from `WM_MOUSEACTIVATE` — but then implement your own hover tracking (`TrackMouseEvent`).
   - Register the class with `hbrBackground = NULL`/black; paint nothing — the DWM thumbnail renders over your window's client area.
2. **DWM thumbnail.**
   - `DwmRegisterThumbnail(HwndDestination=hwnd_preview, HwndSource=hwnd_client, &thumb)` — destination must be your (shown) window; call after `ShowWindow`.
   - `DWM_THUMBNAIL_PROPERTIES` in ctypes: `c_uint dwFlags; RECT rcDestination; RECT rcSource; c_ubyte opacity; BOOL fVisible; BOOL fSourceClientAreaOnly` (`src/Eve-O-Preview/Services/Interop/DWM_THUMBNAIL_PROPERTIES.cs`). Flags: `DWM_TNP_RECTDESTINATION(0x1) | DWM_TNP_OPACITY(0x4) | DWM_TNP_VISIBLE(0x8) | DWM_TNP_SOURCECLIENTAREAONLY(0x10)`. Set `opacity=255`, `fSourceClientAreaOnly=True`, leave `rcSource` zeroed (full client area), `rcDestination = (0, 0, clientW, clientH)` in **preview-window client coordinates**.
   - Call `DwmUpdateThumbnailProperties` after every move/resize/hide of the preview (the code updates it in `Refresh`, i.e. on move/resize/highlight-change only, with an unchanged-check).
   - HRESULT handling: treat `E_INVALIDARG` (0x80070057) as "source window died" and tear down the thumbnail; treat DWM-off as "skip" (mirror `DwmThumbnail.cs:37-54, 89-100`).
   - Optionally re-register on a slow timer if you see stale frames; unregister the *new* failure ordering: always create the new registration before unregistering the old one.
3. **Gestures** (replace WinForms implicit capture):
   - On `WM_RBUTTONDOWN`: `SetCapture(hwnd)`, `drag_origin = GetCursorPos()` (screen px), `drag_active = True`. On `WM_MOUSEMOVE` while captured: `dx, dy = now - last` (re-anchor `last = now` each event — incremental deltas, exactly like `ProcessCustomMouseMode`); if both-buttons-down → resize, else → move.
   - Move: `SetWindowPos(hwnd, 0, x+dx, y+dy, 0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)` — note the original uses `SWP_NOACTIVATE`-free semantics (WinForms move keeps activation); use `SWP_NOACTIVATE` unless you want the preview focused.
   - Resize (both buttons): `SetWindowPos(hwnd, 0, 0, 0, w+dx, h+dy, SWP_NOMOVE | ...)` — width from X delta, height from Y delta, **top-left anchored**, applied to the **outer window size** (use `GetWindowRect`, not `GetClientRect`, if you have borders).
   - On `WM_RBUTTONUP` / `WM_LBUTTONUP`: `ReleaseCapture()`, end drag; optional snap-to-grid: `x = round(x/gridx)*gridx` (defaults on in the original: 100/50 px).
   - Modifiers at button-down: `GetAsyncKeyState(VK_CONTROL) & 0x8000` / `VK_SHIFT` — then: plain LMB-down → activate source; LMB+Ctrl → minimize source (`PostMessage(src, WM_SYSCOMMAND, SC_MINIMIZE, 0)`); LMB+Shift → your app's toggle; LMB+Ctrl+Shift → activate last external foreground HWND (track it from your poll loop, `ThumbnailManager.cs:426-429`).
   - **Activate source on LMB down**: `SetForegroundWindow(src); SetFocus(src);` and if `IsIconic(src)`: `ShowWindowAsync(src, SW_RESTORE)`. On modern Windows, `SetForegroundWindow` from a background process can be rejected; the classic workaround (attach thread input) is *not* used by this app — WinForms gets away with it because the click gives the preview foreground first, so replicate: activating from within a handler of a window that just received the click is allowed.
   - There is **no threshold**: first pixel of movement acts. If you want a dead-zone, add it yourself (this app has none).
4. **Aspect ratio.** Set `rcSource` only if you want cropped zoom; for plain scaling leave it zero and let DWM letterbox. If you want the window itself to keep the source ratio during resize: `ratio = srcW / srcH` from `GetClientRect(src)` (or `DwmQueryThumbnailSourceSize(thumb, &size)` — imported but unused in this app), then when dragging resize pick one driven dimension from the delta and compute the other; clamp both. None of this exists in the reference implementation.
5. **Clamping.** Apply min/max per-dimension inside your resize handler (`w = max(minw, min(w, maxw))`, same for h) using e.g. 192×108 / 960×540 defaults (code values, not README). If you implement hover-zoom, allow zoom to exceed the max, as the original does.
6. **Polling loop.** One `SetTimer` at ~500 ms that: diffs source windows (by process main window), re-applies size/pos/topmost/opacity, re-registers thumbnails every ~1 s, and tracks foreground (`GetForegroundWindow`) for hide-on-focus-loss and "last external app". Debounce hide with a 2-cycle counter.
7. **Input swallowing.** Your preview window must simply *not* forward any input; clicking it activates the source. If you render labels inside the preview, be aware the original puts them in a separate owned, transparent (`WS_EX_LAYERED` + color key) window that owns all hit-testing — you can do it in one window instead: handle `WM_NCHITTEST` to return `HTTRANSPARENT` for label pixels if you want click-through there (`StaticThumbnailImage.cs:8-21` shows the pattern).
8. **Per-monitor DPI.** Call `SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)` before creating windows; do all math in physical pixels; don't try to scale the DWM destination rect by DPI — it's already in the destination window's client pixels.
9. **Source-window lifetime.** Poll `IsWindow(src)`/process list each tick and destroy thumbnail + preview when the source vanishes; wrap `DwmUpdateThumbnailProperties`/`DwmUnregisterThumbnail` in try/except around the HRESULT — a dying source can fail any of them mid-flight (the reference swallows `ArgumentException`/`COMException` for exactly this).

### Verified-verbatim checklist (what to copy)

| Mechanic | File:Lines |
|---|---|
| `WS_EX_TOOLWINDOW` injection | `ThumbnailView.cs:516-524`, `ThumbnailOverlay.cs:254-262` |
| Designer style defaults | `ThumbnailView.Designer.cs:21-44`, `ThumbnailOverlay.Designer.cs:91-108` |
| Overlay geometry math | `ThumbnailView.cs:492-505` |
| DWM register/properties | `DwmThumbnail.cs:21-55`, `77-101`; `DWM_TNP_CONSTANTS.cs:5-9` |
| Dest-rect update + skip-if-unchanged | `LiveThumbnailView.cs:38-54` |
| Re-register each 2nd cycle | `LiveThumbnailView.cs:25-36`; `ThumbnailManager.cs:27,457-468` |
| Gesture dispatch | `ThumbnailView.cs:660-686` |
| Move/resize incremental math | `ThumbnailView.cs:628-651` |
| Snap-to-grid on release | `ThumbnailView.cs:570-588` |
| Highlight aspect inset math | `ThumbnailView.cs:440-471` |
| Zoom anchor math + max-clear | `ThumbnailView.cs:340-390, 606-618` |
| Min/max application points | `ThumbnailConfiguration.cs:121-123, 440-453`; `ThumbnailManager.cs:316-318`; `ThumbnailView.cs:235-239`; `MainForm.cs:480-497` |
| Hover pin/opacity/zoom | `ThumbnailManager.cs:637-674` |
| Activation/minimize/foreground | `WindowManager.cs:200-258`; `ThumbnailManager.cs:676-717` |
| Client caption strip | `ThumbnailManager.cs:886-914` |
| Source enumeration | `ProcessMonitor.cs:72-115` |
| Snap-to-thumbnail | `ThumbnailManager.cs:807-885` |
