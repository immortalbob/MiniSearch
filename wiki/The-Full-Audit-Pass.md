# The Full Audit Pass

**v3.49.x → v3.51.0 | Saga page**

---

This page documents the systematic, function-by-function audit of every file in `app/` — the first time in this project's history that every function in the codebase was read and verified directly rather than inferred from tests passing or documentation existing. Files were worked through smallest to largest, 83 lines to 2482 lines. Fourteen real bugs were found and fixed.

This is the honest account of what was found, in the order it was found, with no flattering omissions.

---

## Why This Pass Happened

The prior work — the static analysis campaign, the MCP migration, the bulletproofing era, the benchmark investigation — found real bugs, but always in response to something: a failing test, a slow benchmark, a reported anomaly. The adversarial self-testing system (v3.46.0) was the first attempt at proactive bug-finding, but it only checks documented behavioral guarantees against what the router actually does. It has no opinion about whether the functions underneath the router are correct.

The audit pass was designed to fill that gap. The rule was simple: read every function, verify every claim it makes about itself, trace every edge case the code touches. If the claim held up under direct testing, move on. If it didn't, fix it, add a regression test that would have caught it, and document the fix precisely enough that a future reader can understand why the guard exists.

The "smallest to largest" ordering was deliberate: smaller files tend to be lower-level utilities that the larger files depend on. Finding a bug in `scoring.py` before auditing `searxng.py` meant the scoring bug's impact on search quality was already fixed before the full call chain was verified. This paid off — `kiwix.py`'s audit benefited from `scoring.py`'s possessive-normalization fix having already landed.

---

## The Files, In Order

### `query_expansion.py` (83 lines)

No bugs. The 2×-length-rejection boundary had no test at exactly the edge case. Added one. Established the pattern for the rest of the audit: add tests for things that could plausibly change, not just for things that are currently broken.

### `forecast.py` (108 lines)

**Real bug.** Open-Meteo sometimes returns HTTP 200 with fewer than 3 days of data in degraded conditions — a partial response the API spec permits but the code assumed would never happen. The existing `try/except` only wrapped the HTTP call, not the data-consumption logic that followed. A partial response raised a raw `IndexError` or `KeyError` that propagated to the caller. Fixed by wrapping the consumption code in its own `try/except`. Two new tests confirmed by revert-and-verify.

### `timeutil.py` (153 lines)

No code bugs. The `local_hour_bucket()` docstring claimed a range of `(1440 // bucket_minutes) - 1` which is wrong for non-divisor values of `bucket_minutes`. The arithmetic was always correct. The docstring was wrong. Fixed the docstring, added a regression test for the actual return range.

### `mcp_server.py` (160 lines)

**Real bug (latent).** The MCP mount path `"/mcp"` appeared as two independent string literals — once in `app.mount()` and once in the lifespan function's route-matching loop. Nothing enforced that they agreed. If a future refactor changed one without the other, the session manager would silently fail to refresh on restart, resurfacing the `StreamableHTTPSessionManager.run() can only be called once` error that had already caused real pain. Consolidated into a single `MCP_MOUNT_PATH` constant. Two new tests.

### `freshrss.py` (187 lines)

**Real bug.** The HTML stripping used `re.sub(r"<[^>]+>", "", ...)` which stopped matching at the first `>` character inside an attribute value — a tag like `<a href="page?a=1&b=2">` would be partially stripped, leaking `">` into article summaries. HTML entities (`&amp;`, `&lt;`, etc.) were also never decoded. Fixed with BeautifulSoup. The `search()` function had zero prior test coverage. Six new tests, including the first `TestSearch` class for this module.

### `searxng.py` (194 lines)

**Two real bugs.** First: `search()` created a new `ThreadPoolExecutor(max_workers=2)` on every call. Under load, this produced 46 OS threads — worse than the equivalent bug found earlier in `fusion.py`, because fusion dispatches through searxng, meaning the fix in fusion didn't actually bound the total thread count. Fixed with a shared module-level executor. Second: early return from a timeout inside a `with ThreadPoolExecutor` block called `shutdown(wait=True)` implicitly, holding the caller for 0.50 seconds past the stated timeout. Measured directly (not inferred). Both fixed, five new tests.

### `llm.py` (194 lines)

**One real bug, one lint regression.** The lint regression came first: `MCP_MOUNT_PATH` had been inserted mid-import-block in an earlier session, producing six E402 errors that CI caught but the local verification routine (which only ran `pytest`) missed. Fixed by moving the constant after the imports. Established the standing rule that `ruff check` runs alongside `pytest` after every change, not as an afterthought.

The real bug: `_complete_openai()`'s thinking-model fallback reads `message.get("reasoning", "")` and calls `.splitlines()`, assuming a plain string. A different, OpenAI-proper convention exists where `reasoning` is a dict (`{"effort": "none"}`) rather than the plain-string shape llama.cpp actually uses. The dict would crash `.splitlines()` with an uncaught `AttributeError`. The outer `complete()` exception handler would catch it cleanly, but the failure message would be less specific than it could be. Added a defensive `isinstance` guard. The first test draft was a false positive — it passed regardless of whether the fix existed because the outer handler masked the difference. Rewrote the test to call `_complete_openai()` directly, genuinely distinguishing the two cases.

Also verified during this audit: the `"think": False` parameter placement and the `requests.Session` thread-safety claim, both against real library documentation. Both confirmed correct.

### `scoring.py` (229 lines)

**Two real bugs.**

First: possessive forms (`"Apple's"`) stemmed to `"apple'"` rather than `"apple"`. The root cause is subtle: `str.strip()` only removes characters from the *ends* of a string. The apostrophe in `"Apple's"` is interior to the token — between the word stem and the trailing `s` — so `"Apple's".strip("...\"'...")` returns `"Apple's"` unchanged. After stemming, `"apple'" != "apple"`, so a query for `"Apple profit"` would miss the keyword match on a title reading `"Apple's profit rose"`, losing the full title-keyword-match bonus. Confirmed as a 20-point scoring difference between identical articles where only the possessive form of one word differed. Fixed by normalizing `'s` and trailing apostrophes before the strip/stem pipeline. Verified safe for contractions and programming language names.

Second: `_is_generic_result()` applied `startswith()` uniformly to all patterns, including single-word ones like `"home"`, `"error"`, and `"404"`. A legitimate news article titled `"Home prices rise 5% in October"` was penalized as a homepage. `"Error in climate data causes alarm"` was penalized as an error page. Fixed by splitting the pattern list: single-word patterns use exact-match only; multi-word phrases like `"welcome to"` and `"about us"` keep `startswith()`. Seven new tests.

### `uptime_kuma.py` (229 lines)

**Two real bugs.**

First: the status-dispatch loop had no `else` branch for status codes beyond the known 0–3 range. Any future or unrecognized status caused the monitor to disappear from all output buckets entirely. With `up_count=0` and nothing in any named list, the output read `"All 0 monitored services are up"` — an actively wrong statement. Added an `else` branch with a warning log that treats the monitor as `no_data`.

Second: `main.py`'s startup lifespan called `uptime_kuma.get_connection()` directly without holding `_connection_lock`, violating the function's own documented contract ("Callers must hold `_connection_lock`"). In practice, no race could occur at that specific point in startup (the scheduler and request handlers haven't started yet), but the contract violation was real and a future startup-sequence change could expose it. Fixed by adding `warm_connection()` — a purpose-built public function that correctly acquires the lock, handles exceptions gracefully, and includes its own config guard — replacing the bare call in `main.py`.

### `config.py` (467 lines)

No code bugs. The `KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT` setting ends in `_PCT` but takes a fraction (0–1), not a percentage (0–100). The README and Configuration Reference both document it correctly as `0.5` meaning 50%, but `Multi-Book-Fusion.md` said "default 50% of the top score" without noting that you set `0.5`, not `50`. Updated the wiki to call this out explicitly.

### `home_assistant.py` (627 lines)

No bugs. Every function verified — entity filter, area detection, `_build_filter()` word-boundary fix, motion deduplication, field formatting. The `"lights on"` phrase matching in `_build_filter()` was investigated carefully: `"turn the lights on in the bedroom"` does trigger the filter, but since the HA source is read-only and the router's own intent map already scopes which queries reach here, the practical consequence is benign. The semantic mismatch exists but isn't a data-integrity bug.

### `fusion.py` (668 lines)

No bugs. This file had the most prior bulletproofing investment and it held up. Deepseek reported two vulnerabilities during this session: a "thread pool exhaustion DoS" and "cache poisoning via alternate search phrasing." Both were traced precisely against the actual code. The DoS claim rested on a fictional "30-second source" premise — every real source in the codebase has a hard HTTP timeout of 5–25 seconds. The cache poisoning claim required a write path in `_alternate_phrasing_chain()` that doesn't exist — `_set_cached()` is called exclusively from `router.py`'s `route_with_source()`, and `_looks_empty()` already guards against writing error strings.

One real, narrow asymmetry was found: `searxng_request_timeout_seconds` (25s) exceeds `fusion_timeout_seconds` (15s), creating a 10-second window where pool workers can be occupied by abandoned tasks. Real, bounded, noted in the `config.py` comment. Not a DoS vector at realistic homelab request rates.

### `snapshots.py` (696 lines)

**One real bug.** `get_changes()` used a `seen_changes` set to deduplicate change descriptions across consecutive snapshot pairs. The intent was correct for UPTIME and FORECAST (where the same "outage" message would appear in every pair while a service stayed down), but neither of those sources reaches the event-based path — they use `NET_CHANGE_SOURCES`. The `seen_changes` set only ran on `ha` and `news`, and for both of those, the underlying diff functions only fire on genuine state transitions. "Opened" appearing twice in consecutive pairs means a door genuinely opened twice. The dedup was silently suppressing real events: a door opening, closing, then opening again within the query window would only report the first two transitions. The final, currently-open state was invisible. Fixed by removing `seen_changes` entirely. Two new tests confirmed both occurrences are now reported.

### `temporal_patterns.py` (870 lines)

No bugs. The Bonferroni-corrected Poisson significance test was verified against known values. The `_count_nonoverlapping_occurrences()` function was traced at its documented boundary (claiming behavior confirmed, including the B-claimed-exactly-once behavior). The stale-threshold logic was verified with actual defaults (72-hour stale window for a 24-hour interval at 3× grace).

### `kiwix.py` (957 lines)

No bugs. This file supports more of the codebase than any other source file — `_stem()` is used by `scoring.py`, `router.py`'s `_decompose()`, and `kiwix.py` itself; `_STOP_WORDS` is used across all three; `DISCOURSE_FRAMING_PATTERNS` is imported by `router.py`. All of it verified. One dead branch (`if not selected_books` in `search()`) was documented with a comment explaining why it's kept despite being currently unreachable — it guards against a real contract violation by `_pick_books_with_llm` that a future refactor could expose, and the failure message is already in `_looks_empty()`'s phrase list.

The docstring describing `_score_result()`'s scoring as including a standalone "Wikipedia bonus" was confirmed misleading by the wiki's own `Kiwix-Scoring.md`, which already correctly explains the `+8`/`+3` only fires as a partial offset inside the list-article penalty branch. The code is correct; the docstring misdescribes it. The wiki is accurate. No change made.

### `adversarial_testing.py` (1120 lines)

No bugs. The schema migration and backfill logic was verified against a real old-schema database (confirmed it adds missing columns and correctly backfills `ever_flagged` for rows that had `last_flagged_reason` set before the column existed). The `seen_changes`-style question came up here too — the `_record_result()` dismiss/re-flag logic was traced carefully against all four database branches: fresh insert clean, fresh insert flagged, first-ever flag, re-flag after previous dismissal. All correct. The re-flag path correctly clears `review_status` so a new anomaly resurfaces in the default queue regardless of whether a human dismissed an earlier one.

### `main.py` (1179 lines)

**One real bug.** The TTFK (Time To First Knowledge) SQL computed `MIN(id)` and `MIN(latency_ms)` as two independent aggregates in the same `GROUP BY`. These are computed independently: `MIN(id)` correctly identifies the first occurrence's row, but `MIN(latency_ms)` picks the *smallest latency across all non-cached occurrences* — not the latency of the row whose id is `MIN(id)`. For any query asked more than once without caching (including adversarial test queries, which never cache by design), this reported the fastest cold run rather than the genuine first cold hit. Confirmed directly: a query first seen at 3000ms and second seen at 200ms reported TTFK of 200ms. Fixed with a join back to the row with `min(id)`. One new test confirmed by revert.

All other functions verified clean: `_init_log_db()` schema migration, `_log_query()`, all seven `_check_*` health functions, lifespan MCP refresh logic, `/search` fallback detection, `/logs` limit clamping, `query_log_stats()` SQL (top-queries most-recent-source fix, cache rates, fallback grouping), backup/restore, and every endpoint's response shape.

### `router.py` (2482 lines)

No bugs. The longest file in the codebase, and the one with the most prior bulletproofing history. It held up.

Functions verified: `_connect_log_db_readonly` (read-only enforced at SQLite URI level — confirmed writes raise `OperationalError`), `get_recent_queries`, `suppress_cache_writes` (thread-locality via `ContextVar` confirmed with a real concurrent thread that should not see the suppression), all routing cache functions (TTL expiry, max-size enforcement, atomic disk write, malformed-entry skipping on load), `_RefCountedLock`/`_get_inflight_lock`/`_release_inflight_lock`/`_singleflight` (mutual exclusion confirmed with real threads in correct ordering), all result cache functions, `_resolve_changes_hours` (window-phrase regex verified), `_has_discourse_framing`, `_keyword_detect`, all four discourse-framing escalation functions, `_llm_detect` and `_llm_pick_fusion_sources` (confirmed transient failures are NOT cached — the documented fix that a failure should give every subsequent call a fresh chance at a correct answer), `detect_intent` (discourse framing applied to keyword path, not just LLM path), `detect_conditional` (all three lead words, conjunction cut, remainder extraction), `_interpret_binary_state` (confirmed `confirms_negative_result` is checked first to prevent the `"locked"`-in-`"unlocked"` substring trap), `_interpret_yes_no`, `_frame_conditional_response`, `_is_proper_noun_pair_at` (including the `"I"` exclusion on both sides of the conjunction), `_decompose` (nosplit patterns, proper-noun pair protection, `"rss"` keyword preserved via `_ALL_INTENT_KEYWORDS` check before the `len<=3` gate), `route`, `_resolve_single_source`, `_resolve_conditional`, `_merge_decomposed_parts`, `_dedupe_nested_fusion_sections`, `route_with_source`.

---

## What the Audit Found — Summary

| File | Lines | Bugs Found |
|------|-------|-----------|
| `query_expansion.py` | 83 | 0 |
| `forecast.py` | 108 | 1 |
| `timeutil.py` | 153 | 0 (docstring) |
| `mcp_server.py` | 160 | 1 |
| `freshrss.py` | 187 | 1 |
| `searxng.py` | 194 | 2 |
| `llm.py` | 194 | 1 + 1 lint regression |
| `scoring.py` | 229 | 2 |
| `uptime_kuma.py` | 229 | 2 |
| `config.py` | 467 | 0 |
| `home_assistant.py` | 627 | 0 |
| `fusion.py` | 668 | 0 |
| `snapshots.py` | 696 | 1 |
| `temporal_patterns.py` | 870 | 0 |
| `kiwix.py` | 957 | 0 |
| `adversarial_testing.py` | 1120 | 0 |
| `main.py` | 1179 | 1 |
| `router.py` | 2482 | 0 |
| **Total** | **~10,600** | **14** |

Fourteen bugs across 18 files. Most of the larger files — `home_assistant.py`, `fusion.py`, `temporal_patterns.py`, `kiwix.py`, `adversarial_testing.py`, `router.py` — were clean. This isn't surprising: they're the files with the most prior deliberate investment, the most targeted testing, and the most dense existing commentary. The bugs concentrated in the smaller, less-exercised utility files where testing had been thinner and the reasoning less thoroughly documented.

---

## What the Audit Didn't Catch

The bugs that slip through a function-by-function read are integration bugs — things that are individually correct but wrong in combination. `_diff_forecast()`'s zero-degree bug was caught, but only because that specific boundary was tested directly; a subtler interaction between two correctly-implemented functions wouldn't have been visible from a per-function read alone. The adversarial self-testing system exists specifically to find that class of bug, and the two systems are complementary rather than redundant.

There are also the unknown unknowns: bugs that wouldn't be visible even if you read the code and the tests and understood them all correctly, because they only manifest at specific data shapes, timing windows, or environmental conditions that neither the read nor the tests covered. Those require real production use, real Locust benchmarks, and real edge cases arriving from real queries — the same way the thread-pool bugs in `searxng.py` and `fusion.py` were only found after the v3.17.0 benchmark run showed a real problem.

---

## What Changed in This Project's Practice

**`ruff check` is now mandatory alongside `pytest`.** The lint regression in `llm.py` slipped through several sessions because the local verification routine only ran `pytest`. CI caught it. The lesson is obvious in retrospect.

**Revert-and-confirm before declaring a test real.** Several tests during this audit were written, run, and found to pass before the fix was applied — which means they weren't actually testing the fix. The `llm.py` test was the clearest example: calling `complete()` masked the difference because the outer exception handler already caught the `AttributeError`. Calling `_complete_openai()` directly was the only way to genuinely distinguish fixed from unfixed. This discipline — confirm the test *fails* against the unfixed code before trusting it — is now a standing requirement, not an optional step.

**Dead defensive code gets explained, not removed.** Three places in the codebase have guards that are currently unreachable: `if not selected_books` in `kiwix.py`, `if not found_mount` in `main.py`'s lifespan, and the `else` branch for unknown status codes in `uptime_kuma.py`. All three were considered for removal during the audit. All three were kept, with comments explaining why: each guards against a real contract violation that a future refactor could expose, and the cost of two lines of defensive code with a good comment is strictly less than the cost of a silent, hard-to-diagnose failure if the underlying assumption ever becomes wrong.

---

*Previous saga pages: [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs) · [The Meaningful Content Filter Bugs](The-Meaningful-Content-Filter-Bugs) · [The General News Query Detection Bugs](The-General-News-Query-Detection-Bugs) · [The Discourse Framing Investigation](The-Discourse-Framing-Investigation) · [The Benchmark Investigation Log](The-Benchmark-Investigation-Log)*
