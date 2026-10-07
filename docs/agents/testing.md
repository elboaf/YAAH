# Agent testing policy

How agents verify changes in this repo without paying the full-suite
tax on every edit, and what CI re-verifies so agents don't have to.

## The problem this solves

`npm test` runs the entire backend suite: 101 files, ~1100 tests.
In CI the suite runs split across two shards and each shard's test
step alone takes 8–10 minutes; locally, unsharded, it is slower. It
also grows by roughly one permanent file per fixed issue
(`test_issue_NNN_*.py` naming convention). CI re-runs
the identical suite on every push and PR anyway, split across two
shards on the self-hosted runners. Running everything twice per
change — full suite locally, full suite again on GitHub — is the
double payment this policy removes. The goal: **faster time to push**
for agents, with CI as the safety net.

## Policy

1. **Run the tests you touched, not the suite.** After a code change,
   run the specific test files that exercise the code you changed —
   the new `test_issue_NNN_*.py` you added, plus any pre-existing
   files that cover the module under edit. Seconds, not minutes.
   Skipping the full suite before push is expected and correct.

2. **CI is the safety net.** Every push and PR triggers `.github/workflows/ci.yml`:
   the full backend suite (two shards) plus frontend typecheck and
   build. Let it catch the cross-file breakage that a targeted local
   run can miss. If it goes red, the failure annotations carry the
   failed test names; rerun just those files locally to fix.

3. **Frontend tests never run in CI.** The `Frontend build` job only
   typechecks and builds; the 73 vitest files under `src/` run only
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
feels — do not reach for that excuse. CI runs the full suite minutes
after your push either way.

## When you can skip local tests entirely

- **Docs, comments, ADRs, GLOSSARY.md** — nothing executable changed.
- **CI/workflow files** (`.github/workflows/*`): CI itself is the
  verification; watch the run instead of running something local.
- **Version bumps and lockfile syncs** (`package-lock.json`,
  `requirements.txt` pin moves): CI verifies the install and build
  matrix.

## Anti-patterns

- Running `npm test` (full backend suite) as a matter of course
  after every edit. That is the tax this policy removes.
- Running the full suite "just to be safe" before every push. CI
  runs it again on the push anyway; the safety net is real.
- Adding a new `test_issue_NNN_*.py` and running the entire suite to
  see the new file pass — run the file.
- Retrying the full suite after an unrelated failure. Rerun the
  failed subset only.
