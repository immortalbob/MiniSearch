# Query Decomposition

Decomposition is what lets Mnemolis answer a single, casually-phrased compound question — *"what's the weather and are the lights on"* — by splitting it into independent sub-queries, routing each one separately, and merging the results back into one response. It runs once, on the full original query, before any per-sub-query [Routing](Routing) happens.

This page covers the splitting logic in detail, including five real bugs found and fixed in the part of it that protects proper-noun pairs from being split apart — the single most-revised piece of logic in this project's history. The full narrative, in the order the bugs were actually found, is in [The Proper-Noun-Pair Saga](The-Proper-Noun-Pair-Saga); this page covers the mechanism as it stands today.

## The splitting mechanism

Decomposition looks for five conjunction phrases: `" and "`, `" also "`, `" plus "`, `" as well as "`, `" in addition "`. It tries two different strategies and keeps whichever produces more genuinely separate, meaningful parts:

1. **Single-conjunction-type split** — try each conjunction type in isolation, see how many parts it produces
2. **Combined split** — find every occurrence of *any* conjunction type across the whole query at once, and split on all of them simultaneously

The combined approach exists because real compound sentences don't politely use only one kind of conjunction — *"check the wifi, and also can you check if the front door is locked and the lights are off"* has two separate `" and "` occurrences that need to be treated as two separate split points, not one.

```text
   "check the weather and also are the lights on"
                    │
                    ▼
        Try " and " alone: 2 parts
        Try " also " alone: 2 parts
        Try combined (both at once): 2 parts
                    │
                    ▼
        Pick whichever produced the MOST
        meaningful parts (ties keep the
        first-found result — conjunctions
        are tried longest-first, so " also "
        (6 chars) is checked before " and "
        (5 chars) and wins this particular tie)
                    │
                    ▼
   ["check the weather and", "are the lights on"]
```

That trailing `"and"` left stuck to the first part isn't a bug — `_filter_meaningful()` only cares whether each part has real content, not whether a part's edges are cosmetically clean, and the part still routes correctly (`detect_intent("check the weather and")` still resolves to `forecast`). It's a real, visible side effect of two adjacent conjunction words (`"and also"`) being collapsed into a single split point by the combined strategy below, with whichever single-type split happens to win the tie-break determining which side of the boundary the leftover word lands on. Worth knowing if you're ever reading raw decomposed fragments out of a log line and wondering why one has a stray conjunction at the end.

## What stops something from being split when it shouldn't be

Not every `" and "` is a conjunction joining two intents. Three separate guards exist, each catching a different real failure mode:

**`_NOSPLIT_PATTERNS`** — a short list of words (`compare`, `difference between`, `vs`, `versus`, `both`, `either`, `neither`, `between`) that, if present anywhere in the query, cancel splitting entirely. *"compare Python and Rust"* needs both halves to stay together to mean anything.

**Meaningful-content filtering** — after a split, each resulting part is checked for actual content. A part that's just stop words and filler (`"and"`, `"the"`, `"is"`) gets discarded rather than kept as a bogus third "intent." This check is stop-word-based, not an allowlist of expected topics, so it has no trouble with technical vocabulary that wouldn't appear on a fixed "intent words" list. The stop-word set itself isn't router-local — it's the exact same `_STOP_WORDS` defined in `kiwix.py`, reused here rather than duplicated, the same one-shared-copy discipline [the discourse-framing pattern list](The-Discourse-Framing-Investigation#a-real-deliberate-single-source-of-truth) already established for a structurally similar problem.

Stop-word stripping isn't actually the *first* check run here, even though it's the one that gives this layer its name. A real `INTENT_MAP` keyword phrase (`_ALL_INTENT_KEYWORDS`, the same flattened list every source's routing already uses) is checked first, and counts as meaningful on its own, regardless of what stop-word stripping would otherwise conclude — this matters because a couple of real keyword phrases are themselves made entirely of common stop words. See [The Meaningful-Content-Filter Bugs](The-Meaningful-Content-Filter-Bugs) for why this check exists and the two real bugs that shaped its current ordering.

A short-fragment length check (anything 3 characters or shorter gets discarded outright) runs *after* both the colloquial-phrase and keyword checks above, not before — the ordering matters specifically because `"rss"` is a real, valid keyword that's also only 3 characters long, and needs the keyword check to protect it before the length gate ever sees it.

**The proper-noun-pair guard** — the hardest of the three, and the one with real history. *"what's happening with Iran and Israel"* should never split into `["what's happening with Iran", "Israel"]`. The guard looks for a specific structural pattern: a capitalized word right before the conjunction, a capitalized word right after it, and a short, name-shaped phrase following — not a place-name list, since that would never generalize, but a structural detector that works for any properly-capitalized pair.

```text
   Decomposition Defense Layers
   ┌─────────────────────────────────────┐
   │  _NOSPLIT_PATTERNS                 │  "compare X and Y" → never split at all
   │  (whole-query veto)                │
   ├─────────────────────────────────────┤
   │  Proper-noun-pair guard            │  "Iran and Israel" → this ONE
   │  (per-occurrence, not global)      │  occurrence isn't a split point,
   │                                     │  but OTHER real conjunctions
   │                                     │  in the same query still split
   ├─────────────────────────────────────┤
   │  Meaningful-content filtering      │  stray fragments with no real
   │  (post-split cleanup)              │  content get discarded, not kept
   │                                     │  as a bogus extra intent
   └─────────────────────────────────────┘
```

That middle layer — "per-occurrence, not global" — is doing a lot of work, and it's exactly where three of the five real bugs documented in [The Proper-Noun-Pair Saga](The-Proper-Noun-Pair-Saga) lived. The guard checks each conjunction occurrence independently, protecting only the specific proper-noun pair it found while leaving every other real split point in the same query alone — so *"check the weather, and Iran and Israel news, and is the door locked"* still splits correctly into all three intents, with only the Iran/Israel pair staying intact.

## Colloquial phrasing

Decomposition (and the search-term cleanup that follows it) needs to handle the way people actually talk, not just formal phrasing. A set of stop-word-style filler terms — `deal`, `thing`, `things`, `stuff`, `keep`, `hearing`, `hear`, `heard`, `up`, `going` — get stripped the same way ordinary stop words do, so *"that mercury thing I keep hearing about"* reduces to just `mercury` instead of carrying along filler that pollutes the actual search.

A small set of recognized colloquial question patterns (`"what's the deal with"`, `"what's up with"`, `"what's this about"`, `"what's the story with"`) are explicitly recognized as legitimate definitional questions, not nonsense — handled with a substring check rather than `.startswith()`, so these phrases are caught whether they appear at the very start of the query or mid-clause.

## What decomposition doesn't do

It splits independent intents — it deliberately does *not* try to handle a leading conditional structure (*"if the back door is unlocked, let me know"*), since that's not a flat list of separate questions, it's a single statement with a condition and a consequence. That's [Conditional Query Detection](Conditional-Query-Detection)'s job, and it runs *before* decomposition at the top level — but a decomposed sub-query can still turn out to be conditional in its own right, which is why conditional detection gets re-applied to every sub-query decomposition produces, not just the original full query.

## Sub-queries resolve concurrently (v3.52.0)

Once split, sub-queries are genuinely independent by construction — the splitter only ever separates distinct intents, and no sub-query's resolution reads another's result. As of v3.52.0 they route *concurrently* rather than back to back: a 3-intent cold compound query previously paid three stacked LLM-routing + source-fetch costs sequentially, the largest remaining sequential-work latency cost in the routing path after the [SearXNG](The-SearXNG-Timeout-Lesson) and [conditional condition/remainder](Conditional-Query-Detection) parallelizations of the same shape.

Three deliberate design points, each carrying a lesson learned elsewhere in this project:

- **A fresh, per-call executor, bounded by `min(len(sub_queries), DECOMPOSE_MAX_PARALLEL)` — deliberately *not* a shared module-level pool like fusion's.** These workers recurse into `route_with_source()`, which can hit the decomposition path again (a sub-query's condition text that itself decomposes) — and a shared pool that submits into itself and blocks on the result is a real deadlock under saturation. A fresh pool per nesting level cannot deadlock, and decomposed multi-intent queries are rare enough that per-call construction isn't the unbounded-thread-pressure pattern fusion's hot path had. Same judgment already made for `_resolve_conditional()`'s own per-call executor.
- **`contextvars.copy_context()` once per task** — the identical constraint documented at every other executor site: `Context.run()` is non-reentrant across concurrent execution, and a bare `submit()` silently drops `suppress_cache_writes()` inside workers. The copied Context shares the same `_ROUTE_STATS` dict object by reference, so cache/fallback facts recorded inside workers still reach `route_query()`'s per-request stats.
- **Futures are collected in submission order, never `as_completed()`** — the merged response's section order must match the order the person actually asked things in, not network completion order. A regression test deliberately makes the first-asked sub-query the slow one and asserts the sections still come back in ask order.

---

## Development Notes

- **Meaningful-content filtering's original allowlist had zero coverage for technical vocabulary** — replaced by the current stop-word-based approach for exactly this reason. See [The Meaningful-Content-Filter Bugs](The-Meaningful-Content-Filter-Bugs#before-this-a-fixed-allowlist-that-had-zero-coverage-for-technical-vocabulary) for the real query that exposed the gap.
- **Two real keyword phrases, `"is it up"` and `"are they up"`, are made entirely of common stop words** and were silently vanishing from decomposed output before the `INTENT_MAP` keyword check was moved ahead of stop-word stripping. See [The Meaningful-Content-Filter Bugs](The-Meaningful-Content-Filter-Bugs) for the full story, including a second, related ordering bug found while fixing the first.
- **The proper-noun-pair guard used to be a global veto** — finding any proper-noun pair anywhere in the query canceled splitting entirely, dropping every other real intent in the same query along with it. Three of the five bugs in [The Proper-Noun-Pair Saga](The-Proper-Noun-Pair-Saga) live in this guard and its fix.
- **Colloquial-phrase detection used to miss phrases that appeared mid-clause** rather than at the very start of the query, before the check was changed from `.startswith()` to a substring match.
