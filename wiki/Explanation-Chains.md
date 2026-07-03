# Explanation Chains

`/search` with `"explain": true` (v3.53.0) returns the ordered trace of what routing actually did — which intent resolution ran and what it decided, every cache hit, every source invocation with its real elapsed time, every fallback, every decomposition split, every conditional extraction, and every [semantic routing](Semantic-Routing-Cache) match with its similarity score.

```json
POST /search
{"query": "what is the weather today and also lights status", "explain": true}
```

```json
"explanation": [
  {"step": "decomposed", "query": "what is the weather today and also lights status",
   "parts": ["what is the weather today", "lights status"]},
  {"step": "intent_keyword", "query": "what is the weather today", "decision": "forecast"},
  {"step": "source_invoked", "source": "forecast", "query": "what is the weather today", "elapsed_ms": 41},
  {"step": "intent_keyword", "query": "lights status", "decision": "ha"},
  {"step": "source_invoked", "source": "ha", "query": "lights status", "elapsed_ms": 38}
]
```

## Why this was nearly free to build

The v3.52.0 `/search` redesign introduced `_ROUTE_STATS` — a per-request ContextVar channel that records cache hits, invocations, and fallbacks at the exact code points where they genuinely happen, from any recursion depth or worker thread. Explanation chains are *the same recording*: `_route_event()` appends into the same dict `_route_stat()` writes booleans into, at the same authoritative sites. The explanation structurally cannot disagree with what actually ran, because both are one recording, never a parallel reconstruction. That's the design property the whole feature rests on — and it's why the feature's marginal architecture was close to zero.

## The event vocabulary

| step | recorded at | fields |
|------|-------------|--------|
| `intent_keyword` | `detect_intent()`'s keyword match | `query`, `decision`, `discourse_escalated` |
| `intent_cached` | routing-cache hit in `_llm_detect()` | `query`, `decision` |
| `intent_semantic` | [semantic routing](Semantic-Routing-Cache) reuse | `query`, `matched_query`, `similarity`, `decision` |
| `intent_llm` | a fresh LLM routing decision | `query`, `decision`, `discourse_escalated` |
| `intent_default` | the kiwix fallback (LLM unconfigured / unusable response) | `query`, `decision`, `reason` |
| `result_cache_hit` | `_get_cached()` | `source`, `query` |
| `source_invoked` | a real handler or fusion call, timed around the call itself | `source`, `query`, `elapsed_ms` |
| `fallback` | the [FALLBACK_CHAIN](Routing) firing in `_resolve_single_source()` | `from_source`, `to_source`, `query` |
| `decomposed` | the [decomposition](Query-Decomposition) split | `query`, `parts` |
| `conditional_detected` | [conditional extraction](Conditional-Query-Detection), top-level and per-sub-query | `query`, `condition`, `consequence`, `remainder` |
| `fusion` | a fusion dispatch, with the sources it fanned out to | `query`, `sources` |

## Event ordering and attribution

Within one thread, events append in genuine execution order — a fallback's chain reads `source_invoked` → `fallback` → `source_invoked`, in that order, because that's the order it happened. Across concurrent [decomposed sub-queries](Query-Decomposition#sub-queries-resolve-concurrently-v3520), interleaving is genuinely nondeterministic (events arrive in completion order), which is why **every event carries the query text it belongs to** — attribution comes from the event's own fields, never from list position. `list.append()` is atomic under the GIL, so concurrent appends are individually safe; only their relative order is undefined, and the design deliberately never depends on it.

## Failure traces

The chain is returned on *failed* requests too — as the partial trace of everything that ran before the exception. That's when a trace earns its keep most: "kiwix ran, came back empty, fallback to web was declared, and *then* it broke" is a diagnosis; a bare error string is a symptom.

## Cost and defaults

Events are always collected (appends of small dicts are noise next to any real routing work — this keeps the recording path branch-free and identical whether anyone is watching), but only *returned* when the request asks. `explain` defaults to false; without it the response's `explanation` field is simply `null`, so existing clients see one new always-null key and nothing else changes. The MCP `search` tool is unchanged — explanation chains are a REST-surface feature for now; extending the MCP tool's result shape is tracked separately.

## Development Notes

This shipped from one of the project's four original design documents (drafted alongside Predictive Pre-Fetching, Self-Healing Source Selection, and Ambient Intent Disambiguation). It was deliberately built *after* the v3.52.0 stats channel rather than before it, because the channel collapsed the design's cost: the original sketch assumed threading explanation state through `route_with_source()`'s recursion-laden signature, the exact change that function's history (see [The Fallback Observability Gap](The-Fallback-Observability-Gap)) had already twice judged not worth its risk. Once the ContextVar channel existed for `cached`/`fallback_occurred`, explanations became "more keys in a dict that already flows everywhere" — the postscript to that saga page predicted exactly this generalization, and this feature is it cashed in.
