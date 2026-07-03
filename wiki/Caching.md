# Caching

Mnemolis has two genuinely different caches, and conflating them is a common source of confusion. The **result cache** stores actual search results so a repeated query skips the real backend call entirely. The **routing cache** stores the *decision* about which source(s) a query should go to, separately from the result itself — so a repeated query still skips the LLM call even if the underlying result has since expired or changed.

## Result cache

Keyed on `source:query` (lowercased, stripped). Every source has its own TTL, because "how stale is acceptable" is genuinely different per source — each is independently configurable (`CACHE_TTL_KIWIX_SECONDS`, `CACHE_TTL_FORECAST_SECONDS`, and so on, one per source; see [Configuration Reference](Configuration-Reference#caching)), but the defaults reflect deliberate, real reasoning about each source's own freshness needs:

| Source | TTL | Why |
|--------|-----|-----|
| `kiwix` | 24 hours | Offline encyclopedic content essentially never changes within a day |
| `forecast` | 30 minutes | A 3-day outlook doesn't need to be fresher than this |
| `news` | 15 minutes | Your RSS feeds update on their own schedule; this is just an upper bound |
| `web` | 1 hour | Live search results are reasonably stable short-term |
| `uptime` | 1 minute | Service status is the kind of thing you want close to real-time |
| `ha` | 30 seconds | Lights and locks change state constantly — this is intentionally the shortest TTL of any source |
| `changes` | 2 minutes | "What changed" needs to stay close to real-time too |
| `fusion` | 30 minutes | A reasonable middle ground for merged multi-source results |

Fusion's cache key is built from the sorted, comma-joined list of sources actually involved (`fusion[sorted,sources]:query`), not the source name "fusion" alone — two different fusion combinations for the same query text get independent cache entries. This applies identically whether the fusion happens at the top level or inside one clause of a [decomposed](Query-Decomposition) compound query — a repeated compound query whose individual clause resolves to multiple sources internally is cached the same way a top-level fusion query is, not re-run on every request.

On a cache hit, none of the underlying sources get touched at all — no network call, no LLM call, nothing. This is why [Benchmarks](Benchmarks) consistently shows warm-cache latency in the tens of milliseconds even for queries whose cold-cache cost was several seconds.

### Why `uptime`'s connection is persistent even though its cache TTL stays short

`uptime`'s 1-minute TTL is intentionally the closest-to-real-time of any source besides `ha` — a stale "all services up" is actively misleading in a way a stale weather forecast isn't, so this TTL is a deliberate design choice, not an oversight to fix. But a short result-cache TTL and an expensive connection are two genuinely separate costs, and conflating them led to the wrong fix being considered for a real time: raising the TTL would have hidden a real, unrelated cost (a fresh Socket.IO connect+login cycle on every single cache miss, including every 2-minute background snapshot tick) rather than actually fixing it.

The real fix was at the connection level, not the cache level: the Socket.IO connection to Uptime Kuma is now persistent, established once and reused across every call — see [Sources](Sources#uptime--service-monitoring) for the mechanism. `CACHE_TTL_UPTIME_SECONDS` itself is completely unchanged by this; a cache miss still happens exactly as often as the TTL dictates. A second, separate fix (v3.50.8) addressed the remaining tail that the persistent connection alone didn't close — see [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-1-uptimes-warm-cache-tail-five-releases-to-a-real-root-cause) for the full history and [Sources](Sources#uptime--service-monitoring) for what that second fix actually was.

### LLM connection pooling and keep-alive

Every LLM call (`app/llm.py`'s `complete()` — source routing, Kiwix book selection, fusion source selection, disambiguation candidates) goes through a single, persistent `requests.Session` (`LLM_CONNECTION_POOL_SIZE`, default 20 — see [Configuration Reference](Configuration-Reference#llm-backend)) rather than opening a fresh connection per call. This matters most for a deployment where the LLM backend runs on separate hardware from Mnemolis itself, since a real network round-trip per connection stacks on top of inference time, which this project's own reference deployment does. Under genuine concurrent load exceeding the pool size, `requests` itself transparently opens additional connections rather than failing — the pool size controls how many stay around for reuse, not a hard concurrency ceiling.

`LLM_KEEP_ALIVE` (default `5m`, matching Ollama's own default — see [Configuration Reference](Configuration-Reference#llm-backend)) is sent on every Ollama-native call, giving Mnemolis its own explicit say in how long the model stays loaded rather than depending entirely on the server's ambient default. Deliberately not sent on the OpenAI-compatible path — Ollama's own OpenAI-compatible endpoint silently ignores it, and other OpenAI-compatible backends have no equivalent concept to send it to.

See [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-2-the-autoconditional-thundering-herd-including-a-real-mistake-caught-by-the-next-benchmark) for the real investigation that found both of these, and [The SearXNG Timeout Lesson](The-SearXNG-Timeout-Lesson) for what turned out to be the actual remaining cause of `auto`'s benchmark plateau after both fixes shipped.

**Disk persistence is batched** — writes accumulate in memory and only get flushed to `cache.json` every 5 writes (`_CACHE_SAVE_INTERVAL`), not on every single cache write. This matters for anyone reading the code expecting every cache update to be immediately durable; a handful of very recent entries could theoretically be lost if the process died between batched saves, which is an acceptable tradeoff for a cache (the source data it's caching is still right there, ready to be re-fetched) but worth knowing. Two v3.52.0 refinements narrow that window further: a clean shutdown (`docker stop`, a deploy restart) now flushes both caches unconditionally via `router.flush_caches()` in the lifespan teardown, so batched-but-unflushed entries only ever die with a hard crash — and each save now fsyncs the temp file before the atomic rename, so an unlucky power cut can no longer leave the path pointing at a zero-length file on filesystems that commit the rename before the data blocks.

**Bounded at `CACHE_MAX_SIZE`** (default 500). When a *new* key would push the cache over this limit, the single oldest entry (by write timestamp) gets evicted first. Updating an already-cached key never counts as "new" for this purpose, so re-caching something you already have doesn't trigger an eviction.

## Routing cache

Keyed the same way (lowercased, stripped query — though the routing cache key doesn't include a source prefix, since the whole point is recording *which* source was chosen). A single flat TTL applies to every routing decision regardless of source (`ROUTING_CACHE_TTL_SECONDS`, default 1 hour) — a query's correct source assignment doesn't really depend on the kind of freshness concerns the result cache has to account for per-source.

This is a genuinely larger key space than it might first appear. Every unique conditional query, every discourse-framing phrase, every Kiwix disambiguation candidate set gets its own routing cache entry on top of plain single-source routing decisions — which is exactly why this cache has its own, larger bound (`ROUTING_CACHE_MAX_SIZE`, default 1000) than the result cache's `CACHE_MAX_SIZE`, using the same bounded-eviction pattern.

**Disk persistence is batched, matching the result cache** — as of v3.52.0, `_set_routing()` accumulates writes in memory and flushes the full file to `routing_cache.json` every 5 writes (`_ROUTING_CACHE_SAVE_INTERVAL`), the identical batching the result cache has always had. Before v3.52.0 the routing cache wrote its entire file on *every single* `_set_routing()` call — an asymmetry this page previously described as intentional, but a function-by-function audit found no stated reason for it that survived contact with the actual write pattern: routing writes come from four separate writers (source picks, book selection, disambiguation candidates, alternate phrasings) and cluster precisely during cold bursts, which is exactly when a full-file JSON dump per decision on the request-serving threads hurts most. Both caches also now flush unconditionally on clean shutdown (`router.flush_caches()`, called from `main.py`'s lifespan), so only a hard crash can lose the up-to-4 not-yet-flushed entries — each of which is just a re-derivable LLM decision. Testing the routing cache's eviction logic still mocks out the disk-write step for the same reason as ever: filling it to 1000 entries in a test would mean real disk writes for logic that has nothing to do with disk I/O.

A defensive cap also applies when loading the routing cache back from disk at startup — a file saved before the size limit existed could theoretically still be over it, so loading trims to the most recently-written entries if so, rather than silently allowing an over-limit cache to persist indefinitely across restarts.

**Only genuine successes get cached, never a fallback default.** If routing or Kiwix disambiguation picks an obviously-wrong generic fallback for a query, that fallback result is never written to the cache — so a transient hiccup doesn't lock the same query into the same wrong answer for the rest of the cache's TTL, the way a genuine success result otherwise would. The Kiwix case needs one extra distinction to get this right: the same fallback *result* can happen for two different *reasons* — the LLM call failing outright (worth retrying) versus the LLM genuinely answering with something that just didn't pass a sanity check (not worth retrying, since the same prompt would likely fail the same way again) — and only the first case skips the cache.

**Concurrent callers for the same uncached key don't duplicate the LLM cost.** A miss on `_get_routing()` is followed by a per-key lock acquisition (`router._singleflight()`) before the real LLM call happens — a second caller arriving while the first is still resolving the identical key blocks on that lock rather than also calling the LLM, then re-checks the cache once it acquires the lock and reuses whatever the first caller just wrote. Different keys never block each other; only two callers racing for the *same* key actually queue. This applies at all four routing-cache call sites that pay an LLM cost on a miss (`_llm_detect()`, `_llm_pick_fusion_sources()`, and Kiwix's own `_pick_books_with_llm()`/`_get_disambiguation_candidates()`) — see [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-2-the-autoconditional-thundering-herd-including-a-real-mistake-caught-by-the-next-benchmark) for why this was needed. The result cache has an identical, structurally-equivalent gap that's deliberately left unfixed (`TestResultCacheThunderingHerd` in `tests/test_router.py` confirms it still exists) — singleflight was scoped to the routing cache only.

## Semantic routing cache (v3.53.0)

A third, optional layer that sits between a routing-cache miss and the routing LLM: embedding-based reuse of routing decisions across *rephrasings* — "will it rain later" borrowing "will it rain this evening"'s already-made decision instead of paying its own cold LLM call. Disabled unless `EMBEDDING_MODEL` is configured; stores only embeddings (decisions always come from the live routing cache at match time, so there is exactly one source of truth and one TTL); in-memory only, re-warmed from the routing cache's persisted queries by a background thread at startup (v3.54.0) and rebuilt through use otherwise. It gets its own full page — see [Semantic Routing Cache](Semantic-Routing-Cache) for the design constraints, the conservative-threshold reasoning, and the setup.

## What's visible without digging through code

[Health & Observability](Health-and-Observability) covers this in full, but briefly: `/health` reports both caches' current entry counts alongside their configured max sizes, so growth toward either bound is visible at a glance. `GET /cache` and `GET /cache/routing` show every individual entry with age and remaining TTL; `GET /cache/semantic` reports the semantic layer's state; the corresponding `/clear` endpoints wipe each cache (from both memory and disk, for the two that persist).

## Why two caches instead of one

They answer genuinely different questions. The result cache says "I already know the answer to this." The routing cache says "I already know *where to look* for the answer to this" — useful even when the actual answer has expired or could have changed, since re-deciding the source via the LLM is itself real, avoidable cost. A query whose result just expired can still skip straight back to the right source without paying for a fresh routing decision. The semantic layer extends the routing cache's question one step further: "I already know where to look for something that *means the same thing* as this."

---

## Real bug history behind these mechanisms

Several of the design choices above exist because of real, found-and-fixed bugs, not just up-front planning:

- **A bad LLM response used to get permanently "stuck" in the routing cache** — found in three separate functions, one at a time, with a real distinction discovered on the third occurrence between a genuine call failure and a genuinely-answered-but-filtered-out response. See [The Cached-Failure Bug, Found Three Times](The-Cached-Failure-Bug-Found-Three-Times).
- **Synthetic adversarial-testing traffic was silently leaking into both real caches**, and a separate file-write race lived underneath both caches' disk persistence. See [The Caching Concurrency Investigation](The-Caching-Concurrency-Investigation) for both.
- **`uptime`'s warm-cache tail took five releases to fully root-cause** — a connection-lifecycle issue, then a library-internal sleep, neither one actually a caching problem despite looking like one at first. See [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-1-uptimes-warm-cache-tail-five-releases-to-a-real-root-cause).
- **`auto`'s own cold-path benchmark plateau survived three independent, correctly-diagnosed fixes** (singleflight, LLM connection pooling, `LLM_KEEP_ALIVE`) before the real cause turned out to be a SearXNG per-engine timeout override having nothing to do with caching or routing at all. See [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-2-the-autoconditional-thundering-herd-including-a-real-mistake-caught-by-the-next-benchmark) and [The SearXNG Timeout Lesson](The-SearXNG-Timeout-Lesson).
