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


@pytest.fixture(autouse=True)
def _gc_after_test():
    yield
    # Force any leaked asyncio transport to fire __del__ right here, while
    # the loop that spawned it is freshly closed: attribution lands on the
    # test that leaked it instead of a random later test whose loop simply
    # happened to be current when GC fired (#82). Pairs with the
    # PytestUnraisableExceptionWarning -> error escalation in pytest.ini.
    gc.collect()

# The suite's loop tests exercise tool MECHANICS (bash, file writes) with no
# user present to answer approval prompts; under the product default ("ask")
# the access-mode gate would block them forever. Run the suite with the gate
# wide open; the gate itself is covered in test_access_modes.py, which
# brings its own config per test.
with open(os.environ["YAAH_CONFIG_PATH"], "w", encoding="utf-8") as f:
    json.dump({"access_mode": "full"}, f)
