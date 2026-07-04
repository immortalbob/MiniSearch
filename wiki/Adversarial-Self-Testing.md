# Adversarial Self-Testing

A background job, running on the same `apscheduler` infrastructure the [snapshot engine](Snapshot-Engine-and-Changes) already uses, that generates structurally-novel queries by combining Mnemolis's own real ingredient vocabulary, runs each one through the real `route_with_source()` pipeline, and flags structural anomalies for human review. It exists to institutionalize the adversarial megaquery testing approach that found most of the bugs documented in [Design History](Home#design-history--real-bugs-real-fixes) — the [proper-noun-pair saga](The-Proper-Noun-Pair-Saga)'s bug 5, in particular — instead of relying on someone deliberately constructing a nasty test sentence by hand each time.

## The one hard rule

**Nothing in this feature ever judges whether a response was correct.** That's not a stylistic choice — it's the load-bearing design constraint the whole feature depends on.

An LLM-as-judge approach to this exact shape of problem (generate a test input *and* an expected answer, then trust an LLM's own judgment about whether a system's real output matches) was measured in real research at 6.3% precision — 93.7% of flagged "failures" were the judge's own invented expected-answer being wrong, not the system under test. Building this feature around that approach would have meant trading a few hours of setup for a permanent, self-inflicted false-positive problem.

Instead, every check here verifies one of Mnemolis's own *documented, already-stated* behavioral guarantees against what the real pipeline actually did:

- Does a `discourse_framing_plus_real_keyword` query actually keep kiwix in the result, the way [the discourse-framing bias](The-Discourse-Framing-Investigation) is supposed to guarantee?
- Does a query built from N independent intents produce something close to N `[SOURCE — LABEL]` headers, the same signal that originally caught the proper-noun-pair bug?
- Does the response contain a raw traceback, an empty-result phrase from `fusion._looks_empty()`, or a source that doesn't match anything the query actually said?

None of those require knowing whether the *content* of the answer was right. They require knowing whether Mnemolis did the thing it claims to do — a fundamentally more reliable kind of check, and one that needs no LLM call and no ground truth.

## Generation — pure combinatorics, no LLM calls

Every generated query comes from one of seven recipes, each pure Python combining real vocabulary already defined elsewhere in the codebase:

- `router.INTENT_MAP` — the same dict `detect_intent()` uses for keyword routing
- `router._CONJUNCTIONS` / `router._NOSPLIT_PATTERNS` — the same lists [query decomposition](Query-Decomposition) uses
- `kiwix.DISCOURSE_FRAMING_PATTERNS` — the same list behind the [discourse-framing investigation](The-Discourse-Framing-Investigation)
- A small hardcoded seed corpus: real proper-noun pairs, and the real conditional phrases from `tests/locustfile.py`'s `CONDITIONAL_QUERIES`/`CONDITIONAL_WITH_REMAINDER_QUERIES` — reused directly rather than re-typed, so the two test surfaces can never silently drift apart

| Recipe | What it stresses |
|---|---|
| `proper_noun_plus_pronoun_intent` | The exact shape that found proper-noun-pair bug 5 — a real pair immediately followed by a conjunction and the pronoun "I" |
| `multi_intent_chain` | 3–5 independent intents from different sources, joined by different conjunctions |
| `conditional_with_remainder` | A real conditional seed plus a genuinely unrelated remainder intent after it |
| `nosplit_adjacent_to_real_conjunction` | A nosplit phrase ("compare", "versus", etc.) placed next to a *different*, unrelated real conjunction elsewhere in the query |
| `discourse_framing_plus_real_keyword` | A discourse phrase followed by a clean keyword match for a different source |
| `nested_proper_noun_pairs` | Two distinct proper-noun pairs in the same query, testing whether the per-occurrence guard protects both independently |
| `no_intent_fallthrough` | A query with no `INTENT_MAP` keyword at all — does it fall through to Kiwix/LLM routing sanely? |

Each generated query is fingerprinted by the *ingredients* used (not the literal string), and generation biases toward fingerprints never seen before, falling back to a repeat only once a recipe's seed vocabulary is genuinely exhausted — confirmed directly: against a single-recipe, five-topic test vocabulary, all five topics surface as novel within the first five generations before repeats begin.

The one place an LLM call would actually be worth its cost is periodic (weekly-scale, not per-cycle) expansion of the seed lists themselves — `PROPER_NOUN_PAIRS`, `CONDITIONAL_SEEDS`, `_DISCOURSE_TOPICS` — not the generation loop itself. That's a deliberate, not-yet-built follow-up, not part of the hot path.

## What gets flagged

Seven checks run in priority order against every generated query's real result:

1. **Crash** — an exception escaped, or a raw traceback ended up in the response body
2. **Source mismatch** — `source_used` doesn't match any source the query's own keywords actually pointed at (fusion is always allowed, since merging multiple real sources is itself correct behavior)
3. **Part-count mismatch** — a `multi_intent_chain` query's intended intent count is significantly off from its result's `[SOURCE — LABEL]` header count
4. **Discourse framing dropped kiwix** — a `discourse_framing_plus_real_keyword` query's result has neither `source_used == "kiwix"` nor a `[KIWIX — ...]` header
5. **Conditional remainder missing sections** — a `conditional_with_remainder` query's result has zero `[SOURCE — LABEL]` headers at all
6. **Unexpected empty** — the result matches one of `fusion._looks_empty()`'s own canonical empty/error phrases
7. **Latency outlier** — more than 1.5x the same recipe's own historical p95, once at least 10 samples exist

A flagged combination is stored, never silently dropped. `GET /adversarial/flagged` returns the union of two things: combinations flagged on their most recent run, and combinations that have *ever* been flagged and haven't been explicitly dismissed by a human yet — not just the narrower "currently flagged" set alone. Each row carries `ever_flagged` (sticky, never auto-resets), `first_flagged_reason`/`first_flagged_timestamp` (the *original* anomaly, preserved even after later clean runs overwrite the `last_*` columns), and `currently_flagged` (true only if the most recent run is still actively anomalous) — so a person can tell "still broken right now" apart from "flagged once, currently clean, still genuinely needs a look." See [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs#two-bugs-the-feature-found-in-itself-before-it-ever-ran-in-production) for why this distinction exists.

The only way a combination actually leaves the default review queue is `POST /adversarial/dismiss?fingerprint=...` — a real human action, not a side effect of a lucky clean run. Dismissal doesn't delete history (`include_dismissed=true` still shows it), and a genuinely *new* flag on a previously-dismissed combination correctly resurfaces it — an old, closed-out review doesn't permanently suppress a fresh, unrelated anomaly on the same fingerprint later.

`POST /adversarial/undismiss?fingerprint=...` reverses a dismissal, restoring `review_status` to exactly the state it was in before the first-ever dismissal — `NULL`, the same as a combination that was never dismissed at all, not a new, third state. See [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs#a-real-mistake-in-using-the-feature-itself-not-in-the-features-own-code) for the real incident that motivated adding this endpoint.

## What this feature has actually found

This feature exists to institutionalize the same adversarial megaquery testing approach that found most of Mnemolis's real bugs historically — and once it ran against real traffic, it kept doing exactly that. Running it for real surfaced two bugs in the feature's own development, a four-bug chain in Mnemolis's actual routing/decomposition/fusion logic, a genuine false positive (and a deeper regex bug underneath it) in this feature's own detector, a real Uptime Kuma timeout with no way to tune it, and one investigation that ended without ever finding a root cause. None of that is a knock on the feature — catching real, previously-unknown bugs in production is the entire point. See [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs) for the full record.

Two real, structural latency-variance mechanisms were also found and fixed at the root — see [The Latency Parallelization Investigation](The-Latency-Parallelization-Investigation) for that story.

## One known limitation worth tracking, not yet tuned

- **Source mismatch on the conditional path** — a conditional query's *condition* text gets routed through LLM-based source selection, which can validly land on a source that doesn't literally appear as an `INTENT_MAP` keyword in the query. The check doesn't yet distinguish "the LLM made a different valid call" from "the LLM made a wrong call" — right now it flags both the same way. Not yet a confirmed real false-positive rate against live traffic (unlike the part-count issue above, which was directly traced and confirmed) — recorded here as a standing, plausible concern worth watching, not yet acted on.
- **A single, global latency-outlier multiplier across every recipe** — the two real, distinct latency-variance mechanisms that originally motivated this observation (`conditional_with_remainder`'s sequential routing, `web`'s query expansion) have both since been fixed at the root rather than accommodated with a per-recipe override. Recorded here in case a third, different mechanism surfaces a similar pattern in the future — at that point, a per-recipe baseline genuinely earns its complexity; fixing the actual cause has been the better trade twice in a row so far.
  - **A third mechanism has since surfaced, though not through this check firing in production** — a user-run Locust benchmark (not Adversarial Self-Testing) found that `conditional_with_remainder`'s real query pool has a 17%-then-7% (now further reduced) chance of needing two concurrent LLM-bound calls at once, which this deployment's `OLLAMA_NUM_PARALLEL=1` constraint serializes rather than parallelizes — see [The Benchmark Investigation Log](The-Benchmark-Investigation-Log#thread-7-conditional_remainder-warm-worse-than-cold--the-same-ollama-constraint-but-deterministic-this-time). This recipe's own `_check_latency_outlier()` would plausibly flag a real occurrence of this exact case the same way the original two mechanisms did, if a generated query happens to draw one of the still-remaining double-LLM-hit pool entries — still recorded as a possibility this check would catch, not a confirmed in-production flag, since it was found via benchmark data rather than this feature firing.

## Configuration

| Setting | Default | What it controls |
|---|---|---|
| `ADVERSARIAL_TEST_ENABLED` | `false` | Master on/off switch — opt-in (defaults off), since when on it generates extra background query traffic against the LLM/SearXNG/Kiwix backends. `false` skips DB init, never registers the scheduler job, and `POST /adversarial/trigger` returns `{"status": "disabled"}` instead of running anyway — checked at both scheduler-registration time and inside `run_adversarial_test_cycle()` itself, so a direct call can never accidentally run real queries against the LLM/SearXNG/Kiwix backends while turned off |
| `ADVERSARIAL_TEST_INTERVAL_MINUTES` | `60` | How often the scheduler tick fires |
| `ADVERSARIAL_TEST_BATCH_SIZE` | `8` | Queries generated per tick — cheap to raise (no LLM calls in the hot path) |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_MULTIPLIER` | `1.5` | How many multiples of a recipe's own historical p95 counts as a real latency outlier |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_FLOOR_MS` | `1000` | A floor below which latency is never flagged regardless of the multiplier — protects fast, cache-hit-driven queries from getting flagged just for being a multiple of an even-faster sample |
| `ADVERSARIAL_TEST_LATENCY_OUTLIER_MIN_SAMPLES` | `10` | How many historical samples a recipe needs before the latency-outlier check engages at all |

`/health` reports `adversarial_testing` alongside `snapshot_jobs`, using the same staleness-grace-multiplier convention (`SNAPSHOT_STALE_GRACE_MULTIPLIER`, default 3x) the snapshot engine already uses. When disabled, it reports `{"status": "disabled"}` directly rather than eventually reading as `"stale"` — a deliberate off-switch shouldn't look like a job that silently stopped running.

## Endpoints

`POST /adversarial/trigger` — manually run one cycle immediately, rather than waiting for the next scheduled tick. Mirrors `/snapshots/trigger`'s exact pattern. Returns `{"status": "ran", "queries_run": N, "flagged": N}`, or `{"status": "disabled", "queries_run": 0, "flagged": 0}` without touching any real backend if `ADVERSARIAL_TEST_ENABLED` is `false`.

`GET /adversarial/flagged?limit=50&include_dismissed=false` — the union of currently-flagged and ever-flagged-but-not-dismissed combinations, most recent first. Each row includes `ever_flagged`, `currently_flagged`, `first_flagged_reason`/`first_flagged_timestamp` (the original anomaly), `review_status`, and `last_flagged_result_excerpt` (up to 500 characters of the real response text that triggered the most recent flag — null on a clean run, see below for why this exists). Pass `include_dismissed=true` for the full audit trail including closed-out rows. Reports `{"status": "disabled", ...}` the same way if turned off. Deliberately left unauthenticated, the same way `/health` and `/areas` already are: it exposes only synthetic, generated test queries and their structural anomaly flags, never real user queries or cache contents, so it sits outside `API_KEYS`' documented scope (`POST /search` and `GET /changes` only) for the same reason those two already do.

`POST /adversarial/dismiss?fingerprint=...` — mark a flagged combination as reviewed and closed. The `fingerprint` is the exact value from a flagged row's own `fingerprint` field, copied verbatim — not constructed by hand. Returns `404` for an unknown fingerprint. History is never deleted by a dismissal; a genuinely new flag on the same fingerprint later resurfaces it normally.

`POST /adversarial/undismiss?fingerprint=...` — the real, symmetric reversal. Use `GET /adversarial/flagged?include_dismissed=true` to find a dismissed row's fingerprint, since the default view no longer shows it once dismissed. Returns `404` for an unknown fingerprint; a fingerprint that was never dismissed in the first place is a safe no-op (the row already has the state this would restore it to).
