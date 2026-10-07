---
name: resolving-merge-conflicts
description: "Use when you need to resolve an in-progress git merge/rebase conflict."
---

> **Compatibility note (ADR-0010, #316; amended by ADR-0015).** In
> YAAH, a landing merge happens inside a chat's own worktree, and
> since ADR-0015 it carries no abort escape: a landing conflict
> resolves with the run branch's side winning, every resolved file
> listed in the landing report. This skill's "always resolve; never
> `--abort`" discipline is therefore the landing discipline too —
> keep choosing resolution over abort for any merge or rebase others
> depend on: an integration branch, a shared branch, the primary
> worktree.


1. **See the current state** of the merge/rebase. Check git history, and the conflicting files.

2. **Find the primary sources** for each conflict. Understand deeply why each change was made, and what the original intent was. Read the commit messages, check the PRs, check original issues/tickets.

3. **Resolve each hunk.** Preserve both intents where possible. Where incompatible, pick the one matching the merge's stated goal and note the trade-off. Do **not** invent new behaviour. Always resolve; never `--abort`.

4. Discover the project's **automated checks** and run them, typically typecheck, then tests, then format. Fix anything the merge broke.

5. **Finish the merge/rebase.** Stage everything and commit. If rebasing, continue the rebase process until all commits are rebased.
