# YAAH

An agent harness where multiple chats and sub-agents can work on the
same workspace concurrently.

## Agent skills

### Issue tracker

Issues and specs live as GitHub Issues on `elboaf/YAAH`, driven with the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

See `docs/agents/triage-labels.md`. The five canonical labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. Exactly one state label per issue.

### Testing policy

Run the tests you touched, not the suite: `npm test` runs the entire
backend suite (10+ minutes, ~1100 tests and growing every issue) and
CI re-runs it at the edges (every PR and every release/RC build)
anyway — plain pushes trigger no CI. See `docs/agents/testing.md`.

### Persistent memory

Before acting on any task, check your persistent memory for relevant facts, rules, and past experiences. Use `memory_search` to find relevant entries. Memory contains project-specific rules, user preferences, past mistakes, and workflow lessons that should inform your approach. Do not skip this step — memory is how the agent avoids repeating past mistakes.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
