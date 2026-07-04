# Benchmarks

What the real performance numbers show, as of the most recent full benchmark run (v3.50.9), with the v3.55.0 answer-synthesis bucket added in its own section below. Full raw tables for every benchmarked release live in `BENCHMARKS.md` in the repo root. For the chronological story behind these numbers — what was found, what got tried, and a real mistake one re-benchmark caught — see [The Benchmark Investigation Log](The-Benchmark-Investigation-Log) instead; this page is current-state reference, not history.

## The one number that's stayed constant since v3.5.0

**Aggregated median latency: ~24ms.** Every feature and fix added since then — confidence-aware fusion, Kiwix disambiguation, multi-book fusion, conditional query detection, the discourse-framing fix, an entire battle-testing campaign, a full bulletproofing pass, Adversarial Self-Testing, Cross-Source Temporal Pattern Detection, the latency-parallelization work, and the connection/caching fixes covered in the investigation log — has added real cost only to the *specific query shapes* that trigger it, never to the steady-state majority of traffic. A plain `"what is nitrogen"` query costs the same today as it did roughly 35 releases ago.

This is by design, not luck: every conditionally-triggered feature (disambiguation only fires for genuinely ambiguous single-word terms, conditional detection only fires for leading `"if X, Y"` phrasing) and every correctness fix in this project's history (word-boundary matching instead of substring search, a capped retry loop instead of an unbounded one) changed *what gets computed*, not *how much gets computed on every request* — confirmed directly across many benchmark runs, not just assumed.

Answer synthesis (v3.55.0) is the strongest case of this yet: its cost is not just conditionally triggered but fully **opt-in per request** — a client that doesn't set `synthesize=true` pays nothing at all. The v3.55.0 run's aggregated median held at **24ms cold / 23ms warm**, unchanged from v3.50.9, confirming directly that adding a whole second-LLM-call feature moved the steady-state majority of traffic by nothing. Its cost is measured honestly in its own bucket, below.

## Cold cache vs. warm cache — the real shape of the cost

The expensive part of almost every feature here is a **first-time** LLM call: picking which Kiwix book to search, generating disambiguation candidates, choosing a routing decision for an ambiguous phrase. Once that decision is cached (see [Caching](Caching)), every subsequent identical query skips it entirely. Representative measured improvements from the v3.50.9 run:

| Query type | Cold (p98) | Warm (p98) | Improvement |
|------------|-----------|-----------|-------------|
| Web search | 2500ms | 36ms | ~70x |
| Kiwix disambiguation | 2900ms | 35ms | ~83x |
| Discourse-framing | 3100ms | 54ms | ~57x |
| `uptime` | 190ms | 69ms | ~3x |

This is the single most important pattern in every benchmark this project has run: a feature's cold-path tail latency can look alarming in isolation, but if it collapses this dramatically on cache hit, the real-world cost is "pays once per unique query, ever, within the cache TTL" — not "pays this every time." `uptime`'s own cold/warm numbers are both already low (190ms/69ms) since there's no LLM call involved at all; its own history of getting there is its own story, covered in the investigation log.

A few query types — `auto`, `conditional`, and `conditional_remainder` — have a real, known additional cost under synthetic concurrent load specifically: 20 simulated users picking from a finite query pool can occasionally collide on the same not-yet-cached query, each paying the cold-routing cost concurrently rather than one paying it and the rest hitting cache. This doesn't happen in real single-household usage (you're not asking about the same back-door lock from 20 places at once), and it's an active, ongoing area of tuning in `tests/locustfile.py` rather than a Mnemolis correctness issue — see the investigation log for the current state of that tuning.

## Answer synthesis (v3.55.0) — the first feature that adds a *second* LLM call

[Grounded answer synthesis](Answer-Synthesis) is the first feature in this project whose cost is a full second LLM *generation* on top of routing. Every other LLM cost here is a routing-class pick — which book, which source, which decision — a short, near-single-token call. Synthesis composes a multi-sentence answer, so its bucket (`[synthesize]`, deliberately kept separate from `[auto]`) is the single most expensive bucket by average on a cold run. That is expected and correct; the point of the separate bucket is to show that cost in the open rather than let it drag the steady-state median.

The cold/warm split is the same story as every other feature on this page, just with a larger cold number that collapses just as hard on cache hit:

| Query type | Cold (avg) | Cold (p90) | Cold (p98) | Warm (p98) | Improvement |
|------------|-----------|-----------|-----------|-----------|-------------|
| Answer synthesis | 609ms | 2700ms | 3500ms | 39ms | ~90x |

Two things drive that cold tail specifically, both confirmed against the v3.55.0 run rather than assumed:

- **Material length.** A synthesis prompt carries the retrieved material, so a fat topic (a transistor or relativity article) is far more input than a one-line forecast, and generation scales with it. The `SYNTHESIS_TOPICS` pool deliberately includes such topics so the bucket measures the real spread, not a best case.
- **Contention — the real one.** Under concurrent load the single LLM backend is serving routing picks *and* synthesis generations at once. An isolated, uncontended synthesis of a short article measured ~640ms by hand; the same class of call under 20-user load reaches ~2.7s at p90. For the voice pipeline this narrows the latency concern to a specific slice — a *novel, complex* question asked *while the backend is otherwise busy* — rather than voice traffic generally, most of which is short and much of which repeats.

The warm p98 of 39ms is not warm *generation* — it is the synthesis cache doing its job. A synthesized answer caches under a source-led key (`{source}:synth:{style}:{query}`) and inherits that source's TTL, so a repeated question is served from cache at result-cache-hit speed and never re-generates within the TTL. On the warm run, the finite `SYNTHESIS_TOPICS` pool is fully cached from the cold pass, so every warm request is a hit — which is exactly the real-world shape for voice: common questions pay the generation cost once, novel ones pay it once each, and nothing pays it twice. The one deployment lever that matters most here is unrelated to any of the above and covered on the [Answer Synthesis](Answer-Synthesis) page: keeping the model resident (`LLM_KEEP_ALIVE`) so a cold *model load* — a separate ~6s one-time cost, an order of magnitude larger than a warm generation — never lands on a spoken reply.

## Hardware context

These are homelab numbers, not a controlled cloud benchmark — they'll vary with your own LLM hardware (faster GPU genuinely means lower cold routing latency), network latency to HA/Uptime Kuma/your other sources, Kiwix ZIM file size and disk I/O speed, and how warm the routing cache happens to be at the moment you run them. Treat the *relative* patterns here (cold vs. warm, which features cost what) as the generalizable part, and the absolute millisecond figures as specific to one particular homelab's hardware (MiniDock: Intel N100, 16GB RAM; Ollama on a separate host: i9-14900KF, RTX 4090).

## Running your own

Before a genuine cold-cache run, clear both caches explicitly — skipping this step produces an artificially clean result instead of real cold numbers:

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

Replace `192.168.1.50` with your actual Mnemolis host's real IP or hostname — not a placeholder. `--host` silently accepts anything that looks like a URL, so a leftover example value doesn't fail loudly; it fails much later as a wall of opaque DNS errors (`Temporary failure in name resolution`) on every single request, which doesn't obviously point back to `--host` as the cause.

Or headless, for a repeatable cold/warm comparison — run the identical command twice, with no clearing in between for the second (warm) pass:

```bash
locust -f tests/locustfile.py --host http://192.168.1.50:8888 \
  --headless --users 20 --spawn-rate 2 --run-time 120s \
  --csv benchmarks
```

If you add a new feature with its own conditionally-triggered cost (the way disambiguation, conditional detection, and the discourse-framing fix all did), it's worth checking whether `locustfile.py` actually has a task type that exercises it — a benchmark can only measure what it's actually pointed at, and more than one feature in this project's history shipped before its real load-time cost had ever been measured at all, simply because nothing in the load test was constructed to trigger it.
