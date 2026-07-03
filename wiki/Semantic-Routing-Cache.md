# Semantic Routing Cache

The third cache layer (v3.53.0), sitting between the exact-match [routing cache](Caching#routing-cache) and the routing LLM: embedding-based reuse of routing decisions across *rephrasings* of the same question.

## The problem it solves

The exact-match routing cache can't see that "will it rain later" and "will it rain this evening" are the same routing decision — each unique phrasing pays its own cold LLM call, ~200–700ms on this project's reference hardware per [BENCHMARKS.md](https://github.com/immortalbob/Mnemolis/blob/main/BENCHMARKS.md). For real personal usage — one person asking natural variations of the same few dozen questions — that's the single largest remaining avoidable latency in the routing path after the v3.50.x campaign fixed everything else.

## How it works

On a routing-cache miss inside `_llm_detect()` (deliberately *inside* the singleflight lock, so concurrent identical queries still coordinate on one lookup):

1. The query is embedded — one small POST to the configured embedding model, riding `llm.py`'s existing persistent connection pool.
2. The vector is compared (cosine similarity, via normalized dot products) against embeddings of previously-decided queries.
3. If the **best** match clears `SEMANTIC_ROUTING_THRESHOLD`, that query's decision is read back from the **live routing cache** and reused — the routing LLM never runs.
4. The new phrasing is promoted to a normal exact-match routing entry (so this precise wording never even pays the embedding call again) and its own vector is stored, making it matchable for the *next* rephrasing.

On a miss, the LLM decides exactly as before — and the vector computed for the lookup is reused to store the fresh decision's embedding, so the cold path costs **at most one embedding call total**, never two for the same text.

## Design constraints, in priority order

**1. Never make routing worse than v3.52.0.** Every failure mode — no embedding model configured, the embedding call failing or timing out, an empty store, nothing above threshold — falls through to exactly what happened before this feature existed. The one failure it *can* introduce is a wrong reuse (silently routing a query to the wrong source), which is why the threshold defaults conservatively to 0.92 — a floor where rephrasings clear it but topic-adjacent-yet-different intents ("is it raining" vs "is my network down") don't, for typical sentence-embedding models.

**2. Decisions are never stored here.** The store holds only `(query → normalized vector)` pairs; the decision is always read back from the routing cache at match time. A candidate whose routing entry has expired or been evicted is skipped *and pruned* — this store can never serve a decision the routing cache itself no longer stands behind, so there is exactly one source of truth for what was decided and one TTL governing how long it's trusted. The same reasoning covers the exact-key case: `find_similar()` only ever runs after the exact-match cache *missed*, so if the store contains the query's own key, that entry's decision has expired — matching a query against itself would just re-serve the decision the TTL deliberately retired, so it's excluded.

**3. In-memory only, rebuilt through use.** The routing cache persists to disk; this store deliberately doesn't. Embeddings rebuild lazily as cold queries recur after a restart — at a cost the feature's own math already covers, since the query rebuilding an embedding was about to pay a full LLM call anyway. Persisting float vectors would mean a new on-disk format, versioning against embedding-model changes, and staleness coupling with `routing_cache.json`, all to save milliseconds on a path that already costs hundreds of them.

## Model changes drop the store

Vectors from different embedding models live in different spaces — their cosine similarities against each other are noise that could clear the threshold by accident. Both public entry points check whether `EMBEDDING_MODEL` changed since the store was built and drop everything if so; a per-entry dimension check catches the residual case of two models sharing a name.

## The vector math is deliberately dependency-free

No numpy. Vectors are L2-normalized once at store time, so every lookup is a plain dot product per candidate — computed with `math.sumprod()` (C-speed, Python 3.12+, which the project's own Dockerfile pins), making a full scan of a maxed 500-entry store sub-millisecond. A pure-Python loop would cost 50–100ms at that size, which is why normalization-at-store-time plus `sumprod` matters and not just as style. The scan never early-exits at the first above-threshold candidate: with a conservative threshold the *best* match is the honest answer, and at C speed the full scan saves nothing measurable to skip.

## Setup

Disabled by default — `EMBEDDING_MODEL` empty means every call is a guaranteed no-op with zero network I/O, so a fresh deployment that never pulled an embedding model pays nothing. To enable on Ollama:

```bash
ollama pull nomic-embed-text
```

```env
EMBEDDING_MODEL=nomic-embed-text
# EMBEDDING_URL defaults to LLM_URL — set only if embeddings live elsewhere
```

`all-minilm` (smaller/faster) and `mxbai-embed-large` (larger/more precise) are reasonable alternatives. See [Configuration Reference](Configuration-Reference) for `SEMANTIC_ROUTING_THRESHOLD`, `SEMANTIC_CACHE_MAX_SIZE`, and `EMBEDDING_TIMEOUT_SECONDS`.

## Observability

`GET /cache/semantic` reports whether the feature is enabled, how many embeddings are stored, which model produced them, and the active threshold. `POST /cache/semantic/clear` empties it. Every semantic reuse is visible three ways: an INFO log line with the matched query and similarity, a `semantic_routing_hit` stat on the request, and an `intent_semantic` event in the [explanation chain](Explanation-Chains) carrying the matched query and the similarity score — which is also the recommended way to audit whether the threshold is right for *your* traffic before tuning it: run your real rephrasings with `explain=true` and look at what matched, and at what similarity.

## Concurrency posture

The same benign-race stance as the routing cache itself: plain dict get/set are atomic under the GIL, two threads racing to store the same query's vector write equivalent values and last-write-wins, and eviction mirrors `_evict_oldest_routing()`'s unlocked O(n) scan (the same accepted judgment — at a 500-entry cap the scan is noise). Writers on the *same* query are already serialized one frame up by `_llm_detect()`'s singleflight lock.
