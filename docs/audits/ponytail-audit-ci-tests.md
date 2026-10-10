# Ponytail audit — CI test pipeline speed

Whole-suite context: 1007 tests / 85 files; CI already has concurrency cancel, 2-shard matrix on "herp" runners, npm/pip caches, parallel frontend/backend jobs. Findings below are verified against measurements taken this session.

## Ranked wins

1. **[shrink]** `backend/tests/test_agent.py` timeout tests (~10s of the file's 18.6s): 6 tests wait out a real `timeout_seconds: 2`. `bash_tools` clamps `timeout = max(1, min(int(timeout_seconds or 60), MAX_BASH_TIMEOUT))` (tools.py ~1129/1164) — the int floor blocks fractional, but 1s still halves the wait (~5s saved). To go lower, change the floor to accept floats and use 0.2s (~9s saved). Tests: `test_powershell_timeout`, `test_bash_timeout_flushes_partial_utf8`, `test_bash_timeout_returns_partial_output`, `test_bash_stream_timeout_kills_tree`, `test_bash_timeout_kills_backgrounded_child`.
2. **[shrink]** `test_mcp_client.py` (8 tests, 30.3s measured) + `test_mcp_http.py` (8, 12.3s): polling loops with fixed `asyncio.sleep(0.25)`. The fixture controls `mcp_fixtures/demo_server.py` — have the server signal a ready event and await it instead of sleeping (~25–35s saved, biggest single win).
3. **[shrink]** `test_sandbox.py` (81 tests, 40.9s): fixed `time.sleep(0.3)` boot waits (~lines 942/955) → poll-with-deadline. Real subprocess cost stays; only the padded waits shrink (~2–5s).
4. **[native]** No intra-shard parallelism: `pytest-xdist -n auto` on the CI shards. Conftest env-redirect is per-process (own temp DB), so it's likely safe; risks are port-binding tests (mcp http) and runner core counts. Potentially ~2–3× on the backend job — verify before wiring.
5. **[yagni]** `get_db()` schema/migrations on every connection: measured **0.6 ms warm** (74.9 ms only on first open). ~1000 tests × 0.6ms ≈ under 1s. **Not worth changing** — skip the "cache applied flag" idea.
6. **[leave]** `conftest._gc_after_test`: deliberate leak attribution (#82). Do not touch.
7. **[leave]** Fixed `timeout=` constants on git spawns (gitutil etc.): failure-only bounds, no wall-time cost.

## Net

Backend job ~95s of measured hot-spot time; wins 1–3 cut ~35–45s directly, xdist (4) is the multiplier. Cheap first move: ready-events in the MCP fixtures + 1s timeout floor in test_agent.py — two small diffs, no new deps.
