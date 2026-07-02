# The Fallback Observability Gap

Two separate, sequential bugs in the same general place: Mnemolis's `kiwix → web` and `news → web` fallback mechanism worked correctly the whole time, but for a real stretch of this project's history, there was no reliable way to actually *see* that it had happened — either from the API response itself, or from the routing layer's own internal logic.

## Bug one: `source_used` reported the intended source, not the actual one

A query routed to `kiwix` that returned nothing usable could silently fall back to `web` internally and come back with a genuinely good result — but the API response's `source_used` field still said `"kiwix"`. The cause: `main.py` independently re-derived the intended source *before* ever calling into the routing logic, with no way to learn afterward that an internal fallback had actually occurred. The routing function itself only ever returned a plain result string, with zero information about which source had actually produced it.

Fixed by introducing `route_with_source()`, which returns `(result, actual_source)` and threads the true source through every real exit path — direct success, fallback success, fusion, decomposed multi-part responses, and unknown-source errors. The plain `route()` function remains a backward-compatible thin wrapper for callers that only need the result text. The decomposed sub-query path also gained fallback capability it had been missing entirely up to that point — a sub-query landing on an empty result never attempted a fallback at all, unlike the top-level single-source path.

Verified against the real production query that originally surfaced this, alongside an unrelated disambiguation bug found in the same investigation: a GPIO/Python permission-error question correctly fell back from `kiwix` to `web` when `kiwix` returned nothing useful, and the API response correctly reported `"source_used": "web"` for the first time.

## Bug two: the two fallback-eligibility checks had quietly drifted apart

With `source_used` now reporting honestly, a second, deeper gap surfaced later: `router.py` and `fusion.py` each carried their own, independently-maintained copy of the "does this result look empty" check — and the two phrase lists had drifted apart in **both directions** since they were originally written separately.

`router.py`'s copy was missing `"not configured"` and `"could not connect"` entirely. The real, concrete consequence: with `FRESHRSS_URL` unset, a `news` query returned the literal configuration-error string — *"FreshRSS is not configured. Set FRESHRSS_URL and FRESHRSS_USER."* — as if it were a genuine, successful result. `source_used` correctly stayed `"news"` by this point (bug one was already fixed), but the real `news → web` fallback never triggered at all, because `router.py`'s own copy of the empty-check never recognized the configuration-error string as empty in the first place. The identical gap applied to `kiwix → web`, for any "not configured"/"could not connect"-style message Kiwix might produce.

Fixed by sharing one canonical phrase list between the two files, rather than maintaining two copies that could silently disagree about what counts as a failure worth falling back from.

## The lesson

Both bugs share the same shape: a real, working mechanism (the fallback chain itself) that nobody could actually verify was working, because the layer responsible for *reporting* whether it ran was broken first, and the layer responsible for *deciding whether to run it* had quietly drifted out of sync with its own sibling copy second. Neither bug affected the fallback's own correctness once it actually fired — both were about whether the system could ever know, or honestly report, that it had.

## Postscript: the third gap (v3.52.0)

The fix for bug one deliberately stopped short of full visibility. `main.py` inferred `fallback_occurred` by comparing its own pre-computed intended source against the resolved source — chosen over widening `route_with_source()`'s return signature, which recurses into itself at four internal call sites. That was the right trade at the time, and its own comment honestly named the cost: the inference was structurally blind to a fallback firing *inside a decomposed sub-query*, because the overall resolved source for those is just `"fusion"`.

v3.52.0 closed that last gap without paying the signature cost after all, using a mechanism the codebase had already proven elsewhere: a per-request ContextVar stats channel (`_ROUTE_STATS`, same pattern as `suppress_cache_writes()`). `route_query()` — the `/search` entry point — installs a small mutable dict; the fallback path in `_resolve_single_source()`, cache hits in `_get_cached()`, and real handler/fusion invocations each record one idempotent boolean into it at the exact point they happen, from any recursion depth and any worker thread (`copy_context()` copies the Context but shares the same dict object by reference). The same channel also eliminated a wasted cold LLM call the old design required: `/search` no longer intent-detects the full query before routing just to compute the `cached` flag — a full-query intent decision the decomposition and conditional paths never even used.

The pattern generalizes and is worth remembering: when a deep call graph with recursion needs to report facts upward without threading them through every signature, an idempotent-write ContextVar channel installed at the entry point gets the information out with zero behavior change for every caller that doesn't install one.
