# Configuration Reference

Every setting is an environment variable, set in `docker-compose.yml`. This page groups them by what they actually control, rather than the README's single flat table — useful for understanding *why* a default is what it is, not just what it is.

## Backend connections

| Variable | Default | Notes |
|----------|---------|-------|
| `KIWIX_URL` | `http://kiwix:8080` | |
| `FRESHRSS_URL` | `http://freshrss` | |
| `FRESHRSS_USER` | _(blank)_ | |
| `FRESHRSS_API_PASSWORD` | _(blank)_ | A separate password from your normal FreshRSS login — generated specifically for API access |
| `FRESHRSS_MAX_ARTICLES` | `10` | |
| `SEARXNG_URL` | `http://searxng:8080` | |
| `SEARXNG_REQUEST_TIMEOUT_SECONDS` | `25` | Mnemolis's own client-side wait time for a SearXNG response — separate from SearXNG's own server-side `request_timeout`/`max_request_timeout` settings (see [The SearXNG Timeout Lesson](The-SearXNG-Timeout-Lesson)). Set this to match or exceed whatever SearXNG is itself configured to wait, or a fix on the SearXNG side won't fully take effect — Mnemolis would still cut the connection first. Raised from an original default of `10` (which this exact mismatch was once found and documented for, but never actually closed — see the CHANGELOG) to `25`, comfortably above the `20.0` `max_request_timeout` this project's own shipped `searxng/settings.yml` now sets. If you raise SearXNG's own value further, raise this to match again |
| `UPTIME_KUMA_URL` | _(blank)_ | Leaving this blank disables the `uptime` source entirely, rather than erroring — `/health` will simply not report a status for it |
| `UPTIME_KUMA_USERNAME` / `UPTIME_KUMA_PASSWORD` | _(blank)_ | |
| `UPTIME_KUMA_TIMEOUT_SECONDS` | `10` | How long the Uptime Kuma client waits before giving up. Lower for faster fallback to other sources on a genuinely unreachable instance |
| `HA_URL` | _(blank)_ | Same graceful-disable behavior as `UPTIME_KUMA_URL` |
| `HA_TOKEN` | _(blank)_ | See [Home Assistant Integration](Home-Assistant-Integration) for how to generate this |

## LLM backend

| Variable | Default | Notes |
|----------|---------|-------|
| `LLM_URL` | _(blank)_ | Leaving this blank disables every LLM-assisted feature — [Routing](Routing) falls back to keyword-only matching, [Kiwix Disambiguation](Kiwix-Disambiguation) and [Query Expansion](Query-Expansion) never trigger, Kiwix book selection falls back to a fixed "search Wikipedia first" rule. Mnemolis still works, with meaningfully less of its actual intelligence available |
| `LLM_MODEL` | `qwen3:8b` | |
| `LLM_API_TYPE` | `ollama` | The other supported value is `openai`, for any OpenAI-compatible endpoint |
| `LLM_CONNECTION_POOL_SIZE` | `20` | Pooled HTTP connections kept open and reused for calls to the LLM backend — see [Caching](Caching#llm-connection-pooling-and-keep-alive) for why this exists and how it was found. Sized for up to 20 genuinely simultaneous LLM calls; raise it if you regularly run with substantially more concurrent traffic than that |
| `LLM_KEEP_ALIVE` | `5m` | How long Ollama keeps the model loaded in VRAM after Mnemolis's last call — Ollama-native backend only, see [Caching](Caching#llm-connection-pooling-and-keep-alive). Accepts any format Ollama's own API documents (a duration string, plain seconds, `-1` for never-unload, `0` for unload-immediately), passed straight through with no reinterpretation. Deliberately left at Ollama's own default rather than `-1` — see the setting's own comment in `app/config.py` for why |

## Weather (`forecast`)

| Variable | Default | Notes |
|----------|---------|-------|
| `FORECAST_LATITUDE` / `FORECAST_LONGITUDE` | _(blank)_ | Required for `forecast` to work at all |
| `FORECAST_LOCATION_NAME` | _(blank)_ | Used to prefix forecast responses, so a [fused](Fusion) response can't be misread as weather somewhere else |
| `FORECAST_TIMEZONE` | `UTC` | |
| `FORECAST_PRECIP_THRESHOLD_PCT` | `20` | Precipitation probability above which the forecast text actually mentions rain chance |
| `FORECAST_WIND_THRESHOLD_MPH` | `15` | Wind speed above which the forecast text mentions wind |
| `FORECAST_TEMP_CHANGE_THRESHOLD` | `5.0` | How large a temperature shift between [snapshots](Snapshot-Engine-and-Changes) needs to be before `changes` reports it as meaningful — a half-degree difference between two snapshots isn't worth surfacing |

## Timezone conversion

| Variable | Default | Notes |
|----------|---------|-------|
| `LOCAL_TIMEZONE` | inherits `TZ`, or `UTC` if `TZ` is unset | Converts stored UTC timestamps (every database timestamp in Mnemolis is UTC internally) into real local time, for any feature bucketing activity by local hour-of-day or day-of-week. See [Timezone Conversion](Timezone-Conversion). Most deployments never need to set this directly — setting `TZ` (see [README's Timezone configuration](https://github.com/immortalbob/Mnemolis#timezone-configuration)) is enough; `LOCAL_TIMEZONE` exists only for the rare case where this conversion should use a *different* zone than `TZ` |

## Time-window phrase resolution

| Variable | Default | Notes |
|----------|---------|-------|
| `MORNING_START_HOUR` | `6` | What hour (local time, 0-23) "this morning" looks back to in [`changes`](Snapshot-Engine-and-Changes#time-window-phrases) queries. A value outside 0-23 (e.g. `24` for midnight, a natural mistake) is wrapped via modulo rather than rejected — `24` is treated as `0` |
| `WORK_START_HOUR` | `9` | Same, for "while at work" / "since work" |

## Fusion

| Variable | Default | Notes |
|----------|---------|-------|
| `FUSION_MAX_SOURCES` | `4` | Hard cap on how many sources one [fusion](Fusion) query can touch. Setting this to `0` correctly returns "no valid sources specified" rather than crashing |
| `FUSION_MAX_CHARS_PER_SOURCE` | `1500` | Per-source truncation before merging |
| `FUSION_TIMEOUT_SECONDS` | `15` | How long any single source gets before fusion moves on without it — as of v3.50.18 this now genuinely bounds how long the *caller* waits too, not just how long the internal gather loop waits before giving up on a straggler |
| `FUSION_THREAD_POOL_SIZE` | `12` | Worker threads in fusion's shared, long-lived thread pool — reused across every concurrent fusion call rather than a fresh pool spun up and torn down per call. See [Fusion](Fusion#concurrency-and-thread-pool-sizing) |
| `DECOMPOSE_MAX_PARALLEL` | `4` | How many [decomposed](Query-Decomposition#sub-queries-resolve-concurrently-v3520) sub-queries one compound query resolves concurrently (v3.52.0). Uses a fresh, per-call executor bounded by `min(len(sub_queries), this)` — deliberately not a shared pool, since these workers recurse into the router itself and a shared pool submitting into itself is a real deadlock under saturation. Realistic decomposed queries are 2–4 intents, so the default covers them fully while capping what a pathological many-conjunction query can fan out to |
| `EMBEDDING_MODEL` | *(empty)* | Enables the [semantic routing cache](Semantic-Routing-Cache) (v3.53.0) when set to an embedding model the backend serves (e.g. `nomic-embed-text` after `ollama pull nomic-embed-text`). Empty — the default — disables the whole feature with zero behavior change and zero network I/O, since routing/completion and embeddings are independent capabilities and a fresh deployment shouldn't pay a failed HTTP call per cold decision for a model that was never pulled |
| `EMBEDDING_URL` | *(empty)* | Empty = use `LLM_URL` (the normal case: the same Ollama instance serves both). Set only if embeddings live on a different backend |
| `EMBEDDING_TIMEOUT_SECONDS` | `5` | Deliberately short — an embedding is a tiny fraction of a completion's cost, and the fallback on timeout is just "do what v3.52.0 did" (call the routing LLM), so waiting long here only delays the path that was about to run anyway |
| `SEMANTIC_ROUTING_THRESHOLD` | `0.92` | Cosine-similarity floor for reusing another query's routing decision. Deliberately conservative: a wrong reuse silently misroutes, which is strictly worse than the ~200–700ms LLM call it saved. Tune DOWN cautiously and only after auditing near-miss pairs on your own traffic via [explanation chains](Explanation-Chains); tune UP if you ever catch a wrong reuse. See [Semantic Routing Cache](Semantic-Routing-Cache) |
| `SEMANTIC_CACHE_MAX_SIZE` | `500` | Max stored query embeddings before oldest-eviction — the same bounded-eviction pattern both other caches use. Entries whose underlying routing decision has expired are skipped at match time and pruned, so the store can never serve a decision the routing cache no longer stands behind |
| `SEMANTIC_WARMUP_ENABLED` | `true` | Re-embed the routing cache's persisted queries in a background thread at startup (v3.54.0), so rephrasings match immediately after a restart instead of only after each query is re-decided once. Never blocks startup; a failed batch falls back to normal lazy population. Only relevant when `EMBEDDING_MODEL` is set — turn off if your embedding backend boots slowly after a host restart and the warmup's failure warnings would be noise |

## Answer synthesis

Opt-in, per-request LLM composition of a short, grounded answer *from the retrieved material only*, returned in a separate `answer` field alongside the raw `result` — never instead of it. See [Answer Synthesis](Answer-Synthesis) (v3.55.0). Additive by contract: with the feature off or a request that doesn't ask for it, the response is byte-identical to before and the `answer`/`answer_sources`/`synthesized` fields are `null`/`[]`/`false`.

| Variable | Default | Notes |
|----------|---------|-------|
| `SYNTHESIS_ENABLED` | `false` | Master switch, standard rollout contract (default false for one release). While false, a request's `synthesize=true` becomes a no-op that emits a `synthesis_skipped: disabled` [explanation](Explanation-Chains) event rather than silently running — no client that doesn't ask pays any latency either way |
| `SYNTHESIS_TIMEOUT_SECONDS` | `20` | The synthesis generation's own timeout, deliberately independent of the routing call's hardcoded 10s — this is a real multi-sentence generation, not a one-token routing pick. On timeout, `answer` is `null` and the raw `result` is untouched |
| `SYNTHESIS_MODEL` | *(empty)* | Blank = `LLM_MODEL`. Exists so routing can run a small, fast model while synthesis runs a larger instruct model on the same backend — the two calls have genuinely different quality/latency tradeoffs |
| `SYNTHESIS_INPUT_BUDGET_CHARS` | `6000` | Total characters of retrieved material offered to the prompt, apportioned proportionally across attributed sections. qwen3:8b at 32K context has ample room; the budget keeps latency predictable and is configurable down for smaller-context models |
| `SYNTHESIS_MIN_INPUT_CHARS` | `200` | Results shorter than this skip synthesis entirely — a one-line HA state answer like "Front Door: locked" is already the ideal voice answer, and rewriting it can only add risk. This rule alone exempts most `ha`/`uptime` traffic |
| `SYNTHESIS_MAX_CHARS` | `2000` | `answer_style=detailed` ceiling. Over-budget replies are truncated at the last sentence boundary under the cap, never mid-sentence |
| `SYNTHESIS_VOICE_MAX_CHARS` | `400` | `answer_style=voice` ceiling (≈≤2 sentences, for TTS). `brief` (the default style) is fixed at 800 — an internal middle point, not a deployment preference |

## Caching

| Variable | Default | Notes |
|----------|---------|-------|
| `CACHE_MAX_SIZE` | `500` | Max [result cache](Caching#result-cache) entries before oldest-eviction |
| `ROUTING_CACHE_MAX_SIZE` | `1000` | Max [routing cache](Caching#routing-cache) entries before oldest-eviction — larger than the result cache's default, since the routing cache's real key space (every unique conditional query, discourse-framing phrase, and disambiguation candidate set) is genuinely bigger |
| `ROUTING_CACHE_TTL_SECONDS` | `3600` | How long a routing decision (source, Kiwix book, disambiguation candidates) stays cached before the LLM gets asked again |
| `CACHE_TTL_KIWIX_SECONDS` | `86400` | Result cache TTL for `kiwix` (24 hours — offline encyclopedic content barely changes within a day) |
| `CACHE_TTL_FORECAST_SECONDS` | `1800` | Result cache TTL for `forecast` |
| `CACHE_TTL_NEWS_SECONDS` | `900` | Result cache TTL for `news` |
| `CACHE_TTL_WEB_SECONDS` | `3600` | Result cache TTL for `web` |
| `CACHE_TTL_UPTIME_SECONDS` | `60` | Result cache TTL for `uptime` |
| `CACHE_TTL_HA_SECONDS` | `30` | Result cache TTL for `ha` (the shortest of any source — lights and locks change state constantly) |
| `CACHE_TTL_CHANGES_SECONDS` | `120` | Result cache TTL for `changes` |
| `CACHE_TTL_FUSION_SECONDS` | `1800` | Result cache TTL for `fusion` |

## Kiwix tuning

| Variable | Default | Notes |
|----------|---------|-------|
| `KIWIX_SEARCH_LIMIT` | `15` | Results requested per [book search](Kiwix-Catalog-and-Article-Fetching#searching-a-book) — higher values give [scoring](Kiwix-Scoring) more candidates to find the right answer among when common terms collide with brand-name results |
| `KIWIX_MAX_BOOKS` | `2` | Max books the LLM can select for one query — raise this to allow broader [multi-book fusion](Multi-Book-Fusion), at the cost of more searches per query |
| `KIWIX_ARTICLE_MAX_CHARS` | `3000` | How many characters of a [fetched article's](Kiwix-Catalog-and-Article-Fetching#fetching-the-actual-article) body to keep before scoring/fusion ever sees it — distinct from `FUSION_MAX_CHARS_PER_SOURCE`, which truncates the already-combined multi-source response, not an individual Kiwix article on its own |
| `KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT` | `0.5` | The actual, central decision threshold for [multi-book fusion](Multi-Book-Fusion): a second book's best result must score at least this fraction of the leading book's top score to be included. Lower for more aggressive fusion, raise for more conservative |

## Web & news scoring

| Variable | Default | Notes |
|----------|---------|-------|
| `WEB_NEWS_SCORE_THRESHOLD` | `0` | Results from [confidence-aware fusion](Confidence-Aware-Fusion) scoring at or below this are dropped |
| `WEB_NEWS_TOP_N` | `10` | Max results kept after scoring |
| `WEB_NEWS_RAW_RESULT_BUDGET` | `25` | How many raw, unscored results to pull from each web search before scoring filters them down — the scoring pipeline's *input* budget, distinct from `WEB_NEWS_TOP_N`'s *output* cap |
| `QUERY_EXPANSION_MIN_WORDS` | `3` | Minimum query length (in words) for web search [query expansion](Query-Expansion) to trigger |

## Snapshot diff thresholds

| Variable | Default | Notes |
|----------|---------|-------|
| `BATTERY_LOW_THRESHOLD_PCT` | `20.0` | Battery level below which a [snapshot diff](Snapshot-Engine-and-Changes) reports "low" |
| `SNAPSHOT_STALE_GRACE_MULTIPLIER` | `3` | How many multiples of a job's own expected interval can pass before [`/health`](Health-and-Observability#background-job-health) flags it as "stale" rather than "ok" — lower for tighter alerting on flakier hardware, raise if normal scheduler jitter on your own hardware is wider than the default assumes |

## Adversarial self-testing

| Variable | Default | Notes |
|----------|---------|-------|
| `ADVERSARIAL_TEST_ENABLED` | `true` | Master on/off switch for [adversarial self-testing](Adversarial-Self-Testing). `false` skips DB init, never registers the scheduler job, and makes `POST /adversarial/trigger` a safe no-op |
| `ADVERSARIAL_TEST_INTERVAL_MINUTES` | `60` | How often the scheduler tick fires |
| `ADVERSARIAL_TEST_BATCH_SIZE` | `8` | Queries generated per tick — cheap to raise, since generation is pure combinatorics with no LLM calls in the hot path |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_MULTIPLIER` | `1.5` | How many multiples of a recipe's own historical p95 latency counts as a real outlier |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_FLOOR_MS` | `1000` | A floor below which latency is never flagged regardless of the multiplier |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_MIN_SAMPLES` | `10` | How many historical samples a recipe needs before the latency-outlier check engages at all |

## Cross-source temporal pattern detection

| Variable | Default | Notes |
|----------|---------|-------|
| `TEMPORAL_PATTERN_DETECTION_ENABLED` | `true` | Master on/off switch for [temporal pattern detection](Cross-Source-Temporal-Pattern-Detection). `false` skips DB init, never registers the scheduler job, and makes `POST /temporal-patterns/trigger` a safe no-op — checked both at scheduler-registration time and inside the cycle function itself |
| `TEMPORAL_PATTERN_MINING_INTERVAL_HOURS` | `24` | How often the mining cycle runs. Deliberately far longer than every other scheduler job in this codebase — mining over a short window is statistically meaningless given how infrequently real structured events actually occur |
| `TEMPORAL_PATTERN_LAG_WINDOW_MINUTES` | `30` | The maximum lag within which event B must follow event A to count as one real occurrence of that pair |
| `TEMPORAL_PATTERN_MIN_OCCURRENCES` | `5` | A hard floor below which a pair is never even significance-tested, regardless of what the math would say. Raise this for a stricter bar on real homelab data volumes; lowering it below the default trades real statistical confidence for catching potential patterns sooner |
| `TEMPORAL_PATTERN_SIGNIFICANCE_LEVEL` | `0.05` | The per-comparison significance level, before Bonferroni correction divides it by the number of pairs actually tested in a given pass |
| `TEMPORAL_PATTERN_VALIDATION_WINDOW_HOURS` | `24` | How much later, non-overlapping data a candidate needs to be re-checked against before it can be promoted to `confirmed` |
| `TEMPORAL_PATTERN_STALE_GRACE_MULTIPLIER` | `3` | Same role as `SNAPSHOT_STALE_GRACE_MULTIPLIER` — how many missed mining intervals before [`/health`](Cross-Source-Temporal-Pattern-Detection#health-reporting) flags this job stale |

## Security

| Variable | Default | Notes |
|----------|---------|-------|
| `API_KEYS` | _(blank — auth disabled)_ | Comma-separated list of valid keys. Protects `POST /search` and `GET /changes` specifically — every other endpoint (`/health`, `/areas`, `/backup`, `/cache`, etc.) stays unauthenticated regardless of this setting, so monitoring tools and discovery requests aren't blocked. Clients send the key via the `X-API-Key` header. Leaving this blank matches the trust model of a homelab sitting behind your own firewall |

## Operational

| Variable | Default | Notes |
|----------|---------|-------|
| `LOG_LEVEL` | `INFO` | `INFO` is what actually shows the interesting decisions — decomposition splits, disambiguation candidates, article selection |

## Where to go from setting a value to understanding what it actually does

Most of the notes above link to the wiki page that covers the real mechanism a setting controls — [Routing](Routing), [Caching](Caching), [Fusion](Fusion), [Kiwix Scoring](Kiwix-Scoring), and so on. The numeric default itself is rarely the interesting part; the page it links to explains why that number, specifically, was chosen.

---

## Development Notes

- **`UPTIME_KUMA_TIMEOUT_SECONDS` didn't exist until recently** — the client previously had a bare, hardcoded `timeout=30` with no way to tune it, found via a real, live latency flag. See [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs#a-real-genuine-backend-timeout--correctly-reported-but-with-no-way-to-tune-it) for the full story.
- **`FUSION_MAX_SOURCES=0` used to crash with a raw error** rather than returning the sensible "no valid sources specified" message that already existed elsewhere in the same code path.
