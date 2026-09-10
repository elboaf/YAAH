"""Isolate tests from the user's real data.

Must run before any backend import: redirects the database and config
to throwaway files so pytest never creates conversations in (or reads
the provider config from) backend/data/.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="yaah-test-")
os.environ["YAAH_DB_PATH"] = os.path.join(_TMP, "agent.db")
os.environ["YAAH_CONFIG_PATH"] = os.path.join(_TMP, "config.json")
