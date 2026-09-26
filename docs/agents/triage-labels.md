# Triage labels

Labels used to track triage state on issues. Apply exactly one state label per issue; never mix labels across states.

## Labels

- `needs-triage`: Default label for every newly-created issue. Nothing else is known yet — it has not been assessed for clarity, actionability, or ownership.
- `needs-info`: The issue cannot be triaged until the reporter supplies more information. When applying, always comment listing exactly what's missing (repro steps, environment, expected vs actual behavior).
- `ready-for-agent`: The issue is fully specified and safe for a coding agent to pick up: clear scope, acceptance criteria, and no blocking unknowns. This is the signal that a session may claim and implement it.
- `ready-for-human`: The issue is fully specified but must be done by a human — it needs product judgement, credentials, external access, or a decision an agent is not authorized to make.
- `wontfix`: Triage concluded the issue will not be addressed (duplicate, out of scope, by-design). Close with a comment explaining why.

## Lifecycle

1. New issues start at `needs-triage`.
2. If information is missing → `needs-info`; return to `needs-triage` when the reporter responds.
3. Once specified → `ready-for-agent` or `ready-for-human`, depending on who can do it.
4. Rejected issues → `wontfix` + close.
5. `ready-for-agent` work in progress may also be claimed by a session (assignee acts as the claim marker).