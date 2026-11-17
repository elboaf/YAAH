---
name: interceptor-file-attach
description: Attach a local file to a web page (upload modals, chat compose boxes, web forms) through the Interceptor CLI with no upload verb and no native file dialog. Covers the CSP-stripped MAIN-world eval, chunked base64 ferry, and DataTransfer attach, plus the PowerShell 5.1 argv trap.
---

# Attaching a local file to a web page via Interceptor

The Interceptor CLI has NO `upload` verb. Native file-picker dialogs are Windows-side and out of
reach for a browser-only install. But almost every upload surface (chat compose boxes, web forms)
is driven by a hidden `<input type="file">` in the page's own DOM — and you can drive that from
inside the page. Proven end-to-end against Google Chat (attach + send a 234 KB image that the
recipient actually received).

## The recipe

### 0. Preconditions
- `interceptor status` shows the extension reachable; you have the target `--tab <id>`.
- The tab is OUTSIDE Interceptor's managed group → every command needs `--any-tab` (explicit
  user authorization required — get it before touching foreign tabs).
- If you have NOT yet evaluated anything in this tab: the FIRST `eval --main` on a strict-CSP
  page (Google properties) makes Interceptor **reload the tab with CSP stripped**. Warn the user
  (in-page state is discarded) and don't fight it — the CSP-stripped reload is what unlocks eval.

### 1. Recon the file input
```
interceptor eval --main "JSON.stringify([...document.querySelectorAll('input[type=file]')].map((el,i)=>({i,accept:el.accept,multiple:el.multiple})))" --tab <id> --any-tab
```
No input? Some apps construct it lazily on paperclip-click — `act` the attach button first, then re-run.

### 2. Prepare the payload (host side)
- PowerShell GDI+ re-encode works fine for images: PNG → JPEG q90 took 2.5 MB → 234 KB.
- Base64 it: `[Convert]::ToBase64String([IO.File]::ReadAllBytes($path)) | Set-Content payload.b64`
- Chunk it mentally at ~28,000 chars per command line (argv-safe).

### 3. Ferry the base64 into the page — **use Git Bash, NOT PowerShell 5.1**
PowerShell 5.1 mangles large double-quoted native args (embedded `""` escaping eats the string and
swallows the trailing `--tab/--any-tab` flags — the CLI then rejects with a confusing
"group gate" error). Bash quotes argv honestly. 12/12 chunks landed via bash after 0/12 via PS.
```
EXE="$LOCALAPPDATA/Programs/Interceptor/interceptor.exe"   # Git Bash expands %LOCALAPPDATA% as $LOCALAPPDATA; resolve to /c/Users/<you>/... if unset
B64=$(cat payload.b64); CHUNK=28000; N=$(( (${#B64} + CHUNK - 1) / CHUNK ))
"$EXE" eval --main "window.__upl='';'init'" --tab <id> --any-tab 2>/dev/null
for ((i=0;i<N;i++)); do
  c=${B64:i*CHUNK:CHUNK}
  "$EXE" eval --main "window.__upl=(window.__upl||'')+'$c';window.__upl.length" --tab <id> --any-tab 2>/dev/null
done
```
Verify the final reported length equals your base64 length before proceeding.

### 4. Assemble + attach via DataTransfer
```
"$EXE" eval --main "(function(){try{var bin=atob(window.__upl);var a=new Uint8Array(bin.length);for(var i=0;i<bin.length;i++)a[i]=bin.charCodeAt(i);var f=new File([a],\"name.jpg\",{type:\"image/jpeg\"});var inp=document.querySelector('input[type=file]');var dt=new DataTransfer();dt.items.add(f);inp.files=dt.files;inp.dispatchEvent(new Event('input',{bubbles:true}));inp.dispatchEvent(new Event('change',{bubbles:true}));return 'OK '+f.size;}catch(e){return 'ERR '+e.message;}})()" --tab <id> --any-tab 2>/dev/null
```
**Note:** on success the app's handler consumes the input immediately, so a post-hoc
`input.files[0]` readback can return undefined ("cannot read properties of undefined") — that
error usually means SUCCESS. Confirm visually (screenshot) or by the UI showing the attachment.

### 5. Compose + send
- `interceptor read --tree-format compact` → refs for the compose textbox and Send button.
- `interceptor type <ref> "<message>" ...` then `interceptor act <send-ref> ...`.
- Verify with `read --text-only` (look for "Message sent") — tool stderr banners are noise;
  exit codes lie on Windows; the page's own text is the only honest witness.

## Gotchas ledger (all empirically verified)
1. **PS 5.1 argv mangling** (step 3) — the #1 way this fails. Symptom: group-gate errors that
   aren't group-gate problems.
2. **`window.scrollTo` does nothing on inner-scroll apps** (Google Chat). Scroll the inner
   containers: `[...document.querySelectorAll('div')].filter(e=>e.scrollHeight>e.clientHeight+200).forEach(e=>e.scrollTop=e.scrollHeight)`.
3. **Post-click diffs can read "0 changes"** mid-rerender (SPA); verify via URL/DOM/text, not the diff.
4. **Screenshots: use `--format png`** — the local image viewer fails to sniff webp.
5. **Stale refs after navigation** — re-`read` for fresh refs; a stale-ref error means nothing was clicked.
6. **`eval` without `--main` needs "Allow User Scripts"** (off by default); `eval --main` works
   everywhere after the CSP-strip reload.
7. **Exit codes on Windows are unreliable** (stderr progress banners → PowerShell NativeCommandError);
   judge by parsed output, never by `$?`.
