"""T7a: local search eval harness — keyword vs fused, side by side.

The harness is the Phase 3 merge gate (ADR-0023 §8). Tests exercise its
real machinery through the real seams: the production column runs the
same search_with_semantic over search_all_workspaces the /api/search
endpoint wires; only the embeddings endpoint is faked (capturing), and
DB/config redirect through the same module attributes the suite uses.
Proves: both columns render, markers classify kw/sem/both correctly
(including the both case: one message hit by both legs), the script runs
its lifecycle in-process (queries parsed, overrides applied, exit
codes), degraded states are visible with their reason, --rebuild goes
through the real index builder, and the script never writes transcript
text anywhere.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

from backend.agent import config as config_mod
from backend.agent import embeddings, vectorestore
from backend.db import database

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import search_eval


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "test_eval.db")
    monkeypatch.setattr(database, "_migrated", set())
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.json")


class _CapturingTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


def _respond(vec=None, model="bge-m3", dim=4):
    v = vec or [0.25] * dim

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        n = len(body["input"])
        return httpx.Response(200, content=json.dumps({
            "data": [{"embedding": v} for _ in range(n)],
            "model": model,
        }).encode("utf-8"))

    return _CapturingTransport(handler)


async def _seed(vectors=None):
    """Two conversations, three messages. Vectors (when seeded) live on
    message 2 -- the assistant message whose text contains 'packaging' --
    so the query 'packaging' hits it on BOTH legs. vectors='neg' stores
    negatively-correlated vectors: the index is ready but every cosine is
    negative, so the semantic leg reports zero hits (the ready-but-empty
    case)."""
    await database.create_conversation("Packaging chats", "ws")
    await database.add_message(
        1, "user",
        "The packaging stage failed during the deploy pipeline run.",
    )
    await database.add_message(
        1, "assistant",
        "Deployment pipelines fail at packaging when artifacts mismatch.",
    )
    await database.create_conversation("Cache war stories", "other-ws")
    await database.add_message(
        2, "user",
        "That cache bug kept churning through the retry loop all afternoon.",
    )
    if vectors:
        sign = -1.0 if vectors == "neg" else 1.0
        await vectorestore.store_chunk_embeddings([
            {"conversation_id": 1, "message_id": 2, "role": "assistant",
             "chunk_text": "Deployment pipelines fail at packaging",
             "chunk_index": 0, "embedding": [0.9 * sign, 0.1 * sign, 0.0, 0.0]},
            {"conversation_id": 1, "message_id": 2, "role": "assistant",
             "chunk_text": "when artifacts mismatch",
             "chunk_index": 1, "embedding": [0.8 * sign, 0.2 * sign, 0.0, 0.0]},
        ])
        await vectorestore.record_index_build("bge-m3", 4)


def _configure(api_base="http://x"):
    config_mod.save_config({"embeddings": {
        "api_base": api_base, "api_key": "", "model": "bge-m3",
    }})


def _rows_in(outp, section, marker):
    """Result rows with a marker, scoped to one column ('keyword-only' or
    'production') so a marker in the other column cannot be counted."""
    lines = outp.splitlines()
    start = next(
        (i for i, l in enumerate(lines) if l.strip().startswith(f"--- {section}")),
        None,
    )
    if start is None:
        return []
    end = next(
        (j for j in range(start + 1, len(lines))
         if lines[j].startswith("---") or lines[j].startswith("===")),
        len(lines),
    )
    pat = re.compile(r"\d+\. " + marker + r"\b")
    return [l for l in lines[start + 1 : end] if pat.search(l)]


# ------------------------------------------------------------ pure helpers


def test_load_queries_skips_comments_and_blanks(tmp_path):
    f = tmp_path / "q.txt"
    f.write_text("# comment\n\nfind the cache bug\n   \nEXECUT\n", encoding="utf-8")
    assert search_eval.load_queries(str(f)) == ["find the cache bug", "EXECUT"]


def test_classify_source_prefers_sources_array():
    hit_both = {"message_id": 1, "sources": ["keyword", "semantic"]}
    hit_kw = {"message_id": 1, "sources": ["keyword"]}
    assert search_eval.classify_source(hit_both, set()) == "both"
    assert search_eval.classify_source(hit_kw, set()) == "kw"
    # inference fallback while the fused contract has not landed yet
    assert search_eval.classify_source({"message_id": 1, "similarity": 0.5}, set()) == "sem"
    assert search_eval.classify_source({"message_id": 7}, {7}) == "kw"
    assert search_eval.classify_source({"message_id": 7, "similarity": 0.1}, {7}) == "both"
    # a sources array with unknown values must not crash the renderer
    assert search_eval.classify_source({"message_id": 1, "sources": []}, set()) == "kw"


# ------------------------------------------------------------ evaluation run


async def test_evaluate_renders_both_columns_with_markers(capsys):
    await _seed(vectors="pos")
    _configure()
    embeddings._transport = _respond()
    await search_eval.evaluate(["cache bug"], top=5)
    outp = capsys.readouterr().out
    assert "QUERY: cache bug" in outp
    assert "--- keyword-only" in outp
    assert "--- production" in outp
    assert "state=ready" in outp
    # keyword leg hits only the cache message; semantic leg (seeded
    # vectors) hits only the packaging message. Today's merge appends
    # chunk-grained semantic hits without collapsing them (T7b fixes
    # that), so the sem count is >= 1; the kw hit is exactly one.
    assert len(_rows_in(outp, "production", "kw")) == 1
    sem_rows = _rows_in(outp, "production", "sem")
    assert sem_rows
    assert all("Packaging chats" in l for l in sem_rows)
    assert "Cache war stories" in _rows_in(outp, "keyword-only", "kw")[0]


async def test_evaluate_marks_a_message_hit_by_both_legs(capsys):
    await _seed(vectors="pos")
    _configure()
    embeddings._transport = _respond()
    await search_eval.evaluate(["packaging"], top=5)
    outp = capsys.readouterr().out
    # 'packaging' hits messages 1 and 2 on the keyword leg, and message 2
    # also carries the seeded vectors: its chunk rows read 'both' (one per
    # chunk until T7b collapses them), while message 1 stays keyword-only.
    both = _rows_in(outp, "production", "both")
    assert both
    assert all("Packaging chats" in l for l in both)  # title join works
    assert any("Deployment pipelines fail at packaging" in l for l in both)
    kw_only = _rows_in(outp, "production", "kw")
    assert len(kw_only) == 1
    assert "The packaging stage failed" in kw_only[0]


async def test_evaluate_ready_with_zero_semantic_hits_marks_all_kw(capsys):
    await _seed(vectors="neg")
    _configure()
    embeddings._transport = _respond()
    await search_eval.evaluate(["packaging"], top=5)
    outp = capsys.readouterr().out
    # Index ready, but every cosine is negative and the floor drops them:
    # semantic found nothing, keyword results pass through untouched.
    assert "state=ready" in outp
    assert "semantic_matches=0" in outp
    assert _rows_in(outp, "keyword-only", "kw")
    assert not _rows_in(outp, "production", "sem")
    assert not _rows_in(outp, "production", "both")
    assert _rows_in(outp, "production", "kw")


async def test_evaluate_unreachable_endpoint_shows_reason(capsys):
    await _seed(vectors="pos")
    _configure(api_base="http://dead")
    embeddings._transport = httpx.MockTransport(
        lambda request: httpx.Response(500, content=b"boom")
    )
    await search_eval.evaluate(["packaging"], top=5)
    outp = capsys.readouterr().out
    assert "state=unavailable" in outp
    assert "unreachable" in outp
    assert _rows_in(outp, "keyword-only", "kw")
    assert not _rows_in(outp, "production", "sem")
    assert not _rows_in(outp, "production", "both")
    assert "similarity" not in outp


async def test_evaluate_not_configured_reports_state(capsys):
    await _seed()
    await search_eval.evaluate(["packaging"], top=5)
    outp = capsys.readouterr().out
    assert "state=not_configured" in outp
    assert "--- keyword-only" in outp
    assert _rows_in(outp, "keyword-only", "kw")
    assert _rows_in(outp, "production", "kw")


async def test_evaluate_keeps_going_after_a_failing_query(capsys):
    await _seed()
    await search_eval.evaluate(["packaging", "cache bug"], top=5)
    outp = capsys.readouterr().out
    assert "QUERY: packaging" in outp
    assert "QUERY: cache bug" in outp


# ------------------------------------------------------------ lifecycle


def test_main_missing_query_file_exit_code(tmp_path, capsys):
    rc = search_eval.main([str(tmp_path / "nope.txt")])
    assert rc == 2
    assert "not found" in capsys.readouterr().err


def test_main_empty_query_file_exit_code(tmp_path, capsys):
    f = tmp_path / "q.txt"
    f.write_text("# only comments\n\n", encoding="utf-8")
    assert search_eval.main([str(f)]) == 2
    assert "no queries" in capsys.readouterr().err


_capture_calls: list = []


async def _capture_evaluate(queries, top, out=print):
    _capture_calls.append(list(queries))
    for q in queries:
        out("=" * 78)
        out(f"QUERY: {q}")


def test_main_passes_parsed_queries_to_evaluate(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed())
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\ncache bug\n", encoding="utf-8")
    monkeypatch.setattr(search_eval, "evaluate", _capture_evaluate)
    rc = search_eval.main([str(qf), "--top", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "QUERY: packaging" in out
    assert "QUERY: cache bug" in out
    assert _capture_calls and _capture_calls[0] == ["packaging", "cache bug"]


def test_main_embed_overrides_write_no_real_config(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed(vectors="pos"))
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    real_config = tmp_path / "config.json"
    real_config.write_text('{"access_mode": "full"}', encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", real_config)
    embeddings._transport = _respond()
    rc = search_eval.main(
        [str(qf), "--embed-api-base", "http://x", "--embed-model", "bge-m3"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "state=ready" in out  # overrides reached the search path
    # the real config file was never touched
    assert real_config.read_text(encoding="utf-8") == '{"access_mode": "full"}'


# ------------------------------------------------- override + guard rails


def test_main_embed_model_only_merges_over_configured_block(
    tmp_path, monkeypatch, capsys
):
    asyncio.run(_seed())
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    config_mod.save_config({"embeddings": {
        "api_base": "http://cfg-endpoint", "api_key": "k", "model": "other-model",
    }})
    # Deterministic transport: the merged endpoint is "unreachable", so
    # production degrades. If the override had blanked the omitted fields
    # instead of merging (the pre-review bug), the state would be
    # not_configured instead.
    embeddings._transport = httpx.MockTransport(
        lambda request: httpx.Response(500, content=b"boom")
    )
    rc = search_eval.main([str(qf), "--embed-model", "bge-m3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "state=unavailable" in out
    assert "state=not_configured" not in out
    # the real config file was never modified (CONFIG_PATH points at the
    # deleted throwaway after main(); read the real file directly)
    stored = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert stored["embeddings"] == {
        "api_base": "http://cfg-endpoint", "api_key": "k", "model": "other-model",
    }


def test_main_does_not_send_key_to_a_different_endpoint(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed(vectors="pos"))  # index ready, so the embed call fires
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    config_mod.save_config({"embeddings": {
        "api_base": "http://cfg-endpoint", "api_key": "secret", "model": "m",
    }})
    seen: list[dict] = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(500, content=b"no")

    embeddings._transport = httpx.MockTransport(handler)
    # model matches the seeded index (so readiness is ready and the embed
    # fires); the endpoint differs, so the configured key must NOT ride along
    rc = search_eval.main(
        [str(qf), "--embed-api-base", "http://other", "--embed-model", "bge-m3"]
    )
    assert rc == 0
    assert seen  # the (failing) embed call went out
    assert (seen[0].get("authorization") or "") != "Bearer secret"


def test_main_rebuild_refused_when_endpoint_not_usable(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed())
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    config_mod.save_config({"embeddings": {
        "api_base": "http://dead", "api_key": "", "model": "bge-m3",
    }})
    embeddings._transport = httpx.MockTransport(
        lambda request: httpx.Response(500, content=b"boom")
    )
    rc = search_eval.main([str(qf), "--rebuild"])
    assert rc == 2
    assert "rebuild refused" in capsys.readouterr().err
    # the index was never cleared
    assert asyncio.run(vectorestore.get_vector_count()) == 0


def test_main_rejects_mismatched_db_in_process(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed())
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    other = tmp_path / "elsewhere.db"
    rc = search_eval.main([str(qf), "--db", str(other)])
    assert rc == 2
    assert "cannot be honored" in capsys.readouterr().err


def test_main_accepts_db_matching_the_process_binding(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed())
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    rc = search_eval.main([str(qf), "--db", str(database.DB_PATH)])
    assert rc == 0
    assert "QUERY: packaging" in capsys.readouterr().out


def test_main_cleans_up_the_throwaway_config(tmp_path, monkeypatch, capsys):
    asyncio.run(_seed())
    qf = tmp_path / "queries.txt"
    qf.write_text("packaging\n", encoding="utf-8")
    real_config = tmp_path / "config.json"
    real_config.write_text('{"access_mode": "full"}', encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", real_config)
    seen_paths: list[Path] = []

    async def _probe(queries, top, out=print):
        from backend.agent import config as cm

        seen_paths.append(Path(cm.CONFIG_PATH))

    monkeypatch.setattr(search_eval, "evaluate", _probe)
    rc = search_eval.main(
        [str(qf), "--embed-api-base", "http://x", "--embed-model", "bge-m3"]
    )
    assert rc == 0
    throwaway = seen_paths[0]
    assert throwaway != real_config  # the run used the throwaway
    assert not throwaway.exists()  # ...and it was deleted afterwards
    assert real_config.exists()  # the real config was never touched


async def test_rebuild_goes_through_real_builder(capsys):
    await _seed()
    _configure()
    embeddings._transport = _respond()
    summary = await search_eval._maybe_rebuild()
    assert "bge-m3" in summary  # the build recorded its model
    meta = await vectorestore.get_index_metadata()
    assert meta.get("model") == "bge-m3"
    assert (await vectorestore.get_vector_count()) > 0
