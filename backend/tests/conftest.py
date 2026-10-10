"""Isolate tests from the user's real data.

Must run before any backend import: redirects the database and config
to throwaway files so pytest never creates conversations in (or reads
the provider config from) backend/data/.
"""
import json
import gc
import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="yaah-test-")
os.environ["YAAH_DB_PATH"] = os.path.join(_TMP, "agent.db")
os.environ["YAAH_CONFIG_PATH"] = os.path.join(_TMP, "config.json")
os.environ["YAAH_SKILLS_PATH"] = os.path.join(_TMP, "skills")
os.environ["YAAH_MEMORY_PATH"] = os.path.join(_TMP, "memory")
# Skip mDNS LAN advertising: every TestClient(app) runs the app lifespan,
# and Zeroconf registration costs ~10s per boot (verified by profiling).
# No test exercises advertising; YAAH_NO_HOSTING is the supported switch.
os.environ["YAAH_NO_HOSTING"] = "1"


@pytest.fixture(autouse=True)
def _gc_after_test():
    yield
    # Force any leaked asyncio transport to fire __del__ right here, while
    # the loop that spawned it is freshly closed: attribution lands on the
    # test that leaked it instead of a random later test whose loop simply
    # happened to be current when GC fired (#82). Pairs with the
    # PytestUnraisableExceptionWarning -> error escalation in pytest.ini.
    gc.collect()


@pytest.fixture(autouse=True)
async def _join_extraction_tasks():
    """The turn-completion hook (#341) fires memory extraction as a
    DETACHED task; a test that drives a real turn with memory enabled
    ends with that task still pending. pytest-asyncio then closes the
    loop out from under it: the task dies mid-DB-call, its aiosqlite
    connection never closes (holding the shared suite DB's lock - later
    tests fail with 'database is locked') and the connection's
    non-daemon worker thread blocks interpreter exit (pytest 'finishes'
    then hangs for hours). Await or cancel the stragglers HERE, while
    the test's loop is still alive - one sweep covers every turn-driving
    test instead of each harness patching itself."""
    yield
    import asyncio

    from backend.agent import extract as _extract
    from backend.agent import loop as _loop

    pending = [
        t for t in (*_loop._extract_tasks, *_extract._tasks) if not t.done()
    ]
    for task in pending:
        # The scheduler swallows its own exceptions; awaiting is safe.
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            task.cancel()
    _loop._extract_tasks.clear()
    _extract._reset_state()

# The suite's loop tests exercise tool MECHANICS (bash, file writes) with no
# user present to answer approval prompts; under the product default ("ask")
# the access-mode gate would block them forever. Run the suite with the gate
# wide open; the gate itself is covered in test_access_modes.py, which
# brings its own config per test.
with open(os.environ["YAAH_CONFIG_PATH"], "w", encoding="utf-8") as f:
    json.dump({"access_mode": "full"}, f)
