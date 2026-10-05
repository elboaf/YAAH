# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently.

## Working tree discipline

Graduated (ADR-0010, #277): the run SOP lives in the harness base
prompt now — every run gets it injected with its chat id filled in:
run worktree at the deterministic path `.scratch/chat-<id>/run` on
branch `run/<title-slug>-<chat-id>` (#312 — named after the work, not
the chat; `run/chat-<id>` when the title is still the generic default),
commit there, land by merging onto the
selected branch inside the chat's own worktree, then clean up. This
repo adds only:

- The primary worktree is the human's; agents have no business there.
- Branch truth is the branch selector: a switch request is a
  `branch_select` call or a chip pick, never a checkout of the primary
  tree, and new branches derive from the chat's selected branch.
- Residue is surfaced, never silent: a left-behind run worktree is
  named in the report and blocks nothing quietly — the user says
  "land it" or "scrap it".
- Design and vocabulary: `docs/adr/0010-per-chat-worktrees.md` and
  `CONTEXT.md`.


## Agent skills

### Issue tracker

Issues and specs live as GitHub Issues on `elboaf/YAAH`, driven with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

See `docs/agents/triage-labels.md`. The five canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. Exactly one state label per issue.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
