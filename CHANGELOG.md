# Changelog

All notable changes to Mnemolis are documented here, from v3.45.0 onward. For everything before that — the original feature build-out (v1.0.0–v3.18.2), the static-analysis-driven complexity refactor campaign and MCP transport migration (v3.19–v3.22), and the battle-testing/bulletproofing era (v3.23–v3.44.1) — see [`CHANGELOG-ARCHIVE.md`](CHANGELOG-ARCHIVE.md) in the repo root. v3.44.1 is the project's own checkpoint marking that earlier era complete, right before this file picks up.

---

## [3.53.1]

### Fixed — The "Love Story (1944 film)" Incident: Colloquial Framings Now Strip as Whole Phrases

Explanation chains' very first real-world trace caught a live Kiwix bug: *"whats the story with molybdenum"* returned **Love Story (1944 film)** — a movie whose plot happens to involve molybdenum mining — instead of the element. The chain showed routing was correct (`intent_llm → kiwix`), isolating the failure to Kiwix's own search in about a minute. Two independent leaks, both confirmed by direct execution:

1. **`"whats"` (no apostrophe) leaked a literal `"what"` into the search terms.** The contraction-normalizing regex in `_build_search_terms()` only fires on an actual apostrophe; `"whats"` wasn't a stop word; and `_stem()` runs *after* stop-word filtering, so it stemmed the survivor into `"what"` on the way out. Fixed by adding the apostrophe-less contraction forms (`whats`, `hows`, `whos`, `wheres`, `thats`, `its`, `dont`, `cant`, …) to `_STOP_WORDS` directly — deliberately excluding real-topic homographs (`id`, `im`).
2. **`"story"` was never stripped at all.** The colloquial-question handling was *word*-level (`"deal"`, `"keep"`, `"hearing"`, `"up"` as bare `_STOP_WORDS` entries), and `"story"` wasn't among them. The search Kiwix actually received was `what story molybdenum`, and the film earned its winning points from `"story"` in both the search terms AND `_score_result()`'s title overlap.

The fix re-applies the lesson `DISCOURSE_FRAMING_PATTERNS`' own comment already documented: **strip whole phrases, not individual meaningful words.** New `COLLOQUIAL_QUESTION_PHRASES` removes `"what's the story with"` / `"whats the deal with"` / `"what is up with"` / `"keep hearing about"` and variants (apostrophe, apostrophe-less, and spelled-out forms — the apostrophe-less form is what actually triggered the incident) as units, applied in both `_build_search_terms()` and `_score_result()`'s scoring set. `_is_definitional_query()` still deliberately receives the original phrasing — those leading phrases are precisely what make such queries definitional, pinned by a test asserting the colloquial phrasing scores identically to formal "what is X".

Auditing the retired word-level entries found they'd been silently breaking every query where those words WERE the topic, confirmed directly: *"what is the New Deal"* had been searching Kiwix for the literal term `new`, and *"what is a hearing aid"* for `aid`, and *"what is up with the up quark"* lost half its subject. `"deal"`, `"keep"`, `"hear"/"heard"/"hearing"`, and `"up"` are no longer stop words; regression tests pin New Deal, hearing aid, and up quark surviving as topics. A knock-on: with `"up"` restored as a content word, zero INTENT_MAP keyword phrases are stop-word-only anymore (previously exactly two: "is it up", "are they up") — the `_filter_meaningful()` safety net deliberately stays for future additions, and its census test now pins the empty set with the full story.

11 new term-building tests + 3 new scoring tests, including the exact live incident pinned end to end (the element must out-score the film under the original query — confirmed the film genuinely won before the fix, 15 vs 40 after). `wiki/The-Discourse-Framing-Investigation.md` gained a postscript, including the operational note that a bad result from this bug persists in the result cache under its exact phrasing until the kiwix TTL — `POST /cache/clear` after deploying.

### Changed — docker-compose.example.yml Gained the LLM and Embedding Sections

The example compose file never had the LLM settings at all (`LLM_URL`, `LLM_MODEL`, `LLM_API_TYPE`, `LLM_KEEP_ALIVE`), and v3.53.0's embedding settings weren't added either — a fresh GitHub deployment following the example got keyword-only routing with no hint the LLM or semantic layers existed. Both sections added with the same inline-comment guidance style as the rest of the file, including the `docker exec ollama ollama pull nomic-embed-text` one-liner and a pointer to the Semantic Routing Cache wiki page.

### Changed
- Version bumped to 3.53.1. Test suite: 1440 passing (from 1429 at v3.53.0), ruff clean.

---

## [3.53.0]

### Added — Semantic Routing Cache

A third, opt-in cache layer between an exact-match routing-cache miss and the routing LLM: embedding-based reuse of routing decisions across *rephrasings* of the same question ("will it rain later" borrowing "will it rain this evening"'s already-made decision). Targets the last avoidable routing-path latency — the ~200–700ms cold LLM call for a query the exact-match cache has seen only in different words, which for real personal usage (one person asking natural variations of the same few dozen questions) is most cold routing calls.

Design constraints, in priority order (full rationale in `app/semantic_routing.py`'s module docstring and `wiki/Semantic-Routing-Cache.md`): (1) never make routing worse than v3.52.0 — every failure mode (feature off, embedding call failed, empty store, nothing above threshold) falls through to exactly the previous behavior, and the one failure it *can* introduce (a wrong reuse silently misrouting) is why `SEMANTIC_ROUTING_THRESHOLD` defaults conservatively to 0.92; (2) decisions are never stored in the semantic layer — it holds only `(query → normalized vector)` pairs, decisions are always read back from the LIVE routing cache at match time, expired candidates are skipped and pruned, so one source of truth and one TTL govern everything; (3) in-memory only, rebuilt through use — the query that rebuilds an embedding after a restart was about to pay a full LLM call anyway.

Mechanics: hooked inside `_llm_detect()`'s singleflight after the exact-match re-check; a hit promotes the new phrasing to a normal exact-match entry (never pays even the embedding call again) and stores its vector for the *next* rephrasing; a miss reuses the lookup's vector when storing the fresh LLM decision, so the cold path costs at most ONE embedding call total. Model changes drop the store (cross-model cosine similarity is noise), with a per-entry dimension check for the residual case. Vector math is dependency-free: L2-normalize at store time, `math.sumprod()` dot products at lookup (C-speed on the Dockerfile's pinned 3.12; a full scan of a maxed 500-entry store is sub-millisecond, vs 50–100ms for a pure-Python loop — the reason the scan can honestly return the BEST match rather than early-exiting at the first above-threshold one).

New: `app/semantic_routing.py`, `llm.embed()`/`llm.embeddings_configured()` (Ollama `/api/embed` and OpenAI-compatible `/v1/embeddings`, riding llm.py's existing session pool), settings `EMBEDDING_MODEL` (blank = feature fully disabled, zero network I/O), `EMBEDDING_URL`, `EMBEDDING_TIMEOUT_SECONDS`, `SEMANTIC_ROUTING_THRESHOLD`, `SEMANTIC_CACHE_MAX_SIZE`, endpoints `GET /cache/semantic` and `POST /cache/semantic/clear`. 18 new tests, including the one-embedding-call-total contract, expired-decision pruning, the exact-key self-match exclusion, model-change reset, and the LLM-never-called integration path.

### Added — Explanation Chains

`/search` with `"explain": true` returns the ordered trace of what routing actually did: intent resolution (keyword / cached / semantic-with-similarity / fresh LLM / default), result-cache hits, every source invocation with its real measured `elapsed_ms`, fallbacks, decomposition splits, conditional extractions, and fusion dispatches with their source lists. Returned on failures too, as the partial trace of everything that ran before the exception — when a trace earns its keep most.

The first of the project's four original design documents to ship, and deliberately built *after* the v3.52.0 stats channel rather than before it: `_route_event()` appends into the same per-request `_ROUTE_STATS` dict the boolean stats write into, at the same authoritative code points — the explanation is the SAME recording as the stats, never a parallel reconstruction, so it structurally cannot disagree with what actually ran. The original design sketch assumed threading explanation state through `route_with_source()`'s recursion-laden signature, exactly the change that function's history had twice judged not worth its risk; the ContextVar channel collapsed the feature to "more keys in a dict that already flows everywhere," as The Fallback Observability Gap's postscript predicted.

Ordering and attribution: within one thread, events append in genuine execution order (a fallback reads invoked → fallback → invoked); across concurrent decomposed sub-queries the interleaving is nondeterministic (list.append is GIL-atomic, order isn't), which is why every event carries the query text it belongs to — attribution comes from fields, never list position. Events are always collected (small-dict appends are noise next to real routing work, and it keeps the recording path branch-free) but only returned when asked; `explain` defaults to false and the response field is `null` otherwise, so existing clients see one new always-null key and nothing else changes. The MCP `search` tool is unchanged. New: `SearchRequest.explain`, `SearchResponse.explanation`, `RouteOutcome.explanation`, `router._route_event()`, `wiki/Explanation-Chains.md`. 10 new tests, including event-order-within-a-thread, attribution-by-field for concurrent sub-queries, and the partial-chain-on-failure path.

### Changed
- Version bumped to 3.53.0. Test suite: 1429 passing (from 1401 at v3.52.0), ruff clean. `wiki/Caching.md`, `wiki/Configuration-Reference.md`, `wiki/Roadmap.md` (new "Post-Audit Optimization & Observability" era section), and the README (env table, `/search` and `/cache/semantic` API docs, test count) all updated.

---

## [3.52.0]

### Fixed — The Kiwix "Wikipedia Bonus" Never Actually Existed

A second full-codebase review pass (external, function-by-function, against the v3.51.1 tarball) found one confirmed scoring bug: `_score_result()`'s docstring has always documented a standalone Wikipedia bonus (+8 definitional / +3 otherwise) plus a −10 list-article penalty — but in the code, the +8/+3 was nested *inside* the list-penalty branch with no `"wikipedia" in book` condition at all. Two confirmed consequences: (1) Wikipedia results never received any bonus — on an identical-title definitional query, the only differentiator was the +2 primary-book bonus, so whenever the LLM picked a non-Wikipedia primary book, a Stack Exchange result genuinely beat the Wikipedia one (measured 32 vs 30); (2) list articles in *any* book had their −10 penalty silently softened to −2/−7 — which the "nets −2 or −7 after its own partial offset" comment in `search()`, and the wiki's own Kiwix Scoring page, had each rationalized as intentional. The two pre-existing Wikipedia-bonus tests passed the entire time, but only via the primary-book bonus (both used Wikipedia as primary). Fixed to match the documentation that was right all along; two new regression tests pin each half in a way that fails against the old code. `wiki/Kiwix-Scoring.md` corrected — the earlier version of that page had the story exactly backwards.

### Fixed — `/backup` Silently Lost Un-Checkpointed WAL Writes

Every Mnemolis database runs in WAL mode, so recently committed writes live in the `-wal` sidecar until checkpoint — and `/backup` tarred only the bare `.db` file. Backups were missing every un-checkpointed write, and a copy taken mid-commit could be internally inconsistent. Each `.db` is now snapshotted via SQLite's online backup API (`Connection.backup()` — complete, consistent, safe under concurrent writers) into a temp file before tarring, with a logged raw-copy fallback for a genuinely corrupted database. The new regression test constructs the exact hazard (WAL mode, autocheckpoint off, writer connection held open across the endpoint call) and fails against the old behavior with `no such table`.

### Fixed — `detect_conditional()`'s Remainder Split Had No Proper-Noun-Pair Guard

`_decompose()` has always refused to split at a conjunction joining a bare proper-noun pair, but the conditional remainder split never applied the same guard: "if the front door is unlocked, tell me the latest on Iran and Israel" cleaved "Israel" off as a supposedly separate intent and mangled the consequence to "tell me the latest on Iran". Now judged per-occurrence via the same `_is_proper_noun_pair_at()` helper — a protected pair early in the consequence doesn't block a genuine separate intent behind a later conjunction. 2 new tests.

### Changed — `/search` No Longer Pays a Wasted LLM Call, and Reports What Actually Happened

`/search` previously called `detect_intent()` on the full query *before* routing, purely to compute the `cached` flag — a full, wasted cold LLM routing call for any query the router decomposes or treats as conditional (those paths never route the full query), plus a `cached` flag computed against a cache key those paths never use. Fallback detection was likewise inferred after the fact by comparing pre-computed intent against the resolved source — structurally blind to fallbacks inside decomposed sub-queries, as its own comment admitted.

Replaced with `route_query()`, a new entry point that installs a per-request ContextVar stats channel (`_ROUTE_STATS` — the same pattern as `suppress_cache_writes()`) around `route_with_source()`. Cache hits, real handler/fusion invocations, fallbacks, and the intended source are each recorded at the exact code point where they genuinely happen, from any recursion depth or worker thread (every recorded value is an idempotent boolean set or plain overwrite, never a counter, so concurrent worker writes can't race into a wrong value). `route_with_source()`'s own recursion-laden 2-tuple signature — the original, still-valid reason the inference approach was chosen — stays completely unchanged; callers that never install the channel see zero behavior change. `cached` is now true only when the response was served entirely from cache; `fallback_occurred` now counts decomposed-sub-query fallbacks (they contribute to `fallback_count` but not the per-target breakdown, which matches rows by `source_used`); failures report the source the query was headed to when it broke, at whatever recursion depth. 13 new tests across router and main. `wiki/The-Fallback-Observability-Gap.md` gained a postscript; `wiki/Health-and-Observability.md` updated.

### Changed — Decomposed Sub-Queries Resolve Concurrently

The decomposition loop routed sub-queries strictly sequentially — a 3-intent cold compound query paid three stacked LLM-routing + source-fetch costs back to back, the largest remaining sequential-work latency cost after the searxng and conditional condition/remainder parallelizations of the same shape. Sub-queries (independent by construction) now dispatch onto a fresh, per-call executor bounded by `min(len(sub_queries), DECOMPOSE_MAX_PARALLEL)` (new setting, default 4) with `contextvars.copy_context()` per task, futures collected strictly in submission order so response sections follow ask order, never completion order. Per-call rather than a shared pool is deliberate: these workers recurse into `route_with_source()`, which can reach this same code path again, and a shared pool submitting into itself and blocking is a real deadlock under saturation — a fresh pool per nesting level cannot deadlock, and decomposed queries are rare enough that per-call construction isn't fusion's unbounded-thread-pressure pattern (the same judgment already made for `_resolve_conditional()`'s per-call executor). The loop body was extracted to `_resolve_decomposed_sub_query()`. 4 new tests, including a timing test confirmed to fail against the sequential implementation, an ask-order test with the slow sub-query deliberately first, and a `suppress_cache_writes()` propagation test.

### Changed — Routing Cache Writes Are Now Batched, Both Caches Flush on Clean Shutdown

`_set_routing()` called `_save_routing_cache()` on every single write — a full JSON dump of up to 1000 entries per routing decision, from four separate writers, clustering exactly during cold bursts, on request-serving threads. Now batched every 5 writes (`_ROUTING_CACHE_SAVE_INTERVAL`), the identical batching the result cache always had. To cover the durability tradeoff, new `router.flush_caches()` runs in the lifespan shutdown: a clean `docker stop` persists everything, so only a hard crash can lose the up-to-4 unflushed entries — each just a re-derivable LLM decision. `_atomic_write_json()` also now fsyncs before the atomic rename, closing the crash-durability gap (a power cut shortly after rename could previously leave a zero-length file on filesystems that commit the rename before the data blocks) — affordable now precisely because saves are batched. `wiki/Caching.md` corrected: it had described the per-write asymmetry as intentional.

### Changed — FreshRSS Token Caching With 401 Self-Healing

Every news query paid a full ClientLogin round trip despite GReader tokens being long-lived by design — the same class of per-call-connection-setup cost already fixed twice (uptime_kuma's Socket.IO login, llm.py's TCP connections), one layer up the stack. The token is now cached module-level; a 401 from the articles request triggers exactly one re-auth and retry, so a stale token (API password change, FreshRSS reinstall) costs one extra request, never a wrong answer, never a retry loop. 8 new tests including the single-retry bound and failed-refresh-clears-stale-token.

### Changed — Persistent HTTP Sessions for Every Source Module

`searxng`, `kiwix`, `home_assistant`, `freshrss`, and `forecast` all used bare `requests.get()`/`post()` — a fresh TCP setup and teardown per call with zero reuse, the identical pattern `llm.py` fixed for its own cross-machine backend. Each module now has a module-level `requests.Session` (never mutated after creation — the same thread-safety analysis `llm.py` documents). Kiwix benefits most (catalog fetch + per-book searches + article fetch per query); Open-Meteo is the one external-internet dependency where skipping the TLS handshake matters most. `snapshot_ha()` also now reuses `home_assistant._get_states()` instead of carrying its own line-for-line duplicate of the same fetch — one less drift-prone copy, and the snapshot job rides the same session.

### Fixed — SQLite Connections Leaked on Exception Paths

~25 call sites across `main.py`, `router.py`, `snapshots.py`, `adversarial_testing.py`, and `temporal_patterns.py` closed their connection only on the success path — any mid-function sqlite error was caught and handled, but the just-opened connection lingered until GC. Short blocks now use `contextlib.closing()`; the handful of long-bodied functions use `con = None` + `finally`. `get_snapshot_job_health()` additionally consolidated four connections-plus-queries (one per source, per `/health` call) into one `GROUP BY` query — a case where SQLite's bare-column guarantee genuinely applies (single `MAX()` aggregate), the inverse of the `query_log_stats()` case where it didn't. `uptime_kuma.search()`'s error path similarly discarded a possibly half-alive connection with a bare `_persistent_api = None`; it now routes through `disconnect()` so the transport closes at the moment the object is discarded. 1 new test.

### Changed — Micro-Optimizations and Cleanups

- `_looks_empty()`'s 20-phrase list and kiwix's definitional-pattern list hoisted to module-level tuples (both rebuilt per call on hot paths); `_COLLOQUIAL_PHRASES` hoisted out of `_decompose()`'s body.
- `_is_definitional_query()`'s redundant `q.startswith(p) or p in q` reduced to `p in q` (a prefix match is by definition a substring match).
- Kiwix `search()` now decorate-sorts once — `_score_result()` was recomputed for the same result up to three times (sort key, `top`, per-book fusion loop); pure function, so identical outcome, strictly less work.
- `_interpret_binary_state()`'s `callable` annotations corrected to `typing.Callable[[str], bool]`.
- Redundant local `import re` removed from `_resolve_changes_hours()` (already imported at module top).

### Changed
- Version bumped to 3.52.0. Test suite: 1401 passing (from 1376 at v3.51.1), ruff clean. README test count updated; `wiki/Query-Decomposition.md`, `wiki/Conditional-Query-Detection.md`, `wiki/Sources.md`, `wiki/Backup-and-Restore.md`, and `wiki/Configuration-Reference.md` updated for the new behaviors and the `DECOMPOSE_MAX_PARALLEL` setting.

---

## [3.51.1]

### Fixed — MCP `search` Tool Now Correctly Sets `isError=True` on Genuine Failures

The `search` tool's broad `try/except` caught all exceptions and returned `"Error: {e}"` as a plain string, so `isError` was always `False` regardless of what actually went wrong underneath. A real internal failure (a genuine bug in routing or source logic) was indistinguishable from a normal "source not configured" response at the MCP protocol level — both looked identical to a client checking `isError`.

The underlying SDK already handled this correctly one layer up: an uncaught exception inside a tool function propagates to the low-level server's `CallToolRequest` handler, which calls `_make_error_result()` and returns `CallToolResult(isError=True, ...)`. Mnemolis's own `try/except` was simply catching everything first, preventing that path from ever being reached.

The fix is removing the `try/except` entirely. `route()` already returns descriptive strings for all expected, recoverable failures (source not configured, empty results, unknown source name) rather than raising — those come back as normal successful responses as before. A genuine unexpected exception now propagates to the SDK and surfaces as `isError=True`, giving real MCP clients a structured way to distinguish the two cases. Confirmed directly: `asyncio.to_thread` propagates exceptions correctly, and the SDK's handler converts them to `isError=True` as documented.

Updated `wiki/MCP-Server.md` to reflect the new contract. Updated `wiki/Roadmap.md` to mark this item complete and add the full audit pass section.

### Changed
- Version bumped to 3.51.1

---

## [3.51.0]

### Systematic Function-by-Function Audit Complete — 14 Real Bugs Found and Fixed

This release closes a sustained, multi-session audit that read every function in every file in `app/` — smallest to largest — and verified each one directly rather than trusting its own documentation. The complete account is in `wiki/The-Full-Audit-Pass.md`. Summary of what was found:

**`query_expansion.py` (83 lines):** No bugs. 2-length-rejection boundary tests added.

**`forecast.py` (108 lines):** Malformed partial API responses (HTTP 200, fewer than 3 days of data) raised uncaught `IndexError`/`KeyError` past the existing `try/except`. Fixed by wrapping data-consumption in a second `try/except`. 2 new tests.

**`timeutil.py` (153 lines):** `local_hour_bucket()` docstring claimed wrong range formula. 1 new regression test.

**`mcp_server.py` (160 lines):** Two independent `"/mcp"` string literals with nothing enforcing agreement. Consolidated into `MCP_MOUNT_PATH` constant. 2 new tests.

**`freshrss.py` (187 lines):** HTML stripping stopped at first `>` in attribute values, leaking `">` into article summaries. HTML entities never decoded. Fixed with BeautifulSoup. 6 new tests.

**`searxng.py` (194 lines):** New `ThreadPoolExecutor` created on every call (46 OS threads at peak). Early timeout return inside `with ThreadPoolExecutor` held caller for 0.50s past timeout. Both fixed with shared module-level executor. 5 new tests.

**`llm.py` (194 lines):** Dict-shaped `reasoning` field (a different OpenAI-proper convention) would crash `.splitlines()`. Defensive `isinstance` guard added. Self-introduced lint regression (`MCP_MOUNT_PATH` placed mid-import-block) found and fixed. 2 new tests.

**`scoring.py` (229 lines):** Possessive forms (`"Apple's"`) stemmed to `"apple'"` instead of `"apple"` — 20-point scoring gap confirmed by direct measurement. Generic-title detection applied `startswith()` uniformly to single-word patterns like `"home"` and `"error"`, penalizing real news articles. Both fixed. 7 new tests.

**`uptime_kuma.py` (229 lines):** Unknown status codes beyond 0–3 dropped monitors silently, producing `"All 0 monitored services are up"`. `get_connection()` called from startup without `_connection_lock` (contract violation). Both fixed. `warm_connection()` added. 2 new tests.

**`config.py` (467 lines):** No code bugs. `KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT` naming vs value clarified in wiki.

**`home_assistant.py` (627 lines):** No bugs.

**`fusion.py` (668 lines):** No bugs. Deepseek's two reported vulnerabilities verified as false (wrong premises confirmed by tracing actual source HTTP timeouts).

**`snapshots.py` (696 lines):** `seen_changes` set in `get_changes()` suppressed legitimate repeated HA and news events — a door opening, closing, then opening again only reported the first two transitions. Removed entirely. 2 new tests confirming second occurrence is reported.

**`temporal_patterns.py` (870 lines):** No bugs. Statistical functions verified against known Poisson values.

**`kiwix.py` (957 lines):** No bugs. Dead defensive guard documented with explanatory comment explaining why it's kept despite being currently unreachable.

**`adversarial_testing.py` (1120 lines):** No bugs.

**`main.py` (1179 lines):** TTFK SQL used `MIN(latency_ms)` and `MIN(id)` as independent aggregates in the same `GROUP BY` — reporting the fastest cold run rather than the genuine first cold hit. Fixed with a join back to the row with `min(id)`. 1 new test.

**`router.py` (2482 lines):** No bugs. Every function verified: routing cache, singleflight lock mechanism, cache suppression thread-locality, discourse-framing escalation paths, conditional detection, decomposition, fallback chain, all SQL and serialization paths.

### Changed
- Version bumped to 3.51.0 — the first release in which every function in every `app/` file has been read and verified directly

---

## [3.50.30]

### Fixed — Three Real Bugs Found via Deliberate Function-by-Function Reads of `snapshots.py` and `main.py`

**`snapshots.py` — `get_changes()` silently dropped legitimate repeated HA and news events.**
The event-based path in `get_changes()` used a `seen_changes` set to deduplicate change descriptions across consecutive snapshot pairs. The intent was to prevent the same change from being reported twice — correct for UPTIME and FORECAST, which would otherwise produce the same "outage" message in every pair while a service stayed down. But UPTIME and FORECAST never reach the event-based path: they use the `NET_CHANGE_SOURCES` path above it. The event-based path only serves `ha` and `news`, and for both of those, the underlying diff functions (`_diff_ha`, `_diff_news`) already only fire on genuine state transitions — the same change text in consecutive pairs means the event literally happened twice. `seen_changes` was therefore silently suppressing real, separate occurrences: a door that opened, closed, then opened again within the query window would only report the first two transitions, leaving the final open state completely invisible in the summary. Confirmed directly: the third "Front Door opened" event was dropped even though it corresponded to a distinct, real state change. Fixed by removing `seen_changes` entirely.

**`main.py` — TTFK (Time To First Knowledge) SQL reported minimum latency instead of first-occurrence latency.**
`query_log_stats()`'s TTFK calculation used `MIN(id)` and `MIN(latency_ms)` as two independent aggregates in the same `GROUP BY`. These are computed independently: `MIN(id)` correctly identifies the first occurrence's row, but `MIN(latency_ms)` picks the *smallest latency across all non-cached occurrences for that query* — not the latency of the row whose id is `MIN(id)`. For a query asked repeatedly without caching (e.g. adversarial test queries), this would report the fastest cold run rather than the genuine first cold hit, consistently under-estimating true TTFK. Confirmed directly: a query with first occurrence at 3000ms and second at 200ms reported TTFK of 200ms. Fixed with a correlated subquery that joins back to the row with `min(id)` to read its actual latency.

**`kiwix.py` — Dead defensive guard comment added (not a code bug).**
The `if not selected_books` guard in `search()` is currently unreachable with the present code structure (the inline fallback path always produces a non-empty list when `books` is non-empty). Kept deliberately — it guards against a real contract violation by `_pick_books_with_llm` that a future refactor could expose. Added a comment explaining why the guard exists despite being currently unreachable, matching this project's existing pattern for similar defensive branches.

### Added (Tests)
- 2 new tests in `test_snapshots.py` (`TestGetChangesEventDedup`): repeated door-open and repeated motion events are both correctly reported after the `seen_changes` removal; tests call through the real SQLite layer to confirm end-to-end
- 1 new test in `test_main.py` (`TestLogsStatsEndpoint::test_ttfk_uses_first_occurrence_latency_not_minimum`): confirms TTFK reports the first-occurrence latency (3000ms), not the minimum across all cold hits (200ms); confirmed it fails against the reverted bug

### Changed
- Version bumped to 3.50.30

---

## [3.50.29]

### Fixed — Four Real Bugs Found via Deliberate Function-by-Function Reads of `scoring.py` and `uptime_kuma.py`

**`scoring.py` — Possessive forms silently scored worse than their base forms.**
`_keywords()` uses `str.strip()` to remove punctuation from token edges, but `str.strip()` only removes characters from the *ends* of a string. The apostrophe in `"Apple's"` is interior to the token — between the word and the trailing `s` — so `"Apple's".strip("...\"'...")` returns `"Apple's"` unchanged. After stemming, `"apple'"` does not equal `"apple"`, so a query for `"Apple profit"` would miss the keyword match on a title reading `"Apple's profit rose"`, losing the full title-keyword-match bonus. Confirmed real and measured: a 20-point scoring difference between identical articles where only the possessive form of one word changed, determined by direct scoring comparison. Fixed by normalizing `'s` and trailing apostrophes before the existing strip/stem pipeline. Verified safe for contractions (`"don't"`, `"won't"` — end in `"t"`, not `"'s?"`) and programming language names (`"c++"`, `"c#"` — unchanged).

**`scoring.py` — Generic-title detection applied `startswith()` uniformly to single-word patterns, causing false positives on real news articles.**
`_is_generic_result()` checked `title_lower == p or title_lower.startswith(p)` for every pattern uniformly. For single-word patterns like `"home"`, `"error"`, and `"404"`, this meant legitimate articles titled `"Home prices rise 5% in October"`, `"Error in climate data causes alarm"`, and `"404 error handling best practices"` were incorrectly flagged as generic site pages and penalized 20 points — while multi-word phrases like `"welcome to"` and `"about us"` correctly use `startswith()` since any title opening with those phrases is always a site page. Fixed by splitting the pattern list: single-word patterns use exact-match only; multi-word phrases keep `startswith()`. The existing `"404 - Page Not Found"` test briefly failed during the fix and required a targeted substring check for that particular form, which lives outside both buckets.

**`uptime_kuma.py` — Unknown status codes dropped monitors silently from all output.**
`search()`'s status-dispatch loop had no `else` branch for status codes beyond the known `0` (DOWN), `1` (UP), `2` (PENDING), `3` (MAINTENANCE). Any monitor returning an unrecognized status fell through all four `elif` branches, disappearing from every output bucket entirely. With `up_count=0` and no monitor in any named list, the output was `"All 0 monitored services are up"` — an actively wrong statement for a deployment that has monitors. Not a realistic concern with the current `uptime-kuma-api` (MonitorStatus values 0-3 have been stable), but silently lying about a monitor that exists is strictly worse than reporting it honestly. Fixed by adding an `else` branch that logs a warning and treats the monitor as `no_data`, following the same pattern already used for genuinely data-absent monitors.

**`uptime_kuma.py` — `get_connection()` called from `main.py` startup without holding `_connection_lock`.**
`get_connection()`'s own docstring explicitly states "Callers must hold `_connection_lock`." `main.py`'s lifespan warm-up called it bare via `uptime_kuma.get_connection` without the lock. No actual race is possible at that specific point (the scheduler hasn't started and the app isn't yet accepting requests when this runs), so this was never a live correctness failure — but the contract violation is real, and a future startup-sequence change could expose the latent race. Fixed by adding `warm_connection()` — a purpose-built public function that correctly acquires `_connection_lock`, handles exceptions gracefully (logging a warning rather than crashing startup), and includes its own config guard — replacing the bare `get_connection()` call in `main.py`.

### Changed (Documentation)
- `Confidence-Aware-Fusion.md` — expanded the generic-result penalty section to explain the exact/prefix distinction and the possessive-normalization fix; both previously undocumented
- `Multi-Book-Fusion.md` — clarified that `KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT` takes a fraction (0–1), not a percentage (0–100), as the `_PCT` suffix could mislead

### Added (Tests)
- 7 new tests in `test_scoring.py`: 4 for possessive normalization (singular possessive, plural possessive, contractions-are-not-mangled, and end-to-end scoring parity), 3 for generic-title false-positive regression (home-prices article, error-in-data article, 404-error-handling article)
- 1 new test in `test_uptime_kuma.py`: unknown status code must not claim all services are up; the named monitor must appear in output

### Changed
- Version bumped to 3.50.29

---

## [3.50.28]

### Fixed — A Real Lint Regression, Self-Introduced and Caught by CI, Not by This Project's Own Verification Routine
`app/main.py` failed `ruff check` with six `E402` (module-level import not at top of file) errors — a real, embarrassing mistake introduced during this session's earlier `mcp_server.py` work: the new `MCP_MOUNT_PATH` constant and its explanatory comment were inserted in the middle of the existing import block instead of after it, pushing every subsequent import (`app.sources.kiwix`, `app.snapshots`, `app.adversarial_testing`, `app.temporal_patterns`, `app.config`) below a non-import statement. This shipped across several versions because the verification routine for this whole session only ever ran `pytest` — which has no opinion about import ordering — never `ruff`, the actual tool this project's own CI pipeline runs. Fixed by moving the constant and its comment back to after the import block, a pure reordering with zero behavior change, confirmed by an unchanged full test suite pass before and after. `ruff check .` now reports zero errors across the entire repository. **Running both `pytest` and `ruff check` together is now the standing verification step for every change going forward, not just `pytest` alone.**

### Fixed — A Real, Defensive Gap Found During a Deliberate Function-by-Function Read of `app/llm.py`
Every concurrency and API-shape claim in this file's own dense, pre-existing comments was verified directly rather than trusted — `requests.Session`'s real thread-safety boundary (confirmed against the library maintainers' own GitHub discussion: safe for concurrent reads, unsafe only under concurrent mutation of shared session state, which this module never does after construction), and the `"think": False` top-level-vs-nested placement (confirmed against multiple real Ollama bug reports — the bug exists, but is scoped to the newer qwen3.5/qwen3-vl renderer architecture, not this project's actually-documented `qwen3:8b`, which uses the older, unaffected template-based thinking control).

One real, defensive gap found: `_complete_openai()`'s thinking-model fallback reads `message.get("reasoning_content", "") or message.get("reasoning", "")` and calls `.splitlines()` on the result, assuming a plain string. A different, OpenAI-proper convention exists where `reasoning` is itself a dict (e.g. `{"effort": "none"}`) rather than the plain-string shape llama.cpp's own real, documented response format actually uses for this fallback. Confirmed via direct testing that this would raise an uncaught `AttributeError` — not reachable through this project's own documented backend, and already caught one layer up by `complete()`'s own outer exception handler, so this was never a crash risk in practice, just a less-specific log message than necessary. Fixed with a cheap `isinstance` guard.

### Added (Tests)
- 1 new test in `test_llm.py`'s `TestCompleteOpenAI`, calling `_complete_openai()` directly rather than the public `complete()` wrapper — confirmed a first draft of this test was flawed (it passed regardless of whether the fix existed, since the outer exception handler already masks the difference) and rewrote it to check the inner function's own behavior directly, the only way to confirm this specific guard does real work

### Changed
- Version bumped to 3.50.28

---

## [3.50.27]

### Fixed — Two Real Bugs From the Same Root Cause: Unbounded Per-Call Thread Creation, and a Timeout That Didn't Actually Return Promptly
A deliberate, full function-by-function read of `app/sources/searxng.py`, the next file in size order. Every existing concurrency claim in this file's own extensive comments was verified directly against real Python semantics before trusting it — the `ThreadPoolExecutor`-doesn't-propagate-`ContextVar`s claim, and the single-`Context`-can't-be-entered-by-two-threads claim, both reproduced exactly as described. Then a deliberate cross-check against `fusion.py`'s own already-documented "remaining unfixed `ThreadPoolExecutor` site" comment (written during this session's earlier `fusion.py` work) led directly to this file.

**Bug one: `search()` spun up a brand-new `ThreadPoolExecutor(max_workers=2)` on every single call** — the identical unbounded-per-call pattern already found and fixed in `fusion.py` earlier this session, just never applied here. Confirmed directly, not estimated: 15 concurrent `search()` calls under realistic network latency produced a measured peak of 46 real OS threads, with no ceiling as concurrent traffic increases. Genuinely worse than the original `fusion.py` case in one real way: `fusion.py` dispatches to `searxng.search()` *through* its own already-bounded `_fusion_executor` pool whenever a fusion query includes `web` — meaning every one of fusion's bounded workers could simultaneously spin up its own additional, unbounded pool here, undermining the very bound `fusion.py`'s own fix exists to establish.

**Bug two, found while fixing the first:** the primary-fetch timeout's early `return` — from inside the now-removed `with ThreadPoolExecutor(...) as executor:` block — didn't actually reach the caller as promptly as the surrounding comment claimed. `return` from inside a `with` block doesn't propagate until `__exit__`'s implicit `shutdown(wait=True)` completes, so a fast, already-decided timeout error still silently waited for the unrelated, slower alternate-phrasing thread to finish first. Confirmed by measurement: reverting to the original per-call structure and re-running the new test reproduced exactly 0.50s of silent waiting (matching a deliberately slow mocked alternate fetch) before the fix, versus near-instant return after it.

Fixed both at once by replacing the per-call executor with a single, shared, module-level `_searxng_executor` — the identical shape `fusion.py`'s own `_fusion_executor` already established, with a new `SEARXNG_THREAD_POOL_SIZE` setting (default 4). The shared pool resolves the latency bug as a side effect: there's no per-call executor left to wait for on the way out, so an abandoned alternate-phrasing straggler simply keeps running in the shared pool and is discarded once it finishes.

### Added (Tests)
- 2 new tests in `test_searxng.py`'s `TestSearxngSharedExecutor`, mirroring `test_fusion.py`'s own `TestFusionSharedExecutor` precedent: confirms concurrent `search()` calls reuse the shared pool rather than creating unbounded threads, and confirms the pool is genuinely sized from the new setting
- 1 new timing-based regression test confirming the primary-timeout path now returns promptly even while the alternate-phrasing thread is still running — confirmed it fails against the reverted, pre-fix code with an exact, measured 0.50s delay before restoring the fix
- 2 new config tests: the new setting's default value, and adding it to the existing env-var isolation list so future "bare defaults" tests aren't affected by a real environment's value

### Changed
- Version bumped to 3.50.27

---

## [3.50.26]

### Fixed — A Real Bug: Naive Regex HTML Stripping Could Leak Malformed Tag Remnants Into Article Summaries
A deliberate, full function-by-function read of `app/sources/freshrss.py`, the next-smallest remaining file. `search()`'s article-summary cleanup used `re.sub(r"<[^>]+>", "", ...)` to strip HTML from FreshRSS article content — a naive pattern with two real, distinct gaps a genuine parser doesn't have.

First, HTML entities (`&amp;`, `&lt;`, `&gt;`) were never decoded, surviving as literal text in the output — cosmetic, not a parsing failure, but still wrong. Second, and more seriously: `[^>]+` stops at the *first* `>` character it finds, with no awareness of quoted attribute values. A real, plausible tag like `<img alt="a description with a > in it">` truncates the match at the `>` inside the quoted attribute, leaving the genuine tag boundary completely unmatched — confirmed directly with a constructed repro, reproducing a real, mangled `'in it">After image text'` output where the regex leaves visible HTML syntax bleeding into the actual summary text. Researched against real FreshRSS API behavior and production bug reports (not just internal code reading) to confirm the rest of the module's HTTP/auth handling — `_get_token()`'s line-scanning, the real `Auth=` response format, a real documented FreshRSS misconfiguration that returns HTTP 200 with no token at all — were all already correct against the genuine, real-world API shape.

Fixed by switching to BeautifulSoup's `html.parser`, already a project dependency used identically elsewhere (`kiwix.py`'s own `get_text()` calls) — a real parser understands attribute-value boundaries and decodes entities as a normal side effect of parsing, fixing both gaps with the same change rather than patching the regex twice. The now-unused `re` import was removed.

`search()` itself had zero direct test coverage of any kind before this pass, despite being the module's actual entry point — confirmed by checking the test file directly, not assumed.

### Added (Tests)
- A new `TestSearch` class in `test_freshrss.py` covering `search()` directly for the first time: the not-configured and auth-failure messages, the general-query unfiltered-feed path, the empty-items message, and two regression tests for this fix specifically (entity decoding, and the quoted-attribute `>` case) — both confirmed to fail against the original regex before restoring the fix
- A regression test for a real, documented FreshRSS-side misconfiguration found via web research (a `ClientLogin` response of plain `"OK"` with no `Auth=` token) — confirmed the existing code already handled this correctly without needing a change, and locked that in

### Changed
- Version bumped to 3.50.26

---

## [3.50.25]

### Fixed — A Real Drift Risk: Two Independent "/mcp" String Literals With Nothing Enforcing They Agreed
A deliberate, thorough read of `app/mcp_server.py` and its real integration point in `main.py`'s lifespan function — verified line by line against the actual installed SDK source (`mcp==1.27.2`), not just trusting the existing docstrings' own claims about what the SDK does. Every existing claim checked out precisely: the session-manager-once-only constraint, the `TransportSecuritySettings` auto-enable trigger condition (only fires when `transport_security is None` *and* `host` is `127.0.0.1`/`localhost`/`::1` — Mnemolis's explicit override bypasses this entirely), and the exact mechanism behind the doubled-path bug from the original migration.

One real, previously-unflagged risk found: `main.py`'s lifespan function matched against a bare `"/mcp"` string literal to find and refresh the real Mount route — completely independent of the `"/mcp"` literal in the actual `app.mount("/mcp", mcp_app)` call elsewhere in the same file. Nothing enforced the two ever agreed. If a future edit changed one without the other, the lifespan's matching loop would silently find no route, leave the stale module-import-time `mcp_app` mounted, and reintroduce the exact "session manager can only be entered once" `RuntimeError` the surrounding fix exists to prevent — not at startup, where it would be immediately obvious, but on the first real MCP request after the next restart.

Fixed by introducing `MCP_MOUNT_PATH`, a single constant used at both real call sites, and adding a defensive warning (mirroring this project's existing `ADVERSARIAL_TEST_ENABLED`-style defense-in-depth pattern) for the case where no matching route is ever found — loud at startup is strictly better than silent failure on the next real request.

### Changed — A Real Inaccuracy Corrected in the Wiki's MCP Error-Handling Claim
[MCP Server](https://github.com/immortalbob/Mnemolis/wiki/MCP-Server)'s existing claim that MCP errors "don't get an MCP protocol-level error" was traced precisely against the SDK and found to be incomplete: the mechanism genuinely exists and works — an uncaught exception inside a tool function gets wrapped as `ToolError` by `Tool.run()`, then converted to a real `CallToolResult(isError=True, ...)` by the low-level server's own dispatcher. Mnemolis's `search` tool specifically avoids this path by catching everything itself first, the same convention every source file in this project already uses for expected, recoverable failures — a deliberate choice, not evidence the mechanism doesn't exist. Corrected to describe the real mechanism precisely, and added a tracked, deliberately-undecided question to the Roadmap: should a genuinely unexpected internal error be allowed to surface as `isError=True`, giving a real MCP client a structured way to distinguish "Mnemolis answered with bad news" from "the call itself broke"? Not changed — a real contract change for any existing integration, not a bug fix.

### Added (Tests)
- 2 new tests in `test_main.py`'s `TestLifespanMountRefresh`: confirms the real app startup finds and genuinely refreshes the real mount (checking the route object directly, not just inferring success from an unrelated endpoint), and confirms the defensive warning fires — and the rest of the app still starts cleanly — when the mount path is patched to something that matches nothing. Confirmed the second test fails against the pre-fix code (0 warning calls) before restoring the fix.

### Changed
- Version bumped to 3.50.25

---

## [3.50.24]

### Fixed — A Real Documentation Bug: `local_hour_bucket()`'s Claimed Return Range Was Wrong for Most Bucket Widths
A deliberate, full function-by-function read of `app/timeutil.py` — continuing past last round's already-confirmed `_resolve_zone()`/`utc_string_to_local()` correctness, into the two functions built on top of them. `local_hour_bucket()`'s own docstring claimed its return value was bounded by `(1440 // bucket_minutes) - 1` — true only when `bucket_minutes` evenly divides 1440 (which the documented default of 30 happens to do, which is exactly why this went unnoticed). For any value that doesn't — 7, 13, 17, 25, 50, 100, and most arbitrary widths — confirmed directly across a range of them that the real last-minute-of-day (23:59) lands in a bucket index exactly one past the old claimed maximum.

The function's own arithmetic was never wrong — `minutes_since_midnight // bucket_minutes` always returns a correct, real bucket index for any input — only the documented range claim was. This module has zero live consumers yet (built ahead of two not-yet-built design docs, per its own module docstring), so there's no current code relying on the wrong bound. But a future caller sizing a fixed-length array or list from the old formula, trusting the docstring rather than re-deriving the math, would have silently allocated one bucket too few. Fixed by correcting the docstring to the real general formula (`ceil(1440 / bucket_minutes)` distinct buckets) and explaining precisely why the clean-divisor default masked the gap.

Also reviewed and confirmed correct: `local_day_of_week()`'s calendar-day-shift handling in both directions (a timezone west of UTC rolling the local date backward, one east of UTC rolling it forward — the existing test suite already covered the first; the second was checked directly and found to rely on the same already-correct `zoneinfo`/`astimezone()` mechanism, so no new test was added for it).

### Added (Tests)
- 1 new test in `test_timeutil.py`'s `TestLocalHourBucket`: confirms the function's actual, correct behavior at a non-divisor `bucket_minutes` value (7), independent of whatever the docstring claims, so a future change to the arithmetic itself would be caught here regardless of documentation drift

### Changed
- Version bumped to 3.50.24

---

## [3.50.23]

### Fixed — A Real Gap: Malformed or Partial Forecast Data Crashed Uncaught Past the Existing Error Handling
A deliberate, full function-by-function read of `app/sources/forecast.py` — the next-smallest file after `query_expansion.py` — found that the existing `try/except` around the Open-Meteo API call only ever covered the network request and JSON parse. Every line that actually *consumes* the parsed `daily` dict (direct list indexing for all three forecast days — `daily['weathercode'][0]`, `daily['time'][2]`, and so on) sat entirely outside that block.

A genuinely successful HTTP 200 response with fewer than 3 days of data, or missing an expected field, is a real, reachable case — `forecast_days=3` is a request parameter sent to Open-Meteo, not a guarantee about what comes back, and a transient API degradation or schema change could plausibly produce a partial response without ever failing the HTTP layer. Before this fix, that case raised an uncaught `IndexError`/`KeyError` that propagated all the way to `main.py`'s `/search` endpoint — which does catch it, so this was never a hard crash or a hung request — but it surfaced as a raw Python exception string in the response instead of the same honest, specific "Unable to retrieve forecast" message every other real failure in this file already produces.

Fixed by wrapping the data-consumption logic in its own `try/except`, catching the specific exception types this code can actually raise (`KeyError`, `IndexError`, `TypeError`, `ValueError`) and returning the same message shape the network-failure path already uses. Confirmed real: reverting to the original structure and re-running the new tests reproduced the exact uncaught `KeyError` directly, before restoring the fix.

Also reviewed and confirmed correct rather than buggy: `_degrees_to_cardinal()`'s wind-direction sector boundaries (checked at every transition point, including the 0°/360° wraparound), `_describe()`'s handling of weather code `0` (clear sky — confirmed `dict.get()` does a real key lookup, not a truthiness check, so this never collides with the "unknown code" fallback), and `_fmt_time()`'s midnight formatting (Python's own `strftime("%I")` already handles the 12-hour wraparound correctly; not given its own test since sunrise/sunset values never realistically land on midnight in practice).

### Added (Tests)
- 2 new tests in `test_forecast.py`'s `TestForecastSearch`: a response with a short/partial `weathercode` array (3 days requested, 2 returned), and a response missing an entire expected field (`sunrise`) — both confirmed to fail with the original uncaught exception before the fix, and pass cleanly after it

### Changed
- Version bumped to 3.50.23

---

## [3.50.22]

### Verified — Confirmed Correct Rather Than Finding a Bug This Time
A deliberate, full function-by-function read of `app/snapshots.py`'s battery-threshold check and `app/query_expansion.py` end to end, specifically hunting for the same untested-exact-boundary shape that found v3.50.21's real zero-degree bug. Both held up — every boundary checked directly against real, constructed edge cases, not just confirmed by reading.

`_diff_ha()`'s battery-low threshold (`old_val >= threshold and new_val < threshold`) was checked at the literal boundary three ways: a battery sitting exactly at the threshold on both snapshots (correctly no change), one landing exactly on the threshold from above (correctly not yet "low," confirming the boundary is exclusive on the low side), and the genuine crossing case immediately adjacent — exactly at threshold dropping to one point below (correctly fires). All three behaved exactly as the existing, comfortably-away-from-the-boundary tests already implied, just never actually exercised at the tight margin before.

`query_expansion.py`'s length-sanity check (`len(alternate.split()) > word_count * 2`) was checked the same way: a rephrasing at exactly double the original's word count is correctly accepted (the wiki's own "more than twice... is discarded" framing implies exactly twice should pass, and it does), and one word past that boundary is correctly rejected. The existing overly-long-response test used 50 words against a 4-word query — nowhere near the actual boundary — so this specific edge had never been directly exercised either.

One real, minor, deliberately-not-fixed observation from the same read: the identical-response check (`alternate.lower().strip() == query.lower().strip()`) only normalizes leading/trailing whitespace, not internal — an LLM response differing from the original only by doubled internal spacing would not be caught and would cause a redundant, functionally-identical second search. Not fixed — this is wasted work, not incorrect output, and doesn't rise to the same bar as a real correctness gap.

### Added (Tests)
- 3 new tests in `test_snapshots.py`'s `TestDiffHA`: battery at exactly the threshold on both sides, landing exactly on the threshold from above, and the genuine crossing case at the tightest possible margin (threshold to one point below)
- 2 new tests in `test_query_expansion.py`'s `TestGetAlternatePhrasing`: a rephrasing at exactly twice the original word count (accepted), and one word past that boundary (rejected)

### Changed
- Version bumped to 3.50.22

---

## [3.50.21]

### Fixed — A Real, Live Bug Found by Going One Step Past an Already-Shipped Fix
`_diff_forecast()`'s temperature-change checks used `if old_high and new_high and ...` — a truthiness check, not an `is not None` check. `0.0` is exactly as falsy in Python as `None` is, meaning a forecast high or low of exactly zero degrees was silently indistinguishable from "couldn't extract a value at all," and a real, large temperature change involving a 0° day never registered in either direction. Confirmed directly: a high changing from 0° to 15° — a real 15-degree swing, well above any sane threshold — produced zero detected changes before this fix.

Found by checking whether the existing, already-shipped negative-temperature fix (the comment three lines above this exact code, explaining why sub-zero forecasts needed regex support) had a sibling gap one step further downstream — it did. The regex fix protected *extraction*; nobody had gone back to check whether *consumption* of the extracted value had the analogous problem for zero specifically. 0° is an entirely ordinary winter temperature for a real deployment somewhere genuinely cold — the same "Mnemolis is deployable anywhere" reasoning that motivated the original fix applies with equal force here. The existing test suite had thorough negative-number coverage (`test_detects_negative_temperature_change`, `test_negative_high_temperature_also_extracted_correctly`) but every single test case used non-zero values for both old and new — the zero case itself was never constructed or considered.

Fixed by changing both checks to `is not None` against the extracted values, rather than relying on truthiness.

### Added (Tests)
- 4 new tests in `test_snapshots.py`'s `TestDiffForecast`: a high changing from exactly 0°, a high changing to exactly 0°, a low changing from exactly 0°, and a no-false-positive control confirming two genuinely identical 0° readings still correctly report no change. Confirmed all three real-bug tests fail against the reverted, pre-fix code and pass against the fix, before considering the fix complete.

### Changed
- Version bumped to 3.50.21

---

## [3.50.20]

### Fixed — A Real, Two-File Garage-Door Gap, Found While Re-Checking Whether v3.50.19's "Intentional" Bitcoin Pairs Actually Were
Pushed back on v3.50.19's own claim that the two remaining `CONDITIONAL_WITH_REMAINDER_QUERIES` double-kiwix-hit pairs (`"the garage door is open"` / `"any cameras go offline"`, each paired with a `"whats happening with bitcoin"` remainder) were intentional kiwix routing on both sides. They weren't — only the bitcoin **remainder** half of each pair was ever a genuine, considered design choice. Checked the **condition** half of each pair directly and found two different things:

- **`"the garage door is open"` was a real, fixable, two-file gap.** `router.py`'s `INTENT_MAP["ha"]` had no trigger for open/closed garage-door phrasing at all — a genuinely different question from the existing locked/unlocked door triggers, since most garage doors have no lock entity of their own. Added `"garage door"`/`"garage"`. Routing alone wasn't the whole fix: even with correct routing, `home_assistant.py`'s own `_QUERY_MAP` had no entry that could find a real garage-door entity once inside the `ha` handler. Added a new `"garage door"`/`"garage"` entry covering **both** real Home Assistant naming conventions for this entity, confirmed via Home Assistant's own developer docs and a live, filed core issue (`home-assistant/core#91131`): a `cover` domain entity (the dedicated HA domain for openings) uses `device_class: "garage"`, while a plain `binary_sensor` reporting the same physical door uses `"garage_door"` instead — same real-world thing, two different device_class strings depending on integration shape, and there's no way to know from outside the deployment which one Mike's actual hardware uses, so both are covered. Also added a dedicated `"Garage Doors"` label (previously would have fallen through to a generic `"Cover"` label) and confirmed via direct, deliberate testing that this stays correctly separate from the existing, pinned `"are the doors locked"` lock-domain behavior in both directions — a garage-door query doesn't pull in unrelated locks, and a lock query doesn't pull in the garage door.
- **`"any cameras go offline"` is genuinely not fixable, and was correctly left alone.** Checked `home_assistant.py`'s existing `"camera"` `_QUERY_MAP` entry directly — it answers motion-*detection* questions (`domains: ["event"], device_classes: ["motion"]`), not camera reachability/connectivity. There is no real Home Assistant concept in this codebase that answers "is my camera reachable," and Uptime Kuma (the other structured-status source) monitors services/hosts, not individual HA entities. `kiwix` remains the least-wrong available destination for this one — a real capability gap, not a routing bug, and out of scope for a keyword-list fix.

`CONDITIONAL_WITH_REMAINDER_QUERIES`'s double-kiwix-hit count drops from 2/30 (v3.50.19's number) to 1/30 as a direct result — the one remaining hit is the genuine `"any cameras go offline"` case, not a gap.

### Tests
- `tests/test_router.py` — `TestKeywordDetect` extended with 3 new tests for the `"garage door"`/`"garage"` trigger, including a direct negative check that `"any cameras go offline"` correctly remains unmatched (confirming this fix is deliberately narrow); `test_conditional_with_remainder_pool_double_kiwix_hit_count_stays_low`'s threshold tightened from `<= 2` to `<= 1` and its docstring updated to explain why the camera case is excluded from the fix rather than just lowering the number
- `tests/test_home_assistant.py` — `TestBuildFilter` extended with 2 new tests confirming the new `"garage door"`/`"garage"` `_QUERY_MAP` entry resolves to the `cover` domain (not `lock`) and covers both real device_class naming conventions; new `TestGarageDoorSupport` class with 4 end-to-end `search()` tests covering both real entity shapes (`cover`/`garage` and `binary_sensor`/`garage_door`) and confirming bidirectional non-interference with existing lock-domain queries
- Version bumped to 3.50.20

---

## [3.50.19]

### Investigated, Not Fixed — `conditional_remainder`'s Warm-Cache-Worse-Than-Cold Benchmark Anomaly
A user-supplied cold/warm Locust benchmark pair showed `conditional_remainder`'s warm-run p98/p99/max (4400/4400/4410ms) worse than its own cold-run numbers (1200/1200/1200ms) — backwards from every other row in both tables and from this project's entire benchmark history. Root cause confirmed via the raw per-second `stats_history.csv` files rather than the aggregate percentile tables alone (which had earlier, incorrectly, pointed the investigation at `fusion_auto` instead — see the design doc's own Part 1 for the full retraction): `_resolve_conditional()`'s existing concurrent condition+remainder dispatch (`ThreadPoolExecutor(max_workers=2)`, added specifically so the two calls cost `max(a,b)` instead of `a+b`) runs into this deployment's own already-documented, already-accepted `OLLAMA_NUM_PARALLEL=1` constraint (see `wiki/The-Benchmark-Investigation-Log.md`, Thread 6) — except here it's a deterministic single-request collision, not Thread 6's diffuse cross-request one: 5 of `CONDITIONAL_WITH_REMAINDER_QUERIES`'s 30 entries had **both** the condition and the remainder independently falling through to `kiwix`'s LLM-bound path at once, degrading the intended `max(a,b)` benefit back toward `a+b` for exactly those entries. The warm-worse-than-cold appearance itself is small-sample variance, not a real warm-cache regression — quantified directly: with only ~13 cold-run picks against a 30-entry pool, there's a real ~30.6% chance per entry that its one-time real cost lands in the warm run instead of the cold one purely by which run `random.choice()` happened to draw it in.

**No code fix to `_resolve_conditional()`, `fusion.py`, or the Ollama deployment configuration.** Three options were considered and explicitly rejected (full reasoning in the design doc's Part 4): making the dispatch sequential again (regresses the 83% of the pool that already benefits from concurrency today), a per-call-site LLM admission lock (treats a global, already-named, already-decided-against deployment constraint as a local code problem), and re-filing this as a `fusion.py`-style merge bug (different category — cost of correct concurrent dispatch hitting a real backend limit, not a correctness defect in the dispatch itself). This project's own prior decision not to raise `OLLAMA_NUM_PARALLEL` (real VRAM cost for a benefit that's largely benchmark cosmetics) stands; this investigation adds one more data point to weigh if that decision is ever revisited, not a reason to revisit it now.

### Fixed — Three Real `INTENT_MAP` Keyword Gaps, Found Along the Way
Three of the five `kiwix`-double-hit pool entries above turned out to be real, narrow keyword-list gaps independent of the concurrency/Ollama story — close enough to existing entries to read as obvious oversights once placed side by side, in the same class of bug this project has already found and fixed once before (the `"is it up"`/`"are they up"` stop-word-only gap; the GPIO/`"on"` word-boundary issue):

- **`forecast`**: `"will it rain"`/`"will it snow"` were the only rain/snow triggers — the equally natural `"is it raining"`/`"is it going to rain"` phrasing had no match at all and fell through to `kiwix`. Added `"is it raining"`, `"is it going to rain"`, `"going to rain"`, `"is it going to snow"`, `"going to snow"`. The snow addition closes a second, independently-found gap in the same family: `"if it is going to snow, remind me to grab a coat"` — a real, existing entry in `CONDITIONAL_QUERIES` — had **zero** keyword match under the pre-fix list and was silently falling through to the LLM on every cold miss, only resolving correctly because the LLM happened to guess right.
- **`uptime`**: `"is everything up"` already matched — the equally natural `"online"` synonym (`"is everything online"`) had no trigger at all. Added `"everything online"`, `"is everything online"`, `"anything online"`. Confirmed the new bare `"online"` trigger doesn't collide with `web`'s own `"look it up online"`/`"find online"` triggers, since `INTENT_MAP` matching is query-contains-trigger, not the reverse.
- **`ha`**: `"are the doors locked"`/`"door locked"`/`"doors locked"` all require word-adjacency as a plain substring check — the equally natural `"doors are locked"` (with `"are"` inserted) matched none of them. Added `"doors are locked"`, `"door is locked"`.

The two remaining double-hit entries (`"whats happening with bitcoin"`, appearing twice) are **not** keyword gaps — `bitcoin` is used deliberately elsewhere in this same locustfile (`DISCOURSE_FRAMING_QUERIES`) as a genuine, intentional `kiwix`-style encyclopedic topic, and correctly continues to route there. `CONDITIONAL_WITH_REMAINDER_QUERIES`'s double-kiwix-hit count drops from 5/30 (17%) to 2/30 (7%, both intentional) as a direct, confirmed result of these three fixes.

### Tests
- `tests/test_router.py` — `TestKeywordDetect` extended with 9 new regression tests for the three fixes above, including a direct negative check that the new `uptime` `"online"` trigger doesn't collide with `web`'s existing `"online"`-containing phrases; new `test_conditional_with_remainder_pool_double_kiwix_hit_count_stays_low` parses `tests/locustfile.py` via the same AST approach as the existing `cache_hit`-collision test (for the same `gevent`-import-safety reason) and asserts the pool's real double-kiwix-hit count via the actual `detect_conditional()`/`detect_intent()` functions, confirmed to catch a reintroduction of the original gap by direct reversion test
- Version bumped to 3.50.19

---

## [3.50.18]

### Fixed — Seven Findings Across `fusion.py` and Its Direct Dependents, From One Deliberately Exhaustive Investigation
Seven separate findings surfaced across four successive audit passes of `app/sources/fusion.py` and its direct dependents in `app/router.py` — found while verifying the v3.50.13 singleflight fix's own interaction with `suppress_cache_writes()`, then investigating a real, recurring `RemoteDisconnected` failure, then a fresh full re-read of the file, then a systematic cross-check of `_looks_empty()`'s phrase list, then adversarial testing of the merge functions, then finishing a systematic audit of `search()`'s own control flow. Nothing below was inferred from reading the code alone — every claim was confirmed by running it. All seven fixes are independent and could have shipped separately; they're bundled here purely because they share a file or its direct dependents.

**1. `ContextVar` propagation gap.** `fusion.search()`'s concurrent dispatch used a bare `executor.submit(fn, *args)` — the exact shape that does not propagate `contextvars.ContextVar` state (specifically `router.suppress_cache_writes()`) into worker threads. `router.py`'s `_resolve_conditional()` and `searxng.py`'s own concurrent fetch had already learned this lesson and fixed it; `fusion.py` was the one remaining unfixed `ThreadPoolExecutor` site in the codebase. Confirmed via direct, real reproduction this had zero effect on real traffic (only `kiwix.py` writes to the routing cache from inside a source handler, and `suppress_cache_writes()` has exactly one real caller anywhere in the codebase — `adversarial_testing.py`) but a real, confirmed effect on adversarial testing's own documented cache-isolation guarantee: quantified across the real keyword/discourse-pattern space, 96 of 96 combinations of a discourse-framing phrase sharing a clause with a real keyword (no conjunction) reach this exact leak shape. Fixed with the same, already-proven `contextvars.copy_context().run(...)` pattern, one call per submitted task.

**2. Unbounded per-request thread creation.** `search()` created a brand-new `ThreadPoolExecutor` on every call — confirmed directly that 20 concurrent fusion-shaped requests produced 81 real, live OS threads at peak, with no ceiling as concurrent traffic increased. Investigated because of a real, recurring `RemoteDisconnected('Remote end closed connection without response')` failure appearing in this project's own benchmark history since v3.50.9, every occurrence landing on a `fusion_*` endpoint and producing zero corresponding application-layer log line. **Not claimed as a proven root cause** — no direct access to the real deployment's ulimits or `dmesg` output from the moment of either failure — but a well-corroborated mechanism with no real downside to fixing. Fixed by replacing the per-request executor with a single, shared, module-level pool (new `FUSION_THREAD_POOL_SIZE` setting, default 12) — the same shape of fix `app/llm.py`'s connection pool already applied to a different unbounded-per-call resource.

**3. Order-dependent deduplication with a real, measured bias against `kiwix`.** `_deduplicate()`'s docstring says the shorter, more-redundant source should be dropped on 60%+ sentence overlap, but the implementation didn't actually check which compared source was longer — confirmed directly that the same two pieces of content, same actual overlap, produced opposite outcomes purely from which key appeared first in the `results` dict, and dict insertion order in the real call site is determined by `as_completed()`'s own completion order. Quantified against this project's own real, measured cold-path latency distributions: under cold-cache conditions, `web` wins the completion race roughly two-thirds of the time, meaning the bug had a real, confirmed lean toward discarding `kiwix`'s content in favor of `web`'s, specifically on the queries most likely to trigger real overlap at all. Fixed by comparing sentence-set sizes directly and always treating the smaller set as the removal candidate.

**4. Five missing `_looks_empty()` failure phrases.** A second, systematic cross-check of every plain-string failure/empty return statement in every source file found five gaps a prior pass missed: `"unable to retrieve"` (forecast.py), `"no valid sources"`/`"no results returned"` (fusion.py's own two self-generated messages), and `"no entity states returned"`/`"no matching entities found"`/`"no significant changes"` (home_assistant.py, snapshots.py). The forecast.py gap was real and user-visible: `forecast.search()`'s exception handler returns `f"Unable to retrieve forecast: {e}"` on any failure, and since `_looks_empty()` didn't recognize it, `router.py` cached it as if it were a genuine, successful weather result for up to 30 minutes (the default forecast cache TTL) after a single transient API hiccup. Fixed by adding all five phrases, confirmed protected by the same markdown-bold gate already guarding the original phrase list.

**5. Title-only item deduplication risk — documented as a finding plus a direction, not yet fully fixed.** `_dedupe_items_across_blobs()` keys purely on an item's leading `**Title**` line, with no consideration of whether the rest of the item actually agrees — confirmed this can treat genuinely different articles as duplicates whenever headlines happen to coincide (wire-service syndication, multiple outlets, SEO-driven duplicate titling). The naive full-content-keying fix was checked against a second realistic scenario and found to introduce its own regression (it would treat the same article reached via two different tracking-parameter URLs as different items). Recorded as a follow-on requiring its own design work, not rushed into this release.

**6. Per-pair separator inconsistency in same-source merging.** Both `fusion._merge_same_source()` and `router.py`'s `_dedupe_nested_fusion_sections()` decided the `"\n\n---\n\n"` vs bare `"\n\n"` item separator independently on each pairwise merge, rather than once for the whole group of same-source parts being combined. A chain mixing single-item and multi-item same-source results got the wrong, ambiguous `"\n\n"` separator on the early pairs — confirmed directly with a real, plausible compound query where two genuinely separate, unrelated headlines ended up joined by a bare blank line, visually indistinguishable from one continuous story. Fixed by restructuring both functions to group first, decide the separator once per group. **One deliberate behavior change:** combining exactly two genuinely single-item same-source parts now always gets `"\n\n---\n\n"` instead of the old `"\n\n"` — correct behavior, not a side effect, since two separate results are inherently multi-item the moment there are two of them.

**7. `FUSION_TIMEOUT_SECONDS` never actually bounded the caller's wait — possibly the most consequential finding here.** `as_completed(futures, timeout=fusion_timeout)`'s own timeout fired exactly when configured — that part always worked. The bug was one level up: `with ThreadPoolExecutor(...) as executor:`'s implicit `shutdown(wait=True)` on exit blocked until every submitted thread genuinely finished, regardless of what `as_completed()` had already given up on. Confirmed directly, measured: a configured 1-second timeout, an actual ~10-second caller-facing wait. The clearest evidence this was already shipping: this project's own existing regression test for this code path had been silently taking 10.4 real seconds every run, the entire time it existed, because it only ever asserted the result's content, never how long producing it took. Fixed by managing the (now-shared) executor's lifecycle explicitly — `executor.shutdown(wait=False)` instead of the implicit context-manager shutdown. Measured after the fix: ~1.14-1.16 seconds against a configured 1-second timeout; the same test's own runtime dropped from 10.4s to 1.0s as a direct, externally-checkable confirmation. A single source slow enough to hit its own configured ceiling (SearXNG's documented cold-tail behavior, for instance) had silently been capable of holding every fusion call open for its own full real duration, on every release, until now.

### Added
- `FUSION_THREAD_POOL_SIZE` setting (default `12`) — see `README.md`/[Configuration Reference](https://github.com/immortalbob/Mnemolis/wiki/Configuration-Reference#fusion)

### Tests
- `tests/test_fusion.py` — new `TestFusionContextVarPropagation` class (suppression propagates into fusion's worker threads, the flip-side unsuppressed confirmation, and a direct reproduction of the real, common discourse-framing-no-conjunction leak shape via the real, unmocked `route_with_source()`); new `TestFusionSharedExecutor` class (concurrent calls reuse the shared pool rather than creating unbounded threads, the pool is genuinely sized from `settings.fusion_thread_pool_size`); `TestFusionDeduplicate` extended with order-independence and real-world unequal-length overlap regression tests; `TestLooksEmpty` extended with one test per newly-added phrase plus the markdown-bold-gate false-positive checks; `TestDedupeItemsAcrossBlobs` updated — the prior two-single-item test's literal-separator assertion was inverted to match the corrected, intentional behavior, and a new mixed single-item/multi-item chain test added
- `tests/test_router.py` — `TestDedupeNestedFusionSections`'s prior dangling-separator test updated to assert a single clean separator rather than zero separators (the corrected, intentional behavior for this case), and a new mixed single-item/multi-item chain test added mirroring the `fusion.py` sibling fix
- `tests/test_config.py` — new `fusion_thread_pool_size` default test (`12`); `FUSION_THREAD_POOL_SIZE` added to the cleared-env-vars list

### Changed
- `wiki/Fusion.md` — new "Concurrency and thread pool sizing" section; Deduplication and same-source-merge sections updated to describe the order-independence and per-group-separator fixes; Development Notes extended
- `wiki/The-Fusion-Merge-Bugs.md` — new "v3.50.18: seven findings, two files, one investigation" section covering all seven fixes in detail
- `wiki/Configuration-Reference.md`, `README.md` — `FUSION_THREAD_POOL_SIZE` documented; `FUSION_TIMEOUT_SECONDS`'s entry updated to note it now bounds the caller's actual wait
- Version bumped to 3.50.18

---

## [3.50.17]

### Changed — Expanded SearXNG Engine List Beyond Just DuckDuckGo
v3.50.16 disabled `duckduckgo` after finding its own stale per-engine timeout was the cause of `auto`'s real benchmark plateau. The very next benchmark run after applying that fix still showed real, if smaller, stalls — `docker logs searxng --since 10m | grep "ERROR:searx.engines"` showed why: `google` failing on every query with a known, recurring SearXNG scraper bug (`IndexError: list index out of range` in `searx/engines/google.py`, confirmed both in this deployment's own logs and in multiple independent external bug reports against fresh installs), `brave` rate-limited (`suspended_time=180`), and `wikipedia` also rate-limited under the same sustained load. Disabling one bad engine just exposed the next one taking on more traffic.

**`searxng/settings.yml` now ships a full, explicit engine list rather than one single override.** Disabled: `duckduckgo` (stale per-engine timeout, CAPTCHA defense), `google`/`bing` (shared scraper fragility — Bing has the identical `IndexError` bug reported against it independently), `brave`/`wikipedia` (both rate-limited under sustained querying this session). Enabled in their place: `mojeek` and `presearch` (both disabled by SearXNG's own default), based on an independent, corroborating real-world report of someone hitting the identical failure signature (`brave` suspended, `duckduckgo` access-denied, `qwant` API error) and finding `mojeek`/`startpage`/`presearch` worked cleanly with zero errors. `startpage` itself needed no change — already enabled by SearXNG's own default, never implicated in any failure this session.

Every entry in the new list is explicit (`disabled: true` or `disabled: false`), not left to SearXNG's own silent default, specifically so the file is self-documenting — each engine's status and the real reason behind it is visible without cross-referencing SearXNG's own `settings.yml`.

**Framed honestly, the same as every fix in this arc**: bot-detection sensitivity and rate-limit thresholds are IP-reputation- and traffic-volume-dependent — what broke repeatedly on this exact deployment under this exact sustained benchmark load may not break the same way on every deployment. Every disabled engine can be flipped back with `disabled: false` if your own instance doesn't see this behavior.

### Changed
- `searxng/settings.yml` — full explicit engine enable/disable list (see above)
- `README.md` — SearXNG section updated with the full table of disabled engines and why, plus the two newly-enabled ones
- Version bumped to 3.50.17

---

## [3.50.16]

### Found and Fixed — The Real Cause of `auto`'s Benchmark Plateau Was Never in `auto`'s Own Code
v3.50.15's own conclusion — that `auto`'s cold p99 plateau was likely just irreducible noise at the tail of a small benchmark sample, since p90 had stayed remarkably stable across every release — was wrong, and live testing against a real deployment found the actual cause within the same investigation session.

**What actually broke the "just noise" theory**: a single, genuinely cold request to a fusion-escalating query (one that pulls in `web` as a fused source) was timed directly, by hand, more than once. The identical query swung between 1.75 seconds and 11-13 seconds across repeated cold runs, with zero code changes in between. A 7x spread on one query is not what statistical noise at the tail of a small sample looks like — that was the signal to keep digging rather than accept the prior round's explanation.

**Two false leads, ruled out by direct testing rather than assumed:**
- **Host DNS/IPv6 loopback fallback** — `localhost:8080` from the host shell took 8-10 seconds while `searxng:8080` (the real container-to-container hostname Mnemolis actually uses) took 9-11ms, every time. Confirmed this was purely a host-`curl`/`docker-proxy` loopback artifact (`curl -4` forcing IPv4 was *still* slow, ruling out simple IPv6 fallback as the mechanism) — and confirmed directly via `docker network inspect` that Mnemolis's own bridge network has `EnableIPv6: false` with every container holding only an IPv4 address, meaning this class of ambiguity is structurally impossible on the actual path Mnemolis's code uses. Zero code or config relevance; included here only because it consumed real investigation time before being ruled out.
- **Adversarial testing or general host/GPU contention** — checked directly (timestamps confirmed no adversarial cycle fired during the slow test windows; the GPU was confirmed running nothing but the LLM and a desktop GUI) rather than assumed from the shared-hardware history documented in earlier releases.

**The real cause, found in SearXNG's own container logs** (not Mnemolis's): `duckduckgo`'s own per-engine `timeout:` override had stayed at SearXNG's old factory default (`10.0`) the entire time — completely unaffected by the global `outgoing.request_timeout`/`max_request_timeout` raise this project's own [The SearXNG Timeout Lesson](https://github.com/immortalbob/Mnemolis/wiki/The-SearXNG-Timeout-Lesson) already documented fixing once before. Per-engine timeout overrides replace the global settings for that one engine; they don't inherit from them — a real, easy-to-miss SearXNG behavior, confirmed directly via the exact log line (`HTTP requests timeout (search duration: 10.2s, timeout: 10.0s)`, repeating, never reaching the raised 20.0s global ceiling). Independently, the same logs showed DuckDuckGo's own CAPTCHA defense firing on every query and a real Brave rate-limit suspension (`suspended_time=180`) — both the predictable consequence of sustained, repeated automated querying against public engines that actively defend against exactly that pattern, not a bug anywhere in this stack.

**None of this was ever reachable from `_llm_detect()` or any of the routing-cache code three prior releases focused on.** The actual bottleneck the whole time was `web`/fusion's fan-out to SearXNG — which is precisely why singleflight (v3.50.13) and LLM connection pooling (v3.50.14), both real, both correctly diagnosed and fixed for what they targeted, could never have closed `auto`'s plateau. They fixed genuine, separate problems on the LLM-routing path; this release fixes a genuine, separate problem on the web-search path that happened to produce a similarly-shaped symptom on the same benchmark endpoint.

### Changed
- `searxng/settings.yml` — `outgoing.request_timeout`/`max_request_timeout`/`pool_connections`/`pool_maxsize` now shipped by default (`10.0`/`20.0`/`100`/`20`) rather than left as a README-only recommendation; `duckduckgo` disabled by name via a per-engine override block (with `timeout: 20.0` left in place in case anyone re-enables it later)
- `app/config.py` — `searxng_request_timeout_seconds` default raised from `10` to `25`, closing a real mismatch this project's own docs had found and described honestly once before (an earlier CHANGELOG entry corrected the *documentation* to admit the default was `10` when it had been claimed as `15`) but never actually corrected at the code level until now
- `wiki/The-SearXNG-Timeout-Lesson.md` — new section covering this third recurrence: a global fix being genuinely live doesn't guarantee every per-engine override beneath it inherited the change
- `wiki/Caching.md` — Development Notes corrected: the prior "likely just small-sample noise" entry is superseded, not left standing, by a new entry explaining what was actually found
- `wiki/The-Benchmark-Investigation-Log.md` — Thread 2's own "lesson" paragraph corrected with the real conclusion, rather than leaving the now-wrong small-sample-noise framing as the final word
- `README.md`, `wiki/Configuration-Reference.md` — `SEARXNG_REQUEST_TIMEOUT_SECONDS`'s documented default and recommendation updated to match; SearXNG timeout section updated to reflect these settings now shipping by default rather than requiring a manual edit
- `tests/test_config.py` — new default test for `searxng_request_timeout_seconds` (`25`); `SEARXNG_REQUEST_TIMEOUT_SECONDS` added to the cleared-env-vars list
- Version bumped to 3.50.16

---

## [3.50.15]

### Added — `LLM_KEEP_ALIVE`, Found While Investigating Why the v3.50.14 Connection-Pooling Fix Also Didn't Move `auto`'s Benchmark Plateau
Three confirmed re-benchmarks against v3.50.14's connection-pooling fix told the same story singleflight's own re-benchmark did one release earlier: `auto`'s cold p99 didn't move (3400ms, 2300ms, 2200ms — squarely inside the same scatter every prior run at this pool size has shown). The connection-pooling fix itself is real and independently verified (genuine reuse, genuine thread-safety, correctly sized pooling — none of that is in question), but two real, independently-confirmed fixes in a row failing to move the same number was the signal to re-examine the number itself rather than reach for a third mechanism on faith.

**Re-reading the same benchmark history with p90 instead of p99 surfaced something that should have been obvious sooner: at this benchmark's real sample sizes, `auto`'s p99 isn't a stable statistic at all.** With roughly 60-80 total `auto` picks per run and only 2 of `AUTO_QUERIES`' 24 entries genuinely LLM-dependent, p99 sits at rank 1-2 from the top of the sample — effectively just reporting the single slowest request in that run, not a repeatable property of the routing code. `auto`'s own p90 (reflecting roughly 7-9 real samples instead of 1) has stayed remarkably consistent across every release this project has ever benchmarked: 710ms (v3.50.9), 720/740/720ms (v3.50.11's three runs), 710/710/91ms (v3.50.13's three runs — the 91ms being the one genuine outlier in the whole series), 720/720/700ms (v3.50.14's three confirmed runs). That consistency suggests ~700-740ms is close to the genuine, ordinary cost of one real, unqueued LLM call on this hardware — and that the apparent "plateau" at p99 was likely never a fixable property either singleflight or connection pooling was positioned to close, just single-sample noise concentrated at the tail of a small sample.

**One real, concrete fix shipped from this round regardless, found the same "read the actual client code" way as the prior two: `app/llm.py` never sent Ollama's `keep_alive` field at all**, relying entirely on the server's own ambient 5-minute default with zero application-level control. This project's own deployment (see the v3.50.11 changelog entry's VRAM math) shares the same `qwen3:8b` instance with an unrelated agentic-coding workflow on the same machine — a real, plausible way for the model to be evicted from VRAM by something entirely outside Mnemolis's own request pattern, independent of anything this codebase does between calls.

New setting `LLM_KEEP_ALIVE` (default `"5m"`, matching Ollama's own server-side default) is now sent on every Ollama-native `/api/generate` call, accepting exactly Ollama's own documented formats — a duration string (`"30m"`, `"3h"`), plain seconds (`"3600"`), `"-1"` for never-unload, or `"0"` for unload-immediately — passed straight through with no reinterpretation, so any future Ollama-documented format keeps working without a code change here. Deliberately defaulted to Ollama's own existing default rather than `-1`: pinning the model in VRAM indefinitely from Mnemolis's side would compete with whatever else the same GPU is doing between real, infrequent household questions, for no benefit during genuine idle periods — but the setting exists precisely so this can be changed to `"20m"`, `"3h"`, `"-1"`, or anything else Ollama accepts, without touching code.

**Deliberately NOT sent on the OpenAI-compatible path.** Confirmed via a real, externally-reported bug (not assumed): Ollama's own OpenAI-compatible endpoint silently ignores `keep_alive` when passed through OpenAI-SDK-style requests, falling back to whatever the server's own ambient default is regardless of what's sent. A genuinely different OpenAI-compatible backend (llama-server, LM Studio) has no standard equivalent concept either. Sending a field that's either silently dropped or meaningless to the actual backend would be a false promise of control this setting can't actually deliver on that path — `_complete_openai()` leaves it out rather than sending it and hoping.

**Recorded with the same honesty as the connection-pooling fix before it.** This is a real, low-risk, idiomatic fix worth having on its own merits regardless of outcome — but there is no direct evidence the other workflow was actually active during any of the specific benchmark runs that showed the plateau, and the p90/p99 analysis above suggests the plateau may simply be irreducible single-sample noise at this benchmark's sample size, which `keep_alive` wouldn't change either. This closes one plausible, real contributor; whether it moves `auto`'s p99 at all is for the next re-benchmark to show, not something to assume from the mechanism alone. See `BENCHMARKS.md`'s v3.50.14 entry for the three re-benchmarks that led here and [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching#llm-connection-pooling-and-keep-alive) for the full mechanism writeup.

### Tests
- `tests/test_llm.py` — `TestCompleteOllama` extended with tests confirming `keep_alive` is read fresh from `settings.llm_keep_alive` on every call (not baked in once), that the default matches Ollama's own ("5m"), and that every documented Ollama format (`"30m"`, `"3h"`, `"3600"`, `"-1"`, `"0"`) passes through to the payload unmodified. `TestCompleteOpenAI` extended with a test confirming `keep_alive` is genuinely absent from that path's payload, not just unused, even when the setting is changed
- `tests/test_config.py` — new `llm_keep_alive` default test, `LLM_KEEP_ALIVE` added to the cleared-env-vars list so the default test can't pass spuriously against a real env var

### Changed
- `wiki/Caching.md` — "LLM connection pooling" section renamed to "LLM connection pooling and keep-alive" and extended with the keep_alive mechanism and its honest framing; new Development Notes entry covering this round's investigation
- `wiki/The-Benchmark-Investigation-Log.md` — Thread 2 extended with the v3.50.14 re-benchmark results, the p99-vs-p90 statistical re-analysis, and the keep_alive fix; all stale anchor references to the renamed Caching section updated
- `wiki/Configuration-Reference.md`, `README.md` — `LLM_KEEP_ALIVE` documented in both
- `BENCHMARKS.md` — new v3.50.14 entry (three confirmed cold/warm pairs, one uncertain-build run noted and excluded from the comparison)
- Version bumped to 3.50.15

---

## [3.50.14]

### Fixed — `app/llm.py` Had Zero HTTP Connection Reuse, Found While Investigating Why Singleflight Didn't Move `auto`'s Benchmark Plateau
Three real re-benchmarks against v3.50.13's singleflight fix produced a genuinely surprising result: `auto`'s cold p99 didn't move at all (2500ms, 2300ms, 2300ms — landing in essentially the same 2300-2700ms band three of four pre-fix runs already showed, never reproducing the pre-fix run's own 990ms favorable outlier). Confirmed directly that singleflight itself wasn't broken — 8 concurrent callers for an identical uncached key still collapse to exactly 1 real LLM call, the mechanism working precisely as designed in isolation — so the non-result pointed at something singleflight structurally cannot touch: cost paid by every call individually, not cost wasted on redundant duplicate work.

The asymmetry that ruled out "just a noisy session of shared backend contention" as the explanation: `kiwix`/`kiwix_disambiguation`/`cache_hit` all dropped substantially run-over-run across the same three benchmarks (`kiwix` cold p99 6400→3900→1600ms; `cache_hit` 4400→1100→1100ms; `kiwix_disambiguation` 6200→2900→2900ms) while `auto` stayed flat. A shared-contention story predicts every LLM-touching endpoint moving together — these did, `auto` conspicuously didn't, which is what motivated reading `app/llm.py` directly rather than re-running a fourth time hoping for a better draw.

**Found: every single call into `complete()` used the bare `requests.post()` module function, never a `requests.Session`** — meaning every LLM call (book selection, source routing, fusion-source selection, disambiguation candidates, the entire LLM-dependent surface this whole investigation has been chasing) opened a brand-new TCP connection to the LLM backend and tore it down again immediately after, with zero reuse. The identical class of bug `app/sources/uptime_kuma.py` already had and fixed (a fresh Socket.IO connect+login cycle on every call) — see [The Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log#thread-1-uptimes-warm-cache-tail-five-releases-to-a-real-root-cause)'s Thread 1 — just never checked for here, despite being the obvious next place to look once the same general shape of symptom showed up again.

Confirmed directly against a real local HTTP/1.1 server, not inferred from the benchmark numbers alone: 10 sequential calls through the old bare-`requests.post()` pattern opened 10 distinct TCP connections (one per call, tracked by source port); the identical 10 calls through a persistent `requests.Session` opened exactly 1, fully reused for every subsequent call. A before/after comparison against the same server confirms this precisely — old behavior, 10 connections for 10 calls; new behavior, 1 connection for 10 calls.

**The fix**: a single, eagerly-constructed module-level `requests.Session` (`app.llm._session`) now backs both `_complete_ollama()` and `_complete_openai()`'s calls, replacing the bare `requests.post()` each previously used independently. No lazy-init-with-lock accessor needed the way `uptime_kuma.get_connection()` required — `Session()` construction does no I/O at all (just builds an empty connection-pool adapter), so there's no "first caller pays a real connection cost" race to guard against. `requests.Session` is documented as safe for concurrent use across threads for making requests (the unsafe case is concurrent mutation of shared session state like `session.headers`, which nothing in this module does after construction) — confirmed directly under real concurrent load: 15-20 simultaneous calls through the shared session all succeeded with zero errors.

**The pool size itself is now explicitly configured, not left at `requests`' own library default.** `requests`' default `pool_maxsize` (10) is sized for general-purpose use, not this project's actual concurrency shape — Starlette's own default thread-pool limit for synchronous routes is 40 (confirmed directly via `anyio.to_thread.current_default_thread_limiter().total_tokens`), comfortably exceeding `requests`' default pool size under the kind of load a 20-concurrent-user Locust benchmark can produce. New setting `LLM_CONNECTION_POOL_SIZE` (default 20) sizes the pool explicitly — confirmed directly: two waves of 20 genuinely-concurrent calls each (40 total) against a real server, with `pool_maxsize=20`, used exactly 20 distinct connections total, the second wave fully reusing the first wave's pooled connections rather than opening new ones.

**Deliberately not pursued in this release: confirming this actually closes `auto`'s plateau.** The mechanism is real and independently verified (genuine connection reuse, genuine thread-safety, genuine pool sizing matched to this project's real concurrency), but whether it's the dominant remaining cost behind `auto`'s specific 2300-2700ms band — as opposed to a real improvement that doesn't fully close the gap, the way the `conditional`/`conditional_remainder` pool-widening correction (v3.50.9) was real progress without being a complete fix — is a prediction for the next benchmark to confirm, not a result to assume from the mechanism alone. See `BENCHMARKS.md`'s v3.50.13 entry for the three re-benchmarks that led here and [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching#llm-connection-pooling) for the full mechanism writeup.

### Added
- `LLM_CONNECTION_POOL_SIZE` setting (default 20) — see `README.md`/[Configuration Reference](https://github.com/immortalbob/Mnemolis/wiki/Configuration-Reference#llm-backend)

### Tests
- `tests/test_llm.py` — new `TestPersistentConnection` class: confirms `app.llm._session` is a real `requests.Session` (not just an object with a matching `.post()` signature), confirms the identical session object backs multiple sequential calls, confirms both `_complete_ollama()` and `_complete_openai()` were migrated independently (each had its own separate `requests.post()` call site before this fix), and confirms the pool size is read from the new setting rather than left at the library default
- Every pre-existing test in `tests/test_llm.py` that patched `app.llm.requests.post` updated to patch `app.llm._session.post` instead — the correct target now that calls route through the persistent session, not the bare module function. No behavioral changes to any of these tests beyond the patch target; same 27 tests, same assertions

### Changed
- `wiki/Caching.md` — new "LLM connection pooling" section (mirroring the existing `uptime` connection section's structure) plus a Development Notes entry covering the investigation that led here
- `wiki/The-Benchmark-Investigation-Log.md` — Thread 2 extended with the three re-benchmark results against the v3.50.13 fix and the connection-pooling investigation/fix that followed
- `wiki/Configuration-Reference.md`, `README.md` — `LLM_CONNECTION_POOL_SIZE` documented in both
- `BENCHMARKS.md` — new v3.50.13 entry (three cold/warm pairs) recording the actual re-benchmark results against the singleflight fix
- Version bumped to 3.50.14

---

## [3.50.13]

### Added — Per-Key In-Flight Deduplication ("Singleflight") for the Routing Cache
Built the application-layer fix the v3.50.12 design proposal named: a per-key lock registry (`router._singleflight()`, backed by a refcounted lock dict, `_inflight_locks`) wrapping the check-then-call-then-write sequence at all four real call sites that shared the identical unprotected structure — `_llm_detect()` (the `auto`-routing path the original investigation traced this to), `_llm_pick_fusion_sources()`, and Kiwix's own `_pick_books_with_llm()`/`_get_disambiguation_candidates()`. A second concurrent caller for an identical, never-yet-cached key now blocks on the first caller's resolution and reuses its result, rather than independently paying the full LLM cost — confirmed directly: 8 concurrent callers for the same uncached query now produce exactly 1 real LLM call, not 8, with total elapsed time matching one call's cost. Per-key, not a single global lock, so unrelated queries (a `forecast` lookup and a `kiwix` lookup, say) still proceed fully concurrently — only callers racing for the *identical* key actually queue.

The proposal's own "real risks and open questions" section is resolved, not skipped:
- **Lock-cleanup correctness under genuine concurrent load** — the original "delete if not locked" sketch had a real, named race (a stale lock reference outliving its own deletion from the registry). Replaced with a `_RefCountedLock` wrapper: every mutation of `_inflight_locks` (create-or-get, increment, decrement-and-maybe-delete) happens under one guard lock, so a key is only ever removed once its refcount is provably zero. Verified with a dedicated concurrency stress test (12 threads, 200 rounds each, hammering one shared key) plus a targeted repeated-repro test for the exact release/reacquire race the original sketch named — the same stress-test discipline this project's `_atomic_write_json()` fix already established, not a claim shipped on inspection alone.
- **Interaction with `suppress_cache_writes()`** — checked directly rather than assumed safe: because the suppression check lives inside `_set_routing()`/`_set_cached()` at write time (not inside the lock itself), a suppressed in-flight resolver's write is silently dropped regardless of lock ordering, and a real caller arriving after it correctly re-checks the (still-empty) cache and pays the cost itself. Confirmed empirically in both arrival orders — no cross-contamination either direction.
- **Sharing the registry across `router.py`/`kiwix.py`** — done via the same lazy-import pattern `kiwix.py` already used for `_get_routing`/`_set_routing` (`_get_singleflight_fn()`, mirroring `_get_routing_fns()`). One shared registry, confirmed directly (`kiwix._get_singleflight_fn() is router._singleflight`).
- **Timeout behavior** — left as the proposal's own accepted tradeoff: a queued caller waits for the in-flight resolution to finish (success or failure), then re-checks cache and proceeds with its own call if the first caller's attempt failed. Strictly better than the pre-fix behavior in the success case; no worse in the failing case.

The narrower pre-warming fallback the proposal documented was not needed — the real fix was judged tractable as scoped.

**Deliberately scoped to the routing cache only**, matching the original proposal — the plain result cache's identical, structurally-equivalent gap (proven to still exist by `TestResultCacheThunderingHerd` in `tests/test_router.py`, written for `cache_hit`'s own collision history) is untouched by this release.

**Not yet re-benchmarked against a real Locust run.** The design rationale predicts this should close `auto`'s 2300-2700ms cold p99 plateau outright, and plausibly improve `kiwix_disambiguation`'s own large cold-tail outliers (6800-7000ms in recent runs) if those share the same root cause — both are predictions to confirm against the next real benchmark, not results to record as settled.

### Found, not fixed — `fusion.py`'s Concurrent Dispatch Doesn't Propagate `suppress_cache_writes()`
While verifying the singleflight fix's interaction with synthetic-traffic suppression, found that `fusion.py`'s `search()` submits each fanned-out source call to its `ThreadPoolExecutor` directly (`executor.submit(SOURCE_MAP[s], query)`), without the `contextvars.copy_context().run(...)` wrapping `router.py`'s `_resolve_conditional()` and `searxng.py`'s own concurrent fetch both already use for the identical reason. A synthetic Adversarial Self-Testing query that resolves to fusion, with one of its fanned-out sources making its own uncached routing-cache write inside that worker thread, would have that write land for real rather than respecting suppression — the same class of leak `suppress_cache_writes()` was built to close, reachable through a path that fix didn't cover. Not yet confirmed how reachable this is in practice, and deliberately not patched here without a failing test first — see `wiki/Roadmap.md`'s "Still tracked, lower priority" section; tracked for its own design doc.

### Tests
- `tests/test_router.py` — `TestInflightLockRegistryPrimitives` (registry refcounting, including the dedicated stress test and race-repro test above), `TestSingleflightContextManager` (mutual exclusion per key, independence across keys, lock release on exception), and per-call-site regression classes (`TestLlmDetectSingleflight`, `TestLlmPickFusionSourcesSingleflight`, `TestKiwixPickBooksSingleflight`, `TestKiwixDisambiguationCandidatesSingleflight`) proving each of the four call sites collapses N concurrent callers to 1 real LLM call, never blocks on an already-warm key, never blocks across different keys, and still preserves each function's own existing failure-fallback-isn't-cached behavior under concurrency.
- `tests/conftest.py`'s autouse cache-isolation fixture extended to snapshot/restore `_inflight_locks` alongside the existing `_cache`/`_routing_cache` handling — the same plain-module-level-dict-shared-across-the-test-process risk, with a worse failure mode if missed (a hang from a leaked lock, not a clear assertion failure).

### Changed
- `wiki/Caching.md` — new Development Notes entry plus a reference-level paragraph in the main Routing cache section describing the singleflight mechanism and its scope
- `wiki/The-Benchmark-Investigation-Log.md` — Thread 2's closing entry updated to describe the fix as built rather than proposed
- `wiki/Roadmap.md` — singleflight entry removed from "Still tracked, lower priority" (shipped); the `fusion.py` propagation gap found along the way added in its place
- Version bumped to 3.50.13

---

## [3.50.12]

### Confirmed — Two Further Re-Benchmarks Show a Real, if Noisy, Improvement From the v3.50.9 Pool-Sizing Correction
Two further real Locust runs against the v3.50.11 codebase, on top of the one already recorded in `BENCHMARKS.md`'s v3.50.11 entry — three genuinely independent cold/warm pairs total at this pool size. `conditional`'s cold p99 across the three: 5300ms, 2500ms, 2500ms — real, substantial improvement from the original 9800ms in every single run, but landing on a different number each time rather than settling, consistent with the design doc's own "inherently noisier metric" caution. `conditional_remainder` showed the same shape (1500ms, 1800ms, 2900ms) — always well below the pre-correction 4200ms, never fully quiet. `uptime` stayed tightly clustered across all three (cold p99 62ms/140ms/63ms, warm 73ms/59ms/61ms) — the one genuinely fully-confirmed, no-caveats result of the three. Zero exceptions/failures recurred on any of the three runs — the `RemoteDisconnected` from the v3.50.9 warm run has now not reappeared across three further attempts.

### Investigated — Why `auto`'s Cold-Path Tail Keeps Landing in the Same Range Despite Three Pool Widenings
`auto`'s cold p99 across four total runs at the current pool size (v3.50.9, plus the three just-described v3.50.11-era runs): 2700ms, 990ms, 2300ms, 2400ms. Three of the four cluster tightly at 2300-2700ms; one (990ms) is a real, favorable outlier — consistent with, not contrary to, a probabilistic collision mechanism that doesn't fire on every single run. `AUTO_QUERIES` has been widened three separate times across this project's history (6→12→24), each pass measurably reducing the worst-case spike (10000ms → 3800/3000ms → the current range) but none touching this specific recurring cost.

Traced to a real, previously-unconnected fact: of `AUTO_QUERIES`'s 24 entries, only **2** (`"what is nitrogen"`, `"whats the temperature outside right now"`) fail keyword matching and require an LLM call at all — confirmed directly by running every entry through the real `_keyword_detect()`. This ratio was discovered once before (the v3.50.9 correction's source-mix analysis), but only as context for why `AUTO_QUERIES`'s widening was lower-risk than `CONDITIONAL_QUERIES`'s — never connected to why the pool's *own* tail had stopped responding to further widening. The absolute count of LLM-dependent entries has never changed across any of the three widenings; only the cheap, keyword-resolved entries multiplied. With `auto_routing` weighted 3 and drawn repeatedly across a 120-second run (51-69 total picks observed per run), a uniform 2/24 draw rate puts a computed 4-6 picks on just those 2 entries in a typical run — a same-entry collision between two of those picks is likely on most runs, though not guaranteed on every one, regardless of total pool size. Modeled directly: even widening the expensive subset itself from 2 to 10 distinct entries only drops per-pick collision risk on that subset from ~100% to ~87% — the real constraint is total pick volume against a small slot count, and no further realistic pool-sizing pass closes this gap.

### Added — Design Doc: Per-Key In-Flight Deduplication for the Routing/Result Caches
Rather than propose a fourth pool-widening pass against a lever that's now demonstrably exhausted, `docs/design/singleflight-routing-cache-deduplication.md` proposes the actual application-layer fix: a per-key lock ("singleflight" / request-coalescing, the standard pattern for exactly this problem) wrapping the check-then-call-then-write sequence in `_llm_detect()`, `_llm_pick_fusion_sources()`, and both of `kiwix.py`'s own routing-cache checks (`_pick_books_with_llm()`, `_get_disambiguation_candidates()`) — all four share the identical unprotected structure, confirmed by reading each one directly. Concurrent callers for the same uncached key would queue behind the first resolver rather than each independently paying the full LLM cost, closing the actual collision mechanism rather than diluting its odds.

This is a real, foundational change to shared caching code used by every source, not a narrow `auto`-specific patch — proposed, not built. The design doc names its own open risks honestly: lock-cleanup correctness under genuine concurrent load needs a real stress test before shipping; whether a synthetic Adversarial Self-Testing query's in-flight lock could cross-contaminate a real concurrent user request needs a dedicated check against `suppress_cache_writes()`'s existing isolation; and queued-caller timeout behavior in the genuinely-failing case needs a deliberate decision, not an assumption. A narrower, lower-risk fallback (pre-warming `AUTO_QUERIES`'s 2 known-expensive entries at startup) is documented as the weaker alternative if the shared-cache-layer change is judged too invasive to take on.

### Changed
- `BENCHMARKS.md` and [The Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log) updated with the two further re-benchmark results and a pointer to the new design doc
- `wiki/Roadmap.md` — added the singleflight proposal to "Still tracked, lower priority," with the real root-cause summary and the open risks the design doc names
- Version bumped to 3.50.12 — no application code changed this release; investigation, benchmark records, and a new design doc only

**Total test count: 1268** (unchanged)

---

## [3.50.11]

### Investigated — `cache_hit`'s Remaining Cold-Run Cost: Ollama Queue Contention, Not a Mnemolis Bug
The v3.50.9 and v3.50.10-era benchmark runs both showed `cache_hit`'s single cold request paying a real, multi-second cost (3800ms, then 3601ms) — a genuinely different question from the earlier, already-fixed query-collision bug (v3.50.6), since `cache_hit`'s dedicated query was confirmed clean of any pool collision. Investigated thoroughly rather than left as a vague "probably noise":

- **Disambiguation eligibility** — ruled out directly. `CACHE_HIT_QUERY`'s search terms reduce to 3 words after stop-word stripping; `_should_disambiguate()` requires exactly 1, so the more expensive two-step disambiguation path structurally never fires for this query.
- **A second, undetected query-pool collision** — ruled out directly. The routing-cache key is the full literal query string, not derived search terms; re-confirmed unique across every pool in `tests/locustfile.py`.
- **Routing-cache disk-write cost growing with cache size over a run** — ruled out by direct measurement. `json.dump()` against a realistic 200-entry routing cache takes under 1ms — nowhere near the observed cost.
- **Ollama's own request queueing** — confirmed as the real, most likely explanation. Ollama defaults to `OLLAMA_NUM_PARALLEL=1` on memory-constrained setups, queuing every other concurrent request FIFO. Mnemolis's own `app/llm.py` has zero client-side concurrency control — every cold LLM call from every source fires as fast as threads call it. Comparing which endpoint shows the single worst sample between the two benchmark runs found a different one each time (`conditional` in v3.50.9, `kiwix_disambiguation` in the later run, with `fusion_auto`/`web`/`cache_hit` all shuffling rank in between) — the signature of every cold LLM call sharing one queue, not an endpoint-specific defect. `cache_hit` itself landed at nearly identical magnitude both runs (3800ms, 3601ms), consistent with "draws from the same shared lottery," not "this specific query is broken."

**Deliberately not pursued as a fix.** Raising `OLLAMA_NUM_PARALLEL` would help, but at a real cost worked out directly against the actual deployment: the currently-loaded `qwen3:8b` already uses 10GB VRAM at `OLLAMA_NUM_PARALLEL=1` with a 32K context window (set for an unrelated agentic-coding workflow, not Mnemolis); doubling parallelism would roughly double the KV-cache portion of that, landing around ~15GB — leaving real headroom on a 24GB card, but `num_parallel=4` would push close to the ceiling. The actual reason it's not worth pursuing: it would compete with VRAM the person actively wants free for other use (gaming), for a benefit that's purely benchmark cosmetics — this contention doesn't reflect real single-household usage at all. Recorded as understood and deliberately left as-is, not silently dropped.

### Fixed — A Real, Separate False-Positive Bug in `_looks_empty()`, Found While Investigating the Above
Checking `_looks_empty()` itself as one of several candidate mechanisms for `cache_hit`'s anomaly ruled it out for that specific purpose, but turned up a real, separate, reproducible bug worth fixing regardless: the function matched phrases like `"not configured"`, `"could not connect"`, `"could not determine"`, and `"error:"` **anywhere** in a result string, with no other constraint. Several of these are ordinary English, not unique sentinel strings.

`_diff_news()` in `app/snapshots.py` echoes raw, unmodified upstream article headlines directly into change descriptions (`f"New article: {story}"`), and `app/sources/freshrss.py`'s own `search()` does the same for article titles and summaries. Real news content can contain a headline like *"Tech Company Could Not Determine Cause of Outage"* by sheer coincidence — confirmed directly with a real reproduction: a genuinely successful, fully-populated, multi-source `changes` response containing exactly such a headline matched the old `_looks_empty()`. Since `news` is a real, live entry in `FALLBACK_CHAIN` (mapped to `web`), this is not a cosmetic or test-only issue — a real, correct `news` answer could be silently discarded in production and replaced with a worse, generic web-search result, for no reason but an unlucky word in a real headline this project has no control over.

**Two heuristics were tried and rejected before landing on the real fix**, both caught by the existing test suite or further reasoning before shipping:
- A length cap (every genuine Mnemolis message is under 80 characters) fails because `kiwix.py`'s `f"Found {title} but could not fetch article content."` has unbounded length (a real article title is interpolated into it), and a short, single-article false positive can still slip under any cap generous enough to keep that real message working.
- A prefix check (does the result *start with* the phrase) fails for the common `"X is not configured"` shape, since the source name always comes first — `"Home Assistant is not configured."` has the phrase starting at index 18, not 0. This would have broken 5 of the project's own real config-error messages; caught immediately by the existing `TestLooksEmpty`/`TestFallbackChainTriggersOnNotConfigured` test classes before the design shipped.

**The actual fix**: every genuine empty/error message this function exists to catch is plain, unformatted prose — confirmed directly against every real `return` statement in every source file that produces one. Every real article/multi-source result this project produces, by contrast, wraps titles in markdown bold (`freshrss.py`, `searxng.py`, `home_assistant.py`, and `snapshots.py`'s `format_changes()` all do this consistently). A bare `**` anywhere in the result is now checked first as a reliable, structural gate — genuine formatted content is never flagged empty, regardless of what words happen to appear inside it. The original phrase-matching logic is otherwise unchanged underneath that gate. Also fixed a separate, smaller gap found while rebuilding the phrase list from scratch against every real message: `"no monitors found"` (uptime_kuma.py's real empty-Uptime-Kuma message) was never in the list at all, meaning fusion's own empty-result filtering would have incorrectly treated it as real content in a fusion query that includes `uptime`.

### Added (Tests)
- 4 new tests in `test_fusion.py`'s `TestLooksEmpty` — a real news headline with an unlucky phrase embedded in a genuine multi-source response correctly not flagged empty; the hardest version of the same case (a short single article, the kind a length heuristic could plausibly still misclassify); confirms `kiwix.py`'s real "could not fetch article content" message (with its unbounded, variable-title prefix) still correctly matches; confirms the new "no monitors found" coverage
- 1 new end-to-end test in `test_router.py`'s `TestFallbackChainTriggersOnNotConfigured` — proves the real, live consequence directly through `route_with_source()`: a genuine `news` result with an unlucky headline is returned as-is, source `"news"`, not silently replaced by the `"web"` fallback. Confirmed both this test and the 4 unit tests above genuinely catch the regression by reverting to the old implementation and watching them fail, then restoring the fix.

### Noted — A Possible, Unconfirmed Connection to a Previously Unsolved Investigation
[The Adversarial Testing Production Bugs](https://github.com/immortalbob/Mnemolis/wiki/The-Adversarial-Testing-Production-Bugs#an-investigation-that-ended-without-a-root-cause) documents a single historical `unexpected_empty` flag that was traced six different ways and never explained — every checked mechanism ruled out against real evidence, including confirming what an *empty* `changes` response looks like. That investigation didn't have this mechanism available to check, since it wasn't found until now: whether the real `changes` response at that historical moment contained genuine content with a coincidentally-matching headline, rather than genuinely being empty, was never tested. This can't be confirmed retroactively — the original incident's raw result text was never recoverable (the entire reason `last_flagged_result_excerpt` was added). Left as an open, unconfirmed possibility rather than claimed as the resolved cause — if `unexpected_empty` fires again, this fix should mean a coincidental-headline cause can no longer be the explanation, narrowing what a future occurrence could mean.

### Changed
- Version bumped to 3.50.11

**Total test count: 1268**

---

## [3.50.10]

### Changed — Split `wiki/Benchmarks.md` Into Reference and Dev-Blog Pages, Trimmed `BENCHMARKS.md`'s Narrative
`wiki/Benchmarks.md` had drifted into doing two genuinely different jobs at once: telling a user what the current numbers mean (present-tense reference) and telling the chronological story of five separate investigation threads across nine benchmark releases (history). The same drift had happened in `BENCHMARKS.md` — every dated table came wrapped in long narrative paragraphs re-explaining the multi-release backstory each time, rather than just the numbers for that specific run.

**Split into two pages.** `wiki/Benchmarks.md` is now pure current-state reference: the constant-median fact, the cold/warm cost-shape table (refreshed to v3.50.9's real numbers), hardware caveats, and how to run your own — no chronology, no "here's what we found and fixed in vX.Y.Z." A new page, **`wiki/The-Benchmark-Investigation-Log.md`**, holds the actual story: `uptime`'s five-release path to a root cause, the `auto`/`conditional` thundering-herd saga including the real v3.50.8 sizing mistake the v3.50.9 re-benchmark caught, `cache_hit`'s collision-and-fix, `/health`'s concurrency fix, and the still-open `RemoteDisconnected` failure. Linked from `wiki/Home.md`'s "Design History" section, matching the project's existing convention for this kind of page (`The-Caching-Concurrency-Investigation`, `The-Latency-Parallelization-Investigation`, and others).

**Trimmed `BENCHMARKS.md`'s long-form narrative** in the v3.50.2/v3.50.4/v3.50.7 sections — the multi-paragraph "against the design doc's success criteria" and cross-release chronology blocks are now brief, run-specific factual captions with a pointer to the investigation log, rather than re-telling the same multi-release story inline at every dated entry. Tables, request counts, and single-run-specific findings (the actual numbers analysis for that one run) are all unchanged — only the repeated chronology was cut. Added a top-of-file pointer to both new/updated wiki pages.

Also added the v3.50.9 benchmark entry to `BENCHMARKS.md` itself, which hadn't been recorded there yet despite the changelog already discussing it — full cold/warm tables, the `uptime` win, the `conditional`/`conditional_remainder` regression, and the open `RemoteDisconnected` failure.

### Changed
- `wiki/Sources.md`/`wiki/Caching.md`/`wiki/Contributing.md`'s cross-links to the old `Benchmarks#what-got-fully-fixed-...` anchor (now removed) repointed to the new investigation log page's relevant section
- Two pre-existing, unrelated wiki link issues were checked for and confirmed unaffected by this restructure (the wiki's own pre-existing anchor-rot inventory, ~18 links, is unchanged by this pass — none of them touch the pages restructured here)
- Version bumped to 3.50.10 — no application code, tests, or benchmark data changed; documentation structure only

**Total test count: 1263** (unchanged)

---

## [3.50.9]

### Confirmed — `uptime`'s `wait_events` Fix (v3.50.8) Actually Worked
A real re-benchmark on MiniDock confirms the root-cause fix: warm p98/p99 dropped from 440ms (every prior run since v3.50.4) to **69ms** — finally in the same order of magnitude as the cleanest sources (kiwix 44ms, forecast 45ms, news 40ms), not a separate tail. Cold tail dropped from 520ms to 190ms. A small minority of requests (1-2 per run) still pay something in the 60-190ms range, consistent with the fix's own design: the one call genuinely needing the full, safe `wait_events` (right after a fresh connect or reconnect) still gets it, by design. This closes the loop the v3.50.4/v3.50.6/v3.50.7 runs left open — `uptime`'s benchmark anomaly, first flagged in v3.17.0, is resolved.

### Fixed — A Real Mistake in v3.50.8's Pool Re-Sizing, Found and Corrected by the Same Re-Benchmark
The same run that confirmed `uptime`'s fix also showed `conditional`'s cold p99 at **9800ms** — the single worst sample this endpoint has ever produced — and `conditional_remainder`'s cold p98/p99 nearly tripling (1300ms → 4200ms) versus the v3.50.7 baseline. Both got *worse*, not better, after v3.50.8's widening. This needed taking seriously rather than dismissing as noise, and re-checking the v3.50.8 sizing decision against the real result rather than assuming the model was already right.

**Two real mistakes, found by re-deriving the math rather than re-guessing:**

1. **Wrong metric.** v3.50.8 sized pools using "expected number of pool entries hit by 2+ of 20 users" — a real calculation, but not what predicts the benchmark's actual tail. The metric that does is "fraction of the 20 users whose first pick collides with someone else's," which declines monotonically with pool size (no peak to dodge) but slowly enough that v3.50.8's sizes (`CONDITIONAL_QUERIES` 20, `CONDITIONAL_WITH_REMAINDER_QUERIES` 12) still left 62-81% of users colliding with someone. Worse: re-reading the wrong-metric model's own output for `conditional_with_remainder`'s specific change (4→12) shows it predicted the absolute collision count getting WORSE (3.90 → 6.07), not better — that prediction was sitting in the v3.50.8 changelog entry's own reasoning and wasn't acted on.

2. **Wrong assumption about collision cost.** `AUTO_QUERIES`'s widening worked well at a similar nominal collision rate (55.5% at 24 entries) because most of its collisions land on a cheap, structured source — confirmed directly: only 2 of 24 `AUTO_QUERIES` entries fall through to `kiwix`'s expensive LLM book-selection path. `CONDITIONAL_QUERIES`'s 20-entry pool was the opposite: 17 of 20 conditions (85%) fell through to `kiwix`, confirmed by running every condition through `detect_intent()` directly — meaning a collision on this pool costs far more per occurrence than a similarly-frequent collision on `auto`'s pool. A lower collision rate alone wasn't going to fix this; the *kind* of collision mattered too, and the original widening's new entries (mostly "if X is in retrograde"-style conditions, written and verified only for correct conditional-detection, never checked against which source they'd actually route to) made this slightly worse, not better.

**The fix**: widened further (`CONDITIONAL_QUERIES` 20→40, `CONDITIONAL_WITH_REMAINDER_QUERIES` 12→30) and fixed the source mix — the 20 new `CONDITIONAL_QUERIES` entries were specifically written to hit `ha`/`uptime`/`forecast`/`changes` keywords in `INTENT_MAP` rather than falling through to `kiwix`, verified directly against `detect_intent()` before being added. This brought the pool's overall `kiwix`-fallback ratio from 85% down to 42%. `CONDITIONAL_WITH_REMAINDER_QUERIES`'s 18 new entries deliberately reuse the same new, better-mixed conditions (confirmed beneficial in v3.50.8: both tasks cache on the identical extracted condition text, so either one warming it helps both). `AUTO_QUERIES` (24 entries) is unchanged — its own re-benchmark result was a clear win (warm p98/p99 dropped to 65-80ms) and didn't need correcting.

Not yet re-benchmarked as of this writing. Given the size of the v3.50.8 miss, this correction is being recorded as a real, falsifiable hypothesis for the next run, not declared fixed in advance.

### Added (Tests)
- `test_locustfile_thundering_herd_pools_are_wide_enough_to_avoid_high_collision_rates` in `test_router.py`'s `TestResultCacheThunderingHerd` — replaces the v3.50.8 version of this test (same enforcement role, corrected minimums: `CONDITIONAL_QUERIES` 40, `CONDITIONAL_WITH_REMAINDER_QUERIES` 30)
- `test_conditional_queries_kiwix_fallback_ratio_stays_below_half` — guards the second half of this fix: a future well-meaning addition of more open-ended, kiwix-routed conditions can't silently walk the fallback ratio back toward 85% without a test catching it

### Flagged — A Real `RemoteDisconnected` Failure During the Warm Run, Not Yet Explained
The warm run's Locust output shows one real failure: `POST /search [fusion_triple]: RemoteDisconnected('Remote end closed connection without response')` — the server closed the TCP connection without sending any HTTP response, not a timeout or an error response. `fusion_triple` queries `uptime` alongside `forecast`/`news`, so it does touch the code changed in v3.50.8, but nothing in `app/sources/uptime_kuma.py` or `app/sources/fusion.py`'s concurrent-dispatch error handling (every per-source exception is caught and converted to a `None` result, never re-raised past the dispatch loop) explains a dropped connection at the HTTP layer. Not dismissed as noise and not attributed to the recent changes either — both would be guessing past the actual evidence. Real server-side logs from around the time of that warm run are the only way to actually know; flagged here as an open item pending that, not closed one way or the other.

### Changed
- Version bumped to 3.50.9

**Total test count: 1263**

---

## [3.50.8]

### Fixed — `uptime`'s Remaining Tail, Actually Root-Caused: `uptime_kuma_api`'s Own Unconditional `wait_events` Sleep
The v3.50.4/v3.50.6/v3.50.7 benchmark runs all showed the identical, deterministic ~440ms slow-tail value for `uptime` — a strong signal this was never noise, just never traced to its actual source. Doing exactly that: a direct, standalone reproduction against the installed `uptime_kuma_api` library (constructing a mock with `_event_data` already fully populated, then calling the real, unpatched `UptimeKumaApi._get_event_data()` against it) confirmed the real mechanism. `_get_event_data()` pays its `wait_events` sleep (default 0.2s) **unconditionally**, every single call, including when the awaited data has been sitting there, complete, since a previous call. Two such calls per `search()` (`get_monitors()` + `get_heartbeats()`) is exactly the ~0.4s structural floor — not lock contention, not server-side variance, a fixed, deterministic library cost paid on every genuine cache miss, fully explaining why the slow-tail value never moved across three separate benchmark runs.

This wasn't a library bug — `wait_events` exists for a real, documented reason: Uptime Kuma's server emits one `heartbeatList` push *per monitor* after login, and Socket.IO gives no signal for "that was the last one of this type," so the client needs a real grace period to let trailing per-monitor pushes land before treating the initial batch as complete. The genuine risk window for shortening it is narrow — only the **first** call after a fresh connect/login, while that initial batch may still be arriving. Confirmed directly that every later call has nothing left to wait for: `_event_heartbeat()` (the steady-state, post-login push handler) appends one complete record per call, with no multi-message batching at all — the persistent connection's whole design already keeps this data current via real-time pushes, independent of `wait_events`.

**The fix**: `app/sources/uptime_kuma.py` now tracks whether a connection's first data fetch has settled (a `_settled` flag set directly on the `UptimeKumaApi` instance, tied to that instance's own lifetime — a reconnect after a dead connection correctly resets it via a brand-new instance). The first call after any fresh connect/login keeps the library's safe default `wait_events` (0.2s), exactly the one call that genuinely needs it. Every later call on the same, now-settled connection gets `wait_events` shrunk to `_SETTLED_WAIT_EVENTS` (0.01s, matching `_get_event_data()`'s own internal polling granularity — a real, brief grace period kept, not eliminated outright). This removes the ~0.4s floor from the large majority of real traffic without touching the one call where the original, full wait genuinely matters.

`search()`'s public contract, `CACHE_TTL_UPTIME_SECONDS`, and `UPTIME_KUMA_TIMEOUT_SECONDS` are all unchanged — this is purely a connection-instance-level adjustment to a setting the persistent-connection fix (v3.50.4) never touched.

### Added (Tests)
- `TestWaitEventsSettling` (6 tests) in `test_uptime_kuma.py` — confirms a fresh connection keeps the safe, full `wait_events` for its first call (the real safety property this fix depends on), confirms it shrinks after that first fetch settles, confirms the settled value is real and nonzero (not eliminated outright), confirms an already-settled connection isn't touched a second time, confirms a reconnect after a dead connection independently re-settles on its own first call rather than inheriting a prior instance's shrunk value, and a genuine wall-clock timing proof (not just an attribute check) that a second call on a settled connection measurably takes less real time than the first.

### Changed — Re-Widened `AUTO_QUERIES`/`CONDITIONAL_QUERIES`/`CONDITIONAL_WITH_REMAINDER_QUERIES`, This Time Against a Worked-Out Model Instead of a Doubling Guess
The v3.50.3 widening (6→12, 4→8, 2→4) was confirmed, across two separate re-benchmark runs (v3.50.5, v3.50.7), to have genuinely helped but not enough — `auto`'s cold p99 dropped from a 10-second spike but stayed multi-second-adjacent, and `conditional`/`conditional_remainder` kept real warm-cache tails that shouldn't exist on a fully-warmed pool. Rather than double the pools again on the same "more options should dilute collisions" intuition that already underperformed once, this pass actually modeled the collision mechanics: with 20 concurrent Locust users, the **expected number of pool entries hit by 2 or more users simultaneously is not monotonically decreasing in pool size** — it's closer to a classic birthday-paradox curve that *peaks* somewhere around pool_size ≈ 10–12 before declining, meaning a small widening from an already-small pool can leave the absolute collision count roughly flat (or, for `conditional_with_remainder`'s 4-entry pool specifically, modeled to get *worse* before getting better).

Concretely worked out (20 users, uniform random pool selection): `conditional_with_remainder` at 4 entries has ~3.9 of 4 entries expected to be hit by 2+ users — essentially total collision, matching exactly what the benchmark kept showing. The peak for all three pools sits around 10-12 entries; meaningful, accelerating decline only resumes well past that. New sizes were chosen to clear that peak, not just to be "bigger than last time": `AUTO_QUERIES` 12→24, `CONDITIONAL_QUERIES` 8→20, `CONDITIONAL_WITH_REMAINDER_QUERIES` 4→12. Every new entry verified directly against `detect_intent()`/`detect_conditional()` before being added, the same discipline the original widening used — none assumed.

`CONDITIONAL_WITH_REMAINDER_QUERIES`'s new entries deliberately reuse several conditions already present in `CONDITIONAL_QUERIES` (e.g. "the back door is unlocked") rather than avoiding overlap — confirmed directly this *helps*, not hurts: both tasks ultimately call `route_with_source()` with the identical extracted condition text as the cache key, so a condition warmed by either task benefits both, the same overlap pattern the original 4-entry pool already relied on.

`app/adversarial_testing.py`'s `CONDITIONAL_SEEDS` updated in lockstep with the 12 new `CONDITIONAL_QUERIES` conditions — `TestSeedVocabularyIntegrity` enforces this directly, the same cross-file dependency already established in v3.50.3.

A genuine, modeled limitation worth stating honestly rather than implying these pools are now "fixed": getting the *expected* colliding-entry count meaningfully below 1 (true elimination, not just dilution) would need pools in the 150-200 entry range at this concurrency level — not realistic to hand-write and individually verify as natural-feeling queries, and it would make the load test itself unwieldy. The new sizes are a genuine, modeled improvement past the worst part of the curve, not a claim that the thundering-herd tail is now fully gone — a real re-benchmark is still the only way to confirm how much this specific change actually moved the numbers.

### Added (Tests)
- `test_locustfile_thundering_herd_pools_are_wide_enough_to_clear_the_collision_peak` in `test_router.py`'s `TestResultCacheThunderingHerd` — enforces the concrete sizing conclusion (not the probability model itself) so a future accidental shrink is caught rather than silently reintroducing the exact tail this pass removed.

### Changed
- Version bumped to 3.50.8

**Total test count: 1262**

---

## [3.50.7]

### Changed — Re-Benchmark: Confirming the `cache_hit` Fix Actually Closed the Gap
A real Locust run on MiniDock (20 users, 120s, cold immediately followed by warm, same methodology as every prior entry), validating v3.50.6's `cache_hit` query-collision fix against the v3.50.5 baseline. Zero exceptions, zero failures on both passes.

**The fix is confirmed working, not just theoretically correct.** `cache_hit`'s cold-cache p90/p98/p99 dropped from 5100ms/8000ms/8000ms (v3.50.5) to 880ms/940ms/940ms — the same shape every other single-source cold-path row in the table shows, no longer the anomalous outlier it was. The warm run lands `cache_hit` at 29ms p99, essentially identical to `kiwix`'s own 34ms — exactly what a never-colliding cache-hit task should look like.

**`uptime` and the v3.50.3 pool widening show the same shape as the v3.50.5 run, as expected** — neither was touched by this release. `uptime` warm p98/p99 this run (440ms/440ms) looks numerically better than the v3.50.5 warm run (850ms/850ms), but the underlying request counts are small enough (21 vs. 29 total) that this is ordinary run-to-run noise, not a second data point about the root cause. Read as confirmation that the v3.50.5 verdict (real, partial fix; real, unexplained minority tail) still stands, not as new evidence either way. Same read for `auto`/`conditional`/`conditional_remainder`: numbers moved in both directions relative to v3.50.5 depending on the specific percentile, consistent with the design doc's own "inherently noisier metric" caution rather than anything having changed.

### Changed
- New dated entry added to `BENCHMARKS.md` (v3.50.7) with full cold/warm tables and explicit per-finding read on what changed, what didn't, and what's just noise
- `wiki/Benchmarks.md` updated: the `cache_hit` paragraph now states the fix is benchmark-confirmed, not just code-confirmed; the top-of-page cold/warm comparison table refreshed to the v3.50.7 numbers; the section heading covering all three findings simplified and renamed (`## What got fully fixed, what got partially fixed, and what's still genuinely unresolved`) now that one of the three is a complete, confirmed fix rather than an open or partial item — every cross-link to the old heading text updated in lockstep (`wiki/Caching.md`, `wiki/Sources.md`)
- Fixed two real, pre-existing issues found while updating these cross-links: `BENCHMARKS.md`'s `## Running benchmarks` heading had been silently dropped during this file's own v3.50.5 edit (a real, embarrassing repeat of the exact double-hyphen-anchor class of mistake this project's own tooling exists to catch — found and fixed here by hand instead, twice, since the first attempt at fixing it accidentally repeated the same mistake before being caught and corrected); and `wiki/Contributing.md` linked to a `Benchmarks` section heading that had already gone stale from an earlier rename and was never updated, found only because this pass touched the same anchor again
- Version bumped to 3.50.7 — no application code changed this release; benchmark records and documentation only

**Total test count: 1255** (unchanged)

---

## [3.50.6]

### Fixed — `cache_hit`'s Real, Surprising 8-Second Cold-Run Tail: a Query Collision, Not a Backend Bug
The v3.50.5 benchmark validation flagged this honestly as an open item rather than guessing at it: `cache_hit`'s cold-cache p90/p99 (5100ms/8000ms) had no precedent anywhere in this file's history, where `cache_hit` has always been one of the cheapest, most boring rows in the table regardless of release — a real anomaly worth running down given a `cache_hit` task spiking into multi-second territory makes no sense on its face.

**Root cause, found by reading `tests/locustfile.py` directly rather than the timing numbers alone**: `cache_hit`'s task used the literal query `"what is nitrogen"` with `source="kiwix"` — which was also the *first entry* in `KIWIX_QUERIES`, the pool the separate, much more frequent `kiwix_search` task (weight 4, the highest-weighted task in `MnemolisSingleSourceUser`) draws from at random. On a cold run, both tasks can independently draw the identical, not-yet-cached `kiwix:what is nitrogen` key at nearly the same instant.

This was never a Mnemolis backend bug. `_resolve_single_source()`'s check-cache → call-handler → write-cache sequence in `app/router.py` has no per-key lock or in-flight-request deduplication — confirmed directly by reading the function, not inferred. Two concurrent callers for the same uncached key both genuinely miss, both pay the full cold-routing cost (a real LLM book-selection call, consistent with the observed multi-second tail and `app/llm.py`'s 10-second client timeout), and both eventually write the same result to cache. This is the exact same thundering-herd shape already documented for `auto`/`conditional`'s small Locust query pools (see the v3.44.0/v3.50.2/v3.50.3 entries) — it just hadn't previously been recognized as applying to `cache_hit` too, since `cache_hit`'s whole purpose is to *never* be exposed to a cache miss at all, and nobody had checked whether its own query happened to collide with another task's pool.

**Fixed at the actual root cause — the query pool, not the caching mechanism.** `cache_hit` now uses its own dedicated query (`CACHE_HIT_QUERY = "what is the boiling point of tungsten"`), confirmed via a direct AST-based parse of every list literal in `tests/locustfile.py` to not appear in any other pool. The old `"what is nitrogen"` stays in `KIWIX_QUERIES` (now 9 entries, down from 10) for the regular `kiwix_search` task. No application code changed — this is a load-test-only fix, the same category as the v3.50.3 pool widening.

### Added (Tests)
- `TestResultCacheThunderingHerd` (3 tests) in `test_router.py` — a direct reproduction against `route_with_source()` itself, not the Locust benchmark (which can only show a symptom, never isolate a cause): two concurrent threads calling the identical uncached source+query pair both genuinely pay the full slow-handler cost (proving the actual gap, not just citing it), a contrast test confirming an *already*-warm key is never recomputed by a concurrent caller (proving this is a narrow race-window bug, not a general cache-correctness problem), and a direct regression test parsing `tests/locustfile.py`'s real list literals via `ast` to confirm `cache_hit`'s query never re-collides with any other pool in the future. The `ast`-based approach mirrors `TestSeedVocabularyIntegrity`'s existing pattern in `test_adversarial_testing.py` (which solved the identical problem for `CONDITIONAL_QUERIES`/`CONDITIONAL_SEEDS`) for the same reason: `locustfile.py` imports `locust` at module level, which isn't in `requirements.txt` and isn't installed by CI — a real import attempt was confirmed to fail two different ways inside an already-running pytest session (a `ModuleNotFoundError` when `locust` isn't installed at all, and a genuine `RecursionError` inside `ssl.SSLContext.minimum_version` when it is, caused by `gevent`'s `monkey.patch_all()` re-patching `ssl` after `requests`/`urllib3` elsewhere in the suite have already imported it — `locust` itself warns about exactly this ordering hazard). Parsing the file as text needs neither `locust` nor any of its monkey-patching, and is arguably the more correct tool for "do these two static lists overlap" regardless of the import hazard.

### Changed
- `tests/locustfile.py` — `cache_hit` task now uses `CACHE_HIT_QUERY` instead of a literal string shared with `KIWIX_QUERIES`; `KIWIX_QUERIES` itself unchanged in spirit (still 9 real, distinct Kiwix queries), just no longer also double-booked as `cache_hit`'s key
- Version bumped to 3.50.6

**Total test count: 1255**

---

## [3.50.5]

### Changed — Re-Benchmark: Validating the Persistent Uptime Kuma Connection and the v3.50.3 Pool Widening Together
A real Locust run on MiniDock (20 users, 120s, cold immediately followed by warm, the same methodology every prior entry uses), validating v3.50.4's persistent Uptime Kuma connection and v3.50.3's `AUTO_QUERIES`/`CONDITIONAL_QUERIES`/`CONDITIONAL_WITH_REMAINDER_QUERIES` pool widening together against the same v3.50.2 baseline already in `BENCHMARKS.md` — one clean pass rather than two separate sessions with a shifted baseline between them, per the design doc both changes shared.

**The persistent-connection fix is real and substantial, but not a complete fix — exactly the outcome the design doc's own pre-written success criteria were built to detect, not retrofit.** `uptime`'s warm-cache p95/p99 dropped from 1500ms (v3.50.2) to 470ms/850ms — a genuine 2-3x improvement, with the large majority of individual requests in both the cold and warm runs landing in the same 22-32ms range every other warm source shows. But the design doc's stated bar was "low tens of milliseconds, matching other sources," and a result still in the hundreds of milliseconds — even much-improved hundreds — means the fix didn't capture the entire mechanism. A second, genuinely surprising result: `uptime`'s *cold*-cache numbers also dropped substantially (1900ms → 500ms p95/p99), which the design doc explicitly flagged in advance as worth a second look if it happened, since the persistent-connection fix should only change *subsequent* calls, not the app's first-ever connection. Neither the remaining warm tail nor the unexpectedly-improved cold numbers have a confirmed root cause. Candidates not yet investigated: event-wait timing inside `uptime_kuma_api`'s own `_get_event_data()` polling loop (confirmed present during the v3.50.4 library-source read), `_connection_lock` contention under 20 concurrent users, or genuine server-side response variance independent of connection reuse. Not overclaimed as fixed — recorded as a real, partial, measured improvement with an honest remaining gap, the same discipline this project applied to the query-expansion concurrency fix's "not a full elimination" framing.

**The v3.50.3 pool widening helped, but likely needs more headroom for 20 concurrent users.** `auto`'s cold p99 dropped from the v3.50.2 single-sample spike (10000ms) to 3800ms — a real improvement — but `auto`'s own cold p98 (1300ms) and `conditional`/`conditional_remainder`'s cold p98/p99 (5100ms and 4300ms respectively) are all still squarely multi-second, and both `conditional` and `conditional_remainder` still show a real warm-cache tail (p95 ~440ms on both) that a fully-warmed pool shouldn't have. Per the design doc's own stated reading: this pattern means the widening (6→12, 4→8, 2→4) wasn't enough, not that the thundering-herd explanation was wrong. Also flagged per the design doc's own caution: this is an inherently noisier metric on a single 120-second run than the connection fix's own result — worth a second run before concluding the pools specifically need more headroom rather than treating this one sample as definitive.

**`/health`'s concurrency fix (v3.50.3) confirmed holding under real load, not just mocked test conditions.** Warm-cache `/health` max this run: 1152ms, p99 1200ms — no recurrence of the v3.50.2 baseline's 5244ms sequential-stacking signature. This run wasn't set up to isolate the fix the way `TestHealthConcurrentSourceChecks` already does at the unit level, but it's a real, supporting data point.

**A genuinely new, unrelated finding, not called for by either change this run was set up to validate**: `cache_hit`'s *cold*-cache p90/p99 (5100ms/8000ms) has no precedent anywhere in this file's prior history, where `cache_hit` has always been one of the cheapest, most boring rows in the table regardless of release. The warm-cache run immediately after shows `cache_hit` back to its expected ~24ms/36ms median/p99, confirming this is specific to the cold pass. Not investigated as part of this run — flagged honestly as an open item for a future pass rather than folded into either of the two real conclusions above.

### Changed
- New dated entry added to `BENCHMARKS.md` (v3.50.5) with full cold/warm tables and an explicit statement of whether each of the two success criteria from the design doc was met for each of the two changes being validated
- `wiki/Benchmarks.md`'s narrative summary rewritten: the "first real, testable hypothesis" framing for `uptime` is now "confirmed real and substantial, but not complete," and the pool-widening framing now reflects the same "real, partial improvement" read rather than an open question with no data
- `wiki/Caching.md` and `wiki/Sources.md`'s persistent-connection language corrected from an unqualified "fixed" to the honest, measured "real, substantial improvement, not a complete one" — both now point to `wiki/Benchmarks.md`'s full account
- Version bumped to 3.50.5 — no application code changed this release; documentation and benchmark records only

**Total test count: 1252** (unchanged — this release is benchmark validation and documentation only)

---

## [3.50.4]

### Fixed — `uptime`'s Recurring Benchmark Tail: a Persistent Socket.IO Connection, Not a TTL Change
The v3.50.3 entry deliberately left `uptime`'s warm-cache tail (reproduced across v3.17.0, v3.44.0, and v3.50.2) as an unconfirmed hypothesis rather than guess-fixing it by raising `CACHE_TTL_UPTIME_SECONDS`. `CACHE_TTL_UPTIME_SECONDS` (60s, the only source TTL shorter than the 120-second benchmark window) explains why a cache miss happens during a run — it never explained why that one miss costs 1.5-1.9 real seconds. That second question needed an actual read of `app/sources/uptime_kuma.py`'s connection mechanism, not just the TTL math.

**Confirmed directly by reading the file**: `search()` opened a brand-new Socket.IO connection, logged in, read data, and disconnected — on every single call, with no persistent connection or pooling. This isn't a rare, benchmark-only cost: `snapshot_uptime()` in `app/snapshots.py` calls this exact same `search()` function every 2 minutes via the background scheduler, independent of whether anyone is ever asking Mnemolis a live `uptime` question — the real-world rate of fresh connect/login cycles is the scheduler's 2-minute interval, not just live query cache misses.

Two options were considered and rejected before settling on the real fix. Raising `CACHE_TTL_UPTIME_SECONDS` would trade a deliberate design choice (uptime status staying close to real-time — a stale "all services up" is actively misleading in a way a stale weather forecast isn't) for hiding a cost that's still there underneath; it wouldn't make any individual connect/login cycle faster, just less frequent. Lowering `UPTIME_KUMA_TIMEOUT_SECONDS` bounds the failure case, not the success case — the observed 1.5-1.9s tail is a *successful* connection that's simply slow, not a near-timeout.

**The real fix**: a persistent Uptime Kuma connection, reused across calls and managed by `app/main.py`'s existing `lifespan()` — the same established pattern already used for the snapshot scheduler and the MCP session manager, not a new resource-lifecycle convention. `get_connection()` returns the shared connection, creating and logging in only on first use or after a confirmed-dead connection; `disconnect()` cleanly closes it on app shutdown via `stack.callback()`, mirroring `scheduler.shutdown()` a few lines below it. The connection is warmed once during startup (before the scheduler starts, so the scheduler's own immediate startup `snapshot_uptime()` call finds it already live) rather than paying the first connect cost on whichever request happens to arrive first.

**A real assumption from this fix's own design doc was wrong, and confirmed wrong before any code shipped, not after**: the doc's pseudocode checked `_persistent_api.connected`, flagging this explicitly as unverified and naming reading the actual installed library source as the required first step. Doing exactly that (`python3 -c "import uptime_kuma_api, inspect; print(inspect.getsource(uptime_kuma_api.UptimeKumaApi))"`) confirmed `UptimeKumaApi` has no `.connected` property of its own — the real liveness signal is its underlying `sio` attribute's `.connected`, a genuine public attribute on `python-socketio`'s `BaseClient` (`True` after a successful connect, `False` after any disconnect). Reading the library source also confirmed two more things the design doc treated as open questions: `UptimeKumaApi.__init__()` already calls `self.connect()` itself, so constructing the object performs the handshake — no separate `.connect()` call is needed; and `get_monitors()`/`get_heartbeats()` both read from `self._event_data`, populated by push events the server sends automatically after login and continuously thereafter (every real heartbeat gets pushed live to a persistent connection) — not two additional independent request/response round-trips on top of login, consistent with the design doc's "one handshake + one login exchange" correction to the v3.50.3 changelog's "real, slow-but-successful Socket.IO round-trip" framing.

A genuinely new failure mode this design introduces, absent from the old fresh-connection-every-time approach: a persistent connection can go stale (an Uptime Kuma restart, a dropped network socket) in a way a fresh-every-time connection structurally cannot. `get_connection()` checks `sio.connected` before reuse and transparently reconnects if the existing connection is dead, rather than silently reusing it in a broken state.

`search()`'s public contract is unchanged: same signature, same `not_configured` message, same `"Could not connect to Uptime Kuma: {e}"` failure format every existing test (and `fusion._looks_empty()`'s fallback detection) already depends on. `UPTIME_KUMA_TIMEOUT_SECONDS` keeps its exact existing meaning; `CACHE_TTL_UPTIME_SECONDS` is untouched.

### Added (Tests)
- `TestPersistentConnection` (6 tests) in `test_uptime_kuma.py` — proves the actual properties this fix introduces, not just unchanged output: connection reuse across two sequential calls (`UptimeKumaApi`'s constructor called exactly once, `get_monitors`/`get_heartbeats` still called fresh each time), dead-connection detection and replacement (a connection whose `sio.connected` goes `False` between calls gets discarded and replaced, not silently reused), genuine thread-safety under real concurrent calls (8 threads calling `search()` at once still construct exactly one connection), `disconnect()` actually closing a live connection and leaving the module ready for a fresh one afterward, and a safe no-op when `disconnect()` is called with no connection ever established. One test (`test_get_connection_uses_sio_connected_not_a_nonexistent_api_connected`) is a deliberate regression guard for the wrong-attribute assumption above — confirmed it actually catches that exact mistake by temporarily reintroducing it and watching the dead-connection test fail before restoring the fix.
- `TestUptimeKumaLifespanIntegration` (2 tests) in `test_main.py` — confirms `get_connection()` genuinely gets exercised during app startup when `UPTIME_KUMA_URL`/`UPTIME_KUMA_USERNAME` are configured (and `disconnect()` runs on shutdown, not before), and confirms neither gets called at all when uptime is left unconfigured — the same graceful-disable behavior every other optional source already has.
- Existing `TestUptimeKumaStatus`/`TestUptimeKumaConfigurableTimeout` tests updated for the new mocking shape (`mock_api.sio.connected = True` instead of the old `__enter__`/`__exit__` context-manager mocks, since `search()` no longer uses `with UptimeKumaApi(...) as api:`) — every original assertion preserved exactly, only the mock construction changed to match the real new call pattern.

### Changed
- `wiki/Sources.md`'s `uptime` section and `wiki/Caching.md` updated to note the connection is now persistent across calls, not opened fresh each time
- Version bumped to 3.50.4 — also corrects `app/main.py`'s FastAPI `version=` string, which had drifted to a stale `3.50.1` (the changelog itself was already correctly at 3.50.3; this was a separate, pre-existing string nobody had re-synced)

**Total test count: 1252**

---

## [3.50.3]

### Fixed — `/health`'s Seven Source Checks Now Run Concurrently, Not Sequentially
The first real Locust benchmark run since v3.44.0 (see `BENCHMARKS.md`) surfaced a fresh, previously-undocumented finding: a warm-cache `/health` sample hit `5244ms`, several times worse than its own `750ms` median. `_check_kiwix()`, `_check_forecast()`, `_check_news()`, `_check_web()`, `_check_uptime()`, `_check_ha()`, and `_check_llm()` were plain sequential calls in `app/main.py`'s `health()` endpoint, each a real network request with its own 3-5 second timeout — one or two genuinely slow real checks (the LLM ping reaching across to a separate machine; a slow SearXNG `/healthz`) could stack additively into a multi-second worst case.

This is the same "sequential where it could be concurrent" shape already fixed elsewhere in this codebase (`fusion.py`'s multi-source dispatch, `searxng.py`'s query-expansion chain, `_resolve_conditional()`'s condition/remainder split) — `/health` just never got the same treatment, since it was never on anyone's hot path the way search queries are.

Fixed with the same `ThreadPoolExecutor` pattern `fusion.py` already established, genuinely simpler here since every `_check_*` function already catches its own exceptions internally and never raises — none of `fusion.py`'s `as_completed()`/exception-propagation handling was needed.

### Added (Tests)
- `TestHealthConcurrentSourceChecks` (3 tests) in `test_main.py` — a real timing-based proof of genuine concurrency (not just that the response shape is unchanged, which a refactor that accidentally stayed sequential could still pass), confirmation a single slow check doesn't block the other six from completing, and confirmation response content is identical regardless of execution order. Both timing tests needed to mock `requests.get` at two separate import sites (`app.main` and `app.sources.kiwix`, since `get_books()` makes its own independent real network call) and needed a genuinely valid, non-empty OPDS feed in the mock response — an empty feed makes `get_books()`'s own real caching check (`if _book_cache: return _book_cache`, falsy for an empty list) re-fetch on every single call rather than ever caching, which would have made the timing assertions measure that separate, pre-existing quirk instead of the concurrency property actually being tested.

### Investigation Note — A Real, Separate, Pre-Existing Quirk Found While Writing the Tests Above, Not Yet Fixed
`get_books()`'s cache check (`if _book_cache: return _book_cache`) is falsy for a genuinely empty list — a Kiwix instance with zero books in its catalog (a fresh install before any ZIM files are added, or a real outage) would never actually cache that empty result, and would re-fetch the full catalog on every single call that touches it, forever, rather than caching the "no books" result the same way a real result gets cached. Found incidentally while building the `/health` concurrency tests above; out of scope for this release, not yet fixed.

### Changed — Real Benchmark Run Against Current Code (First Since v3.44.0)
A full cold/warm Locust run (20 users, 120s, the same methodology every prior entry uses) against the current v3.50.1 codebase — the last real benchmark in `BENCHMARKS.md` was v3.44.0; everything since (the config-completeness audit, Adversarial Self-Testing's full build-out, Cross-Source Temporal Pattern Detection, and the full latency-parallelization investigation) had never been measured under real load. See `BENCHMARKS.md`'s new v3.50.2 entry for the full tables.

**Real, measured improvements, consistent with documented fixes shipped in between:** `web`'s cold p99 dropped from `3900ms` to `1300ms`; `discourse_framing`'s cold p98 dropped from `4200ms` to `2100ms`. Both plausible given the query-expansion concurrency fix and the discourse-framing/fusion-merge fixes, though neither drop's exact magnitude is cleanly attributable to a single documented fix — flagged honestly as directionally consistent, not precisely proven.

**`uptime`'s warm-cache tail reproduced a third time (v3.17.0, v3.44.0, now v3.50.2) — and this run produced the first real, testable hypothesis for it.** Unlike `auto`/`conditional`'s already-explained thundering-herd cache-write collisions (which need a small query pool to collide on), `uptime`'s benchmark task uses one fixed, literal query — there's no pool here, so that explanation can't apply. `CACHE_TTL_UPTIME_SECONDS` (60s) is the only source TTL shorter than the 120-second benchmark run itself, meaning the `uptime` cache entry genuinely expires and gets refetched live mid-run, every run. The observed tail sits comfortably within `UPTIME_KUMA_TIMEOUT_SECONDS`'s 10-second cap, consistent with a real, slow-but-successful Socket.IO round-trip rather than a timeout. **Deliberately left unconfirmed and unfixed** — raising `CACHE_TTL_UPTIME_SECONDS` on the strength of an unconfirmed benchmark-methodology hypothesis would trade a real, deliberate design tradeoff (uptime status staying close to real-time) for a guess; confirming this needs either a direct check of Uptime Kuma's own connection logs during a run, or a diagnostic run with the TTL temporarily raised, neither of which was done this release.

**Real, fresh finding, not previously documented**: `/health`'s own worst-case latency, traced and fixed above.

### Changed — `tests/locustfile.py` Pools Widened, Per a Recommendation Made Twice and Not Previously Acted On
`AUTO_QUERIES` widened from 6 to 12 entries; `CONDITIONAL_QUERIES` widened from 4 to 8; `CONDITIONAL_WITH_REMAINDER_QUERIES` widened from 2 to 4 — diluting the thundering-herd cache-write-collision odds the v3.44.0 benchmark entry already identified and recommended fixing, reproduced again (worse: a full `10000ms` p99) in this release's run. Every new entry verified directly against `detect_intent()`/`detect_conditional()` before being added, not assumed — one initial candidate (`"check if everything is running"`) was caught resolving to the wrong source (`kiwix`, not `uptime`) and replaced before being committed.

**A real cross-file dependency caught by the existing test suite, not missed:** `app/adversarial_testing.py`'s `CONDITIONAL_SEEDS` must have a matching entry for every `CONDITIONAL_QUERIES` entry in `locustfile.py` — `TestSeedVocabularyIntegrity` enforces this directly, specifically to keep the two corpora from silently drifting apart, and correctly failed when the widening above landed without a matching `CONDITIONAL_SEEDS` update. Fixed by adding the four new condition fragments there too.

The widened pools have not yet been re-benchmarked — confirming whether this actually reduces the collision rate, rather than just moving it around, is the natural next run.

### Changed
- Version bumped to 3.50.3

**Total test count: 1244**

---

## [3.50.2]

### Changed — Wiki Restructured: Reference-First Pages, Dev-Blog-Style Design History
The wiki had accumulated a real, genuine problem across many releases: bug-discovery narrative was interleaved sentence-by-sentence with the actual mechanism reference on most pages, making it hard to read a page for "how does this work today" without wading through "here's how we found out it didn't." Every page in the wiki was re-read and restructured on the same principle: reference content first, real bug history either condensed into a same-page "Development Notes" section at the bottom, or — when a story had its own real arc worth reading independently — promoted to a new, dedicated saga page.

**Five new dedicated saga pages**, each pulling fully-told investigation narrative out of the reference pages it used to live inside:
- **The Fusion Merge Bugs** — the three-bug same-source merge chain, the `[FUSION — FUSION]` double-header bug, and the mixed-speed timeout crash, extracted from `Fusion.md`
- **The MCP Transport Migration** — rewritten with the corrected chronology after a changelog cross-check found an ordering mistake: an external community audit triggered the SSE→Streamable HTTP migration; the session-manager caching bug was found and fixed *during* that same migration, before it ever shipped; a real client connecting for the first time *afterward* found two more bugs (a doubled `/mcp/mcp` endpoint path, LAN connections rejected by DNS-rebinding protection) that no in-process test could have caught
- **The Caching Concurrency Investigation** — synthetic Adversarial Self-Testing traffic silently polluting real caches, and the separate file-write race found while investigating it
- **The Latency Parallelization Investigation** — the single most cross-page-scattered story in the project's history, consolidated into one page told in the order it actually happened, including the real regression one fix shipped with and the second fix that avoided repeating it
- **The Adversarial Testing Production Bugs** — the complete record of everything Adversarial Self-Testing has found in Mnemolis itself: the four-bug fusion-merge chain (from the production-discovery side, cross-linked to the merge-logic mechanism page rather than re-told), the false positive and the regex bug underneath it, the Uptime Kuma timeout, and the investigation that ended without a root cause

**Fifteen existing pages** gained a same-page "Development Notes" section, with narrative trimmed out of the reference text above it: `Sources`, `Routing`, `Query-Decomposition`, `Conditional-Query-Detection`, `Fusion`, `LLM-Client`, `Home-Assistant-Integration`, `Multi-Book-Fusion`, `Kiwix-Disambiguation`, `Snapshot-Engine-and-Changes`, `Caching`, `Confidence-Aware-Fusion`, `Configuration-Reference`, `Health-and-Observability`, `First-Time-Setup`.

**Real gaps found and closed that the wiki had never actually documented**, surfaced by a full, deliberate re-read of `CHANGELOG.md` against the wiki's existing content rather than trusting the wiki's own prior coverage:
- `home_assistant.py` — an area-scoped query (e.g. "indoor air quality in the living room") silently skipping real `exclude_entity_keywords` filtering that the same unscoped query already correctly applied; the three-bug chain around `binary_sensor`-style motion entity support never being wired into the relevant keywords; a small grammar fix — none of these had a wiki mention before this pass
- `freshrss.py` — 9 of 9 natural phrasings of a general news request ("tell me the news," "give me the headlines," etc.) being misclassified as specific-topic queries
- `Caching` — fusion's cache key applying at the sub-query level too, not just top-level, for a decomposed query whose own clause resolves to internal fusion
- `Kiwix Disambiguation` — the three single-guess prompting strategies tried and discarded before the current multi-candidate architecture was settled on, previously undocumented

**A real duplication found and closed**: `Cross-Source-Temporal-Pattern-Detection.md` told the same motion-tracking gap twice, in two different sections, with slightly different wording each time.

**Programmatic link/anchor verification, run after every single page edit rather than once at the end** — built a slugifier matching GitHub's real heading-anchor algorithm and checked every `[text](Page#anchor)` link in the wiki against it. Caught and fixed real breaks every time a section was moved or a heading was renamed, including the exact double-hyphen em-dash failure mode this verification was specifically built to catch (`[FUSION — FUSION]`-style headings slugify to a single hyphen, not two, since GitHub collapses the punctuation and surrounding whitespace together).

**Home.md re-examined directly** (not just trusted as already-correct) and found three real issues: Core Concepts was ordered Routing-before-Decomposition-before-Conditional-Detection, contradicting the actual pipeline order stated in the pages it links to (conditional detection runs first, then decomposition, then per-piece routing) — reordered to match; the LLM Client entry credited only two of its three real dependents (`Routing`, `Kiwix Disambiguation`) and omitted `Query Expansion` entirely, and none of the three actually linked back to `LLM-Client` despite Home's own description claiming the dependency — fixed both the description and added the three missing cross-links; Backup & Restore's summary said "five data files," stale since `temporal_patterns.db` brought the real count to six.

**Design History reordered to be genuinely chronological**, with an explicit note that it is — it had drifted into extraction-order-this-session rather than actual-event-order.

### Changed — README Corrected Against Actual Code and the Restructured Wiki
A full line-by-line read of `README.md`, with every numeric default in the Configuration table re-verified directly against `app/config.py` (all 64 settings checked; every one was already accurate) and every claim cross-checked against current code or the now-restructured wiki.

**Real, internal README inconsistencies found and fixed:**
- The `GET /backup` endpoint blurb said "four files," while the Backup & Restore section two pages later — correctly — said six. Both now say six.
- Test count claim was stale: 1204, actual current count is 1241.
- Disambiguation candidate count was overstated as a flat "3" in two places (the LLM-routing list and the Kiwix Internal Flow diagram); the actual function generates 2-3, matching the wiki's own more careful phrasing. Both fixed.

**Real errors found outside the README, in files people actually copy and run:**
- `docker-compose.example.yml`'s header comment still documented `http://your-host:8888/mcp/sse` — the pre-migration SSE endpoint, removed back in the v3.19.0 transport migration. Fixed to the real, current `/mcp` Streamable HTTP path.
- `mnemolis_tool.py`'s `MNEMOLIS_URL` default (`http://mnemolis:8000`) and its own description field (which only mentioned port `8888`) were genuinely confusing read together — the default itself is correct (the container's internal port, for same-Docker-network deployments), but nothing explained why it differed from every `8888` reference in the README, which documents the host-mapped port for external access. Description rewritten to state both cases explicitly rather than changing a default that was already right.

**A real documentation gap, not an error**: `FORECAST_TIMEZONE` — a third, genuinely separate timezone setting from `TZ`/`LOCAL_TIMEZONE`, controlling only what timezone Open-Meteo expresses forecast sunrise/sunset times in — had no mention anywhere in the README's Timezone configuration section. Added.

**Debloated**: two Configuration table cells (`UPTIME_KUMA_TIMEOUT_SECONDS`, `SEARXNG_REQUEST_TIMEOUT_SECONDS`) had bug-history framing ("previously a hardcoded 30...") baked directly into the table description, duplicating — less cleanly — what the wiki's own `Configuration Reference` page already separates into a clean reference cell plus a linked Development Note. Trimmed both to state current behavior only.

Checked and confirmed *not* stale, despite initially looking like candidates: the README's seven architecture diagrams (Voice Assistant Flow, Multi-Client Architecture, Snapshot Engine, Source Fusion, Query Decomposition, Conditional Query Detection, Kiwix Internal Flow) were suspected of duplicating the wiki's own per-mechanism diagrams — checked one directly (Query Decomposition) against the wiki's version and found they're complementary, not redundant: the README shows the general decision structure, the wiki shows a real worked trace. Left as-is.

### Changed — Section-Placement Review, and a New Page for a Real Gap That Review Surfaced
A direct question about why `Kiwix`/`Web & News` mechanism pages sit in their own Deep Dive sections rather than `Core Concepts`, and whether `Cross-Source Temporal Pattern Detection` belongs in `Core Concepts` too, led to checking the actual import graph rather than relying on the existing section framing.

**Confirmed correct, with the real reasoning written down for the first time**: `Core Concepts` is genuinely "the per-query pipeline every (or almost every) query passes through" — Sources, Conditional Detection, Decomposition, Routing, LLM Client, Fusion, Caching, Timezone Conversion. The three Kiwix pages and two Web & News pages are correctly excluded — `_score_result()`, `_get_disambiguation_candidates()`, `_pick_books_with_llm()`, and `_fuse_multi_book_results()` are genuinely Kiwix-internal, confirmed directly: nothing outside `kiwix.py` calls any of them. `Adversarial Self-Testing` and `Cross-Source Temporal Pattern Detection` are correctly excluded too, for a different, real reason: both are background jobs on the same `apscheduler` infrastructure as the snapshot engine, running on a timer independent of whether anyone ever queries Mnemolis at all — not steps in a query's own path.

**One real, genuine miss found in that same review**: `kiwix.py` exports two module-level constants — `DISCOURSE_FRAMING_PATTERNS` and `_STOP_WORDS` — that `router.py` imports directly and depends on for logic with nothing to do with whether a query ever reaches Kiwix at all (the discourse-framing routing bias in `Routing`, and the meaningful-content filter in `Query Decomposition`), and `adversarial_testing.py` depends on the same pattern list for its own seed vocabulary. This is the identical shape of cross-cutting dependency already correctly called out for `LLM-Client` — but it had no cross-links in the direction that matters (from `Routing`/`Query-Decomposition` back to where the shared list actually lives) until this pass. Added: a link from `Routing`'s discourse-framing section to [The Discourse-Framing Investigation](https://github.com/immortalbob/Mnemolis/wiki/The-Discourse-Framing-Investigation#a-real-deliberate-single-source-of-truth), a parallel link from `Query Decomposition`'s meaningful-content filter (confirmed it's the exact same `_STOP_WORDS` set, not a separate router-local copy), and a new note in `Home.md`'s `Core Concepts` section naming the dependency explicitly so a reader isn't surprised by it later.

**A real, substantial gap found while checking that import graph**: `kiwix.py`'s own catalog-discovery and article-fetching mechanism — `_fetch_catalog_page()`, `get_books()`, `refresh_catalog()`, `_search_book()`, `_fetch_article()` — had zero wiki coverage anywhere, despite all three existing Kiwix Deep Dive pages silently assuming "your catalog" and "a search result" already exist. Real, substantial mechanism sitting underneath all three: OPDS feed pagination with a real termination condition, the exact field used to extract a book's full versioned name, a real security hardening (`defusedxml` instead of the standard library's XML parser, specifically to reject entity-expansion attacks), and two further previously-undocumented real bugs — a CSS-selector-vs-tag-name bug that meant table-of-contents boxes were never actually being stripped from any fetched article despite the code's clear intent, and an unbounded article-fetch retry loop with a real, quantified worst case (up to 59 sequential attempts at a 10-second timeout each, nearly 10 minutes for one request) capped at 5. Both bugs already had a one-clause mention in `Roadmap.md`/`Benchmarks.md` ("broken table-of-contents stripping," "a capped retry loop instead of an unbounded one") but no actual explanation anywhere of what either meant.

**New wiki page**: **Kiwix Catalog & Article Fetching**, added to the Kiwix Deep Dive section ahead of the three existing pages, since it covers what they all assume already happened. Cross-linked from `Kiwix-Disambiguation`, `Kiwix-Scoring`, `Multi-Book-Fusion`, `Sources`, `Health-and-Observability`, `Configuration-Reference` (×2), and the README (×2, including the existing `/catalog/refresh` documentation and the Kiwix Internal Flow diagram's closing cross-reference line).

### Changed
- Version bumped to 3.50.2

**Total test count: 1241** (unchanged — this release is documentation and config-comment corrections only, no application code touched)

---

## [3.50.1]

### Added — `last_flagged_result_excerpt`, Because an Investigation Just Hit a Wall the Schema Itself Built
A real `unexpected_empty` flag (`"vs Python and JavaScript, plus while at work"`, routed to `changes`) got traced as far as production logs and live container state would allow — six separate, real hypotheses checked and ruled out one at a time: the LLM being down (it was, but this query never touches it), a different query's failures bleeding across in the same batch, a time-window edge case, cold-start snapshots, a swallowed exception, a timezone misconfiguration, and stale message text. Every one confirmed negative against real evidence, not assumption. The investigation reached the genuine limit of what was recoverable and stopped — `times_generated` was `1`; it had only happened once, and there was nothing left to check.

The real, lasting finding wasn't a root cause — it was that `adversarial_combinations` recorded *that* a known empty/error phrase matched, but never *what* the actual response text was. Once that one occurrence was gone, no amount of code-reading could recover it.

New `last_flagged_result_excerpt` column (up to 500 characters), populated only when a flag genuinely fires — never on a clean run, so the overwhelming majority of combinations that never need this stay exactly as small as before. Exposed through `GET /adversarial/flagged` and `review_flagged.py`'s own `list`/`list-dismissed` output. Migrates cleanly onto MiniDock's real, already-running database the same way every previous schema addition to this table has.

### Added (Tests)
- `TestFlaggedResultExcerpt` (6 tests) in `test_adversarial_testing.py` — excerpt stored only on a real flag, truncation at 500 chars, exposure through the public API, preservation through a later clean run (the same "don't erase the original evidence" convention `first_flagged_*` already follows), and a real migration test against a reconstructed pre-existing database matching MiniDock's actual schema

### Changed
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated with the full, honest account of the investigation — including that it ended without a root cause, which is itself the real finding
- Version bumped to 3.50.1

**Total test count: 1241**

---

## [3.50.0]

### Fixed — `conditional_with_remainder`'s Condition and Remainder Now Run Concurrently
The other half of the latency-stacking investigation, finished properly this time. `_resolve_conditional()`'s condition and remainder calls used to run as two separate, sequential, blocking `route_with_source()` calls — found via real, live Adversarial Self-Testing latency data, and originally left as a documented, accepted cost on the (re-examined and corrected) reasoning that the surrounding code's unrelated real bug history made any change here too risky.

Re-deriving the actual data dependencies directly: the condition and remainder calls don't depend on each other at all — the same as [Query Expansion](https://github.com/immortalbob/Mnemolis/wiki/Query-Expansion)'s two SearXNG fetches, fixed in 3.49.0/3.49.1. Built on the lesson from that fix rather than relearning it: each task submitted to the executor gets its own `contextvars.copy_context()` call before submission, so `suppress_cache_writes()` correctly propagates into both worker threads (the exact regression 3.49.0 shipped with and 3.49.1 fixed). Only spins up the thread pool when a remainder genuinely exists — a plain `"if X, Y"` query with no trailing conjunction (the more common real-world shape) has an empty remainder and never needed a second call in the first place; that path is byte-for-byte unchanged.

Verified with the same three checks that caught two real bugs in the `web` case before trusting it: a genuine timing proof of concurrency, direct confirmation `suppress_cache_writes()` reaches both worker threads correctly, and confirmation normal caching still works when suppression isn't active. A fourth check — real exception propagation from the remainder thread — was added too, since this case's failure semantics differ from the alternate-phrasing chain's deliberately non-fatal design. All four passed cleanly; no second regression this time.

Verified against realistic timings matching the real flagged query's shape: `2.0s` concurrent versus what would have been `3.5s` sequential.

### Added (Tests)
- `TestConditionalRemainderConcurrency` (5 tests) in `test_router.py` — genuine concurrency timing, the empty-remainder fast path confirmed unaffected, `suppress_cache_writes()` propagation into both threads, normal unsuppressed caching, and real exception propagation

### Changed
- Wiki's [Conditional Query Detection](https://github.com/immortalbob/Mnemolis/wiki/Conditional-Query-Detection) and [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated to reflect this is now fixed, not just found feasible — both real recipe-latency-variance mechanisms this investigation uncovered are now resolved at the root
- Version bumped to 3.50.0

**Total test count: 1235**

---

## [3.49.1]

### Fixed — A Real Regression in 3.49.0's Own Concurrent Fetch Fix
Asked directly afterward whether the *other*, more cautiously-treated parallelization candidate (conditional+remainder's sequential routing) was actually infeasible or just assumed to be. Re-deriving the real data dependencies confirmed it has no more of a dependency problem than the `web` query-expansion case already fixed — and re-examining the original "this code has a real bug history" caution found both real bugs live in unrelated parsing/interpretation logic, not in call ordering.

That re-investigation is what surfaced this: researching whether `ThreadPoolExecutor` actually propagates `contextvars.ContextVar` state into worker threads (confirmed, via official Python docs: it does **not**, by default) led to testing the *already-shipped* 3.49.0 fix directly against this. Found a real, live regression: `suppress_cache_writes()` active in the calling thread was being silently ignored inside the concurrent alternate-phrasing thread in `searxng.py`, meaning a synthetic Adversarial Self-Testing query could leak a real write into the routing cache — precisely the bug `suppress_cache_writes()` exists to prevent, reintroduced by 3.49.0's own fix. Confirmed directly with a failing test before this fix.

Fixed by giving each task submitted to the executor its own `contextvars.copy_context()` call before submission, rather than submitting the functions directly. A first attempt shared one captured context between both tasks, which failed a second, separate way — confirmed via a real `RuntimeError: cannot enter context... already entered` from the test suite itself: a single `Context` object cannot be entered by two threads simultaneously (`Context.run()` is documented as non-reentrant across concurrent execution). Each task needs its own, independently-copied context.

### Added (Tests)
- `test_suppress_cache_writes_genuinely_suppresses_writes_from_the_concurrent_alternate_thread` and `test_normal_unsuppressed_call_still_caches_correctly` in `test_searxng.py` — the real regression, reproduced and confirmed fixed, plus confirmation normal caching still works when suppression isn't active

### Changed
- Wiki's [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching), [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing), and [Conditional Query Detection](https://github.com/immortalbob/Mnemolis/wiki/Conditional-Query-Detection) updated with the full, honest account — including correcting Conditional Query Detection's own earlier reasoning for not parallelizing that case, which conflated "this code has unrelated bug history" with "this specific change is risky." That case is now recorded as likely feasible and not yet attempted, a meaningfully different status than "deliberately rejected" — distinct from being implemented in this release
- Version bumped to 3.49.1

**Total test count: 1230**

---

## [3.49.0]

### Fixed — A Real, Pre-Existing Concurrent File-Write Race in Both Caches
Found while researching whether `web` query expansion's two sequential SearXNG fetches could safely be parallelized — auditing every writer of the routing cache surfaced a real, separate, pre-existing bug unrelated to that question's eventual answer. Both `_save_routing_cache()` and `_save_cache()` persisted to disk with a bare `open(path, "w")` followed by `json.dump()`, with no protection against two concurrent writers truncating the same file at the same time.

This was never a hypothetical risk the parallelization work below would introduce — FastAPI's `/search` endpoint is a synchronous route, so Starlette already runs genuinely concurrent real requests on its own thread pool today, making two near-simultaneous cache saves a real, already-live scenario. Confirmed directly, not just reasoned about: a deliberate 8-writer/8-reader concurrent stress test against the old pattern produced 79,609 JSON corruption errors in two seconds. The real blast radius was bounded (the existing `except json.JSONDecodeError` fallback in `load_routing_cache()` already catches a corrupted file and starts fresh rather than crashing), but silently losing the entire on-disk cache on next restart was still a real, avoidable cost.

Fixed with a new shared `_atomic_write_json()` helper in `router.py`: write to a temporary file in the same directory, then `os.replace()` onto the real target — atomic on POSIX (what this project runs on in production), so the file is always either the complete old version or the complete new one, never a partial write from either side. The identical stress test against the fix: zero errors. Both `_save_routing_cache()` and `_save_cache()` now share this one helper.

### Fixed — `web` Query Expansion's Primary Fetch and Alternate-Phrasing Chain Now Run Concurrently
The actual goal of this investigation, made possible by the fix above: `searxng.py`'s `search()` used to pay for the primary SearXNG fetch, the LLM call behind [Query Expansion](https://github.com/immortalbob/Mnemolis/wiki/Query-Expansion)'s `get_alternate_phrasing()`, and a second SearXNG fetch entirely sequentially — a real, live Adversarial Self-Testing latency flag traced this to roughly 4x the cost of a single fetch on a query that triggered expansion.

The primary fetch and the alternate-phrasing chain have no real data dependency on each other (`get_alternate_phrasing()` only needs the original query text), so unlike [Conditional Query Detection](https://github.com/immortalbob/Mnemolis/wiki/Conditional-Query-Detection)'s own, similarly-shaped sequential cost (left as a documented, accepted tradeoff — see that page), this one was both genuinely parallelizable and, once the file-write race above was confirmed fixed, safe to actually parallelize. Both run concurrently now via a small `ThreadPoolExecutor`, the same pattern `fusion.py` already uses for its own multi-source dispatch — including correctly preserving the primary fetch's specific, real, user-facing timeout message (see "The SearXNG Timeout Lesson") and the alternate chain's existing non-fatal-failure guarantee. Verified against the exact original repro timings: `4.15s` sequential down to `3.04s` concurrent.

### Added (Tests)
- `TestAtomicWriteJson` (6 tests) in `test_cache_persistence.py` — including a real concurrent stress test (6 writer + 6 reader threads) proving zero corruption against the fix
- `TestSearxngConcurrentFetch` (4 tests) in `test_searxng.py` — a real timing-based proof of genuine concurrency (not just unchanged output), the timeout message preserved correctly while the alternate thread is still in flight, and the non-fatal-failure guarantee re-verified under real concurrency
- `TestUptimeKumaConfigurableTimeout` carried forward unchanged from 3.48.9

### Changed
- Wiki's [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching), [Query Expansion](https://github.com/immortalbob/Mnemolis/wiki/Query-Expansion), and [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated with the full investigation and the real, measured fix — the query-expansion latency case is no longer a documented limitation, it's resolved
- Version bumped to 3.49.0

**Total test count: 1228**

---

## [3.48.10]

### Documented — A Second, Real Latency-Stacking Mechanism Found After Clearing Caches and Re-Running
A fresh Adversarial Self-Testing flag after clearing both caches and running a few clean cycles: `nosplit_adjacent_to_real_conjunction` on `"difference between Iran and Israel, and find online"`, `6412ms` vs. a recipe p95 of `2502ms`. Traced to a genuinely different mechanism than the existing, documented conditional+remainder latency cost, despite the same symptom shape — this query never decomposes and resolves to a single source (`web`), no fusion or conditional handling involved at all.

The real cost lives entirely inside `searxng.py`'s `search()`: a primary SearXNG fetch, followed by [Query Expansion](https://github.com/immortalbob/Mnemolis/wiki/Query-Expansion)'s `get_alternate_phrasing()` (a real, blocking LLM completion call), followed by a *second* SearXNG fetch for the alternate phrasing — three sequential round-trips billed as one source's latency whenever expansion actually fires. Reproduced directly with realistic mocked timings at roughly 4x the cost of a single fetch.

Checked whether this is safely parallelizable, since the two fetches have no real data dependency on each other (unlike the conditional+remainder case): `_fetch_searxng()` is a pure function with no shared state, genuinely thread-safe. `get_alternate_phrasing()`'s own routing-cache read/write is the one place giving pause — not because it's known-unsafe, but because this project has real history (`suppress_cache_writes()`'s own design) showing concurrent cache access looks safe until it isn't. Documented as a known, accepted cost for now rather than attempted as a fix, the same call made for the conditional+remainder case — verifying it's actually safe to parallelize is real, separate work this finding didn't set out to do.

Two distinct recipes now have real, legitimate latency-variance mechanisms a single, global `ADVERSARIAL_TEST_LATENCY_OUTLIER_MULTIPLIER` can't tell apart from a genuine anomaly — recorded as a real, now twice-motivated design question (a per-recipe latency baseline) in the known-limitations section, not yet built.

### Changed
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) and [Query Expansion](https://github.com/immortalbob/Mnemolis/wiki/Query-Expansion) updated with the real, traced finding and a corrected account of exactly what the routing cache does and doesn't save on a repeat (skips the LLM call; does NOT skip the second SearXNG fetch, which only the alternate-phrasing *text* is cached, not its results)
- Version bumped to 3.48.10

**Total test count: 1218** (no code changes this release — investigation and documentation only)

---

## [3.48.9]

### Fixed — Uptime Kuma's Connection Timeout Was a Bare, Unconfigurable Literal
The second and last of the two genuinely unresolved Adversarial Self-Testing flags from this whole investigation. `unexpected_empty` on `"if any services are down, let me know right away, as well as lights off"`, latency `30056ms` — the number itself was the clue: `UptimeKumaApi(settings.uptime_kuma_url, timeout=30)` was hardcoded, and `30056ms` is exactly that plus normal overhead.

Confirmed via direct tracing that this is **not a Mnemolis bug** — the Uptime Kuma client connection genuinely timed out, the exception was caught, and Mnemolis correctly returned `"Could not connect to Uptime Kuma: {e}"` rather than hiding the failure. `fusion._looks_empty()` correctly recognized the real `"could not connect"` phrase, and the adversarial check correctly flagged the resulting empty response. Every layer worked exactly as designed.

The real, fixable gap: every other source touched by this project (`SEARXNG_REQUEST_TIMEOUT_SECONDS`, `FUSION_TIMEOUT_SECONDS`) already has a configurable timeout; Uptime Kuma's bare `30` was the one literal left over, despite being a same-LAN service that should respond in well under a second. New `UPTIME_KUMA_TIMEOUT_SECONDS` setting (default `10`), wired directly into the real client call — the documented fallback behavior on a genuine failure is completely unchanged, only how long Mnemolis waits before reaching it.

### Added (Tests)
- `TestUptimeKumaConfigurableTimeout` (3 tests) in `test_uptime_kuma.py` — confirms the new default, confirms the configured value is genuinely passed to the real `UptimeKumaApi` call (not a renamed constant defaulting to the same old value), and confirms the documented fallback message still appears correctly on a genuine timeout regardless of the configured value

### Changed
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated with the full investigation — both genuinely unresolved flags from this round are now real, closed findings, not open questions
- `README.md` and Configuration Reference updated with the new setting
- Version bumped to 3.48.9

**Total test count: 1218**

---

## [3.48.8]

### Fixed — A Real Header-Counting Bug Found While Actually Reviewing the Two Unresolved Flags
After the 3.48.7 undismiss mishap got sorted out, the two genuinely unresolved flags (`unexpected_empty`, `conditional_remainder_missing_sections`) got a real look instead of staying parked. Tracing `conditional_remainder_missing_sections` on `"if it is raining, I will be careful with communication, as well as feeds"` — a response this check claimed had zero real sections — led directly to `_HEADER_PATTERN`, the regex both this check and `_check_multi_intent_part_count` use to count headers in a result string.

The regex required exactly one literal `" — "` separator, with the character class after it deliberately excluding the em-dash itself. `kiwix`'s real label (`"ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW"`) and `news`'s real label (`"RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED"`) both legitimately contain a *second* em-dash — so neither header could ever be matched, full stop, regardless of any threshold setting. Reconstructing the real flagged query's actual merge output confirmed this directly as the root cause.

This also reframes the two real `part_count_mismatch` flags from 3.48.1, which both involved `news` as an intended source: reconstructing a realistic 5-header result including both vulnerable headers showed the regex undercounting by exactly 2 — the precise shape of both flags' literal text (`"intended 5, found 3"`). The 3.48.1 fix (loosening the threshold to "fewer than half survived") was real and still correct, but it made the check tolerant of this exact undercount without ever finding why the undercount existed — this fix is the actual root cause, not a second layer on top of an unrelated one.

Several existing tests for both checks had been using fabricated header text (`"[KIWIX — A]"`) that happened to be *equally* invisible to the same broken regex for an unrelated reason — meaning the tests accidentally agreed with the bug instead of catching it. This is the real, structural reason it survived as long as it did.

**Fixed** by rebuilding `_HEADER_PATTERN` from the real, exact header strings `fusion._format_header()` actually produces (`re.escape()`'d, not a generic bracket-matching character class) — the same safe approach `router.py`'s own `_dedupe_nested_fusion_sections()` already uses for the identical need.

### Added (Tests)
- `TestHeaderPatternMatchesEveryRealHeader` (4 tests) — every real header matched exactly once, the two specifically-vulnerable headers tested explicitly, a real 5-header reconstruction counting correctly, and confirmation fabricated header text is correctly rejected (the fix is a genuine exact-match, not a slightly-widened pattern that could drift again)
- Every existing test for `_check_multi_intent_part_count` and `_check_conditional_remainder_sections` that used fabricated header text rewritten to use real strings from `fusion._format_header()`

### Changed
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated with the real, corrected root-cause story for both the `conditional_remainder_missing_sections` flag and the earlier `part_count_mismatch` flags
- Version bumped to 3.48.8

**Total test count: 1215**

---

## [3.48.7]

### Added — `POST /adversarial/undismiss`, the Real Reversal Dismiss Never Had
Found necessary via real usage on MiniDock, not written defensively up front: a real batch-dismiss review session matched index numbers against a flagged-combination listing fetched a turn earlier, rather than a freshly re-fetched one — the live queue had reordered in between (a new flag had recorded since), so the indices no longer lined up with the current list, and two genuinely unresolved flags (`unexpected_empty`, `conditional_remainder_missing_sections`) got dismissed alongside seven that were actually understood and fixed. There was no way back short of editing the database by hand.

New `undismiss_flagged_combination()` in `app/adversarial_testing.py`, the real, symmetric counterpart to the existing `dismiss_flagged_combination()` — restores `review_status` to exactly `NULL`, the same value a combination has before its first-ever dismissal, not a new third state. New `POST /adversarial/undismiss?fingerprint=...` endpoint, mirroring `/adversarial/dismiss`'s exact contract (404 on unknown fingerprint; a fingerprint that was never dismissed is a safe no-op).

### Added (Tests)
- 6 new tests across `test_adversarial_testing.py`: the real restore-to-default-view round trip, confirming the restored state is exactly `NULL` and not a distinct sentinel, the unknown-fingerprint 404 case, the never-dismissed no-op case, and the full endpoint-level round trip via `TestClient`

### Changed
- `review_flagged.py` (the standalone CLI helper) updated with `list-dismissed`, `undismiss`, and `undismiss-all` commands, and `list`/`dismiss` now always re-fetch the live queue immediately before acting rather than trusting an index from an earlier `list` call — the exact gap that caused the real mis-dismissal this release fixes
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated with the new endpoint and the real story behind why it exists
- Version bumped to 3.48.7

**Total test count: 1210**

---

## [3.48.6]

### Changed — Every Wiki Diagram and Numeric Claim Verified Against Real Code
A second documentation pass, narrower and deeper than 3.48.5's structural audit: every `text`-block diagram across all 15 wiki pages that have one, traced step by step against the actual function it claims to describe, plus a full programmatic cross-check of all 63 real config defaults against both the README and Configuration Reference. Not a re-skim — every diagram was walked against real code with real test queries, not assumed correct because it looked plausible.

**Real, confirmed errors fixed:**

- **`Query-Decomposition.md`** — the combined-split worked example claimed output `["check the weather", "are the lights on"]`; the real code produces `["check the weather and", "are the lights on"]` for that exact input, since `" also "` (6 chars) is tried before `" and "` (5 chars) in the real longest-first ordering and wins the tie. Diagram corrected to show the real output, with an explanation of the (harmless, routing-correct) trailing-conjunction artifact.
- **`Fusion.md`** — the flowchart's final step claimed `fusion.search()` performs a same-source merge. It doesn't, by construction (`valid`'s own dedup set guarantees no duplicate source ever reaches that point) — confirmed directly against the function and its own comment documenting exactly this. Diagram fixed; the real merge (a genuinely different function) is now clearly distinguished.
- **`Conditional-Query-Detection.md`** — the `forecast` interpretable-source bullet implied symmetric rain/storm keyword checking on both the condition and result sides. It's actually asymmetric (`rain`/`raining` only on the condition side; `storm`/`shower` are result-only signals) — confirmed directly with `_interpret_yes_no()`. The `uptime` bullet was also missing the real `"not up"` condition keyword. Both corrected.
- **`Multi-Book-Fusion.md`** — the fusion-decision diagram was missing a real guard (`top_score > 0`, with its own negative-score bug history) and presented the 50%-of-top-score threshold as a fixed constant. It's `KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT`, a real, configurable setting — made configurable specifically because it's the page's own documented "central decision." Diagram and prose both corrected.
- **`The-Discourse-Framing-Investigation.md`** — the bitcoin-example diagram's intermediate search-terms claim (`"what whole bitcoin everyone obsessed"`) and specific score numbers (2 → 32) no longer reproduce against current code (`_build_search_terms()` now correctly strips `"what"` as a stop word; real current scores are different). Rewritten to state the real, current, reproducible search term and to be explicit that specific historical point values are illustrative of the mechanism, not a number to expect byte-for-byte today.
- **`SEARXNG_REQUEST_TIMEOUT_SECONDS`** — documented as `15` in three places (`Configuration-Reference.md` once, `README.md` twice); the real default in `app/config.py` is `10`. This one mattered beyond a cosmetic number: the surrounding advice ("set this to match or exceed SearXNG's own timeout") was undermined by citing the wrong figure, and the real default is now visibly below the `20`-second target [The SearXNG Timeout Lesson](https://github.com/immortalbob/Mnemolis/wiki/The-SearXNG-Timeout-Lesson) itself recommends — both docs now say so explicitly.
- **`README.md`** — 13 real settings (every `ADVERSARIAL_TEST_*` and `TEMPORAL_PATTERN_*` variable) were entirely absent from the config table, despite every other feature area's settings being present there with no "see wiki" deferral pattern anywhere else in the document. Added, using the same descriptions already established in Configuration Reference.

**Verified clean, no changes needed:** `Routing.md`, `Kiwix-Disambiguation.md`, `LLM-Client.md`, `Snapshot-Engine-and-Changes.md`, `The-Recursion-Design-Bug.md`, `The-SearXNG-Timeout-Lesson.md`, `The-Meaningful-Content-Filter-Bugs.md`, `Open-WebUI-System-Prompt-Guide.md` (its specific 3-part decomposition claim re-verified against current code), `Cross-Source-Temporal-Pattern-Detection.md` (including the specific "reuses the original corrected_threshold, never recomputes it" design claim, confirmed directly).

**Minor addition, not a correction:** `Query-Expansion.md` didn't mention that a generated alternate phrasing is itself cached in the routing cache (`altquery:{query}`) — a real, harmless gap, not an error. Added.

A full programmatic check confirmed zero broken internal links and zero broken anchor fragments across the entire wiki, re-run after every edit in this pass, not just once at the end.

### Changed
- Version bumped to 3.48.6

**Total test count: 1204** (no code changes this release — documentation only)

---

## [3.48.5]

### Changed — Full Wiki & README Audit Against Current Code
A systematic pass, not a skim: every real config setting, every endpoint, and every scheduled job cross-checked against what the wiki actually documents, followed by a full re-read of the last 15+ versions of this changelog (3.36.0 through 3.48.4) to catch anything a quick page-by-page check would miss. Settings, endpoints, and most fix-specific documentation updates from that window held up correctly — the project's habit of updating docs alongside fixes mostly worked. Six pages had real, confirmed drift; two real gaps had no dedicated page at all.

**`Routing.md`** — the decision-flow diagram and "discourse-framing bias" section described the bias as applying only to LLM-assisted source selection. As of 3.48.1, it also applies to the keyword-matching path, and a perfectly ordinary keyword match (`"rss"`, `"news"`, dozens of other common `INTENT_MAP` words) can otherwise silently defeat the entire point of detecting discourse framing. Diagram and prose corrected.

**`Query-Decomposition.md`** — "meaningful-content filtering" was described as purely stop-word-based, missing both the real-keyword check added in 3.48.1 and the ordering fix added in 3.48.2 that makes it actually work. Corrected, with a link to the new dedicated bug-history page below.

**`Fusion.md`** — promised a "(see below)" section on same-source merging that didn't exist, and had zero mention of three real, sequential bugs found this same investigation (`_dedupe_nested_fusion_sections()`, `_dedupe_items_across_blobs()`, and `_merge_same_source()`'s actual structural limitation). Added a full new section.

**`The-Discourse-Framing-Investigation.md`** — stated as its confident closing summary that "Black Hole of Calcutta" was the accepted, final outcome for a real query. Directly contradicted by later, more thoroughly verified evidence: the real fix makes the actual astrophysics article win. Corrected, with honest acknowledgment that the saga had two more real chapters after this page's original ending, not the two it looked like at the time.

**`Conditional-Query-Detection.md`** — no mention anywhere of the sequential (not parallel) condition+remainder routing and its real, additive latency cost, previously documented only inside Adversarial Self-Testing's own narrative where someone reading the actual mechanism page would never find it. Added.

**`README.md`** — one stale number: "1161 tests" corrected to the real, current 1204. Notable that there's a *prior* changelog entry documenting this exact line being corrected once before for the same reason — a real, recurring maintenance gap worth naming rather than just quietly fixing again.

### Added — Two New Wiki Pages for Real Gaps With No Dedicated Page

**[The Meaningful-Content-Filter Bugs](https://github.com/immortalbob/Mnemolis/wiki/The-Meaningful-Content-Filter-Bugs)** — the `"is it up"`/`"are they up"` stop-word-only-keyword bug and the `"rss"` length-gate-ordering bug, previously documented only inside Adversarial Self-Testing's narrative. Structurally distinct from [The Proper-Noun-Pair Saga](https://github.com/immortalbob/Mnemolis/wiki/The-Proper-Noun-Pair-Saga) (a different decomposition sub-mechanism), so it didn't belong folded into that page — it gets its own, the same way that saga did.

**[LLM Client](https://github.com/immortalbob/Mnemolis/wiki/LLM-Client)** — there was no dedicated page for `llm.py` at all, despite a real, serious historical bug (3.36.0: every single completion on the OpenAI-compatible path would silently return `None` for any thinking model) being mentioned in exactly one line of the Roadmap's terse summary list and nowhere else. Covers the dual-backend design, the fail-safe contract every caller depends on, and the thinking-model bug in full. A matching `Troubleshooting.md` entry added too, since this is exactly the shape of problem someone would hit on a real deployment and search for by symptom.

### Changed — Home.md Redesigned
Rewritten introduction: a longer, genuinely user-facing description of what Mnemolis actually does and why someone would want it, not just a one-line summary — real example queries, the actual list of backends, the local-first/no-subscription framing, REST+MCP accessibility. "Core Concepts" reordered to better match the real order a query actually flows through (`Sources` → `Routing` → `LLM Client` → decomposition/conditional/fusion). `MCP Server` and `Troubleshooting` moved from Core Concepts/Getting Started into Operations, where "how do I connect to this" and "something's broken" both more naturally belong. Both new pages added in their respective sections.

Every internal wiki link and anchor fragment — not just the ones touched this pass — verified programmatically against GitHub's real slugification rules: zero broken links, zero broken anchors, zero orphaned pages across the entire wiki.

### Changed
- Version bumped to 3.48.5

**Total test count: 1204** (no code changes this release — documentation only)

---

## [3.48.4]

### Fixed — Duplicate Content Inside a Correctly-Deduplicated Section, Found Verifying 3.48.3's Fix
Re-running the exact query from 3.48.3's fix against MiniDock's real stack confirmed that fix worked exactly as designed — a single, correct `[NEWS — ...]` header, not two — but surfaced a fourth, separate bug: the same several headlines still appeared twice inside that one, structurally-correct section's own body.

Root cause: `_dedupe_nested_fusion_sections()` (3.48.3) fixed the structural duplication, but the actual content join — both in `fusion._merge_same_source()` and in `_dedupe_nested_fusion_sections()`'s own header-merge branch — was a plain string concatenation with zero content-level awareness. Two genuinely independent calls to `news.search()` (one nested inside an internal-fusion sub-query, one a separately-decomposed clause's own bare resolution) both legitimately returned overlapping recent headlines, since FreshRSS's own `_is_general_query()` path returns "everything, no filtering" for a broad query — and nothing anywhere deduplicated across the two calls.

A first attempt deduped items *after* the join, by re-splitting the already-merged text on its own item separator — confirmed broken via a failing test: once two blobs are joined with a bare `"\n\n"`, the real boundary between them is no longer reliably distinguishable from an ordinary paragraph break within either blob's own content, so a later split can silently merge two items into one and miss a real duplicate. Fixed properly by moving the dedup *before* the join instead: a new `fusion._dedupe_items_across_blobs()` helper runs at the one point where the boundary between the two original results is still completely unambiguous, removing any item from the second blob whose leading `**Title**` line exactly matches one already in the first (exact match only, never fuzzy). Both join sites now also use the real `"---"` item separator (not a bare double-newline) when joining genuinely multi-item content, so the merged result's own visual structure stays as clean as the dedup logic needs it to be.

### Added (Tests)
- `TestDedupeItemsAcrossBlobs` (6 tests) in `test_fusion.py` — the real overlapping-headlines scenario, complete overlap, non-multi-item no-op, and confirmation `_merge_same_source()` itself now uses the correct separator for multi-item content while leaving plain content's existing behavior untouched
- `test_real_world_regression_case_with_overlapping_headlines` added to `TestDedupeNestedFusionSections` in `test_router.py` — the full real-world shape end-to-end, confirming every distinct headline appears exactly once with clean item separators

### Changed
- Version bumped to 3.48.4
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) extended with this fourth finding, documented as part of the same investigation as 3.48.1–3.48.3's fixes

**Total test count: 1204**

---

## [3.48.3]

### Fixed — Duplicate Section in Merged Fusion Results, Found Verifying the 3.48.2 Fix
Re-running the exact query from 3.48.2's fix to confirm it against MiniDock's real stack — and it worked, the real Black Hole article correctly led the kiwix section — surfaced a third, separate bug in the actual merged answer: a redundant second `[NEWS — ...]` section appeared near the end, duplicating real headlines already shown earlier (`[KIWIX, NEWS, WEB, NEWS]` instead of the correct `[KIWIX, NEWS, WEB]`).

Root cause: once decomposition correctly splits a query into two independent clauses, one clause's own LLM-judged source selection can land on internal fusion (multiple sources sharing one already-headered, nested blob), while a different, separately-decomposed clause resolves to a bare source that happens to be one of the sources already inside that blob. `_merge_same_source()` — which already correctly merges two bare same-source tuples — only ever compares the OUTER tuple label (`"fusion"` vs `"news"`, genuinely different), so it has no way to see a section nested inside the fusion blob duplicates the second, separate tuple. Confirmed real and pre-existing, not a regression from 3.48.1/3.48.2's fixes — already reachable via any query shape where one clause's ordinary LLM judgment happens to overlap with a different clause's source; this recipe's shape just made it reliably reachable instead of needing a rarer coincidence.

Fixed with a second, separate post-processing pass, `_dedupe_nested_fusion_sections()`, run on the final assembled text after the existing tuple-level merge. Splits on the exact, real header strings `fusion._format_header()` produces (`re.escape()`'d, not a generic bracket pattern — confirmed safe against real content containing bracket-like or dash-like text), merges duplicate sections' content while preserving first-occurrence position, and is a true no-op for the overwhelming majority of results that never contain a duplicate at all.

### Added (Tests)
- `TestDedupeNestedFusionSections` (8 tests) in `test_router.py` — the real production scenario, non-adjacent duplicates, 3+ duplicate headers, and a direct check that real content containing literal `[NEWS]` brackets or `---` dashes is never falsely treated as a section boundary

### Changed
- Version bumped to 3.48.3
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) extended with this third finding, documented as part of the same investigation as 3.48.2's fixes

**Total test count: 1197**

---

## [3.48.2]

### Fixed — A Real Answer-Quality Bug a Manual Verification Check Surfaced, Not Adversarial Self-Testing Itself
Manually confirming the 3.48.1 discourse-framing keyword-path fix against a live `/search` call returned a result that was technically correct by every Adversarial Self-Testing check (`source_used: "fusion"`, real `[NEWS — ...]` and `[KIWIX — ...]` sections both present) but had a real, visible answer-quality problem: kiwix returned an unrelated Space StackExchange thread and an unrelated podcast Wikipedia article for `"everyone keeps talking about black holes, and rss"`, never the real Black Hole article. Exactly the kind of thing this feature's own hard rule (never judge correctness) means it can't catch — finding it took a human reading a result, not a structural check — but worth fixing since it traces through the same recipe shape this project already generates synthetically.

Two distinct, real root causes, both upstream of anything already shipped:

**Decomposition failed to split the query, due to a real ordering bug.** `"rss"` — confirmed the only real `INTENT_MAP` keyword that is itself 3 characters or shorter — was discarded by `_filter_meaningful()`'s `if len(p) <= 3: continue` length gate *before* the `_ALL_INTENT_KEYWORDS` check (added in 3.48.1, for `"is it up"`/`"are they up"`) ever got a chance to protect it. Fixed by reordering the keyword/colloquial checks ahead of the length gate. Once decomposition correctly splits `"black holes"` from `"rss"` into two independent clauses, each routes and resolves on its own — kiwix never receives `"rss"` as part of its own search text at all, closing the cross-source pollution problem at the actual root rather than trying to make kiwix defensively robust against arbitrary noise it shouldn't have received in the first place. (A generic "strip every other source's keywords inside kiwix.py" approach was deliberately checked and rejected: confirmed real, legitimate kiwix queries like "tell me about the weather" or "what is google" would have been broken by stripping words that are simultaneously other sources' triggers AND genuine standalone topics.)

**Scoring never used the cleaned text search-term-building already produces.** [The Discourse-Framing Investigation](https://github.com/immortalbob/Mnemolis/wiki/The-Discourse-Framing-Investigation) correctly fixed discourse-framing words polluting the actual Kiwix search query, but `_score_result()` — which ranks whatever comes back — was never updated to match, and still scores against the raw, unstripped query. Confirmed this gap exists even for the *original* bitcoin case the wiki documents as fully fixed (`"everyone"`/`"obsessed"` are still real, counted words in that case's scoring today); it just never visibly changed the outcome there, since the real Bitcoin article's title-overlap signal was strong enough to win regardless. Fixed by stripping discourse framing from the word set used for keyword-overlap scoring specifically — the exact-match check and `_is_definitional_query()` still use the full, original phrasing, since both genuinely need real leading-phrase structure that the discourse strip doesn't touch.

### Added (Tests)
- `TestDecomposeShortKeywordBeforeLengthGate` (7 tests) in `test_router.py` — the real, live regression case plus boundary coverage confirming the reordering can't let trivial filler fragments through
- `TestScoreResultDiscourseFramingWordsExcluded` (5 tests) in `test_kiwix.py` — the real article now winning against both wrong candidates, the original bitcoin case still correctly passing, and `_is_definitional_query()` confirmed unaffected

### Changed
- Version bumped to 3.48.2
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) updated with the full investigation — both root causes, why the generic cross-source-stripping approach was rejected, and why this was found by manual verification rather than the automated checks themselves

**Total test count: 1189**

---

## [3.48.1]

### Fixed — Two Real Bugs Found by Tracing Real Adversarial Self-Testing Production Data
After roughly a day of real runs against MiniDock's actual stack (136 combinations tried, 9 flagged), every single flag was traced to its real root cause — not just the ones that looked interesting. Two were genuine, previously-unknown Mnemolis bugs.

**Discourse-framing escalation never ran on the keyword-match path.** [The Discourse-Framing Investigation](https://github.com/immortalbob/Mnemolis/wiki/The-Discourse-Framing-Investigation) correctly fixed all four real code paths inside `_llm_detect()` — but `detect_intent()`'s own `if source: return source` short-circuited before `_llm_detect()` (and every one of its four fixed paths) was ever reached, for ANY keyword match, single or multi. A real, live flag caught this directly: `"everyone keeps talking about black holes, and rss"` resolved to bare `"news"` in 35ms, kiwix never considered — reproduced and confirmed general, not a one-keyword edge case, since `INTENT_MAP` contains dozens of short, ordinary words that can easily co-occur with genuine discourse framing in a real sentence. Fixed by applying the same, already-existing escalation helpers directly inside `detect_intent()`'s keyword-match branch.

**Two real `INTENT_MAP` keywords made entirely of stop words were silently dropped during decomposition.** `"is it up"` and `"are they up"` (real, literal `uptime` keywords; confirmed the only two of all 113 real keyword phrases with this property) were discarded by `_filter_meaningful()`'s generic stop-word check, which has no awareness of `INTENT_MAP` at all — a clause consisting only of one of these phrases came back with zero real content words and vanished entirely from the decomposed result, not even folded into a neighboring clause. Fixed the same way `_COLLOQUIAL_PHRASES` already handles a structurally identical problem: a real keyword phrase now always counts as meaningful, closing the general case (any future all-stop-word keyword), not just these two by name.

### Fixed — A Real False Positive in Adversarial Self-Testing's Own `part_count_mismatch` Check
Tracing a third flag (5 intended intents, 3 headers) found a false positive, not a Mnemolis bug: decomposition and per-part routing were both correct; the 2 "missing" sources legitimately and correctly returned empty results, which `route_with_source()` deliberately drops before merging with no trace left behind. An exact-count comparison fundamentally cannot tell "legitimate empty results" apart from "real content loss" — both produce an identical signature.

Loosened the check from an exact-count-with-tolerance comparison to **"fewer than half of the intended sources produced any header at all"** — loose enough to never fire on ordinary empty-result variance across this recipe's real range (3–5 intended sources), while still catching a genuine large-scale collapse with the same shape as the original proper-noun-pair bug 5 this check exists to catch. As a direct, deliberate consequence, `ADVERSARIAL_TEST_PART_COUNT_MISMATCH_TOLERANCE` has been removed entirely — there's nothing left to tune; the new threshold is a fixed, principled rule, not a per-deployment knob. This also fixes a separate, worse blind spot the old check had for free: its `n_headers > 0` guard meant a complete collapse to zero headers could never be flagged at all, regardless of intended count — the new check correctly flags that case.

### Changed
- Version bumped to 3.48.1
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) rewritten with a full account of every flag from this real production run, including the false positive and the structural (non-bug) latency finding below
- New wiki section documenting conditional+remainder queries' sequential (not parallel) routing as a known, accepted, structural latency characteristic — deliberately not changed to a concurrency model, since the same conditional-handling code has already had two separate, carefully-reasoned bug fixes and a parallelization change would introduce real new risk for a payoff that's the less common case in the real data so far
- `ADVERSARIAL_TEST_PART_COUNT_MISMATCH_TOLERANCE` removed from `app/config.py` and both configuration wiki pages

**Total test count: 1177**

---

## [3.48.0]

### Added — Shared Groundwork: Local-Timezone Conversion and Read-Only `query_log.db` Access
Pre-work for two not-yet-built design docs (Predictive Pre-Fetching with Confidence Calibration, Self-Healing Source Selection Through Reinforcement, Ambient Intent Disambiguation Through Context) that each independently identified the same two real, missing pieces of shared infrastructure during their own research. Built once, here, rather than letting two or three separate features each invent their own version.

**`app/timeutil.py` — UTC-to-local-time conversion.** Every database timestamp this project writes (`query_log.db`, `snapshots.db`, `adversarial_testing.db`, `temporal_patterns.db`) is hardcoded UTC. Meanwhile, `_hours_since()` (`app/router.py`, resolving "this morning"/"while at work") already has a real, working, *separate* notion of local time, sourced entirely from the container's own `TZ` environment variable, with no connection to anything in `app/config.py`. Any feature needing to bucket a stored UTC timestamp by local hour-of-day or day-of-week was about to either invent a third, independent mechanism, or — far worse — silently bucket by raw UTC hour-of-day, which is only correct for a deployment physically in the UTC zone. For this project's own real reference deployment (Kingman, AZ — `America/Phoenix`, UTC-7, no DST), that mistake would have silently shifted every time-of-day bucket by exactly 7 hours, forever, with no error anywhere.

New setting: `LOCAL_TIMEZONE`, defaulting to read the same `TZ` environment variable `_hours_since()` already implicitly depends on — a deployment that's already correctly set `TZ` per the README gets this conversion capability for free, at zero new configuration cost. An explicit `LOCAL_TIMEZONE` always overrides `TZ`, for the rare case where they should genuinely differ.

Built on the standard library's `zoneinfo` (Python 3.9+, already available, no new dependency) rather than a naive fixed-offset calculation — confirmed directly via dedicated tests that DST transitions are handled correctly and automatically (`America/New_York` converts UTC noon to 7am in January, 8am in July, both correct). An invalid timezone name (a real, plausible typo) falls back to UTC with a logged warning rather than crashing, the same defensive judgment `morning_start_hour`'s own `% 24` fix already applies to a different setting; a malformed stored timestamp raises loudly rather than being silently swallowed, since every real row this project's own databases ever write is already in the correct format, and a malformed one indicates a real bug in whatever wrote it.

**`router.py` gains its own independent, read-only connection to `query_log.db`.** `query_log.db`, `_LOG_DB`, and `_log_query()` all live in `app/main.py`; `router.py` had zero existing access to any of them. Two of the three pending design docs need `router.py` to read recent query history directly. The new `get_recent_queries()` connects via SQLite's own `?mode=ro` URI — a real, enforced read-only connection, not just a documented convention — confirmed directly with a dedicated test that a write attempt against it fails immediately with `sqlite3.OperationalError`, and that the read-only mode never silently creates a missing database file as a side effect of being asked to read from it. Deliberately a direct connection by file path, never an import from `app.main`, since `main.py` already imports from `router.py` — the reverse direction would be a genuine circular import, the same class of problem `app/sources/fusion.py`'s own docstring already names as the reason a different shared function lives there rather than in `router.py`.

### Fixed (incidental, found during this work)
A real test-isolation bug in this release's own first draft of `tests/test_timeutil.py`: testing `local_timezone`'s default-resolution behavior (evaluated once at class-definition time) initially used `importlib.reload(app.config)`, which creates a brand-new `Settings` class and module-level `settings` object — but every other already-imported module in this codebase (confirmed: essentially all of them) did `from app.config import settings`, binding that object reference at import time. After the reload, those modules' own `settings` name kept pointing at the stale, pre-reload object, silently breaking 8 unrelated tests in `test_uptime_kuma.py` that only failed when run after these new tests in the same process — caught directly by running the full suite, not just the new file in isolation, the same discipline this project's own README and Contributing page already call for. Fixed by loading `app/config.py` as a genuinely separate module instance via `importlib.util.spec_from_file_location()`, under a different name, never touching `sys.modules['app.config']` at all.

A second, smaller correction, found immediately after this release shipped while updating the three pending design docs (Predictive Pre-Fetching, Self-Healing Source Selection, Ambient Intent Disambiguation) that this release's groundwork affects: `get_recent_queries()`'s own docstring claimed both Self-Healing Source Selection and Ambient Intent Disambiguation "want a small, bounded, most-recent-first window" — true for the latter, not actually true for the former, which needs a time-bounded bulk scan with real outcome columns instead. Corrected in both the function's own docstring and its test class's docstring (`app/router.py`, `tests/test_router.py`) to state this precisely rather than repeat an unverified assumption about a consumer that doesn't exist yet. No behavior change — `get_recent_queries()`'s actual implementation was always correct for its real, intended use (Ambient Intent); only the docstring's claim about a second use case was wrong.

### Changed
- Version bumped to 3.48.0
- README's "Timezone configuration" section updated to document `LOCAL_TIMEZONE`'s relationship to `TZ`

**Total test count: 1161**

---

## [3.47.3]

### Fixed — Real GitHub Actions CI Failure: Cross-Test Cache Pollution in `test_cache_persistence.py`
A genuine CI failure, reported directly from a real GitHub Actions run of `tests.yml` (`pip install -r requirements.txt` + `pytest tests/ -v`, no special flags or environment): `TestLoadCache::test_no_file_starts_fresh` failed with a real, leftover entry — `'kiwix:a real concurrent user query'` — sitting in `app.router._cache` when the test asserted it should be empty.

Root cause: `app.router._cache` and `_routing_cache` are plain module-level dicts, shared across the entire pytest process. Several test classes — some predating this release entirely, spread across `test_router.py`, `test_routing_cache.py`, `test_fusion.py`, `test_cache_persistence.py`, and the two new tests added in 3.47.2 (`TestFullCycle::test_cycle_does_not_pollute_real_cache_with_unmocked_routing` and `test_real_user_query_unaffected_by_concurrent_adversarial_suppression`) — write real entries into one or both dicts and never restore prior state afterward. Each individual test passes in isolation; the bug only surfaces when a *different* test, running later in the same process, depends on either cache being empty.

This had been silently masked, for the entire life of this project so far, by a mundane but real environmental fact: any dev machine or CI runner that has ever started the app for real leaves a `cache.json`/`routing_cache.json` file on disk, and `load_cache()`/`load_routing_cache()` reset the in-memory dict to whatever that file contains as a side effect of successfully parsing it — incidentally cleaning up leftover pollution from a prior test, purely by accident. A genuinely fresh GitHub Actions checkout has no such file, so `load_cache()`'s early-return path (taken specifically when the file doesn't exist) leaves whatever a prior test left behind completely untouched — confirmed directly by reproducing the exact two-test sequence (`test_real_user_query_unaffected_by_concurrent_adversarial_suppression` followed by `test_no_file_starts_fresh`) with `/app/data` genuinely absent, which deterministically reproduces the exact reported failure, byte-for-byte matching cache key included.

Fixed with a new `tests/conftest.py`: a single `autouse=True` fixture that snapshots and restores both `_cache` and `_routing_cache` around **every** test in the suite, regardless of file or class — closing the entire bug class at the root rather than hand-patching each affected class's own `setup_method`/`teardown_method` individually (a real fix for the classes found this round, but one that just reintroduces the same risk for the next test someone adds without remembering the same discipline). A test that already manages this state correctly itself is unaffected; the fixture's own save/restore is simply a redundant, harmless second safety net in that case.

Also restored `.github/workflows/`, `.github/dependabot.yml`, `.gitignore`, and `.dockerignore` to the repository — all four confirmed missing from every tarball produced since the 3.47.0 release, an oversight in archive packaging unrelated to the cache-pollution bug itself, but the reason the original report's CI failure couldn't initially be reproduced against the shipped tarball. `.gitignore`/`.dockerignore` were found in a second pass after the `.github` restoration prompted a fuller audit of the original archive's top-level file listing against what had actually been shipping; a stray `.ruff_cache/` directory (a local lint-run artifact, never meant to be packaged) was found and removed in the same pass.

### Added (Tests)
- `tests/conftest.py` — the autouse fixture described above
- Direct hand-verification (not a committed test, since it duplicates existing coverage once the fixture is in place) that the exact two-test sequence from the original failure report passes deterministically with the fix applied

### Changed
- Version bumped to 3.47.3

**Total test count: 1138** (unchanged from 3.47.2 — this release fixes test isolation, not test coverage)

---

## [3.47.2]

### Fixed — Adversarial Self-Testing's Synthetic Queries Were Silently Polluting the Real Result and Routing Caches
Found via a deliberate cross-check while researching an unrelated design doc, not a reported failure. `run_adversarial_test_cycle()`'s own docstring claimed it "never touches cache.json, routing_cache.json... or any real user-facing state" — confirmed directly, with an unmocked call, that this was false: `route_with_source()` writes to both the result cache and routing cache as an unconditional side effect of any successful query, synthetic or real, several calls deep inside `_resolve_single_source()` and `_llm_detect()`/`_llm_pick_fusion_sources()`. A single synthetic adversarial query really did land in the real, in-memory `_cache` dict, and would have persisted to `cache.json` on the next batched disk save. `test_cycle_never_touches_real_cache_files` could never have caught this — it mocks `route_with_source()` out entirely, proving only "if this function doesn't run, nothing else here touches the cache files either," not the actual claim the test is named for.

Fixed with a new `router.suppress_cache_writes()` context manager, which `run_adversarial_test_cycle()` now wraps its real `route_with_source()` call in. `_set_cached()`/`_set_routing()` both check it and no-op if set.

**Deliberately built on `contextvars.ContextVar`, not a plain module-level boolean** — a plain flag would have been a real, not theoretical, new bug: `BackgroundScheduler` runs Adversarial Self-Testing on its own thread pool, genuinely concurrent with FastAPI's request-handling threads, and a real live request's legitimate cache write landing in the same window a plain global flag was set would have been silently dropped too — strictly worse than the bug being fixed. `ContextVar` is thread-local (and task-local under asyncio) by construction; verified directly with a real two-thread test that a concurrent live request is completely unaffected by suppression active on another thread for an overlapping window.

### Added (Tests)
- `test_cycle_does_not_pollute_real_cache_with_unmocked_routing` — the actual regression test for the found gap: runs the real cycle with `route_with_source()` genuinely unmocked (only the underlying source handler and LLM call are stubbed) and confirms the real `_cache`/`_routing_cache` dicts are empty afterward
- `test_real_user_query_unaffected_by_concurrent_adversarial_suppression` — two real threads, proving a live request's cache write survives a concurrent, overlapping adversarial-testing suppression window
- `TestSuppressCacheWrites` (5 tests) in `test_cache_persistence.py` — direct unit coverage for the new context manager itself: basic suppression for both caches, resumption after the context exits, exception-safety (the flag must reset even if the suppressed code raises), and correct behavior under nested suppression calls

### Changed
- Version bumped to 3.47.2
- Wiki's [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching) updated with a new section explaining the found gap and the fix, including why `ContextVar` was required over a plain flag

**Total test count: 1138**

---

## [3.47.1]

### Fixed — Cross-Source Temporal Pattern Detection: Motion Events Were Never Actually Extracted
Code review (not a reported failure) caught a real, significant gap: `snapshot_ha()` always captured motion/window/opening device-class sensors, but `_iter_ha_entity_changes()` only ever had branches for `lock`, `door`, and `battery` — meaning the feature's own headline, lead-sentence example ("does a front-door lock event reliably precede a motion event") was never actually testable. Confirmed directly: a real motion "off"→"on" transition produced an empty event list.

Fixed by extending the shared comparison core with the missing branches — door logic generalized to cover `door`/`window`/`opening` together (same real binary open/closed semantics), plus a new, separate motion branch that deliberately only reports the "off"→"on" detection edge, not the reverse — the reverse transition is the sensor settling back to rest, not a new, independently meaningful occurrence.

**A second, related bug was found while fixing the first one, before either ever ran against real data:** `extract_ha_events()`'s event-type labeling only ever distinguished "lock or door" from "everything else, assumed to mean battery_low" — so the newly-added motion/window/opening kinds silently fell into the battery-event branch and were mislabeled `:battery_low` instead of their own correct labels. Fixed by making every kind explicit, with a real, future-proofing change: an unhandled kind now raises rather than silently falling through to a wrong label, so a future new kind added to the shared comparison core without a matching update here fails loudly instead of mislabeling itself.

### Fixed — A Real Discrepancy Between the Changelog and the Shipped Test Suite
3.47.0's changelog described the false-positive validation as "stress-tested... 30 independent random seeds... zero candidates in every single trial" — but the actual committed test ran exactly once, with a single fixed seed. The underlying statistical claim was independently re-verified and confirmed true (30 real, independent trials, zero false positives), but nothing in the shipped code would have caught a future regression across that broader range, and the specific provenance described wasn't backed by anything repeatable. Rewritten as a genuine 30-seed loop, so the claim is true of what's actually committed and re-checked on every test run.

### Fixed — Two Lint Errors
A dead import (`collections.Counter`, never used) and a dead local variable in the hand-rolled Poisson tail-probability function (`_poisson_sf`) — cosmetic, the underlying math was independently verified correct against `scipy.stats.poisson.sf` across a real range of inputs before either fix was applied.

### Added (Tests)
- 4 new tests in `test_snapshots.py`: window-opened detection, motion-detected detection, confirmation that the reverse motion transition correctly produces no event, alongside the pre-existing door/lock/battery coverage
- 2 new tests in `test_temporal_patterns.py`: confirming `extract_ha_events()` correctly labels window and motion events, not the `:battery_low` mislabel the first attempted fix introduced

### Changed
- Wiki's [Cross-Source Temporal Pattern Detection](https://github.com/immortalbob/Mnemolis/wiki/Cross-Source-Temporal-Pattern-Detection) updated: the event-type table now accurately includes motion/window/opening, with an honest note about the gap and fix; a third entry added to the "real bug found during development" section, explicitly noting this one was caught by review rather than development-time testing
- `_diff_ha()`'s own docstring corrected to mention window/opening sensors and the motion detection-edge-only behavior
- Version bumped to 3.47.1

**Total test count: 1131**

---

## [3.47.0]

### Added — Cross-Source Temporal Pattern Detection
A background job, on the same `apscheduler` infrastructure the snapshot engine and Adversarial Self-Testing already run on, that mines structured event history for reliable timing relationships between event types — does a front-door lock event reliably precede a motion event within some lag window, does an HA event reliably precede a service outage — and reports anything that survives a real statistical bar as a candidate, never a causal claim. This was the roadmap's "🔬 Speculative" entry; it's no longer speculative.

Scope is deliberately narrow for this first version: `ha`-internal event pairs (lock/door/battery transitions against each other) and `ha`-to-coarse-`uptime` pairs only. `forecast`/`news` event extraction is explicitly out of scope — neither source's current snapshot shape supports clean event typing without new, separate groundwork.

The statistical core, summarized (full reasoning, with citations, in the new wiki page):
- **Non-overlapping occurrence counting** — once a real B has been claimed as the match for some A, that same B can never be claimed twice. A naive "every A within range of every B" count would badly overstate how often a relationship actually fires.
- **A hard minimum-occurrence floor** (`TEMPORAL_PATTERN_MIN_OCCURRENCES`, default 5) — checked *before* any significance test runs. A pair with 2-3 raw occurrences isn't a pattern yet regardless of what the math would say.
- **Bonferroni-corrected significance testing** — the per-comparison threshold gets divided by the total number of (A, B) pairs actually tested in a given pass, against a null-hypothesis expected count built from each event type's own real, observed base rate (not an assumed uniform one).
- **Mandatory out-of-sample re-validation** — a `candidate` pattern is mechanically re-checked against a later, non-overlapping window of new data before it can ever be promoted to `confirmed`. A pattern that fails to replicate is marked `unconfirmed`, not deleted — a real, honest finding in its own right, the same "status changes, history doesn't disappear" philosophy already established by Adversarial Self-Testing's dismiss mechanism.

Every single returned pattern — `candidate`, `confirmed`, or `unconfirmed` — carries the literal note *"This reflects observed timing correlation only and does not establish a causal relationship."* directly on the row, not just in documentation. This feature lives only in its own dedicated endpoint, never blended into `GET /changes` or a normal search response — a caveat that important is too easy to lose once folded into an ordinary conversational answer.

New endpoints: `GET /temporal-patterns` (optional `?status=candidate|confirmed|unconfirmed` filter), `POST /temporal-patterns/trigger`. New `/health` field: `temporal_pattern_detection`, with one genuinely new status beyond the usual `ok`/`stale`/`never_ran`/`disabled` set — **`insufficient_data`**, reported when the job ran correctly but the real event volume in its window was below the floor needed to test even one pair. This is the honest, expected state for the first weeks of this feature's life on any real deployment, distinct from `ok` (a real result against a real amount of data, even if that result is "found nothing").

New settings: `TEMPORAL_PATTERN_DETECTION_ENABLED` (default `true`, checked at both scheduler-registration time and inside the cycle function itself, mirroring `ADVERSARIAL_TEST_ENABLED`'s exact defense-in-depth precedent), `TEMPORAL_PATTERN_MINING_INTERVAL_HOURS` (default 24), `TEMPORAL_PATTERN_LAG_WINDOW_MINUTES` (default 30), `TEMPORAL_PATTERN_MIN_OCCURRENCES` (default 5), `TEMPORAL_PATTERN_SIGNIFICANCE_LEVEL` (default 0.05), `TEMPORAL_PATTERN_VALIDATION_WINDOW_HOURS` (default 24), `TEMPORAL_PATTERN_STALE_GRACE_MULTIPLIER` (default 3).

New backup file: `temporal_patterns.db` (event history and mined pattern candidates) — `_BACKUP_DATA_FILES` is now six entries, not five.

Event extraction for `ha` is built directly on `_iter_ha_entity_changes()` — a new shared comparison core factored out of `_diff_ha()` itself, so the new structured-event extractor and the existing free-text "what changed" diff output can never independently drift apart from each other. `_diff_ha()`'s own external behavior is completely unchanged by this refactor; every pre-existing test for it still passes against the exact same inputs/outputs.

Full design rationale, the mining/validation diagrams, and the exact statistical reasoning behind every threshold: wiki's [Cross-Source Temporal Pattern Detection](https://github.com/immortalbob/Mnemolis/wiki/Cross-Source-Temporal-Pattern-Detection).

### Fixed — Two Real Bugs Found During Development, Before Ever Running Against Production Data
Both caught the same way most of this project's real bugs get caught: deliberately testing a harder, more realistic scenario than the simple case that already passed.

- **Non-overlapping counting silently dropped real occurrences.** The first draft of the occurrence counter correctly prevented a single B from being double-claimed by advancing its scan position to just past whichever B got claimed — but that also silently skipped over any genuine, not-yet-evaluated A occurrences sitting between the claiming A and the B it claimed. A burst of 3 A's followed by 3 B's, all within the lag window, returned a count of 1 instead of the correct 3. Fixed by tracking which B *indices* have already been claimed in a separate set, and scanning every A exactly once regardless of what any earlier A claimed.
- **Uptime event misclassification from a shared substring.** `_diff_uptime()`'s own recovery message ("All services restored — previously reported outage resolved") and its own pending message ("Service check pending — possible outage starting") both genuinely contain the literal substring `"outage"`. An early version of `extract_uptime_events()` checked for `"outage"` before checking the more specific `"pending"`/`"restored"` phrasing, misclassifying both as plain outages. Fixed by matching each message's own distinct, unambiguous leading phrase instead of a substring more than one real message type happens to share.

Both bugs are now permanent regression tests, not just inline comments — `TestCountNonoverlappingOccurrences` includes the exact burst scenario plus eight further adversarial variants (shared-B claims, directionality, mixed in-range/out-of-range pairs), and `TestExtractUptimeEvents` locks in the corrected classification for every real message `_diff_uptime()` can produce.

### Changed
- Version bumped to 3.47.0
- Roadmap's "🔬 Speculative" section retired — its one entry shipped and moved into "Battle Testing & Operational Maturity — complete"
- Wiki: new [Cross-Source Temporal Pattern Detection](https://github.com/immortalbob/Mnemolis/wiki/Cross-Source-Temporal-Pattern-Detection) page; [Configuration Reference](https://github.com/immortalbob/Mnemolis/wiki/Configuration-Reference), [Health & Observability](https://github.com/immortalbob/Mnemolis/wiki/Health-and-Observability), [Backup & Restore](https://github.com/immortalbob/Mnemolis/wiki/Backup-and-Restore), and [Home](https://github.com/immortalbob/Mnemolis/wiki/Home) updated for the new feature
- README's Project Structure listing also corrected to include `adversarial_testing.py`/`test_adversarial_testing.py`, which had drifted out of sync with the actual repo before this release, alongside the new `temporal_patterns.py`/`test_temporal_patterns.py` entries; the long-stale "1012 tests" line corrected to the real, current count

### Statistical validation, not just unit tests
Beyond ordinary unit coverage, the mining procedure was stress-tested against synthetic data specifically built to check the claim this feature's whole design rests on: a purely random, mutually-independent event stream (8 event types, realistic per-type volumes, 30 independent random seeds) produced **zero false-positive candidates** in every single trial — confirming Bonferroni correction is doing real, load-bearing work rather than just being present as inert code. A complementary synthetic test with one genuinely reliable signal planted inside a pool of unrelated noise correctly found the real pattern in **30/30** trials with no spurious extras from the noise.

**Total test count: 1126**

---

## [3.46.2]

### Fixed — Adversarial Self-Testing: Real Flag-Visibility Gap
Code review (not a reported failure) caught a real, meaningful gap the previous design had explicitly documented as deliberate: `last_flagged_reason` got overwritten to `NULL` whenever the same fingerprint happened to be re-rolled and ran clean, and since the dedup logic only checked "have I seen this at all" (not "was it flagged"), a previously-flagged combination could silently vanish from `GET /adversarial/flagged` with no human ever reviewing or dismissing it — a real risk specifically for intermittent anomalies (a flaky latency outlier, a transient bug that doesn't reproduce on every run).

"Currently flagged" and "ever flagged" are now genuinely separate, tracked facts. New columns: `ever_flagged` (sticky, never auto-resets), `first_flagged_reason`/`first_flagged_timestamp` (the original anomaly, preserved across any number of later clean runs), `review_status`. `GET /adversarial/flagged` now returns the union of currently-flagged and ever-flagged-but-not-dismissed rows by default, with `currently_flagged` exposed explicitly so a human can still tell "still broken right now" apart from "flagged once, currently clean, still needs a look." New endpoint: `POST /adversarial/dismiss?fingerprint=...` — the actual human action that closes a review out; history is never deleted, and a genuinely new flag on a previously-dismissed fingerprint correctly resurfaces it.

A real schema migration handles every already-deployed 3.46.x database (including the live one on MiniDock, two real days of history) — `init_adversarial_db()` adds the four new columns via guarded `ALTER TABLE` and backfills `ever_flagged`/`first_flagged_*` for any row already flagged before this fix shipped, so no history is lost on upgrade.

Writing this fix surfaced two more real bugs in its own first draft, both caught by failing tests rather than found by inspection: a migration-ordering bug (an index was created on the new `ever_flagged` column before the column itself existed on a pre-existing table, raising `no such column: ever_flagged` on every real already-deployed database — the migration order was corrected), and a missing `review_status` reset (a dismissed combination that got a genuinely new, different flag later stayed permanently invisible, since nothing cleared the earlier dismissal — `_record_result()` now clears `review_status` back to `NULL` specifically when a new flag fires on an already-flagged row).

### Fixed — Two Smaller Issues From the Same Review
- A dead code comment referencing `expand_seed_vocabulary()` — a function that doesn't exist anywhere in the codebase. Corrected to clearly state this is a not-yet-built follow-up, not something already wired in.
- A factually wrong wiki claim: the "part-count mismatch under fallback" known limitation stated a header-less fallback result "always reads as 1 header." Traced directly against `_HEADER_PATTERN`: a header-less string produces zero matches, not one, and `_check_multi_intent_part_count`'s own `n_headers > 0` guard already excludes that case correctly. There was no real limitation here — the claim has been removed from the wiki rather than corrected to a different wrong one, with two new regression tests locking in the correct behavior (including the real, narrower case — a genuine partial fallback with at least one header present — that DOES still correctly flag).

### Changed
- Version bumped to 3.46.2
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) rewritten to document the actual fixed flag-tracking design and the new endpoint, with the corrected known-limitations section

**Total test count: 1076**

---

## [3.46.1]

### Added — Adversarial Self-Testing: Real On/Off Switch and Tunable Thresholds
The first real run against MiniDock's actual Kiwix/SearXNG/Ollama stack came back clean (8/8, zero flags) — see the wiki page for what it generated. Follow-up based on direct feedback: every threshold that was previously a hardcoded constant in `adversarial_testing.py` is now a real setting, plus a genuine master enable/disable switch.

- **`ADVERSARIAL_TEST_ENABLED`** (default `true`) — checked at both scheduler-registration time in `main.py`'s lifespan AND inside `run_adversarial_test_cycle()` itself (defense in depth), so a direct call — including the new `POST /adversarial/trigger` — can never accidentally run real queries against the LLM/SearXNG/Kiwix backends while the feature is supposed to be off. `/health`'s `adversarial_testing` field reports `{"status": "disabled"}` directly when off, rather than eventually reading as `"stale"` — a deliberate off-switch shouldn't look like a job that silently stopped running.
- **`ADVERSARIAL_TEST_LATENCY_OUTLIER_MULTIPLIER`** (default `1.5`), **`ADVERSARIAL_TEST_LATENCY_OUTLIER_FLOOR_MS`** (default `1000`), **`ADVERSARIAL_TEST_LATENCY_OUTLIER_MIN_SAMPLES`** (default `10`) — previously hardcoded; the real first run showed legitimate cache-driven variance (276ms vs. 2028ms on the same recipe) that different hardware will see differently
- **`ADVERSARIAL_TEST_PART_COUNT_MISMATCH_TOLERANCE`** (default `2`) — previously hardcoded

### Added — `POST /adversarial/trigger`
Manually run one cycle immediately rather than waiting for the next scheduled tick — mirrors `/snapshots/trigger`'s exact pattern, and removes the need for the `docker compose exec ... python3 -c "..."` workaround used to force the very first verification run. Returns a real summary (`queries_run`, `flagged`) rather than a bare 200.

### Changed
- Version bumped to 3.46.1
- Wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing) and [Configuration Reference](https://github.com/immortalbob/Mnemolis/wiki/Configuration-Reference) updated with all five new settings, the new endpoint, and the real first-run data from MiniDock

**Total test count: 1061**

---

## [3.46.0]

### Added — Adversarial Self-Testing
A background job, on the same `apscheduler` infrastructure the snapshot engine already runs on, that generates structurally-novel queries by combining Mnemolis's own real ingredient vocabulary (`router.INTENT_MAP`, `router._CONJUNCTIONS`, `router._NOSPLIT_PATTERNS`, `kiwix.DISCOURSE_FRAMING_PATTERNS`), runs each through the real `route_with_source()` pipeline, and flags structural anomalies for human review. Institutionalizes the adversarial megaquery testing approach that found most of the bugs documented in the wiki's Design History, instead of relying on someone constructing a nasty test sentence by hand.

Generation is pure-Python combinatorics — seven recipes, zero LLM calls in the hot path — fingerprint-deduplicated so repeated cycles bias toward never-seen ingredient combinations before falling back to a repeat. Every anomaly check verifies a documented Mnemolis behavioral guarantee against what the real pipeline actually did (does a multi-intent query produce a matching number of result sections, does a discourse-framing query actually keep kiwix in the result, does the result match a known empty/error phrase) — never a correctness judgment about response content, the same distinction that made an LLM-as-judge approach a non-starter for this exact problem shape in published research.

New endpoint: `GET /adversarial/flagged` — every currently-flagged combination, most recent first. New `/health` field: `adversarial_testing`, same `ok`/`stale`/`never_ran` shape as `snapshot_jobs`. New settings: `ADVERSARIAL_TEST_INTERVAL_MINUTES` (default 60), `ADVERSARIAL_TEST_BATCH_SIZE` (default 8). New backup file: `adversarial_testing.db` (synthetic generated queries only, never real user data) — `_BACKUP_DATA_FILES` is now five entries, not four.

Full design rationale, the seven recipes, and a real false-negative bug the discourse-framing check caught in itself during its own unit testing: wiki's [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing).

### Changed
- Version bumped to 3.46.0
- Wiki's Roadmap, Backup & Restore, Configuration Reference, and Health & Observability pages updated to reflect the new feature
- README's Backup & Restore section updated to "five files," matching the new file count

**Total test count: 1049**

---

## [3.45.0]

### Investigation Note — A Config-Completeness Audit, After Battle Testing and Bulletproofing
With the complexity-investigation campaign and the bulletproofing pass both complete, a different kind of review: systematically searching every file in `app/` for hardcoded values a real homelab deployment might genuinely want to tune, rather than hunting for bugs. Deliberately left out of this audit's additions: LLM `max_tokens` values (internal sizing for a specific prompt, not a real user preference), the 3-disambiguation-candidates count in `kiwix.py` (tightly coupled to the actual prompt wording — changing the count without rewriting the prompt would produce inconsistent behavior), and `home_assistant.py`'s minute/hour/day formatting thresholds (structural facts about time, not deployment preferences).

### Fixed — A Real Gap That Directly Undermined Existing Documentation
`searxng.py`'s client-side request timeout was hardcoded at 10 seconds, while the README's own documented fix for `"Error reaching SearXNG: connection failed"` tells people to raise SearXNG's own server-side `max_request_timeout` to 20 seconds for genuinely slow engines. Following that documented advice exactly as written wouldn't have fully worked — Mnemolis's own client would have cut the connection at 10s regardless of how generously SearXNG itself was configured to wait. Now configurable via `SEARXNG_REQUEST_TIMEOUT_SECONDS` (default 15), with the README's own SearXNG timeout section updated to say so explicitly.

### Added — 16 New Configurable Settings
Found hardcoded with no way to adjust them, despite several being presented in the README/wiki as deliberate, reasoned design choices:

- **`SEARXNG_REQUEST_TIMEOUT_SECONDS`** (default 15) — see above
- **`KIWIX_ARTICLE_MAX_CHARS`** (default 3000) — per-article truncation before scoring/fusion ever sees it, distinct from `FUSION_MAX_CHARS_PER_SOURCE`'s post-merge truncation
- **`KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT`** (default 0.5) — the actual, central "should a second book be fused in" decision, documented in the README/wiki as the real mechanism but previously impossible to tune
- **`WEB_NEWS_RAW_RESULT_BUDGET`** (default 25) — the scoring pipeline's input budget, distinct from `WEB_NEWS_TOP_N`'s output cap
- **`QUERY_EXPANSION_MIN_WORDS`** (default 3) — matches the README's documented "3+ words" trigger, which was previously just a fact, not a setting
- **`SNAPSHOT_STALE_GRACE_MULTIPLIER`** (default 3) — `/health`'s staleness-alerting sensitivity
- **`ROUTING_CACHE_TTL_SECONDS`** (default 3600) — presented as a deliberate default in the wiki's Caching page, previously not actually adjustable
- **8 per-source result cache TTLs** (`CACHE_TTL_KIWIX_SECONDS`, `CACHE_TTL_FORECAST_SECONDS`, `CACHE_TTL_NEWS_SECONDS`, `CACHE_TTL_WEB_SECONDS`, `CACHE_TTL_UPTIME_SECONDS`, `CACHE_TTL_HA_SECONDS`, `CACHE_TTL_CHANGES_SECONDS`, `CACHE_TTL_FUSION_SECONDS`) — each independently configurable now rather than one shared hardcoded dict

`config.py` itself now documents this audit's own scope and exclusions directly in the `Settings` class docstring, and groups every setting by what it actually controls rather than the order it happened to be added in.

### Changed — Documentation
- README's config table updated with all 16 new settings, grouped to match `config.py`
- README's SearXNG timeout section updated to mention the new client-side setting explicitly
- Wiki's [Configuration Reference](https://github.com/immortalbob/Mnemolis/wiki/Configuration-Reference) updated with all 16 settings, each in its matching existing section
- Wiki's [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching) page's per-source TTL table and routing cache TTL description both updated to reference the real, now-configurable env var names instead of presenting the values as fixed facts

### Changed
- Version bumped to 3.45.0

**Total test count: 1012**


---

## Earlier history

Everything before v3.45.0 — the original feature build-out, the complexity refactor campaign, the MCP transport migration, and the full battle-testing/bulletproofing era — is preserved in full, unedited, in [`CHANGELOG-ARCHIVE.md`](CHANGELOG-ARCHIVE.md).
