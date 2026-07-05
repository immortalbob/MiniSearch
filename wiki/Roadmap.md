# Roadmap

This page reflects where the project actually stands, not a static wishlist — it gets revisited and corrected as work lands, the same way a stale README section gets caught and fixed rather than left to drift.

## Capability Expansion — complete

The five original items that defined the project's early feature set are all done:

1. ✅ Configurable thresholds
2. ✅ Kiwix search term disambiguation — see [Kiwix Disambiguation](Kiwix-Disambiguation)
3. ✅ Multi-book Kiwix fusion — see [Multi-Book Fusion](Multi-Book-Fusion)
4. ✅ Confidence-aware fusion with expanded ingest — see [Confidence-Aware Fusion](Confidence-Aware-Fusion)
5. ✅ Conditional query detection — see [Conditional Query Detection](Conditional-Query-Detection)

## Battle Testing & Operational Maturity — complete

Three real gaps, found through deliberate review rather than reported failures, all closed:

- ✅ Discourse-framing routing bypass — see [The Discourse-Framing Investigation](The-Discourse-Framing-Investigation)
- ✅ Fallback visibility in `/logs/stats`
- ✅ Routing cache size bounding + visibility in `/health`
- ✅ Background snapshot job health
- ✅ Adversarial self-testing — see [Adversarial Self-Testing](Adversarial-Self-Testing)
- ✅ Cross-source temporal pattern detection — see [Cross-Source Temporal Pattern Detection](Cross-Source-Temporal-Pattern-Detection)

Full mechanism detail for the operational maturity work lives in [Health & Observability](Health-and-Observability) and [Caching](Caching).

## Bulletproofing Pass — complete

A deliberate, full read of every file in `app/`, top to bottom — specifically ignoring complexity scores and looking at the kind of small, simple-looking code that score-driven review naturally skips. Found and fixed real bugs in nearly every file touched, several of them significant:

- ✅ `home_assistant.py` — a severe word-boundary bug ("is the front door locked" silently returning no results), an area-filtered query silently skipping real exclusion-keyword filtering, a three-bug chain around `binary_sensor`-style motion entity support, and a small grammar fix
- ✅ `kiwix.py` — non-deterministic book selection, broken table-of-contents stripping, a single-character search-term bug, and an unbounded retry loop with a real multi-minute worst case
- ✅ `fusion.py` — a real crash on `FUSION_MAX_SOURCES=0`
- ✅ `snapshots.py` — uptime history only covering 9.6 real hours instead of a full week
- ✅ `router.py` / `fusion.py` — a cross-file drift in the shared "did this source actually fail" logic that silently disabled the `news`→`web` fallback for unconfigured sources
- ✅ `forecast.py` — an unconfigured deployment silently returning real weather data for the wrong place on Earth
- ✅ `llm.py` — thinking models on the OpenAI-compatible backend silently returning no answer at all

`mcp_server.py`, `query_expansion.py`, and `searxng.py` were read with the same scrutiny and came back genuinely clean — a real, useful outcome in its own right, confirming prior work in those files holds up.

A later, separate seven-finding investigation into `fusion.py` and its direct dependents (v3.50.18) found one more item in the same spirit — listed here rather than as its own section, since it's the identical "deliberate full read catches a real gap" shape as the rest of this pass, just arriving later: ✅ `fusion.py`'s concurrent source dispatch wasn't propagating `suppress_cache_writes()` into its worker threads, even though `router.py`'s `_resolve_conditional()` and `searxng.py`'s own concurrent fetch had already established the correct `contextvars.copy_context()` pattern for this exact problem. See [The Caching Concurrency Investigation](The-Caching-Concurrency-Investigation#the-sharp-edge-this-design-left-behind) for the mechanism and [The Fusion Merge Bugs](The-Fusion-Merge-Bugs#the-contextvar-propagation-gap) for this specific fix.

## Full Function-by-Function Audit — complete

A systematic, deliberate read of every function in every file in `app/`, smallest to largest (83 lines to 2482 lines). First time in this project’s history that every function was verified directly rather than inferred from tests passing or documentation existing. Fourteen real bugs found and fixed across 18 files; four of the largest files (including `router.py` at 2482 lines) came back genuinely clean.

- ✅ `forecast.py` — uncaught exception on partial API responses
- ✅ `mcp_server.py` — two independent `"/mcp"` literals (now `MCP_MOUNT_PATH`); `isError=True` fix
- ✅ `freshrss.py` — HTML stripping broke on attribute values; entities never decoded
- ✅ `searxng.py` — new `ThreadPoolExecutor` per call (46 threads at peak); early timeout held caller 500ms past limit
- ✅ `llm.py` — dict-shaped `reasoning` field would crash `.splitlines()`
- ✅ `scoring.py` — possessives scored 20 points worse; generic-title false positives on real news articles
- ✅ `uptime_kuma.py` — unknown status codes silently claimed “All 0 services up”; lockless startup contract violation
- ✅ `snapshots.py` — `seen_changes` suppressed legitimate repeated HA/news events
- ✅ `main.py` — TTFK SQL reported minimum latency instead of first-occurrence latency

Full per-file account in [The Full Audit Pass](The-Full-Audit-Pass). Benchmark at v3.51.0: zero failures across 1750 requests at 20 concurrent users; warm aggregated p95 63ms.

## Documentation Restructuring — complete

- ✅ This wiki — every page split between user-facing reference and dev-blog-style Design History, narrative moved out of mechanism pages into dedicated saga pages or same-page Development Notes sections
- ✅ `CHANGELOG.md` split at the v3.44.1/v3.45.0 seam — the project's own real checkpoint between the original feature/refactor/bulletproofing era and the current investigation-driven one. Everything before v3.45.0 lives in `CHANGELOG-ARCHIVE.md`, unedited; every substantial multi-bug investigation from that range that didn't already have a wiki page got one — see [The Complexity Refactor Campaign](The-Complexity-Refactor-Campaign).
- ⬜ The README stays lean going forward — deep-dive material gets added here instead of growing the README further

## Post-Audit Optimization & Observability — complete

The v3.51.1 external function-by-function review and the two releases implementing everything it recommended, plus the first of the original four design documents to ship:

- ✅ **v3.52.0** — the `/search` `route_query()` redesign (no wasted pre-route LLM call for compound queries; `cached`/`fallback_occurred` recorded at the code points where they genuinely happen via the `_ROUTE_STATS` ContextVar channel), parallel decomposed sub-queries (`DECOMPOSE_MAX_PARALLEL`), persistent HTTP sessions for every source module, FreshRSS token caching with 401 self-healing, batched routing-cache persistence with clean-shutdown flushing, WAL-consistent `/backup`, the Kiwix Wikipedia-bonus scoring fix, the conditional-remainder proper-noun-pair guard, and the SQLite connection-leak sweep. Benchmarked: zero failures across 1741 requests; warm aggregated p95 54ms, a new project best.
- ✅ **v3.53.0 — [Semantic Routing Cache](Semantic-Routing-Cache)** — embedding-based reuse of routing decisions across rephrasings, targeting the last avoidable routing-path latency (the ~200–700ms cold LLM call for a query the exact-match cache has seen only in different words). Opt-in via `EMBEDDING_MODEL`; conservative similarity threshold; decisions always read from the live routing cache.
- ✅ **v3.53.0 — [Explanation Chains](Explanation-Chains)** — `/search` `explain=true` returns the ordered trace of what routing actually did, built as more keys in the v3.52.0 stats channel rather than the signature-threading the original design doc assumed. First of the four original design documents to ship.

## Fable Capability Extensions — in progress

A set of design documents authored as a development roadmap to extend Mnemolis's existing capabilities rather than add unrelated features. They are built and tested one at a time.

- ✅ **Answer Synthesis** — per-request `synthesize=true` grounded answer composition, with `voice`/`brief`/`detailed`/`digest` styles. See [Answer Synthesis](Answer-Synthesis). Shipped v3.55.0–v3.55.2.
- ✅ **History/Time-Series Source** — the `history` source: time-series memory over the house's own numeric sensors and service state, with deterministic highs/lows/averages/counts/trends, coverage disclosure, and the shared `resolve_window()` extraction (`changes` byte-identity preserved). See [History & Trends](History-and-Trends). Shipped v3.56.0.
- ⬜ **Sentinel-Standing-Queries**
- ⬜ **Conversational-Sessions**
- ⬜ **Access-Partitioning-and-Memory**
- ⬜ **Semantic-Relevance-Layer**
- ⬜ **Vision-Source-Cross-Modal-Grounding**
- ⬜ **Curator-Continuous-Evaluation**
- ⬜ **Mnemoforge-Corpus-Foundry**
