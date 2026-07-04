# Health & Observability

Three real gaps got found and closed during a deliberate operational-maturity review — not in response to a reported failure, but because the question "would I actually know if this silently broke?" turned out to have an honest "no" for several things. This page covers what `/health` and `/logs/stats` report today, and the reasoning behind each piece.

## `/health` — is everything actually reachable right now

```json
{
  "status": "ok",
  "kiwix_books_loaded": 27,
  "cache_entries": 12,
  "cache_max_size": 500,
  "routing_cache_entries": 340,
  "routing_cache_max_size": 1000,
  "snapshot_jobs": { "...": "see below" },
  "adversarial_testing": { "...": "see below" },
  "temporal_pattern_detection": { "...": "see below" },
  "sources": { "...": "see below" }
}
```

**`sources`** runs a real, live check against every configured backend — not a check that a config value is merely present. `_check_kiwix()` actually queries the [catalog](Kiwix-Catalog-and-Article-Fetching#catalog-discovery); `_check_llm()` actually pings the configured model endpoint; `_check_web()` actually hits SearXNG. This is genuinely useful precisely because it catches real, current failures: a SearXNG instance whose request timeout is set too aggressively, or a Docker network that two containers have silently fallen off of, both show up here as `"status": "error"` with the actual underlying error message attached — not after search results degrade and someone has to debug it by hand, but immediately, the next time anyone checks. All seven checks run concurrently, so the endpoint's worst case is governed by the single slowest check, not the sum of all seven.

**`cache_entries` / `cache_max_size` / `routing_cache_entries` / `routing_cache_max_size`** — see [Caching](Caching) for what these caches actually do. The pairing of current count against configured max exists specifically so growth toward either bound is visible at a glance, without needing to dig through logs or read code to even know a bound exists.

**`snapshot_jobs`** — see below.

**`adversarial_testing`** — same `ok` / `stale` / `never_ran` shape as `snapshot_jobs`, plus `total_combinations_tried` and `flagged_for_review` counts. See [Adversarial Self-Testing](Adversarial-Self-Testing) for the full feature; flagged combinations themselves are reviewed via `GET /adversarial/flagged`, not through `/health` directly.

**`temporal_pattern_detection`** — same overall shape, plus one status the other two jobs don't have: `insufficient_data`, reported when the job ran successfully but the real event volume in its most recent window was below the floor needed to test even one pair. This is the expected, honest state for the first weeks of this feature's life on real homelab data volumes, not a failure — see [Cross-Source Temporal Pattern Detection](Cross-Source-Temporal-Pattern-Detection#health-reporting) for the full set of states and what each one actually means.

## Background job health

Every [snapshot job](Snapshot-Engine-and-Changes) already caught its own exceptions internally and just logged a warning on failure. That's good defensive coding for "don't crash the scheduler" — but it also meant a job that started failing on *every single run* would produce zero externally visible signal beyond a log line nobody was necessarily watching. The scheduler object itself had no external visibility either; it's a local variable inside `main.py`'s startup code, never exposed to any endpoint.

`get_snapshot_job_health()` closes this using data that already existed — every snapshot is timestamped and stored, so there was no need for new instrumentation, just a query comparing "when did this job last actually succeed" against "how often is it supposed to run":

```json
{
  "uptime":   {"status": "ok", "last_snapshot": "...", "minutes_since_last_snapshot": 1.3, "expected_interval_minutes": 2},
  "forecast": {"status": "ok", "...": "..."},
  "news":     {"status": "stale", "minutes_since_last_snapshot": 240.0, "expected_interval_minutes": 60},
  "ha":       {"status": "never_ran", "expected_interval_minutes": 5}
}
```

Four possible states: **`ok`** (recent enough), **`stale`** (more than 3x its expected interval since the last success — a generous grace window meant to absorb normal jitter without false-alarming on a slightly delayed scheduler tick), **`never_ran`** (zero snapshots ever stored for this source), and **`unknown`** (a corrupted or unparseable timestamp — degrades gracefully rather than raising an exception that would take down the rest of `/health` over one bad row).

## `/logs/stats` — fallback visibility

[Routing](Routing#fallback--when-a-source-comes-back-empty) falls back from `kiwix` or `news` to `web` when a result looks empty. Whether that happened on any given query used to be completely invisible outside of reading raw logs — `source_used` correctly reported the *actual* source after a fallback, but nothing recorded *that a fallback occurred at all*.

```json
{
  "fallback_count": 4,
  "fallback_rate_pct": 2.1,
  "fallback_by_target": {
    "kiwix_or_news_fallback_to_web": 4
  }
}
```

This is tracked with a single boolean column (`fallback_occurred`) on the query log. As of v3.52.0 it's recorded *directly at the one code point where a fallback genuinely fires* — inside `router._resolve_single_source()`, via the per-request stats channel `route_query()` installs (a ContextVar holding a small mutable dict, the same pattern `suppress_cache_writes()` already uses; see the `_ROUTE_STATS` comment in `app/router.py`). Before v3.52.0 it was inferred after the fact in `main.py`, by comparing the pre-computed intended source against the resolved source — a mechanism whose own comment admitted it was structurally blind to fallbacks inside decomposed sub-queries, since those never surface in the overall resolved source (`"fusion"`). Those are now counted too. The original reason for the inference — deliberately not widening `route_with_source()`'s recursion-laden return signature — still stands, and the stats channel honors it: the signature is unchanged; the facts just travel out-of-band. One reporting nuance follows from the more complete counting: a decomposed query whose sub-query fell back logs `source_used="fusion"`, so it counts toward `fallback_count` but not toward the per-target breakdown below, which matches rows by `source_used`.

**Why the breakdown says `kiwix_or_news_fallback_to_web`, not separate counts for each** — `kiwix` and `news` both fall back to the same target, `web`. A single boolean genuinely cannot tell you which of the two originally intended sources triggered any specific fallback; querying per-original-source would run the identical SQL query under two different labels and double-count the same underlying rows. Reporting an honest, combined label is the correct tradeoff here — it's less granular than attributing fallbacks to a specific source, but it's *true*, which a confidently-wrong per-source breakdown would not have been.

`/logs/stats` also reports `latency_by_source` (average latency per source) and `top_queries` (your 10 most-repeated queries, each with its hit count and average latency).

When [Answer Synthesis](Answer-Synthesis) is in use, `/logs/stats` also reports a `synthesis` block — `{requested, served, not_in_sources, rejected_or_skipped}` — derived from a nullable `synthesized` status column on the query log (`NULL` = the request didn't ask for synthesis, `1` = a real grounded answer was served, `2` = the honest `NOT_IN_SOURCES` miss, `0` = requested but skipped or gate-rejected). It's the at-a-glance health of the feature over time: a `rejected_or_skipped` that climbs relative to `served` is the signal to look at *why* (the [explanation chain](Explanation-Chains)'s `synthesis_rejected`/`synthesis_skipped` events name the specific gate or reason). The column is added by a migration-safe guarded `ALTER TABLE`, so a log DB predating synthesis reports zeros rather than erroring.

`latency_by_source` is an overall average — it includes both cold queries (a real backend call) and warm ones (a cache hit), not warm-only. That's deliberate: a warm-only number would look almost identical across every source regardless of how slow that source actually is when it has real work to do, since cache hits are fast no matter the source.

Each entry in `top_queries` reports the source that answered it **most recently**, not whichever source happened to answer it most often historically. This matters if routing behavior for a query has changed over time — you'll see what it does *now*, not a stale answer from before a routing change.

## `/logs` — the raw query log

`GET /logs?limit=N` returns recent entries directly: timestamp, query, source requested, source used, cached flag, success, latency in milliseconds, and `fallback_occurred`. `limit` is clamped to a sane range (1-1000) — SQLite treats a negative `LIMIT` as "no limit at all," so this bound exists specifically to keep `?limit=-1` from returning the entire query log. Useful for spot-checking a specific recent query's behavior without waiting for it to show up in aggregate stats.

`POST /logs/clear` deletes every row from `query_log.db` — all of it, not a time-windowed subset — and returns `{"status": "cleared", "entries_removed": N}` with the real count. This also resets every aggregate built from the log (`/logs/stats`'s fallback rate, latency-by-source, top queries) back to empty, since they're all computed from this same table rather than tracked independently. `query_log.db` is one of the six files [`GET /backup`](Backup-and-Restore) covers — worth a backup first if the history has any value, since this has no undo.
