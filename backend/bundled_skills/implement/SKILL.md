---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Run typechecking regularly, and the single test files that cover the code you touched — never the full suite: CI re-runs it at the edges (every PR and every release/RC build), so a local full-suite run is pure double payment. See docs/agents/testing.md.

Once done, use /code-review to review the work.

Commit your work to the current branch.
