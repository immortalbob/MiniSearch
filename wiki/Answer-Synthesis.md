# Answer Synthesis

*Added v3.55.0. Opt-in, off by default. Design Doc 4.*

Mnemolis retrieves; historically it never *answered*. Every response is source text — a truncated Kiwix article, a scored web-result list, a fused blob with `[SOURCE — DESCRIPTION]` headers — and the job of turning that into an answer was pushed onto whichever client asked. That works fine for Open WebUI and Claude Desktop, which have their own LLM in the loop. It works badly for the deployment that matters most: the **voice pipeline**. When a satellite asks *"what's happening with the space program lately,"* Home Assistant's TTS ends up reading fused headers and three headline excerpts out loud, while the strongest hardware in the stack (the Beast's RTX 4090 running qwen3:8b) sits one hop away doing nothing but routing.

Answer synthesis closes that gap. With `synthesize=true` on `/search` (or the MCP `search` tool), the local LLM composes a short, grounded answer **from the retrieved material only**, returned in a separate `answer` field **alongside** the raw result — never instead of it.

## The five constraints

Everything below follows from five constraints, in priority order. They're worth reading before the mechanics, because every design choice traces back to one of them.

1. **Additive, never substitutive.** The `result` field always carries the raw retrieved text, byte-identical to before, whether or not synthesis ran. Synthesis only ever populates the new `answer` field (`null` otherwise). Any failure — timeout, empty reply, gate rejection, LLM unconfigured — yields `answer: null` plus a `synthesis_skipped`/`synthesis_rejected` [explanation event](Explanation-Chains), and the caller has exactly what they'd have had today. This is the semantic cache's "constraint #1" posture applied to a new pipeline stage.
2. **Grounded or silent.** The model answers *only* from the material. When the material doesn't contain an answer, the correct output is the honest miss — `"The retrieved sources don't answer this."` — surfaced as a real `answer`, because "the sources don't say" *is* a grounded answer. Synthesis must never become the place hallucinations enter a system whose entire wiki is about not returning wrong things confidently.
3. **Never on the path of clients that don't want it.** Per-request `synthesize` flag defaulting false, under a `SYNTHESIS_ENABLED` master switch (default false for one release — the standard rollout). No client that doesn't ask pays a single token of latency.
4. **Attribution survives synthesis.** Multi-source answers must say which source claims what; single-source answers carry a one-line attribution tag.
5. **Budgeted for voice.** The primary consumer speaks its output. Length is a first-class request parameter (`answer_style`), enforced by sentence-boundary truncation as a backstop.

## Request / response surface

```jsonc
// request
{ "query": "...", "source": "auto", "synthesize": true, "answer_style": "voice" }

// response — new fields only; everything else is unchanged
{
  "answer": "SpaceX flew its ninth Starship test on Tuesday ... (web, news)",
  "answer_sources": ["web", "news"],
  "synthesized": true
}
```

`answer_style` is one of `voice` (≈≤2 sentences, `SYNTHESIS_VOICE_MAX_CHARS`, for TTS), `brief` (one short paragraph, fixed 800 chars — the default), `detailed` (`SYNTHESIS_MAX_CHARS`), or `digest` (see below). Existing clients that never set `synthesize` see three new keys that are always `null`/`[]`/`false` — the same additive precedent [explanation chains](Explanation-Chains) set with its one always-null field.

### The `digest` style — preserve items, don't fuse them (v3.55.1)

`voice`, `brief`, and `detailed` all ask the model to *answer the question* by combining the retrieved material into a single reply. That's exactly right for a fact question ("what's happening with the space program") — but exactly wrong for a breadth request. Ask "summarize the news" in `voice` and the model faithfully compresses ten headlines into two sentences: it leads with the top story and gestures vaguely at the rest, because a two-sentence budget structurally cannot hold ten items. Nothing is hallucinated — it's the length budget destroying nine-tenths of the point.

`digest` inverts the framing. Its style instruction tells the model to **list each distinct item or key point as its own short line, preserving specific names, numbers, and dates, and not to merge separate items or drop them to save space.** It also carries two larger budgets than the other styles, because preservation is the whole job:

- **Output:** `SYNTHESIS_DIGEST_MAX_CHARS` (3000, vs voice's 400), so many items survive rather than being truncated to a couple.
- **Input:** `SYNTHESIS_DIGEST_INPUT_BUDGET_CHARS` (12000, vs the shared 6000), so a wide result — ten news headlines, a broad fusion — actually reaches the model before per-section apportioning trims it. This is the ceiling on how many items *can* survive at all; a digest can only preserve what it was allowed to see.

Use `digest` for "summarize", "list", or "read me all the …" requests; keep `voice`/`brief`/`detailed` for questions with a single answer. Two caveats worth knowing:

- **The numeric-grounding gate gets *more* load-bearing, not less.** A dense multi-item digest carries many figures, and every one must appear verbatim in the source or the whole answer is rejected. For fidelity that's the point — it's what stops a "summary" from quietly inventing a number — but it does mean a digest that reformats or rounds a figure is more likely to trip `synthesis_rejected: numeric` and fall back to the raw result. Watch that event when soaking a digest deployment.
- **Attribution stays whole-answer, not per-item.** The trailing `(news)` / `(web, news)` covers the digest as a whole; per-item source tags are the same "precision theater" non-goal (§9) that rules out per-sentence citations elsewhere.

For voice specifically, `digest` is reachable only if the caller sets it — the `mnemolis_intents` tool exposes `answer_style` so the conversation agent can pick `digest` for list-shaped questions while defaulting to `voice` for everything else (mnemolis_intents v1.4.1+).

## The pipeline

Synthesis lives in `app/synthesis.py` and runs inside `route_query()` **after** `route_with_source()` returns, in the same `_ROUTE_STATS` context so its events land in the explanation chain for free. It is deliberately **not** inside `route()`/`route_with_source()` — it's a post-processing stage over a completed retrieval, and keeping it out of the recursion-laden core is the same judgment that kept explanation chains out of `route_with_source()`'s signature.

### Pre-flight — cheap rejections before any LLM call

Synthesis skips (→ `answer: null`, event `synthesis_skipped` with a `reason`) when:

- `SYNTHESIS_ENABLED` is false (`reason: disabled`) or `LLM_URL` is unconfigured (`reason: llm_unconfigured`);
- the retrieved result *is* an empty/error shape per `router._looks_empty()` (`reason: empty_result`) — synthesizing an apology over "Nothing found" wastes a call and invites invention;
- the result is shorter than `SYNTHESIS_MIN_INPUT_CHARS` (default 200; `reason: input_too_short`) — a one-line HA state answer like `"Front Door: locked"` is already the ideal voice answer, and rewriting it can only add risk. This rule alone exempts most `ha`/`uptime` traffic, which is correct;
- `source_used == "changes"` (`reason: changes_prose`) — `format_changes()` output is already synthesized prose at any length, so re-synthesizing it is pure invention risk with no retrieval benefit, and voice can read a changes digest directly.

The cache is checked *before* the content pre-flight, so a fresh answer skips the LLM entirely.

### Prompt assembly

The result is split into attributed sections on the fusion/decomposition header pattern — using `fusion.HEADER_PATTERN`, the **exact exported pattern** `fusion._format_header()` writes with, never a re-derived one, so a header-format change can't silently drift the parser away from the producer. (The `_looks_empty()` cross-file-drift bug — two "identical" copies diverging until a real fallback stopped firing — is the cautionary tale this exported constant exists to prevent repeating; a drift test formats a real header and asserts the pattern matches it.) A headerless result is one section attributed to `source_used`. Material is truncated per section, proportionally, to `SYNTHESIS_INPUT_BUDGET_CHARS`; temperature is pinned low (0.1).

### The gates

One `llm.generate()` call — a *real* generation with its own `SYNTHESIS_TIMEOUT_SECONDS` budget, not the routing call's hardcoded 10s — then a stack of gates, each rejecting to `answer: null` with a distinct event, in the order that keeps them correct:

- **Empty / whitespace** reply (also catches a timeout, since `generate()` returns `None` then) → `synthesis_rejected: empty`.
- **`NOT_IN_SOURCES`** → becomes the honest `answer: "The retrieved sources don't answer this."` with `answer_sources: []` — a *success* (constraint #2), recorded as `synthesis_invoked`, and deliberately **not cached** (see below).
- **Attribution parse** → the trailing `(tag, tag)` must parse to a non-empty subset of the section tags offered. Unparseable/missing → for a single-section input, strip it and attribute to `[source_used]`; for a multi-section input, **reject** (`synthesis_rejected: attribution`) — a multi-source answer that can't say who said what doesn't ship.
- **Echo** → the answer body (with attribution stripped) must not be the question restated (`synthesis_rejected: echo`).
- **Numeric grounding** → every number token in the answer (digits, decimals, percents; the four-digit current year alone exempted) must appear in the material after normalizing commas (`1,500` == `1500`). A miss **rejects** the answer and logs the offending token at WARNING — numbers are where confident invention does the most damage (temperatures, versions, scores), and this WARNING is the live-verification hook the v3.53.x–v3.54.x incident pattern shows will find the real bugs.
- **Length** → over-budget answers are truncated at the last sentence boundary under the style cap, never mid-sentence, then recombined with the attribution suffix.

### Caching

Synthesized answers cache in the existing result cache under a key that leads with the source — effectively `{source_used}:synth:{style}:{query}` — so the answer inherits the underlying source's TTL exactly (`CACHE_TTL[source_used]`) and **can never outlive the retrieval it grounds**. Every existing clear path (`/cache/clear`, eviction, reload) keys off the same dict, so invalidation needs nothing new. The honest `NOT_IN_SOURCES` miss is **never cached** — the underlying result may improve within its TTL via fallback variance, and a cached "don't know" is [the Cached Failure Bug found three times](The-Cached-Failure-Bug-Found-Three-Times).

## Observability

Explanation events: `synthesis_invoked` (`elapsed_ms`, `model`, `sections`, `style`), `synthesis_cached`, `synthesis_skipped` (`reason`), `synthesis_rejected` (`gate`). `/logs/stats` gains a `synthesis` block — `{requested, served, not_in_sources, rejected_or_skipped}` — derived from a new nullable `synthesized` status column on the query log (`NULL` = not requested, `1` = served, `2` = honest miss, `0` = requested-but-skipped/rejected), added by a migration-safe guarded `ALTER TABLE`, the established pattern for the log DB.

## Voice integration

`mnemolis_intents` opts in with `{"synthesize": true, "answer_style": "voice"}` and speaks `answer ?? result` — one conditional. The fallback chain is airtight: a gate rejection or timeout means the voice pipeline reads exactly what it reads today. Recommended deployment note: `LLM_KEEP_ALIVE=30m` on a Beast-class dedicated backend so voice synthesis never pays a model load.

## Non-goals (v1)

Streaming token output; multi-turn answer refinement; synthesis of `explain` traces; per-sentence citation markers (paragraph-level attribution is the honest ceiling of a prompt-level contract — claiming finer would be precision theater); and — never — retrieval re-ranking based on the synthesis. No feedback loop from generation into retrieval; that is how grounding dies quietly.

## Related

- [Configuration Reference — Answer synthesis](Configuration-Reference#answer-synthesis)
- [Explanation Chains](Explanation-Chains) — the trace substrate synthesis records into
- [Fusion](Fusion) — where the `[SOURCE — DESCRIPTION]` headers synthesis parses come from
- [The Cached Failure Bug Found Three Times](The-Cached-Failure-Bug-Found-Three-Times) — why the honest miss is never cached
