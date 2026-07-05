# Sources

Every query Mnemolis answers eventually resolves to one or more of these eight sources. Each is a real, independent backend — there's no shared internal "knowledge base," just a router deciding which external service (or combination) is most likely to have the answer.

| Source | Backend | What it actually does |
|--------|---------|------------------------|
| `kiwix` | [Kiwix](https://www.kiwix.org/) | Offline encyclopedic/technical knowledge — Wikipedia, Stack Exchange, iFixit, FreeCodeCamp, DevDocs, whatever ZIMs you've loaded |
| `forecast` | [Open-Meteo](https://open-meteo.com/) | 3-day weather forecast for your configured home coordinates |
| `news` | [FreshRSS](https://freshrss.github.io/FreshRSS/) | Your own RSS feeds, fetched via the GReader API |
| `web` | [SearXNG](https://searxng.github.io/searxng/) | Live web search through your self-hosted SearXNG instance |
| `uptime` | [Uptime Kuma](https://uptime.kuma.pet/) | Status of whatever you're monitoring — reports down services, or confirms everything's up |
| `ha` | [Home Assistant](https://www.home-assistant.io/) | Entity state summaries — lights, locks, sensors, motion, batteries, power |
| `changes` | Snapshot Engine (internal) | "What changed since X" — diffs across the four sources above that get periodically snapshotted |
| `history` | History Engine (internal) | Recorded time series over the house's own sensors/services — highs, lows, averages, counts, trends. Off by default (`HISTORY_ENABLED`) |

Two more values exist but aren't really sources in their own right:

- **`fusion`** — not a backend, a *mode*. Tells the router to query several real sources concurrently and merge the results. See [Fusion](Fusion).
- **`auto`** — also not a backend. Tells the router to figure out which source(s) actually apply. See [Routing](Routing).

---

## `kiwix` — Offline Knowledge

Backed by a Kiwix server holding whatever ZIM files you've loaded — typically full Wikipedia plus a set of Stack Exchange communities relevant to homelab/technical work (Unix, Raspberry Pi, electronics, etc.), iFixit repair guides, FreeCodeCamp, and DevDocs reference material.

This is the most architecturally complex source by far — it dynamically discovers what books exist from your actual Kiwix catalog, has its own LLM-assisted book selection, multi-candidate disambiguation for ambiguous bare words, and multi-book fusion when a question genuinely spans more than one ZIM. See [Kiwix Catalog & Article Fetching](Kiwix-Catalog-and-Article-Fetching), [Kiwix Disambiguation](Kiwix-Disambiguation), [Kiwix Scoring](Kiwix-Scoring), and [Multi-Book Fusion](Multi-Book-Fusion) for the full mechanics.

**Falls back to `web`** if it returns nothing usable — see [Routing](Routing) for exactly what counts as "nothing usable."

## `forecast` — Weather

A thin wrapper around the Open-Meteo API, configured once with your home coordinates (`FORECAST_LATITUDE`, `FORECAST_LONGITUDE`). Returns a 3-day outlook — today, tomorrow, the day after — with high/low temps, wind, and sunrise/sunset, prefixed with your configured location name so a fused response can't be mistaken for weather somewhere else.

This is the only source that makes a network call outside your own infrastructure (Open-Meteo's public API) — see [About Mnemolis](About) for the local-first philosophy this is the one deliberate exception to.

## `news` — Your RSS Feeds

Reads from FreshRSS via its GReader-compatible API. This is genuinely *your* feeds, not a generic news search — if you haven't subscribed to something in FreshRSS, Mnemolis can't surface it here. For headline-style queries ("latest news", "what's happening") it returns your feed unfiltered, newest first — see [The General-News-Query Detection Bugs](The-General-News-Query-Detection-Bugs) for the real history behind recognizing that case correctly. For queries with an actual topic, results are scored against the query the same way `web` results are — see [Confidence-Aware Fusion](Confidence-Aware-Fusion).

As of v3.52.0 the GReader auth token is cached module-level instead of re-fetched per query — every news query previously paid a full ClientLogin round trip (an extra HTTP request plus credential exchange) despite GReader tokens being long-lived by design. Invalidation is self-healing rather than TTL-guessed: an HTTP 401 from the articles request (the real signal the token stopped working — an API password change, a FreshRSS reinstall) triggers exactly one re-auth and retry, so a stale token costs one extra request, never a wrong answer and never a retry loop. The same release gave every HTTP-based source module (`freshrss`, `searxng`, `kiwix`, `home_assistant`, `forecast`) a persistent `requests.Session` — connection reuse instead of a fresh TCP setup per call, the identical fix `llm.py` already applied to its own per-call-connection cost. Together, a warm news query now costs exactly one HTTP request on an already-open connection where it previously cost two fresh TCP connections every single time.

**Falls back to `web`** if scoring rejects every article in your feed as irrelevant to the actual query.

## `web` — Live Search

A SearXNG instance you run yourself, queried via its JSON API. For longer queries (3+ words), Mnemolis also tries one LLM-generated alternate phrasing and merges both result sets — see [Query Expansion](Query-Expansion). Every result is scored for relevance before being returned; SearXNG's own ranking isn't trusted blindly.

If you're seeing `"Error reaching SearXNG: connection failed"`, check [The SearXNG Timeout Lesson](The-SearXNG-Timeout-Lesson) before assuming it's a Mnemolis bug — it usually isn't.

## `uptime` — Service Monitoring

Connects to Uptime Kuma over its Socket.IO interface and reports a simple summary: either "all N services are up" or a list of what's currently down, pending, in maintenance, or — a real, distinct category — has no heartbeat data at all yet (a brand-new monitor, or one whose check interval hasn't fired since Uptime Kuma's own restart).

The Socket.IO connection is persistent — established once and reused across calls for the app's full lifetime, rather than reconnected fresh each time. A dead or stale connection is detected and transparently replaced on the next call. See [Caching](Caching#why-uptimes-connection-is-persistent-even-though-its-cache-ttl-stays-short) for why this is a connection-level design, not a caching one, and [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-1-uptimes-warm-cache-tail-five-releases-to-a-real-root-cause) for the full story behind it.

This is one of three sources with a genuinely binary, structured signal (up/down) — which matters specifically for [Conditional Query Detection](Conditional-Query-Detection)'s yes/no verdicts.

## `ha` — Home Assistant

Reads entity states directly from your Home Assistant instance via its REST API. Unlike the other sources, `ha` answers a category of question HA's own built-in voice assistant doesn't handle well — analytical, multi-entity summaries rather than single-device commands:

- "house status summary" — lights, locks, sensors, motion, batteries, all at once
- "indoor air quality" — CO2, temperature, humidity from indoor sensors specifically
- "security status" — locks, doors, recent motion with relative time ("2 hours ago")
- "battery status" — every device's battery level in one summary
- "outdoor conditions" — weather station sensors, distinct from `forecast`'s predictive data
- "how much power am I using" — current and historical consumption

Full setup details (generating a long-lived access token, the exact config vars) are in [Home Assistant Integration](Home-Assistant-Integration).

`ha`'s lock/unlock and door open/closed states are the other two structured, binary signals conditional detection can give a confident yes/no verdict against.

**If "is the front door locked" or similar specific entity questions ever returned "no matching entities found" even though the entity clearly existed, that's fixed now** — see [Home Assistant Integration](Home-Assistant-Integration#development-notes) for what happened and what to check if anything still looks wrong.

## `changes` — What's Different Since X

Not a live backend at all — it reads from Mnemolis's own snapshot history, captured every 2–60 minutes (interval varies per source) by a background scheduler, and diffs the most recent snapshot against an earlier one to report what actually changed: outages and recoveries, meaningful weather shifts, new headlines, lock/door/battery state changes.

Time-window phrases ("this morning," "while I was at work") resolve to a specific hour window using your configured `MORNING_START_HOUR` and `WORK_START_HOUR`. Full mechanics, including why outage/weather changes are collapsed to net change while news/HA events are reported individually, live in [Snapshot Engine & Changes](Snapshot-Engine-and-Changes).

---

## `history` — Time-Series Memory (opt-in)

Also not a live backend — it reads from Mnemolis's own recorded sample history. Where `changes` answers *"what's different since X,"* `history` answers *"what were the actual values over X"*: highs, lows, averages, counts, and trends over the numeric sensors (temperature, CO2, humidity, power, battery…) and service-uptime counts the snapshot path already fetches every few minutes but previously threw away after diffing.

Off by default (`HISTORY_ENABLED=false`) because it has a real, bounded on-disk cost. The sampler piggybacks on the same Home Assistant fetch `snapshot_ha` already makes — enabling it adds **zero** additional HA load. Event counts (*"how many times did the front door open today"*) read the temporal feature's event table read-only, so they additionally require `TEMPORAL_PATTERN_DETECTION_ENABLED=true`.

Answers are assembled deterministically — no LLM anywhere in the aggregation path — and every answer states its actual data coverage when a question reaches back further than recording has existed. Full mechanics, the retention math, and the trend gates live in [History & Trends](History-and-Trends).

---

## How a query reaches one of these

None of this matters until something decides *which* source(s) a given query should go to. That decision — keyword matching, LLM-assisted selection, and a few real, hard-won exceptions — is covered in full in [Routing](Routing).
