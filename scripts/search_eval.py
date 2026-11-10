"""Local search evaluation harness (issue #724, Phase 3 / ADR-0023 section 8).

Runs a personal query list against the live local transcript store and
prints keyword-only vs production search results side by side, with a
per-hit source marker, so a human can judge whether fusion improved
concept queries without regressing identifier queries.

The query file is personal and stays UNTRACKED: one query per line,
blank lines and # comments ignored. Queries only -- the script never
writes transcript text anywhere (it reads the store, prints excerpts).

The production column goes through the same seam as the UI endpoint
(search_with_semantic over search_all_workspaces), so it shows today's
merge until T7b lands and fused ranking after -- this script is the
required merge gate for that PR.

Usage:
  python scripts/search_eval.py queries.txt [--top N] [--db PATH]
      [--config PATH] [--embed-api-base URL --embed-model NAME [--embed-key KEY]]
      [--rebuild]

  --db               Alternate store (default: this install's YAAH_DB_PATH /
                     backend/data/agent.db).
  --config           Alternate config file (default: this install's config).
  --embed-*          Override the embeddings block without touching any real
                     config file (a throwaway config is generated). Useful in
                     a separate process, where the bundled local model server
                     is invisible.
  --rebuild          Rebuild the vector index first (user-triggered rebuild,
                     same code path as the Settings button; needs a working
                     embeddings endpoint).
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Depth fetched from each leg (the DB layer clamps to 50). Display depth
# is --top; fetching deep shows more of each ranking than the UI default.
FETCH_DEPTH = 50


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("queries", help="path to a plain query file (one query per line, untracked)")
    ap.add_argument("--top", type=int, default=5, help="results shown per column (default 5)")
    ap.add_argument("--db", default=None, help="alternate transcript store path")
    ap.add_argument("--config", default=None, help="alternate config.json path")
    ap.add_argument("--embed-api-base", default=None, help="override embeddings endpoint base")
    ap.add_argument("--embed-model", default=None, help="override embeddings model id")
    ap.add_argument("--embed-key", default="", help="override embeddings API key")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the vector index first")
    return ap.parse_args(argv)


def load_queries(path: str) -> list[str]:
    """One query per line; blank lines and # comments skipped."""
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def classify_source(hit: dict, keyword_ids: set) -> str:
    """kw / sem / both for a production hit. Prefers the `sources` array
    (the fused contract T7b will add); falls back to inference: a
    similarity score means the semantic leg produced it, membership in
    the keyword result set means the keyword leg did."""
    sources = hit.get("sources")
    if isinstance(sources, list) and sources:
        has_kw = "keyword" in sources
        has_sem = "semantic" in sources
        return "both" if has_kw and has_sem else ("kw" if has_kw else "sem")
    sem = "similarity" in hit
    kw = hit.get("message_id") in keyword_ids
    if sem and kw:
        return "both"
    return "sem" if sem else "kw"


def _one_line(text: str, width: int) -> str:
    return " ".join((text or "").split())[:width]


async def render_query_block(query: str, kw: dict, prod: dict, top: int) -> str:
    """The side-by-side pair for one query: two stacked ranked lists with a
    shared rank/source/kind gutter, then the production state line."""
    lines = ["=" * 78, f"QUERY: {query}"]

    kw_matches = (kw.get("matches") or [])[:top]
    prod_matches = (prod.get("matches") or [])[:top]
    kw_ids = {m.get("message_id") for m in kw_matches}

    # Titles: semantic-origin hits carry no conversation_title (the vector
    # store does not join titles), so the harness joins them itself --
    # read-only: from the keyword hits first, then one lookup for the rest.
    titles = {
        m.get("conversation_id"): m.get("conversation_title")
        for m in kw_matches
        if m.get("conversation_id") is not None
    }
    missing = {
        m.get("conversation_id")
        for m in prod_matches
        if m.get("conversation_id") is not None
        and m.get("conversation_id") not in titles
    }
    if missing:
        titles.update(await _conversation_titles(sorted(missing)))

    lines.append(
        f"--- keyword-only  (count={kw.get('count')}, truncated={kw.get('truncated')}) ---"
    )
    for i, m in enumerate(kw_matches, 1):
        lines.append(_row(i, "kw", m, titles))

    state = prod.get("semantic_state", "?")
    extra = ""
    if state == "ready":
        extra = f", semantic_matches={prod.get('semantic_matches')}"
    elif prod.get("semantic_unavailable"):
        extra = f" ({prod['semantic_unavailable']})"
    lines.append(
        f"--- production    (count={prod.get('count')}, state={state}{extra}) ---"
    )
    for i, m in enumerate(prod_matches, 1):
        lines.append(_row(i, classify_source(m, kw_ids), m, titles))

    if not kw_matches and not prod_matches:
        lines.append("  (no results on either leg)")
    return "\n".join(lines)


def _row(rank: int, marker: str, hit: dict, titles: dict) -> str:
    title = hit.get("conversation_title") or titles.get(hit.get("conversation_id")) or ""
    kind = hit.get("kind", "?")
    excerpt = _one_line(hit.get("excerpt") or "", 96)
    return f" {rank:>2}. {marker:<4} {kind:<9} {_one_line(title, 32)} -- {excerpt}"


async def _maybe_rebuild() -> str:
    from backend.agent import indexbuilder

    summary = await indexbuilder.build_index()
    return json.dumps(summary, default=str)


async def evaluate(queries: list[str], top: int, out=print) -> None:
    """Run every query through both legs and render the comparison.

    Backend modules are imported here (not at file top) so a caller --
    including the test suite -- can redirect DB_PATH / CONFIG_PATH and
    fake the embeddings transport first."""
    from backend.agent import embeddings, vectorestore
    from backend.agent.search_semantic import search_with_semantic
    from backend.db.database import search_all_workspaces

    emb_cfg = embeddings._embeddings_config()
    meta = await vectorestore.get_index_metadata()
    vectors = await vectorestore.get_vector_count()
    out(
        f"# embeddings: model={emb_cfg['model'] or '-'} base={emb_cfg['api_base'] or '-'}"
        f" | index: model={(meta or {}).get('model') or '-'}"
        f" dims={(meta or {}).get('dimensions') or '-'}"
        f" vectors={vectors}"
    )
    if meta and emb_cfg["model"] and meta.get("model") != emb_cfg["model"]:
        out("# WARNING: configured model differs from the index model -- production")
        out("#          column will degrade (mismatch). Rebuild with --rebuild.")

    async def keyword_fn(q: str, n: int) -> dict:
        return await search_all_workspaces(q, n)

    for q in queries:
        try:
            kw = await search_all_workspaces(q, FETCH_DEPTH)
            if kw.get("error"):
                out(f"!! keyword search refused query {q!r}: {kw['error']}")
                continue
            prod = await search_with_semantic(keyword_fn, q, FETCH_DEPTH)
            out(await render_query_block(q, kw, prod, top))
        except Exception as e:  # keep evaluating the remaining queries
            out(f"!! query {q!r} failed: {e}")
    out("=" * 78)


async def _conversation_titles(conversation_ids: list[int]) -> dict:
    """conversation_id -> title, read-only."""
    from backend.db.database import open_db

    ids = [int(c) for c in conversation_ids if c is not None]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    async with open_db() as db:
        cur = await db.execute(
            f"SELECT id, title FROM conversations WHERE id IN ({marks})", ids
        )
        return {r["id"]: r["title"] for r in await cur.fetchall()}


def _override_config(config_mod, args) -> None:
    """Point CONFIG_PATH at a throwaway config when --embed-* overrides are
    given; otherwise honor --config. Module-attribute redirect only: no real
    config file is ever modified."""
    if args.embed_api_base or args.embed_model:
        cfg = config_mod.load_config()
        cfg["embeddings"] = {
            "api_base": args.embed_api_base or "",
            "api_key": args.embed_key or "",
            "model": args.embed_model or "",
        }
        cfg["server"] = {"binary": "", "model": ""}
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        )
        json.dump(cfg, tmp)
        tmp.close()
        config_mod.CONFIG_PATH = Path(tmp.name)
    elif args.config:
        config_mod.CONFIG_PATH = Path(args.config)


def main(argv=None) -> int:
    args = parse_args(argv)
    queries_path = Path(args.queries)
    if not queries_path.exists():
        print(f"query file not found: {queries_path}", file=sys.stderr)
        return 2
    queries = load_queries(str(queries_path))
    if not queries:
        print("no queries in file (blank lines and # comments are skipped)", file=sys.stderr)
        return 2

    # YAAH_DB_PATH is read at import time; must be set before backend imports.
    if args.db:
        os.environ["YAAH_DB_PATH"] = args.db

    from backend.agent import config as config_mod

    _override_config(config_mod, args)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    if args.rebuild:
        print("# rebuilding vector index (user-triggered, same path as Settings)...")
        print(f"# {asyncio.run(_maybe_rebuild())}")

    asyncio.run(evaluate(queries, args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
