# Mnemolis

[![Tests](https://github.com/immortalbob/Mnemolis/actions/workflows/tests.yml/badge.svg)](https://github.com/immortalbob/Mnemolis/actions/workflows/tests.yml)
[![Lint](https://github.com/immortalbob/Mnemolis/actions/workflows/lint.yml/badge.svg)](https://github.com/immortalbob/Mnemolis/actions/workflows/lint.yml)
[![Docker Build](https://github.com/immortalbob/Mnemolis/actions/workflows/docker-build.yml/badge.svg)](https://github.com/immortalbob/Mnemolis/actions/workflows/docker-build.yml)

A unified local knowledge search API for self-hosted homelabs. Mnemolis runs as a Docker container on your internal network and routes queries to the appropriate backend — offline knowledge, weather forecast, RSS news, live web search, service monitoring, or multiple sources concurrently — via a single endpoint.

Exposes both a **REST API** and an **MCP server** so any client can connect to it.

This README covers what it is, installation, and the API reference. For deep-dive mechanism detail, exact scoring weights, and the real bugs found and fixed along the way, see the **[Wiki](https://github.com/immortalbob/Mnemolis/wiki)**.

## Why Mnemolis

A homelab accumulates real, distinct sources of truth — your own RSS feeds, an offline encyclopedia, weather, service uptime, Home Assistant state — but each one normally needs its own query language, its own client, its own mental context switch. Mnemolis exists so you can ask one plain-language question and not have to know in advance which backend actually has the answer, or query three of them yourself when the real answer spans more than one. It runs entirely on your own infrastructure — Open-Meteo is the one deliberate exception — so asking it something never means sending your query, your home's state, or your reading habits to a third party.

## Sources

| Source | Backend | Description |
|--------|---------|-------------|
| `kiwix` | [Kiwix](https://www.kiwix.org/) | Offline knowledge base — Wikipedia, Stack Exchange, iFixit, FreeCodeCamp, DevDocs |
| `forecast` | [Open-Meteo](https://open-meteo.com/) | 3-day weather forecast, no API key required |
| `news` | [FreshRSS](https://freshrss.github.io/FreshRSS/) | Recent articles from your RSS feeds via GReader API |
| `web` | [SearXNG](https://searxng.github.io/searxng/) | Live web search via your local SearXNG instance |
| `uptime` | [Uptime Kuma](https://uptime.kuma.pet/) | Service monitor status — reports any down services |
| `ha` | [Home Assistant](https://www.home-assistant.io/) | Entity state summaries — lights, locks, sensors, motion, batteries, power |
| `changes` | Snapshot Engine | Detected changes since last snapshot — outages, weather shifts, new headlines |
| `fusion` | — | Query multiple sources concurrently and merge results |
| `auto` | — | Mnemolis detects intent and picks the best source |

## Integrations

| Client | Protocol | How |
|--------|----------|-----|
| [Open WebUI](mnemolis_tool.py) | REST | Lightweight tool that POSTs to `/search` |
| [Mnemolis Intents](https://github.com/immortalbob/mnemolis_intents) | REST | Native HA LLM API integration |
| Any MCP client (Claude Desktop, Cursor, etc.) | MCP/Streamable HTTP | Connect to `http://your-host:8888/mcp` |

## MCP

Mnemolis exposes an MCP server via Streamable HTTP at `/mcp` on the same port as the REST API. (Previously SSE at `/mcp/sse` — migrated since SSE is being superseded across the MCP ecosystem. See the wiki's [MCP Server](https://github.com/immortalbob/Mnemolis/wiki/MCP-Server) page for the full reasoning and a real bug found and fixed during the migration.)

### Connecting Claude Desktop

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mnemolis": {
      "url": "http://your-host-ip:8888/mcp"
    }
  }
}
```

### Connecting any MCP client

Streamable HTTP endpoint: `http://your-host-ip:8888/mcp`

The MCP server exposes a single `search` tool with the same interface as the REST API, including `fusion_sources` support.

## Requirements

- Docker + Docker Compose
- A Docker network for container communication (default: `mnemo-net`)
- One or more of the supported backends running and reachable on the same network

## Quick Start

### Full stack (recommended)

The repo includes an example compose file and SearXNG config to get all services running together:

```bash
git clone https://github.com/immortalbob/Mnemolis
cd Mnemolis

# Create the shared network if it doesn't exist
docker network create mnemo-net

# Copy and edit the example compose file
cp docker-compose.example.yml docker-compose.yml
# Fill in credentials, your coordinates, and secret_key in searxng/settings.yml

docker compose up -d
```

### What's not in the full stack
The example compose intentionally excludes Home Assistant, your LLM backend, and Uptime Kuma — these are typically long-running services with their own existing setup.

If you're running any of these in Docker and want them reachable by Mnemolis, connect them to `mnemo-net`:

```bash
docker network connect mnemo-net ollama
docker network connect mnemo-net homeassistant
```

### Mnemolis only

If you already have the backends running:

```bash
git clone https://github.com/immortalbob/Mnemolis
cd Mnemolis
# Edit docker-compose.yml with your settings
docker compose up -d
```

Hit `http://your-host:8888/health` to confirm it's running.
Full API docs at `http://your-host:8888/docs`.

## Configuration

All settings are passed as environment variables in `docker-compose.yml`:

| Variable | Description | Default |
|----------|-------------|---------|
| `KIWIX_URL` | Kiwix container URL | `http://kiwix:8080` |
| `FRESHRSS_URL` | FreshRSS container URL | `http://freshrss` |
| `FRESHRSS_USER` | FreshRSS username | |
| `FRESHRSS_API_PASSWORD` | FreshRSS API password | |
| `FRESHRSS_MAX_ARTICLES` | Max articles to fetch | `10` |
| `SEARXNG_URL` | SearXNG container URL | `http://searxng:8080` |
| `SEARXNG_REQUEST_TIMEOUT_SECONDS` | How long Mnemolis itself waits for a SearXNG response — set to match or exceed SearXNG's own server-side `max_request_timeout`, see [SearXNG request timeout](#searxng-request-timeout) | `25` |
| `WEB_NEWS_RAW_RESULT_BUDGET` | How many raw, unscored results to pull from each web search before confidence-aware scoring filters them down — the scoring pipeline's *input* budget, distinct from `WEB_NEWS_TOP_N`'s *output* cap below | `25` |
| `QUERY_EXPANSION_MIN_WORDS` | Minimum query length (in words) for web search query expansion to trigger | `3` |
| `KIWIX_ARTICLE_MAX_CHARS` | How many characters of a fetched Kiwix article to keep before scoring/fusion sees it — distinct from `FUSION_MAX_CHARS_PER_SOURCE`, which truncates the already-combined multi-source response | `3000` |
| `KIWIX_MULTI_BOOK_FUSION_THRESHOLD_PCT` | A second book's best result must score at least this fraction of the leading book's top score to be included in a multi-book fusion response. Lower for more aggressive fusion, raise for more conservative | `0.5` |
| `SNAPSHOT_STALE_GRACE_MULTIPLIER` | How many multiples of a job's own expected interval can pass before `/health` flags it as "stale" rather than "ok" | `3` |
| `ROUTING_CACHE_TTL_SECONDS` | How long a routing decision (source, Kiwix book, disambiguation candidates) stays cached before the LLM gets asked again | `3600` |
| `CACHE_TTL_KIWIX_SECONDS` | Result cache TTL for `kiwix` | `86400` |
| `CACHE_TTL_FORECAST_SECONDS` | Result cache TTL for `forecast` | `1800` |
| `CACHE_TTL_NEWS_SECONDS` | Result cache TTL for `news` | `900` |
| `CACHE_TTL_WEB_SECONDS` | Result cache TTL for `web` | `3600` |
| `CACHE_TTL_UPTIME_SECONDS` | Result cache TTL for `uptime` | `60` |
| `CACHE_TTL_HA_SECONDS` | Result cache TTL for `ha` | `30` |
| `CACHE_TTL_CHANGES_SECONDS` | Result cache TTL for `changes` | `120` |
| `CACHE_TTL_FUSION_SECONDS` | Result cache TTL for `fusion` | `1800` |
| `FORECAST_LATITUDE` | Forecast location latitude — required for `forecast` to work at all; leaving it unset correctly reports `forecast` as not configured rather than returning weather for the wrong place | _(unset)_ |
| `FORECAST_LONGITUDE` | Forecast location longitude — same requirement as above | _(unset)_ |
| `FORECAST_LOCATION_NAME` | Human-readable location name | _(blank)_ |
| `FORECAST_TIMEZONE` | Timezone for forecast times | `UTC` |
| `UPTIME_KUMA_URL` | Uptime Kuma URL | _(blank — disables uptime source)_ |
| `UPTIME_KUMA_USERNAME` | Uptime Kuma username | |
| `UPTIME_KUMA_PASSWORD` | Uptime Kuma password | |
| `UPTIME_KUMA_TIMEOUT_SECONDS` | How long the Uptime Kuma client waits before giving up. Lower for faster fallback on a genuinely unreachable instance | `10` |
| `HA_URL` | Home Assistant URL | _(blank — disables HA source)_ |
| `HA_TOKEN` | Home Assistant long-lived access token | |
| `LLM_URL` | LLM backend URL for intelligent routing | _(blank — disables LLM routing)_ |
| `LLM_MODEL` | Model to use for source and book selection | `qwen3:8b` |
| `LLM_API_TYPE` | API format: `ollama` or `openai` | `ollama` |
| `LLM_CONNECTION_POOL_SIZE` | How many pooled HTTP connections to keep open to the LLM backend at once, for reuse across calls. Raise this if you run with significantly more than 20 concurrent users/requests | `20` |
| `LLM_KEEP_ALIVE` | How long Ollama keeps the model resident in VRAM after Mnemolis's last call (Ollama-native backend only — see below). Accepts Ollama's own formats: a duration string (`30m`, `3h`), plain seconds, `-1` (never unload), or `0` (unload immediately) | `5m` |
| `EMBEDDING_MODEL` | Embedding model for the [semantic routing cache](https://github.com/immortalbob/Mnemolis/wiki/Semantic-Routing-Cache) — reuses routing decisions across rephrasings of the same question, skipping the cold LLM routing call. Blank disables the feature entirely (zero behavior change). On Ollama: `ollama pull nomic-embed-text`, then set this to match | _(blank — disabled)_ |
| `EMBEDDING_URL` | Backend serving the embedding model. Blank = same as `LLM_URL` | _(blank — uses `LLM_URL`)_ |
| `SEMANTIC_ROUTING_THRESHOLD` | Cosine-similarity floor for reusing another query's routing decision. Deliberately conservative — a wrong reuse silently misroutes | `0.92` |
| `MORNING_START_HOUR` | Reference hour (0-23, local time) for resolving "this morning" in changes queries | `6` |
| `WORK_START_HOUR` | Reference hour (0-23, local time) for resolving "while at work" in changes queries | `9` |
| `API_KEYS` | Comma-separated list of valid API keys. Protects `POST /search` and `GET /changes`. | _(blank — auth disabled)_ |
| `FORECAST_PRECIP_THRESHOLD_PCT` | Precipitation probability (%) above which the forecast mentions rain chance | `20` |
| `FORECAST_WIND_THRESHOLD_MPH` | Wind speed (mph) above which the forecast mentions wind | `15` |
| `FORECAST_TEMP_CHANGE_THRESHOLD` | Temperature shift (°) between snapshots that counts as a meaningful weather change | `5.0` |
| `BATTERY_LOW_THRESHOLD_PCT` | Battery level (%) below which a snapshot diff reports "low" | `20.0` |
| `FUSION_MAX_SOURCES` | Maximum number of sources allowed in a single fusion query | `4` |
| `FUSION_MAX_CHARS_PER_SOURCE` | Characters per source result before truncation in fusion output | `1500` |
| `FUSION_TIMEOUT_SECONDS` | Maximum time to wait for any single source in a fusion query — now also bounds the caller's actual wait, not just the gather loop (see v3.50.18) | `15` |
| `FUSION_THREAD_POOL_SIZE` | Worker threads in fusion's shared, long-lived thread pool, reused across every concurrent fusion call instead of a fresh pool per call | `12` |
| `CACHE_MAX_SIZE` | Maximum result cache entries before oldest-eviction kicks in | `500` |
| `ROUTING_CACHE_MAX_SIZE` | Maximum routing cache entries before oldest-eviction kicks in | `1000` |
| `KIWIX_SEARCH_LIMIT` | Results requested per book per Kiwix search — higher values help the scoring function find the right answer among brand-name collisions | `15` |
| `KIWIX_MAX_BOOKS` | Maximum number of Kiwix books the LLM can select for a single query — raise for broader multi-book fusion | `2` |
| `WEB_NEWS_SCORE_THRESHOLD` | Web/news results scoring at or below this are dropped as irrelevant | `0` |
| `WEB_NEWS_TOP_N` | Maximum web/news results kept after scoring | `10` |
| `LOG_LEVEL` | Application log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) — `INFO` shows decomposition splits, disambiguation candidates, and article selection decisions | `INFO` |
| `ADVERSARIAL_TEST_ENABLED` | Master on/off switch for [Adversarial Self-Testing](https://github.com/immortalbob/Mnemolis/wiki/Adversarial-Self-Testing). `false` skips DB init, never registers the scheduler job, and makes `POST /adversarial/trigger` a safe no-op | `true` |
| `ADVERSARIAL_TEST_INTERVAL_MINUTES` | How often the adversarial-testing scheduler tick fires | `60` |
| `ADVERSARIAL_TEST_BATCH_SIZE` | Queries generated per tick — cheap to raise, since generation is pure combinatorics with no LLM calls in the hot path | `8` |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_MULTIPLIER` | How many multiples of a recipe's own historical p95 latency counts as a real outlier | `1.5` |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_FLOOR_MS` | A floor below which latency is never flagged regardless of the multiplier | `1000` |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_MIN_SAMPLES` | How many historical samples a recipe needs before the latency-outlier check engages at all | `10` |
| `TEMPORAL_PATTERN_DETECTION_ENABLED` | Master on/off switch for [Cross-Source Temporal Pattern Detection](https://github.com/immortalbob/Mnemolis/wiki/Cross-Source-Temporal-Pattern-Detection). `false` skips DB init, never registers the scheduler job, and makes `POST /temporal-patterns/trigger` a safe no-op | `true` |
| `TEMPORAL_PATTERN_MINING_INTERVAL_HOURS` | How often the mining cycle runs — deliberately far longer than every other scheduler job, since mining over a short window is statistically meaningless given how infrequently real structured events occur | `24` |
| `TEMPORAL_PATTERN_LAG_WINDOW_MINUTES` | The maximum lag within which event B must follow event A to count as one real occurrence of that pair | `30` |
| `TEMPORAL_PATTERN_MIN_OCCURRENCES` | A hard floor below which a pair is never even significance-tested, regardless of what the math would say | `5` |
| `TEMPORAL_PATTERN_SIGNIFICANCE_LEVEL` | The per-comparison significance level, before Bonferroni correction divides it by the number of pairs actually tested in a given pass | `0.05` |
| `TEMPORAL_PATTERN_VALIDATION_WINDOW_HOURS` | How much later, non-overlapping data a candidate needs to be re-checked against before it can be promoted to `confirmed` | `24` |
| `TEMPORAL_PATTERN_STALE_GRACE_MULTIPLIER` | Same role as `SNAPSHOT_STALE_GRACE_MULTIPLIER` — how many missed mining intervals before `/health` flags this job stale | `3` |

### FreshRSS API setup
1. Enable API access: **Administration → Authentication → Allow API access**
2. Set an API password: **Profile → API password**
3. Use that password for `FRESHRSS_API_PASSWORD` (it's separate from your login password)

### SearXNG JSON format
Mnemolis queries SearXNG's JSON API. The included `searxng/settings.yml` already has this enabled. If you're using an existing SearXNG instance, make sure `json` is in your formats list:

```yaml
search:
  formats:
    - html
    - json
```

Also generate a unique `secret_key` in `searxng/settings.yml`:

```bash
openssl rand -hex 32
```

### SearXNG request timeout

SearXNG's default `request_timeout` (3.0s) is too short for several real, commonly-used engines, which can take 15-25+ seconds to respond under normal conditions. The shipped `searxng/settings.yml` already raises this:

```yaml
outgoing:
  request_timeout: 10.0
  max_request_timeout: 20.0
  pool_connections: 100
  pool_maxsize: 20
```

If you're upgrading from an older deployment, copy these into your own `settings.yml` and restart SearXNG. If `"Error reaching SearXNG: connection failed"` persists after the change, **verify SearXNG actually picked it up** — a correctly-edited config file doesn't help if the container was never restarted. Full story, including how this was diagnosed: **[The SearXNG Timeout Lesson](https://github.com/immortalbob/Mnemolis/wiki/The-SearXNG-Timeout-Lesson)**.

`SEARXNG_REQUEST_TIMEOUT_SECONDS` (default `25`, on the Mnemolis side) is already set above SearXNG's own `max_request_timeout` shown here — if you raise the SearXNG-side value further, raise this to match or exceed it too, or Mnemolis will cut the connection first regardless of how generously SearXNG itself is configured to wait.

**Several major engines are disabled by default in the shipped `settings.yml`, in favor of a smaller, more reliable set.** Found via direct log inspection under real, sustained query load against a small self-hosted instance:

| Engine | Why it's disabled |
|--------|--------------------|
| `duckduckgo` | Its own per-engine `timeout:` stays at SearXNG's old factory value (10.0s) even after the global timeouts above are raised — per-engine overrides don't inherit from global settings — and it independently hit its own CAPTCHA defense under sustained querying |
| `google` | SearXNG's own Google scraper has a known, recurring, externally-reported bug (`IndexError: list index out of range`) whenever Google's HTML structure shifts — not specific to this deployment |
| `bing` | The identical class of scraper fragility reported against Google also affects Bing's scraper |
| `brave` | Hit a real rate-limit suspension (`suspended_time=180`) under sustained querying |
| `wikipedia` | Also hit a real rate-limit suspension under the same load |

`mojeek` and `presearch` are explicitly enabled in their place (both disabled by SearXNG's own default) — corroborated by an independent report of someone hitting the identical failure pattern (`brave` suspended, `duckduckgo` access-denied) and finding `mojeek`/`startpage`/`presearch` worked cleanly. `startpage` is already enabled by SearXNG's own default.

A frequently-hanging or frequently-blocked engine contributes nothing to a fused result while still costing a slow timeout or a failed request on every query that includes it. Re-enable any of the disabled engines (`disabled: false` in the `engines:` block) if your own instance doesn't see this behavior — bot-detection sensitivity and rate-limit thresholds vary by IP reputation and query volume, so what broke on one deployment may not break on another.

### LLM-assisted routing
Mnemolis uses a local LLM backend in five ways:

1. **Source selection** — when `auto` is used and no keyword matches, the LLM picks the best source based on the query, returning multiple sources for complex multi-topic queries to trigger fusion automatically. Also biases toward including Kiwix for "everyone's talking about X"-style discourse framing — see [Routing](https://github.com/immortalbob/Mnemolis/wiki/Routing#the-discourse-framing-bias).
2. **Book selection** — once routed to Kiwix, the LLM picks the best books from your catalog for the query, up to `KIWIX_MAX_BOOKS` (default 2)
3. **Search term disambiguation** — for short, definitional Kiwix queries (e.g. "what is a galaxy"), the LLM generates 2-3 candidate disambiguation terms to break brand-name/homonym collisions. Each candidate is actually searched and scored against real Kiwix results rather than trusting a single guess — see [Kiwix Internal Flow](#kiwix-internal-flow).
4. **Fusion source selection** — when `fusion` is used without specifying sources, the LLM picks the best 2-3 sources for the query
5. **Web query expansion** — for web searches of 3+ words, the LLM generates one alternate phrasing so SearXNG is queried twice and results merged, scored against your original query — see [Confidence-aware fusion](#confidence-aware-fusion-web--news)

**Auto-fusion escalation** — `source="auto"` now detects multi-topic queries at the keyword level too. If a query matches triggers from multiple sources (e.g. "weather" + "services up"), fusion is triggered automatically without an LLM call.

Routing decisions (including disambiguation candidates and alternate phrasings) are cached for 1 hour so repeated queries skip the LLM call entirely.

Query decomposition and conditional detection (see [Query Decomposition](#query-decomposition) and [Conditional Query Detection](#conditional-query-detection) above) are deliberately pure pattern matching with no LLM involvement at all — they need to run on every single query before any LLM call happens at all, including when no LLM is configured.

**Supported backends** via `LLM_API_TYPE`:
- `ollama` — Ollama native API (default)
- `openai` — OpenAI-compatible API (llama-server, LM Studio, etc.)

All LLM calls go through a single, persistent HTTP connection pool (`LLM_CONNECTION_POOL_SIZE`, default 20) rather than opening a fresh connection per call — matters most if your LLM backend runs on separate hardware from Mnemolis itself, where each fresh connection pays a real, avoidable network round-trip on top of inference time.

On the Ollama-native backend, every call also sends `keep_alive` (`LLM_KEEP_ALIVE`, default `5m`, matching Ollama's own server-side default) so Mnemolis has its own explicit say in how long the model stays resident in VRAM, rather than depending entirely on whatever else might be sharing the same Ollama instance. Set this higher (`30m`, `3h`, or `-1` for never-unload) if your LLM backend is dedicated to Mnemolis or otherwise idle between requests for longer than 5 minutes. Not sent on the OpenAI-compatible path — Ollama's own OpenAI-compatible endpoint is confirmed to ignore it, and other OpenAI-compatible backends (llama-server, LM Studio) have no equivalent.

The book list is built dynamically from your Kiwix catalog at startup — see [Kiwix Catalog & Article Fetching](https://github.com/immortalbob/Mnemolis/wiki/Kiwix-Catalog-and-Article-Fetching) for the actual discovery mechanism. To force a refresh after adding ZIMs:

```bash
curl -X POST http://your-host:8888/catalog/refresh
```

If `LLM_URL` is left blank, Mnemolis falls back to keyword-based routing and Wikipedia for all Kiwix queries.

### Timezone configuration
Set `TZ` in `docker-compose.yml` to your local timezone (e.g. `America/New_York`). Without it, the container defaults to UTC, which causes time-window phrases in `changes` queries ("this morning," "while at work") to be calculated against the wrong reference time — off by your UTC offset.

```yaml
environment:
  TZ: "America/New_York"
```

This same `TZ` value is also used by `LOCAL_TIMEZONE`, a setting that converts stored UTC timestamps (every database timestamp in Mnemolis is UTC internally) into real local time for any feature that needs to bucket activity by local hour-of-day or day-of-week — set `TZ` once and both get the correct timezone for free. If you specifically want that conversion to use a *different* zone than `TZ`, set `LOCAL_TIMEZONE` explicitly; it always takes priority over `TZ`:

```yaml
environment:
  TZ: "America/New_York"
  LOCAL_TIMEZONE: "America/Los_Angeles"   # only if you want these to differ
```

Most deployments should only ever need to set `TZ`.

**`FORECAST_TIMEZONE`** is a separate, third setting — it tells Open-Meteo what timezone to express forecast times in, and doesn't affect `TZ`/`LOCAL_TIMEZONE` or anything else. Defaults to `UTC` and is independent on purpose: it's a parameter sent to an external API, not something Mnemolis's own internal time logic depends on. Set it to match your actual timezone if you want forecast sunrise/sunset times to read correctly in local time.

### API key authentication (optional)

**By default, Mnemolis has no authentication at all — anyone who can reach it on your network can query it, with no key required.** This matches the trust model of a homelab where Mnemolis sits behind your own firewall and isn't reachable from the open internet. If Mnemolis is ever exposed beyond a fully trusted local network — a VPN with split tunneling, a reverse proxy, a port forward — set `API_KEYS` before doing so, not after.

To require an API key for `POST /search` and `GET /changes`:

```yaml
environment:
  API_KEYS: "your-secret-key-here"
```

Multiple keys are supported, comma-separated:

```yaml
environment:
  API_KEYS: "key-for-open-webui,key-for-claude-desktop"
```

Clients must send the key in the `X-API-Key` header:

```bash
curl -X POST http://your-host:8888/search \
  -H "X-API-Key: your-secret-key-here" \
  -H "Content-Type: application/json" \
  -d '{"query": "what is nitrogen", "source": "kiwix"}'
```

**Setting `API_KEYS` only protects `POST /search` and `GET /changes` — every other endpoint stays unauthenticated regardless of this setting**, including `/health`, `/cache`, `/logs`, `/backup`, and `/areas`. This is intentional, not an oversight: it keeps monitoring tools and discovery requests from being blocked, but it means `API_KEYS` is not a substitute for actual network-level access control if any of that other data (query logs, cache contents, a full backup of Mnemolis's state) would be sensitive in your specific deployment.

### Home Assistant setup
Generate a long-lived access token in Home Assistant:
1. Go to your **Profile** (click your username in the sidebar)
2. Scroll to **Long-lived access tokens**
3. Click **Create Token**, give it a name, copy the token
4. Set `HA_URL` to your HA instance URL (e.g. `http://192.168.1.100:8123`)
5. Set `HA_TOKEN` to the generated token

The `ha` source handles analytical queries that go beyond HA's built-in single-entity intent handling:
- **"house status summary"** — lights, locks, sensors, motion, batteries
- **"indoor air quality"** — CO2, temperature, humidity from indoor sensors
- **"security status"** — locks, doors, recent motion with time-ago
- **"battery status"** — all device battery levels
- **"outdoor conditions"** — weather station sensors
- **"how much power am I using"** — current and historical consumption

The `ha` source also participates in fusion — "house status and what's the weather" automatically fuses `ha` + `forecast`.

## Architecture

### Voice Assistant Flow

```text
ESP32 Voice Assistant
          │
          ▼
   Home Assistant
          │
          ▼
 Mnemolis Intents
          │
          ▼
     Mnemolis
          │
          ├────────────────────┐
          │                    │
          ▼                    ▼
      LLM Backend         Source Providers
          │               ├─ Kiwix
          │               ├─ FreshRSS
          ▼               ├─ SearXNG
   Smart Routing          ├─ Open-Meteo
   ├─ Single source       ├─ Uptime Kuma
   ├─ Auto-fusion         ├─ Home Assistant
   └─ Decomposition  ────►└─ Snapshot Engine (changes)
          │
          ▼
      Response
          │
          ▼
 Home Assistant TTS
          │
          ▼
      ESP32
```

### Multi-Client Architecture

```text
   Open WebUI    Claude Desktop    Cursor    Home Assistant
       │                │             │     (Mnemolis Intents)
    REST API            MCP          MCP          REST API
       │                │             │               │
       └────────────────┴─────────────┴───────────────┘
                                │
                                ▼
                           Mnemolis
                                │
                          Smart Routing
        ┌───────────────┬───────────────┬───────────────┐
        ▼               ▼               ▼               ▼
  Single Source    Auto-Fusion    Decomposition   Conditional
 (keyword or LLM) (multi-keyword  (conjunction      Detection
                       /LLM)         split)      ("if X, Y")
        │               │               │               │
        └───────────────┴───────────────┴───────────────┘
                                │
                           ┌────┴────┐
                           ▼         ▼
                    REST API   MCP/Streamable HTTP
                           │         │
               Home Assistant    Any MCP
             (Mnemolis Intents)   Client
                           │
                    Voice Pipeline
```

### Snapshot Engine

```text
              Background Scheduler (APScheduler)
                            │
        ┌──────────┬────────┬───────────┐
        ▼          ▼        ▼           ▼
     Uptime     Forecast   News         HA
     (2 min)    (30 min)  (60 min)    (5 min)
        │          │        │           │
        └──────────┴────────┴───────────┘
                          │
                          ▼
                  Store snapshot
              (SQLite, JSON for HA)
                          │
              Retain history scaled per source
              (more frequent sources keep more
               rows, so every source covers
               at least a full week)
                          │
                          ▼
              Diff consecutive snapshots
        ┌──────────┬────────┬───────────┐
        ▼          ▼        ▼           ▼
    Outages/    Temp/      New        Lock/door/
    Recovery    Precip   headlines     battery
   (net change) changes               changes
   (configurable thresholds)
        └──────────┴────────┴───────────┘
                          │
                          ▼
              GET /changes?hours=N
           source="changes" (auto-routed)
                          │
                          ▼
                Formatted summary
             "what changed today?"
```

Full mechanics, including why outage/weather changes are collapsed to net change while news/HA events are reported individually: **[Snapshot Engine & Changes](https://github.com/immortalbob/Mnemolis/wiki/Snapshot-Engine-and-Changes)**.

### Source Fusion

```text
   source="auto"                    source="fusion"
        │                                 │
        ▼                                 ▼
 Keyword scan all sources      LLM picks 2-3 sources
 Multiple match? → fuse        (or you specify explicitly)
 Single match? → direct              │
        │                            │
        └────────────┬───────────────┘
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
       Kiwix        HA        Forecast
      FreshRSS   SearXNG      Uptime
    (any combination of available sources,
         queried concurrently)
         │           │           │
         └───────────┴───────────┘
                     │
        Filter empty / failed results
        web/news results scored + ranked
        (app/scoring.py — keyword overlap,
         generic-result penalty, recency)
        Partial failure OK — best effort
                     │
   Merge with [SOURCE — DESCRIPTION] headers
   (descriptive label prevents cross-source
    inference, e.g. weather ≠ news location)
                     │
               Single Response
```

Fusion queries all specified sources concurrently, filters empty or failed results, and merges the remainder with descriptive source attribution headers (e.g. `[FORECAST — WEATHER FORECAST FOR YOUR CONFIGURED HOME LOCATION]`) so the LLM reading the fused response can't mistakenly infer facts across unrelated sections. If only one source returns results, it is returned directly without headers. Full mechanics: **[Fusion](https://github.com/immortalbob/Mnemolis/wiki/Fusion)**.

### Query Decomposition

```text
   source="auto"
        │
        ▼
 Nosplit check
 "compare", "vs", "between", etc.
        │
        ▼
 Try every conjunction type
 "and", "also", "plus", "as well as"
 (not just the first one found —
  keep whichever split produces
  the most genuine sub-intents)
        │
        ▼
 Each candidate sub-query must contain
 either a recognized intent word/noun
 OR a colloquial phrase anywhere in it
 ("what's the deal with X",
  "what's up with X", etc.)
        │
   ┌────┴────┐
   ▼         ▼
Single    Multiple
intent    intents
   │         │
   │    ┌────┴────────────┐
   │    ▼                 ▼
   │  Sub-query 1    Sub-query 2
   │    │                 │
   │  Route            Route
   │  independently    independently
   │    │                 │
   │    └────────┬────────┘
   │             │
   │    Same source? → Merge headers
   │    Different?  → Keep separate
   │    Resolved to internal fusion?
   │      → Pass through unwrapped
   │        (already self-headered)
   │             │
   └─────────────┤
                 ▼
          Single Response
     with [SOURCE — DESCRIPTION] attribution
```

Decomposition only applies to `source="auto"`. It handles casual, colloquial phrasing the same as formal phrasing, protects bare proper-noun pairs ("Iran and Israel") from being mistaken for two separate intents, and biases discourse-framing queries ("everyone keeps talking about X") toward including Kiwix rather than letting them route past it entirely. Full mechanics, including the real bugs found and fixed in this logic: **[Query Decomposition](https://github.com/immortalbob/Mnemolis/wiki/Query-Decomposition)** and **[The Proper-Noun-Pair Saga](https://github.com/immortalbob/Mnemolis/wiki/The-Proper-Noun-Pair-Saga)**.

### Conditional Query Detection

```text
   "if X, Y" / "should X, Y" / "in case X, Y"
                    │
                    ▼
   Leading-comma pattern match
   (deliberately narrow — see below)
                    │
            ┌───────┴───────┐
            ▼               ▼
        No match          Match
            │               │
    Route normally    Extract condition, consequence,
    (decomposition,        and any remainder text
     conditional check          │
     re-applied to        Search ONLY the condition
     each sub-query)            │
            │           Source a structured,
            │           binary signal?
            │         (ha / uptime / forecast-rain)
            │               │
            │       ┌───────┴───────┐
            │       ▼               ▼
            │      No              Yes
            │       │               │
            │  Present real    State explicit verdict
            │  result, note    ("It IS/IS NOT the
            │  it's conditional    case that X...")
            │       │               │
            │       └───────┬───────┘
            │               ▼
            │      Remainder present?
            │       (real intent that
            │        followed the
            │        conditional)
            │               │
            │       ┌───────┴───────┐
            │       ▼               ▼
            │      No              Yes
            │       │               │
            │   Return         Search remainder
            │   framed         independently,
            │   response       merge into response
            └───────┬───────────────┘
                    ▼
              Final Response
```

Detection is deliberately narrow — only a leading `"if X, Y"` / `"should X, Y"` / `"in case X, Y"` form with an explicit comma is recognized, since "if" is genuinely ambiguous in English and this form sidesteps that ambiguity entirely. Mnemolis has no reminder or trigger capability, so the response is framed honestly around the condition's real answer rather than pretending to act on the consequence — a genuine verdict for structured sources (HA locks, uptime, forecast rain), an honest "you'll need to judge" for everything else. Full design rationale and the real recursion bug found while building this: **[Conditional Query Detection](https://github.com/immortalbob/Mnemolis/wiki/Conditional-Query-Detection)** and **[The Recursion Design Bug](https://github.com/immortalbob/Mnemolis/wiki/The-Recursion-Design-Bug)**.

### Kiwix Internal Flow

```text
              query
                │
                ▼
       LLM picks 1-2 books
     (or Wikipedia-first fallback
        if LLM not configured)
                │
                ▼
   Definitional query? Single word?
   Wikipedia selected? LLM configured?
                │
        ┌───────┴───────┐
        ▼               ▼
       No              Yes
        │               │
        │      Ask LLM for 2-3 candidate
        │      disambiguation terms
        │      (broad field / specific
        │       synonym / bare word)
        │               │
        └───────┬───────┘
                ▼
   Search each book × each candidate
   term, merge results, dedupe by URL
                │
                ▼
        Score every result
     against the ORIGINAL query
   (exact match, stemmed title/excerpt
    overlap, Wikipedia bonus, list penalty)
                │
                ▼
   2+ books scoring within 50% of
   the leading book's top result?
                │
        ┌───────┴───────┐
        ▼               ▼
       No              Yes
        │               │
  Return single    Fuse best result per
  best-scoring     book — multi-book
  article           fusion response
```

This is the layer that fixed the "galaxy returns Samsung phones, battery returns military fortifications" problem — rather than trusting one LLM guess about which search term will work, Mnemolis tries several candidates and verifies against real Kiwix results, scored the same way regardless of which term found them. Full mechanics and exact scoring weights: **[Kiwix Catalog & Article Fetching](https://github.com/immortalbob/Mnemolis/wiki/Kiwix-Catalog-and-Article-Fetching)**, **[Kiwix Disambiguation](https://github.com/immortalbob/Mnemolis/wiki/Kiwix-Disambiguation)**, and **[Kiwix Scoring](https://github.com/immortalbob/Mnemolis/wiki/Kiwix-Scoring)**.

## REST API

### `POST /search`

Single source:

```json
{
  "query": "what is molybdenum",
  "source": "auto"
}
```

Fusion — LLM picks sources automatically:

```json
{
  "query": "what is happening with the space program lately",
  "source": "fusion"
}
```

Fusion — explicit source list:

```json
{
  "query": "what is happening with the space program lately",
  "source": "fusion",
  "fusion_sources": ["kiwix", "web", "news"]
}
```

With an explanation chain — the ordered trace of what routing actually did (intent resolution, cache hits, timed source invocations, fallbacks, decomposition, conditionals, semantic matches). See the [Explanation Chains](https://github.com/immortalbob/Mnemolis/wiki/Explanation-Chains) wiki page for the full event vocabulary:

```json
{
  "query": "what is molybdenum",
  "source": "auto",
  "explain": true
}
```

Response:

```json
{
  "query": "what is molybdenum",
  "source_used": "kiwix",
  "result": "# Molybdenum\nSource: wikipedia_en_all_maxi_2026-02\n\n...",
  "success": true,
  "cached": false,
  "error": null,
  "explanation": null
}
```

(`explanation` carries the event list only when the request set `"explain": true`; otherwise it's `null`.)

### `GET /sources`
Returns the list of available sources.

### `GET /health`
Returns status, number of Kiwix books loaded, result and routing cache entry counts alongside their configured max sizes (so growth toward either bound is visible without digging through logs or code), background snapshot job health (each job's status compared against its expected interval — `ok`, `stale`, `never_ran`, or `unknown`, since every snapshot job already catches its own exceptions and silently logs a warning rather than surfacing failure anywhere externally visible), and connectivity status for every configured source — these are real, live network checks against each dependency, not just a check that a config value is present.

### `GET /catalog`
Lists all books currently loaded from the Kiwix OPDS catalog.

### `POST /catalog/refresh`
Forces a re-scan of the Kiwix catalog without restarting the container.

### `GET /cache`
Shows all current result cache entries with age and remaining TTL.

### `POST /cache/clear`
Clears all result cache entries from memory and disk.

### `GET /cache/routing`
Shows all current routing cache entries — source and Kiwix book selection decisions cached to avoid redundant LLM calls. Bounded at `ROUTING_CACHE_MAX_SIZE` (default 1000), evicting the oldest entry once full.

### `POST /cache/routing/clear`
Clears all routing cache entries from memory and disk.

### `GET /cache/semantic`
Shows the [semantic routing cache](https://github.com/immortalbob/Mnemolis/wiki/Semantic-Routing-Cache)'s state — whether it's enabled (`EMBEDDING_MODEL` configured), how many query embeddings are stored, which model produced them, and the active similarity threshold. Disabled by default; requires an embedding model (e.g. `ollama pull nomic-embed-text`).

### `POST /cache/semantic/clear`
Clears the semantic routing cache (in-memory only — its embeddings deliberately don't persist; they rebuild through use).

### `GET /backup`
Downloads a tarball of all six Mnemolis data files — result cache, routing cache, query log, snapshot history, adversarial self-testing history, and temporal pattern detection history. See [Backup & Restore](#backup--restore) below.

### `GET /backup/info`
Shows file sizes and last-modified times for each data file without creating a backup.

### `GET /areas`
Lists all detected Home Assistant areas with entity counts and matching natural-language aliases.

### `GET /changes`
Returns meaningful changes detected across snapshot sources within the last N hours. Optional `?hours=N` parameter (default 24). Detects service outages and recoveries, forecast temperature shifts above `FORECAST_TEMP_CHANGE_THRESHOLD` (default 5°), precipitation changes, and new news headlines.

### `POST /snapshots/trigger`
Manually trigger all snapshot jobs immediately.

### `GET /logs`
Returns recent query log entries — timestamp, query, source requested, source used, cached flag, success, latency in milliseconds, and whether a `FALLBACK_CHAIN` fallback occurred (e.g. a `kiwix` request that resolved to `web`). Optional `?limit=N` parameter (default 50, clamped to 1-1000).

### `POST /logs/clear`
Clears all query log entries.

### `GET /logs/stats`
Returns query log statistics — Time To First Knowledge (TTFK), cache hit rate, success rate, fallback count and rate, average latency by source, top 10 most-asked queries, unique query count, and learned query count. Fallback stats are reported per fallback *target* rather than per original source, since multiple sources can share one target — see [Health & Observability](https://github.com/immortalbob/Mnemolis/wiki/Health-and-Observability) for the field-by-field detail.

## Caching

Results are cached in memory and persisted to disk, with per-source TTLs:

| Source | TTL |
|--------|-----|
| `kiwix` | 24 hours |
| `web` | 1 hour |
| `fusion` | 30 minutes |
| `forecast` | 30 minutes |
| `news` | 15 minutes |
| `uptime` | 1 minute |
| `ha` | 30 seconds |

Routing decisions are cached separately for 1 hour. Both caches are size-bounded (`CACHE_MAX_SIZE`, `ROUTING_CACHE_MAX_SIZE`) with oldest-entry eviction. Full mechanics, including why the two caches exist separately and a real gap found and fixed in routing cache bounding, are in the wiki: **[Caching](https://github.com/immortalbob/Mnemolis/wiki/Caching)**.

## Kiwix ZIM files

Download ZIM files from [library.kiwix.org](https://library.kiwix.org) and place them in `./data/kiwix/`. The example compose mounts this directory into the Kiwix container automatically.

Popular ZIMs for a homelab stack:
- `wikipedia_en_all_maxi` — full English Wikipedia
- `unix.stackexchange.com_en_all` — Unix & Linux Stack Exchange
- `raspberrypi.stackexchange.com_en_all` — Raspberry Pi Stack Exchange
- `ifixit_en_all` — iFixit repair guides
- `freecodecamp_en_all` — FreeCodeCamp
- `devdocs_en_python` — Python DevDocs

### Multi-book fusion

When a query genuinely spans multiple books — "python raspberry pi gpio setup" touching both Python and Raspberry Pi Stack Exchange — Mnemolis merges results from more than one book instead of returning only the single highest-scoring article. Raise `KIWIX_MAX_BOOKS` (default 2) for broader multi-book fusion. Full mechanics: **[Multi-Book Fusion](https://github.com/immortalbob/Mnemolis/wiki/Multi-Book-Fusion)**.

## Confidence-aware fusion (web & news)

Web (SearXNG) and news (FreshRSS) results are scored against the query and filtered before being returned — not trusted at face value just because they came back. For web search specifically, longer queries also get a second, differently-phrased search merged in. Full scoring weights and the query-expansion mechanism: **[Confidence-Aware Fusion](https://github.com/immortalbob/Mnemolis/wiki/Confidence-Aware-Fusion)** and **[Query Expansion](https://github.com/immortalbob/Mnemolis/wiki/Query-Expansion)**.

## Adding a New Source

1. Create `app/sources/your_source.py` with a `search(query: str) -> str` function
2. Add any config vars to `app/config.py` and `docker-compose.yml`
3. Import and register it in `app/router.py` — add to `SOURCE_MAP`, `INTENT_MAP`, `SOURCE_DESCRIPTIONS`, and `CACHE_TTL`
4. Optionally add an entry to `FALLBACK_CHAIN` if your source should fall back to another (e.g. `kiwix` falls back to `web`) when it returns nothing useful — this is tracked and surfaced in `/health` and `/logs/stats`, so a source with a real, well-matched fallback target gets the same visibility as the built-in ones
5. Rebuild: `docker compose up -d --build`

Why registration is explicit rather than auto-discovered, and what a new source does/doesn't inherit automatically: **[Adding a New Source](https://github.com/immortalbob/Mnemolis/wiki/Adding-a-New-Source)**.

The new source is automatically available via both REST and MCP — and immediately fusable with any other source.

## Backup & Restore

All Mnemolis state — result cache, routing cache, query log, snapshot history, adversarial self-testing history, and temporal pattern detection history — lives in six files under `/app/data`, backed by the `mnemolis_data` Docker volume (see the volume naming note below for how Docker Compose actually names it).

### Backing up

```bash
curl -o mnemolis-backup.tar.gz http://your-host:8888/backup
```

Check what would be included first:

```bash
curl -s http://your-host:8888/backup/info | python3 -m json.tool
```

Automate it with cron:

```bash
0 3 * * * curl -s -o /path/to/backups/mnemolis-$(date +\%Y\%m\%d).tar.gz http://your-host:8888/backup
```

### A note on volume naming

Docker Compose automatically prefixes named volumes with your **project name** — the folder `docker-compose.yml` lives in, by default. A volume named `mnemolis_data` in the YAML doesn't necessarily get created with that exact name; check first:

```bash
docker volume ls | grep data
# or, for a running container:
docker inspect mnemolis --format '{{json .Mounts}}' | python3 -m json.tool
```

Use the exact name Docker reports in any manual `docker run -v` command. Set `COMPOSE_PROJECT_NAME` in a `.env` file for a stable, predictable prefix regardless of folder name:

```bash
echo "COMPOSE_PROJECT_NAME=mnemolis" > .env
```

### Restoring

```bash
# Stop the container
docker compose down

# Extract the backup into the data volume
docker run --rm -v mnemolis_data:/app/data -v $(pwd):/backup alpine \
  sh -c "cd /app/data && tar xzf /backup/mnemolis-backup.tar.gz"

# Restart
docker compose up -d
```

### What's NOT in the backup

Kiwix ZIM files, your `docker-compose.yml` configuration, and `searxng/settings.yml` are not included — back those up separately as part of your normal homelab backup routine. The `/backup` endpoint only covers Mnemolis's own state: caches, logs, and snapshot history.

## Running Tests

```bash
docker exec mnemolis python3 -m pytest /app/tests/ -v
```

For load testing:

```bash
pip install locust
locust -f tests/locustfile.py --host http://your-host:8888
```

See `BENCHMARKS.md` for documented results.

1451 tests across every source module, the routing/decomposition/conditional-detection pipeline, caching, adversarial self-testing, cross-source temporal pattern detection, timezone conversion, and the FastAPI/MCP endpoints — see the test file list under [Project Structure](#project-structure) below for what each file actually covers, or the [Contributing](https://github.com/immortalbob/Mnemolis/wiki/Contributing) page for what a good test for this project looks like.

## Project Structure

```
Mnemolis/
├── Dockerfile
├── docker-compose.yml              # your config (not committed)
├── docker-compose.example.yml      # full stack example
├── requirements.txt
├── pytest.ini
├── CHANGELOG.md
├── CHANGELOG-ARCHIVE.md            # v1.0.0–v3.44.1, split out once the live file grew too large to navigate
├── BENCHMARKS.md
├── mnemolis_tool.py                # Open WebUI bridge tool
├── README.md
├── searxng/
│   └── settings.yml               # SearXNG config with JSON enabled
├── tests/
│   ├── conftest.py                 # autouse fixture isolating router.py's shared in-memory caches between tests
│   ├── test_router.py              # intent detection, cache, decomposition, conditional detection, time-window resolution, read-only query_log.db access
│   ├── test_routing_cache.py       # routing cache logic and corruption handling
│   ├── test_cache_persistence.py   # cache eviction, disk persistence, .corrupt recovery
│   ├── test_config.py              # settings defaults and env isolation
│   ├── test_timeutil.py            # UTC-to-local-time conversion, DST handling, timezone setting resolution
│   ├── test_kiwix.py               # scoring, stemming, search term cleaning, discourse-framing phrase stripping (pure logic)
│   ├── test_kiwix_network.py       # catalog parsing, book selection, disambiguation, multi-book fusion
│   ├── test_freshrss.py            # general query detection, recency bonus
│   ├── test_freshrss_network.py    # FreshRSS network calls via mocking
│   ├── test_forecast.py            # forecast parsing, location attribution, configurable thresholds
│   ├── test_searxng.py             # SearXNG search, query expansion, scoring integration
│   ├── test_scoring.py             # shared web/news relevance scoring, generic-result penalty, URL normalization
│   ├── test_query_expansion.py     # alternate phrasing generation and sanity checks
│   ├── test_uptime_kuma.py         # Uptime Kuma status parsing via mocking
│   ├── test_fusion.py              # fusion merging, truncation, deduplication, header formatting
│   ├── test_home_assistant.py      # HA entity filtering, area detection, the core matching engine
│   ├── test_main.py                # FastAPI endpoint tests, API key auth, catalog/areas endpoints
│   ├── test_llm.py                 # Ollama/OpenAI-compatible LLM client behavior
│   ├── test_mcp_server.py          # MCP tool schema and call dispatch
│   ├── test_snapshots.py           # snapshot diff engines, net-change collapsing, background job health
│   ├── test_snapshot_jobs.py       # scheduled snapshot job functions
│   ├── test_adversarial_testing.py # combinatorial query generation, structural anomaly checks, flagged-combination review
│   ├── test_temporal_patterns.py   # structured event extraction, non-overlapping occurrence counting, Bonferroni-corrected mining, out-of-sample validation
│   ├── test_security.py            # SQL injection, path traversal, fuzz, concurrency
│   ├── test_property.py            # Hypothesis property-based fuzz testing
│   └── locustfile.py               # Locust load testing suite
└── app/
    ├── main.py                     # FastAPI app + MCP mount + cache/catalog/areas endpoints + API key auth
    ├── snapshots.py                # Snapshot engine — scheduler, diff logic, change detection, background job health reporting
    ├── adversarial_testing.py      # Adversarial self-testing — combinatorial query generation, structural anomaly detection
    ├── temporal_patterns.py        # Cross-source temporal pattern detection — event extraction, Bonferroni-corrected mining, out-of-sample validation
    ├── timeutil.py                 # UTC-to-local-time conversion, shared groundwork for time-of-day-aware features
    ├── mcp_server.py               # MCP server (Streamable HTTP transport)
    ├── router.py                   # Intent detection, source routing, decomposition, conditional detection, caching, read-only query_log.db access
    ├── llm.py                      # LLM client — Ollama native and OpenAI-compatible
    ├── scoring.py                  # Shared relevance scoring for web/news — keyword overlap, generic-result penalty
    ├── query_expansion.py          # Alternate query phrasing for web search multi-query expansion
    ├── config.py                   # Settings via environment variables
    └── sources/
        ├── kiwix.py                # Offline knowledge base — catalog, disambiguation, multi-book fusion, discourse-framing phrase stripping
        ├── forecast.py             # Open-Meteo weather forecast
        ├── freshrss.py             # FreshRSS RSS reader with confidence-aware scoring
        ├── searxng.py              # SearXNG web search with multi-query expansion
        ├── uptime_kuma.py          # Uptime Kuma service monitoring
        ├── home_assistant.py       # Home Assistant entity state summaries, area awareness
        └── fusion.py               # Multi-source concurrent fusion with descriptive headers
```

## Philosophy

Local-first, privacy-preserving, subscription-free. Mnemolis is designed for homelabs where the data stays home. Open-Meteo is the only external network call — every other source (Kiwix, FreshRSS, SearXNG, Uptime Kuma, Home Assistant) runs on your own infrastructure. More on what this project is and isn't: **[About Mnemolis](https://github.com/immortalbob/Mnemolis/wiki/About)**.

## Contributing

PRs welcome. New source modules are the easiest contribution — drop a file in `sources/`, register it in the router, done. The new source is immediately available via REST, MCP, and fusion with no additional work.

### Proposed modules

Looking for contributors interested in building out additional sources:

- **Jellyfin** — search local media library by title, genre, or actor
- **Paperless-ngx** — search scanned documents and OCR'd content
- **Mealie** — search self-hosted recipe library
- **Grocy** — query pantry inventory, shopping list, or expiring items
- **Calibre** — search local ebook library
- **Navidrome** — search self-hosted music library by artist, album, or track
- **Immich** — search local photo library by date, album, or description

Each source only needs a single `search(query: str) -> str` function. See any existing file in `app/sources/` as a reference.

## Part of the Mnemo-net stack

- [Mnemolis Intents](https://github.com/immortalbob/mnemolis_intents) — native Home Assistant LLM integration for Mnemolis
- [Mnemovox-T7S3](https://github.com/immortalbob/Mnemovox-T7S3) — ESP32-S3 voice satellite with CO2, temperature, and humidity sensing

## License

MIT — see [LICENSE](LICENSE)
