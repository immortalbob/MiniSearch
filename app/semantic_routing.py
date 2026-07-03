"""
Mnemolis Semantic Routing Cache

Embedding-based reuse of LLM routing decisions across *rephrasings* of
the same question. The exact-match routing cache can't see that "will it
rain later" and "will it rain this evening" are the same routing
decision — each unique phrasing pays its own cold LLM call (~200-700ms
on this project's reference hardware, per BENCHMARKS.md). With an
embedding model configured, a routing-cache miss first embeds the query
and compares it against embeddings of previously-decided queries; a
cosine similarity above SEMANTIC_ROUTING_THRESHOLD reuses that decision
and skips the routing LLM entirely.

Design constraints, in order of importance:

1. NEVER make routing worse than v3.52.0. Every failure mode — no
   embedding model configured, the embedding call failing or timing
   out, an empty store, no candidate above threshold — falls through to
   exactly what happened before this module existed: the routing LLM
   call. A wrong reuse is the one failure this can introduce, which is
   why the threshold default is deliberately conservative (see its
   comment in app/config.py) and why matches only ever reuse decisions
   the LIVE routing cache still stands behind (see point 3).

2. Decisions are never stored here. This module stores only
   (query -> normalized embedding vector) pairs; the decision itself is
   always read back from the routing cache at match time via the
   caller-provided lookup. If the matched query's routing entry has
   expired or been evicted, that candidate is skipped and pruned — this
   store can never serve a decision the routing cache itself no longer
   holds, so there is exactly one source of truth for what was decided
   and one TTL governing how long it's trusted.

3. In-memory only, rebuilt through use. The routing cache persists to
   disk; this store deliberately doesn't — embeddings rebuild lazily as
   cold queries recur after a restart, at a cost this module's whole
   point already covers (the query that rebuilds an embedding was about
   to pay a full LLM call anyway). Persisting float vectors would mean
   a new on-disk format, versioning against embedding-model changes
   (vectors from different models are not comparable — see
   reset_if_model_changed()), and staleness coupling with
   routing_cache.json, all for saving milliseconds on a path that
   already costs hundreds of them.

Concurrency: the same benign-race posture as the routing cache itself
(see _set_routing()'s surroundings in app/router.py) — plain dict
get/set are atomic under the GIL, two threads racing to store the same
query's vector both write equivalent values and last-write-wins, and
the eviction scan mirrors _evict_oldest_routing()'s unlocked shape.
Writers on the SAME query are already serialized by _llm_detect()'s
singleflight lock one frame up.

Vector math: vectors are L2-normalized once at store time, so a lookup
is a plain dot product per candidate — computed with math.sumprod()
(C-speed, Python 3.12+, which this project's own Dockerfile pins),
making a full scan of a maxed-out 500-entry store sub-millisecond
rather than the 50-100ms a pure-Python loop would cost. A guarded
fallback covers older interpreters for anyone running the code outside
the shipped container.
"""

import logging
import math
import time
from typing import Callable, NamedTuple

from app.config import settings
from app.llm import embed, embed_batch, embeddings_configured

_LOGGER = logging.getLogger(__name__)

# query text -> (normalized vector, stored-at timestamp).
# Timestamps exist only for oldest-eviction, mirroring the routing cache.
_store: dict[str, tuple[list[float], float]] = {}

# The embedding model the current store's vectors came from. Vectors
# from different models live in different spaces and their cosine
# similarities are meaningless against each other — if the configured
# model changes at runtime (a settings edit + reload, a test), the
# store must be dropped, not compared across.
_store_model: str = ""

# sumprod is 3.12+; the Dockerfile pins python:3.12-slim so the shipped
# container always takes the fast path, but the fallback keeps the
# module honest for anyone running the code directly on an older
# interpreter rather than failing with an AttributeError at first use.
if hasattr(math, "sumprod"):
    _dot: Callable[[list[float], list[float]], float] = math.sumprod
else:  # pragma: no cover — unreachable on the shipped 3.12 container
    def _dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b))


def _normalize(vector: list[float]) -> list[float] | None:
    """L2-normalize once at store time so every lookup is a bare dot
    product. Returns None for a zero vector (a degenerate embedding
    response) rather than dividing by zero."""
    norm = math.sqrt(_dot(vector, vector))
    if norm == 0.0:
        return None
    return [x / norm for x in vector]


def reset_if_model_changed() -> None:
    """Drop the store if EMBEDDING_MODEL changed since it was built —
    vectors from different embedding models are not comparable, and a
    cross-model cosine similarity is noise that could clear the
    threshold by accident. Called by both public entry points so the
    check can't be forgotten at a call site."""
    global _store_model
    if settings.embedding_model != _store_model:
        if _store:
            _LOGGER.info(
                "Embedding model changed (%r -> %r) — dropping %d semantic cache entries",
                _store_model, settings.embedding_model, len(_store),
            )
        _store.clear()
        _store_model = settings.embedding_model


def _evict_oldest() -> None:
    """Evict the single oldest entry — the identical bounded-eviction
    shape _evict_oldest_routing() uses, and deliberately the same
    accepted O(n) scan: at the default 500-entry cap this is noise, per
    the same judgment already made (and documented in the v3.52.0
    review) for the routing cache's own eviction."""
    if not _store:
        return
    oldest_key = min(_store, key=lambda k: _store[k][1])
    del _store[oldest_key]


def store(query: str, vector: list[float] | None = None) -> None:
    """Store `query`'s embedding for future similarity matches.

    Called right after a fresh routing decision is cached, and after a
    semantic match writes the new query's exact-match entry (the vector
    was just computed for the lookup — storing it is free and makes the
    new phrasing itself matchable). Pass `vector` when the caller
    already has it; otherwise one embedding call is made — on the cold
    path only, which just paid a full LLM call, so the ~tens of
    milliseconds are a rounding error against what the next rephrasing
    saves.

    No-ops (never raises) when embeddings are unconfigured or the
    embedding call fails — see design constraint 1 in the module
    docstring.
    """
    if not embeddings_configured():
        return
    reset_if_model_changed()
    if vector is None:
        vector = embed(query)
        if vector is None:
            return
    normalized = _normalize(vector)
    if normalized is None:
        return
    key = query.lower().strip()
    if key not in _store and len(_store) >= settings.semantic_cache_max_size:
        _evict_oldest()
    _store[key] = (normalized, time.time())


class SemanticMatch(NamedTuple):
    """One accepted semantic match — see find_similar()."""
    matched_query: str
    decision: str
    similarity: float


def find_similar(
    query: str, decision_lookup: Callable[[str], str | None]
) -> tuple[SemanticMatch | None, list[float] | None]:
    """Find a previously-decided query semantically equivalent to
    `query`. Returns (match, query_vector):

    - match: a SemanticMatch, or None when nothing clears the threshold
      (or the feature is off / the embedding call failed / the store is
      empty).
    - query_vector: the normalized embedding computed for `query`
      during the lookup, or None if none was computed. Returned even on
      a miss so the caller can pass it straight to store() after the
      LLM decides — the whole cold path then costs exactly ONE
      embedding call total, never two for the same text.

    `decision_lookup` maps a stored query text back to its LIVE routing
    decision (router passes a closure over _get_routing) — this module
    never stores decisions itself, per design constraint 2. A candidate
    whose decision has expired or been evicted from the routing cache
    is skipped AND pruned from this store, so dead entries don't keep
    paying dot-product cost on every future lookup.

    Exactly one embedding call per invocation (for `query` itself), and
    only when the store is non-empty — an empty store returns before
    any network I/O, so the feature's cost on a fresh start is
    genuinely zero until there's something to match against.

    The comparison scans every stored entry rather than stopping at the
    first above-threshold candidate — with a conservative threshold the
    BEST match is the honest answer, and at C-speed dot products the
    full scan of a maxed store is sub-millisecond (see module
    docstring), so early exit would save nothing measurable while
    occasionally returning a worse match.
    """
    if not embeddings_configured():
        return None, None
    reset_if_model_changed()
    if not _store:
        return None, None

    vector = embed(query)
    if vector is None:
        return None, None
    normalized = _normalize(vector)
    if normalized is None:
        return None, None

    key = query.lower().strip()
    best_query: str | None = None
    best_similarity = 0.0
    dead: list[str] = []
    # list() — a stable snapshot of keys, since pruning below and
    # concurrent store() calls can both mutate the dict mid-scan.
    for candidate, (candidate_vector, _ts) in list(_store.items()):
        if candidate == key:
            # The exact key can be present here while its routing entry
            # has expired (that's the only way this lookup runs at all —
            # the caller already missed the exact-match cache). Matching
            # a query against itself would just re-serve the expired
            # decision the TTL deliberately retired.
            continue
        if len(candidate_vector) != len(normalized):
            # Dimension mismatch — a stale entry from a different model
            # that slipped past the model-change reset (e.g. two models
            # sharing a name). Not comparable; prune.
            dead.append(candidate)
            continue
        similarity = _dot(normalized, candidate_vector)
        if similarity > best_similarity:
            best_similarity = similarity
            best_query = candidate

    if best_query is not None and best_similarity >= settings.semantic_routing_threshold:
        decision = decision_lookup(best_query)
        if decision is not None:
            _LOGGER.info(
                "Semantic routing match: '%s' ~ '%s' (similarity=%.3f) -> %s",
                query[:50], best_query[:50], best_similarity, decision,
            )
            for d in dead:
                _store.pop(d, None)
            return SemanticMatch(best_query, decision, best_similarity), normalized
        # The best match's decision expired out of the routing cache —
        # prune it so it stops winning lookups it can't pay off.
        dead.append(best_query)

    for d in dead:
        _store.pop(d, None)
    return None, normalized


_WARMUP_BATCH_SIZE = 32


def warm(entries: list[tuple[str, float]]) -> int:
    """Pre-populate the store from previously-decided queries — the
    startup warmup. Returns how many embeddings were stored.

    The store is deliberately in-memory only (design constraint 3), so
    every restart used to mean cold rephrasings paid the routing LLM
    again until the store lazily repopulated. But routing_cache.json
    PERSISTS — the queries and their decisions survive the restart, and
    only the vectors were missing. Re-embedding those persisted queries
    at startup is cheap (batched — see embed_batch()'s own comment) and
    restores the full pre-restart matching ability without introducing
    any of the on-disk-vector problems constraint 3 exists to avoid:
    nothing is persisted, model changes still just mean re-embedding,
    and the routing cache remains the single source of truth for
    decisions exactly as before.

    `entries` is (query, decided_at) pairs whose routing decisions are
    LIVE — the caller (router.warm_semantic_routing_cache()) filters by
    the routing TTL before handing them over, so this never embeds a
    query whose decision find_similar() would immediately skip as
    expired. Newest-first up to the capacity the store has left, so
    when there are more persisted queries than room, the ones most
    likely to be rephrased soon win the slots. Already-present keys are
    skipped rather than re-embedded (a warm store from a previous
    warmup, or requests that arrived before this background thread got
    scheduled).

    One failed batch aborts the whole warmup with a warning rather than
    retrying or continuing: a batch failure here almost always means
    the embedding backend is down or the model isn't pulled, and every
    subsequent batch would fail identically — and a partial (or empty)
    store is exactly the state the feature already handles gracefully,
    since lazy through-use population remains the normal path.
    """
    if not embeddings_configured():
        return 0
    reset_if_model_changed()

    capacity = settings.semantic_cache_max_size - len(_store)
    if capacity <= 0:
        return 0

    fresh = [
        (query.lower().strip(), ts) for query, ts in entries
        if query.lower().strip() not in _store
    ]
    fresh.sort(key=lambda pair: pair[1], reverse=True)  # newest first
    fresh = fresh[:capacity]
    if not fresh:
        return 0

    stored = 0
    now = time.time()
    for i in range(0, len(fresh), _WARMUP_BATCH_SIZE):
        batch = fresh[i:i + _WARMUP_BATCH_SIZE]
        vectors = embed_batch([q for q, _ts in batch])
        if vectors is None:
            _LOGGER.warning(
                "Semantic cache warmup aborted after %d embeddings — batch "
                "embed failed (backend down or model not pulled?); the "
                "store will continue to populate lazily through use as "
                "always", stored,
            )
            return stored
        for (query, _ts), vector in zip(batch, vectors):
            normalized = _normalize(vector)
            if normalized is None:
                continue
            _store[query] = (normalized, now)
            stored += 1
    return stored


def stats() -> dict:
    """Small observability surface for /cache/semantic — entry count,
    the model the vectors came from, and the configured threshold, so
    'is this feature actually on and populated' is answerable without
    reading logs."""
    return {
        "enabled": embeddings_configured(),
        "entries": len(_store),
        "max_size": settings.semantic_cache_max_size,
        "embedding_model": _store_model or settings.embedding_model,
        "threshold": settings.semantic_routing_threshold,
    }


def clear() -> int:
    """Clear the store. Returns count removed."""
    count = len(_store)
    _store.clear()
    return count
