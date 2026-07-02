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

## Known limitations (tracked, accepted, not blocking)

These are real, understood boundaries — not bugs waiting for a fix, but deliberate scope decisions or honest, accepted ceilings. A reader-facing version of this same list, written for evaluating fit rather than tracking status, lives at [Known Limitations](Known-Limitations):

- **Single ambiguous bare words** (e.g. "galaxy") can land on a thematically-related but imprecise match when the index genuinely contains multiple comparably-relevant senses of the word. See [Kiwix Scoring](Kiwix-Scoring#where-scoring-still-has-a-real-ceiling).
- **Conditional phrasing without an explicit comma** ("if the front door is unlocked tell me") is intentionally not detected — a real grammatical-parsing problem, not a pattern-matching one. See [Conditional Query Detection](Conditional-Query-Detection#why-the-pattern-is-this-narrow).
- **A decomposed segment merging two unrelated topics** may route to a single source that doesn't serve both well — an accepted, minor side effect of the [proper-noun-pair guard's](The-Proper-Noun-Pair-Saga) content-preservation fix, not a regression.

## Tabled, revisit in ~1 year

These are still squarely in "permitted to fail, no obligation to succeed" territory — the same honest framing the now-shipped temporal pattern detection work used to carry above, before it actually landed.

**Cross-modal grounding** — correlating a camera snapshot with a text answer ("did anything weird happen at the back door" pulling the actual image alongside the sensor log) would be a genuine "wow" capability, not just well-executed plumbing. Deliberately not pursued yet — the current camera setup (Ring) isn't infrastructure worth building on top of long-term; revisit once a self-controlled NVR solution exists instead.

## Still tracked, lower priority

- **New source modules** — see [Contributing](Contributing) for the current list of proposed ones looking for contributors
- **HA/voice pipeline architecture question** — whether to bypass Home Assistant's own conversation/intent layer for non-device-control voice queries, piping STT output more directly to Mnemolis's `/search` instead, and keeping HA for device control and audio I/O only. Raised, never designed — a genuinely different kind of work (infrastructure/integration) than anything else on this list.
- **`fusion.py`'s title-only item deduplication can treat genuinely different articles as duplicates.** `_dedupe_items_across_blobs()` keys purely on an item's leading `**Title**` line — confirmed this can mis-merge wire-service syndication or multiple outlets covering the same event with identical headline phrasing. A naive full-content key was checked and found to introduce its own regression (the same article reached via two different tracking-parameter URLs would wrongly count as separate items). The right fix — title plus normalized URL when present, falling back to title-only when not — is specified as a direction, not yet implemented. Found during the v3.50.18 `fusion.py` investigation; see [The Fusion Merge Bugs](The-Fusion-Merge-Bugs#title-only-item-deduplication-risk--documented-not-yet-fixed) for the full reasoning.
✅ **The MCP `search` tool now correctly surfaces genuine unexpected failures as `isError=True`.** All expected, recoverable failures (source not configured, empty results, unknown source name) are returned as descriptive strings by `route()` itself — those still come back as successful responses. A genuine unexpected exception (a real bug) now propagates to the SDK’s own handler, which returns `CallToolResult(isError=True, ...)`, giving MCP clients a structured way to distinguish the two cases. Fixed by removing the broad `try/except` from `search()`. See [MCP Server](MCP-Server) for the updated contract.
