import os
from pydantic_settings import BaseSettings
from pydantic import ConfigDict


class Settings(BaseSettings):
    """
    All settings grouped by source/feature, not by when they were added —
    found via a deliberate config-completeness audit (after the battle-
    testing and bulletproofing passes) that searched every file in app/
    for hardcoded values a real homelab deployment might genuinely want to
    tune. Some hardcoded values were deliberately left out of this audit's
    additions: LLM max_tokens values (internal sizing for a specific
    prompt, not a real user preference), the 3-disambiguation-candidates
    count in kiwix.py (tightly coupled to the actual prompt wording —
    changing the count without rewriting the prompt would produce
    inconsistent behavior), and home_assistant.py's minute/hour/day
    formatting thresholds (structural facts about time, not deployment
    preferences).
    """

    model_config = ConfigDict(env_file=".env")

    # -------------------------------------------------------------------
    # Kiwix
    # -------------------------------------------------------------------
    kiwix_url: str = "http://kiwix:8080"

    # Results requested per book per search. Higher values give the
    # scoring function more candidates to find the right answer among
    # when common terms are crowded out by brand-name collisions (e.g.
    # "galaxy" returning dozens of Samsung phone articles). Increases
    # Kiwix request size.
    kiwix_search_limit: int = 15

    # Maximum number of books the LLM can select for a single query.
    # Raise this if you want broader multi-book fusion (e.g. querying
    # Python, Raspberry Pi, and Unix Stack Exchange together) at the cost
    # of more concurrent Kiwix requests per search and more candidates to
    # score.
    kiwix_max_books: int = 2

    # How many characters of a fetched article's body to keep before
    # scoring/fusion ever sees it. Found via a deliberate config-
    # completeness audit: every real call site relied on this same
    # hardcoded default, with no override anywhere — distinct from
    # FUSION_MAX_CHARS_PER_SOURCE, which truncates the already-combined
    # multi-source response, not an individual Kiwix article on its own.
    kiwix_article_max_chars: int = 3000

    # The actual, central "should a second book be fused in, or dropped
    # as noise" decision: a second book's best result must score at
    # least this fraction of the leading book's top score to be included
    # in a multi-book fusion response. Found hardcoded via the same
    # audit — documented in the README and wiki as the real mechanism,
    # but previously impossible to tune. Lower this for more aggressive
    # fusion (more books included more often); raise it for more
    # conservative fusion (only fuse when a second book is genuinely
    # competitive).
    kiwix_multi_book_fusion_threshold_pct: float = 0.5

    # -------------------------------------------------------------------
    # FreshRSS
    # -------------------------------------------------------------------
    freshrss_url: str = "http://freshrss"
    freshrss_user: str = ""
    freshrss_api_password: str = ""
    freshrss_max_articles: int = 10

    # -------------------------------------------------------------------
    # SearXNG / Web Search
    # -------------------------------------------------------------------
    searxng_url: str = "http://searxng:8080"

    # How long Mnemolis itself waits for a SearXNG response, separate
    # from SearXNG's own server-side request_timeout setting. Originally
    # found hardcoded at 10s via a config-completeness audit and made
    # configurable, but left at a default (10) below what the documented
    # fix for "Error reaching SearXNG" (raising SearXNG's own
    # max_request_timeout to 20s — see The SearXNG Timeout Lesson)
    # actually needs — a real, previously-documented-but-unfixed mismatch
    # (see CHANGELOG.md's own "found the docs said 15, the real default
    # was 10" entry, which corrected the DOCS to be honest about the
    # mismatch without ever actually closing it).
    #
    # Raised to 25 — comfortably above the 20.0 max_request_timeout this
    # project's own shipped searxng/settings.yml now sets, with headroom
    # rather than racing it exactly. Found via direct, live investigation
    # against a real deployment under real benchmark load, not assumed:
    # a per-engine timeout override (DuckDuckGo's own stale 10.0s,
    # overriding SearXNG's raised global default) was confirmed in
    # SearXNG's own logs as the actual mechanism behind a real,
    # reproducible 10-13 second tail on queries that included `web` as a
    # fused source — see wiki/Caching.md's own SearXNG section and
    # searxng/settings.yml's own comments for the full mechanism and the
    # engine-level fix that accompanies this default change. If you
    # raise SearXNG's own max_request_timeout further, raise this to
    # match or exceed it again — the two settings don't sense each
    # other's values.
    #
    # Note: this is intentionally LONGER than fusion_timeout_seconds
    # (default 15s). When a fusion query includes `web`, the fusion
    # timeout fires first and marks the source as failed, but the
    # underlying SearXNG worker thread keeps running for up to
    # (searxng_request_timeout_seconds - fusion_timeout_seconds) more
    # seconds before its own HTTP timeout fires. That's a bounded,
    # short transient pool pressure, not a persistent problem — but if
    # you raise SearXNG's timeout significantly above fusion's, you
    # increase the window during which pool workers are occupied by
    # already-abandoned tasks.
    searxng_request_timeout_seconds: int = 25

    # How many raw, unscored results to pull from each of the (up to two)
    # SearXNG searches before confidence-aware scoring filters them down.
    # This is the scoring pipeline's INPUT budget, distinct from
    # WEB_NEWS_TOP_N below, which caps the OUTPUT after scoring. Raise
    # this if you want scoring to consider a deeper pool of SearXNG's own
    # results per search; lower it to reduce how much raw data gets
    # pulled and scored per query.
    web_news_raw_result_budget: int = 25

    # Web/News confidence-aware fusion — results scoring at or below this
    # threshold are dropped as irrelevant before formatting. Survivors are
    # capped at top_n to bound response size.
    web_news_score_threshold: int = 0
    web_news_top_n: int = 10

    # Minimum query length (in words) for web search query expansion to
    # trigger — below this, a query is assumed simple enough that a
    # second, LLM-generated alternate phrasing wouldn't meaningfully
    # improve results, and isn't worth the extra LLM call. Documented in
    # the README as "3+ words"; found hardcoded with no way to adjust it.
    query_expansion_min_words: int = 3

    # -------------------------------------------------------------------
    # Forecast (Open-Meteo)
    # -------------------------------------------------------------------
    forecast_latitude: float = 0.0
    forecast_longitude: float = 0.0
    forecast_location_name: str = ""
    forecast_timezone: str = "UTC"

    # When to mention precipitation/wind in the summary, and when a
    # snapshot-to-snapshot temperature shift counts as a meaningful
    # "change" — deployment-specific preferences, not algorithm tuning.
    forecast_precip_threshold_pct: int = 20
    forecast_wind_threshold_mph: int = 15
    forecast_temp_change_threshold: float = 5.0

    # -------------------------------------------------------------------
    # Uptime Kuma
    # -------------------------------------------------------------------
    uptime_kuma_url: str = ""
    uptime_kuma_username: str = ""
    uptime_kuma_password: str = ""
    # Previously a bare, hardcoded `timeout=30` literal with no way to
    # tune it — found via a real, live Adversarial Self-Testing flag: a
    # conditional_with_remainder query took 30056ms and was flagged
    # unexpected_empty, traced directly to UptimeKumaApi's connection
    # genuinely timing out and Mnemolis correctly, honestly reporting
    # "Could not connect to Uptime Kuma" rather than hiding the
    # failure. 30 seconds is a long time to wait on what should be a
    # fast, same-LAN service before giving up — lower this for faster
    # fallback to web/other sources on a genuinely unreachable
    # instance, raise it only if your own Uptime Kuma instance is
    # known to be slow to respond for some other reason.
    uptime_kuma_timeout_seconds: int = 10

    # -------------------------------------------------------------------
    # Home Assistant
    # -------------------------------------------------------------------
    ha_url: str = ""
    ha_token: str = ""

    # Battery level (%) below which a snapshot diff reports "low"
    battery_low_threshold_pct: float = 20.0

    # -------------------------------------------------------------------
    # Timezone — shared across any feature that needs to convert a stored
    # UTC timestamp into the person's actual local time
    # -------------------------------------------------------------------
    # Found via a deliberate cross-check while researching two separate,
    # not-yet-built design docs (Predictive Pre-Fetching, Ambient Intent
    # Disambiguation): every database timestamp this project writes
    # (query_log.db, snapshots.db, adversarial_testing.db,
    # temporal_patterns.db) is hardcoded UTC, confirmed directly — but
    # _hours_since() (app/router.py, resolves "this morning"/"while at
    # work") already has a real, working, separate notion of local time,
    # sourced entirely from the container's OS-level TZ environment
    # variable (documented in README.md's "Timezone configuration"
    # section), with no reference to anything in this file at all. These
    # were two independent, previously-unreconciled mechanisms for "what
    # time is it for this person." Rather than have a third, new feature
    # invent yet another way to answer that question, or — far worse —
    # silently bucket a stored UTC timestamp by raw UTC hour-of-day,
    # which is only correct for a deployment physically in the UTC zone,
    # this setting names the SAME timezone concept _hours_since() already
    # implicitly depends on, and defaults to reading the exact same TZ
    # variable, so a deployment that's already correctly set TZ per the
    # README gets this conversion capability for free, at zero new
    # configuration cost. See app/timeutil.py for the actual conversion
    # logic this setting feeds.
    local_timezone: str = os.environ.get("TZ", "UTC")

    # -------------------------------------------------------------------
    # Snapshot Engine — time-window phrase defaults and job health
    # -------------------------------------------------------------------
    # Used to resolve phrases like "this morning" or "while at work" into
    # hour windows.
    morning_start_hour: int = 6   # "this morning" looks back to this hour, local time
    work_start_hour: int = 9      # "while at work" / "since work" looks back to this hour, local time

    # How many multiples of a job's own expected interval can pass before
    # /health flags it as "stale" rather than "ok" — generous enough by
    # default to absorb normal scheduler jitter without false-alarming on
    # a slightly delayed tick. Found hardcoded via a deliberate config-
    # completeness audit: lower this for tighter alerting on flakier
    # hardware where you want to know sooner; raise it if normal jitter
    # on your own hardware is wider than 3x the expected interval.
    snapshot_stale_grace_multiplier: int = 3

    # -------------------------------------------------------------------
    # LLM backend — for intelligent source and Kiwix book selection
    # -------------------------------------------------------------------
    # Leave LLM_URL blank to disable LLM routing and fall back to keyword
    # matching + Wikipedia.
    llm_url: str = ""
    llm_model: str = "qwen3:8b"
    llm_api_type: str = "ollama"  # "ollama" (native) or "openai" (OpenAI-compatible)

    # How many pooled HTTP connections app/llm.py's persistent Session
    # keeps open to the LLM backend at once. requests' own library
    # default (10) is sized for general-purpose use, not for this
    # project's actual concurrency shape — Starlette's own default
    # thread-pool limit for synchronous routes is 40 (confirmed
    # directly: anyio.to_thread.current_default_thread_limiter().
    # total_tokens), and a real 20-concurrent-user Locust benchmark can
    # plausibly have several of those threads simultaneously mid-LLM-call
    # at once. Sized at 20 — covers every concurrent Locust user being
    # simultaneously mid-call (this benchmark's realistic worst case)
    # without being wastefully large for a real single-household
    # deployment, where this many genuinely simultaneous LLM calls would
    # be unusual. Once concurrent calls exceed this size, requests
    # itself transparently falls back to opening additional, unpooled
    # connections rather than failing — this setting controls how many
    # of those connections stay around for reuse, not a hard concurrency
    # ceiling.
    llm_connection_pool_size: int = 20

    # How long Ollama keeps qwen3:8b resident in VRAM after Mnemolis's
    # last call to it, passed as the `keep_alive` field on every Ollama
    # native (/api/generate) call. Found missing while investigating why
    # the v3.50.14 connection-pooling fix didn't move `auto`'s own
    # benchmark plateau either: app/llm.py never sent this field at all,
    # meaning every call relied entirely on Ollama's own server-side
    # default (5 minutes) with zero application-level control — and this
    # project's own deployment documents (see CHANGELOG.md's v3.50.11
    # entry) that the same qwen3:8b instance is shared with an unrelated
    # agentic-coding workflow on the same machine, which can plausibly
    # evict the model from VRAM independent of anything Mnemolis does.
    #
    # Accepts exactly Ollama's own documented keep_alive formats, passed
    # straight through rather than reinterpreted into a different shape
    # — a duration string ("30m", "3h"), a plain number of seconds
    # ("3600"), "-1" for "never unload," or "0" for "unload immediately
    # after this call" (confirmed directly: Ollama's own FAQ documents
    # all four as valid). A real, deliberate decision NOT to default
    # this to "-1": pinning a model in VRAM indefinitely from Mnemolis's
    # side would compete with whatever else the same GPU is doing
    # between Mnemolis's own calls, for no benefit during real idle
    # periods between actual user questions — left at a value close to
    # Ollama's own default so Mnemolis's behavior doesn't surprise
    # anyone who hasn't touched this setting, while still giving Mnemolis
    # its own explicit say rather than depending entirely on whatever the
    # server's ambient default happens to be.
    #
    # Only sent on the Ollama-native path. Confirmed NOT reliably honored
    # by Ollama's own OpenAI-compatible endpoint (a real, externally
    # reported gap — passing keep_alive through OpenAI-SDK-style calls
    # is silently ignored, server falls back to its own default
    # regardless of what's sent), so sending it there would be a false
    # promise of control this setting can't actually deliver — see
    # _complete_openai()'s own comment for why it's deliberately omitted
    # there instead of sent anyway and hoped for.
    llm_keep_alive: str = "5m"

    # -------------------------------------------------------------------
    # Grounded Answer Synthesis — opt-in LLM composition of a short,
    # grounded answer from retrieved material (see Design Doc 4 and
    # wiki/Answer-Synthesis.md). Additive by contract: the `result`
    # field always carries the raw retrieved text whether or not
    # synthesis ran; synthesis only ever populates the separate
    # `answer` field, and any failure yields answer=null plus an
    # explanation event, never a degraded result.
    # -------------------------------------------------------------------
    # Master switch, standard rollout contract: default false for one
    # release, and while false the per-request `synthesize` flag becomes
    # a no-op with a `synthesis_skipped: disabled` explanation event
    # rather than silently running anyway.
    synthesis_enabled: bool = False

    # Timeout for the synthesis generation call — deliberately its own
    # budget, independent of the routing call's hardcoded 10s: this is a
    # real multi-sentence generation, not a one-token routing pick.
    synthesis_timeout_seconds: int = 20

    # Blank = use LLM_MODEL. Exists so routing can run a small, fast
    # model while synthesis runs a larger instruct model on the same
    # backend — the two calls have genuinely different quality/latency
    # tradeoffs.
    synthesis_model: str = ""

    # Total characters of retrieved material offered to the synthesis
    # prompt, apportioned proportionally across attributed sections.
    # qwen3:8b at 32K context has room for far more; the budget keeps
    # latency predictable and is configurable down for smaller models.
    synthesis_input_budget_chars: int = 6000

    # Results shorter than this skip synthesis entirely — a one-line HA
    # state answer like "Front Door: locked" is already the ideal voice
    # answer, and rewriting it can only add risk. This rule alone
    # exempts most ha/uptime traffic, which is correct.
    synthesis_min_input_chars: int = 200

    # Per-style answer length ceilings (characters), enforced by
    # sentence-boundary truncation as a backstop behind the prompt's own
    # style instruction — length is a first-class request parameter for
    # the voice pipeline, not a prompt suggestion alone. "brief" is
    # fixed at 800 in app/synthesis.py (an internal middle point between
    # these two real deployment-tunable extremes, per the config audit's
    # own "not every internal sizing constant is a user preference"
    # judgment).
    synthesis_max_chars: int = 2000
    synthesis_voice_max_chars: int = 400

    # "digest" answer style (v3.55.1) — for "summarize / list / read me
    # everything" queries where the value is breadth, not a single fused
    # answer. It gets its own, larger output cap so many distinct items
    # survive rather than being compressed into two sentences, and its own
    # larger INPUT budget (below) so a big multi-item result — ten news
    # headlines, a wide fusion — actually reaches the model before
    # per-section apportioning truncates it. Both default well above the
    # other styles precisely because preservation is the point.
    synthesis_digest_max_chars: int = 3000
    synthesis_digest_input_budget_chars: int = 12000

    # -------------------------------------------------------------------
    # Fusion — concurrency, payload size, and timeout limits
    # -------------------------------------------------------------------
    fusion_max_sources: int = 4
    fusion_max_chars_per_source: int = 1500
    fusion_timeout_seconds: int = 15

    # How many worker threads the shared, module-level fusion executor
    # keeps available across every concurrent fusion call. Found via a
    # deliberate investigation into a real, recurring, previously-
    # unexplained RemoteDisconnected failure: fusion.py used to spin up
    # a brand-new, uncapped ThreadPoolExecutor on every single call
    # (sized to len(valid), typically 2-3) — confirmed directly that 20
    # concurrent fusion-shaped requests produce 81 real, live OS threads
    # at peak, with no ceiling at all as concurrent fusion traffic
    # increases. Replaced with a single, shared, long-lived pool — the
    # same shape of fix app/llm.py's connection pool already applied to
    # a different unbounded-per-call resource (HTTP connections). Sized
    # generously relative to FUSION_MAX_SOURCES' own default (4) times a
    # handful of genuinely concurrent in-flight fusion requests —
    # bounding the ceiling, not throttling realistic load.
    fusion_thread_pool_size: int = 12

    # Found via a deliberate function-by-function read of searxng.py:
    # search() spun up a brand-new, 2-worker ThreadPoolExecutor on every
    # single call (one worker for the primary fetch, one for the
    # alternate-phrasing chain) — the identical unbounded-per-call
    # pattern already found and fixed in fusion.py above, just never
    # checked for here. Confirmed directly, not estimated: 15 concurrent
    # search() calls under realistic network latency produced a measured
    # peak of 46 real OS threads, with no ceiling as concurrent traffic
    # increases. Worse here than the original fusion.py case in one real
    # way: fusion.py dispatches to searxng.search() THROUGH its own
    # already-bounded _fusion_executor pool when a fusion query includes
    # `web` — meaning every one of fusion's 12 bounded workers could
    # simultaneously spin up its own additional, unbounded 2-worker pool,
    # undermining the very bound fusion.py's own fix was meant to
    # establish. Replaced with the identical shared-pool shape as
    # FUSION_THREAD_POOL_SIZE — sized smaller (2 is the genuine per-call
    # need; this only needs enough headroom for a few requests'
    # primary+alternate pairs to be in flight at once, not capacity for
    # every concurrent request the way fusion's source-fanout does).
    searxng_thread_pool_size: int = 4

    # How many decomposed sub-queries route_with_source() resolves
    # concurrently for one compound query. Sub-queries are genuinely
    # independent by construction (see _decompose() — it only splits on
    # conjunctions joining separate intents), so a 3-intent cold
    # compound query no longer pays three stacked LLM-routing +
    # source-fetch costs back to back. Uses a fresh, per-call executor
    # bounded by min(len(sub_queries), this value) — deliberately NOT a
    # shared module-level pool, since these workers recurse into
    # route_with_source() itself and a shared pool submitting into
    # itself and blocking is a real deadlock under saturation (see the
    # comment at the dispatch site in app/router.py). This value caps
    # per-query fanout; realistic decomposed queries are 2-4 intents,
    # so the default covers them fully without letting a pathological
    # many-conjunction query spin up unbounded concurrent source work.
    decompose_max_parallel: int = 4

    # -------------------------------------------------------------------
    # Semantic routing cache — embedding-based reuse of routing decisions
    # -------------------------------------------------------------------
    # The one performance frontier left after the v3.50.x latency
    # campaign: a cold LLM routing call costs ~200-700ms on this
    # project's own reference hardware, and the exact-match routing
    # cache can't see that "will it rain later" and "will it rain this
    # evening" are the same routing decision. With an embedding model
    # configured, a routing-cache miss first embeds the query (~one
    # small POST on llm.py's existing connection pool) and compares it
    # against embeddings of previously-decided queries; a match above
    # the similarity threshold reuses that decision and skips the LLM
    # entirely. See wiki/Semantic-Routing-Cache.md for the full design.
    #
    # EMBEDDING_MODEL empty (the default) disables the whole feature
    # with zero behavior change — routing/completion and embeddings are
    # independent capabilities, and a fresh GitHub deployment shouldn't
    # pay a failed HTTP call per cold decision for a model that was
    # never pulled. On Ollama, `ollama pull nomic-embed-text` (or
    # all-minilm, mxbai-embed-large) then set EMBEDDING_MODEL to match.
    embedding_model: str = ""
    # Empty = use LLM_URL (the normal case: same Ollama instance serves
    # both). Set only if embeddings live on a different backend.
    embedding_url: str = ""
    # Deliberately short — an embedding is a tiny fraction of a
    # completion's cost, and the fallback on timeout is just "do what
    # v3.52.0 did" (call the routing LLM), so waiting long here only
    # delays the path that was about to run anyway.
    embedding_timeout_seconds: int = 5
    # Cosine-similarity floor for reusing another query's routing
    # decision. Deliberately conservative: a wrong reuse silently
    # routes a query to the wrong source, which is strictly worse than
    # the ~200-700ms LLM call it saved. 0.92 was chosen as a floor
    # where rephrasing ("will it rain later" / "will it rain this
    # evening") clears it but topic-adjacent-yet-different intents
    # ("is it raining" / "is my network down") do not, for typical
    # sentence-embedding models. Tune DOWN cautiously and only after
    # watching /cache/semantic for near-miss pairs on your own real
    # traffic; tune UP if you ever catch a wrong reuse.
    semantic_routing_threshold: float = 0.92
    # Max stored (query -> embedding) entries before oldest-eviction —
    # the same bounded-eviction pattern both other caches use. Sized to
    # the routing cache's own "source:" key population; entries whose
    # underlying routing-cache decision has expired are skipped at
    # match time and pruned opportunistically, so this store can never
    # serve a decision the routing cache itself no longer stands
    # behind.
    semantic_cache_max_size: int = 500
    # Re-embed the routing cache's persisted "source:" queries in a
    # background thread at startup, restoring the semantic store's full
    # pre-restart matching ability (the store itself is deliberately
    # in-memory only — see the persistence rationale in
    # app/semantic_routing.py — but routing_cache.json persists, so
    # only the vectors need rebuilding, batched into a handful of
    # requests). Never blocks startup; a failed batch just falls back
    # to the normal lazy through-use population. Only relevant when
    # EMBEDDING_MODEL is set. Off switch exists for deployments where
    # the embedding backend is slow to come up after a reboot and the
    # warmup's failure warnings would be noise.
    semantic_warmup_enabled: bool = True

    # -------------------------------------------------------------------
    # Caching — result cache, routing cache, and per-source TTLs
    # -------------------------------------------------------------------
    # Result cache — max entries before oldest-eviction kicks in.
    # Lower this on memory-constrained hardware (e.g. an N100 with
    # limited RAM).
    cache_max_size: int = 500

    # Routing cache — max entries before oldest-eviction kicks in.
    # Separate from cache_max_size since the routing cache key space is
    # genuinely larger: every unique conditional query, discourse-framing
    # phrase, and disambiguation candidate set gets its own entry, on top
    # of plain source-routing decisions. Found via real usage — this
    # cache had NO size limit at all until this setting was added, unlike
    # the result cache, which could grow unboundedly over sustained
    # real-world usage.
    routing_cache_max_size: int = 1000

    # How long a routing decision (which source, which Kiwix book, which
    # disambiguation candidates) stays cached before the LLM gets asked
    # again. Found hardcoded via a deliberate config-completeness audit —
    # presented as a deliberate, reasoned default in the wiki's Caching
    # page, but previously impossible to actually adjust.
    routing_cache_ttl_seconds: int = 3600

    # Per-source result cache TTLs — found hardcoded via the same audit.
    # Each reflects how stale an answer from that specific source is
    # acceptable to be, by design (offline encyclopedic content barely
    # changes within a day; lights and locks change state constantly).
    # Lower a given source's TTL if your own deployment needs fresher
    # answers from it than the default assumes; raise it to reduce real
    # backend load from repeated identical queries.
    cache_ttl_kiwix_seconds: int = 86400      # 24 hours
    cache_ttl_forecast_seconds: int = 1800    # 30 minutes
    cache_ttl_news_seconds: int = 900         # 15 minutes
    cache_ttl_web_seconds: int = 3600         # 1 hour
    cache_ttl_uptime_seconds: int = 60        # 1 minute
    cache_ttl_ha_seconds: int = 30            # 30 seconds
    cache_ttl_changes_seconds: int = 120      # 2 minutes — changes are near-real-time
    cache_ttl_fusion_seconds: int = 1800      # 30 minutes

    # -------------------------------------------------------------------
    # API key authentication — protects /search and /changes
    # -------------------------------------------------------------------
    # Leave blank to disable auth entirely (default, backward compatible).
    # Comma-separated list of valid keys, e.g. "key1,key2,key3".
    api_keys: str = ""

    # -------------------------------------------------------------------
    # Adversarial self-testing — background combinatorial query generation
    # -------------------------------------------------------------------
    # Master on/off switch. Disabling skips DB init, never registers the
    # scheduler job, and POST /adversarial/trigger returns a clear
    # "disabled" response instead of silently running anyway — a real
    # opt-out for anyone who'd rather not have any extra background
    # traffic hitting their LLM/SearXNG/Kiwix backends, not a setting
    # that only half-works.
    adversarial_test_enabled: bool = True

    # How often the adversarial test scheduler tick runs. 60 minutes is
    # frequent enough to accumulate real coverage over days/weeks while
    # never meaningfully competing with real traffic for resources. Don't
    # hardcode this — per the project's own config-completeness audit
    # philosophy, anything with a reasonable default still gets a real
    # setting rather than a magic number buried in main.py.
    adversarial_test_interval_minutes: int = 60

    # How many queries to generate per scheduler tick. A small batch costs
    # nothing extra (no LLM calls in the hot path — generation is pure
    # combinatorics) and produces more real coverage per tick than
    # generating one query at a time. Raise this on more powerful
    # hardware (e.g. The Beast) for faster coverage accumulation; lower
    # it on weaker hardware (e.g. MiniDock's N100) if a tick visibly
    # competes with real query latency.
    adversarial_test_batch_size: int = 8

    # How many multiples of a recipe's own historical p95 latency counts
    # as a real outlier, not just normal variance — same role as
    # SNAPSHOT_STALE_GRACE_MULTIPLIER, but for the latency-outlier check
    # rather than job staleness. Real, observed variance within a single
    # recipe (two conditional_with_remainder queries differing 2028ms vs.
    # 276ms on identical hardware, almost certainly a cache hit/miss
    # difference) means this needs real headroom by default rather than
    # a tight multiplier that flags normal cache behavior as an anomaly.
    adversarial_test_latency_outlier_multiplier: float = 1.5

    # A floor below which latency is never flagged as an outlier
    # regardless of the multiplier — protects against flagging
    # genuinely fast, cache-hit-driven queries (276ms in real testing)
    # just because they happen to be 1.5x some even-faster historical
    # sample. Different hardware has very different "fast" baselines,
    # so this is exposed rather than fixed.
    adversarial_test_latency_outlier_floor_ms: int = 1000

    # How many historical latency samples a recipe needs before the
    # latency-outlier check engages at all — below this, "is this slow"
    # genuinely isn't yet decidable, so the check stays silent rather
    # than guessing off too little data. Lower this for faster feedback
    # on a high-traffic deployment; raise it if early flags before
    # enough real history exists feel premature.
    adversarial_test_latency_outlier_min_samples: int = 10

    # -------------------------------------------------------------------
    # Cross-Source Temporal Pattern Detection — speculative pattern-mining
    # over structured event history. See the design doc and wiki page for
    # the full statistical reasoning; the settings below are summarized
    # here only.
    # -------------------------------------------------------------------
    # Master on/off switch, following the exact precedent
    # ADVERSARIAL_TEST_ENABLED established: checked at both
    # scheduler-registration time and inside the cycle function itself
    # (defense in depth), reporting {"status": "disabled"} directly in
    # /health rather than eventually reading as stale.
    temporal_pattern_detection_enabled: bool = True

    # How often the mining cycle runs. Deliberately much longer than
    # either the snapshot engine's or adversarial testing's intervals —
    # mining over a short window is statistically meaningless (nothing
    # to find yet) and wasteful to re-run constantly. Once daily is
    # plenty given the real event volumes involved (see the design
    # doc's section 2.1 — tens to low hundreds of genuine events per
    # month even on the densest structured source).
    temporal_pattern_mining_interval_hours: int = 24

    # The maximum lag ("expiry time", in the frequent-episode-mining
    # sense) within which event B must follow event A to count as one
    # real occurrence of the (A, B) pair. Mirrors the AID/frequent-
    # episode-mining "expiry time" concept directly — searching for "did
    # B ever follow A, at any distance" is both statistically
    # meaningless (something will eventually match, given enough time)
    # and far more expensive to compute.
    temporal_pattern_lag_window_minutes: int = 30

    # A hard floor below which a pattern is never even considered,
    # regardless of what the corrected significance test says. A
    # pattern based on 2-3 raw occurrences shouldn't be reported no
    # matter what the math says — the literal, honest truth is there
    # isn't enough data yet to say anything (design doc section 2.4's
    # "under-specified with limited data" finding).
    temporal_pattern_min_occurrences: int = 5

    # The significance level for Bonferroni-corrected hypothesis tests
    # — the per-comparison alpha gets divided by the total number of
    # (A, B, lag-bucket) tests run in a single mining pass before being
    # compared against. 0.05 is the conventional default; lower this
    # for a stricter bar (fewer, more confident candidates), raise it
    # only with a clear understanding that this widens the
    # already-documented false-positive risk this feature is built
    # around minimizing.
    temporal_pattern_significance_level: float = 0.05

    # How much later (non-overlapping) data a candidate pattern needs
    # to be re-checked against before it can be promoted to
    # "confirmed". A separate setting from the mining interval above —
    # so re-validation cadence isn't accidentally coupled to how often
    # the scheduler tick happens to fire. Default: one full mining
    # interval's worth of new data (24h), the simplest concrete
    # definition of "a later, independent window" that doesn't require
    # tracking anything beyond what the mining cycle already does.
    temporal_pattern_validation_window_hours: int = 24

    # Same role as SNAPSHOT_STALE_GRACE_MULTIPLIER / the adversarial
    # testing job's own staleness convention — how many multiples of
    # the mining interval can pass before /health flags this job
    # "stale" rather than "ok".
    temporal_pattern_stale_grace_multiplier: int = 3


settings = Settings()
