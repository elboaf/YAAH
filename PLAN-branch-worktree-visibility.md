# Plan: branch + worktree visibility in the status strip

*From the #48 handoff (session worktree/branch visibility design). Motivation, verified
current state, and the decided design. Status: all open questions settled 2026-02-07
(this session); ready to implement.*

## Why (the failure that motivated this)

Issue #48 (file drag & drop dead in chat) was fixed and merged to master (8ee304a). Its
root cause doubles as this design's motivation: the one-line fix (`dragDropEnabled: false`)
was authored months earlier on `feat/42-workspace-drag-reorder` (commit 4d8fcc0) — that
branch never merged back to master, and the fix silently vanished from every shipped
build. **A turn's work landing on a branch and not making it to master is the exact
failure mode this feature must make visible.**

## Product vision

The user thinks in *project + branches*. Worktrees are plumbing — never a user concept.
"Fix issue #48" → YAAH makes a branch, fixes it; the user never hears about worktrees.
New chat opens on the default branch. When work starts, the working branch is created
silently; the chip switches to it and stays for the session.

## Decided (from the #48 session, unchanged)

1. **Auto merge-back STAYS.** Master continues to auto-merge at turn end. (Consciously
   supersedes the earlier idea that master should only move on explicit user request.)
2. **Chip pair in the status bar:** a sticky *branch chip* (the chat's working branch,
   not mid-run-only) plus a separate *worktree chip* ("changes are NOT in your main
   folder yet").
3. **Chip-drop = merge-back.** Absence of the worktree chip means "your folder has
   everything"; its lifetime is the lifetime of *unmerged work*, not of the worktree
   directory (ADR 0003 keeps the directory alive until session end).
4. The worktree chip is the **honesty layer** that makes auto-merging acceptable: a
   dropped merge-back leaves the chip visibly stuck instead of failing silently.

## Decided (open questions, settled this session)

### Q1 — Worktree chip: "in worktree · N↑", pure indicator

- Label: `in worktree` while the session is bound; the unmerged-commit count is
  appended once commits exist unmerged (`in worktree · 2↑`), reusing the existing
  `↑` arrow language. Pulsing amber dot carried over from the mid-run chip.
- Click does nothing. No merge button, no popover, no diff — merge stays automatic,
  refusals surface via the existing persisted `git_merge_back` pill, next turn retries.
- Count source: extend `git_workspace_info` (`backend/agent/gitinfo.py:127`) with one
  field — unmerged commits on the session branch vs main HEAD (`rev-list --count
  <main>..<agent-branch>`) — flowing through the existing 2-second `git-info` poll.
  No new endpoint.
- Chip appears on `worktree_bound` (first write-capable tool call — read-only chats
  never see it) and drops when the count reaches zero after a merge.

### Q2 — Branch chip is display-only everywhere

- The branch chip shows state only; the checkout dropdown comes out of the strip.
  (This also fixes today's quirk that the dropdown still opens mid-run.)
- Manual checkout moves to conversation: "switch to branch X" — the agent does the git.
- Consequence: no human checkout of the main tree mid-chat, which also removes the
  divergence a manual checkout would set up against session merge-back.

### Q3 — Cleanup transparency: nothing new

- Existing TTL machinery is the design: `release_session` deletes merged/zero-commit
  branches with the worktree; the reaper (`worktrees.py:1257`) salvages-then-prunes
  orphaned worktrees and kept `agent/*` branches after the branch TTL.
- No system log lines, no settings maintenance card. Branches are plumbing, already
  filtered from the UI branch list; disk is the only cost and it is bounded.

### Q4 — Chip truth is git-derived; commits only

- **What holds the chip up: unmerged commits only.** Uncommitted worktree WIP does not
  (ADR 0003 deliberately lets WIP live across turns; it is salvaged at session end).
  Documented caveat: "no chip" means "all commits merged", not literally every file.
- Chip state derives from git (the git-info count), not from ephemeral stream events —
  so a crashed turn, a backend restart, or a second window all show the same reality,
  and the chip resolves itself the moment a merge lands. Restart recovery reads the
  worktree from its path shape (`.yaah/worktrees/<chat-id>`) + branch, no in-memory
  state needed.
- Failed/aborted turn: the `finally` merge-back still attempts; a refusal keeps the
  chip stuck at N↑ (plus the existing red-pill cause) until the next turn's auto-retry
  succeeds or the session ends (teardown salvages; a kept branch goes with the chat).
- `worktree_released` no longer fires unconditionally at turn end: turn-end events
  become outcome-shaped (`worktree_merged` / `worktree_stuck {reason}`); the chip's
  idle truth comes from the poll regardless.

### Q5 — Disclosure wording: folder-first, one sentence per state

- Idle + count: "<N> turn(s) not yet in your project folder — they merge in
  automatically."
- Running: "Working — changes reach your folder when the turn ends."
- Stuck (refused merge): "<N> turn(s) couldn't merge in — <short cause>. The next
  turn retries."
- The word "worktree" appears in the chip label only (it is the machinery's name, the
  one place); tooltips speak in folder consequences. Branch chip tooltip stays
  display-info ("branch <name> — this chat's working branch").

## Verified current state (code, this tree)

- **Store** (`src/store.ts:115`): `AgentBranchInfo { branch, boundAt }` +
  `agentBranchByConv` per conversation, set by `worktree_bound` (`components.tsx:7370`),
  cleared by `worktree_released` (`:7373`). Not persisted — fine once chip truth is
  git-derived (Q4).
- **Branch chip** (`src/components.tsx:5565-5618`, `GitChipCluster`):
  `showAgentBranch = Boolean(agentBranch && streaming)` — mid-run-only today; the chip
  still opens the checkout dropdown mid-run (both change per Q1/Q2).
- **Git readout** (`backend/agent/gitinfo.py:127`, polled every 2 s): main tree only —
  gains the session-worktree unmerged-count field (Q1).
- **Lifecycle** (ADR 0003): session binding, created lazily at the first write-capable
  tool call (`worktree_bound` fires only on fresh binding, `loop.py:1352, 1414`),
  never released mid-session; `worktree_released` (`loop.py:1728`) currently fires at
  the end of every bound turn — semantics change per Q4.
- **Merge-back** (`worktrees.py:869`, `:994`): per-turn merge under the mutex;
  refusals (dirty overlap / conflict / mid-merge) keep the session bound, surface as
  the persisted `git_merge_back` pill, retried next turn; zero-commit turns noop.
- **Session end / reaper** (`release_session` ~`:1050`, `reap_stale` `:1257`): trash
  drop, salvage, worktree removal, branch hygiene by unmerged commits; TTL pruning.
  Stays silent per Q3.
- **Sub-agents** never merge; branch pinned in their final message's first line.
- **Branch picker**: `agent/*` already filtered out of the UI branch list.

## Implementation sketch

1. **Backend — git-info extension**: unmerged-commit count (+ stuck reason, from the
   last refused merge if still unresolved) on the session worktree; derive from disk
   path shape so restarts need no memory. Sub-agent bindings excluded (parent owns it).
2. **Backend — events**: rename/replace unconditional `worktree_released` with
   outcome events (`worktree_merged` when count → 0, `worktree_stuck {reason}` on
   refusal); `worktree_bound` unchanged.
3. **Store**: session-branch info goes sticky (no longer cleared on turn end);
   `worktreePending`/count state seeded from git-info, nudged by events for
   instant paint between 2-second polls.
4. **UI — `GitChipCluster`**: branch chip → display-only (dropdown removed; its
   entry point dies with Q2 — no replacement affordance in the strip); add the
   `in worktree · N↑` chip with the three tooltip states; drop the
   `&& streaming` gate.
5. **Tests**: git-info count on bound/unbound/restarted; event outcomes on
   merge/refusal; chip render per state (bound-no-commits, bound-N↑, stuck, idle);
   dropdown absence.
6. **ADR note**: amend `docs/adr/0003` — chip semantics change (sticky branch chip,
   worktree chip tracks unmerged commits), worktree *lifetime* unchanged.
