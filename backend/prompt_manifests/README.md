# Prompt manifests

Committed render matrix for YAAH's prompt surface. Each JSON file is one
manifest produced by `backend/agent/prompt_manifest.py` — the harness that
drives the REAL prompt-assembly entry points (loop, subagents, auxiliary
producers) across every configuration combination and records ordered
sections, byte sizes, content hashes, tool schemas, sub-agent/auxiliary
prompts, and the fully rendered text.

## Regeneration

    python -m backend.agent.prompt_manifest --all

Renders every combo THIS host can canonically render — on Windows that is
the win-* and kind-* set, i.e. exactly the 140 files committed here. The
other platform's combos are never committed from a flip-simulation; a posix
host would regenerate its own set natively. **Never run `--all` casually**:
it rewrites up to 140 files, and a prompt change without regeneration fails
the drift guard
(`backend/tests/test_prompt_manifest.py::test_committed_manifests_match_regeneration`)
— make prompt edits and `--all` output part of the same PR.

Single renders while iterating:

    python -m backend.agent.prompt_manifest --list
    python -m backend.agent.prompt_manifest --render win-local-compaction --out /tmp/prompt.txt

## Remote-on-one-machine decision

The `*-remote-*` combos render against a REAL
`backend.agent.remote.RemoteSession` built from a fixture handshake info
dict: its `env_line()` only reads the info dict and never connects
(precedent: `test_remote_workspace_target.py`, `test_agent.py`). No
production seam was added for this.

## Combo id grammar

    <win|posix>-<family>[-plan|-noskills|-nomemory|-noshot|-override|-compaction|-sandboxonly]
    kind-subagents-<win|posix>-<skills|noskills>
    kind-auxiliary-prompts

Families: `local` (full chat turn), `remote` (chat turn over a
remote-namespaced workspace), `remote-offline` (the offline note from
`remote_runner._system_prompt`), plus the two `kind-*` renderers (built-in
sub-agent prompts; auxiliary prompts). `noshot` exists only on win (the
screenshot-tool toggle); every flag is a 2^N toggle in canonical order.
