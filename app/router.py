import re
import time
import json
import logging
import os
import sqlite3
import threading
import contextvars
import tempfile
import concurrent.futures
from contextlib import contextmanager, closing
from typing import Callable, NamedTuple
from app.sources import kiwix, forecast, freshrss, searxng, uptime_kuma, fusion, home_assistant
from app.snapshots import get_changes, format_changes
from app import semantic_routing
from app.config import settings

_LOGGER = logging.getLogger(__name__)

CACHE_FILE = "/app/data/cache.json"

# ---------------------------------------------------------------------------
# Read-only query_log.db access
# ---------------------------------------------------------------------------
#
# Found via research for two separate, not-yet-built design docs (Self-
# Healing Source Selection Through Reinforcement, Ambient Intent
# Disambiguation Through Context): both need router.py to read recent rows
# from query_log.db (recent queries as a disambiguating signal; historical
# routing outcomes as training labels), but query_log.db, _LOG_DB, and
# _log_query() all live in app/main.py today, and router.py has zero
# existing access to any of them — confirmed directly, no references
# anywhere in this file before this addition.
#
# This is a direct, independent SQLite connection by file path — mirroring
# main.py's own _connect()/_LOG_DB pattern exactly, and the same general
# shape snapshots.py, adversarial_testing.py, and temporal_patterns.py each
# already use for their own separate .db files — deliberately NOT an import
# from app.main. main.py already imports from router.py
# (`from app.router import (...)`, confirmed directly at the top of that
# file); the reverse import direction would be a genuine circular import —
# the identical class of problem app/sources/fusion.py's own docstring names
# as the reason _looks_empty()'s canonical copy lives there rather than in
# router.py, rather than the other way around.
#
# Read-only and defensive on purpose: router.py does not own query_log's
# schema (app/main.py's _init_log_db() does, including its own column-
# migration logic) and has no business writing to it. The functions below
# never INSERT/UPDATE/DELETE anything, and degrade to an empty result
# rather than raising if the database file or table doesn't exist yet —
# a real, reachable case for any process that imports router.py before
# main.py's own lifespan has ever run (e.g. a standalone script, or a test
# that constructs router.py's own objects directly without booting the
# full FastAPI app first).
_LOG_DB = "/app/data/query_log.db"


def _connect_log_db_readonly() -> sqlite3.Connection:
    """Open a read-only connection to query_log.db.

    Uses SQLite's own URI ?mode=ro to enforce read-only at the connection
    level, not just by convention in this file's own code — a real,
    additional safety property: a bug introduced later in this module
    that accidentally tries to write would fail loudly and immediately
    (sqlite3.OperationalError: attempt to write a readonly database)
    rather than silently succeeding and creating exactly the kind of
    schema-ownership confusion this function's own module-level comment
    explains router.py should never have in the first place.

    Raises sqlite3.OperationalError if the file doesn't exist — callers
    in this module catch this themselves (see get_recent_queries() below)
    rather than this helper silently returning None, so a genuine,
    unexpected connection failure (a permissions problem, a corrupted
    file) is never confused with the normal, expected "no queries logged
    yet" case.
    """
    uri = f"file:{_LOG_DB}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    return con


def get_recent_queries(limit: int = 20) -> list[tuple[str, str]]:
    """Return the most recent (query, timestamp) pairs from query_log.db,
    most recent first — read-only, never touches the log's own schema or
    write path, both of which app/main.py exclusively owns.

    Returns an empty list, never raises, if the database or table doesn't
    exist yet (a fresh install with no query ever logged) or any other
    read error occurs — the same defensive, log-and-continue convention
    every background-job read function elsewhere in this project already
    follows (confirmed directly: app/snapshots.py's _get_snapshots_since(),
    app/temporal_patterns.py's analogous reads, both catch broadly and
    return an empty result rather than letting a caller crash over what
    is, in the overwhelming majority of real cases, simply "nothing has
    happened yet").

    `limit` is the most recent N rows — deliberately not "since N
    minutes/hours ago": Ambient Intent Disambiguation's own context
    window wants exactly this shape (a small, bounded, most-recent-first
    window regardless of how much real wall-clock time those N queries
    happen to span, not an unboundedly large result during a quiet
    stretch followed by an unboundedly large one during a busy stretch).

    Found via a direct re-check while updating both consuming design
    docs after this function shipped: this function is NOT a fit for
    Self-Healing Source Selection's or Predictive Pre-Fetching's own
    real needs, despite an earlier version of this docstring claiming
    otherwise. Both of those features need a genuinely different access
    pattern — a bounded, time-window-based bulk scan over query_log
    (e.g. "every row from the last 6 hours," for Pre-Fetching's mining
    step; the full historical table for Self-Healing's clustering and
    training) returning the real outcome columns (source_used, success,
    fallback_occurred), not just (query, timestamp) for the most recent
    N rows regardless of when they happened. Each of those two features'
    own design docs name this gap explicitly and call for their own,
    separate read function when actually built — this one was correctly
    scoped to Ambient Intent Disambiguation's real need alone, not a
    one-size-fits-all query_log reader for every future feature.
    """
    try:
        # closing() guarantees the connection is released even when the
        # SELECT itself raises — previously the except below caught the
        # error and returned [], but the just-opened connection was
        # never closed and lingered until GC. Same fix applied across
        # every _connect() call site in this pass.
        with closing(_connect_log_db_readonly()) as con:
            rows = con.execute(
                "SELECT query, timestamp FROM query_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.Error as e:
        _LOGGER.debug("Could not read recent queries from query_log.db (likely none logged yet): %s", e)
        return []

# ---------------------------------------------------------------------------
# Synthetic-traffic cache write suppression
# ---------------------------------------------------------------------------
#
# Found via a deliberate cross-check while researching a separate design
# doc: adversarial_testing.py's run_adversarial_test_cycle() docstring
# claims it "never touches cache.json, routing_cache.json... or any real
# user-facing state" — but route_with_source() writes to BOTH _cache and
# _routing_cache (via _set_cached()/_set_routing(), deep inside the call
# graph: _resolve_single_source(), the fusion paths in route_with_source()
# itself, and _llm_detect()/_llm_pick_fusion_sources()) as an unconditional
# side effect of any successful query, synthetic or real. Confirmed
# directly with an unmocked call: a single synthetic adversarial query
# really does land in _cache and would really persist to cache.json on
# the next batched save. The existing test for this
# (test_cycle_never_touches_real_cache_files) only proved this couldn't
# happen because it mocks out route_with_source() entirely — it never
# actually exercised the real write path it claims to guard.
#
# _SUPPRESS_CACHE_WRITES is a contextvars.ContextVar, NOT a plain module
# global. This distinction is load-bearing, not a style preference: a
# plain global boolean would be a genuine, real race condition, not a
# theoretical one — main.py's lifespan runs adversarial testing on
# APScheduler's own BackgroundScheduler thread pool, fully concurrent
# with FastAPI's synchronous /search request handlers running on
# Starlette's own thread pool. A real live request's _set_cached() call
# landing in the exact window between adversarial testing setting and
# clearing a plain global flag would have its OWN legitimate write
# silently dropped — a strictly worse bug than the one this fixes.
# ContextVar is the correct primitive specifically because it is
# thread-local (and task-local under asyncio): one thread's call to
# suppress_cache_writes() is invisible to every other concurrently
# running thread, confirmed directly against this exact concern before
# choosing this approach over threading a parameter through every
# _set_cached()/_set_routing() call site in this file (there are 8,
# spanning _llm_detect(), _llm_pick_fusion_sources(),
# _resolve_single_source(), and two sites inside route_with_source()'s
# own fusion handling) — a far larger, riskier change to heavily-tested
# routing logic for what is fundamentally a "don't let synthetic test
# traffic touch production state" problem, not a routing problem.
_SUPPRESS_CACHE_WRITES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_SUPPRESS_CACHE_WRITES", default=False
)


@contextmanager
def suppress_cache_writes():
    """Suppress _set_cached()/_set_routing() writes for the duration of
    this context, scoped strictly to the current thread/task — see the
    module-level comment above for why a ContextVar, not a plain global,
    is required for this to be safe under real concurrency.

    Intended for exactly one real caller: adversarial_testing.py's
    run_adversarial_test_cycle(), so its synthetic, generated queries can
    run through the genuinely real route_with_source() pipeline (proving
    real routing/fallback/fusion behavior, which is the entire point of
    that feature) without any of those synthetic queries' results or
    routing decisions ever being written into cache.json/routing_cache.json
    — closing the real gap this comment block documents.

    Uses try/finally, not a bare set-then-clear, specifically so an
    exception raised anywhere inside the `with` block (a query that
    crashes mid-route, for instance — already a normal, expected,
    individually-caught case in run_adversarial_test_cycle()'s own loop)
    can never leave the suppression flag stuck on for this context
    afterward.
    """
    token = _SUPPRESS_CACHE_WRITES.set(True)
    try:
        yield
    finally:
        _SUPPRESS_CACHE_WRITES.reset(token)

INTENT_MAP = {
    "forecast": [
        "forecast", "weather", "tomorrow", "this weekend",
        "later today", "will it rain", "will it snow",
        "high temp", "low temp", "precipitation", "wind forecast",
        "going to be hot", "going to be cold",
        # Found via a deliberate investigation into a real
        # conditional_remainder benchmark anomaly (warm cache p99 worse
        # than cold — see wiki/The-Benchmark-Investigation-Log.md and
        # the conditional_remainder design doc): "is it raining" and
        # "is it going to rain" are real, natural phrasings that
        # genuinely mean weather, but matched none of this list's
        # existing rain-related triggers ("will it rain" requires the
        # word "will", not "is"). Confirmed via direct testing that
        # this previously fell through to kiwix's expensive LLM
        # book-selection path instead of the cheap, structured
        # forecast source it should have hit.
        "is it raining", "is it going to rain", "going to rain",
        "is it going to snow", "going to snow",
    ],
    "news": [
        "news", "headlines", "feeds", "rss",
        "recent articles", "my feeds",
    ],
    "web": [
        "search the web", "google", "look it up online",
        "current events", "web search", "find online",
        "who won", "search online",
    ],
    "ha": [
        "which lights are on", "lights on", "lights off", "lights status",
        "house status", "home status", "house summary",
        "are the doors locked", "door locked", "doors locked",
        "indoor air", "air quality", "indoor sensors",
        "how much power", "power consumption", "energy usage",
        "battery status", "battery levels", "low battery",
        "any motion", "recent motion", "motion detected",
        "security status", "house secure",
        "outdoor conditions", "outside conditions",
        # Found via the same conditional_remainder investigation as
        # forecast's "is it raining" gap above: "check if the doors
        # are locked" is a real, natural phrasing of the same question
        # "are the doors locked" already covers, but the inserted
        # "are" breaks ALL THREE of this list's existing door triggers
        # as a plain substring check ("are the doors locked", "door
        # locked", "doors locked" — none of them match "doors are
        # locked"). The same word-insertion class of bug this project
        # has already found and fixed once before (the GPIO/"on"
        # word-boundary issue) — confirmed via direct testing this
        # fell through to kiwix's expensive LLM book-selection path
        # instead of the cheap, structured ha source it should have
        # hit.
        "doors are locked", "door is locked",
        # Found while researching whether the "garage door"/"camera
        # offline" kiwix-double-hit pairs (see the conditional_remainder
        # design doc) were genuinely intentional kiwix routing or a real
        # gap — confirmed it's a real, two-file gap, not intentional:
        # "the garage door is open" is asking about OPEN/CLOSED state,
        # a question this list's existing door triggers never covered
        # at all (they're all locked/unlocked phrasing, a semantically
        # different question — a garage door has no lock state of its
        # own in most installations). Deliberately a SEPARATE trigger
        # from "door"/"doors locked" rather than widening those, since
        # "are the doors locked" has its own pinned test
        # (test_door_query_returns_lock_domain in
        # tests/test_home_assistant.py) confirming it stays lock-only —
        # this needed a new trigger, not a wider old one. Routing alone
        # isn't the whole fix: home_assistant.py's own _QUERY_MAP needed
        # a matching "garage" entry too, since a real cover/garage_door
        # entity wasn't reachable there either — see that file's own
        # comment on the new "garage" key for the second half of this
        # fix.
        "garage door", "garage",
    ],
    "changes": [
        "what changed", "any changes", "whats new", "what's new",
        "any new outages", "new outages", "any outages today",
        "weather change", "forecast change",
        "new articles", "new news", "new headlines",
        "anything different", "what happened today",
        "since last time", "changes today",
        "changed in the house", "house since", "anything changed",
        "while i was at work", "while i've been at work", "while at work",
        "since work", "since this morning", "this morning while",
        "since i left", "since i woke up",
    ],
    "uptime": [
        "uptime", "is down", "what's down", "whats down",
        "any outages", "service status", "network status",
        "are all services", "is everything up", "what is offline",
        "my services", "services up", "services down",
        "anything down", "everything down", "everything up",
        "network down", "network up",
        "what's offline", "whats offline", "anything offline",
        "any services", "check services",
        "is my network", "is the network",
        "server down", "server up", "server status",
        "is it running", "is it up", "is it down",
        "are they up", "are they down",
        # Found via the same conditional_remainder investigation as
        # forecast's "is it raining" gap above: "is everything online"
        # is a real, natural phrasing for the same uptime question
        # this list already covers via "is everything up", but had no
        # "online" variant at all. Confirmed via direct testing this
        # fell through to kiwix's expensive LLM book-selection path
        # instead. Bare "online" (not a longer phrase) deliberately —
        # checked it doesn't collide with web's own "look it up
        # online"/"find online" triggers, since INTENT_MAP matching is
        # query-contains-trigger, not the reverse, and neither of
        # web's phrases is a substring of "is everything online".
        "everything online", "is everything online", "anything online",
    ],
}

def _hours_since(hour_of_day: int) -> float:
    """Return the number of hours elapsed since the given hour today (local time).
    If that hour hasn't happened yet today, looks back to yesterday's occurrence.
    """
    from datetime import datetime, timedelta
    # Found via a deliberate "bulletproofing" pass: MORNING_START_HOUR
    # and WORK_START_HOUR are plain, unvalidated ints — a genuinely
    # natural config mistake (writing 24 for midnight, a common
    # 24-hour-notation convention) crashed this function with a raw
    # ValueError ("hour must be in 0..23") the moment any "this
    # morning"/"while at work" query needed it. Modulo 24 correctly
    # clamps 24 -> 0 specifically, while also sensibly handling any
    # other out-of-range value (negative hours wrap correctly too,
    # since Python's % always returns a non-negative result for a
    # positive divisor) rather than crashing on the next mistake found.
    hour_of_day = hour_of_day % 24
    now = datetime.now()
    target = now.replace(hour=hour_of_day, minute=0, second=0, microsecond=0)
    if target > now:
        target -= timedelta(days=1)
    elapsed = (now - target).total_seconds() / 3600
    return max(elapsed, 0.1)  # avoid zero/negative windows


def _resolve_changes_hours(query: str) -> float:
    """Resolve a changes query into a precise hours-since window.

    Time-window phrases are checked in order of specificity (most specific first)
    so "this morning while at work" doesn't get misread by a less specific match.
    """
    q = query.lower()

    # Explicit hour count — "in the last 3 hours", "in the past 2 hours"
    #
    # Found via a deliberate complexity-investigation pass: the original
    # regex (r"(\d+)\s*hour") matched ANY number adjacent to the word
    # "hour", regardless of context — a real, reachable compound query
    # like "any updates on my 3 hour delay flight, also what changed
    # today" would incorrectly resolve to a 3-hour window from the
    # unrelated "3 hour delay" phrase, silently ignoring the user's
    # actual, more relevant "today" signal and searching a window 8x
    # narrower than intended. Confirmed reachable: this source's
    # keyword routing is a substring match, not an exact-phrase
    # requirement, so any query containing a recognized "changes"
    # trigger anywhere (e.g. "what changed") routes here regardless of
    # what else the query mentions. Fixed by requiring an actual window
    # phrase (last/past/in) immediately before the number, rather than
    # treating any nearby number as a time-window request — verified
    # this doesn't reject genuine window phrasings ("in the last 3
    # hours", "in the past 2 hours", "in the last 5 hours or so") while
    # correctly rejecting both the original false-positive case and a
    # second one found during the same investigation ("24 hour clock
    # display").
    if "hour" in q:
        m = re.search(r"(?:last|past|in the last|in the past|within the last)\s*(\d+)\s*hour", q)
        if m:
            return float(m.group(1))

    # Specific time-of-day phrases — resolved against configured start hours
    if "this morning" in q or "since morning" in q or "since this morning" in q:
        return _hours_since(settings.morning_start_hour)

    if "at work" in q or "since work" in q or "while at work" in q or "while i was at work" in q or "while i've been at work" in q:
        return _hours_since(settings.work_start_hour)

    if "tonight" in q or "this evening" in q:
        return _hours_since(18)

    # Broader windows
    if "yesterday" in q or "since yesterday" in q:
        return 48.0

    if "week" in q:
        return 168.0

    if "today" in q:
        return 24.0

    # Default — no specific window detected
    return 24.0


def _search_changes(query: str) -> str:
    """Search changes source — returns detected changes from snapshots,
    resolving natural language time-window phrases into precise hour windows."""
    hours = _resolve_changes_hours(query)
    detected = get_changes(since_hours=hours)
    return format_changes(detected, since_hours=round(hours, 1))


SOURCE_MAP = {
    "kiwix": kiwix.search,
    "forecast": forecast.search,
    "news": freshrss.search,
    "web": searxng.search,
    "uptime": uptime_kuma.search,
    "ha": home_assistant.search,
    "changes": _search_changes,
    "fusion": None,  # handled specially in route() — accepts fusion_sources list
}

SOURCE_DESCRIPTIONS = {
    "kiwix": "Offline knowledge base — Wikipedia, Stack Exchange, iFixit, FreeCodeCamp, DevDocs. Use for factual, encyclopedic, or technical questions.",
    "forecast": "3-day weather forecast. Use for any question about future weather conditions, temperature, rain, wind, or sunrise/sunset.",
    "news": "Recent RSS news articles from the user's feeds. Use for current events, headlines, or recent news.",
    "web": "Live web search via SearXNG. Use for current events, recent information, or anything that may have changed recently.",
    "uptime": "Uptime Kuma monitor status. Use when asked about service status, what is down, or network health.",
    "ha": "Home Assistant entity states. Use for house status summaries, which lights are on, door and lock status, battery levels, indoor sensors, motion events, or power consumption.",
    "changes": "Snapshot diff engine. Use when asked what changed, any new outages, weather changes, or new news since a given time.",
    "fusion": "Multi-source fusion — queries multiple sources concurrently and merges results. Use for complex queries that benefit from combining offline knowledge, live web, and recent news.",
}

# Fallback chain — if a source returns no results, try these in order
FALLBACK_CHAIN = {
    "kiwix": "web",
    "news": "web",
}

# Cache TTL in seconds per source. Built from individual settings rather
# than hardcoded — found via a deliberate config-completeness audit: this
# whole dict was previously fixed, despite the wiki's Caching page
# presenting these exact numbers as deliberate, reasoned defaults someone
# might want to tune for their own deployment's freshness needs. Every
# existing `CACHE_TTL.get(source, ...)` call site keeps working unchanged,
# since only the values backing this dict became configurable, not its
# shape or how it's accessed.
CACHE_TTL = {
    "kiwix": settings.cache_ttl_kiwix_seconds,
    "forecast": settings.cache_ttl_forecast_seconds,
    "news": settings.cache_ttl_news_seconds,
    "web": settings.cache_ttl_web_seconds,
    "uptime": settings.cache_ttl_uptime_seconds,
    "ha": settings.cache_ttl_ha_seconds,
    "changes": settings.cache_ttl_changes_seconds,
    "fusion": settings.cache_ttl_fusion_seconds,
}

# ---------------------------------------------------------------------------
# Routing cache — stores source and book selection decisions to skip LLM calls
# ---------------------------------------------------------------------------

ROUTING_CACHE_FILE = "/app/data/routing_cache.json"
# Found via a deliberate config-completeness audit: presented as a
# deliberate, reasoned default in the wiki's Caching page, but previously
# impossible to actually adjust.
ROUTING_CACHE_TTL = settings.routing_cache_ttl_seconds
_routing_cache: dict[str, tuple[str, float]] = {}
_ROUTING_CACHE_MAX_SIZE: int = settings.routing_cache_max_size  # max entries before evicting oldest
_routing_dirty_count: int = 0
# Save to disk every N writes — the identical batching the result cache
# (_CACHE_SAVE_INTERVAL below) has always had. Found via a deliberate
# function-by-function audit: _set_routing() previously called
# _save_routing_cache() unconditionally on EVERY single write, meaning
# every cold routing decision (LLM source pick, book selection,
# disambiguation candidates, alternate phrasing — four separate writers)
# serialized the ENTIRE routing cache (up to _ROUTING_CACHE_MAX_SIZE =
# 1000 entries) to disk as one full JSON dump, per write. Under exactly
# the workload where routing writes cluster (a cold-cache benchmark or
# any burst of genuinely new queries), that's the same full-file write
# amplified once per decision, on the same request-serving threads. The
# result cache accepted the identical tradeoff (losing up to
# INTERVAL-1 recent writes on a hard crash, in exchange for 5x fewer
# full-file writes) from the start; there was never a stated reason for
# the routing cache to be different — the wiki's own Caching page
# describes the batching as the shared persistence design.
_ROUTING_CACHE_SAVE_INTERVAL: int = 5


def _routing_cache_key(query: str) -> str:
    return query.lower().strip()


def _get_routing(query: str) -> str | None:
    """Return cached routing decision for query, or None if not cached/expired."""
    key = _routing_cache_key(query)
    if key in _routing_cache:
        decision, timestamp = _routing_cache[key]
        if time.time() - timestamp < ROUTING_CACHE_TTL:
            _LOGGER.info("Routing cache hit for query: '%s' -> %s", query[:50], decision)
            return decision
        else:
            del _routing_cache[key]
    return None


def _evict_oldest_routing() -> None:
    """Remove the oldest routing cache entry.

    Found via real usage — this cache had NO size limit at all until this
    was added. The result cache (_cache) already had this exact pattern;
    the routing cache's key space is genuinely larger in practice (every
    unique conditional query, discourse-framing phrase, and disambiguation
    candidate set gets its own entry on top of plain source-routing
    decisions), making unbounded growth over sustained real-world usage
    a real, not just theoretical, concern.
    """
    if not _routing_cache:
        return
    oldest_key = min(_routing_cache, key=lambda k: _routing_cache[k][1])
    del _routing_cache[oldest_key]
    _LOGGER.debug("Evicted oldest routing cache entry: %s", oldest_key)


def _set_routing(query: str, decision: str) -> None:
    """Cache a routing decision for a query.

    No-ops if called inside a suppress_cache_writes() context — see that
    function's docstring; this is the actual enforcement point for the
    real, found gap it documents.
    """
    global _routing_dirty_count
    if _SUPPRESS_CACHE_WRITES.get():
        return
    key = _routing_cache_key(query)
    # Evict oldest if at capacity (and this is a new entry)
    if key not in _routing_cache and len(_routing_cache) >= _ROUTING_CACHE_MAX_SIZE:
        _evict_oldest_routing()
        _LOGGER.info("Routing cache at max size (%d), evicted oldest entry", _ROUTING_CACHE_MAX_SIZE)
    _routing_cache[key] = (decision, time.time())
    _routing_dirty_count += 1
    _LOGGER.info("Cached routing decision: '%s' -> %s", query[:50], decision)
    # Batched persistence — see _ROUTING_CACHE_SAVE_INTERVAL's own
    # comment above for why this now mirrors _set_cached()'s existing
    # batching rather than writing the full file on every decision.
    if _routing_dirty_count >= _ROUTING_CACHE_SAVE_INTERVAL:
        _save_routing_cache()
        _routing_dirty_count = 0


def _atomic_write_json(filepath: str, data: dict) -> None:
    """Write `data` as JSON to `filepath` without ever leaving it in a
    truncated or corrupted state, even under real concurrent writers.

    Found via a real investigation, not written defensively up front:
    researching whether searxng.py's two sequential fetches (primary +
    query-expansion alternate) could safely run concurrently led to
    auditing every writer of the routing cache, which surfaced this —
    both _save_routing_cache() and _save_cache() previously did a bare
    `open(path, "w")` followed by `json.dump()`. FastAPI's /search
    endpoint is a SYNC route, meaning Starlette already runs concurrent
    real requests on its own thread pool today — two requests landing
    close enough together to both trigger a cache save is a real,
    already-possible scenario, not a hypothetical one this change
    introduces.

    The actual risk isn't really the in-memory dict (individual dict
    operations are already safe under the GIL) — it's the FILE. If
    thread A's `open(path, "w")` truncates the file and thread B's
    `open(path, "w")` ALSO truncates it before thread A's `json.dump()`
    finishes writing, the file can end up malformed or missing data
    entirely. The existing `load_routing_cache()`/load-cache logic
    already catches a `json.JSONDecodeError` and falls back to an
    empty cache rather than crashing — so the real-world blast radius
    of this bug was never a hard crash, just silently losing the
    ENTIRE on-disk cache (not just one entry) on the next restart,
    which is still a real, avoidable cost worth fixing properly rather
    than accepting.

    Fixed with the standard, well-established pattern for this exact
    problem: write to a temporary file in the SAME directory (so it's
    guaranteed to be on the same filesystem — required for the next
    step to actually be atomic), then `os.replace()` it onto the real
    target path. `os.replace()` is atomic on POSIX (which is what this
    project actually runs on — Docker containers on Linux): at every
    point in time, the target path either has the complete OLD content
    or the complete NEW content, never a partial write from either
    writer, no matter how two concurrent calls interleave. The
    temporary file's name includes the PID to avoid two concurrent
    calls colliding with each other's temp file before either reaches
    the replace step.
    """
    directory = os.path.dirname(filepath) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(filepath)}.", suffix=f".{os.getpid()}.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
            # fsync BEFORE the replace — without it, os.replace() gives
            # crash-CONSISTENCY (the path always names a complete old or
            # complete new file with respect to concurrent writers) but
            # not crash-DURABILITY: on a power loss shortly after the
            # rename, some filesystems can commit the rename before the
            # temp file's data blocks reach disk, leaving the path
            # pointing at a truncated or empty file. Flushing Python's
            # buffer and fsyncing the file descriptor first guarantees
            # the bytes are on disk before the rename can make them the
            # canonical copy. The cost — one synchronous disk flush —
            # was material back when the routing cache wrote its entire
            # file on every single routing decision, but both caches now
            # batch their saves (every _CACHE_SAVE_INTERVAL /
            # _ROUTING_CACHE_SAVE_INTERVAL writes), so this fires a
            # handful of times a minute at most, on background-ish
            # paths, in exchange for backups and restarts never finding
            # a zero-length cache file after an unlucky power cut.
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
    except Exception:
        # Clean up the temp file on any failure — os.replace() never
        # ran, so it's still sitting there under its own, never-
        # collided-with name and won't be picked up by anything else.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _save_routing_cache() -> None:
    """Persist routing cache to disk.

    Uses _atomic_write_json() rather than a bare open()+json.dump() —
    found via a real investigation into whether two of searxng.py's
    own sequential fetches could safely run concurrently, which
    surfaced a real, pre-existing file-write race here. See that
    function's own docstring for the full mechanism and why it matters.
    """
    try:
        _atomic_write_json(ROUTING_CACHE_FILE, _routing_cache)
    except Exception as e:
        _LOGGER.warning("Could not save routing cache to disk: %s", e)


def load_routing_cache() -> None:
    """Load routing cache from disk on startup."""
    global _routing_cache
    try:
        if not os.path.exists(ROUTING_CACHE_FILE):
            return
        with open(ROUTING_CACHE_FILE, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            _routing_cache.clear()
            return
        now = time.time()
        loaded = {}
        for key, value in raw.items():
            try:
                if not isinstance(value, list) or len(value) != 2:
                    continue
                decision, timestamp = value
                if not isinstance(decision, str) or not isinstance(timestamp, (int, float)):
                    continue
                if now - float(timestamp) < ROUTING_CACHE_TTL:
                    loaded[key] = (decision, float(timestamp))
            except Exception:
                continue
        _routing_cache.clear()
        _routing_cache.update(loaded)
        # Defensive cap on load too — in practice this rarely matters
        # since ROUTING_CACHE_TTL (1 hour) means anything surviving the
        # expiry filter above was written recently anyway, but a disk
        # file saved before _ROUTING_CACHE_MAX_SIZE existed could
        # theoretically still be over the limit. Keep only the most
        # recently-written entries if so, rather than silently allowing
        # an over-limit cache to persist across a restart.
        if len(_routing_cache) > _ROUTING_CACHE_MAX_SIZE:
            newest_keys = sorted(
                _routing_cache, key=lambda k: _routing_cache[k][1], reverse=True
            )[:_ROUTING_CACHE_MAX_SIZE]
            _routing_cache = {k: _routing_cache[k] for k in newest_keys}
            _LOGGER.info(
                "Routing cache loaded from disk exceeded max size, trimmed to %d most recent entries",
                _ROUTING_CACHE_MAX_SIZE
            )
        _LOGGER.info("Loaded %d routing cache entries from disk", len(_routing_cache))
    except json.JSONDecodeError as e:
        _LOGGER.warning("Routing cache corrupted: %s, starting fresh", e)
        _routing_cache.clear()
    except Exception as e:
        _LOGGER.warning("Could not load routing cache: %s", e)
        _routing_cache.clear()


def get_routing_cache_stats() -> list[dict]:
    """Return routing cache entries with age and expiry info."""
    now = time.time()
    entries = []
    for key, (decision, timestamp) in _routing_cache.items():
        age = int(now - timestamp)
        entries.append({
            "query": key,
            "decision": decision,
            "age_seconds": age,
            "ttl_seconds": ROUTING_CACHE_TTL,
            "expires_in": max(0, ROUTING_CACHE_TTL - age),
        })
    return entries


def clear_routing_cache() -> int:
    """Clear all routing cache entries. Returns count removed."""
    global _routing_dirty_count
    count = len(_routing_cache)
    _routing_cache.clear()
    _routing_dirty_count = 0
    _save_routing_cache()
    return count


# ---------------------------------------------------------------------------
# Per-key in-flight deduplication ("singleflight") for the routing cache
# ---------------------------------------------------------------------------
#
# Closes a real, confirmed thundering-herd gap: _get_routing()'s read and
# the matching _set_routing() write that follows a cold LLM call have no
# protection between them, so two concurrent callers for the identical
# uncached key (e.g. two of 20 Locust users both drawing "what is
# nitrogen" before either's routing decision has been cached) both miss,
# both pay the full ~700-1000ms LLM cost, and both eventually write the
# same decision. See wiki/Caching.md's "Routing cache" section for the
# mechanism this implements and wiki/The-Benchmark-Investigation-Log.md's
# Thread 2 for the benchmark history this addresses — confirmed directly
# against this codebase, not assumed.
#
# A _RefCountedLock wrapper, not a bare threading.Lock, because the
# simpler "delete a key's lock from the registry once it's no longer
# held" approach has a real, named race: thread C could fetch a
# reference to a key's lock between thread B's release() and the
# dict-cleanup check, then thread D calls _get_inflight_lock() after
# that cleanup deletes the entry, creating a FRESH lock object for the
# same key — C and D would then hold two different lock objects for one
# logical key and neither would ever see the other. Refcounting closes
# this: every mutation of _inflight_locks (create-or-get, increment,
# decrement-and-maybe-delete) happens while holding
# _inflight_locks_guard, so "is anyone else still referencing this key's
# lock" is always answered atomically with whatever action is about to
# be taken on the dict, never as a separate, racy follow-up check on a
# dict that could have changed in between.
class _RefCountedLock:
    """A threading.Lock plus a count of how many callers currently hold
    a reference to it (whether or not they've actually called acquire()
    yet) — used so _release_inflight_lock() can tell, atomically, whether
    it's safe to remove this key's entry from _inflight_locks or whether
    a concurrent caller is still relying on this exact object.
    """
    __slots__ = ("lock", "refcount")

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.refcount = 0


_inflight_locks: dict[str, _RefCountedLock] = {}
_inflight_locks_guard = threading.Lock()  # protects _inflight_locks itself


def _get_inflight_lock(key: str) -> _RefCountedLock:
    """Return the (possibly newly-created) _RefCountedLock for `key`,
    incrementing its refcount under the same guard that creates it — so
    a caller is guaranteed the entry can't be cleaned up out from under
    it between this call and the matching _release_inflight_lock() call.

    Every caller of this function MUST call _release_inflight_lock()
    exactly once when done, in a finally block, regardless of whether
    the work in between succeeded, failed, or raised.
    """
    with _inflight_locks_guard:
        entry = _inflight_locks.get(key)
        if entry is None:
            entry = _RefCountedLock()
            _inflight_locks[key] = entry
        entry.refcount += 1
        return entry


def _release_inflight_lock(key: str, entry: _RefCountedLock) -> None:
    """Release the lock acquired via _get_inflight_lock(), and remove
    `key`'s entry from _inflight_locks if this was the last caller still
    referencing it.

    The refcount decrement and the removal check happen under the same
    _inflight_locks_guard acquisition specifically so no other thread can
    call _get_inflight_lock() for this key in between (which would
    otherwise either resurrect a key that's about to be deleted, or read
    a refcount this function is mid-update on) — eliminating the race a
    simpler "delete if not locked" approach would have left open.
    """
    entry.lock.release()
    with _inflight_locks_guard:
        entry.refcount -= 1
        if entry.refcount <= 0 and _inflight_locks.get(key) is entry:
            del _inflight_locks[key]


def get_inflight_lock_count() -> int:
    """Return the number of routing-cache keys currently mid-resolution.

    Exposed for tests and /health-style introspection — should be 0 in
    steady state between requests; a persistently nonzero count would
    indicate a real leak (a caller that acquired but never released).
    """
    with _inflight_locks_guard:
        return len(_inflight_locks)


@contextmanager
def _singleflight(key: str):
    """Block until no other caller is resolving `key`, then hold the
    in-flight lock for the duration of the `with` block.

    Callers must re-check the routing cache immediately after entering
    this context — a concurrent caller may have already resolved and
    cached `key` while this one was waiting to acquire the lock, in
    which case the right move is to use that cached value, not pay the
    LLM cost a second time. This context manager only provides mutual
    exclusion; it deliberately doesn't do the re-check itself, since
    each of the four call sites' "what does a cache hit look like"
    logic is genuinely different (plain source string vs comma-joined
    fusion list vs pipe-joined disambiguation candidates) and trying to
    generalize that here would either need a callback parameter at every
    call site or silently assume one shape — re-checking inline at each
    site, the same few lines already used for the FIRST check, is both
    simpler and clearly correct by inspection.

    Per-key, not a single global lock: a `forecast` query and an
    unrelated `kiwix` disambiguation lookup never compete for the same
    routing-cache key, so they must never block each other either. Only
    callers racing for the IDENTICAL key actually queue.
    """
    entry = _get_inflight_lock(key)
    entry.lock.acquire()
    try:
        yield
    finally:
        _release_inflight_lock(key, entry)


# In-memory cache: key -> (result, timestamp)
_cache: dict[str, tuple[str, float]] = {}
_cache_dirty_count: int = 0
_CACHE_SAVE_INTERVAL: int = 5   # save to disk every N writes
_CACHE_MAX_SIZE: int = settings.cache_max_size  # max entries before evicting oldest


# ---------------------------------------------------------------------------
# Cache internals
# ---------------------------------------------------------------------------

def _cache_key(source: str, query: str) -> str:
    return f"{source}:{query.lower().strip()}"


def _get_cached(source: str, query: str) -> str | None:
    key = _cache_key(source, query)
    if key in _cache:
        result, timestamp = _cache[key]
        ttl = CACHE_TTL.get(source, 3600)
        if time.time() - timestamp < ttl:
            _LOGGER.info("Cache hit for source='%s' query='%s'", source, query[:50])
            _route_stat("served_any_from_cache", True)
            _route_event("result_cache_hit", source=source, query=query)
            return result
        else:
            del _cache[key]
    return None


def _evict_oldest() -> None:
    """Remove the oldest cache entry."""
    if not _cache:
        return
    oldest_key = min(_cache, key=lambda k: _cache[k][1])
    del _cache[oldest_key]
    _LOGGER.debug("Evicted oldest cache entry: %s", oldest_key)


def _set_cached(source: str, query: str, result: str) -> None:
    """Cache a result for a source+query.

    No-ops if called inside a suppress_cache_writes() context — see
    that function's docstring; this is the actual enforcement point for
    the real, found gap it documents.
    """
    global _cache_dirty_count
    if _SUPPRESS_CACHE_WRITES.get():
        return
    key = _cache_key(source, query)
    # Evict oldest if at capacity (and this is a new entry)
    if key not in _cache and len(_cache) >= _CACHE_MAX_SIZE:
        _evict_oldest()
        _LOGGER.info("Cache at max size (%d), evicted oldest entry", _CACHE_MAX_SIZE)
    _cache[key] = (result, time.time())
    _cache_dirty_count += 1
    _LOGGER.info("Cached result for source='%s' query='%s'", source, query[:50])
    if _cache_dirty_count >= _CACHE_SAVE_INTERVAL:
        _save_cache()
        _cache_dirty_count = 0


def _save_cache() -> None:
    """Persist cache to disk.

    Uses _atomic_write_json() — the identical race _save_routing_cache()
    had, fixed the same way; see that function's own comment and
    _atomic_write_json()'s docstring for the full investigation.
    """
    try:
        _atomic_write_json(CACHE_FILE, _cache)
    except Exception as e:
        _LOGGER.warning("Could not save cache to disk: %s", e)


def _looks_empty(result: str) -> bool:
    # Genuinely shared with fusion.py's own _looks_empty() — found via a
    # second, deliberate "bulletproofing" re-pass that these were two
    # separate, independently-maintained copies with an overlapping but
    # NOT identical phrase list, a real, significant drift (this
    # module's own copy was missing "not configured" and "could not
    # connect", meaning FALLBACK_CHAIN's real "news" -> "web" fallback
    # never triggered when FreshRSS was genuinely unconfigured — see
    # fusion.py's docstring for the full account, including the
    # opposite-direction gaps found in fusion.py's own list too).
    return fusion._looks_empty(result)


# ---------------------------------------------------------------------------
# Cache public API
# ---------------------------------------------------------------------------

def load_cache() -> None:
    """Load cache from disk on startup."""
    global _cache
    try:
        if not os.path.exists(CACHE_FILE):
            _LOGGER.info("No cache file found at %s, starting fresh", CACHE_FILE)
            return
        with open(CACHE_FILE, "r") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            _LOGGER.warning("Cache file malformed (not a dict), starting fresh")
            _cache = {}
            return
        now = time.time()
        loaded = {}
        skipped = 0
        for key, value in raw.items():
            try:
                # Validate structure: must be [result_str, timestamp_float]
                if not isinstance(value, list) or len(value) != 2:
                    skipped += 1
                    continue
                result, timestamp = value
                if not isinstance(result, str) or not isinstance(timestamp, (int, float)):
                    skipped += 1
                    continue
                source = key.split(":")[0]
                ttl = CACHE_TTL.get(source, 3600)
                if now - timestamp < ttl:
                    loaded[key] = (result, float(timestamp))
            except Exception:
                skipped += 1
                continue
        _cache = loaded
        if skipped:
            _LOGGER.warning("Skipped %d malformed cache entries on load", skipped)
        _LOGGER.info("Loaded %d cache entries from disk", len(_cache))
    except json.JSONDecodeError as e:
        _LOGGER.warning("Cache file corrupted (JSON error: %s), starting fresh", e)
        _cache = {}
        # Rename corrupted file for inspection
        try:
            corrupt_path = CACHE_FILE + ".corrupt"
            os.rename(CACHE_FILE, corrupt_path)
            _LOGGER.info("Moved corrupted cache to %s", corrupt_path)
        except Exception:
            pass
    except Exception as e:
        _LOGGER.warning("Could not load cache from disk: %s", e)
        _cache = {}


def check_cached(source: str, query: str) -> bool:
    """Return True if a valid cached result exists for this source+query."""
    return _get_cached(source, query) is not None


def get_cache_stats() -> list[dict]:
    """Return cache entries with age and expiry info."""
    now = time.time()
    entries = []
    for key, (result, timestamp) in _cache.items():
        source, query = key.split(":", 1)
        ttl = CACHE_TTL.get(source, 3600)
        age = int(now - timestamp)
        entries.append({
            "source": source,
            "query": query,
            "age_seconds": age,
            "ttl_seconds": ttl,
            "expires_in": max(0, ttl - age),
        })
    return entries


def get_cache_count() -> int:
    """Return number of cache entries."""
    return len(_cache)


def clear_cache() -> int:
    """Clear all cache entries and persist to disk. Returns count removed."""
    global _cache_dirty_count
    count = len(_cache)
    _cache.clear()
    _cache_dirty_count = 0
    _save_cache()
    return count


def flush_caches() -> None:
    """Persist both the result cache and the routing cache to disk
    immediately, regardless of either dirty counter's current value.

    Exists for exactly one caller: main.py's lifespan shutdown. Both
    caches batch their disk writes (every _CACHE_SAVE_INTERVAL /
    _ROUTING_CACHE_SAVE_INTERVAL writes), which means up to
    INTERVAL-1 recent entries were always in memory only at any given
    moment — acceptable for a hard crash (each lost entry is just a
    re-derivable source result or LLM decision), but a clean shutdown
    (docker stop, a deploy restart) shouldn't discard them when one
    cheap final write preserves everything.
    """
    global _cache_dirty_count, _routing_dirty_count
    _save_cache()
    _cache_dirty_count = 0
    _save_routing_cache()
    _routing_dirty_count = 0


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

# Phrases signaling that a query frames its (possibly encyclopedic) topic
# as current public discourse rather than a pure knowledge lookup — "what's
# the deal with X everyone keeps talking about" reads to a small local LLM
# almost word-for-word onto news/web's own descriptions ("current events",
# "recent information"), since kiwix's description ("factual, encyclopedic,
# or technical questions") gives no signal that an evergreen topic can
# ALSO be currently trending in public conversation. Found via extensive
# real-usage testing: "mercury retrogade", "galaxy", "bitcoin", and "black
# holes" all reproducibly routed past kiwix to news/web when phrased this
# way, even though several of these are genuinely encyclopedic topics
# kiwix's disambiguation-backed search is well suited to answer.
#
# Rather than editing SOURCE_DESCRIPTIONS to nudge the LLM's free-text
# judgment (an indirect, unverifiable lever), this is detected explicitly
# and biases the routing decision directly: if discourse-framing language
# is present and kiwix wasn't already part of the LLM's chosen source(s),
# kiwix is added and the result escalated to fusion. This doesn't override
# or discard whatever the LLM found useful (web/news content was often
# genuinely relevant in testing) — it guarantees kiwix gets a real chance
# to contribute its disambiguation-backed encyclopedic answer alongside it,
# rather than being silently excluded for these specific phrasings.
#
# kiwix.py is the canonical source of this list — it ALSO uses these
# patterns to strip the discourse-framing phrase before building Kiwix
# search terms, since "everyone"/"obsessed"/"talking"/"keep" surviving as
# literal search noise was found to matter just as much as the routing
# decision itself (kiwix matching "Howard Wolowitz" for a bitcoin query
# because the search string was "what whole bitcoin everyone obsessed").
# Importing from kiwix.py rather than duplicating avoids drift between
# the two — router.py safely imports FROM kiwix (it already does, via
# `from app.sources import kiwix`), the reverse would be circular.
_DISCOURSE_FRAMING_PATTERNS = kiwix.DISCOURSE_FRAMING_PATTERNS


def _has_discourse_framing(query: str) -> bool:
    """Return True if the query frames its topic as current public
    discourse ("everyone keeps talking about X") rather than a pure
    knowledge lookup. See _DISCOURSE_FRAMING_PATTERNS for the rationale."""
    q = query.lower()
    return any(p in q for p in _DISCOURSE_FRAMING_PATTERNS)


def _keyword_detect(query: str) -> str | list[str] | None:
    """Fast keyword-based intent detection.

    Returns:
    - A single source name if only one source matches
    - A list of source names if multiple sources match (auto-escalates to fusion)
    - None if no keywords match
    """
    query_lower = query.lower()
    matched = []
    for source, triggers in INTENT_MAP.items():
        for trigger in triggers:
            if trigger in query_lower:
                if source not in matched:
                    matched.append(source)
                    _LOGGER.info(
                        "Keyword intent: '%s' matched trigger '%s' -> %s",
                        query[:50], trigger, source
                    )
                break  # one trigger per source is enough

    if not matched:
        return None
    if len(matched) == 1:
        return matched[0]

    _LOGGER.info("Multi-keyword match for '%s' -> escalating to fusion: %s", query[:50], matched)
    return matched


def _escalate_multi_source_for_discourse_framing(query: str, sources: list[str]) -> list[str]:
    """
    If the query has discourse-framing language and kiwix isn't already
    among the given sources, add it.

    Extracted from _llm_detect(), where this exact pattern appeared
    twice — once for a cached fusion decision, once for a fresh
    multi-source LLM decision. Confirmed via direct testing that NOT
    re-caching the escalated result after this runs is correct, not an
    oversight: _has_discourse_framing() is re-evaluated fresh on every
    call regardless of what's cached, so the escalation self-heals on
    every request rather than depending on whether a stale cache entry
    happened to bake the bias in — verified directly that a query whose
    cached decision predates this bias still correctly escalates on
    every subsequent call, not just the first.
    """
    if _has_discourse_framing(query) and "kiwix" not in sources:
        return sources + ["kiwix"]
    return sources


def _escalate_single_source_for_discourse_framing(query: str, source: str) -> list[str] | None:
    """
    If the query has discourse-framing language and the given source
    isn't kiwix, return a [source, "kiwix"] list to escalate to fusion.
    Returns None if no escalation is needed (caller should keep using
    the plain single source in that case).

    Extracted from _llm_detect() alongside
    _escalate_multi_source_for_discourse_framing() — this is the
    single-source counterpart, also previously duplicated twice (once
    for a cached single-source decision, once for a fresh one).
    """
    if _has_discourse_framing(query) and source != "kiwix":
        return [source, "kiwix"]
    return None


def _interpret_cached_source_decision(query: str, cached: str) -> str | list[str] | None:
    """Interpret a routing-cache hit for `source:{query}` into the same
    shape _llm_detect() returns, applying the discourse-framing bias the
    same way a fresh decision would get it. Returns None if `cached`
    doesn't actually resolve to anything usable (a malformed or stale
    entry), in which case the caller should treat this as a miss.

    Extracted so _llm_detect() can call this identically both before
    acquiring the in-flight lock (the fast, common-case path) and again
    immediately after acquiring it (the re-check a concurrent caller
    that finished resolving this key while we were waiting needs) —
    without duplicating the discourse-framing escalation logic at both
    call sites, which would otherwise be exactly the kind of
    byte-for-byte duplication this project's own complexity-reduction
    passes have repeatedly found and fixed real drift in elsewhere
    (e.g. _fallback_book_choice() in kiwix.py).
    """
    if "," in cached:
        # A cached fusion decision
        sources = [s.strip() for s in cached.split(",") if s.strip() in SOURCE_MAP and s.strip() != "fusion"]
        if sources:
            sources = _escalate_multi_source_for_discourse_framing(query, sources)
            _LOGGER.info("Routing cache hit (fusion): '%s' -> %s", query[:50], sources)
            return sources
        return None
    if cached in SOURCE_MAP:
        # Apply the same discourse-framing bias to a cached decision —
        # otherwise a routing cache entry written before this fix
        # existed (or before kiwix was added) would silently bypass it
        # for up to its full TTL, since the cache check above returns
        # before the bias logic further down ever runs.
        escalated = _escalate_single_source_for_discourse_framing(query, cached)
        if escalated is not None:
            _LOGGER.info(
                "Routing cache hit but discourse-framing detected — escalating '%s' to fusion: %s",
                query[:50], escalated
            )
            return escalated
        _LOGGER.info("Routing cache hit: '%s' -> %s", query[:50], cached)
        return cached
    return None


def _llm_detect(query: str) -> str | list[str]:
    """Ask LLM to pick the best source(s) for the query.

    Returns a single source name for focused queries, or a list of source names
    for complex multi-topic queries that benefit from fusion.

    The LLM decides in one call — if it returns a comma-separated list, the
    caller will trigger fusion automatically.

    Checks routing cache first to avoid redundant LLM calls.
    Falls back to 'kiwix' if LLM is not configured or returns invalid sources.

    Singleflight: a second concurrent caller for the same uncached query
    blocks on _singleflight() rather than also paying the LLM cost — see
    the module-level comment above _RefCountedLock/_inflight_locks for
    the full mechanism and why a per-key lock (not a global one) is the
    right shape here.
    """
    from app.llm import complete, is_configured

    cache_key = f"source:{query}"

    # Check routing cache first — cheap read, stays outside any lock so
    # a warm hit (the overwhelming majority of real traffic) never pays
    # any lock overhead at all.
    cached = _get_routing(cache_key)
    if cached:
        result = _interpret_cached_source_decision(query, cached)
        if result is not None:
            _route_event("intent_cached", query=query, decision=cached)
            return result

    with _singleflight(cache_key):
        # Re-check now that we hold the lock — a concurrent caller may
        # have already resolved and cached this exact query while we
        # were waiting to acquire it, in which case use that instead of
        # paying for a second, redundant LLM call.
        cached = _get_routing(cache_key)
        if cached:
            result = _interpret_cached_source_decision(query, cached)
            if result is not None:
                _route_event("intent_cached", query=query, decision=cached)
                return result

        # Semantic routing cache — before paying the LLM call, check
        # whether a SEMANTICALLY equivalent query already has a live
        # routing decision the exact-match cache can't see ("will it
        # rain later" vs "will it rain this evening"). Sits inside the
        # singleflight deliberately: concurrent identical queries still
        # coordinate on one lookup, and a hit writes the exact-match
        # entry below so every subsequent identical phrasing is a plain
        # cache hit that never reaches this code at all. Every failure
        # mode (feature off, embedding call failed, empty store, nothing
        # above threshold) returns (None, maybe-vector) and falls
        # through to exactly what v3.52.0 did — see the design
        # constraints in app/semantic_routing.py's module docstring.
        semantic_match, query_vector = semantic_routing.find_similar(
            query, lambda q: _get_routing(f"source:{q}")
        )
        if semantic_match is not None:
            result = _interpret_cached_source_decision(query, semantic_match.decision)
            if result is not None:
                _route_stat("semantic_routing_hit", True)
                _route_event(
                    "intent_semantic",
                    query=query,
                    matched_query=semantic_match.matched_query,
                    similarity=round(semantic_match.similarity, 3),
                    decision=semantic_match.decision,
                )
                _LOGGER.info(
                    "Semantic routing reuse: '%s' -> %s (via '%s')",
                    query[:50], semantic_match.decision, semantic_match.matched_query[:50],
                )
                # Promote to a normal exact-match entry so this precise
                # phrasing never pays even the embedding call again, and
                # make the new phrasing itself matchable for the NEXT
                # rephrasing (the vector was already computed for the
                # lookup — storing it is free).
                _set_routing(cache_key, semantic_match.decision)
                semantic_routing.store(query, query_vector)
                return result

        if not is_configured():
            _route_event("intent_default", query=query, decision="kiwix",
                         reason="llm_not_configured")
            return "kiwix"

        source_list = "\n".join(
            f"- {name}: {desc}"
            for name, desc in SOURCE_DESCRIPTIONS.items()
            if name != "fusion"
        )

        prompt = (
            f"You are a search router. Given a user query and a list of available search sources, "
            f"return the best source or sources.\n\n"
            f"Rules:\n"
            f"- For a focused single-topic query: return ONLY one source name\n"
            f"- For a complex or multi-topic query that needs multiple sources: return 2-3 source names separated by commas\n"
            f"- No explanation, no punctuation except commas between source names\n\n"
            f"Query: {query}\n\n"
            f"Available sources:\n{source_list}\n\n"
            f"Best source name(s):"
        )

        raw = (complete(prompt, max_tokens=30) or "").lower().strip()

        # Multi-source response — trigger fusion
        if "," in raw:
            sources = []
            for candidate in raw.split(","):
                candidate = candidate.strip().strip(".")
                if candidate in SOURCE_MAP and candidate != "fusion" and candidate not in sources:
                    sources.append(candidate)
            if len(sources) >= 2:
                sources = _escalate_multi_source_for_discourse_framing(query, sources)
                _LOGGER.info("LLM escalated to fusion: '%s' -> %s", query[:50], sources)
                _set_routing(cache_key, ",".join(sources))
                # Make this fresh decision semantically matchable —
                # reuses the vector the lookup above already computed
                # (or embeds now if the store was empty; either way the
                # cold path pays at most ONE embedding call).
                semantic_routing.store(query, query_vector)
                _route_event("intent_llm", query=query, decision=",".join(sources))
                return sources
            _LOGGER.warning("LLM returned multi-source but too few valid: '%s'", raw)

        # Single source response
        chosen = raw.strip(".").strip()
        if chosen in SOURCE_MAP and chosen != "fusion":
            escalated = _escalate_single_source_for_discourse_framing(query, chosen)
            if escalated is not None:
                _LOGGER.info(
                    "Discourse-framing detected — escalating '%s' to fusion: %s",
                    query[:50], escalated
                )
                _set_routing(cache_key, ",".join(escalated))
                semantic_routing.store(query, query_vector)
                _route_event("intent_llm", query=query, decision=",".join(escalated),
                             discourse_escalated=True)
                return escalated
            _LOGGER.info("LLM intent: '%s' -> %s", query[:50], chosen)
            _set_routing(cache_key, chosen)
            # Same one-embedding-call-total accounting as the fusion
            # branch above — see that site's comment.
            semantic_routing.store(query, query_vector)
            _route_event("intent_llm", query=query, decision=chosen)
            return chosen

        if raw:
            _LOGGER.warning("LLM returned unknown source '%s', falling back to kiwix", raw)
        _route_event("intent_default", query=query, decision="kiwix",
                     reason="llm_returned_unknown_source" if raw else "llm_returned_nothing")

        # Found alongside the same real bug in _llm_pick_fusion_sources(),
        # via the same complexity-investigation pass: this used to cache
        # the kiwix fallback under the same key a genuine success would
        # use, permanently locking a query into kiwix for the full routing
        # cache TTL after a single transient LLM hiccup — even though a
        # retry moments later would likely have succeeded with the actual,
        # correct source. Not caching the fallback gives every subsequent
        # identical query a fresh, real chance at a correct decision.
        return "kiwix"


def _llm_pick_fusion_sources(query: str) -> list[str]:
    """Ask LLM to pick 2-3 best sources for an explicit fusion query.
    Falls back to ["kiwix", "web"] if LLM is not configured.

    Singleflight: see _llm_detect()'s docstring and the module-level
    comment above _RefCountedLock/_inflight_locks — this is the second
    of four call sites sharing the identical check-call-write gap (the
    others are this module's own _llm_detect() and kiwix.py's
    _pick_books_with_llm()/_get_disambiguation_candidates()).
    """
    from app.llm import complete, is_configured

    def _interpret_cached_fusion_sources(cached: str) -> list[str] | None:
        sources = [s.strip() for s in cached.split(",") if s.strip() in SOURCE_MAP and s.strip() != "fusion"]
        if sources:
            _LOGGER.info("Routing cache hit for fusion sources: '%s' -> %s", query[:50], sources)
            return sources
        return None

    cache_key = f"fusion_sources:{query}"
    cached = _get_routing(cache_key)
    if cached:
        result = _interpret_cached_fusion_sources(cached)
        if result is not None:
            return result

    with _singleflight(cache_key):
        # Re-check now that we hold the lock — see _llm_detect()'s
        # identical re-check for why this is necessary, not redundant.
        cached = _get_routing(cache_key)
        if cached:
            result = _interpret_cached_fusion_sources(cached)
            if result is not None:
                return result

        if not is_configured():
            return ["kiwix", "web"]

        source_list = "\n".join(
            f"- {name}: {desc}"
            for name, desc in SOURCE_DESCRIPTIONS.items()
            if name != "fusion"
        )

        prompt = (
            f"You are a search router. Given a user query, pick 2 or 3 sources that together "
            f"would give the most complete answer. Return ONLY the source names separated by commas. "
            f"No explanation, no punctuation other than commas. Pick 2 sources for focused queries, "
            f"3 for complex or multi-topic queries.\n\n"
            f"Query: {query}\n\n"
            f"Available sources:\n{source_list}\n\n"
            f"Best source names (comma-separated):"
        )

        raw = (complete(prompt, max_tokens=30) or "").lower()
        chosen = []
        for candidate in raw.split(","):
            candidate = candidate.strip().strip(".")
            if candidate in SOURCE_MAP and candidate != "fusion" and candidate not in chosen:
                chosen.append(candidate)

        if len(chosen) >= 2:
            _LOGGER.info("LLM fusion sources for '%s': %s", query[:50], chosen)
            _set_routing(cache_key, ",".join(chosen))
            return chosen[:3]

        # Found via a deliberate complexity-investigation pass: this used to
        # cache the failure-default fallback under the exact same key as a
        # genuine success — a single transient LLM hiccup (a truncated
        # response, a momentary parsing glitch) would permanently lock this
        # specific query into the generic ["kiwix", "web"] fallback for the
        # full routing cache TTL, even though a retry moments later would
        # likely have succeeded with a better, more specific source
        # selection. Confirmed directly: a query that failed once and would
        # have genuinely succeeded on a second attempt never even reached
        # the LLM the second time, since the cached failure short-circuited
        # the function before the real call. Not caching the fallback means
        # every subsequent identical query gets a fresh, real chance at
        # success, at the (acceptable, since this is a per-query auto-fusion
        # path, not the hot single-source routing path) cost of re-querying
        # the LLM each time until it actually succeeds.
        _LOGGER.warning("LLM returned invalid fusion sources '%s', using defaults", raw)
        return ["kiwix", "web"]


def detect_intent(query: str) -> str | list[str]:
    """Detect intent using keyword matching first, LLM as fallback.

    Returns a single source name for focused queries, or a list of source names
    when the LLM determines fusion would give a better answer.

    Applies the discourse-framing bias to BOTH the keyword-match result
    and the LLM-detection result — not just the latter. Found via a
    real, reproducible gap: _escalate_single_source_for_discourse_framing()
    and _escalate_multi_source_for_discourse_framing() were both already
    correctly wired into every path inside _llm_detect() (fresh and
    cached, single- and multi-source — confirmed against The
    Discourse-Framing Investigation's own "all four real code paths"
    claim), but detect_intent()'s own `if source: return source` early
    return meant ANY keyword match — even a single one, on a generic
    word like "rss" or "feeds" — short-circuited before _llm_detect()
    was ever reached, so discourse escalation never ran at all for that
    query. Confirmed directly and reproducibly: "everyone keeps talking
    about black holes, and rss" returned bare "news" with kiwix never
    considered, the exact failure mode The Discourse-Framing
    Investigation documents as "the longest-standing known limitation
    in the whole project" — the original fix narrowed this bug's
    surface area (closing the LLM-routing-only version) without closing
    the keyword-routing version, since INTENT_MAP contains dozens of
    short, common words/phrases ("news", "weather", "rss", "feeds",
    "door locked") that can easily co-occur with genuine discourse
    framing in a real, natural sentence.
    """
    source = _keyword_detect(query)
    if source:
        if isinstance(source, list):
            escalated_multi = _escalate_multi_source_for_discourse_framing(query, source)
            _route_event("intent_keyword", query=query,
                         decision=",".join(escalated_multi))
            return escalated_multi
        escalated = _escalate_single_source_for_discourse_framing(query, source)
        final = escalated if escalated is not None else source
        _route_event("intent_keyword", query=query,
                     decision=final if isinstance(final, str) else ",".join(final),
                     discourse_escalated=escalated is not None)
        return final
    _LOGGER.info("No keyword match for '%s', asking LLM for source selection", query[:50])
    return _llm_detect(query)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Query decomposition — conjunction splitting
# ---------------------------------------------------------------------------

# Conjunctions that signal independent sub-queries
_CONJUNCTIONS = [" and ", " also ", " plus ", " as well as ", " in addition "]

# Phrases that look like conjunctions but shouldn't split
# e.g. "Python and Rust", "Phoenix and Kingman", "cats and dogs"
# Colloquial question phrases — "what's the deal with X", "what's up
# with X" are real standalone intents regardless of what specific noun
# follows, so a sub-query containing one of these anywhere should
# always count as meaningful even if the trailing noun isn't itself
# recognized as a content word. Matched as a substring anywhere in the
# clause, not just at position zero — "and remind me what's up with X"
# has the marker mid-clause, since the clause itself still carries the
# leftover conjunction/filler word ("and remind me...") from wherever
# the split actually occurred. Module-level (hoisted out of
# _decompose()'s body, where it was rebuilt on every call — _decompose()
# runs on every auto-routed query) and a tuple: immutable module state.
_COLLOQUIAL_PHRASES = (
    "what's the deal with", "whats the deal with",
    "what's up with", "whats up with",
    "what's this about", "whats this about",
    "what's the story with", "whats the story with",
)

_NOSPLIT_PATTERNS = [
    "compare", "difference between", "vs", "versus",
    "both", "either", "neither", "between",
]

# Deliberately narrow conditional pattern — ONLY matches a leading "if X,
# Y" / "should X, Y" / "in case X, Y" structure with an explicit comma.
#
# "if" is genuinely ambiguous in English: it has both a conditional sense
# ("if it's raining, bring an umbrella") and a "whether" sense ("check if
# the lights are on" = "check whether the lights are on"). The whether
# sense never appears at the very start of a sentence followed by a
# comma — it's always embedded after a verb like "check"/"see"/"tell me"/
# "let me know". Restricting to the leading-comma form sidesteps that
# ambiguity entirely rather than trying to disambiguate verb context,
# which runs into genuinely unresolvable cases ("let me know if X" could
# mean either "tell me the current status" or "notify me if it changes" —
# even a human reader can't always tell without more context).
#
# Mid-sentence and trailing "if" ("remind me to bring an umbrella if it's
# raining") are NOT matched here — deliberately out of scope for now,
# since reliably distinguishing those from "whether" usage would require
# real grammatical parsing, not pattern matching, and the value of
# guessing wrong (misframing a search result) outweighs catching the
# easier, narrower, completely unambiguous leading-comma case correctly.
_CONDITIONAL_LEAD_PATTERN = re.compile(
    r"^(if|should|in case)\s+(.+?),\s*(.+)$", re.IGNORECASE
)


def detect_conditional(query: str) -> tuple[str, str, str] | None:
    """
    Detect a leading "if X, Y" / "should X, Y" / "in case X, Y" structure.

    Returns (condition, consequence, remainder) if matched, else None.
    The condition is the searchable part ("it's raining"); the
    consequence is plain text describing what the person wants to
    happen or know, which Mnemolis cannot act on (no reminder/trigger
    capability exists) but can reference when framing the response
    around the condition's actual answer. The remainder is any
    additional, genuinely separate content that followed a conjunction
    after the consequence — empty string if none.

    Found via real usage: "if any services are down, let me know, and
    also whats the weather" originally captured "let me know, and also
    whats the weather" as a single consequence, silently swallowing a
    completely unrelated second intent ("whats the weather") that should
    have been decomposed and searched independently. The remainder is
    now split off and returned separately so the caller can route it as
    its own real intent rather than either losing it entirely or letting
    it pollute the conditional's consequence text.
    """
    m = _CONDITIONAL_LEAD_PATTERN.match(query.strip())
    if not m:
        return None
    _, condition, consequence = m.groups()
    condition = condition.strip()
    consequence = consequence.strip()

    consequence_lower = consequence.lower()
    # Found via a deliberate "bulletproofing" pass: the previous version
    # had a redundant `if p != -1` filter after this list comprehension
    # — genuinely dead code, since the comprehension's own condition
    # (`if conj in consequence_lower`) already guarantees a real
    # substring match before `.find()` ever runs on it, and Python's
    # `in` / `.find()` can never disagree (confirmed directly across
    # unicode, empty-string, and emoji edge cases) — if the substring
    # is genuinely present, `.find()` is logically guaranteed to return
    # a real, non-negative index, never -1.
    #
    # Per-occurrence proper-noun-pair protection, mirroring _decompose():
    # found via a deliberate function-by-function audit — _decompose()
    # has always refused to split at a conjunction joining a bare
    # proper-noun pair ("Iran and Israel"), but this remainder split
    # never applied the same guard, so "if the front door is unlocked,
    # tell me the latest on Iran and Israel" cleaved "Israel" off as a
    # supposedly separate intent and mangled the consequence to "tell me
    # the latest on Iran". Only the FIRST non-protected occurrence
    # matters (min() below), and each occurrence is judged individually
    # via the same _is_proper_noun_pair_at() helper _decompose() uses —
    # a protected pair early in the consequence must not block a genuine
    # separate intent later ("...on Iran and Israel, and also the
    # weather" still splits at the second conjunction).
    cut_points = []
    for conj in _CONJUNCTIONS:
        search_from = 0
        while True:
            idx = consequence_lower.find(conj, search_from)
            if idx == -1:
                break
            if not _is_proper_noun_pair_at(consequence, idx, len(conj)):
                cut_points.append(idx)
                break  # only the earliest non-protected occurrence of this conj matters
            search_from = idx + len(conj)
    remainder = ""
    if cut_points:
        cut = min(cut_points)
        # Find which conjunction matched at this exact cut point, so we
        # can strip it cleanly from the start of the remainder
        for conj in _CONJUNCTIONS:
            if consequence_lower[cut:cut + len(conj)] == conj:
                remainder = consequence[cut + len(conj):].strip()
                break
        consequence = consequence[:cut].strip()

    return condition, consequence, remainder


# Only these sources have a structured, reliably-interpretable yes/no
# signal in their output — HA's lock/door states and uptime's service
# status are genuine binary enums (locked/unlocked, up/down), not
# free-text that would require real semantic judgment to interpret.
# Forecast is included ONLY for explicit precipitation, not subjective
# conditions like "hot enough" (no universal threshold exists for that).
# Kiwix, web, and news are deliberately excluded — their content is
# open-ended free text with no structured signal to safely key off of,
# and guessing wrong here would actively mislead rather than just be
# unhelpful, which is worse than not attempting interpretation at all.
_YES_NO_INTERPRETABLE_SOURCES = {"ha", "uptime", "forecast"}


def _interpret_binary_state(
    condition_lower: str,
    result_lower: str,
    negative_condition_keywords: list[str],
    positive_condition_keywords: list[str],
    confirms_negative_result: Callable[[str], bool],
    confirms_positive_result: Callable[[str], bool],
) -> bool | None:
    """
    Shared logic for interpreting a structured source's result against
    a condition that asserts one of two opposite states (down/up,
    unlocked/locked, raining/clear) — extracted from _interpret_yes_no()
    after finding the uptime and ha branches shared this exact shape.

    A real, genuine substring trap was found and fixed while extracting
    this: "locked" is a literal substring of "unlocked", so checking for
    "locked" in a result before checking for "unlocked" produces a false
    positive on any result that's actually unlocked. The ORIGINAL code
    avoided this correctly by always checking "unlocked" first,
    regardless of which polarity the condition asserted — but a first,
    naive attempt at generalizing this checked whichever result-keyword
    matched the condition's OWN polarity first, which got the order
    backwards for the "condition asserts locked" case and silently
    returned the wrong answer. Fixed by always checking
    confirms_negative_result first, in a FIXED order independent of
    which condition polarity was detected — verified against 14 manually
    constructed test cases across all three real callers (uptime, ha,
    forecast) before this was trusted, including the exact substring-trap
    scenario that exposed the bug in the first naive version.

    confirms_negative_result/confirms_positive_result are caller-supplied
    functions (not fixed strings) specifically because uptime's result
    check is a compound condition ("all" in result AND "up" in result),
    not a single keyword the way ha's ("unlocked"/"locked") and
    forecast's ("rain"/"clear") are.
    """
    is_negative_condition = any(k in condition_lower for k in negative_condition_keywords)
    is_positive_condition = any(k in condition_lower for k in positive_condition_keywords)
    if not is_negative_condition and not is_positive_condition:
        return None

    if confirms_negative_result(result_lower):
        result_is_negative = True
    elif confirms_positive_result(result_lower):
        result_is_negative = False
    else:
        return None

    return result_is_negative if is_negative_condition else not result_is_negative


def _interpret_yes_no(condition: str, result: str, source: str) -> bool | None:
    """
    Attempt to determine whether `result` confirms or denies `condition`,
    restricted to sources with a genuinely structured, reliable signal.
    Returns True (condition holds), False (condition does not hold), or
    None if no safe interpretation is possible — callers must fall back
    to presenting the raw result without a yes/no claim when None.
    """
    if source not in _YES_NO_INTERPRETABLE_SOURCES:
        return None

    result_lower = result.lower()
    condition_lower = condition.lower()

    if source == "uptime":
        return _interpret_binary_state(
            condition_lower, result_lower,
            negative_condition_keywords=["down", "not up"],
            positive_condition_keywords=["up", "running", "working"],
            confirms_negative_result=lambda r: "down" in r,
            confirms_positive_result=lambda r: "all" in r and "up" in r,
        )

    if source == "ha":
        return _interpret_binary_state(
            condition_lower, result_lower,
            negative_condition_keywords=["unlocked"],
            positive_condition_keywords=["locked"],
            confirms_negative_result=lambda r: "unlocked" in r,
            confirms_positive_result=lambda r: "locked" in r,
        )

    if source == "forecast":
        # Only explicit precipitation — never attempt subjective
        # conditions ("hot enough", "nice out") which have no universal
        # threshold and would require a real guess, not a safe inference.
        # No positive_condition_keywords at all — deliberately one-
        # directional, verified the generalized helper handles an empty
        # list correctly (always returns None unless the condition
        # actually mentions rain).
        return _interpret_binary_state(
            condition_lower, result_lower,
            negative_condition_keywords=["rain", "raining"],
            positive_condition_keywords=[],
            confirms_negative_result=lambda r: "rain" in r or "storm" in r or "shower" in r,
            confirms_positive_result=lambda r: "clear" in r,
        )

    return None


def _frame_conditional_response(condition: str, consequence: str, condition_result: str, condition_source: str) -> str:
    """
    Compose the final response for a detected conditional query.

    When a safe yes/no interpretation exists (structured sources only —
    see _interpret_yes_no), state explicitly whether the consequence
    applies, e.g. "It IS raining, so you may want to bring an umbrella."
    Otherwise, present the real condition result plainly with a note
    that this was a conditional question, since Mnemolis has no
    reminder/trigger capability and guessing wrong on an open-ended
    condition (encyclopedic facts, subjective thresholds) would actively
    mislead rather than just be unhelpful.

    Takes the actual source string returned by route_with_source()
    directly, rather than guessing it from header text in the result —
    a single, non-decomposed result has no [SOURCE] header at all
    (headers are only added when merging multiple decomposed parts), so
    text-based source guessing silently failed for the most common case.
    """
    verdict = _interpret_yes_no(condition, condition_result, condition_source)

    if verdict is True:
        return (
            f"This was a conditional question: \"if {condition}, {consequence}.\"\n\n"
            f"It is the case that {condition} — so you may want to: {consequence}\n\n"
            f"{condition_result}"
        )
    if verdict is False:
        return (
            f"This was a conditional question: \"if {condition}, {consequence}.\"\n\n"
            f"It is NOT the case that {condition} — so the suggested action ({consequence}) may not apply.\n\n"
            f"{condition_result}"
        )

    # No safe interpretation available — present the real result plainly
    return (
        f"This was a conditional question: \"if {condition}, {consequence}.\" "
        f"Here is what was found regarding the condition — you'll need to judge "
        f"whether it applies:\n\n{condition_result}"
    )


def _is_proper_noun_pair_at(query: str, idx: int, conj_len: int) -> bool:
    """
    Given a specific conjunction occurrence (its start index and length),
    return True if THIS PARTICULAR occurrence looks like a bare
    proper-noun pair ("Iran and Israel") rather than two independent
    clauses — without making any judgment about the rest of the query.

    This is checked per-occurrence, not as a single global yes/no gate
    for the whole query. Found via real usage: a query can contain both
    a genuine proper-noun pair AND genuinely separate real intents in
    the same sentence ("what's happening with Iran and Israel right
    now, and also has anything weird happened with my back door, plus
    I keep getting a numpy import error on my pi") — a global gate that
    aborts ALL splitting the moment it finds ANY proper-noun pair
    anywhere incorrectly discards the real, separate door/GPIO intents
    too. Each conjunction occurrence must be judged on its own.
    """
    before = query[:idx].strip()
    after_full = query[idx + conj_len:]

    # Bound "after" to just the next clause — stop at the first comma or
    # the start of any other conjunction, so we're comparing against the
    # immediate next name, not the rest of a potentially long sentence
    after_lower = after_full.lower()
    cut_points = [after_lower.find(",")]
    for other_conj in _CONJUNCTIONS:
        p = after_lower.find(other_conj)
        if p != -1:
            cut_points.append(p)
    cut_points = [p for p in cut_points if p != -1]
    cut = min(cut_points) if cut_points else len(after_full)
    after = after_full[:cut].strip()

    before_words = before.split()
    after_words = after.split()
    if not before_words or not after_words:
        return False

    before_tail = before_words[-1]
    after_head = after_words[0].rstrip(",.;:")

    # Only the word immediately after the conjunction matters for "is
    # this a bare name" — "Israel right now" still starts with a bare
    # proper noun even though trailing filler ("right now") follows
    # within the same comma-bounded segment. The proper noun itself may
    # be 1-2 words ("Israel" or "New York"), but the entire bounded
    # segment doesn't need to be that short — trailing filler is fine.
    both_capitalized = before_tail[:1].isupper() and after_head[:1].isupper()

    # Found via a deliberate, thorough complexity-investigation pass:
    # the pronoun "I" is always capitalized in English regardless of
    # sentence position, making it look exactly like a proper noun to
    # this naive capitalization check. "what's happening in Texas, plus
    # I need help with my router" was being incorrectly protected as a
    # bare proper-noun pair ("Texas" + "I"), causing the ENTIRE query to
    # not split at all — a genuinely common, natural way to phrase a
    # second, unrelated request, not a contrived edge case. "I" can
    # never be the real-world entity half of a genuine pair like "Iran
    # and Israel," so it's excluded explicitly rather than trying to
    # build a broader pronoun list — no other common English pronoun
    # (he/she/they/we) is unconditionally capitalized regardless of
    # context the way "I" uniquely is, so no other word produces this
    # exact false-positive shape.
    #
    # The symmetric check on before_tail closes an asymmetric gap found
    # during the SAME re-read that found the after_head case above:
    # "I and Texas" (the unusual word order, "I" directly adjacent to
    # the conjunction with no verb between them) still triggered the
    # false positive, even after the after_head fix. Verified this is
    # genuinely low-reachability through natural English — "I" is
    # almost always followed by a verb ("I want", "I think", "I need"),
    # not directly by a conjunction, so before_tail being exactly "I"
    # essentially never occurs in a real, natural compound request the
    # way after_head being "I" commonly does ("X, plus I need..."). Low
    # reachability isn't the same as zero, though, and the fix is cheap
    # — added for completeness rather than leaving a known, if narrow,
    # asymmetry in place.
    if after_head.rstrip(",.;:").lower() == "i" or before_tail.rstrip(",.;:").lower() == "i":
        return False

    after_name_is_short = (
        len(after_words) == 1
        or (len(after_words) >= 2 and after_words[1][:1].islower())
    )
    return both_capitalized and after_name_is_short


# Every real INTENT_MAP keyword phrase, flattened across all sources,
# computed once at import time rather than rebuilt on every _decompose()
# call. Used by _filter_meaningful() (inside _decompose()) to recognize
# a real, literal keyword phrase as meaningful even when every individual
# word in it happens to be a stop word — see that function's own
# docstring for the real "is it up"/"are they up" gap this closes.
_ALL_INTENT_KEYWORDS = [kw for keywords in INTENT_MAP.values() for kw in keywords]


def _decompose(query: str) -> list[str]:
    """Split a query into independent sub-queries on conjunction words.

    Returns a list with the original query if no meaningful split is found,
    or a list of 2+ sub-queries if the query contains independent intents.

    Avoids splitting at any specific conjunction occurrence that's part
    of a comparison/single-concept query, or that joins a short
    proper-noun pair rather than two independent clauses — checked per
    occurrence, not as a single whole-query gate, since a long compound
    query can contain both a genuine proper-noun pair AND genuinely
    separate real intents in the same sentence.

    Tries every conjunction type and keeps whichever produces the most
    meaningful sub-queries — a query can contain multiple different
    conjunction words (e.g. "X, and also Y, and Z"), and the first
    conjunction encountered isn't necessarily the one that splits the
    query into its real intents. A query with one " also " and two
    " and "s should split on " and " (3 correct parts), not stop early
    on " also " just because it happens to produce >=2 technically-valid
    parts first.

    Also tries splitting on every conjunction occurrence at once,
    regardless of type — queries can genuinely mix conjunction words
    ("X, and also Y, plus Z, and W"), where no single conjunction type's
    isolated split would ever separate every intent, since each type's
    "leftover" half still contains the other conjunction words bundled
    inside it. Whichever approach (single-type or combined) produces the
    most meaningful parts wins.
    """
    q = query.strip()
    q_lower = q.lower()

    # Don't split comparison queries
    if any(p in q_lower for p in _NOSPLIT_PATTERNS):
        return [q]

    best_split: list[str] | None = None

    def _filter_meaningful(parts: list[str]) -> list[str]:
        """
        A sub-query is meaningful if, after stripping stop words and
        filler, at least one real content word remains — OR it contains
        a recognized colloquial question phrase regardless of what
        follows it — OR it contains a real, literal INTENT_MAP keyword
        phrase, even one made entirely of stop words.

        This replaced a fixed allowlist of "intent words" (door, light,
        wifi, router, etc.) that had to be hand-extended every time a new
        domain came up — found via real usage that GPIO/Python/technical
        troubleshooting clauses had zero coverage in that list at all,
        silently dropping real content during decomposition ("Ive been
        getting a python pigpio no permission to update GPIO error on my
        pi" matched nothing and was discarded). Reusing kiwix.py's
        already-hardened _STOP_WORDS set means ANY real noun/topic word
        counts as meaningful, with no domain-specific list to maintain —
        the same logic kiwix.py already uses to decide what's left of a
        query once filler is stripped.

        The INTENT_MAP check exists for a real, separate gap found via
        production data: two genuine INTENT_MAP keyword phrases —
        "is it up" and "are they up" (both real, documented `uptime`
        triggers, confirmed the only two across all 113 real keyword
        phrases in INTENT_MAP) — are made ENTIRELY of common English
        stop words ("is", "it", "up", "are", "they"). A real multi-
        conjunction query containing one of these as its own clause
        (e.g. "feeds plus is it up in addition later today also door
        locked") had that clause's content_words list come back
        completely empty, silently dropping a real, intended intent
        from the final decomposition — confirmed directly: this exact
        query previously decomposed to 4 parts with "is it up" entirely
        missing, not even folded into an adjacent part. Checked the
        same substring-anywhere way _COLLOQUIAL_PHRASES already is,
        for the same reason: a clause carrying leftover conjunction
        text from wherever the split occurred (e.g. "also is it up")
        still genuinely contains the real keyword phrase, just not at
        position zero.
        """
        meaningful = []
        for p in parts:
            p_lower = p.lower()
            # Colloquial-phrase and real-keyword checks run BEFORE the
            # length<=3 gate below — found via tracing a real, live bad
            # fusion result: "rss" is the only INTENT_MAP keyword that is
            # itself <=3 characters, and the length gate previously
            # discarded it outright before the keyword check just below
            # (added earlier for "is it up"/"are they up") ever got a
            # chance to protect it. The practical effect: "black holes,
            # and rss" never decomposed into ["black holes", "rss"] —
            # the whole unsplit string got routed to fusion, "rss" rode
            # along as noise into kiwix's own search/scoring for a query
            # about black holes, and an unrelated Stack Exchange/podcast
            # result outscored the real Black Hole article. Confirmed
            # this reordering can't accidentally keep a trivial filler
            # fragment ("and", "the", a stray comma) that happens to be
            # <=3 chars: none of those substring-match any real
            # INTENT_MAP keyword, since "rss" is the only keyword short
            # enough to even be a candidate, and substring matching
            # requires p to contain the WHOLE keyword, not the reverse.
            if any(s in p_lower for s in _COLLOQUIAL_PHRASES):
                meaningful.append(p)
                continue
            if any(kw in p_lower for kw in _ALL_INTENT_KEYWORDS):
                meaningful.append(p)
                continue
            if len(p) <= 3:
                continue
            # Strip trailing 's/'t contractions before stop-word matching —
            # "internet's" otherwise survives as "internet'" after a naive
            # split, the same class of bug found and fixed in kiwix.py
            normalized_words = [
                re.sub(r"['']\w*$", "", w) for w in p.lower().split()
            ]
            content_words = [
                w for w in normalized_words
                if w not in kiwix._STOP_WORDS and len(w) > 1
            ]
            if content_words:
                meaningful.append(p)
        return meaningful

    # Try every conjunction type in isolation, keep whichever single-type
    # split has the most meaningful parts
    for conj in sorted(_CONJUNCTIONS, key=len, reverse=True):
        if conj not in q_lower:
            continue

        parts = []
        # segment_start marks where the CURRENT accumulating part began;
        # search_from marks where to resume looking for the next
        # occurrence of this conjunction. These differ specifically when
        # a proper-noun pair is skipped — found via real usage: the
        # original version reset `remaining` (and therefore the next
        # part's start) to right after EVERY occurrence, including
        # skipped ones, which silently discarded real, meaningful text
        # that came before a protected pair ("also whats happening with
        # Iran and" was discarded entirely, just because "Iran and
        # Israel" needed protecting — the text before the pair was real
        # content from a genuinely separate intent, not part of the pair
        # at all). Now, skipping a proper-noun-pair occurrence advances
        # search_from (so we don't re-examine the same occurrence) but
        # leaves segment_start untouched, so that text accumulates into
        # the next real part instead of vanishing.
        segment_start = 0
        search_from = 0
        while True:
            idx = q_lower.find(conj, search_from)
            if idx == -1:
                break
            if _is_proper_noun_pair_at(q, idx, len(conj)):
                search_from = idx + len(conj)
                continue
            part = q[segment_start:idx].strip()
            if part:
                parts.append(part)
            segment_start = idx + len(conj)
            search_from = segment_start
        remaining = q[segment_start:].strip()
        if remaining:
            parts.append(remaining)

        meaningful = _filter_meaningful(parts)
        if len(meaningful) >= 2 and (best_split is None or len(meaningful) > len(best_split)):
            best_split = meaningful

    # Also try splitting on EVERY conjunction occurrence at once, regardless
    # of type — a query can genuinely mix conjunction words ("X, and also Y,
    # plus Z, and W"), and no single conjunction type's isolated split would
    # ever separate all of those intents, since each one's "leftover" half
    # still contains the other conjunction words bundled inside it. Found
    # via real usage: a 5-intent query mixing "and also", "plus", "and",
    # "also" only ever produced 2 parts under the single-type approach,
    # because every type's split left the other three conjunctions stuck
    # together in one half or the other.
    all_matches = []
    for conj in _CONJUNCTIONS:
        start = 0
        while True:
            idx = q_lower.find(conj, start)
            if idx == -1:
                break
            # Skip THIS occurrence if it's a bare proper-noun pair —
            # other occurrences elsewhere in the same query are still
            # checked independently and may be genuinely real intents
            if not _is_proper_noun_pair_at(q, idx, len(conj)):
                all_matches.append((idx, idx + len(conj)))
            start = idx + len(conj)
    all_matches.sort()

    if len(all_matches) > 1:
        # Collapse adjacent/overlapping matches ("and also" = " and "
        # immediately followed by " also ") into a single split point,
        # otherwise the tiny gap between them becomes a near-empty fragment
        collapsed = []
        for start, end in all_matches:
            if collapsed and start <= collapsed[-1][1]:
                collapsed[-1] = (collapsed[-1][0], max(collapsed[-1][1], end))
            else:
                collapsed.append((start, end))

        parts = []
        last_end = 0
        for start, end in collapsed:
            part = q[last_end:start].strip()
            if part:
                parts.append(part)
            last_end = end
        remaining = q[last_end:].strip()
        if remaining:
            parts.append(remaining)

        meaningful = _filter_meaningful(parts)
        if len(meaningful) >= 2 and (best_split is None or len(meaningful) > len(best_split)):
            best_split = meaningful

    if best_split:
        _LOGGER.info("Decomposed query into %d parts: %s", len(best_split), best_split)
        return best_split

    return [q]


def route(query: str, source: str = "auto", fusion_sources: list[str] | None = None) -> str:
    """Backward-compatible wrapper around route_with_source() — returns
    just the result string for callers that don't need to know which
    source actually produced it (e.g. fusion.search() calling into
    individual sources, existing tests written against this signature)."""
    result, _actual_source = route_with_source(query, source, fusion_sources)
    return result


# ---------------------------------------------------------------------------
# Per-request route stats — how main.py's /search learns what really
# happened inside route_with_source() without changing its signature
# ---------------------------------------------------------------------------
#
# The problem this solves, confirmed via a deliberate function-by-function
# audit: /search previously called detect_intent(request.query) BEFORE
# route_with_source(), purely to compute the `cached` response field —
# meaning (1) a query that route_with_source() would decompose or treat as
# conditional paid a full, wasted cold LLM routing call for a full-query
# intent decision the decomposition/conditional paths never even use, and
# (2) the `cached` flag was computed against a single-source cache key
# that doesn't correspond to what actually ran for those compound shapes.
# Fallback detection had the same structural gap: main.py inferred it
# after the fact by comparing its own pre-computed intent against the
# resolved source — an inference its own comment documents as blind to
# fallbacks inside decomposed sub-queries.
#
# Widening route_with_source()'s return tuple was considered and
# deliberately rejected (again — the same call main.py's original
# fallback-detection comment documents): that function recurses into
# itself at 4 internal call sites and is unpacked as a 2-tuple across
# dozens of tests; threading a third element through every one of those
# is a much larger, riskier change than the information flow requires.
#
# Instead, the same ContextVar pattern this module already uses for
# suppress_cache_writes(): route_query() (the one real /search entry
# point) installs a small mutable stats dict in a ContextVar; the few
# genuinely authoritative points deep in the call graph (_get_cached()
# hits, real handler/fusion invocations, the fallback path inside
# _resolve_single_source()) record into it via _route_stat(); everything
# else — including every recursive call and every worker thread, since
# contextvars.copy_context() copies the Context but shares the SAME dict
# object by reference — sees and mutates the one shared dict. Every
# recorded value is an idempotent boolean set or a plain overwrite, never
# a read-modify-write counter, precisely so concurrent worker-thread
# writes can never race each other into a wrong value. Callers that never
# install the ContextVar (fusion's internal dispatch, the MCP server's
# route(), adversarial testing, every existing test) leave it at its None
# default and every _route_stat() call is a no-op — zero behavior change
# for them.
_ROUTE_STATS: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_ROUTE_STATS", default=None
)


def _route_stat(key: str, value) -> None:
    """Record one fact about the current route_query() call, if one is
    in progress — a no-op for every caller that didn't come through
    route_query(). Idempotent-set semantics only; see the module comment
    above _ROUTE_STATS for why values are never incremented."""
    stats = _ROUTE_STATS.get()
    if stats is not None:
        stats[key] = value


def _route_event(step: str, **fields) -> None:
    """Append one explanation-chain event to the current route_query()
    call's trace, if one is in progress — a no-op for every caller that
    didn't come through route_query(), exactly like _route_stat().

    Events are the substrate for /search's explain=true (see
    wiki/Explanation-Chains.md): small dicts recorded at the same
    authoritative code points the boolean stats already are, so the
    explanation can never disagree with what actually ran — both are
    the same recording, not a parallel reconstruction.

    Thread-safety: list.append() is a single atomic operation under the
    GIL, so appends from concurrent decompose/conditional worker
    threads are individually safe. Their INTERLEAVING is genuinely
    nondeterministic — two concurrent sub-queries' events arrive in
    completion order, not ask order — which is why every event carries
    the query text it belongs to: attribution comes from the event's
    own fields, never from its position in the list.
    """
    stats = _ROUTE_STATS.get()
    if stats is not None:
        events = stats.get("events")
        if events is not None:
            events.append({"step": step, **fields})


class RouteOutcome(NamedTuple):
    """Everything /search needs to know about one routed query —
    returned by route_query() below."""
    result: str
    source_used: str
    cached: bool
    fallback_occurred: bool
    success: bool
    error: str | None
    explanation: list[dict]


def route_query(query: str, source: str = "auto", fusion_sources: list[str] | None = None) -> RouteOutcome:
    """The /search entry point: run route_with_source() with a per-request
    stats channel installed, and return the result together with what
    genuinely happened — whether the response was served entirely from
    cache, whether any FALLBACK_CHAIN fallback actually fired (including
    inside decomposed sub-queries, which main.py's old after-the-fact
    inference structurally could not see), and, on failure, the source
    the query was headed to when it broke.

    `cached` is True only when the response was served ENTIRELY from the
    result cache — at least one cache hit and zero real handler/fusion
    invocations. For a plain single-source query this is exactly the old
    pre-check semantics; for a decomposed query it's strictly more
    honest than the old behavior (which checked a cache key that never
    corresponds to anything the decomposition path actually does).

    Never raises: an unexpected exception from routing is caught here
    and reported as success=False with the intended source preserved
    from whatever intent resolution had already happened — the same
    honest "what was this query trying to do" reporting main.py's own
    error path previously reconstructed from its pre-computed intent.
    """
    stats: dict = {
        "served_any_from_cache": False,
        "invoked_any_source": False,
        "fallback_occurred": False,
        "intended_source": None,
        # The explanation chain — ordered _route_event() records. Always
        # collected (appends of small dicts are noise next to any real
        # routing work); main.py decides whether to RETURN it based on
        # the request's explain flag, so non-explain responses carry
        # nothing extra beyond a null field.
        "events": [],
    }
    token = _ROUTE_STATS.set(stats)
    try:
        result, source_used = route_with_source(query, source, fusion_sources)
        cached = stats["served_any_from_cache"] and not stats["invoked_any_source"]
        return RouteOutcome(
            result=result,
            source_used=source_used,
            cached=cached,
            fallback_occurred=stats["fallback_occurred"],
            success=True,
            error=None,
            explanation=stats["events"],
        )
    except Exception as e:
        # Report the source this query was actually headed to when it
        # broke — recorded by route_with_source()'s own intent
        # resolution — rather than echoing back the literal string
        # "auto", which is a non-answer. Falls back to the requested
        # source only when the exception fired before any intent was
        # ever resolved.
        failure_source = stats["intended_source"] or source
        return RouteOutcome(
            result="",
            source_used=failure_source,
            cached=False,
            fallback_occurred=stats["fallback_occurred"],
            success=False,
            error=str(e),
            # The partial chain of what ran BEFORE the failure — a trace
            # earns its keep most on exactly this path.
            explanation=stats["events"],
        )
    finally:
        _ROUTE_STATS.reset(token)


def _resolve_single_source(source: str, query: str) -> tuple[str, str]:
    """
    Resolve a single (non-fusion) source for a query: check cache, call
    the handler, and fall back to FALLBACK_CHAIN's target if the result
    looks empty — checking the fallback target's own cache first too.

    Extracted from two previously slightly-different inline
    implementations found during a deliberate refactoring pass (prompted
    by a cyclomatic-complexity check flagging route_with_source() as the
    most complex function in the codebase by a wide margin) — one inside
    the decomposition loop, one at the top level for a directly-routed
    query. Comparing them side by side surfaced a real, previously
    undetected inconsistency: the decomposition loop's fallback path
    called the fallback handler directly with no cache check, while the
    top-level path correctly checked _get_cached(fallback_source, query)
    first. This unified version follows the more correct, top-level
    behavior — a fallback result, once cached, should be served from
    cache the same way any other result is, regardless of which code
    path (direct routing or a decomposed sub-query) led to it.

    Returns (result, source_used) — source_used reflects the actual
    source that produced the result, which may differ from the
    originally-intended `source` argument if a fallback occurred.
    """
    handler = SOURCE_MAP.get(source)
    if not handler:
        return f"Unknown source '{source}'. Valid options: {', '.join(SOURCE_MAP.keys())}.", source

    cached = _get_cached(source, query)
    if cached:
        return cached, source

    _route_stat("invoked_any_source", True)
    invoke_start = time.monotonic()
    result = handler(query)
    _route_event("source_invoked", source=source, query=query,
                 elapsed_ms=int((time.monotonic() - invoke_start) * 1000))

    if _looks_empty(result) and source in FALLBACK_CHAIN:
        fallback_source = FALLBACK_CHAIN[source]
        _LOGGER.info("Result from '%s' looks empty, falling back to '%s'", source, fallback_source)
        _route_event("fallback", from_source=source, to_source=fallback_source, query=query)
        fallback_handler = SOURCE_MAP.get(fallback_source)
        if fallback_handler:
            cached_fallback = _get_cached(fallback_source, query)
            if cached_fallback:
                _route_stat("fallback_occurred", True)
                return cached_fallback, fallback_source
            invoke_start = time.monotonic()
            fallback_result = fallback_handler(query)
            _route_event("source_invoked", source=fallback_source, query=query,
                         elapsed_ms=int((time.monotonic() - invoke_start) * 1000))
            if not _looks_empty(fallback_result):
                _LOGGER.info("Fallback to '%s' succeeded", fallback_source)
                # Recorded HERE, at the one place a fallback genuinely
                # happens, rather than inferred after the fact in
                # main.py by comparing intended vs resolved source —
                # that inference was structurally blind to fallbacks
                # inside decomposed sub-queries (its own comment said
                # so), which this direct recording now sees too.
                _route_stat("fallback_occurred", True)
                _set_cached(fallback_source, query, fallback_result)
                return fallback_result, fallback_source
            _LOGGER.warning("Fallback to '%s' also returned empty result", fallback_source)

    if not _looks_empty(result):
        _set_cached(source, query, result)

    return result, source


def _resolve_conditional(query: str, source: str) -> tuple[str, str] | None:
    """
    Detect and resolve a leading "if X, Y" conditional structure, if
    present. Returns (result, source_used) if the query was genuinely
    conditional, or None if it wasn't — callers should fall through to
    normal routing/decomposition on None, exactly as route_with_source()
    already did before this logic was a separate function.

    Extracted from route_with_source() during the same complexity-
    reduction effort that produced _resolve_single_source() — this is
    the second-most complex remaining piece of that function, and the
    one with the densest real bug history in the project (see the wiki's
    "The Recursion Design Bug" page for the full story of why this logic
    looks the way it does, including a depth-counter approach that was
    tried, found to have a real bug, and replaced with the simpler
    recursive-on-the-extracted-condition-text approach used here).

    Conditional detection only ever applies to source == "auto" — an
    explicit source request skips it entirely, the same way it already
    skips decomposition. Only the condition is searched; Mnemolis has no
    reminder/trigger capability to act on the consequence, but the
    response is framed around the condition's actual real answer rather
    than presenting it as an unconditional fact.

    Recurses into route_with_source() by passing the already-extracted
    CONDITION text, never the original "if X, Y" string — so the
    recursive call's input essentially never re-matches the leading
    "if/should/in case" pattern again, naturally self-limiting without
    needing an explicit recursion-depth counter.
    """
    if source != "auto":
        return None

    conditional = detect_conditional(query)
    if not conditional:
        return None

    condition, consequence, remainder = conditional
    _LOGGER.info(
        "Detected conditional query — condition=%r consequence=%r remainder=%r",
        condition[:50], consequence[:50], remainder[:50]
    )
    _route_event("conditional_detected", query=query, condition=condition,
                 consequence=consequence, remainder=remainder)

    # The condition and remainder calls have no real data dependency on
    # each other — route_with_source(remainder, ...) only needs the
    # REMAINDER text, never the condition's result, and
    # _frame_conditional_response() (which DOES need condition_result)
    # only needs the CONDITION's own outputs. Confirmed via the same
    # direct research, and fixed with the same verified-safe pattern,
    # already applied to searxng.py's structurally identical two-fetch
    # case — see that module's search() and the wiki's Adversarial
    # Self-Testing page for the real, traced history: a genuine, pre-
    # existing file-write race in both on-disk caches (now fixed via
    # router._atomic_write_json()) was the actual reason this was once
    # treated as too risky to attempt, not anything about THIS specific
    # pair of calls.
    #
    # Only spun up at all when a remainder genuinely exists — a plain
    # "if X, Y" query with no trailing conjunction (the more common
    # real-world shape) has an empty remainder and never needed a
    # second call in the first place; the ordinary single-call path
    # below is completely unchanged for that case.
    if remainder:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            # contextvars.copy_context() is called ONCE per task, not
            # once shared between both — a single Context object cannot
            # be entered by two threads simultaneously (Context.run() is
            # documented as non-reentrant across concurrent execution;
            # confirmed directly via a real RuntimeError from exactly
            # this mistake while building searxng.py's own fix).
            condition_future = executor.submit(
                contextvars.copy_context().run, route_with_source, condition, "auto"
            )
            remainder_future = executor.submit(
                contextvars.copy_context().run, route_with_source, remainder, "auto"
            )
            condition_result, condition_source = condition_future.result()
            remainder_result, remainder_source = remainder_future.result()
    else:
        condition_result, condition_source = route_with_source(condition, "auto")
        remainder_result, remainder_source = None, None

    framed = _frame_conditional_response(condition, consequence, condition_result, condition_source)

    # A real, separate intent followed the conditional statement
    # ("...let me know, and also whats the weather") — search it
    # independently and merge it in, rather than either losing it
    # entirely or letting it pollute the conditional's consequence.
    #
    # If the remainder itself decomposes into multiple distinct
    # sources, route_with_source() already returns "fusion" as
    # its reported source for an already-self-headered result
    # (each contributing source has its own [SOURCE — DESC]
    # header baked in). Wrapping that in ANOTHER header using the
    # literal string "fusion" produces the same nonsensical
    # "[FUSION — FUSION]" double-header bug found and fixed
    # earlier this session in the decomposition loop — this is
    # the same root cause showing up at a different call site
    # that needed the identical fix: only wrap genuinely
    # single-source results, pass fusion results through as-is.
    if remainder:
        if not _looks_empty(remainder_result):
            overall_source = "fusion" if remainder_source != condition_source else condition_source
            remainder_section = (
                remainder_result if remainder_source == "fusion"
                else f"{fusion._format_header(remainder_source)}\n{remainder_result}"
            )
            condition_section = (
                framed if condition_source == "fusion"
                else f"{fusion._format_header(condition_source)}\n{framed}"
            )
            merged_text = f"{condition_section}\n\n---\n\n{remainder_section}"
            return merged_text, overall_source

    return framed, condition_source


def _merge_decomposed_parts(parts: list[tuple[str, str]]) -> tuple[str, str]:
    """
    Merge a list of (source, result) tuples from decomposed sub-queries
    into one final, headered response string, plus the overall source
    label to report.

    Extracted from route_with_source()'s decomposition branch during the
    same complexity-reduction effort that produced _resolve_single_source()
    and _resolve_conditional() — this is the formatting/merging half of
    that branch; the per-sub-query resolution loop that builds `parts` in
    the first place stays inline, since it recurses into route_with_source()
    itself and extracting it cleanly would mean threading several more
    pieces of loop state through a function boundary for comparatively
    little complexity reduction.

    Consecutive results from the same source are merged into one block
    first, so e.g. "indoor air quality and are the doors locked" (both
    resolving to `ha`) returns one [HA] section, not two — via
    fusion._merge_same_source(), genuinely shared with fusion.search()'s
    own identical need to merge consecutive same-source results
    (originally a byte-for-byte duplicate, found and unified during a
    deliberate complexity-investigation pass on fusion.search() itself).
    A sub-query whose own intent resolved to internal fusion already
    contains its own per-source [SOURCE — DESC] headers (added by
    fusion.search() itself) — wrapping that block in another header at
    this outer level produces a nonsensical "[FUSION — FUSION]" label,
    since "fusion" isn't a real source with its own entry in
    _HEADER_LABELS, just self-headered content passing through. Only
    genuinely single-source results get wrapped here.

    A second, separate merge pass — _dedupe_nested_fusion_sections()
    below — runs after assembly to catch a real bug _merge_same_source()
    above structurally cannot see: when ONE decomposed sub-query
    resolves to internal fusion (e.g. the discourse-framing escalation
    picking ["kiwix", "news"] for one clause) and a DIFFERENT,
    separately-decomposed sub-query resolves to bare "news" on its own,
    parts here looks like [("fusion", "<already-headered kiwix+news
    blob>"), ("news", "<separate news result>")] — two genuinely
    different outer labels, so _merge_same_source()'s consecutive-
    same-source check (which only compares the OUTER label) correctly
    has no reason to merge them, even though the real, nested [NEWS —
    ...] section already inside the fusion blob and the second,
    separate news result are the exact same source duplicated. Found
    via a real, live result on MiniDock: "everyone keeps talking about
    black holes, and rss" — the discourse-framing-escalated clause
    pulled in kiwix+news+web together, and the separately-decomposed
    "rss" clause resolved to its own, second, redundant news section —
    producing [KIWIX, NEWS, WEB, NEWS] in the final answer instead of
    [KIWIX, NEWS, WEB]. This is a real, pre-existing gap in how nested
    fusion results interact with the outer merge — not something the
    discourse-framing/decomposition fixes earlier this release
    introduced; it was already reachable via any other query shape
    where one decomposed clause's own LLM-judged fusion happens to
    share a source with a different, separately-decomposed clause —
    this query shape just made it reliably, easily reachable instead
    of needing a rarer LLM-judgment coincidence to trigger.
    """
    merged = fusion._merge_same_source(parts)

    sections = []
    for src, result in merged:
        if src == "fusion":
            sections.append(result)
        else:
            sections.append(f"{fusion._format_header(src)}\n{result}")

    # Multiple decomposed sources merged — report "fusion" as the overall
    # source if more than one distinct source contributed, otherwise
    # report the single source used
    distinct_sources = {s for s, _ in merged}
    overall_source = "fusion" if len(distinct_sources) > 1 else next(iter(distinct_sources))
    final_text = "\n\n---\n\n".join(sections)
    final_text = _dedupe_nested_fusion_sections(final_text)
    return final_text, overall_source


def _dedupe_nested_fusion_sections(text: str) -> str:
    """Merge duplicate [SOURCE — LABEL] sections that ended up adjacent
    or non-adjacent in a final, already-assembled result string — the
    fix for the real nested-fusion duplication bug documented in
    _merge_decomposed_parts()'s own docstring above.

    Operates on the fully-formatted text using the exact, real header
    strings fusion._format_header() can produce (re.escape()'d, not a
    generic bracket-matching pattern) — safe against any real content
    that happens to contain bracket-like text, since a match requires
    the literal, exact header string, not just a bracket shape. Only
    ever called on text this module itself just assembled, never on
    arbitrary external input.

    A true no-op (returns the input completely unchanged) for the
    overwhelming majority of results, which never contain a duplicate
    section at all — only does any work when the exact same header
    genuinely appears more than once.
    """
    known_headers = [fusion._format_header(src) for src in fusion._HEADER_LABELS]
    pattern = re.compile(
        "(" + "|".join(re.escape(h) for h in known_headers) + ")"
    )

    matches = list(pattern.finditer(text))
    if len(matches) <= 1:
        return text  # nothing to dedupe — the common case

    # Split into (header, content) pairs by header position. Each
    # captured content slice can have a leftover "---" section
    # separator dangling at its start or end (the original text's own
    # separator between what WAS two adjacent sections, before this
    # split — splitting only on the header itself, not the separator,
    # leaves it attached to whichever side of the header it originally
    # sat on). Stripped here so a later merge doesn't visually
    # duplicate the outer "\n\n---\n\n" join with a leftover inner one.
    _SEPARATOR_EDGE = re.compile(r"^\s*---\s*|\s*---\s*$")
    pieces = []
    preamble = text[: matches[0].start()]
    for i, m in enumerate(matches):
        header = m.group(0)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end]
        content = _SEPARATOR_EDGE.sub("", content)
        pieces.append((header, content))

    if len({h for h, _ in pieces}) == len(pieces):
        return text  # every header is already unique — nothing to merge

    # Merge content for duplicate headers, preserving first-occurrence
    # order — the same "first seen wins position" convention
    # fusion._merge_same_source() already uses. Dedupes items BETWEEN
    # the two blobs being merged here via fusion._dedupe_items_across_
    # blobs() before joining them — at this exact point, deliberately,
    # while the boundary between the two original results is still
    # completely unambiguous. Tried deduping AFTER the join first
    # (operating on already-merged text); confirmed broken via a
    # failing test — by the time two blobs are joined with a bare
    # "\n\n", the real boundary between them is no longer reliably
    # distinguishable from an ordinary paragraph break within either
    # blob's own content, so a later content-level split can silently
    # merge what should have been two separate items into one and miss
    # a real, earlier duplicate. Fixed by moving the dedup here
    # instead — the same fix applied to fusion._merge_same_source()'s
    # own join, for the same real reason.
    #
    # Groups every piece by header FIRST, then decides the separator
    # ONCE per header group — mirroring the identical fix applied to
    # fusion._merge_same_source(). A prior version decided
    # "\n\n---\n\n" vs bare "\n\n" independently on each individual
    # pairwise merge, based on whether either side of that one pair
    # already happened to contain "---" internally — meaning the EARLY
    # pairs in a chain mixing single-item and multi-item content got
    # the wrong, ambiguous "\n\n" separator even though the assembled
    # whole is unambiguously multi-item by the time the chain finishes.
    # Confirmed via the analogous reconstruction at the text-assembly
    # level: three [NEWS — ...] sections among other real headers, the
    # first two single-item, the third multi-item — the merged
    # section's first two items got joined with a bare "\n\n", only the
    # rest correctly getting "---". Combining 2+ genuinely separate
    # pieces under the same header is, definitionally, a multi-item
    # situation the moment there are two of them, independent of
    # whether any individual piece happened to already contain "---"
    # on its own.
    groups = {}
    for header, content in pieces:
        groups.setdefault(header, []).append(content.strip("\n"))

    seen_order = []
    merged_content = {}
    for header, contents in groups.items():
        seen_order.append(header)
        if len(contents) == 1:
            merged_content[header] = contents[0]
        else:
            acc = contents[0]
            for nxt in contents[1:]:
                acc, nxt, _ = fusion._dedupe_items_across_blobs(acc, nxt)
                acc = acc.rstrip() + "\n\n---\n\n" + nxt.lstrip()
            merged_content[header] = acc

    rebuilt = preamble + ("\n\n---\n\n".join(
        f"{header}\n{merged_content[header]}" for header in seen_order
    ))
    return rebuilt



def _resolve_decomposed_sub_query(sub_q: str) -> list[tuple[str, str]]:
    """Resolve ONE decomposed sub-query into zero, one, or two
    (source, result) parts — extracted from route_with_source()'s
    decomposition loop so sub-queries can run concurrently (each call
    is dispatched onto its own worker thread there). The earlier note
    in _merge_decomposed_parts()'s docstring about this loop staying
    inline predates the parallelization; the only loop state the body
    ever actually needed was the parts it produced, which is exactly
    this function's return value.

    Each decomposed sub-query may itself contain a leading "if X, Y"
    structure that the top-level conditional check never sees —
    detect_conditional() only runs once, against the FULL original
    query, before decomposition. A query like "what is the weather and
    if the back door is unlocked, let me know" doesn't start with "if"
    so the top-level check correctly returns None, but the second
    decomposed sub-query ("if the back door is unlocked, let me know")
    absolutely does match and was previously never re-checked at all.

    Mirrors the top-level conditional handling exactly: extract the
    condition and search ONLY that (via a recursive call on the
    condition text, not the original "if X, Y" string), then frame the
    response. An earlier version of this fix recursed on the original
    sub_q string with a manual _depth counter meant to stop infinite
    recursion — but that counter blocked the recursive call's OWN
    necessary re-detection of the very same conditional it was meant
    to handle, since the depth incremented before the conditional was
    actually consumed. Passing the already-extracted condition (not
    the still-"if"-prefixed sub_q) sidesteps the whole problem: the
    condition text essentially never re-matches the leading
    "if/should/in case" pattern, so this naturally terminates without
    needing any artificial depth limit at all.
    """
    parts: list[tuple[str, str]] = []

    sub_conditional = detect_conditional(sub_q)
    if sub_conditional:
        sub_condition, sub_consequence, sub_remainder = sub_conditional
        _route_event("conditional_detected", query=sub_q, condition=sub_condition,
                     consequence=sub_consequence, remainder=sub_remainder)
        sub_condition_result, sub_source = route_with_source(sub_condition, "auto")
        sub_result = _frame_conditional_response(
            sub_condition, sub_consequence, sub_condition_result, sub_source
        )
        if not _looks_empty(sub_condition_result):
            parts.append((sub_source, sub_result))
        # A real, separate intent followed the conditional within this
        # one decomposed sub-query — search it independently too, rather
        # than losing it or letting it pollute the consequence text
        if sub_remainder:
            remainder_result, remainder_source = route_with_source(sub_remainder, "auto")
            if not _looks_empty(remainder_result):
                parts.append((remainder_source, remainder_result))
        return parts

    intent = detect_intent(sub_q)
    if isinstance(intent, list):
        # Found via a deliberate complexity-reduction investigation
        # (comparing this dispatch against the top-level single-query
        # fusion dispatch, the same side-by-side-comparison discipline
        # that found two real bugs during the prior extraction pass): a
        # decomposed sub-query that itself resolves to fusion had NO
        # caching at all, unlike every other path in the system — every
        # individual single-source sub-query result gets cached via
        # _resolve_single_source(), the overall merged decomposed
        # response gets no cache of its own either, but a
        # sub-query-level fusion result fell through both, meaning a
        # repeated compound query whose individual clause happened to
        # resolve to multiple sources internally re-ran
        # _llm_pick_fusion_sources() and re-queried every fusion source
        # on every single request, even identical repeats. Fixed by
        # using the exact same cache-key convention the top-level
        # fusion path already uses.
        sub_source = "fusion"
        sub_fusion_key = ",".join(sorted(intent))
        sub_cache_key = f"fusion[{sub_fusion_key}]:{sub_q}"
        cached_sub_fusion = _get_cached("fusion", sub_cache_key)
        if cached_sub_fusion:
            sub_result = cached_sub_fusion
        else:
            _route_stat("invoked_any_source", True)
            _route_event("fusion", query=sub_q, sources=list(intent))
            invoke_start = time.monotonic()
            sub_result = fusion.search(sub_q, intent)
            _route_event("source_invoked", source="fusion", query=sub_q,
                         elapsed_ms=int((time.monotonic() - invoke_start) * 1000))
            if not _looks_empty(sub_result):
                _set_cached("fusion", sub_cache_key, sub_result)
    else:
        sub_result, sub_source = _resolve_single_source(intent, sub_q)
    if not _looks_empty(sub_result):
        parts.append((sub_source, sub_result))
    return parts


def route_with_source(query: str, source: str = "auto", fusion_sources: list[str] | None = None) -> tuple[str, str]:
    """
    Route a query to the appropriate source(s) and return both the result
    and the source that ACTUALLY produced it.

    This distinction matters because of fallback behavior — a query
    routed to 'kiwix' that returns no usable result can silently fall
    back to 'web', and the caller needs to know that happened rather than
    reporting 'kiwix' as the source_used when 'web' is what actually
    answered. Found via real usage: a GPIO troubleshooting query resolved
    to kiwix, kiwix's search came back empty for the long colloquial
    phrase, fell back to web, got good results — but the API response's
    source_used field still said 'kiwix' because main.py independently
    re-derived the intended source before calling route(), with no way
    to learn that an internal fallback had actually occurred.

    Conditional detection (both at the top level and re-checked against
    each decomposed sub-query) recurses into this function by passing the
    already-extracted CONDITION text, never the original "if X, Y" string
    — so the recursive call's input essentially never re-matches the
    leading "if/should/in case" pattern again. This is naturally
    self-limiting without needing an explicit recursion-depth counter.
    An earlier version used a manual _depth parameter for this, but that
    introduced a real bug: the depth incremented before the conditional
    was actually consumed, so a sub-query's recursive call (still
    containing the full "if X, Y" text) had its OWN necessary
    conditional re-detection blocked by the very counter meant to guard
    against runaway recursion that was never actually possible.
    """
    # Conditional detection — only for auto routing, checked before
    # decomposition since "if X, Y" is structurally a single statement
    # with a condition and a consequence, not a flat list of independent
    # intents the way "X and Y" is.
    conditional_result = _resolve_conditional(query, source)
    if conditional_result is not None:
        return conditional_result

    # Query decomposition — only for auto routing
    if source == "auto":
        sub_queries = _decompose(query)
        if len(sub_queries) > 1:
            _LOGGER.info("Routing %d decomposed sub-queries for: '%s'", len(sub_queries), query[:50])
            _route_event("decomposed", query=query, parts=list(sub_queries))
            # Sub-queries are genuinely independent by construction —
            # _decompose() only ever splits on conjunctions joining
            # separate intents, and no sub-query's resolution reads
            # another's result — so they run CONCURRENTLY rather than
            # paying each sub-query's full cold cost back to back. A
            # 3-intent cold compound query previously paid three stacked
            # LLM-routing + source-fetch costs sequentially; this was the
            # largest remaining sequential-work latency cost in the
            # routing path after searxng.py's and _resolve_conditional()'s
            # structurally identical fixes.
            #
            # A FRESH, per-call executor — deliberately NOT a shared
            # module-level pool like fusion.py's — because these workers
            # recurse into route_with_source(), which can itself hit this
            # exact code path again (a sub-query's condition text that
            # decomposes further): submitting into a shared pool from
            # inside that same pool's own worker and blocking on the
            # result is a real deadlock under saturation. A fresh pool
            # per nesting level cannot deadlock (each level's workers
            # wait only on a deeper level's own pool), and decomposed
            # multi-intent queries are rare enough that per-call pool
            # construction is not the unbounded-thread-pressure pattern
            # fusion.py's hot path had — the same judgment already made
            # for _resolve_conditional()'s own per-call executor.
            #
            # contextvars.copy_context() once per task, never shared —
            # Context.run() is non-reentrant across concurrent execution
            # (the identical constraint documented at every other
            # executor site in this project). The copied Context shares
            # the same _ROUTE_STATS dict object by reference, so cache/
            # fallback facts recorded inside workers still reach
            # route_query()'s per-request stats correctly.
            #
            # Futures are collected strictly IN SUBMISSION ORDER (a plain
            # list walk, not as_completed()) — parts order determines the
            # final response's section order, which must match the order
            # the person actually asked things in, not network completion
            # order.
            max_workers = min(len(sub_queries), settings.decompose_max_parallel)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(
                        contextvars.copy_context().run, _resolve_decomposed_sub_query, sub_q
                    )
                    for sub_q in sub_queries
                ]
                parts = []  # list of (source, result) tuples
                for future in futures:
                    parts.extend(future.result())

            if parts:
                return _merge_decomposed_parts(parts)
            # All sub-queries returned empty — fall through to single query routing

    if source == "auto":
        intent = detect_intent(query)
        # LLM may escalate to fusion for multi-topic queries
        if isinstance(intent, list):
            _LOGGER.info("Auto-escalating to fusion for query: '%s' sources=%s", query[:50], intent)
            fusion_sources = intent
            source = "fusion"
        else:
            source = intent
        # Recorded for route_query()'s failure reporting — if an
        # exception fires past this point, /search can honestly report
        # the source this query was actually headed to instead of the
        # literal string "auto". Recursive calls overwrite this with
        # whichever sub-query was being processed when things broke,
        # which is strictly more informative for diagnosis, not less.
        _route_stat("intended_source", source)
    else:
        _LOGGER.info("Source explicitly set to '%s' for query: '%s'", source, query[:50])
        _route_stat("intended_source", source)

    # Handle fusion — LLM picks sources if none specified
    if source == "fusion":
        if not fusion_sources:
            fusion_sources = _llm_pick_fusion_sources(query)
        # Build a stable cache key from sorted sources
        fusion_key = ",".join(sorted(fusion_sources))
        cache_key_query = f"fusion[{fusion_key}]:{query}"
        cached = _get_cached("fusion", cache_key_query)
        if cached:
            return cached, "fusion"
        _route_stat("invoked_any_source", True)
        _route_event("fusion", query=query, sources=list(fusion_sources))
        invoke_start = time.monotonic()
        result = fusion.search(query, fusion_sources)
        _route_event("source_invoked", source="fusion", query=query,
                     elapsed_ms=int((time.monotonic() - invoke_start) * 1000))
        if not _looks_empty(result):
            _set_cached("fusion", cache_key_query, result)
        return result, "fusion"

    return _resolve_single_source(source, query)
