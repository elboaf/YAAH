# ADR 0023: Hybrid retrieval fuses keyword and semantic legs by reciprocal rank fusion, with the keyword leg as the model-free floor

Date: 2026-11-09
Status: Accepted
Driven by: Phase 3 gate of the retrieval roadmap (elboaf/YAAH#679), opened once
Phases 1–2a/2b landed (FTS5 trigram keyword index, model-agnostic embeddings
client, bundled local model server). Decisions settled in the 2026-11-09
grilling session; see the issue for the ticket tree.
Supersedes: the ad-hoc merge in `backend/agent/search_semantic.py`
(semantic-first prepend with message_id dedupe), which remains in place until
Phase 3 lands and is then replaced by the fusion described here.

## Context

Search runs two retrievers today. The keyword leg is SQLite FTS5 with the
trigram tokenizer (bm25-ordered, order-only: the rank is used for ORDER BY and
never emitted as a score). The semantic leg cosine-scores the entire scoped
vector store in a worker thread and returns chunk-grained hits (score = cosine
similarity). `search_with_semantic` merges the two ad hoc: semantic hits
prepended, keyword hits appended if not already present, dedupe by
`message_id`, truncate to `max_results`. Both surfaces — the agent search tool
and the human HTTP/UI endpoint — share that one seam. Both legs run at depth
`max_results`, so the merge can only reorder what it already sees: a keyword
hit at position 12 can never surface into a fused top-10.

The question for Phase 3: how the two score families (unbounded bm25 ordering
vs bounded cosine) become one ranked list, without normalization brittleness,
without a fragile router, and without breaking the degrade guarantees of
ADR-0021.

## Decision

1. **Reciprocal Rank Fusion, not weighted score fusion.** Each leg
   contributes weight 1/(k + rank), summed per message; k = 60; equal leg
   weights. Ranks are scale-free, so bm25's unbounded scores and cosine's
   0..1 never need normalization — the normalization layer that weighted
   fusion requires (and that drifts with query length and embedding-model
   swaps) is designed out entirely. Weighted score fusion was rejected for
   that brittleness; a learned fuser was rejected for lack of training data.

2. **Deep slice per leg: K = 100, cut back to `max_results`.** Each leg
   contributes its top-100 to the fuser; the fused list is truncated to the
   caller's `max_results`. The semantic leg already scores the whole store
   before truncating (the deep slice costs it nothing); the keyword fetch
   loop raises its collection target. K=100 is a fixed constant, chosen over
   a proportional rule for explainability; a personal transcript store makes
   it cheap.

3. **Per-message aggregation: best chunk per leg, summed across legs.** A
   message's semantic contribution is its highest-ranked chunk only — no bias
   toward long messages that spawn many chunks. Its fused score is the sum of
   its keyword and semantic contributions. On collision, the keyword leg's
   literal excerpt wins (it shows the actually-matched text); otherwise the
   best chunk's text.

4. **Always both legs, with an explicit opt-out, never a router.** Every
   query goes through both retrievers; RRF naturally ranks exact identifier
   matches first (the keyword leg puts them at position 1, and cross-leg
   agreement reinforces them). A heuristic router (identifier-like → keyword
   only) was rejected: a conceptual query that merely looks identifier-like
   silently loses its semantic leg, with no test able to prove the router
   right. The explicit opt-out is a leading `kw:` prefix on the query string,
   stripped before search, honored identically on the agent tool and the
   human search box. It forces keyword-only results and emits
   `semantic_state: "off"` — a state the response contract already promised
   and no code path produced until now.

5. **One fusion path, both surfaces, over the assembled scope.** Fusion lives
   in the shared `search_with_semantic` seam and therefore upgrades the agent
   tool and the human endpoint together. It runs over the assembled scope:
   workspace + related workspaces for the agent tool, all workspaces for the
   human endpoint. When the semantic leg is unavailable for any reason
   (ADR-0021 ladder: unconfigured, no index, model mismatch, endpoint
   unreachable, dimension drift, store fault), fusion auto-degrades to
   keyword-only — the keyword leg remains the model-free floor, untouched by
   anything embeddings-related.

6. **Deterministic ordering.** Fused score descending, then keyword rank
   ascending (keyword breaks ties — the floor is the tiebreaker), then
   `message_id` ascending. Same store, same query, same order every run;
   tests assert exact orderings against a seeded fake embedder.

7. **Honest, additive wire shape.** Existing keys keep their names:
   `semantic_matches` is redefined to how many returned fused hits received a
   semantic contribution; `truncated` reflects fused candidates beyond the
   cut (no longer keyword-leg-only); each hit gains an optional
   `sources: ["keyword", "semantic"]` array. Nothing else on the wire moves.

8. **Evaluation is a merge gate, locally.** `scripts/search_eval.py` is
   committed; it runs a plain personal query file (untracked, queries only —
   transcripts stay home per ADR-0021) against the live local store and
   prints keyword-only vs fused top-N side by side with per-hit source
   markers. A pasted run is a required check on the fusion PR. The harness
   lands first so the paste judges the branch's own code with landed tooling.

9. **Reranking is deferred behind the evidence, sketched now.** llama-server
   supports a cross-encoder rerank endpoint (`--reranking --pooling rank`,
   `POST /v1/rerank`), but the flag is mutually exclusive with `--embeddings`,
   so a reranker is a second llama-server process on its own port — exactly
   what ADR-0022's one-model-per-process rule independently requires. If the
   eval paste shows RRF's ceiling on real queries, a follow-on adds a
   manifest entry (new task hint), a second `ServerManager` tenant, and a
   rerank of the fused top-20 when it exceeds `max_results`. Reranking is
   not built now and not declared permanently out of scope.

## Consequences

- The merge in `search_with_semantic` changes character from
  "semantic-first prepend" to a true fusion; consumers that relied on
  "all semantic hits first" ordering must instead read the fused order (the
  response `sources` array makes each hit's origin visible).
- Score normalization never enters the codebase: no bm25 rescaling to go
  stale across embedding-model swaps, the exact brittleness ADR-0021's
  model-agnostic posture tries to contain.
- Keyword depth rises from `max_results` to K=100 per search — a bounded,
  cheap increase on FTS5.
- `semantic_state: "off"` becomes a real emitted state; the docstring
  contract finally matches behavior.
- Fusion behavior is default-on with no toggle: degraded mode is the escape
  hatch, and a pasted eval run validates ranking changes before merge.
- A reranker later slots in as a second model-server tenant without
  re-opening this decision — the second-process shape is already settled
  here.
