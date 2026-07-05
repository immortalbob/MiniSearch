# Mnemolis Wiki

Mnemolis is a self-hosted knowledge broker for homelabs. Point it at a question the way you'd ask a person — *"is the front door locked," "what's the deal with that whole mercury retrograde thing everyone keeps talking about," "what's the weather and are any of my services down"* — and it figures out which backend actually has the answer, asks it, and hands back a real response instead of a list of links to sort through yourself.

Under the hood, that means routing every query to one or more of: an offline Wikipedia-scale knowledge base (Kiwix), live weather, your own RSS feeds, real web search, your homelab's uptime monitoring, and your Home Assistant entity states — then, when a question genuinely spans more than one of those (*"is the back door locked, and what's the forecast for this weekend"*), querying all the relevant ones at once and merging the results into a single, coherent answer instead of forcing you to ask twice. It understands compound questions, conditional phrasing (*"if any services are down, let me know"*), and the messy, casual way people actually talk — not just clean, single-topic queries. Everything runs on your own infrastructure: Kiwix, FreshRSS, SearXNG, Uptime Kuma, and Home Assistant are all things you already self-host, and the only external network call in the whole system is to Open-Meteo for weather. There's no subscription, no cloud dependency for the data itself, and (optionally) no dependency on any external LLM either — Mnemolis works with keyword-only routing if you never configure one, and gets meaningfully smarter about ambiguous phrasing if you point it at a local model.

It's accessible over a REST API and over MCP (Model Context Protocol), so it drops into Claude Desktop, Open WebUI, or anything else that speaks either interface.

This wiki holds the deep-dive material that doesn't belong in the [README](https://github.com/immortalbob/Mnemolis/blob/main/README.md) — mechanism-level detail, design rationale, and the real bugs found and fixed along the way. The README stays lean: what it is, quick start, core config, API reference. Start there if you just want to get it running. Come here when you want to know *why* it works the way it does, or *what* it would actually take to break it.

---

## Getting Started

- **[About Mnemolis](About)** — what it is, what it isn't, and the philosophy behind it (local-first, privacy-preserving, subscription-free)
- **[First-Time Setup](First-Time-Setup)** — full stack vs. Mnemolis-only install paths, the actual order of operations, and the things that bite people on a first install
- **[Configuration Reference](Configuration-Reference)** — every environment variable, grouped by what it actually controls, with the reasoning behind each default, including optional API key authentication
- **[Home Assistant Integration](Home-Assistant-Integration)** — token setup, the analytical queries the `ha` source handles beyond HA's own built-in intents, and how it participates in fusion
- **[Known Limitations](Known-Limitations)** — a single, honest list of what Mnemolis doesn't do well, written for evaluating fit before diving into mechanism pages

## Core Concepts

The actual path a query takes, roughly in the order it's useful to read them:

- **[Sources](Sources)** — what each backend (`kiwix`, `forecast`, `news`, `web`, `uptime`, `ha`, `changes`, `history`) actually does, and where its data comes from
- **[Conditional Query Detection](Conditional-Query-Detection)** — the `"if X, Y"` feature: checked first, before decomposition, since a leading conditional is structurally one statement, not a flat list of independent intents
- **[Query Decomposition](Query-Decomposition)** — how compound questions get split into independent sub-intents, including the mixed-conjunction and proper-noun-pair handling
- **[Routing](Routing)** — how each resulting piece — a plain query, an extracted condition, or a decomposed sub-query — gets from "what you typed" to "which source(s) answer it": keyword matching, LLM-assisted selection, and the discourse-framing bias
- **[LLM Client](LLM-Client)** — the dual Ollama-native/OpenAI-compatible client that Routing, Kiwix Disambiguation, and Query Expansion all depend on, how it fails safely when nothing's configured, and the real bug that used to silently break it for thinking models
- **[Fusion](Fusion)** — how multiple sources get queried concurrently and merged into one coherent response
- **[Answer Synthesis](Answer-Synthesis)** — the final stage of the path (default on, gated per-request via `synthesize=true`): the local LLM composes a short, grounded answer *from the retrieved material only*, returned in a separate `answer` field alongside the raw `result` — never instead of it. `voice`/`brief`/`detailed`/`digest` styles; additive by contract; grounded-or-silent through a stack of sanity gates (numeric grounding, attribution, honest NOT_IN_SOURCES miss). Built for the voice pipeline, where TTS would otherwise read fused headers aloud
- **[Snapshot Engine & Changes](Snapshot-Engine-and-Changes)** — the background scheduler that captures source state over time, diffs it, and answers "what changed since X"
- **[History & Trends](History-and-Trends)** — the `history` source (off by default): time-series memory over the house's own numeric sensors and service state, answering highs/lows/averages/counts/trends over real recorded samples — piggybacking on the snapshot path's existing HA fetch, with coverage disclosure when a question reaches back further than the data does
- **[Caching](Caching)** — the result cache and routing cache: what's cached, for how long, and how size is bounded
- **[Semantic Routing Cache](Semantic-Routing-Cache)** — the optional third cache layer: embedding-based reuse of routing decisions across *rephrasings* of the same question, so "will it rain later" borrows "will it rain this evening"'s already-made decision instead of paying its own cold LLM call. Opt-in via `EMBEDDING_MODEL`, deliberately conservative about what counts as "the same question," and decisions always come from the live routing cache — this layer stores only vectors
- **[Timezone Conversion](Timezone-Conversion)** — converting stored UTC timestamps into real local time, and why this needed its own dedicated piece rather than reusing `_hours_since()`'s existing logic

One real cross-cutting dependency worth knowing about up front: the discourse-framing phrase list and the stop-word set used in Routing's bias and Query Decomposition's content filtering are both defined once, in `kiwix.py`, not duplicated locally — see [The Discourse-Framing Investigation](The-Discourse-Framing-Investigation#a-real-deliberate-single-source-of-truth) for why. The rest of `kiwix.py` — disambiguation, scoring, multi-book fusion, catalog discovery — is genuinely Kiwix-internal and covered in its own Deep Dive section below.

## Kiwix Deep Dive

- **[Kiwix Catalog & Article Fetching](Kiwix-Catalog-and-Article-Fetching)** — how Mnemolis discovers what books exist, searches one, and turns a winning result into actual article text
- **[Kiwix Disambiguation](Kiwix-Disambiguation)** — the multi-candidate search-and-score approach that replaced trusting a single LLM guess, and why
- **[Kiwix Scoring](Kiwix-Scoring)** — the exact point values behind article selection, spelled out in full
- **[Multi-Book Fusion](Multi-Book-Fusion)** — when and why Mnemolis merges results from more than one Kiwix book instead of picking just one

## Web & News Deep Dive

- **[Confidence-Aware Fusion](Confidence-Aware-Fusion)** — why web and news results get scored at all, and the exact weights behind that scoring
- **[Query Expansion](Query-Expansion)** — the alternate-phrasing mechanism for web search, and why it doesn't apply to news

## Operations

- **[MCP Server](MCP-Server)** — the Model Context Protocol interface, how it differs from the REST API, and connecting Claude Desktop or other MCP clients
- **[Health & Observability](Health-and-Observability)** — what `/health` and `/logs/stats` actually check, including fallback visibility and background job health
- **[Explanation Chains](Explanation-Chains)** — `/search` with `explain=true`: the ordered trace of what routing actually did (intent resolution, cache hits, timed source invocations, fallbacks, decomposition, semantic matches) — recorded at the same code points as the routing itself, so it structurally can't disagree with what ran. Returned on failures too, as the partial trace of everything before the break — and it caught a real Kiwix bug on its first day live
- **[Adversarial Self-Testing](Adversarial-Self-Testing)** — the background job that generates combinatorial edge-case queries from Mnemolis's own real vocabulary and flags structural anomalies for review (opt-in — off by default)
- **[Cross-Source Temporal Pattern Detection](Cross-Source-Temporal-Pattern-Detection)** — the background job that mines `ha`/`uptime` event history for statistically-corrected, out-of-sample-validated timing relationships, with correlation-not-causation framing baked into every result
- **[Troubleshooting](Troubleshooting)** — the real problems found and fixed this project's life, indexed by symptom (start here if something's broken)
- **[Backup & Restore](Backup-and-Restore)** — the seven data files, the Docker volume naming gotcha, and how to actually restore from a backup
- **[Benchmarks](Benchmarks)** — real performance data across every major release, cold cache vs. warm cache
- **[Adding a New Source](Adding-a-New-Source)** — the four files a contributor touches, and the one optional fifth (fallback chains)

## Design History — Real Bugs, Real Fixes

These pages exist because the lessons are genuinely worth keeping, not just because something broke once. Each one is a real investigation — root cause traced, fix verified against production data, sometimes with a second, third, or fourth bug found while verifying the first fix actually worked. Roughly chronological, earliest first:

- **[The SearXNG Timeout Lesson](The-SearXNG-Timeout-Lesson)** — a correctly-edited config file that silently didn't take effect, and how `/health` caught it
- **[The Proper-Noun-Pair Saga](The-Proper-Noun-Pair-Saga)** — five distinct bugs in one piece of logic protecting "Iran and Israel" style phrases from incorrect splitting
- **[The Recursion Design Bug](The-Recursion-Design-Bug)** — how an over-cautious depth counter in conditional detection blocked its own necessary logic, and the simpler design that replaced it
- **[The Discourse-Framing Investigation](The-Discourse-Framing-Investigation)** — why "everyone's obsessed with X" queries routed past Kiwix, and why the real fix needed four separate, sequential discoveries, not the two it looked like at first
- **[The MCP Transport Migration](The-MCP-Transport-Migration)** — a real external community audit that turned into a full transport migration, the upstream session-manager bug found before it shipped, and two more bugs a real client found the moment it actually tried to connect
- **[The Complexity Refactor Campaign](The-Complexity-Refactor-Campaign)** — a static-analysis-driven refactor run (radon, mypy, bandit, vulture, pip-audit) that found real bugs almost entirely by comparing near-duplicate code paths side by side, including a self-introduced bug caught before it ever shipped
- **[The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs)** — everything Adversarial Self-Testing has actually found in Mnemolis itself: a four-bug fusion-merge chain, a false positive with a regex bug underneath it, a real backend timeout, and one investigation that ended without a root cause
- **[The Meaningful-Content-Filter Bugs](The-Meaningful-Content-Filter-Bugs)** — two real bugs in a different piece of decomposition, found by the same real-traffic testing above, both the same shape: a generic filter discarding something a more specific check already knew was meaningful
- **[The General-News-Query Detection Bugs](The-General-News-Query-Detection-Bugs)** — three sequential bugs across three releases in deciding whether a news request is a general headline ask or a specific topic, all the same shape: correct for the grammatically clean phrasing, blind to the casual one
- **[The Fallback Observability Gap](The-Fallback-Observability-Gap)** — two sequential bugs that meant a real, working fallback mechanism couldn't be reliably seen working, from either the API response or the routing layer's own internal logic
- **[The Cached-Failure Bug, Found Three Times](The-Cached-Failure-Bug-Found-Three-Times)** — the same routing-cache bug pattern found in three separate functions, with a real wrinkle on the third occurrence that needed a genuinely different fix
- **[The Home Assistant Bulletproofing Pass](The-Home-Assistant-Bulletproofing-Pass)** — five real bugs found in one source file during a deliberate full read, including a three-bug chain found one fix at a time
- **[The Kiwix Bulletproofing Pass](The-Kiwix-Bulletproofing-Pass)** — five more real bugs found the same way in a different source file, including a non-deterministic book-selection bug found via test coverage audit rather than a code read
- **[The Snapshot Engine Bulletproofing Pass](The-Snapshot-Engine-Bulletproofing-Pass)** — six bugs found in the background snapshot/diff engine, from a per-source retention gap to a malformed entity that could crash an entire diff, including a zero-degree truthiness bug found later by checking an earlier fix for a sibling gap
- **[The Temporal Pattern Detection Development Bugs](The-Temporal-Pattern-Detection-Development-Bugs)** — three bugs caught before this feature ever ran against real data, including one that meant its own headline example couldn't actually fire in the version that first shipped
- **[The Fusion Merge Bugs](The-Fusion-Merge-Bugs)** — three sequential bugs in same-source result merging, plus the `[FUSION — FUSION]` double-header bug and a mixed-speed timeout crash
- **[The Caching Concurrency Investigation](The-Caching-Concurrency-Investigation)** — synthetic test traffic silently polluting real caches, and a separate file-write race underneath both caches, found while researching whether to parallelize query expansion
- **[The Latency Parallelization Investigation](The-Latency-Parallelization-Investigation)** — the single most cross-page-scattered story in the project's history: two structurally similar latency problems, one fixed first, one initially (and wrongly) left alone, and a real regression caught only by going back and checking that reasoning
- **[The Benchmark Investigation Log](The-Benchmark-Investigation-Log)** — seven separate threads traced across many benchmark releases: `uptime`'s tail finally root-caused on the fifth try, a real pool-sizing mistake caught by the very next re-benchmark, an intermittent connection failure that recurred once more before a real candidate fix shipped, and a deterministic single-request version of a known Ollama constraint found via a real user benchmark
- **[The Full Audit Pass](The-Full-Audit-Pass)** — a systematic, function-by-function read of every file in `app/`, smallest to largest (83 lines to 2482 lines) — the first time every function was verified directly rather than inferred from tests passing. Fourteen real bugs found and fixed across 18 files; four of the largest files came back genuinely clean. Benchmark at v3.51.0: zero failures across 1750 requests, warm aggregated p95 63ms
- **[The Answer Synthesis Voice Soak](The-Answer-Synthesis-Voice-Soak)** — how a real satellite deployment shaped answer synthesis after it shipped: the "summarize the news" query that returned one story of ten, the `digest` style it drove, why digest was then the wrong tool for "summarize," and the prose-fluency fix for answers that narrated the feed instead of the news — four findings, none reachable from a test against a mocked LLM

## Reference

- **[Roadmap](Roadmap)** — what's been completed across the project's major phases, and the Fable Capability Extensions currently being built one at a time
- **[Open WebUI System Prompt Guide](Open-WebUI-System-Prompt-Guide)** — the verified-working prompt that stops an LLM from silently narrowing a compound question to one tool call, with real before/after evidence
- **[Contributing](Contributing)** — proposed source modules looking for contributors, and what a good PR looks like

---

*This wiki is a living document. If something here goes stale, that's worth fixing the same way a stale README section was — the project has a real track record of catching exactly that kind of drift.*
