# Fossil probe: one-command index provenance for the primary

When a primary tree's staged index looks suspicious - after a tangled
landing, an interrupted session, or a "did anything survive?" moment -
do NOT hand-run git plumbing against it. One command answers whether
the staged content is live WIP or a landing fossil (#353's
nine-handoff diagnosis class; ADR-0016 section 5):

```
python -m backend.agent.landing <primary-workspace-path>
```

(``<primary-workspace-path>`` is the human's checkout, e.g.
``C:\Users\<you>\<repo>``; from the backend repo root you may omit the
argument to probe the current directory.) The output is a JSON
verdict, safe to read and to paste into an issue:

- **`"verdict": "clean"`** - the index tree equals the HEAD tree:
  nothing is staged beyond HEAD. Nothing to rescue.
- **`"verdict": "fossil-candidate"`** - every differing blob is
  already committed in some local branch or tag (evidence lines name
  the containing refs, e.g. `hello.txt in refs/heads/wip/chat-704`).
  The staged index is a leftover; a landing or a `reset --hard` loses
  nothing (the content lives on the named branches - verify there
  before discarding anything).
- **`"verdict": "live-wip"`** - at least one differing blob exists in
  NO ref (evidence lines name the unreachable blobs). That content
  exists only because someone typed it into the index. **Do NOT reset
  it away** - save it first (commit it to a `wip/` branch, or ask the
  human).
- **`"verdict": "error"`** - git could not answer (not a repo, an
  unmerged index, a failed command). The evidence field carries why.
  An error is data, not absence - never treat it as `clean`.

Rules of thumb:

- The probe is read-only: it hashes the index (`write-tree`) without
  touching it. It changes no staged state.
- `fossil-candidate` does not authorize you to reset the primary - it
  tells you a rescue is not urgent because the content is committed.
  Landings still go through the verified landing module.
- If you need to land something after a probe, use the harness landing
  flow (or the `land` tool once #356 wires it); never hand-run
  `update-ref`/`reset` against the primary.
- The verdict is a plain JSON dict: paste it verbatim into an issue or
  a chat when escalating to a human.
