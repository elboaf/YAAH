# Agent testing policy

How agents verify changes in this repo without paying the full-suite
tax, and where CI re-verifies so agents don't have to.

## The problem this solves

`npm test` runs the entire backend suite: ~1100 tests and growing every
issue (`test_issue_NNN_*.py` naming convention). In CI the suite runs
split across two shards, ~8–10 minutes per shard; locally, unsharded,
it is slower. The full suite is a **gate at the edges**, and the edges
are exactly two: PR review and release. Running everything twice per
change — full suite locally, full suite again at the next edge — is
the double payment this policy removes. The goal: **faster time to
push** for agents, with CI as the safety net.

## Where the full suite runs (and nowhere else)

1. **PR edge** — `.github/workflows/ci.yml` runs the full backend suite
   (two shards) plus frontend typecheck and build on every push to an
   open PR, against the merge result. Consecutive pushes cancel the
   in-flight run, so a burst of pushes pays once: only the last push's
   run survives.
2. **Release edge** — `.github/workflows/release.yml` runs the same
   suite against the release/RC tag before any installer is built or
   published, covering both the RC channel and stable releases.
   Frontend build coverage comes free there: `tauri build` compiles
   the frontend.

Plain pushes trigger no CI at all: branches without an open PR, and
commits landing on master (a merge-commit master push is the exact
tree the PR edge already tested). **Your push buys no CI.** Between
your edits and the next edge, the only verification that exists is
your targeted local run — which is why the rules below are rules and
not suggestions.

## Policy

1. **Run the tests you touched, not the suite.** After a code change,
   run the specific test files that exercise the code you changed —
   the new `test_issue_NNN_*.py` you added, plus any pre-existing
   files that cover the module under edit. Seconds, not minutes.
   Skipping the full suite before push is expected and correct.

2. **The PR is your safety net.** The PR edge catches the cross-file
   breakage a targeted run can miss. If it goes red, the failure
   annotations carry the failed test names; rerun just those files
   locally to fix. Red CI on your PR is a normal event and a return
   trip, not a catastrophe — never pre-pay the suite to avoid it.

3. **Frontend tests never run in CI.** CI's frontend job only
   typechecks and builds; the ~80 vitest files under `src/` run only
   when someone runs them locally. If you touch code covered by a
   vitest file, run that file (`npx vitest run src/foo.test.tsx`).
   CI will still build-check it, but tests do not gate frontend in CI.

# Targeted-run commands

Backend, single file:

    python -m pytest backend/tests/test_issue_296_say_fire.py -q

Backend, the files your change plausibly affects:

    python -m pytest backend/tests/test_a.py backend/tests/test_b.py -q

Frontend, single file:

    npx vitest run src/deltaBuffer.test.ts

Frontend, whole suite (deliberate full check only):

    npm run test:frontend

## When the full suite IS the job

One situation justifies a full `python -m pytest backend/tests -q`:

- **Shared test-infrastructure changes**: anything under
  `backend/tests/conftest.py`, `backend/tests/gitutil.py`,
  `support_say.py`, `mcp_fixtures/`, or pytest.ini. Conftest changes
  affect every test; only a full run verifies that.

A change so broad it defies file-level targeting is rarer than it
feels — do not reach for that excuse. If you do run the full suite
under this exception, say so in the PR body: a *declared* full-suite
run is the exception working as intended, a *silent* one is a policy
miss.

Chunking the suite (per directory, a few directories at a time) is
not a middle ground — it is the same tax paid slower.

## When you can skip local tests entirely

- **Docs, comments, ADRs, GLOSSARY.md** — nothing executable changed.
- **CI/workflow files** (`.github/workflows/*`): CI itself is the
  verification; watch the PR's checks instead of running something
  local.
- **Version bumps and lockfile syncs** (`package-lock.json`,
  `requirements.txt` pin moves): the release edge verifies the install
  and build matrix.

## Anti-patterns

- Running `npm test` (full backend suite) as a matter of course
  after every edit. That is the tax this policy removes.
- Running the full suite "just to be safe" before every push. The
  PR edge runs it minutes later; the safety net is real.
- Running the full suite because CI is slow or queued. The queue
  clears; your redundant run doesn't.
- Adding a new `test_issue_NNN_*.py` and running the entire suite to
  see the new file pass — run the file.
- Retrying the full suite after an unrelated failure. Rerun the
  failed subset only.
- Waiting for CI green before opening the PR or pushing the next
  slice. Push when your targeted runs pass.
