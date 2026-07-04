"""
Mnemolis Fusion Source
Queries multiple sources concurrently and merges results.
"""
import logging
import contextvars
import concurrent.futures
import re
from app.config import settings

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared, module-level thread pool — reused across every fusion call
# ---------------------------------------------------------------------------
#
# Found via a deliberate investigation into a real, recurring,
# previously-unexplained RemoteDisconnected('Remote end closed connection
# without response') failure that had appeared sporadically in this
# project's own benchmark history since v3.50.9, on fusion_* endpoints
# only. search() used to create a brand-new ThreadPoolExecutor on every
# single call, sized to len(valid) (typically 2-3) — confirmed directly,
# not estimated: 20 concurrent fusion-shaped requests produce 81 real,
# live OS threads at peak (20 outer "request" threads, each spinning up
# its own 3-worker inner executor, plus baseline), with no ceiling at all
# as concurrent fusion traffic increases.
#
# Both confirmed RemoteDisconnected occurrences that prompted this fix
# landed within the first 33 seconds of a cold benchmark run — the
# single worst-case moment for concurrent thread pressure, immediately
# after a cache clear guarantees every concurrent user's next pick is a
# cold miss. Neither occurrence produced a corresponding application-
# layer log line, consistent with the failure happening at the OS/socket
# level rather than inside Python code this project controls — the layer
# undocumented thread-count pressure would actually manifest at on a
# resource-constrained host like the N100 this project's reference
# deployment runs on. NOT confirmed as the definitive, proven mechanism
# behind those specific historical failures (no direct access to the
# real deployment's ulimits or dmesg output from the moment of either
# failure) — recorded honestly as a well-corroborated, plausible
# mechanism with no real downside to fixing, not an overstated certainty.
#
# Replaced with a single, shared, long-lived pool — the same shape of
# fix app/llm.py's connection pool already applied to a different
# unbounded-per-call resource (HTTP connections, in that case). A plain
# module-level singleton, not a lazy-init-with-lock accessor —
# ThreadPoolExecutor construction does no real work eagerly (worker
# threads are spawned on first submit, not at construction), so there's
# no "first caller pays a real cost" race to guard against.
#
# One real risk checked before shipping this, not assumed away: whether
# any fusion-dispatchable source module relies on thread-local state that
# assumed a fresh, never-reused thread per call — a shared pool means the
# same OS thread now runs many different source calls over its lifetime,
# not just one. Checked directly: no module in app/sources/ or app/llm.py
# uses threading.local() or any thread-local storage. Worth re-checking
# if any source module ever adds thread-local state in the future.
_fusion_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=settings.fusion_thread_pool_size,
    thread_name_prefix="fusion",
)


def _looks_empty(result: str) -> bool:
    """Check if a result is empty or an error.

    Genuinely shared with router.py, which used to carry a separate,
    independently-maintained copy of this exact function with an
    overlapping but NOT identical phrase list — found via a second,
    deliberate "bulletproofing" re-pass specifically looking for the
    same kind of cross-file drift already found and fixed once this
    release cycle (_merge_same_source). The drift here was real and
    significant, in both directions:

    router.py's list was missing "not configured" and "could not
    connect" — meaning a genuinely real, reachable scenario (FreshRSS
    unconfigured, asking for news) returned the literal config-error
    string "FreshRSS is not configured. Set FRESHRSS_URL and
    FRESHRSS_USER." as if it were real, successful content, since
    router.py's _looks_empty() never recognized it as empty — and
    FALLBACK_CHAIN's real, configured "news" -> "web" fallback never
    triggered as a result. Confirmed directly: route_with_source("give
    me the news", "news") with FRESHRSS_URL unset returned the raw
    config-error message with source_used="news", not the automatic
    fallback to "web" the fallback chain is clearly designed to
    provide.

    fusion.py's own list was separately missing "unknown source" (the
    real fix from a previous pass this same release cycle) and "error
    reaching" — found while verifying the unified list against every
    real failure message every source file actually produces:
    "Error reaching SearXNG: connection failed." doesn't contain a bare
    "error:" (the colon comes after "SearXNG", not immediately after
    "Error"), so it needed its own, more specific phrase.

    router.py already imports this module directly (it calls
    fusion.search() for internal multi-source dispatch), making this
    the safe home for the shared, canonical version — the reverse
    import direction would create a circular import.

    A REAL FALSE POSITIVE, FOUND AND FIXED THIS RELEASE: the original
    version checked whether any of these phrases appeared ANYWHERE in
    the result, with no other constraint. Several of these phrases
    ("not configured", "could not connect", "could not determine",
    "error:") are ordinary English, not unique sentinel strings —
    found by investigating a real, unexplained benchmark anomaly
    (`cache_hit`'s single cold request paying a multi-second cost in
    both the v3.50.9 and v3.50.11 runs, eventually traced to ordinary
    Ollama queue contention, NOT this bug — but checking _looks_empty()
    itself along the way as one of several candidate mechanisms turned
    up this real, separate issue). `_diff_news()` in snapshots.py
    echoes RAW, unmodified upstream article headlines directly into
    change descriptions (`f"New article: {story}"`), and freshrss.py's
    own search() does the same for article titles/summaries — real
    news content can absolutely contain a headline like "Tech Company
    Could Not Determine Cause of Outage" by sheer coincidence.
    Confirmed directly with a real reproduction: a genuinely successful,
    fully-populated, multi-source `changes` response containing one
    such headline matched the OLD _looks_empty() and would have been
    silently discarded by FALLBACK_CHAIN's real "news" -> "web"
    fallback in a live deployment — a worse, generic web-search answer
    substituted for a perfectly good one, for no reason but an unlucky
    word in a real headline this project has no control over.

    Two heuristics were tried and rejected before landing on the real
    fix. A length cap (real Mnemolis messages are all under 80 chars)
    fails because kiwix.py's `f"Found {title} but could not fetch
    article content."` has unbounded length (a real article title is
    interpolated into it) — and a SHORT, single-article false positive
    (a brief headline with little summary text) can still slip under
    any length cap that's generous enough to keep that real message
    working. A prefix check (does the result START WITH the phrase)
    fails for the common "X is not configured" shape, since the
    SOURCE NAME always comes first ("Home Assistant is not
    configured." — the phrase starts at index 18, not 0) — confirmed
    this would have broken 5 of the project's own real config-error
    messages, caught by the existing test suite before this design
    shipped.

    The actual fix: every genuine empty/error message this function
    exists to catch is plain, unformatted prose — confirmed directly
    against every real `return` statement that produces one. Every
    real article/multi-source result this project produces, by
    contrast, wraps titles in markdown bold (`f"**{title}** ..."` —
    freshrss.py, searxng.py, home_assistant.py, and snapshots.py's
    format_changes() all do this consistently). A bare "**" anywhere in
    the result is a reliable, structural signal that this is genuine
    formatted content, regardless of what words happen to appear
    inside it — confirmed this distinction holds for every real
    message (none contain "**") and every constructed false-positive
    case (all of them do, since the offending headline only ever
    appears wrapped in the same markdown formatting every other
    real article does).

    FIVE MORE REAL PHRASE-LIST GAPS, FOUND AND FIXED THIS RELEASE: a
    second, systematic cross-check — every plain-string failure/empty
    return statement in every source file, checked one at a time against
    the phrase list above — found five more real, directly-reachable
    gaps the first pass missed: "unable to retrieve" (forecast.py's own
    exception handler, `f"Unable to retrieve forecast: {e}"`, returned on
    ANY failure — a network timeout, Open-Meteo briefly down, a malformed
    response), "no valid sources"/"no results returned" (fusion.py's own
    two self-generated messages — this function never recognized its own
    module's failure output, only every other module's), and "no entity
    states returned"/"no matching entities found"/"no significant
    changes" (home_assistant.py and snapshots.py).

    The forecast.py gap was a real, user-visible, caching-driven bug, not
    just a theoretical one: router.py's _resolve_single_source() calls
    `_set_cached(source, query, result)` whenever `not _looks_empty(result)`
    — and since this function didn't recognize "Unable to retrieve
    forecast: ..." as empty, a single transient API hiccup got cached as
    if it were a genuine, successful weather result, for
    cache_ttl_forecast_seconds (30 minutes by default). A single network
    blip could leave every forecast query returning that exact stale
    error for up to half an hour, instead of correctly retrying on the
    very next request the way it already did for every other recognized
    failure phrase. The two new fusion.py phrases matter for a different,
    non-caching reason: router.py's decomposition loop calls
    fusion.search() on an individual decomposed sub-query and checks
    `_looks_empty(sub_result)` before merging it in — without these two
    phrases, a genuinely failed nested fusion call's own error message
    would be treated as real content and merged into the user-facing
    response.

    Confirmed the same structural gate already protecting the original
    phrase list (the "**" check above, evaluated before any phrase
    comparison) protects these five identically — a real article headline
    containing any of these phrases' words still passes through correctly
    as long as it's wrapped in the markdown bold every genuine article
    title already uses.
    """
    if not result:
        return True
    if "**" in result:
        # Genuine formatted content (an article title, a changes
        # header, an HA entity label) — never a plain failure/empty
        # message, regardless of what words happen to appear inside it.
        return False
    result_lower = result.lower().strip()
    return any(phrase in result_lower for phrase in _EMPTY_PHRASES)


# Hoisted from _looks_empty()'s body — the function is called on every
# single source result (fusion filtering, FALLBACK_CHAIN checks,
# decomposed sub-query filtering), and rebuilding a 20-element list per
# call is pure per-call allocation for a set of phrases that never
# changes at runtime. A tuple, not a list: immutable module state.
_EMPTY_PHRASES = (
    "no results found", "no recent articles", "not yet implemented",
    "could not fetch", "no books available", "could not determine",
    "unknown source", "not configured", "could not connect",
    "error:", "error reaching", "error fetching",
    "no sufficiently relevant results", "no monitors found",
    "unable to retrieve", "no valid sources", "no results returned",
    "no entity states returned", "no matching entities found",
    "no significant changes",
)


def _truncate(result: str, max_chars: int | None = None) -> str:
    """Truncate a result to max_chars, preserving whole lines.
    Defaults to settings.fusion_max_chars_per_source when not specified."""
    if max_chars is None:
        max_chars = settings.fusion_max_chars_per_source
    if len(result) <= max_chars:
        return result
    truncated = result[:max_chars]
    # Cut at last newline to avoid mid-line truncation
    last_newline = truncated.rfind("\n")
    if last_newline > max_chars // 2:
        truncated = truncated[:last_newline]
    return truncated.rstrip() + "\n…"


def _deduplicate(results: dict[str, str]) -> dict[str, str]:
    """Remove sources whose content is substantially contained in another source's result.

    Checks sentence-level overlap — if 60%+ of a source's sentences already
    appear in a longer result, drop it as redundant.

    Always compares the SHORTER side's overlap ratio against the 0.6
    threshold, regardless of which source happens to be `s1` vs `s2` in
    iteration order. A prior version checked `overlap / len(sents2) >= 0.6`
    first, unconditionally discarding `s2` on that branch — which only
    correctly identifies "s2 is redundant" when s1 is the longer source.
    If s1 happened to be the shorter one, this exact same branch could
    still fire (s2's overlap ratio crosses 0.6 from either direction) and
    unconditionally dropped s2 anyway, regardless of which one actually
    had more unique content. Confirmed via a direct, real reproduction:
    the same two sources, same actual overlap, produced opposite outcomes
    purely from which key appeared first in the `results` dict — and in
    the real call site, dict insertion order is determined by
    concurrent.futures.as_completed()'s own completion order, i.e. by
    whichever source's network/processing call happened to finish first,
    a detail with zero semantic relationship to which source's content
    is actually more complete or useful.

    Measured directly against this project's own real benchmark latency
    distributions: under cold-cache conditions (the condition under which
    two sources are actually likely to produce real, overlapping content
    worth deduplicating in the first place), `web` wins the completion
    race roughly two-thirds of the time against `kiwix` — meaning the old
    order-dependent bug had a real, confirmed lean toward discarding
    kiwix's content (the more often encyclopedic, substantive source) in
    favor of web's, specifically on the queries most likely to trigger
    real overlap at all. This fix removes that bias by making the outcome
    depend only on which source's content is actually shorter and more
    redundant, never on which one happened to finish its network call
    first.
    """
    if len(results) <= 1:
        return results

    def sentences(text: str) -> set[str]:
        return {
            s.strip().lower() for s in text.replace("\n", ". ").split(".")
            if len(s.strip()) > 20
        }

    sources = list(results.keys())
    keep = set(sources)

    for i, s1 in enumerate(sources):
        if s1 not in keep:
            continue
        for s2 in sources[i+1:]:
            if s2 not in keep:
                continue
            sents1 = sentences(results[s1])
            sents2 = sentences(results[s2])
            if not sents1 or not sents2:
                continue
            overlap = len(sents1 & sents2)
            # Always treat the source with FEWER sentences as the
            # candidate for removal — deciding once, by actual size,
            # rather than per-branch by which variable name happens to
            # hold which source.
            if len(sents1) <= len(sents2):
                shorter, longer, shorter_len = s1, s2, len(sents1)
            else:
                shorter, longer, shorter_len = s2, s1, len(sents2)
            if overlap / shorter_len >= 0.6:
                _LOGGER.info("Fusion: dropping '%s' as redundant with '%s'", shorter, longer)
                keep.discard(shorter)

    return {s: r for s, r in results.items() if s in keep}


def _merge_same_source(parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Merge consecutive results from the same source into one block.

    Genuinely shared with app/router.py's _merge_decomposed_parts(),
    which used to carry a byte-for-byte identical copy of this exact
    logic — found during a deliberate complexity-investigation pass on
    this function. router.py already imports this module directly
    (it calls fusion.search() for internal multi-source dispatch), so
    this is the safe import direction; fusion.py never imports from
    router.py, avoiding a circular import the reverse direction would
    create.

    Only ever compares the OUTER tuple label — if one part's source is
    the literal string "fusion" (an already-headered, nested multi-
    source blob) and a separate part's source is a bare label that
    happens to ALSO be one of the sources nested inside that blob, this
    function has no way to see the duplication, since it can't look
    inside an opaque "fusion"-labeled string. router.py's
    _merge_decomposed_parts() runs a second, separate pass —
    _dedupe_nested_fusion_sections() — after this one specifically to
    catch that case, operating on the final assembled text rather than
    on these tuples. Found via a real, live duplicate-section bug.

    Deduplicates individual items BETWEEN the two blobs being merged
    here, via _dedupe_items_across_blobs() below, before joining them —
    done at THIS exact point deliberately, while the boundary between
    "result from call 1" and "result from call 2" is still completely
    unambiguous (two distinct strings, not yet concatenated). A second,
    real bug found verifying the first nested-fusion-section fix: two
    independent calls to the same backend (e.g. one nested inside an
    internal-fusion sub-query, one a separately-decomposed clause's own
    bare resolution) can return overlapping items — confirmed via a
    real FreshRSS "general query, return everything" case where the
    same recent headlines legitimately came back from two separate,
    redundant calls. Deduping AFTER the plain "\\n\\n" join below would
    require re-finding that same boundary inside already-merged text,
    where it's no longer reliably distinguishable from an ordinary
    paragraph break within one blob's own content — tried first,
    confirmed broken via a failing test, fixed by moving the dedup
    earlier instead.

    GROUPS FIRST, DECIDES THE SEPARATOR ONCE PER GROUP — a prior version
    decided the "\\n\\n---\\n\\n" vs bare "\\n\\n" separator independently
    on each individual pairwise merge, based on whether EITHER side of
    that one pair already happened to contain "---" internally. When a
    chain mixes genuinely single-item results with genuinely multi-item
    ones (entirely realistic — freshrss.py returns a bare, separator-free
    single article when exactly one matches a query, and a proper
    "---"-joined list when more than one does), the EARLY pairs in the
    chain — before any multi-item result has joined the accumulator — got
    the wrong, ambiguous "\\n\\n" separator, even though the assembled
    whole is unambiguously a multi-item list by the time the chain
    finishes. Confirmed directly with a real, plausible compound query
    (three news-resolved clauses, the first two each returning exactly
    one article, the third returning three): the boundary between the
    first two genuinely separate, unrelated headlines came out as a bare
    blank line — visually indistinguishable from two paragraphs of ONE
    story, exactly the failure mode _dedupe_items_across_blobs()'s own
    docstring already warns about for a different boundary, recurring
    here one level up in the function that's supposed to be applying
    that exact lesson. Fixed by collecting every consecutive same-source
    part into one group first, then deciding and applying a single
    separator for the whole group: combining 2+ genuinely separate
    same-source results is, definitionally, a multi-item situation the
    moment there are two of them, independent of whether any individual
    part happened to already contain "---" on its own.

    One real, deliberate behavior change worth stating plainly: a merge
    of exactly two genuinely single-item same-source parts (e.g. two
    plain HA results with no internal "---") now always gets
    "\\n\\n---\\n\\n" instead of the prior code's "\\n\\n" — this is the
    CORRECT behavior, not an accidental side effect; two separate results
    being combined into one source's section is inherently multi-item the
    moment there are two of them. No existing test asserted the literal
    separator character for that case, only content presence and section
    count, so this is a safe, non-breaking correction."""
    if not parts:
        return parts
    merged = []
    i = 0
    while i < len(parts):
        current_source, current_result = parts[i]
        group = [current_result]
        j = i + 1
        while j < len(parts) and parts[j][0] == current_source:
            group.append(parts[j][1])
            j += 1
        if len(group) == 1:
            merged.append((current_source, current_result))
        else:
            # Combining 2+ genuinely separate same-source results IS
            # inherently a multi-item situation, regardless of whether
            # any individual part happened to contain "---" on its own.
            acc = group[0]
            for nxt in group[1:]:
                acc, nxt, _ = _dedupe_items_across_blobs(acc, nxt)
                acc = acc.rstrip() + "\n\n---\n\n" + nxt.lstrip()
            merged.append((current_source, acc))
        i = j
    return merged


def _dedupe_items_across_blobs(first: str, second: str) -> tuple[str, str, bool]:
    """Remove items from `second` that already appear in `first`,
    before the two get joined by _merge_same_source() above.

    Splits each blob on the real, established "\\n\\n---\\n\\n" item
    separator every multi-item source (freshrss.py's news, searxng.py's
    web) already uses for its own individual result blocks. Dedup key
    is each item's first line — the "**Title** (Source)" or "**Title**"
    line every one of these sources leads with — exact match only,
    never a fuzzy/similarity comparison, so this can only ever remove a
    genuinely identical leading line, not something that merely looks
    similar.

    A true no-op (returns both inputs completely unchanged) for any
    blob with no real overlap — the overwhelming common case — and for
    any content that isn't built from this "**Title**"-leading,
    "---"-separated item convention at all (e.g. a single-item plain-
    text result, or Home Assistant's differently-shaped bulleted-list
    content), since those simply won't split into multiple items with
    a shared leading line to begin with.

    Returns (first, second, is_multi_item) — the third value tells the
    caller whether either blob looked like real multi-item list
    content, so the caller can join the two with the real "\\n\\n---\\n\\n"
    item separator instead of a bare "\\n\\n" that would otherwise leave
    the boundary between the two original blobs visually indistinguishable
    from an ordinary paragraph break within either one's own content —
    confirmed via a real failing test this exact ambiguity is what
    silently broke a first version of this fix's own dedup logic
    downstream.
    """
    first_items = first.split("\n\n---\n\n")
    second_items = second.split("\n\n---\n\n")
    is_multi_item = len(first_items) > 1 or len(second_items) > 1
    if not is_multi_item:
        return first, second, False  # neither blob looks like a multi-item list — nothing to dedupe

    seen_titles = {item.strip().split("\n", 1)[0].strip() for item in first_items if item.strip()}
    deduped_second_items = [
        item for item in second_items
        if item.strip().split("\n", 1)[0].strip() not in seen_titles
    ]
    if len(deduped_second_items) == len(second_items):
        return first, second, True  # multi-item, but nothing was actually a duplicate
    return first, "\n\n---\n\n".join(deduped_second_items), True


_HEADER_LABELS = {
    "kiwix": "ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW",
    "forecast": "WEATHER FORECAST FOR YOUR CONFIGURED HOME LOCATION",
    "news": "RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED",
    "web": "LIVE WEB SEARCH RESULTS",
    "uptime": "YOUR HOMELAB SERVICE STATUS",
    "ha": "YOUR HOME ASSISTANT ENTITY STATES",
    "changes": "DETECTED CHANGES SINCE LAST SNAPSHOT",
}


# The separator between the source tag and its descriptive label inside a
# section header. A named constant rather than a literal inside
# _format_header()'s f-string because HEADER_PATTERN below must match
# EXACTLY what _format_header() writes — the two are built from the same
# constant so a format change here cannot silently drift away from the
# parser that reads it. The _looks_empty() cross-file-drift bug (two
# independently-maintained copies of "the same" logic diverging until a
# real fallback stopped firing — see _looks_empty()'s own docstring) is
# the cautionary tale this constant exists to prevent repeating.
HEADER_SEPARATOR = " — "

# Matches any header _format_header() can produce — including headers for
# sources not in _HEADER_LABELS, whose label is just source.upper(), which
# is why the label side is "anything up to the closing bracket" rather
# than an alternation over the known labels. Exported for
# app/synthesis.py's section parser (attributed prompt assembly); also
# exercised by a dedicated drift test that formats a real header and
# asserts this pattern matches it, so a change to either side fails
# loudly in the suite rather than quietly in a synthesized answer's
# attribution.
HEADER_PATTERN = re.compile(
    r"^\[([A-Z][A-Z0-9_]*)" + re.escape(HEADER_SEPARATOR) + r"[^\]\n]*\]",
    re.MULTILINE,
)


def _format_header(source: str) -> str:
    """Build a fusion section header. Includes a descriptive label to prevent
    the LLM from cross-referencing unrelated sections (e.g. inferring location
    from a news article when reading the forecast section)."""
    label = _HEADER_LABELS.get(source, source.upper())
    return f"[{source.upper()}{HEADER_SEPARATOR}{label}]"


def search(query: str, sources: list[str] | None = None) -> str:
    """
    Query multiple sources concurrently and merge results.

    Args:
        query: The search query
        sources: List of source names to query. If None, uses default ["kiwix", "web"]

    Returns:
        Merged result string with source attribution headers.
        If only one source succeeds, returns its result directly without a header.
    """
    # Import here to avoid circular imports
    from app.router import SOURCE_MAP

    if not sources:
        sources = ["kiwix", "web"]

    # Validate and deduplicate
    valid = []
    seen = set()
    for s in sources:
        if s == "fusion":
            _LOGGER.warning("Fusion source cannot reference itself — skipping")
            continue
        if s not in SOURCE_MAP:
            _LOGGER.warning("Unknown source '%s' in fusion request — skipping", s)
            continue
        if s not in seen:
            valid.append(s)
            seen.add(s)

    if not valid:
        return "No valid sources specified for fusion query."

    # Found via a deliberate "bulletproofing" pass: FUSION_MAX_SOURCES
    # is a plain, unvalidated int — setting it to 0 (a plausible
    # misconfiguration, e.g. someone trying to "disable" fusion
    # entirely) capped `valid` to an empty list AFTER the only existing
    # empty-list check above, meaning ThreadPoolExecutor(max_workers=0)
    # crashed with a raw ValueError ("max_workers must be greater than
    # 0") instead of the sensible "no valid sources" message already
    # used for the genuinely equivalent case just above. Re-checking
    # for emptiness after capping reuses that same, already-correct
    # error path rather than introducing a second one.
    max_sources = settings.fusion_max_sources
    if len(valid) > max_sources:
        _LOGGER.warning(
            "Fusion request has %d sources, capping at %d",
            len(valid), max_sources
        )
        valid = valid[:max_sources]

    if not valid:
        return "No valid sources specified for fusion query."

    _LOGGER.info("Fusion query: '%s' sources=%s", query[:50], valid)

    # Query all sources concurrently
    #
    # Three fixes live in this block together, found across the same
    # investigation and sharing this exact code surface:
    #
    # 1. contextvars.copy_context() per submitted task — a bare
    #    executor.submit(fn, *args) does NOT propagate
    #    contextvars.ContextVar state (router.suppress_cache_writes())
    #    into worker threads. router.py's _resolve_conditional() and
    #    searxng.py's own concurrent fetch already learned this lesson
    #    and already fixed it the identical way; fusion.py was the one
    #    remaining unfixed ThreadPoolExecutor site in the codebase.
    #    Confirmed scope: only matters via the kiwix source (the only
    #    source module that writes to the routing cache from inside a
    #    source handler), and only during adversarial testing — a real
    #    user's /search request never sets this flag, so this has zero
    #    effect on real traffic. One copy_context() call per task, not
    #    one shared object — Context.run() is documented as
    #    non-reentrant across concurrent execution (confirmed via a real
    #    RuntimeError hit while building searxng.py's own fix from
    #    sharing one Context across two executor.submit() calls).
    #
    # 2. The shared, module-level _fusion_executor (see its own comment
    #    above) instead of a fresh per-call ThreadPoolExecutor.
    #
    # 3. Explicit executor.shutdown(wait=False) instead of the implicit
    #    shutdown(wait=True) a `with ThreadPoolExecutor(...) as executor:`
    #    block triggers on exit. Confirmed directly, via a real
    #    partial-completion race forced between a fast and a genuinely
    #    slow source: as_completed(futures, timeout=fusion_timeout)'s
    #    own timeout fires exactly when configured (that part already
    #    worked correctly) — but the surrounding `with` block does not
    #    actually return control to the caller until every submitted
    #    thread genuinely finishes, completely independent of whatever
    #    as_completed() already gave up on. fusion_timeout_seconds
    #    correctly bounded how long search() waited for results before
    #    giving up on them; it never actually bounded how long the
    #    CALLER waited for search() to return, on any release until now
    #    — confirmed measured: a configured 1-second timeout, an actual
    #    ~10-second caller-facing wait, dropping to ~1.16s with this fix.
    #    Moot now anyway with a shared, long-lived pool (point 2 above)
    #    — there is no per-call executor left to shut down at all; an
    #    abandoned straggler from a timed-out source simply keeps
    #    running in the shared pool and is discarded once it finishes,
    #    with no resource leak (confirmed directly: thread count returns
    #    to baseline once the abandoned task completes).
    fusion_timeout = settings.fusion_timeout_seconds
    results = {}
    futures = {
        _fusion_executor.submit(contextvars.copy_context().run, SOURCE_MAP[s], query): s
        for s in valid
    }
    try:
        for future in concurrent.futures.as_completed(futures, timeout=fusion_timeout):
            source = futures[future]
            try:
                result = future.result(timeout=fusion_timeout)
                results[source] = result
                _LOGGER.info("Fusion: source '%s' returned %d chars", source, len(result))
            except concurrent.futures.TimeoutError:
                _LOGGER.warning("Fusion: source '%s' timed out", source)
                results[source] = None
            except Exception as e:
                _LOGGER.warning("Fusion: source '%s' failed: %s", source, e)
                results[source] = None
    except concurrent.futures.TimeoutError:
        # Found via a deliberate complexity-investigation pass: this
        # is as_completed()'s OWN overall timeout, distinct from the
        # per-future future.result(timeout=...) timeout caught
        # inside the loop above. as_completed() raises this for the
        # entire iteration once the deadline passes, regardless of
        # how many individual futures had already completed — and
        # since this was previously uncaught here, a single slow
        # source mixed with a fast one crashed the ENTIRE fusion
        # call, discarding the fast source's genuinely successful
        # result along with it, even though that data already
        # existed and was sitting in `results`. This directly
        # undermined fusion's own documented graceful-degradation
        # design — "filters empty or failed results... if only one
        # source returns results, it is returned directly" — by
        # turning a partial success into a total, opaque failure
        # instead. Any future not already in `results` by the time
        # this fires is genuinely still running past the deadline;
        # mark it as failed without losing whatever real results
        # were already gathered before the timeout.
        _LOGGER.warning("Fusion: overall timeout reached, %d source(s) still running", len(futures) - len(results))
        for future, source in futures.items():
            if source not in results:
                results[source] = None

    # Filter successful non-empty results — preserve original source order
    successful = {
        s: r for s in valid
        if (r := results.get(s)) and not _looks_empty(r)
    }

    if not successful:
        return "No results returned from any source in fusion query."

    # Single successful source — return directly without header
    if len(successful) == 1:
        source, result = next(iter(successful.items()))
        _LOGGER.info("Fusion: only '%s' returned results, returning directly", source)
        return result

    # Deduplicate — drop sources whose content is mostly covered by another
    successful = _deduplicate(successful)

    if len(successful) == 1:
        source, result = next(iter(successful.items()))
        return result

    # Truncate verbose results before merging
    truncated = {s: _truncate(r) for s, r in successful.items()}

    # Build parts list preserving source order
    #
    # Found via a deliberate "bulletproofing" pass: this used to call
    # _merge_same_source() here too, with a comment claiming it "fixes
    # duplicate [HA] from decomposition" — but that scenario can't
    # actually occur AT THIS CALL SITE. `valid` (built at the top of
    # this function) is already deduplicated via its own `seen` set
    # before `parts` is ever built, so `parts` here can never contain
    # two entries for the same source — there's nothing for
    # _merge_same_source() to merge. The comment's real scenario (two
    # independently-decomposed sub-queries both resolving to the same
    # source, e.g. "ha") genuinely happens in router.py's own
    # _merge_decomposed_parts(), the OTHER real call site for this
    # shared function — that one still needs it; this one never did.
    parts = [(s, truncated[s]) for s in valid if s in truncated]

    merged = "\n\n---\n\n".join(
        f"{_format_header(source)}\n{result}" for source, result in parts
    )

    _LOGGER.info(
        "Fusion: merged %d sources for query '%s'",
        len(parts), query[:50]
    )
    return merged
