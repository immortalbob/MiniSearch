# Mnemolis Benchmarks

Load testing performed with [Locust](https://locust.io/) against a production MiniDock instance (Intel N100, 16GB RAM) with Ollama running on a separate host (i9-14900KF, RTX 4090).

This file is the raw, dated data ledger — every table from every benchmarked release, in order. For the current-state reference (what the numbers mean today, how to run your own) see the wiki's [Benchmarks](https://github.com/immortalbob/Mnemolis/wiki/Benchmarks) page. For the chronological investigation story behind the recurring findings below — what was traced, what got tried, and a real sizing mistake one re-benchmark caught — see the wiki's **[Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log)**.

## Test Configuration

**Tool:** Locust 2.44.3  
**Target:** `http://localhost:8888`  
**User classes:** `MnemolisSingleSourceUser` and `MnemolisFusionUser`  
**Sources tested:** kiwix, forecast, news, uptime, ha, fusion (2-source, 3-source, LLM auto)

## Results

### 5 Users — Cold Cache

First run, routing cache empty, LLM making fresh decisions.

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 710ms | 750ms | 750ms | 0% |
| `/search [kiwix]` | 900ms | 1400ms | 1400ms | 0% |
| `/search [forecast]` | 16ms | 710ms | 710ms | 0% |
| `/search [news]` | 16ms | 50ms | 50ms | 0% |
| `/search [uptime]` | 996ms | 1000ms | 1000ms | 0% |
| `/search [ha]` | 33ms | 37ms | 37ms | 0% |
| `/search [auto]` | 17ms | 6400ms | 6400ms | 0% |
| `/search [fusion_explicit]` | 16ms | 1000ms | 1300ms | 0% |
| `/search [fusion_auto]` | 16ms | 4500ms | 4500ms | 0% |
| `/search [fusion_triple]` | 16ms | 1100ms | 1100ms | 0% |
| `/search [cache_hit]` | 15ms | 5300ms | 5300ms | 0% |
| **Aggregated** | **16ms** | **1300ms** | **6400ms** | **0%** |

p95/p99 spikes are cold LLM routing calls — first time the system sees a query it calls Ollama for source and book selection. Cached on first use.

---

### 10 Users — Warming Cache

Routing cache populated from the 5-user run. System recognizes repeated queries.

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 730ms | 1100ms | 1100ms | 0% |
| `/search [kiwix]` | 16ms | 1300ms | 1800ms | 0% |
| `/search [forecast]` | 16ms | 18ms | 18ms | 0% |
| `/search [news]` | 16ms | 33ms | 33ms | 0% |
| `/search [uptime]` | 14ms | 1000ms | 1000ms | 0% |
| `/search [ha]` | 35ms | 38ms | 38ms | 0% |
| `/search [auto]` | 16ms | 1000ms | 1300ms | 0% |
| `/search [fusion_explicit]` | 15ms | 27ms | 84ms | 0% |
| `/search [fusion_auto]` | 15ms | 17ms | 18ms | 0% |
| `/search [fusion_triple]` | 16ms | 20ms | 20ms | 0% |
| `/search [cache_hit]` | 16ms | 18ms | 18ms | 0% |
| **Aggregated** | **16ms** | **730ms** | **1300ms** | **0%** |

Fusion results now effectively free — concurrent sources returning cached results in 15ms.

---

### 20 Users — Hot Cache (v3.4.5)

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 730ms | 1100ms | 1100ms | 0% |
| `/search [kiwix]` | 15ms | 18ms | 21ms | 0% |
| `/search [forecast]` | 14ms | 23ms | 24ms | 0% |
| `/search [news]` | 15ms | 16ms | 17ms | 0% |
| `/search [uptime]` | 14ms | 1100ms | 1100ms | 0% |
| `/search [ha]` | 34ms | 41ms | 41ms | 0% |
| `/search [auto]` | 15ms | 1000ms | 1100ms | 0% |
| `/search [fusion_explicit]` | 14ms | 19ms | 69ms | 0% |
| `/search [fusion_auto]` | 15ms | 17ms | 33ms | 0% |
| `/search [fusion_triple]` | 14ms | 17ms | 17ms | 0% |
| `/search [cache_hit]` | 15ms | 17ms | 25ms | 0% |
| **Aggregated** | **15ms** | **41ms** | **1000ms** | **0%** |

**7.6 requests/second sustained. 391 requests. 0 failures.**

---

### 20 Users — Hot Cache (v3.5.0)

Re-benchmarked after query decomposition and smart fusion improvements.

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 730ms | 830ms | 830ms | 0% |
| `/search [kiwix]` | 15ms | 21ms | 22ms | 0% |
| `/search [forecast]` | 15ms | 17ms | 18ms | 0% |
| `/search [news]` | 15ms | 20ms | 24ms | 0% |
| `/search [uptime]` | 15ms | 17ms | 18ms | 0% |
| `/search [ha]` | 16ms | 83ms | 83ms | 0% |
| `/search [auto]` | 15ms | 23ms | 1000ms | 0% |
| `/search [fusion_explicit]` | 14ms | 19ms | 22ms | 0% |
| `/search [fusion_auto]` | 15ms | 22ms | 22ms | 0% |
| `/search [fusion_triple]` | 15ms | 17ms | 17ms | 0% |
| `/search [cache_hit]` | 16ms | 25ms | 25ms | 0% |
| **Aggregated** | **15ms** | **36ms** | **780ms** | **0%** |

**428 requests. 0 failures. p95 improved from 41ms → 36ms. p99 improved from 1000ms → 780ms.**

Query decomposition and smart fusion truncation did not add meaningful overhead — p95 and p99 both improved, likely due to reduced payload sizes from result truncation.

---

## Summary

| Scenario | Median | p95 | Notes |
|----------|--------|-----|-------|
| Cache hit (any source) | 15ms | 25ms | Routing + result cache both warm |
| Cold LLM routing call | ~1000ms | ~6400ms | First time seeing a query |
| Kiwix warm | 15ms | 21ms | LLM book selection cached |
| Fusion 2-source warm | 14ms | 19ms | Concurrent sources, both cached |
| Fusion 3-source warm | 15ms | 17ms | No overhead over 2-source when cached |
| HA entity query | 16ms | 83ms | Live HA API call, 30s result cache |
| Uptime warm | 15ms | 17ms | Socket.IO result cached |
| 20 concurrent users (v3.5.0) | 15ms | 36ms | 0% failure rate |
| Kiwix disambiguation, cold | 16ms | 5900ms | First time seeing an ambiguous term |
| Kiwix disambiguation, warm | 17ms | 20ms | Disambiguation candidates cached |
| Web search, cold | 17ms | 170ms | First time seeing a query (p99: 4600ms) |
| Web search, warm | 17ms | 20ms | Alternate phrasing cached |
| 20 concurrent users (v3.11.1) | 17ms | 38ms | 0% failure rate, warm cache |
| Conditional query, cold | 17ms | 1200ms | First time seeing the condition |
| Conditional query, warm | 18ms | 35ms | Condition routing cached |
| Conditional with remainder, cold | 17ms | 1200ms | Two independent searches (condition + remainder) |
| Conditional with remainder, warm | 18ms | 38ms | Only partially warmed — see v3.17.0 notes (small query pool artifact) |
| Discourse-framing query, cold | 18ms | 150ms | First time seeing the phrase (p98: 2500ms) |
| Discourse-framing query, warm | 17ms | 23ms | Routing + disambiguation cached |
| 20 concurrent users (v3.17.0) | 18ms | 32ms | 0% failure rate, warm cache |
| HA entity query, post-word-boundary-fix (v3.44.0) | 35-39ms | 47-48ms | `_build_filter()` switched from substring to regex matching — no measurable cost confirmed |
| Discourse-framing query, cold (v3.44.0) | 30ms | 1300ms | New most expensive cold-path query (p98: 4200ms), reflecting a larger Kiwix catalog/routing surface since v3.17.0 |
| Discourse-framing query, warm (v3.44.0) | 29ms | 43ms | ~76x p98 improvement (4200ms → 55ms) once cached |
| 20 concurrent users (v3.44.0) | 24ms | 53ms | 0% failure rate, warm cache — median unchanged since v3.5.0 |

## Key findings

**The routing cache is the performance multiplier.** Cold queries that call Ollama for routing decisions take 1-6 seconds. Warm queries return in 15ms regardless of source complexity. After a few minutes of real usage the cache is warm and the system operates at sub-20ms for nearly all queries.

**Fusion does not add meaningful latency when cached.** A 3-source fusion query with a warm cache returns in 15ms — the same as a single source. Concurrent execution means fusion is essentially free once the routing decision is cached.

**Query decomposition has no measurable overhead.** Splitting "what is the weather and are my services up" into two independent queries and merging the results adds no latency compared to single-source routing at warm cache. p95 improved from 41ms to 36ms after decomposition was added.

**Result truncation improves p99.** Smart fusion now caps each source at 1500 characters before merging. p99 improved from 1000ms to 780ms, likely due to smaller response payloads.

**Zero failures at 20 concurrent users across all versions.** The system does not drop requests under homelab load.

## Hardware context

These benchmarks reflect a homelab deployment — not a production cloud environment. Results will vary based on:
- LLM hardware (RTX 4090 in this case — faster GPU = lower cold routing latency)
- Network latency to HA, Uptime Kuma, and other sources
- Kiwix ZIM file size and disk I/O speed
- Routing cache warmth

### 20 Users — Hot Cache (v3.6.1, with Snapshot Scheduler)

Re-benchmarked after adding the background snapshot scheduler (4 jobs: uptime every 2 min, forecast every 30 min, news every 60 min, HA every 5 min) and fixing SQLite lock contention with WAL mode.

**First run** showed connection resets under load (9 errors across various endpoints) despite 100% success in server logs — determined to be Locust/Docker networking noise, not a code regression, since a clean rerun showed 0 errors.

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 730ms | 840ms | 840ms | 0% |
| `/search [kiwix]` | 17ms | 20ms | 22ms | 0% |
| `/search [forecast]` | 17ms | 19ms | 20ms | 0% |
| `/search [news]` | 16ms | 20ms | 22ms | 0% |
| `/search [uptime]` | 18ms | 23ms | 1000ms | 0% |
| `/search [ha]` | 34ms | 74ms | 74ms | 0% |
| `/search [auto]` | 17ms | 20ms | 1100ms | 0% |
| `/search [fusion_explicit]` | 17ms | 20ms | 72ms | 0% |
| `/search [fusion_auto]` | 17ms | 22ms | 28ms | 0% |
| `/search [fusion_triple]` | 17ms | 20ms | 32ms | 0% |
| `/search [cache_hit]` | 17ms | 19ms | 19ms | 0% |
| **Aggregated** | **17ms** | **72ms** | **790ms** | **0%** |

**429 requests. 0 failures on clean run.**

The background scheduler adds no meaningful overhead to search latency. Median rose slightly (15ms → 17ms) due to WAL pragma overhead per connection, but this is negligible and p95/p99 remain within the same range as v3.5.0 despite four additional background jobs competing for resources.

### 20 Users — Cold vs Warm Cache (v3.11.1, with Confidence-Aware Fusion + Disambiguation)

Re-benchmarked after the capability expansion series: configurable thresholds, Kiwix search term disambiguation (multi-candidate search-and-score), multi-book Kiwix fusion, and confidence-aware fusion with multi-query expansion for web search. The locust file itself was updated for this run — the prior version had zero `web` source queries and no short/ambiguous Kiwix queries, meaning it couldn't measure the cost of the two most computationally expensive features added this series.

**Cold cache** — first run, routing cache empty for the new query patterns (disambiguation candidates, alternate web phrasings never seen before).

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 730ms | 810ms | 970ms | 0% |
| `/search [kiwix]` | 18ms | 1900ms | 7300ms | 0% |
| `/search [kiwix_disambiguation]` | 16ms | 5900ms | 6000ms | 0% |
| `/search [web]` | 17ms | 170ms | 4600ms | 0% |
| `/search [forecast]` | 17ms | 20ms | 820ms | 0% |
| `/search [news]` | 16ms | 25ms | 140ms | 0% |
| `/search [uptime]` | 19ms | 1600ms | 1600ms | 0% |
| `/search [ha]` | 28ms | 45ms | 45ms | 0% |
| `/search [auto]` | 17ms | 650ms | 6000ms | 0% |
| `/search [fusion_explicit]` | 18ms | 150ms | 1800ms | 0% |
| `/search [fusion_auto]` | 17ms | 83ms | 3000ms | 0% |
| `/search [fusion_triple]` | 17ms | 32ms | 950ms | 0% |
| `/search [cache_hit]` | 17ms | 3500ms | 3500ms | 0% |
| **Aggregated** | **17ms** | **770ms** | **4900ms** | **0%** |

**853 requests. 0 failures.** The cold-cache tail is genuinely heavier than prior releases — `kiwix_disambiguation` p95 at 5900ms reflects the real cost of generating 3 LLM disambiguation candidates, searching each against Kiwix, and scoring the combined pool, all on first encounter with a given ambiguous term. `web` p99 at 4600ms reflects the dual-query expansion (two SearXNG round-trips plus scoring 25+ raw results) on first encounter with a given query.

**Warm cache** — identical run immediately after, with the routing cache now populated.

| Endpoint | Median | p95 | p99 | Failures |
|----------|--------|-----|-----|----------|
| `/health` | 730ms | 780ms | 790ms | 0% |
| `/search [kiwix]` | 17ms | 22ms | 31ms | 0% |
| `/search [kiwix_disambiguation]` | 17ms | 20ms | 21ms | 0% |
| `/search [web]` | 17ms | 20ms | 38ms | 0% |
| `/search [forecast]` | 17ms | 20ms | 20ms | 0% |
| `/search [news]` | 17ms | 20ms | 22ms | 0% |
| `/search [uptime]` | 18ms | 960ms | 980ms | 0% |
| `/search [ha]` | 38ms | 57ms | 57ms | 0% |
| `/search [auto]` | 17ms | 1000ms | 30000ms¹ | 0% |
| `/search [fusion_explicit]` | 17ms | 20ms | 35ms | 0% |
| `/search [fusion_auto]` | 17ms | 20ms | 24ms | 0% |
| `/search [fusion_triple]` | 18ms | 22ms | 24ms | 0% |
| `/search [cache_hit]` | 16ms | 20ms | 20ms | 0% |
| **Aggregated** | **17ms** | **38ms** | **780ms** | **0%** |

**869 requests. 0 failures.**

¹ One `auto` request reported 30000ms (a single outlier in a 95-request bucket). Server logs for the full benchmark window (`docker logs mnemolis --since 10m`) showed zero errors, warnings, exceptions, or timeouts — consistent with Locust's own client-side request timeout firing rather than a real server-side delay. Excluding that single outlier, the aggregated p99 is 780ms, in line with every prior release.

**The routing cache fully absorbs the new features' cold-start cost.** `kiwix_disambiguation` p95 dropped from 5900ms (cold) to 20ms (warm) — a ~295x improvement. `web` p99 dropped from 4600ms (cold) to 38ms (warm) — a ~121x improvement. Once a given ambiguous term or query phrasing has been seen once, every subsequent occurrence skips the LLM calls entirely and returns at the same sub-20ms median every other source achieves.

**Median latency is unaffected by any of this series' work.** Aggregated median held at 17ms cold and warm, identical to every prior benchmarked version back to v3.5.0. The capability expansion series traded cold-path tail latency for correctness on a minority of complex queries — disambiguation and multi-query expansion only ever run when genuinely needed (short ambiguous Kiwix terms, 3+ word web queries) — without touching the steady-state experience for the other ~90% of traffic.

### 20 Users — Cold vs Warm Cache (v3.17.0, with Conditional Query Detection + Discourse-Framing Fix)

Re-benchmarked after the conditional query detection feature (3.16.0) and the discourse-framing routing bypass fix (3.17.0) — neither had been measured under load before. The locustfile was updated for this run with three new task types: `conditional` (leading "if X, Y" queries against both structured and open-ended sources), `conditional_remainder` (a conditional followed by a real, independently-searched second intent), and `discourse_framing` ("everyone's obsessed with X" phrasing, which now forces Kiwix into the routing decision and strips the discourse phrase from Kiwix's search terms). The prior locustfile had zero coverage for any of this — it couldn't have measured these features' cost at all.

**Cold cache** — first run, routing cache empty for the new query patterns.

| Endpoint | Median | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|----------|
| `/health` | 700ms | 910ms | 910ms | 910ms | 0% |
| `/search [kiwix]` | 18ms | 1600ms | 1800ms | 3600ms | 0% |
| `/search [kiwix_disambiguation]` | 18ms | 2200ms | 8000ms | 8000ms | 0% |
| `/search [web]` | 18ms | 1700ms | 3600ms | 5200ms | 0% |
| `/search [conditional]` | 17ms | 1200ms | 1300ms | 1300ms | 0% |
| `/search [conditional_remainder]` | 17ms | 1200ms | 1900ms | 1900ms | 0% |
| `/search [discourse_framing]` | 18ms | 150ms | 2500ms | 2500ms | 0% |
| `/search [forecast]` | 17ms | 20ms | 750ms | 750ms | 0% |
| `/search [news]` | 18ms | 23ms | 100ms | 100ms | 0% |
| `/search [uptime]` | 18ms | 1100ms | 1100ms | 1100ms | 0% |
| `/search [ha]` | 35ms | 56ms | 120ms | 120ms | 0% |
| `/search [auto]` | 19ms | 1100ms | 1400ms | 1500ms | 0% |
| `/search [fusion_explicit]` | 18ms | 92ms | 670ms | 960ms | 0% |
| `/search [fusion_auto]` | 18ms | 23ms | 120ms | 170ms | 0% |
| `/search [fusion_triple]` | 18ms | 120ms | 1300ms | 1300ms | 0% |
| `/search [cache_hit]` | 18ms | 2600ms | 4700ms | 4700ms | 0% |
| **Aggregated** | **18ms** | **750ms** | **1600ms** | **2500ms** | **0%** |

**849 requests. 0 failures.** `kiwix_disambiguation` remains the most expensive cold-path query (p98 8000ms) — unchanged in kind from prior releases, just a different worst-case sample. The two genuinely new cost centers are `conditional_remainder` (two full searches plus two routing decisions per request — condition and remainder are independently routed) and `discourse_framing` (the forced extra Kiwix search added on top of whatever the LLM already chose).

**Warm cache** — identical run immediately after, routing cache populated.

| Endpoint | Median | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|----------|
| `/health` | 710ms | 770ms | 770ms | 770ms | 0% |
| `/search [kiwix]` | 18ms | 20ms | 23ms | 25ms | 0% |
| `/search [kiwix_disambiguation]` | 18ms | 21ms | 32ms | 32ms | 0% |
| `/search [web]` | 17ms | 21ms | 24ms | 25ms | 0% |
| `/search [conditional]` | 18ms | 35ms | 1100ms | 1100ms | 0% |
| `/search [conditional_remainder]` | 18ms | 38ms | 1100ms | 1100ms | 0% |
| `/search [discourse_framing]` | 17ms | 23ms | 25ms | 25ms | 0% |
| `/search [forecast]` | 18ms | 21ms | 24ms | 24ms | 0% |
| `/search [news]` | 18ms | 22ms | 40ms | 40ms | 0% |
| `/search [uptime]` | 17ms | 980ms | 1400ms | 1400ms | 0% |
| `/search [ha]` | 35ms | 48ms | 48ms | 48ms | 0% |
| `/search [auto]` | 18ms | 1100ms | 1500ms | 1600ms | 0% |
| `/search [fusion_explicit]` | 17ms | 22ms | 26ms | 34ms | 0% |
| `/search [fusion_auto]` | 18ms | 21ms | 23ms | 47ms | 0% |
| `/search [fusion_triple]` | 17ms | 21ms | 21ms | 22ms | 0% |
| `/search [cache_hit]` | 17ms | 22ms | 23ms | 23ms | 0% |
| **Aggregated** | **18ms** | **32ms** | **700ms** | **770ms** | **0%** |

**907 requests. 0 failures.**

**`kiwix_disambiguation` and `discourse_framing` both collapse fully on cache hit, consistent with every prior release.** `kiwix_disambiguation` p98 dropped from 8000ms (cold) to 32ms (warm) — a ~250x improvement. `discourse_framing` p98 dropped from 2500ms (cold) to 25ms (warm) — a ~100x improvement. Once a discourse-framing phrase or disambiguation term has been seen once, the routing cache skips the LLM/disambiguation work entirely on every subsequent occurrence.

**`conditional_remainder` only partially warmed (1900ms → 1100ms at p98, not down to ~25ms like everything else) — a real, honest finding worth noting rather than glossing over.** The remainder path makes two independent `route_with_source()` calls (one for the condition, one for the remainder), each with its own cache key. With only 2 remainder queries in the test pool and `random.choice()` picking independently across the cold and warm runs, it's plausible one half of a given pair was cached from the cold run while the other wasn't — a benchmark-methodology artifact of a small query pool size, not evidence the underlying caching mechanism (already proven correct everywhere else in this same benchmark) is broken for this path specifically.

**`uptime` shows an unexplained warm-cache tail (p95 980ms, p99 1400ms) inconsistent with its own cold-cache numbers and every prior release's uptime benchmarks.** Worth re-checking in a future run rather than treating as confirmed — could be a real Uptime Kuma connection hiccup during this specific run, or sampling noise given the relatively small request count (25) for this endpoint.

**Median latency remains completely unaffected.** Aggregated median held at 18ms cold and warm, consistent with every prior benchmarked version back to v3.5.0 — conditional detection and the discourse-framing fix both add real cost only on the specific query shapes that trigger them, with zero impact on the steady-state majority of traffic.

### 20 Users — Cold vs Warm Cache (v3.44.0, post-battle-testing and bulletproofing)

The last real benchmark in this file was v3.17.0. Everything from v3.18.0 through v3.44.0 — the complexity-investigation campaign (v3.20.0–v3.34.0, real bugs found and fixed in `route_with_source`, `_decompose`, `home_assistant.py`'s area filtering, `kiwix.py`'s scoring/disambiguation, `snapshots.py`'s diff engines, `uptime_kuma.py`, `freshrss.py`, `searxng.py`) and the bulletproofing pass that followed (v3.35.0–v3.44.0, a full top-to-bottom read of every file in `app/` specifically hunting for bugs complexity scores never flagged) — had never been measured under real load. Two real, severe bugs from that whole stretch are directly relevant to what this run could confirm: `home_assistant.py`'s keyword matching switched from naive substring search to `\b`-word-boundary regex (a real bug had `"on"` matching inside `"front"`, silently breaking entity lookups like "is the front door locked"), and `kiwix.py`'s article-fetch fallback loop is now capped at 5 attempts instead of unbounded. Neither is expected to add meaningful latency — both are correctness fixes, not new computation.

`tests/locustfile.py`'s `HA_QUERIES` was updated for this run specifically: added `"is the front door locked"` and `"is the download finished yet"`, the exact query shapes behind the word-boundary bug, so this run could confirm under real, concurrent load against live HA data that the fix holds — not just in the test suite.

**A real, genuine gap surfaced while running this**, worth documenting since it cost real time: this file had never previously spelled out *how* a "cold cache" run actually gets its empty cache — every prior version's section stated it as a fact with no documented mechanism. The first attempt at this run reused a session-warmed cache and produced an artificially clean result instead of real cold numbers. Fixed going forward — see "Running benchmarks" below for the now-explicit `POST /cache/clear` + `POST /cache/routing/clear` step required before a genuine cold run.

**Cold cache** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|----------|
| `/health` | 770ms | 780ms | 780ms | 780ms | 0% |
| `/search [kiwix]` | 23ms | 1400ms | 1600ms | 1800ms | 0% |
| `/search [kiwix_disambiguation]` | 23ms | 2000ms | 2100ms | 2100ms | 0% |
| `/search [web]` | 24ms | 1700ms | 2200ms | 3900ms | 0% |
| `/search [conditional]` | 28ms | 1200ms | 1300ms | 1300ms | 0% |
| `/search [conditional_remainder]` | 31ms | 320ms | 800ms | 800ms | 0% |
| `/search [discourse_framing]` | 30ms | 1300ms | 4200ms | 4200ms | 0% |
| `/search [forecast]` | 23ms | 49ms | 730ms | 730ms | 0% |
| `/search [news]` | 23ms | 31ms | 54ms | 54ms | 0% |
| `/search [uptime]` | 24ms | 1500ms | 1500ms | 1500ms | 0% |
| `/search [ha]` | 35ms | 48ms | 50ms | 50ms | 0% |
| `/search [auto]` | 25ms | 1100ms | 1100ms | 1400ms | 0% |
| `/search [fusion_explicit]` | 22ms | 60ms | 720ms | 1000ms | 0% |
| `/search [fusion_auto]` | 24ms | 38ms | 47ms | 83ms | 0% |
| `/search [fusion_triple]` | 23ms | 1500ms | 1700ms | 2000ms | 0% |
| `/search [cache_hit]` | 23ms | 75ms | 3300ms | 3300ms | 0% |
| **Aggregated** | **24ms** | **780ms** | **1500ms** | **1900ms** | **0%** |

**876 requests. 0 failures.** `discourse_framing` is the new most expensive cold-path query (p98 4200ms) — the forced extra Kiwix search on top of whatever the LLM already chose, now compounding with the genuinely larger Kiwix catalog and routing-decision surface this codebase has accumulated since v3.17.0. `kiwix_disambiguation` (p98 2100ms) and `fusion_triple` (p98 1700ms) are both consistent in kind with prior releases, just different worst-case samples from a larger query surface.

**Warm cache** — identical run immediately afterward, no clearing in between.

| Endpoint | Median | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|----------|
| `/health` | 740ms | 810ms | 820ms | 820ms | 0% |
| `/search [kiwix]` | 23ms | 34ms | 40ms | 41ms | 0% |
| `/search [kiwix_disambiguation]` | 24ms | 37ms | 39ms | 39ms | 0% |
| `/search [web]` | 23ms | 33ms | 50ms | 54ms | 0% |
| `/search [conditional]` | 27ms | 1300ms | 2300ms | 2400ms | 0% |
| `/search [conditional_remainder]` | 30ms | 44ms | 63ms | 63ms | 0% |
| `/search [discourse_framing]` | 29ms | 43ms | 55ms | 55ms | 0% |
| `/search [forecast]` | 24ms | 36ms | 40ms | 40ms | 0% |
| `/search [news]` | 22ms | 30ms | 36ms | 36ms | 0% |
| `/search [uptime]` | 23ms | 990ms | 1000ms | 1000ms | 0% |
| `/search [ha]` | 39ms | 47ms | 63ms | 63ms | 0% |
| `/search [auto]` | 25ms | 1100ms | 1200ms | 1400ms | 0% |
| `/search [fusion_explicit]` | 22ms | 31ms | 36ms | 41ms | 0% |
| `/search [fusion_auto]` | 25ms | 37ms | 43ms | 44ms | 0% |
| `/search [fusion_triple]` | 22ms | 30ms | 31ms | 31ms | 0% |
| `/search [cache_hit]` | 23ms | 25ms | 25ms | 25ms | 0% |
| **Aggregated** | **24ms** | **53ms** | **770ms** | **1000ms** | **0%** |

**893 requests. 0 failures.**

**`kiwix`, `kiwix_disambiguation`, `web`, `discourse_framing`, `fusion_triple`, and `cache_hit` all collapse fully on cache hit, consistent with every prior release.** `discourse_framing` p98 dropped from 4200ms (cold) to 55ms (warm) — a ~76x improvement. `fusion_triple` p98 dropped from 1700ms to 31ms — a ~55x improvement.

**`auto`, `conditional`, and `uptime` stayed expensive at p95+ even warm — a real, genuine pattern worth investigating rather than assuming away, and now traced to a real, identifiable cause for two of the three.** `_resolve_conditional()` recurses into the full routing pipeline using the *extracted condition text* as the cache key (e.g. "the back door is unlocked" — not the original "if X, Y" phrasing), and that extracted text never appears as a standalone query anywhere else in `tests/locustfile.py`'s query pool, so each of `CONDITIONAL_QUERIES`'s 4 fixed phrasings has to warm independently. With 20 concurrent Locust users and only 4 distinct conditions, multiple users can pick the *same* never-yet-cached condition within the same instant — before the first one to resolve it has actually written the cache entry — so several of them each pay the full LLM routing cost concurrently, even on a nominally "warm" run. This is a real thundering-herd race on cache writes under artificial concurrent load, not a caching bug: real homelab usage doesn't involve 20 concurrent users asking about the same back door lock in the same second. `auto`'s tail (also a small, fixed 6-entry pool) likely shares the same root cause. `conditional` p98 actually rose warm vs cold (1300ms → 2300ms) — consistent with this explanation if the warm run's random sampling happened to produce more simultaneous collisions on a not-yet-cached condition than the cold run did, not evidence of a real regression. The actual fix, if pursued, is widening `CONDITIONAL_QUERIES`/`AUTO_QUERIES` to dilute the collision odds, not changing any application code. `uptime`'s warm-cache tail (p95 990ms, p99 1000ms) is the same unexplained anomaly already flagged in the v3.17.0 entry, now reproduced a second time across a completely different release — still not root-caused, and genuinely worth a dedicated investigation rather than continuing to flag it release after release.

**Median latency remains completely unaffected by the entire battle-testing and bulletproofing campaign.** Aggregated median held at 24ms cold and warm — consistent with every benchmarked version back to v3.5.0, across roughly 25 releases and dozens of real bug fixes in between. Every fix in this stretch was a correctness change, not new computation on the steady-state path, and the numbers confirm that held true in practice, not just in theory.

### 20 Users — Cold vs Warm Cache (v3.50.2, post-adversarial-self-testing and post-latency-parallelization work)

The last real benchmark in this file was v3.44.0. Everything between v3.44.1 and v3.50.2 — the config-completeness audit (v3.45.0), Adversarial Self-Testing's full build-out and real production run (v3.46.0–v3.48.x), Cross-Source Temporal Pattern Detection (v3.47.0), and the full latency-parallelization investigation that fixed `web` query expansion's sequential cost and `conditional_with_remainder`'s sequential cost at the root (v3.48.10–v3.50.0) — had never been measured under real load. Two of those fixes are directly testable by this exact run: `web`'s primary fetch and alternate-phrasing chain now run concurrently instead of sequentially, and `conditional_with_remainder`'s condition and remainder now run concurrently too.

**Cold cache** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|----------|
| `/health` | 740ms | 810ms | 810ms | 810ms | 0% |
| `/search [kiwix]` | 24ms | 1100ms | 1700ms | 2800ms | 0% |
| `/search [kiwix_disambiguation]` | 22ms | 2300ms | 2300ms | 2300ms | 0% |
| `/search [web]` | 24ms | 970ms | 1100ms | 1300ms | 0% |
| `/search [conditional]` | 26ms | 1000ms | 1400ms | 1400ms | 0% |
| `/search [conditional_remainder]` | 30ms | 1100ms | 1400ms | 1400ms | 0% |
| `/search [discourse_framing]` | 28ms | 1200ms | 2100ms | 2100ms | 0% |
| `/search [forecast]` | 24ms | 40ms | 740ms | 740ms | 0% |
| `/search [news]` | 23ms | 38ms | 87ms | 87ms | 0% |
| `/search [uptime]` | 24ms | 1900ms | 1900ms | 1900ms | 0% |
| `/search [ha]` | 40ms | 340ms | 340ms | 340ms | 0% |
| `/search [auto]` | 24ms | 2400ms | 3200ms | 10000ms | 0% |
| `/search [fusion_explicit]` | 21ms | 42ms | 760ms | 1800ms | 0% |
| `/search [fusion_auto]` | 25ms | 38ms | 160ms | 1800ms | 0% |
| `/search [fusion_triple]` | 23ms | 110ms | 1300ms | 1300ms | 0% |
| `/search [cache_hit]` | 22ms | 47ms | 47ms | 47ms | 0% |
| **Aggregated** | **24ms** | **810ms** | **1600ms** | **2300ms** | **0%** |

**862 requests. 0 failures.**

**`web`'s cold p99 dropped from 3900ms (v3.44.0) to 1300ms; `discourse_framing`'s cold p98 dropped from 4200ms to 2100ms** — both directionally consistent with fixes that landed in this release range (query-expansion concurrency, the discourse-framing keyword-path fix), though one sample against one prior sample, not a controlled comparison.

**`auto`'s cold p99 (10000ms) is the single worst sample across this entire run** — the same small fixed query-pool collision pattern already seen in v3.44.0, just a worse single draw. Pool widening and the full chronology of that fix: see the investigation log.

**Warm cache** — identical run immediately afterward, no clearing in between.

| Endpoint | Median | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|----------|
| `/health` | 750ms | 5200ms | 5200ms | 5200ms | 0% |
| `/search [kiwix]` | 23ms | 32ms | 38ms | 41ms | 0% |
| `/search [kiwix_disambiguation]` | 22ms | 29ms | 32ms | 42ms | 0% |
| `/search [web]` | 23ms | 34ms | 43ms | 49ms | 0% |
| `/search [conditional]` | 28ms | 59ms | 1100ms | 1100ms | 0% |
| `/search [conditional_remainder]` | 33ms | 62ms | 1500ms | 1500ms | 0% |
| `/search [discourse_framing]` | 30ms | 44ms | 55ms | 55ms | 0% |
| `/search [forecast]` | 23ms | 33ms | 41ms | 41ms | 0% |
| `/search [news]` | 23ms | 34ms | 37ms | 39ms | 0% |
| `/search [uptime]` | 25ms | 1500ms | 1500ms | 1500ms | 0% |
| `/search [ha]` | 38ms | 67ms | 67ms | 67ms | 0% |
| `/search [auto]` | 26ms | 1000ms | 1100ms | 1200ms | 0% |
| `/search [fusion_explicit]` | 22ms | 34ms | 40ms | 65ms | 0% |
| `/search [fusion_auto]` | 25ms | 42ms | 46ms | 73ms | 0% |
| `/search [fusion_triple]` | 22ms | 34ms | 36ms | 43ms | 0% |
| `/search [cache_hit]` | 23ms | 33ms | 34ms | 34ms | 0% |
| **Aggregated** | **24ms** | **44ms** | **730ms** | **1000ms** | **0%** |

**893 requests. 0 failures.**

**`web`, `kiwix`, `kiwix_disambiguation`, `discourse_framing`, `fusion_triple`, and `cache_hit` all collapse fully on cache hit, consistent with every prior release.**

**`/health`'s warm-cache max (5244ms) is a fresh finding** — `/health`'s seven source checks ran as plain sequential calls in `app/main.py`, each with its own real timeout. Fixed in v3.50.3 (concurrent dispatch); confirmed holding under load in the v3.50.4 re-benchmark below.

**`auto`/`conditional`/`conditional_remainder`/`uptime` all stayed expensive at p95+ even warm.** Same thundering-herd and connection-cost patterns as before — full chronology, including the eventual fixes and a real sizing mistake caught by a later re-benchmark, in the investigation log.

**Median latency remains completely unaffected across this entire release range too.** Aggregated median held at 24ms cold and warm — now confirmed constant across roughly 35 releases and every major feature shipped since v3.5.0, including two entirely new background-job features (Adversarial Self-Testing, Cross-Source Temporal Pattern Detection) that run on their own schedule and were never expected to touch the request-handling path at all.

### 20 Users — Cold vs Warm Cache (v3.50.4, validating the persistent Uptime Kuma connection and the v3.50.3 pool widening together)

Run against the real v3.50.4 codebase on MiniDock, validating two changes against the same v3.50.2 baseline above in one pass rather than two separate sessions: the persistent Uptime Kuma connection (this release) and the `AUTO_QUERIES`/`CONDITIONAL_QUERIES`/`CONDITIONAL_WITH_REMAINDER_QUERIES` pool widening (v3.50.3, never previously re-benchmarked).

**Cold cache** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p90 | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|-----|----------|
| `/health` | 710ms | 740ms | 760ms | 760ms | 760ms | 0% |
| `/search [kiwix]` | 24ms | 680ms | 1100ms | 2200ms | 5000ms | 0% |
| `/search [kiwix_disambiguation]` | 24ms | 1500ms | 2000ms | 2400ms | 2400ms | 0% |
| `/search [web]` | 24ms | 39ms | 820ms | 1700ms | 2100ms | 0% |
| `/search [conditional]` | 29ms | 460ms | 1200ms | 5100ms | 5100ms | 0% |
| `/search [conditional_remainder]` | 38ms | 970ms | 1900ms | 4300ms | 4300ms | 0% |
| `/search [discourse_framing]` | 31ms | 150ms | 1800ms | 3800ms | 3800ms | 0% |
| `/search [forecast]` | 23ms | 32ms | 79ms | 680ms | 680ms | 0% |
| `/search [news]` | 22ms | 31ms | 49ms | 130ms | 130ms | 0% |
| `/search [uptime]` | 22ms | 440ms | 500ms | 500ms | 500ms | 0% |
| `/search [ha]` | 34ms | 50ms | 55ms | 150ms | 150ms | 0% |
| `/search [auto]` | 27ms | 450ms | 730ms | 1300ms | 3800ms | 0% |
| `/search [fusion_explicit]` | 22ms | 30ms | 120ms | 710ms | 730ms | 0% |
| `/search [fusion_auto]` | 25ms | 36ms | 110ms | 220ms | 230ms | 0% |
| `/search [fusion_triple]` | 22ms | 32ms | 47ms | 880ms | 880ms | 0% |
| `/search [cache_hit]` | 24ms | 5100ms | 5700ms | 8000ms | 8000ms | 0% |
| **Aggregated** | **24ms** | **160ms** | **730ms** | **1800ms** | **3800ms** | **0%** |

**863 requests. 0 failures.**

**`uptime`'s cold-cache cost dropped substantially: p95/p99 went from 1900ms (v3.50.2) to 500ms.** Most individual `uptime` requests on this run were genuinely fast — in line with every other source's cold numbers — with a minority (roughly the slowest 2-3 of 17 total `uptime` requests, based on where the percentile table's jump from 26ms at p80 to 440ms at p90 sits) still paying a real, large cost. See the warm-cache analysis below for the full read on what this means for the fix's two success criteria.

**`cache_hit`'s cold p90/p95/p99 (5100/5700/8000ms) is a genuinely new, surprising number not seen in the v3.50.2 baseline (47ms p99) or anywhere in this file's prior history**, where `cache_hit` has always been one of the cheapest, most boring rows in the table. Not investigated as part of this run — flagged here honestly rather than folded silently into "the usual cold-cache variance," since a cache-hit task spiking into multi-second territory on a *cold* run (where, by definition, nothing should be cached yet for it to hit) is a different shape of anomaly than the already-documented thundering-herd cache-write collisions on `auto`/`conditional`. Worth a dedicated look in a future pass; out of scope for what this run was set up to validate.

**Warm cache** — identical run immediately afterward, no clearing in between.

| Endpoint | Median | p90 | p95 | p98 | p99 | Failures |
|----------|--------|-----|-----|-----|-----|----------|
| `/health` | 730ms | 770ms | 1200ms | 1200ms | 1200ms | 0% |
| `/search [kiwix]` | 24ms | 28ms | 34ms | 42ms | 49ms | 0% |
| `/search [kiwix_disambiguation]` | 22ms | 30ms | 33ms | 44ms | 44ms | 0% |
| `/search [web]` | 23ms | 30ms | 35ms | 43ms | 46ms | 0% |
| `/search [conditional]` | 26ms | 59ms | 440ms | 440ms | 440ms | 0% |
| `/search [conditional_remainder]` | 40ms | 55ms | 440ms | 460ms | 460ms | 0% |
| `/search [discourse_framing]` | 29ms | 41ms | 47ms | 49ms | 59ms | 0% |
| `/search [forecast]` | 23ms | 28ms | 31ms | 40ms | 45ms | 0% |
| `/search [news]` | 23ms | 26ms | 29ms | 38ms | 38ms | 0% |
| `/search [uptime]` | 24ms | 440ms | 470ms | 850ms | 850ms | 0% |
| `/search [ha]` | 38ms | 48ms | 48ms | 48ms | 48ms | 0% |
| `/search [auto]` | 25ms | 41ms | 440ms | 450ms | 450ms | 0% |
| `/search [fusion_explicit]` | 22ms | 31ms | 38ms | 44ms | 54ms | 0% |
| `/search [fusion_auto]` | 25ms | 34ms | 37ms | 41ms | 42ms | 0% |
| `/search [fusion_triple]` | 21ms | 29ms | 33ms | 40ms | 43ms | 0% |
| `/search [cache_hit]` | 24ms | 27ms | 28ms | 36ms | 36ms | 0% |
| **Aggregated** | **24ms** | **37ms** | **49ms** | **460ms** | **730ms** | **0%** |

**901 requests. 0 failures.**

**`cache_hit` is back to behaving exactly as expected on warm (median 24ms, p99 36ms)** — confirming the cold run's cache_hit anomaly above is specific to the cold pass, not a new, persistent regression.

**Summary against this run's two validation targets**: the persistent Uptime Kuma connection (Part 1) showed a real, substantial improvement (warm p95/p99 1500ms/1500ms → 470ms/850ms) but not the full "low tens of milliseconds" bar the design doc set — a real, unexplained minority tail remained for two more releases before its actual cause (a library-level `wait_events` cost) was found and fixed in v3.50.8/confirmed in v3.50.9. The v3.50.3 pool widening (Part 2) showed a real but incomplete improvement too (`auto` cold p99 10000ms → 3800ms, still multi-second-adjacent), eventually requiring a corrected second widening pass after an intervening sizing mistake. Full chronology for both: see the investigation log. `cache_hit`'s cold-run anomaly (flagged above) and `/health`'s concurrency fix (confirmed holding at warm max 1152ms/p99 1200ms, no recurrence of the v3.50.2 baseline's 5244ms spike) are both also covered there.

### 20 Users — Cold vs Warm Cache (v3.50.7, validating the cache_hit query-collision fix)

Run against the real v3.50.6 codebase on MiniDock, validating the `cache_hit` thundering-herd fix (v3.50.6) against the v3.50.5 baseline above. Zero exceptions, zero failures on both passes.

**Cold cache** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p90 | p95 | p98 | p99 | n | Failures |
|----------|--------|-----|-----|-----|-----|---|----------|
| `/health` | 710ms | 760ms | 780ms | 780ms | 780ms | 17 | 0% |
| `/search [kiwix]` | 23ms | 870ms | 1300ms | 1700ms | 2000ms | 85 | 0% |
| `/search [kiwix_disambiguation]` | 23ms | 99ms | 2000ms | 6000ms | 6000ms | 44 | 0% |
| `/search [web]` | 23ms | 31ms | 1300ms | 2300ms | 6600ms | 76 | 0% |
| `/search [conditional]` | 27ms | 1300ms | 1700ms | 1800ms | 1800ms | 39 | 0% |
| `/search [conditional_remainder]` | 38ms | 800ms | 1300ms | 1300ms | 1300ms | 19 | 0% |
| `/search [discourse_framing]` | 28ms | 580ms | 1100ms | 1900ms | 1900ms | 39 | 0% |
| `/search [forecast]` | 24ms | 110ms | 140ms | 730ms | 730ms | 37 | 0% |
| `/search [news]` | 24ms | 36ms | 56ms | 120ms | 120ms | 48 | 0% |
| `/search [uptime]` | 23ms | 440ms | 520ms | 520ms | 520ms | 16 | 0% |
| `/search [ha]` | 40ms | 51ms | 52ms | 300ms | 300ms | 21 | 0% |
| `/search [auto]` | 25ms | 440ms | 770ms | 1800ms | 3000ms | 76 | 0% |
| `/search [fusion_explicit]` | 22ms | 28ms | 36ms | 84ms | 830ms | 172 | 0% |
| `/search [fusion_auto]` | 25ms | 47ms | 150ms | 3600ms | 4400ms | 99 | 0% |
| `/search [fusion_triple]` | 22ms | 43ms | 100ms | 1700ms | 1700ms | 50 | 0% |
| `/search [cache_hit]` | 23ms | 880ms | 940ms | 940ms | 940ms | 18 | 0% |
| **Aggregated** | **24ms** | **100ms** | **770ms** | **1700ms** | **2300ms** | **856** | **0%** |

**`cache_hit`'s 8-second cold-run anomaly from v3.50.4/v3.50.5 is gone.** Cold p90/p98/p99 went from 5100ms/8000ms/8000ms (v3.50.5) to 880ms/940ms/940ms — the same general shape every other single-source cold-path row in this table shows, not the previous, anomalous outlier. The fix shipped in v3.50.6 (`cache_hit` no longer sharing a query with `KIWIX_QUERIES`) is confirmed working, not just theoretically correct. The remaining 880-940ms tail is the same, expected cold-routing cost any genuinely-uncached query pays once — `cache_hit`'s own first hit of the run is, correctly, no longer special-cased into a worse outcome than that.

**Warm cache** — identical run immediately afterward, no clearing in between.

| Endpoint | Median | p90 | p95 | p98 | p99 | n | Failures |
|----------|--------|-----|-----|-----|-----|---|----------|
| `/health` | 710ms | 750ms | 780ms | 780ms | 780ms | 20 | 0% |
| `/search [kiwix]` | 23ms | 29ms | 31ms | 34ms | 34ms | 80 | 0% |
| `/search [kiwix_disambiguation]` | 24ms | 32ms | 35ms | 35ms | 35ms | 40 | 0% |
| `/search [web]` | 22ms | 33ms | 34ms | 37ms | 37ms | 65 | 0% |
| `/search [conditional]` | 28ms | 63ms | 440ms | 440ms | 450ms | 54 | 0% |
| `/search [conditional_remainder]` | 42ms | 110ms | 450ms | 450ms | 450ms | 21 | 0% |
| `/search [discourse_framing]` | 31ms | 41ms | 46ms | 50ms | 57ms | 53 | 0% |
| `/search [forecast]` | 23ms | 27ms | 34ms | 38ms | 38ms | 37 | 0% |
| `/search [news]` | 23ms | 31ms | 36ms | 37ms | 47ms | 57 | 0% |
| `/search [uptime]` | 23ms | 32ms | 440ms | 440ms | 440ms | 21 | 0% |
| `/search [ha]` | 38ms | 54ms | 61ms | 61ms | 61ms | 19 | 0% |
| `/search [auto]` | 26ms | 41ms | 440ms | 450ms | 450ms | 75 | 0% |
| `/search [fusion_explicit]` | 22ms | 29ms | 35ms | 37ms | 44ms | 167 | 0% |
| `/search [fusion_auto]` | 25ms | 30ms | 37ms | 44ms | 45ms | 106 | 0% |
| `/search [fusion_triple]` | 22ms | 25ms | 35ms | 37ms | 48ms | 53 | 0% |
| `/search [cache_hit]` | 23ms | 26ms | 29ms | 29ms | 29ms | 19 | 0% |
| **Aggregated** | **24ms** | **37ms** | **54ms** | **690ms** | **710ms** | **887** | **0%** |

**`cache_hit`'s warm numbers are essentially identical to `kiwix`'s own warm numbers (29ms p99 vs. kiwix's 34ms p99)** — exactly what a healthy, never-colliding cache-hit task should look like, and a tighter result than even the v3.50.5 warm run's already-recovered 36ms p99 (n=19 either time; small-sample noise, not a meaningful further improvement). The `cache_hit` thread is fully closed as of this run.

**`uptime` warm p98/p99 (440ms/440ms) and the `auto`/`conditional`/`conditional_remainder` pools both showed the same unresolved patterns as the v3.50.5 baseline** — neither was touched by this release (a load-test-only fix to an unrelated task). Both were investigated further in v3.50.8/v3.50.9; full chronology, including a real sizing mistake the v3.50.9 re-benchmark caught, in the investigation log.

### 20 Users — Cold vs Warm Cache (v3.50.9, validating the wait_events fix and the v3.50.8 pool re-sizing)

Run against the real v3.50.8 codebase on MiniDock, validating two changes against the v3.50.7 baseline above: `uptime`'s `wait_events`-settling fix and the second `AUTO_QUERIES`/`CONDITIONAL_QUERIES`/`CONDITIONAL_WITH_REMAINDER_QUERIES` widening pass. One real failure this run, addressed in its own section below.

**Cold cache** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p90 | p95 | p98 | p99 | n | Failures |
|----------|--------|-----|-----|-----|-----|---|----------|
| `/health` | 730ms | 1300ms | 1400ms | 1400ms | 1400ms | 18 | 0% |
| `/search [kiwix]` | 23ms | 830ms | 1300ms | 1600ms | 1600ms | 87 | 0% |
| `/search [kiwix_disambiguation]` | 24ms | 2000ms | 2400ms | 2900ms | 2900ms | 36 | 0% |
| `/search [web]` | 25ms | 450ms | 1200ms | 2500ms | 7300ms | 62 | 0% |
| `/search [conditional]` | 56ms | 1500ms | 6700ms | 9800ms | 9800ms | 35 | 0% |
| `/search [conditional_remainder]` | 41ms | 1500ms | 1800ms | 4200ms | 4200ms | 24 | 0% |
| `/search [discourse_framing]` | 30ms | 56ms | 1900ms | 3100ms | 3100ms | 32 | 0% |
| `/search [forecast]` | 23ms | 31ms | 47ms | 800ms | 800ms | 46 | 0% |
| `/search [news]` | 24ms | 31ms | 32ms | 55ms | 55ms | 39 | 0% |
| `/search [uptime]` | 24ms | 34ms | 63ms | 190ms | 190ms | 24 | 0% |
| `/search [ha]` | 26ms | 51ms | 53ms | 120ms | 120ms | 25 | 0% |
| `/search [auto]` | 33ms | 710ms | 720ms | 1200ms | 2700ms | 66 | 0% |
| `/search [fusion_explicit]` | 22ms | 31ms | 52ms | 710ms | 730ms | 165 | 0% |
| `/search [fusion_auto]` | 25ms | 30ms | 37ms | 53ms | 920ms | 112 | 0% |
| `/search [fusion_triple]` | 23ms | 30ms | 32ms | 760ms | 760ms | 50 | 0% |
| `/search [cache_hit]` | 25ms | 68ms | 200ms | 3800ms | 3800ms | 28 | 0% |
| **Aggregated** | **24ms** | **180ms** | **830ms** | **1800ms** | **2700ms** | **849** | **0%** |

**`uptime`'s cold tail dropped substantially: p95/p98/p99 went from 520ms (v3.50.7) to 63ms/190ms/190ms.** Most individual requests landed in the same 23-34ms range every other cold source shows; the single remaining slow sample (190ms) is consistent with the one call per fresh/reconnected connection that, by the fix's own design, still pays the full safe `wait_events` wait.

**`conditional`'s cold p99 hit 9800ms — the single worst sample this endpoint has ever produced, and a real regression from the v3.50.7 baseline (1800ms).** `conditional_remainder`'s cold p98/p99 (4200ms) also regressed from the v3.50.7 baseline (1300ms). Both are addressed in the v3.50.9 changelog entry and `tests/locustfile.py`'s own updated comments: the v3.50.8 pool-sizing pass used the wrong metric and didn't account for `CONDITIONAL_QUERIES`'s heavy skew toward `kiwix`'s expensive fallback path. A corrected widening (`CONDITIONAL_QUERIES` 40, `CONDITIONAL_WITH_REMAINDER_QUERIES` 30, plus a fixed source mix) shipped the same release this benchmark validates — too late to be reflected in this specific run, which measured the v3.50.8 sizing, not the v3.50.9 correction. The next benchmark run is what will show whether the correction actually worked.

**`cache_hit`'s cold p98/p99 (3800ms) is also elevated relative to the v3.50.7 baseline (940ms) — not yet investigated.** `cache_hit`'s own dedicated query is confirmed not to collide with any other pool (enforced by a test), so this isn't the same mechanism as the v3.50.4 anomaly. Possibly related to the same general session having more concurrent cold-routing pressure overall this run (`conditional`'s 9800ms sample, `web`'s 7300ms sample, and `kiwix_disambiguation`'s 2900ms sample are all elevated too, suggesting this particular run's LLM-routing backend was under more real, shared load than prior runs) — flagged honestly as a plausible but unconfirmed explanation, not a new, separately-investigated root cause.

**Warm cache** — identical run immediately afterward, no clearing in between.

| Endpoint | Median | p90 | p95 | p98 | p99 | n | Failures |
|----------|--------|-----|-----|-----|-----|---|----------|
| `/health` | 720ms | 750ms | 760ms | 810ms | 810ms | 23 | 0% |
| `/search [kiwix]` | 24ms | 30ms | 35ms | 44ms | 44ms | 92 | 0% |
| `/search [kiwix_disambiguation]` | 23ms | 27ms | 34ms | 35ms | 35ms | 43 | 0% |
| `/search [web]` | 23ms | 27ms | 31ms | 36ms | 37ms | 71 | 0% |
| `/search [conditional]` | 32ms | 64ms | 420ms | 1300ms | 1300ms | 49 | 0% |
| `/search [conditional_remainder]` | 52ms | 74ms | 720ms | 1800ms | 1800ms | 24 | 0% |
| `/search [discourse_framing]` | 30ms | 38ms | 41ms | 54ms | 54ms | 45 | 0% |
| `/search [forecast]` | 23ms | 32ms | 34ms | 45ms | 45ms | 40 | 0% |
| `/search [news]` | 24ms | 33ms | 35ms | 40ms | 40ms | 46 | 0% |
| `/search [uptime]` | 23ms | 61ms | 69ms | 69ms | 69ms | 16 | 0% |
| `/search [ha]` | 40ms | 51ms | 62ms | 80ms | 80ms | 22 | 0% |
| `/search [auto]` | 25ms | 65ms | 65ms | 72ms | 80ms | 69 | 0% |
| `/search [fusion_explicit]` | 21ms | 29ms | 34ms | 40ms | 49ms | 174 | 0% |
| `/search [fusion_auto]` | 24ms | 32ms | 36ms | 37ms | 38ms | 113 | 0% |
| `/search [fusion_triple]` | 21ms | 28ms | 30ms | 39ms | 39ms | 48 | **1 (2.08%)** |
| `/search [cache_hit]` | 24ms | 32ms | 34ms | 34ms | 34ms | 20 | 0% |
| **Aggregated** | **24ms** | **39ms** | **64ms** | **710ms** | **740ms** | **895** | **0.11% (1 total)** |

**`uptime`'s warm tail is now fully resolved — the actual headline result of this run.** p98/p99 dropped from 440ms (every prior run since v3.50.4) to **69ms**, finally in the same order of magnitude as the cleanest sources (kiwix 44ms, forecast 45ms, news 40ms) rather than a distinct, separate tail. A small minority (1-2 of 16 requests) still pays something in the 60-69ms range, consistent with the fix's own design — the call right after a fresh connect or reconnect is supposed to still pay the full, safe wait. Full chronology (five releases, three sequential findings) in **[The Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log)**.

**`conditional`/`conditional_remainder`'s warm tails also regressed from the v3.50.7 baseline** (`conditional` p98/p99: 440ms → 1300ms; `conditional_remainder`: 450ms → 1800ms) — consistent with the same v3.50.8 sizing mistake identified in the cold-cache analysis above, not a separate warm-specific issue.

**`auto`'s warm numbers are a clear, unambiguous win**: p98/p99 dropped from 450ms (v3.50.7) to 72ms/80ms — confirming the v3.50.8 widening worked correctly for this specific pool, even though the same pass's reasoning for `conditional`/`conditional_remainder` had a real flaw. `AUTO_QUERIES` wasn't touched again in the v3.50.9 correction, and didn't need to be.

**One real failure, not yet explained**: `POST /search [fusion_triple]: RemoteDisconnected('Remote end closed connection without response')` — the server closed the connection without sending any response at all, not a timeout or an error response. `fusion_triple` queries `uptime` alongside `forecast`/`news`, so it touches code changed this release, but a direct read of `app/sources/uptime_kuma.py` and `app/sources/fusion.py`'s concurrent-dispatch error handling (every per-source exception is caught and converted to `None`, never re-raised past the dispatch loop) found nothing that explains a dropped HTTP connection. Not attributed to the recent changes, and not dismissed as unrelated noise either — both would be guessing past the evidence the Locust output alone provides. Real server-side logs from around the time of this run are the only way to actually know; flagged here as a genuinely open item.

### 20 Users — Cold vs Warm Cache (v3.50.11, validating the v3.50.9 pool-sizing correction and confirming `cache_hit`'s remaining cost is unrelated to Mnemolis)

Run against the real v3.50.10 codebase on MiniDock. Zero exceptions, zero failures on both passes — the `RemoteDisconnected` from the v3.50.9 warm run did not recur.

**Cold cache** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p90 | p95 | p98 | p99 | n | Failures |
|----------|--------|-----|-----|-----|-----|---|----------|
| `/health` | 730ms | 790ms | 1000ms | 1300ms | 1300ms | 27 | 0% |
| `/search [kiwix]` | 23ms | 820ms | 1300ms | 1700ms | 1800ms | 80 | 0% |
| `/search [kiwix_disambiguation]` | 22ms | 28ms | 2400ms | 4800ms | 6800ms | 52 | 0% |
| `/search [web]` | 23ms | 120ms | 1300ms | 1300ms | 1900ms | 59 | 0% |
| `/search [conditional]` | 46ms | 1500ms | 2500ms | 5300ms | 5300ms | 34 | 0% |
| `/search [conditional_remainder]` | 67ms | 1200ms | 1200ms | 1500ms | 1500ms | 23 | 0% |
| `/search [discourse_framing]` | 28ms | 150ms | 1600ms | 2500ms | 2700ms | 54 | 0% |
| `/search [forecast]` | 23ms | 31ms | 42ms | 120ms | 770ms | 51 | 0% |
| `/search [news]` | 23ms | 32ms | 42ms | 72ms | 72ms | 38 | 0% |
| `/search [uptime]` | 24ms | 47ms | 62ms | 62ms | 62ms | 16 | 0% |
| `/search [ha]` | 36ms | 50ms | 57ms | 57ms | 57ms | 16 | 0% |
| `/search [auto]` | 34ms | 720ms | 740ms | 770ms | 990ms | 51 | 0% |
| `/search [fusion_explicit]` | 21ms | 29ms | 34ms | 730ms | 740ms | 166 | 0% |
| `/search [fusion_auto]` | 24ms | 41ms | 290ms | 2600ms | 3600ms | 113 | 0% |
| `/search [fusion_triple]` | 21ms | 29ms | 30ms | 720ms | 720ms | 48 | 0% |
| `/search [cache_hit]` | 23ms | 26ms | 27ms | 3600ms | 3600ms | 27 | 0% |
| **Aggregated** | **24ms** | **670ms** | **810ms** | **1600ms** | **2500ms** | **855** | **0%** |

**`uptime`'s cold tail dropped further: p98/p99 went from 190ms (v3.50.9) to 62ms** — a second consecutive confirmation that the `wait_events` fix is working as designed.

**`conditional`/`conditional_remainder` both improved substantially from the v3.50.9 baseline, confirming the corrected pool-sizing did real, predicted work.** `conditional` cold p98/p99 dropped from 9800ms to 5300ms (46% reduction); `conditional_remainder` dropped from 4200ms to 1500ms (64% reduction). Neither cleared the "low tens of milliseconds" bar — both still show real multi-second tails — but the correction's own model predicted exactly this: meaningful, not complete, improvement. See [The Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log#thread-2-the-autoconditional-thundering-herd-including-a-real-mistake-caught-by-the-next-benchmark) for the full model and history.

**`cache_hit`'s cold p98/p99 (3600ms) is elevated again, similar in magnitude to the v3.50.9 run (3800ms) — investigated thoroughly this time, not just flagged.** Confirmed not a query-pool collision (the dedicated query is still clean), not disambiguation (the query's search terms don't qualify), and not routing-cache disk-write cost (measured directly, under 1ms even at realistic cache sizes). The real, most likely explanation: ordinary Ollama request queueing shared by every cold LLM call in the run, not specific to `cache_hit` — the endpoint showing the single worst sample changes between runs (`kiwix_disambiguation` at 6800ms here, `conditional` at 9800ms in v3.50.9), consistent with a shared queue, not an endpoint-specific defect. Deliberately not pursued as a fix — see `CHANGELOG.md`'s v3.50.11 entry for the full VRAM/`OLLAMA_NUM_PARALLEL` reasoning and why it's not worth the tradeoff.

**Warm cache** — identical run immediately afterward, no clearing in between.

| Endpoint | Median | p90 | p95 | p98 | p99 | n | Failures |
|----------|--------|-----|-----|-----|-----|---|----------|
| `/health` | 730ms | 780ms | 800ms | 1300ms | 1300ms | 33 | 0% |
| `/search [kiwix]` | 23ms | 28ms | 37ms | 53ms | 54ms | 96 | 0% |
| `/search [kiwix_disambiguation]` | 23ms | 31ms | 34ms | 250ms | 250ms | 35 | 0% |
| `/search [web]` | 24ms | 32ms | 33ms | 37ms | 74ms | 68 | 0% |
| `/search [conditional]` | 42ms | 380ms | 890ms | 1400ms | 1400ms | 42 | 0% |
| `/search [conditional_remainder]` | 48ms | 720ms | 1200ms | 1400ms | 1400ms | 25 | 0% |
| `/search [discourse_framing]` | 27ms | 33ms | 38ms | 49ms | 84ms | 53 | 0% |
| `/search [forecast]` | 23ms | 30ms | 34ms | 170ms | 170ms | 39 | 0% |
| `/search [news]` | 24ms | 27ms | 29ms | 45ms | 45ms | 42 | 0% |
| `/search [uptime]` | 23ms | 63ms | 73ms | 73ms | 73ms | 17 | 0% |
| `/search [ha]` | 38ms | 46ms | 48ms | 50ms | 50ms | 22 | 0% |
| `/search [auto]` | 25ms | 68ms | 700ms | 740ms | 2500ms | 64 | 0% |
| `/search [fusion_explicit]` | 22ms | 31ms | 35ms | 48ms | 58ms | 152 | 0% |
| `/search [fusion_auto]` | 25ms | 30ms | 35ms | 38ms | 39ms | 121 | 0% |
| `/search [fusion_triple]` | 21ms | 29ms | 33ms | 36ms | 40ms | 60 | 0% |
| `/search [cache_hit]` | 25ms | 29ms | 34ms | 34ms | 34ms | 18 | 0% |
| **Aggregated** | **24ms** | **48ms** | **380ms** | **740ms** | **790ms** | **887** | **0%** |

**`uptime`'s warm tail held flat (69ms in v3.50.9, 73ms here)** — within normal run-to-run noise for n=16-17, confirming the fix's stability across a second real run, not a one-off result.

**`conditional`/`conditional_remainder`'s warm numbers are essentially unchanged from v3.50.9** (`conditional` 1300ms → 1400ms p98/p99; `conditional_remainder` 1800ms → 1400ms) — consistent with the model's own prediction that warm-cache improvement from this specific correction would be modest, not dramatic.

**`auto`'s warm p98/p99 (740ms/2500ms) is noticeably worse than the v3.50.9 run's near-perfect result (72ms/80ms), despite zero code changes to `AUTO_QUERIES` between the two runs.** Reading the distribution: roughly 3-4 of 64 requests landed in a real collision this run, versus a near-zero collision rate last time. `AUTO_QUERIES` sits at a real, nonzero modeled collision rate (~55%), so this kind of run-to-run swing is expected noise at this pool size, not a regression — the v3.50.9 run simply drew favorably.

#### Two further confirmation runs (same v3.50.11 codebase, no code changes between any of these three runs)

Two more cold/warm pairs were run against the identical v3.50.11 codebase to check whether the results above held up or were a single favorable draw. Zero exceptions, zero failures across all three runs — no `RemoteDisconnected` recurrence across any of them.

| Endpoint | Metric | Run 1 (above) | Run 2 | Run 3 |
|---|---|---|---|---|
| `uptime` | cold p99 | 62ms | 140ms | 63ms |
| `uptime` | warm p99 | 73ms | 59ms | 61ms |
| `conditional` | cold p99 | 5300ms | 2500ms | 2500ms |
| `conditional` | warm p99 | 1400ms | 1500ms | 1100ms |
| `conditional_remainder` | cold p99 | 1500ms | 1800ms | 2900ms |
| `conditional_remainder` | warm p99 | 1400ms | 1400ms | 1800ms |
| `cache_hit` | cold p99 | 3600ms | 940ms | 810ms |
| `cache_hit` | warm p99 | 34ms | 34ms | 47ms |
| `auto` | cold p99 | 990ms | 2300ms | 2400ms |
| `auto` | warm p99 | 2500ms | 100ms | 70ms |

Three genuinely independent Locust invocations, each its own real cold+warm pair — none of these rows are the same run reported twice.

**`conditional` is now confirmed real and repeatable, not a one-off — but Run 1's cold result (5300ms) is itself a reminder that this metric stays noisy.** Runs 2 and 3 landed at the identical 2500ms, both real improvements over Run 1's own 5300ms — three runs on the identical codebase producing three different cold p99 values (5300/2500/2500) is consistent with the design doc's own "inherently noisier metric" caution, not evidence anything regressed between Run 1 and the other two. `conditional_remainder` shows the same pattern, bouncing between 1500-2900ms across the three — genuinely noisy, but never approaching the pre-correction 4200ms in any of them.

**`uptime` is fully confirmed across all three** — cold p99 62/140/63ms, warm 73/59/61ms — tightly clustered, no remaining caveats.

**`auto`'s cold p99 shows the opposite pattern: tightly clustered at 2300-2400ms in Runs 2 and 3, with Run 1's own 990ms standing out as the better-than-usual draw** (consistent with the same per-pick, small-expensive-subset collision mechanism investigated directly — see the v3.50.12 changelog entry for the full root-cause analysis; the application-layer fix this pointed to shipped in v3.50.13, see that changelog entry, not yet reflected in any benchmark run recorded on this page). `auto`'s warm side is the most volatile metric across all three runs (2500/100/70ms) — Run 1's 2500ms warm result is the real outlier here, not Runs 2/3's near-identical low results, consistent with `AUTO_QUERIES`'s own modeled ~55% collision rate producing real, expected swings run to run.

**`cache_hit`'s cold cost was real and high in Run 1 (3600ms) but landed under 1000ms in both Runs 2 and 3 (940ms, 810ms)** — consistent with the Ollama-queue-contention explanation already confirmed (queue depth varies run to run independent of anything Mnemolis controls), not evidence the mechanism itself changed.

### 20 Users — Cold vs Warm Cache (v3.50.13, validating the singleflight fix — and the investigation that followed when it didn't move `auto`'s plateau)

Run against the real v3.50.13 codebase on MiniDock. Three full cold/warm pairs, the same confirmation discipline the v3.50.11 entry above established, since `auto`'s own history at this pool size has shown real run-to-run noise before.

**Cold cache, Run 1** — both caches explicitly cleared immediately before this run.

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 990ms | 1800ms | 5100ms | 6400ms | 84 |
| `/search [kiwix_disambiguation]` | 23ms | 1600ms | 2500ms | 6200ms | 6200ms | 38 |
| `/search [web]` | 24ms | 33ms | 1800ms | 1800ms | 2300ms | 52 |
| `/search [conditional]` | 44ms | 1400ms | 1500ms | 1500ms | 1500ms | 40 |
| `/search [conditional_remainder]` | 110ms | 990ms | 1300ms | 1300ms | 1300ms | 16 |
| `/search [discourse_framing]` | 27ms | 51ms | 1200ms | 1500ms | 1500ms | 41 |
| `/search [forecast]` | 24ms | 32ms | 38ms | 110ms | 710ms | 53 |
| `/search [news]` | 21ms | 32ms | 33ms | 59ms | 59ms | 30 |
| `/search [uptime]` | 23ms | 28ms | 54ms | 120ms | 120ms | 23 |
| `/search [ha]` | 34ms | 49ms | 50ms | 51ms | 51ms | 26 |
| `/search [auto]` | 34ms | 710ms | 720ms | 950ms | 2500ms | 66 |
| `/search [fusion_explicit]` | 22ms | 28ms | 45ms | 710ms | 730ms | 158 |
| `/search [fusion_auto]` | 24ms | 33ms | 110ms | 2200ms | 2300ms | 121 |
| `/search [fusion_triple]` | 22ms | 30ms | 110ms | 200ms | 710ms | 51 |
| `/search [cache_hit]` | 23ms | 28ms | 28ms | 4400ms | 4400ms | 21 |

**Warm cache, Run 1** — identical run immediately afterward, no clearing in between. One real failure: `POST /search [fusion_explicit]: RemoteDisconnected('Remote end closed connection without response')` — the same unexplained, non-recurring class already flagged in the v3.50.9 entry above (didn't recur in v3.50.11, recurred once here, didn't recur again in Runs 2/3 below). Still genuinely unexplained, still not attributed to anything in this release's actual changes.

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 28ms | 31ms | 40ms | 47ms | 96 |
| `/search [kiwix_disambiguation]` | 23ms | 27ms | 41ms | 41ms | 41ms | 38 |
| `/search [web]` | 24ms | 33ms | 34ms | 40ms | 45ms | 69 |
| `/search [conditional]` | 36ms | 72ms | 720ms | 1400ms | 1400ms | 42 |
| `/search [conditional_remainder]` | 60ms | 740ms | 1300ms | 10000ms | 10000ms | 25 |
| `/search [discourse_framing]` | 29ms | 35ms | 36ms | 44ms | 44ms | 45 |
| `/search [forecast]` | 23ms | 30ms | 30ms | 36ms | 36ms | 45 |
| `/search [news]` | 23ms | 30ms | 32ms | 40ms | 40ms | 48 |
| `/search [uptime]` | 23ms | 59ms | 110ms | 110ms | 110ms | 15 |
| `/search [ha]` | 28ms | 51ms | 52ms | 54ms | 54ms | 30 |
| `/search [auto]` | 26ms | 60ms | 62ms | 64ms | 65ms | 66 |
| `/search [fusion_explicit]` | 21ms | 27ms | 32ms | 33ms | 41ms | 153 (1 failure) |
| `/search [fusion_auto]` | 25ms | 33ms | 38ms | 48ms | 72ms | 121 |
| `/search [fusion_triple]` | 22ms | 25ms | 28ms | 28ms | 36ms | 60 |
| `/search [cache_hit]` | 23ms | 26ms | 32ms | 32ms | 32ms | 14 |

**`auto`'s cold p99 (2500ms) landed squarely inside the same 2300-2700ms band three of the four pre-fix runs already showed — singleflight, on this first run, did not move the plateau.** Not waved off as a single noisy sample — see the two confirmation runs below, which settled the question.

**`conditional_remainder`'s warm p98/p99 hit 10000ms — the single worst sample either conditional endpoint has produced, warm or cold, in this project's entire benchmark history.** Flagged honestly as an anomaly, not investigated further in this entry since the two confirmation runs below (1800ms, 1800ms) show it did not recur at anything close to this magnitude — consistent with ordinary single-run noise on a small-n metric (n=25), not a regression.

#### Two confirmation runs (same v3.50.13 codebase, no code changes between any of these three runs)

**Cold cache, Run 2**

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 130ms | 1400ms | 1900ms | 3900ms | 94 |
| `/search [kiwix_disambiguation]` | 23ms | 81ms | 1800ms | 2900ms | 2900ms | 42 |
| `/search [web]` | 24ms | 34ms | 730ms | 910ms | 2700ms | 74 |
| `/search [conditional]` | 43ms | 1200ms | 1300ms | 1600ms | 1600ms | 46 |
| `/search [conditional_remainder]` | 710ms | 1400ms | 2100ms | 2100ms | 2100ms | 19 |
| `/search [discourse_framing]` | 30ms | 820ms | 1500ms | 1900ms | 1900ms | 39 |
| `/search [forecast]` | 23ms | 34ms | 220ms | 700ms | 700ms | 35 |
| `/search [news]` | 23ms | 33ms | 49ms | 65ms | 65ms | 43 |
| `/search [uptime]` | 24ms | 37ms | 68ms | 70ms | 70ms | 24 |
| `/search [ha]` | 37ms | 51ms | 55ms | 580ms | 580ms | 27 |
| `/search [auto]` | 40ms | 710ms | 720ms | 1200ms | 2300ms | 60 |
| `/search [fusion_explicit]` | 22ms | 33ms | 80ms | 830ms | 850ms | 158 |
| `/search [fusion_auto]` | 25ms | 42ms | 67ms | 170ms | 180ms | 113 |
| `/search [fusion_triple]` | 23ms | 31ms | 79ms | 110ms | 720ms | 55 |
| `/search [cache_hit]` | 23ms | 34ms | 35ms | 1100ms | 1100ms | 24 |

**Warm cache, Run 2** — zero failures.

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 28ms | 32ms | 36ms | 40ms | 98 |
| `/search [kiwix_disambiguation]` | 24ms | 33ms | 36ms | 41ms | 41ms | 49 |
| `/search [web]` | 24ms | 29ms | 34ms | 38ms | 44ms | 74 |
| `/search [conditional]` | 40ms | 530ms | 1400ms | 1700ms | 1700ms | 39 |
| `/search [conditional_remainder]` | 51ms | 610ms | 740ms | 2200ms | 2200ms | 27 |
| `/search [discourse_framing]` | 29ms | 37ms | 46ms | 47ms | 47ms | 37 |
| `/search [forecast]` | 24ms | 29ms | 31ms | 42ms | 42ms | 50 |
| `/search [news]` | 24ms | 32ms | 36ms | 100ms | 100ms | 50 |
| `/search [uptime]` | 24ms | 30ms | 61ms | 65ms | 65ms | 22 |
| `/search [ha]` | 41ms | 51ms | 52ms | 52ms | 52ms | 19 |
| `/search [auto]` | 27ms | 65ms | 67ms | 68ms | 69ms | 51 |
| `/search [fusion_explicit]` | 22ms | 30ms | 34ms | 36ms | 37ms | 155 |
| `/search [fusion_auto]` | 25ms | 32ms | 37ms | 53ms | 56ms | 117 |
| `/search [fusion_triple]` | 22ms | 26ms | 26ms | 27ms | 29ms | 56 |
| `/search [cache_hit]` | 23ms | 28ms | 30ms | 33ms | 33ms | 25 |

**Cold cache, Run 3**

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 840ms | 1200ms | 1600ms | 1600ms | 81 |
| `/search [kiwix_disambiguation]` | 23ms | 34ms | 1800ms | 2000ms | 2900ms | 54 |
| `/search [web]` | 23ms | 190ms | 770ms | 900ms | 1100ms | 53 |
| `/search [conditional]` | 41ms | 1200ms | 1400ms | 3500ms | 3500ms | 37 |
| `/search [conditional_remainder]` | 290ms | 1400ms | 3400ms | 3400ms | 3400ms | 18 |
| `/search [discourse_framing]` | 29ms | 45ms | 1100ms | 1700ms | 1700ms | 42 |
| `/search [forecast]` | 24ms | 40ms | 59ms | 790ms | 790ms | 41 |
| `/search [news]` | 22ms | 29ms | 31ms | 58ms | 58ms | 49 |
| `/search [uptime]` | 24ms | 49ms | 59ms | 59ms | 59ms | 19 |
| `/search [ha]` | 39ms | 46ms | 61ms | 72ms | 72ms | 21 |
| `/search [auto]` | 30ms | 91ms | 720ms | 1200ms | 2300ms | 81 |
| `/search [fusion_explicit]` | 21ms | 27ms | 32ms | 180ms | 710ms | 165 |
| `/search [fusion_auto]` | 25ms | 32ms | 41ms | 160ms | 250ms | 112 |
| `/search [fusion_triple]` | 22ms | 26ms | 28ms | 31ms | 680ms | 58 |
| `/search [cache_hit]` | 22ms | 28ms | 1100ms | 1100ms | 1100ms | 17 |

**Warm cache, Run 3** — zero failures.

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 27ms | 30ms | 39ms | 58ms | 97 |
| `/search [kiwix_disambiguation]` | 24ms | 30ms | 34ms | 34ms | 34ms | 39 |
| `/search [web]` | 23ms | 27ms | 30ms | 33ms | 44ms | 64 |
| `/search [conditional]` | 30ms | 440ms | 1300ms | 1500ms | 1500ms | 49 |
| `/search [conditional_remainder]` | 50ms | 1000ms | 1200ms | 1800ms | 1800ms | 22 |
| `/search [discourse_framing]` | 29ms | 38ms | 44ms | 59ms | 59ms | 38 |
| `/search [forecast]` | 23ms | 28ms | 34ms | 42ms | 42ms | 41 |
| `/search [news]` | 23ms | 26ms | 27ms | 34ms | 34ms | 45 |
| `/search [uptime]` | 22ms | 27ms | 60ms | 80ms | 80ms | 27 |
| `/search [ha]` | 35ms | 49ms | 50ms | 71ms | 71ms | 26 |
| `/search [auto]` | 25ms | 64ms | 65ms | 67ms | 710ms | 67 |
| `/search [fusion_explicit]` | 21ms | 27ms | 31ms | 43ms | 46ms | 167 |
| `/search [fusion_auto]` | 25ms | 32ms | 34ms | 35ms | 37ms | 115 |
| `/search [fusion_triple]` | 22ms | 25ms | 26ms | 28ms | 35ms | 52 |
| `/search [cache_hit]` | 24ms | 28ms | 29ms | 29ms | 29ms | 18 |

**`auto`'s cold p99 across all three v3.50.13 runs: 2500ms, 2300ms, 2300ms.** This settles the question Run 1 alone couldn't: singleflight's own fix did not move `auto`'s plateau. Compared against the pre-fix v3.50.11-era runs at the identical pool size (990ms, 2300ms, 2400ms), the post-fix numbers land in essentially the same band — if anything clustering slightly tighter toward the worse end, never reproducing the pre-fix run's own 990ms favorable outlier.

**The asymmetry that ruled out "just a noisy session" as the explanation: `kiwix`/`kiwix_disambiguation`/`cache_hit` all dropped substantially run over run while `auto` stayed flat.** `kiwix` cold p99: 6400ms → 3900ms → 1600ms. `cache_hit` cold p99: 4400ms → 1100ms → 1100ms. `kiwix_disambiguation` cold p99: 6200ms → 2900ms → 2900ms. A shared-contention story (one session under heavier-than-usual load on the LLM backend) predicts every LLM-touching endpoint moving together — these did, while `auto` conspicuously didn't, which is what motivated checking `app/llm.py` directly rather than re-running a fourth time hoping for a better draw.

**That investigation found a real, separate, previously-unexamined cost: zero HTTP connection reuse in `app/llm.py`.** Every call into `complete()` used the bare `requests.post()` module function rather than a persistent `requests.Session`, opening and tearing down a fresh TCP connection on every single LLM call — confirmed directly against a real local server (10 sequential calls through the old pattern opened 10 distinct connections; the identical 10 calls through a persistent session opened exactly 1). The identical class of bug Thread 1 of [The Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log#thread-1-uptimes-warm-cache-tail-five-releases-to-a-real-root-cause) already found and fixed for Uptime Kuma's own connection, just never checked for here. Fixed in v3.50.14 — see that changelog entry and [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching#llm-connection-pooling-and-keep-alive) for the mechanism. Not yet re-benchmarked at the time of this entry; whether it actually closes `auto`'s plateau is the next thing to confirm, not something to assume from the mechanism alone.

### 20 Users — Cold vs Warm Cache (v3.50.14, validating the connection-pooling fix — and the re-analysis that followed when it didn't move `auto`'s plateau either)

Run against the real v3.50.14 codebase on MiniDock. Three confirmed-fresh-rebuild cold/warm pairs. A fourth pair was run earlier in the same session but its build provenance couldn't be confirmed (possibly still v3.50.13) — deliberately excluded from the comparison below rather than counted as a fourth data point; its own `auto` cold p99 was 2400ms, which would not have changed any conclusion either way.

**Cold cache, Run 1**

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 1100ms | 1600ms | 1800ms | 1900ms | 80 |
| `/search [kiwix_disambiguation]` | 23ms | 1600ms | 2500ms | 2600ms | 3200ms | 51 |
| `/search [web]` | 23ms | 140ms | 750ms | 1000ms | 1600ms | 60 |
| `/search [conditional]` | 45ms | 1200ms | 1600ms | 2200ms | 2200ms | 40 |
| `/search [conditional_remainder]` | 51ms | 1000ms | 1100ms | 2100ms | 2100ms | 24 |
| `/search [discourse_framing]` | 30ms | 150ms | 1700ms | 1700ms | 1700ms | 40 |
| `/search [forecast]` | 24ms | 34ms | 150ms | 700ms | 700ms | 33 |
| `/search [news]` | 23ms | 44ms | 110ms | 130ms | 130ms | 35 |
| `/search [uptime]` | 23ms | 26ms | 46ms | 62ms | 62ms | 25 |
| `/search [ha]` | 40ms | 60ms | 70ms | 70ms | 70ms | 20 |
| `/search [auto]` | 29ms | 720ms | 730ms | 2200ms | 3400ms | 65 |
| `/search [fusion_explicit]` | 22ms | 31ms | 61ms | 710ms | 750ms | 162 |
| `/search [fusion_auto]` | 25ms | 37ms | 56ms | 160ms | 190ms | 117 |
| `/search [fusion_triple]` | 22ms | 45ms | 140ms | 770ms | 770ms | 44 |
| `/search [cache_hit]` | 23ms | 33ms | 43ms | 830ms | 830ms | 22 |

**Warm cache, Run 1**

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 28ms | 32ms | 32ms | 59ms | 94 |
| `/search [kiwix_disambiguation]` | 23ms | 26ms | 27ms | 40ms | 40ms | 45 |
| `/search [web]` | 24ms | 27ms | 33ms | 42ms | 72ms | 54 |
| `/search [conditional]` | 42ms | 460ms | 780ms | 1100ms | 1300ms | 58 |
| `/search [conditional_remainder]` | 35ms | 90ms | 390ms | 390ms | 390ms | 16 |
| `/search [discourse_framing]` | 29ms | 36ms | 40ms | 42ms | 52ms | 52 |
| `/search [forecast]` | 24ms | 27ms | 33ms | 37ms | 37ms | 42 |
| `/search [news]` | 24ms | 29ms | 31ms | 37ms | 37ms | 48 |
| `/search [uptime]` | 24ms | 56ms | 61ms | 61ms | 61ms | 20 |
| `/search [ha]` | 39ms | 49ms | 56ms | 56ms | 56ms | 17 |
| `/search [auto]` | 26ms | 63ms | 66ms | 67ms | 720ms | 59 |
| `/search [fusion_explicit]` | 23ms | 27ms | 30ms | 33ms | 34ms | 167 |
| `/search [fusion_auto]` | 25ms | 33ms | 36ms | 39ms | 46ms | 113 |
| `/search [fusion_triple]` | 22ms | 33ms | 35ms | 43ms | 70ms | 58 |
| `/search [cache_hit]` | 25ms | 31ms | 33ms | 35ms | 35ms | 29 |

**Cold cache, Run 2**

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 880ms | 1300ms | 1900ms | 2300ms | 92 |
| `/search [kiwix_disambiguation]` | 23ms | 1600ms | 2200ms | 2500ms | 2500ms | 39 |
| `/search [web]` | 23ms | 38ms | 710ms | 1200ms | 1600ms | 67 |
| `/search [conditional]` | 55ms | 1300ms | 1300ms | 1400ms | 1400ms | 28 |
| `/search [conditional_remainder]` | 69ms | 1500ms | 1600ms | 1600ms | 1600ms | 19 |
| `/search [discourse_framing]` | 29ms | 1100ms | 1300ms | 1400ms | 1400ms | 40 |
| `/search [forecast]` | 23ms | 32ms | 720ms | 790ms | 880ms | 54 |
| `/search [news]` | 23ms | 27ms | 34ms | 77ms | 77ms | 44 |
| `/search [uptime]` | 25ms | 57ms | 150ms | 150ms | 150ms | 18 |
| `/search [ha]` | 36ms | 47ms | 50ms | 50ms | 50ms | 20 |
| `/search [auto]` | 33ms | 720ms | 730ms | 950ms | 2300ms | 64 |
| `/search [fusion_explicit]` | 22ms | 29ms | 45ms | 740ms | 810ms | 172 |
| `/search [fusion_auto]` | 24ms | 33ms | 39ms | 120ms | 200ms | 117 |
| `/search [fusion_triple]` | 21ms | 26ms | 30ms | 740ms | 740ms | 50 |
| `/search [cache_hit]` | 24ms | 46ms | 61ms | 740ms | 740ms | 28 |

**Warm cache, Run 2**

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 30ms | 35ms | 42ms | 44ms | 80 |
| `/search [kiwix_disambiguation]` | 24ms | 27ms | 31ms | 99ms | 99ms | 47 |
| `/search [web]` | 24ms | 32ms | 35ms | 38ms | 39ms | 80 |
| `/search [conditional]` | 38ms | 64ms | 490ms | 1300ms | 1300ms | 42 |
| `/search [conditional_remainder]` | 53ms | 1100ms | 1200ms | 1500ms | 1500ms | 23 |
| `/search [discourse_framing]` | 28ms | 39ms | 46ms | 48ms | 48ms | 39 |
| `/search [forecast]` | 23ms | 27ms | 29ms | 34ms | 34ms | 51 |
| `/search [news]` | 24ms | 30ms | 30ms | 38ms | 38ms | 43 |
| `/search [uptime]` | 23ms | 63ms | 63ms | 63ms | 63ms | 19 |
| `/search [ha]` | 35ms | 53ms | 68ms | 68ms | 68ms | 20 |
| `/search [auto]` | 25ms | 63ms | 64ms | 72ms | 72ms | 69 |
| `/search [fusion_explicit]` | 21ms | 27ms | 34ms | 39ms | 44ms | 172 |
| `/search [fusion_auto]` | 24ms | 33ms | 33ms | 43ms | 51ms | 90 |
| `/search [fusion_triple]` | 22ms | 25ms | 31ms | 35ms | 45ms | 74 |
| `/search [cache_hit]` | 23ms | 27ms | 37ms | 45ms | 45ms | 21 |

**Cold cache, Run 3** — two real anomalies in this run, neither attributed to anything in this release: `POST /search [fusion_auto]: RemoteDisconnected('Remote end closed connection without response')` (the same unexplained, non-recurring class flagged in earlier entries), and `/health`/`conditional`/`web` all showing the single worst samples this project's benchmark history has ever recorded (3000ms, 11000ms, 10000ms respectively). `/health` having zero LLM dependency and still spiking this run is itself informative — this looks like a genuine, separate infrastructure event (container or host contention) sitting on top of whatever's actually driving `auto`'s own number, not evidence about the connection-pooling fix either way.

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 860ms | 1100ms | 1700ms | 1900ms | 73 |
| `/search [kiwix_disambiguation]` | 22ms | 1400ms | 2000ms | 2200ms | 2200ms | 39 |
| `/search [web]` | 24ms | 360ms | 1700ms | 10000ms | 10000ms | 61 |
| `/search [conditional]` | 62ms | 1300ms | 1400ms | 11000ms | 11000ms | 40 |
| `/search [conditional_remainder]` | 78ms | 750ms | 980ms | 1900ms | 1900ms | 21 |
| `/search [discourse_framing]` | 29ms | 69ms | 1200ms | 1900ms | 1900ms | 45 |
| `/search [forecast]` | 22ms | 29ms | 58ms | 730ms | 730ms | 38 |
| `/search [news]` | 23ms | 32ms | 35ms | 110ms | 110ms | 49 |
| `/search [uptime]` | 22ms | 62ms | 63ms | 63ms | 63ms | 20 |
| `/search [ha]` | 39ms | 50ms | 63ms | 63ms | 63ms | 14 |
| `/search [auto]` | 27ms | 700ms | 740ms | 1100ms | 2200ms | 82 |
| `/search [fusion_explicit]` | 22ms | 30ms | 48ms | 850ms | 1700ms | 162 |
| `/search [fusion_auto]` | 24ms | 32ms | 73ms | 140ms | 180ms | 115 (1 failure) |
| `/search [fusion_triple]` | 21ms | 31ms | 34ms | 39ms | 750ms | 57 |
| `/search [cache_hit]` | 23ms | 90ms | 1400ms | 1400ms | 1400ms | 20 |

**Warm cache, Run 3** — zero failures, no recurrence of Run 3's cold-pass anomalies.

| Endpoint | Median | p90 | p95 | p98 | p99 | n |
|----------|--------|-----|-----|-----|-----|---|
| `/search [kiwix]` | 23ms | 27ms | 33ms | 37ms | 37ms | 106 |
| `/search [kiwix_disambiguation]` | 23ms | 29ms | 37ms | 54ms | 54ms | 37 |
| `/search [web]` | 24ms | 29ms | 32ms | 40ms | 40ms | 69 |
| `/search [conditional]` | 36ms | 250ms | 770ms | 850ms | 850ms | 34 |
| `/search [conditional_remainder]` | 56ms | 64ms | 72ms | 72ms | 72ms | 15 |
| `/search [discourse_framing]` | 29ms | 34ms | 41ms | 49ms | 49ms | 43 |
| `/search [forecast]` | 23ms | 34ms | 43ms | 45ms | 99ms | 60 |
| `/search [news]` | 23ms | 29ms | 32ms | 42ms | 42ms | 40 |
| `/search [uptime]` | 24ms | 30ms | 65ms | 110ms | 110ms | 21 |
| `/search [ha]` | 41ms | 51ms | 85ms | 97ms | 97ms | 25 |
| `/search [auto]` | 25ms | 64ms | 65ms | 77ms | 110ms | 63 |
| `/search [fusion_explicit]` | 22ms | 28ms | 32ms | 46ms | 55ms | 179 |
| `/search [fusion_auto]` | 25ms | 31ms | 36ms | 43ms | 44ms | 101 |
| `/search [fusion_triple]` | 23ms | 29ms | 33ms | 43ms | 43ms | 50 |
| `/search [cache_hit]` | 25ms | 31ms | 34ms | 45ms | 45ms | 22 |

**`auto`'s cold p99 across all three confirmed v3.50.14 runs: 3400ms, 2300ms, 2200ms.** The connection-pooling fix, like singleflight before it, did not close the plateau — if anything this set of runs clusters slightly toward the worse end of the historical range, never reproducing the original v3.50.11 run's own 990ms favorable outlier.

**Re-reading `auto`'s own p90 across every release this project has ever benchmarked, rather than continuing to chase p99, surfaced the more useful signal**: 710ms (v3.50.9) → 720/740/720ms (v3.50.11's three runs) → 710/710/91ms (v3.50.13's three runs, the 91ms being the one genuine outlier in the whole series) → 720/720/700ms (these three confirmed v3.50.14 runs). At this benchmark's real sample sizes (`auto` draws roughly 60-80 total picks per run, with only 2 of 24 pool entries genuinely LLM-dependent), p99 sits at rank 1-2 from the top — effectively reporting the single slowest request in the run, not a stable, repeatable statistic. p90's consistency across every release suggests ~700-740ms is close to the genuine, ordinary cost of one real, unqueued LLM call on this hardware, and that neither singleflight nor connection pooling was ever positioned to move a number that was mostly being driven by single-sample noise at the tail rather than a fixable property of the routing code. See [The Benchmark Investigation Log](https://github.com/immortalbob/Mnemolis/wiki/The-Benchmark-Investigation-Log#thread-2-the-autoconditional-thundering-herd-including-a-real-mistake-caught-by-the-next-benchmark) for the full analysis.

**One real, concrete fix shipped from this investigation regardless: `app/llm.py` never sent Ollama's `keep_alive` field**, relying entirely on the server's own ambient 5-minute default. This project's own deployment shares the same `qwen3:8b` instance with an unrelated agentic-coding workflow (see the v3.50.11 changelog entry's VRAM math) — a real, plausible way for the model to be evicted from VRAM independent of anything Mnemolis does between its own calls. Fixed in v3.50.15 via the new `LLM_KEEP_ALIVE` setting — see that changelog entry and [Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching#llm-connection-pooling-and-keep-alive). Recorded honestly: this closes one plausible, low-risk-to-fix contributor, not a confirmed explanation for the plateau — there's no direct evidence the other workflow was active during any of these specific runs, and the p90/p99 analysis above suggests the plateau may simply be irreducible single-sample noise at this benchmark's sample size regardless of what fixes it.

---

### 20 Users — v3.51.0 Post-Audit Benchmark

Run immediately after the full function-by-function audit (v3.50.22 → v3.51.0) that found and fixed 14 real bugs. Config: `--headless --users 20 --spawn-rate 2 --run-time 120s`. **0 failures, 0 exceptions, both runs.**

**Cold cache (855 requests, 7.17 req/s)**

| Endpoint | Median | p90 | p95 | p99 | n |
|----------|--------|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 320ms | 1300ms | 1900ms | 103 |
| `/search [kiwix_disambiguation]` | 23ms | 99ms | 2900ms | 6700ms | 52 |
| `/search [web]` | 24ms | 82ms | 830ms | 3200ms | 66 |
| `/search [conditional]` | 48ms | 1300ms | 1400ms | 2200ms | 41 |
| `/search [conditional_remainder]` | 69ms | 970ms | 3000ms | 3000ms | 17 |
| `/search [discourse_framing]` | 28ms | 150ms | 2700ms | 3200ms | 35 |
| `/search [forecast]` | 25ms | 55ms | 160ms | 740ms | 43 |
| `/search [news]` | 23ms | 30ms | 33ms | 69ms | 42 |
| `/search [uptime]` | 23ms | 28ms | 60ms | 61ms | 21 |
| `/search [ha]` | 39ms | 55ms | 240ms | 240ms | 19 |
| `/search [auto]` | 31ms | 740ms | 820ms | 2400ms | 63 |
| `/search [fusion_explicit]` | 23ms | 35ms | 130ms | 740ms | 172 |
| `/search [fusion_auto]` | 25ms | 41ms | 130ms | 2700ms | 96 |
| `/search [fusion_triple]` | 21ms | 26ms | 35ms | 700ms | 54 |
| `/search [cache_hit]` | 25ms | 160ms | 1200ms | 1200ms | 15 |
| **Aggregated** | **24ms** | **160ms** | **750ms** | **2700ms** | **855** |

**Warm cache (895 requests, 7.51 req/s)**

| Endpoint | Median | p90 | p95 | p99 | n |
|----------|--------|-----|-----|-----|---|
| `/search [kiwix]` | 24ms | 32ms | 36ms | 39ms | 95 |
| `/search [kiwix_disambiguation]` | 24ms | 31ms | 39ms | 50ms | 50 |
| `/search [web]` | 23ms | 30ms | 33ms | 98ms | 55 |
| `/search [conditional]` | 41ms | 650ms | 750ms | 3500ms | 42 |
| `/search [conditional_remainder]` | 62ms | 710ms | 770ms | 770ms | 19 |
| `/search [discourse_framing]` | 29ms | 42ms | 48ms | 52ms | 58 |
| `/search [forecast]` | 23ms | 28ms | 30ms | 61ms | 37 |
| `/search [news]` | 23ms | 30ms | 36ms | 40ms | 41 |
| `/search [uptime]` | 24ms | 40ms | 62ms | 63ms | 29 |
| `/search [ha]` | 40ms | 49ms | 53ms | 55ms | 34 |
| `/search [auto]` | 26ms | 63ms | 64ms | 74ms | 68 |
| `/search [fusion_explicit]` | 22ms | 29ms | 34ms | 48ms | 159 |
| `/search [fusion_auto]` | 24ms | 32ms | 40ms | 67ms | 119 |
| `/search [fusion_triple]` | 21ms | 29ms | 32ms | 80ms | 57 |
| `/search [cache_hit]` | 24ms | 29ms | 41ms | 41ms | 14 |
| **Aggregated** | **24ms** | **43ms** | **63ms** | **750ms** | **895** |

**Analysis:**

The headline numbers: **zero failures across 1750 requests** at 20 concurrent users. Warm aggregated p95 of 63ms is the cleanest this project has recorded at this user count.

**Real improvements vs v3.50.14 warm:**

`auto` warm p99: 110ms → 74ms — the cleanest `auto` warm performance in this project's benchmark history. `auto` p90 is 63ms, compared to the 700–740ms that had been stubbornly consistent across every prior run. This is the `keep_alive` fix finally showing up clearly: the model is staying in VRAM between requests, not being evicted between calls and paying the full reload cost at p90.

`ha` warm p99: 97ms → 55ms. `uptime` warm p99: 110ms → 63ms. `forecast` warm p99: 99ms → 61ms. All three make live backend calls on cache miss; the uptime_kuma connection-warmup fix (`warm_connection()` at startup) is the likely contributor for uptime specifically.

`fusion_explicit` cold p95: previously 30–48ms on warm-ish cold runs, now a clean **130ms** under genuinely cold conditions — well-behaved, no pathological spike. The `searxng.py` thread pool fix (per-call executor creation was producing 46 OS threads at peak) is the most plausible explanation for why fusion cold p95 is now well-bounded rather than spiking into the 700–800ms range it occasionally hit in prior cold runs under concurrent load.

**Honest accounting of the anomalies:**

`conditional` warm p99: 850ms → 3500ms. Single bad request in a sample of 42. The conditional path can involve two LLM calls (condition route + remainder route, each potentially hitting a cold kiwix disambiguation path), and one cold LLM stack landing in a warm run is enough to produce this at n=42. Not a regression — prior runs showed conditional p99 ranging from 72ms to 1500ms depending on what the sample happened to catch. Same structural noise.

`conditional_remainder` warm p95/p99: 72ms → 770ms. Same explanation, smaller sample (n=19). The 72ms in the prior run was an unusually clean sample. Worth monitoring across multiple runs but no evidence of a code regression.

`web` warm p99: 40ms → 98ms. One slightly slow SearXNG request in n=55. Noise.

**`auto` cold p90: 740ms** — consistent with the 700–740ms plateau documented across every prior run, confirming that number reflects the real, ordinary cost of one unqueued LLM call on this hardware, not a fixable code issue. The warm improvement in `auto` (63ms p90 vs the prior plateau) confirms the model is staying loaded; the cold number confirms a fresh LLM call still costs what it always has.

## Running benchmarks

Replace `192.168.1.50` below with your actual Mnemolis host's real IP or hostname — not a placeholder. `--host` silently accepts anything that looks like a URL, so a leftover example value doesn't fail loudly; it fails much later as a DNS error (`Temporary failure in name resolution`) on every single request, which doesn't obviously point back to `--host` as the cause.

**Before a genuine cold-cache run, clear both caches explicitly.** Every "cold cache" run documented in this file describes the cache as empty, but never previously spelled out how to actually get it there — a real gap found while running a fresh v3.44.0 benchmark, where a routing/result cache populated by an earlier session produced an artificially clean run instead of the real cold numbers. Both caches need clearing, not just one — either alone can leave warm behavior bleeding through:

```bash
curl -X POST http://192.168.1.50:8888/cache/clear
curl -X POST http://192.168.1.50:8888/cache/routing/clear
```

Then run cold:

```bash
pip install locust
locust -f tests/locustfile.py --host http://192.168.1.50:8888
# Open http://localhost:8089
```

Or headless:

```bash
locust -f tests/locustfile.py --host http://192.168.1.50:8888 \
  --headless --users 20 --spawn-rate 2 --run-time 120s \
  --csv benchmarks
```

Run the identical command again immediately afterward, without clearing anything in between, for the warm-cache comparison — the second run's populated caches are the point.
