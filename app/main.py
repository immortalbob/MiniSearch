import asyncio
import threading
import concurrent.futures
import logging
import os
import sqlite3
import time
import requests
from contextlib import asynccontextmanager, AsyncExitStack, closing
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Header, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from starlette.routing import Mount
from pydantic import BaseModel

from app.router import (
    route_query,
    SOURCE_MAP,
    FALLBACK_CHAIN,
    get_cache_stats,
    get_cache_count,
    clear_cache,
    load_cache,
    load_routing_cache,
    warm_semantic_routing_cache,
    flush_caches,
    get_routing_cache_stats,
    clear_routing_cache,
)
from app.mcp_server import mcp_app, get_mcp_app
from app.sources.kiwix import get_books, refresh_catalog
from app.sources import uptime_kuma
from app import semantic_routing
from app.snapshots import (
    init_snapshot_db,
    snapshot_uptime,
    snapshot_forecast,
    snapshot_news,
    snapshot_ha,
    get_changes,
    format_changes,
    get_snapshot_job_health,
)
from app.adversarial_testing import (
    init_adversarial_db,
    run_adversarial_test_cycle,
    get_adversarial_test_summary,
    get_flagged_combinations,
    dismiss_flagged_combination,
    undismiss_flagged_combination,
)
from app.temporal_patterns import (
    init_temporal_patterns_db,
    run_temporal_pattern_mining_cycle,
    get_temporal_pattern_summary,
    get_temporal_patterns,
)
from app.history import (
    init_history_db,
    get_history_job_health,
    get_metrics_overview,
    fetch_samples,
)
from app.config import settings

# Defined once, used at both real call sites below (app.mount() and the
# lifespan function's own route-matching loop) — found via a deliberate
# function-by-function read of mcp_server.py and its real consumer here:
# the two were previously two independent "/mcp" string literals with
# nothing enforcing they ever agreed. If they silently drifted apart (a
# future refactor renaming one without the other), the lifespan's
# matching loop would find no route at all, silently leave the stale
# module-import-time mcp_app mounted, and reintroduce the exact
# "StreamableHTTPSessionManager.run() can only be called once" bug this
# fix exists to prevent — not at startup where it would be immediately
# obvious, but on the first real MCP request after the next restart.
MCP_MOUNT_PATH = "/mcp"

# Configure logging at startup — without this, the root logger defaults to
# WARNING with no attached handler, which silently swallows every
# _LOGGER.info() call across the entire codebase (router.py's decomposition
# logging, kiwix.py's disambiguation/article selection logging, snapshots.py's
# job logging, etc). Only uvicorn's own access logger (a separate logger with
# its own handler) was ever visible in `docker logs`, making it look like
# requests were processed silently with no application-level diagnostic
# output at all — found via real debugging where expected INFO log lines
# never appeared despite the underlying code paths definitely running.
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# API key authentication — protects /search and /changes only
# ---------------------------------------------------------------------------

def _valid_api_keys() -> set[str]:
    """Parse the comma-separated API_KEYS setting into a set of valid keys."""
    if not settings.api_keys:
        return set()
    return {k.strip() for k in settings.api_keys.split(",") if k.strip()}


async def require_api_key(x_api_key: str | None = Header(default=None)):
    """
    FastAPI dependency enforcing API key auth on protected endpoints.
    No-op (always passes) if API_KEYS is unset — auth is opt-in and
    backward compatible with existing deployments.
    """
    valid_keys = _valid_api_keys()
    if not valid_keys:
        return  # auth disabled
    if not x_api_key or x_api_key not in valid_keys:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")

# ---------------------------------------------------------------------------
# Query logging — SQLite
# ---------------------------------------------------------------------------

_LOG_DB = "/app/data/query_log.db"

# Shared by /backup and /backup/info — found via a deliberate
# "bulletproofing" pass: this exact list was duplicated identically in
# both functions, a real, if minor, maintenance risk where adding or
# removing a tracked data file could easily update one copy and forget
# the other, leaving the two endpoints silently disagreeing about what
# Mnemolis actually tracks.
_BACKUP_DATA_FILES = [
    "/app/data/cache.json",
    "/app/data/routing_cache.json",
    "/app/data/query_log.db",
    "/app/data/snapshots.db",
    "/app/data/adversarial_testing.db",
    "/app/data/temporal_patterns.db",
    "/app/data/history.db",
]


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and busy timeout to reduce lock contention."""
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _init_log_db():
    """Create query log table if it doesn't exist, and migrate existing
    tables to add columns introduced after the table was first created.

    CREATE TABLE IF NOT EXISTS only affects fresh installs — an existing
    deployment's table already exists and won't gain new columns just
    because the CREATE statement changed, so any new column needs an
    explicit ALTER TABLE migration here, run defensively (existing
    databases that already have the column will hit a harmless,
    caught exception on the ALTER).
    """
    try:
        with closing(_connect(_LOG_DB)) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS query_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    query TEXT NOT NULL,
                    source_requested TEXT NOT NULL,
                    source_used TEXT NOT NULL,
                    cached INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    fallback_occurred INTEGER NOT NULL DEFAULT 0,
                    synthesized INTEGER
                )
            """)
            # Migration for tables created before fallback_occurred existed —
            # ALTER TABLE ADD COLUMN fails harmlessly if the column is already
            # present (fresh installs created with the CREATE TABLE above
            # already have it), so this is safe to run unconditionally
            try:
                con.execute("ALTER TABLE query_log ADD COLUMN fallback_occurred INTEGER NOT NULL DEFAULT 0")
            except Exception:
                pass  # column already exists
            # Migration for the synthesized flag (Design Doc 4) — nullable
            # so pre-synthesis rows read as NULL ("synthesis wasn't a
            # concept then") rather than a misleading 0 ("synthesis ran and
            # was skipped"). Guarded by the same defensive try/except; a
            # PRAGMA table_info check would work equally, but the caught
            # ALTER is the established pattern for this DB.
            try:
                con.execute("ALTER TABLE query_log ADD COLUMN synthesized INTEGER")
            except Exception:
                pass  # column already exists
            con.commit()
    except Exception as e:
        _LOGGER.warning("Could not initialize query log db: %s", e)


def _log_query(query: str, source_requested: str, source_used: str, cached: bool, success: bool, latency_ms: int, fallback_occurred: bool = False, synthesized: int | None = None):
    """Write a query log entry.

    `synthesized` is a nullable status code, not a bare boolean, because
    /logs/stats needs to tell four synthesis outcomes apart from this one
    column: NULL = synthesis not requested; 1 = a real grounded answer
    was served; 2 = the honest NOT_IN_SOURCES miss (a success, but no
    sources); 0 = requested but skipped or gate-rejected (no answer). A
    pre-synthesis row reads NULL, which is correct — synthesis wasn't a
    concept when it was written.
    """
    try:
        with closing(_connect(_LOG_DB)) as con:
            con.execute(
                "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms, fallback_occurred, synthesized) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), query, source_requested, source_used, int(cached), int(success), latency_ms, int(fallback_occurred), synthesized)
            )
            con.commit()
    except Exception as e:
        _LOGGER.warning("Could not write query log: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load Kiwix catalog, cache, and start snapshot scheduler on startup.

    Also rebuilds and re-mounts the MCP Streamable HTTP app fresh on every
    startup, then enters its lifespan — a real, currently-open issue
    across the broader MCP/FastMCP ecosystem (not specific to how
    Mnemolis is built) made this more involved than a simple "enter the
    sub-app's lifespan too":

    `FastMCP.streamable_http_app()` lazily creates ONE session manager
    and caches it on the FastMCP instance — calling it again still
    returns the same cached manager wrapped in a NEW Starlette app, but
    `StreamableHTTPSessionManager.run()` can only ever be entered once
    per instance. A module-level `mcp_app` built once at import time
    means every independent app lifecycle (every container restart in
    production; every `with TestClient(app) as client:` block in this
    test suite) tries to re-run the same already-exhausted session
    manager, raising a hard RuntimeError on the second attempt — this
    surfaced as several `test_security.py` tests failing only when run
    after `test_main.py`, since both files spin up their own TestClient
    against the same imported `app`.

    Verified the first attempt at fixing this was genuinely incomplete:
    resetting `mcp._session_manager` to None before calling
    `streamable_http_app()` again does create a fresh session manager,
    but the ALREADY-MOUNTED route from module-import time still holds a
    reference to the OLD app object's lifespan closure and request
    handler — resetting the FastMCP instance's cached attribute doesn't
    retroactively change what an already-built Starlette app's router
    closure points to. The real fix rebuilds the app fresh AND finds the
    actual `/mcp` Mount route in app.router.routes to reassign its `.app`
    reference, so the object whose lifespan gets entered is genuinely the
    same object that serves real requests during that lifecycle — not
    two different objects that happen to share an FastMCP instance.
    """
    fresh_mcp_app = get_mcp_app()
    found_mount = False
    for r in app.router.routes:
        if isinstance(r, Mount) and r.path == MCP_MOUNT_PATH:
            r.app = fresh_mcp_app
            found_mount = True
            break
    if not found_mount:
        # Defense in depth, mirroring this project's existing pattern for
        # similar real risks (e.g. ADVERSARIAL_TEST_ENABLED checked at
        # both scheduler-registration time and inside the cycle function
        # itself): if this ever fires, the stale module-import-time
        # mcp_app is still mounted and will fail on its first real
        # request, not at startup — loud here is strictly better than
        # silent there.
        _LOGGER.warning(
            "No Mount route found at '%s' during startup — the MCP app "
            "was NOT refreshed for this lifecycle. If this fires, check "
            "that MCP_MOUNT_PATH and the app.mount() call below still agree.",
            MCP_MOUNT_PATH,
        )

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(fresh_mcp_app.router.lifespan_context(fresh_mcp_app))

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, get_books)
        await loop.run_in_executor(None, load_cache)
        await loop.run_in_executor(None, load_routing_cache)
        # Semantic routing cache warmup — re-embed the routing cache's
        # just-loaded persisted queries so rephrasings match immediately
        # after a restart instead of only after each query has been
        # re-decided once (the store itself is deliberately in-memory
        # only). A daemon THREAD, not an awaited executor call like the
        # loads above: those must finish before requests are served,
        # while the warmup must NOT delay startup — it's pure
        # optimization, its absence is just the lazy population that was
        # already normal, and it makes network calls to a backend that
        # may itself still be booting after a host restart. Guarded
        # inside warm_semantic_routing_cache() (no-op when
        # SEMANTIC_WARMUP_ENABLED is off or no EMBEDDING_MODEL is set).
        threading.Thread(
            target=warm_semantic_routing_cache,
            name="semantic-warmup",
            daemon=True,
        ).start()
        await loop.run_in_executor(None, _init_log_db)
        await loop.run_in_executor(None, init_snapshot_db)
        if settings.adversarial_test_enabled:
            await loop.run_in_executor(None, init_adversarial_db)
        if settings.temporal_pattern_detection_enabled:
            await loop.run_in_executor(None, init_temporal_patterns_db)
        if settings.history_enabled:
            await loop.run_in_executor(None, init_history_db)

        # Warm the persistent Uptime Kuma connection (if configured) before
        # the scheduler starts -- connects and logs in once here, during
        # startup, rather than paying that cost on whichever request or
        # snapshot tick happens to arrive first. See get_connection() in
        # app/sources/uptime_kuma.py for why this exists: a fresh
        # Socket.IO handshake+login on every single call (a live query AND
        # every 2-minute snapshot_uptime() tick) was the real, confirmed
        # cost behind uptime's recurring benchmark tail (v3.17.0, v3.44.0,
        # v3.50.2) -- not the result-cache TTL, which only explains why a
        # cache miss happens, not why it costs as much as it does.
        if settings.uptime_kuma_url and settings.uptime_kuma_username:
            await loop.run_in_executor(None, uptime_kuma.warm_connection)
            stack.callback(uptime_kuma.disconnect)

        # Start snapshot scheduler
        scheduler = BackgroundScheduler()
        scheduler.add_job(snapshot_uptime, "interval", minutes=2, id="snapshot_uptime")
        scheduler.add_job(snapshot_forecast, "interval", minutes=30, id="snapshot_forecast")
        scheduler.add_job(snapshot_news, "interval", minutes=60, id="snapshot_news")
        scheduler.add_job(snapshot_ha, "interval", minutes=5, id="snapshot_ha")
        if settings.adversarial_test_enabled:
            scheduler.add_job(
                run_adversarial_test_cycle, "interval",
                minutes=settings.adversarial_test_interval_minutes,
                id="adversarial_testing",
            )
        else:
            _LOGGER.info("Adversarial testing is disabled (ADVERSARIAL_TEST_ENABLED=false); scheduler job not registered")
        if settings.temporal_pattern_detection_enabled:
            # Mirrors adversarial testing's exact defense-in-depth
            # pattern: checked here at scheduler-registration time AND
            # again inside run_temporal_pattern_mining_cycle() itself,
            # so a direct call (including a future manual-trigger
            # endpoint) can never accidentally mine real event data
            # while the feature is supposed to be off.
            scheduler.add_job(
                run_temporal_pattern_mining_cycle, "interval",
                hours=settings.temporal_pattern_mining_interval_hours,
                id="temporal_pattern_mining",
            )
        else:
            _LOGGER.info("Temporal pattern detection is disabled (TEMPORAL_PATTERN_DETECTION_ENABLED=false); scheduler job not registered")
        # NOTE: history has NO scheduler job of its own — it ingests
        # inside snapshot_ha()/snapshot_uptime(), riding the fetches those
        # jobs already make (Design Doc 5 constraint #1: zero additional
        # HA/uptime load). init_history_db() above is its only startup
        # step; the first ingest happens on the immediate snapshot calls
        # below.
        if not settings.history_enabled:
            _LOGGER.info("History source is disabled (HISTORY_ENABLED=false); snapshot jobs will not ingest samples")
        scheduler.start()
        _LOGGER.info("Snapshot scheduler started")

        # Take immediate snapshots on startup so /changes has data right away
        await loop.run_in_executor(None, snapshot_uptime)
        await loop.run_in_executor(None, snapshot_forecast)
        await loop.run_in_executor(None, snapshot_news)
        await loop.run_in_executor(None, snapshot_ha)

        yield

        scheduler.shutdown()
        _LOGGER.info("Snapshot scheduler stopped")
        # Both caches batch their disk writes (every N entries), so up
        # to N-1 recent entries live only in memory at any moment — fine
        # for a hard crash, but a clean shutdown shouldn't discard them
        # when one final write preserves everything. See
        # router.flush_caches()'s own docstring.
        flush_caches()
        _LOGGER.info("Caches flushed to disk")


app = FastAPI(
    title="Mnemolis",
    description="Unified local knowledge search API with multi-source fusion. Routes queries to Kiwix, Open-Meteo, FreshRSS, SearXNG, Uptime Kuma, or multiple sources concurrently.",
    version="3.56.0",
    lifespan=lifespan,
)

app.mount(MCP_MOUNT_PATH, mcp_app)


class SearchRequest(BaseModel):
    query: str
    source: str = "auto"
    fusion_sources: list[str] | None = None  # only used when source="fusion"
    # Explanation chains — when true, the response's `explanation` field
    # carries the ordered trace of routing events (intent resolution,
    # cache hits, source invocations with elapsed times, fallbacks,
    # decomposition, conditionals, semantic matches). Off by default:
    # without it the field is null, so existing clients see one new
    # always-null key and nothing else changes. See
    # wiki/Explanation-Chains.md.
    explain: bool = False
    # Grounded answer synthesis (Design Doc 4) — when true (and
    # SYNTHESIS_ENABLED is set), the local LLM composes a short, grounded
    # answer from the retrieved material and returns it in the separate
    # `answer` field ALONGSIDE the raw `result`, never instead of it. Off
    # by default and additive: without it the answer/answer_sources/
    # synthesized fields are null/false and the response is byte-identical
    # to today's. answer_style controls length and shape: "voice" (≤2
    # sentences, for TTS), "brief" (one short paragraph, default),
    # "detailed" (a few paragraphs), or "digest" (many distinct items
    # preserved rather than fused — for "summarize/list/read me
    # everything" queries). See wiki/Answer-Synthesis.md.
    synthesize: bool = False
    answer_style: str = "brief"


class SearchResponse(BaseModel):
    query: str
    source_used: str
    result: str
    success: bool
    cached: bool = False
    error: Optional[str] = None
    # Present only when the request set explain=true — see SearchRequest.
    explanation: Optional[list[dict]] = None
    # Grounded answer synthesis — see SearchRequest.synthesize. answer is
    # null on every skip/failure (the raw `result` is always intact);
    # answer_sources names the sources the answer drew on; synthesized is
    # true iff answer is non-null.
    answer: Optional[str] = None
    answer_sources: list[str] = []
    synthesized: bool = False


def _check_kiwix() -> dict:
    books = get_books()
    if not settings.kiwix_url:
        return {"status": "not_configured"}
    try:
        resp = requests.get(f"{settings.kiwix_url}/catalog/v2/entries?count=1", timeout=3)
        resp.raise_for_status()
        return {"status": "ok", "books": len(books)}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_forecast() -> dict:
    if not settings.forecast_latitude or not settings.forecast_longitude:
        return {"status": "not_configured"}
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": settings.forecast_latitude, "longitude": settings.forecast_longitude, "current": "temperature_2m"},
            timeout=5,
        )
        resp.raise_for_status()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_news() -> dict:
    if not settings.freshrss_url or not settings.freshrss_user:
        return {"status": "not_configured"}
    try:
        resp = requests.get(f"{settings.freshrss_url}/api/greader.php/accounts/ClientLogin", timeout=3)
        # 401 means reachable but needs auth — that's fine, service is up
        if resp.status_code in (200, 401, 400):
            return {"status": "ok"}
        return {"status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_web() -> dict:
    if not settings.searxng_url:
        return {"status": "not_configured"}
    try:
        resp = requests.get(f"{settings.searxng_url}/healthz", timeout=3)
        if resp.status_code in (200, 404):  # 404 means SearXNG is up but no /healthz route
            return {"status": "ok"}
        return {"status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_uptime() -> dict:
    if not settings.uptime_kuma_url or not settings.uptime_kuma_username:
        return {"status": "not_configured"}
    try:
        resp = requests.get(settings.uptime_kuma_url, timeout=3)
        if resp.status_code in (200, 301, 302):
            return {"status": "ok"}
        return {"status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_ha() -> dict:
    if not settings.ha_url or not settings.ha_token:
        return {"status": "not_configured"}
    try:
        resp = requests.get(
            f"{settings.ha_url}/api/",
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=3,
        )
        if resp.status_code == 200:
            return {"status": "ok"}
        if resp.status_code == 401:
            return {"status": "error", "error": "Invalid token"}
        return {"status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_llm() -> dict:
    if not settings.llm_url or not settings.llm_model:
        return {"status": "not_configured"}
    try:
        if settings.llm_api_type == "openai":
            resp = requests.get(f"{settings.llm_url}/v1/models", timeout=3)
        else:
            resp = requests.get(f"{settings.llm_url}/api/tags", timeout=3)
        if resp.status_code == 200:
            return {"status": "ok", "model": settings.llm_model, "api_type": settings.llm_api_type}
        return {"status": "error", "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/health")
def health():
    """
    Health check. Returns container status, Kiwix books loaded, result and
    routing cache entry counts (with their configured max sizes, so growth
    toward the bound is visible without needing to dig through logs),
    background snapshot job health (each job's status compared against
    its expected interval, since every snapshot job already catches its
    own exceptions and silently logs a warning rather than surfacing
    failure anywhere externally visible), and connectivity status for
    every configured source.

    The seven source checks below run concurrently, not sequentially.
    Found via a real v3.50.2 benchmark run: a warm-cache /health sample
    hit 5244ms, several times worse than its own 750ms median — each
    check is a real network call with its own 3-5 second timeout, run as
    plain sequential calls, so one or two slow real checks (the LLM ping
    reaching across to a separate machine; a slow SearXNG /healthz) could
    stack additively into a multi-second worst case. Every _check_*
    function already catches its own exceptions internally and never
    raises, so this is genuinely safe to parallelize the same way
    fusion.py's own multi-source dispatch already is — no exception
    handling needed in the executor itself, unlike fusion's case.
    """
    books = get_books()
    checks = {
        "kiwix": _check_kiwix,
        "forecast": _check_forecast,
        "news": _check_news,
        "web": _check_web,
        "uptime": _check_uptime,
        "ha": _check_ha,
        "llm": _check_llm,
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(checks)) as executor:
        futures = {name: executor.submit(fn) for name, fn in checks.items()}
        sources = {name: future.result() for name, future in futures.items()}
    # Overall status — ok if container is running regardless of source health
    return {
        "status": "ok",
        "kiwix_books_loaded": len(books),
        "cache_entries": get_cache_count(),
        "cache_max_size": settings.cache_max_size,
        "routing_cache_entries": len(get_routing_cache_stats()),
        "routing_cache_max_size": settings.routing_cache_max_size,
        "snapshot_jobs": get_snapshot_job_health(),
        "adversarial_testing": get_adversarial_test_summary(),
        "temporal_pattern_detection": get_temporal_pattern_summary(),
        "history": get_history_job_health(),
        "sources": sources,
    }


@app.get("/sources")
def list_sources():
    """List all available search sources including 'auto'."""
    return {"sources": list(SOURCE_MAP.keys()) + ["auto"]}


@app.get("/catalog")
def catalog():
    """List all books currently loaded from the Kiwix OPDS catalog."""
    books = get_books()
    return {"count": len(books), "books": books}


@app.post("/catalog/refresh")
def catalog_refresh():
    """
    Force a re-scan of the Kiwix OPDS catalog without restarting the container.
    Use this after adding new ZIM files to Kiwix.
    """
    books = refresh_catalog()
    return {"status": "refreshed", "count": len(books)}


@app.get("/cache")
def cache_stats():
    """
    Show all current result cache entries with age and remaining TTL.
    Result cache stores actual search results keyed by source and query.
    """
    entries = get_cache_stats()
    return {"count": len(entries), "entries": entries}


@app.post("/cache/clear")
def cache_clear():
    """Clear all result cache entries from memory and disk."""
    count = clear_cache()
    return {"status": "cleared", "entries_removed": count}


@app.get("/cache/routing")
def routing_cache_stats():
    """
    Show all current routing cache entries with age and remaining TTL.
    Routing cache stores source and Kiwix book selection decisions to avoid
    redundant Ollama calls for repeated queries.
    """
    entries = get_routing_cache_stats()
    return {"count": len(entries), "entries": entries}


@app.post("/cache/routing/clear")
def routing_cache_clear():
    """Clear all routing cache entries from memory and disk."""
    count = clear_routing_cache()
    return {"status": "cleared", "entries_removed": count}


@app.get("/cache/semantic")
def semantic_cache_stats():
    """
    Show the semantic routing cache's current state — whether it's
    enabled (EMBEDDING_MODEL configured), how many query embeddings are
    stored, which model produced them, and the active similarity
    threshold. The store holds only embeddings, never decisions; every
    match reads its decision back from the live routing cache at match
    time (see app/semantic_routing.py).
    """
    return semantic_routing.stats()


@app.post("/cache/semantic/clear")
def semantic_cache_clear():
    """Clear the semantic routing cache (in-memory only — there is
    deliberately nothing on disk to clear; see the persistence
    rationale in app/semantic_routing.py's module docstring)."""
    count = semantic_routing.clear()
    return {"status": "cleared", "entries_removed": count}


@app.post("/search", response_model=SearchResponse, dependencies=[Depends(require_api_key)])
def search(request: SearchRequest):
    # route_query() returns everything this endpoint previously
    # reconstructed around route_with_source() — the ACTUAL source that
    # produced the result (fallbacks included), whether the response was
    # served entirely from cache, and whether any FALLBACK_CHAIN fallback
    # genuinely fired. Found via a deliberate function-by-function audit:
    # this endpoint previously called detect_intent(request.query) BEFORE
    # routing, purely to compute the `cached` flag — meaning a query that
    # route_with_source() decomposes or treats as conditional paid a
    # full, wasted cold LLM routing call for a full-query intent decision
    # those paths never use, and the `cached` flag was computed against a
    # single-source cache key that doesn't correspond to anything those
    # paths actually do. Fallback detection was likewise inferred after
    # the fact by comparing pre-computed intent against the resolved
    # source — an inference (its own former comment here said so) that
    # was structurally blind to fallbacks inside decomposed sub-queries.
    # route_query() records all three facts at the exact code points
    # where they genuinely happen instead — see its docstring and the
    # _ROUTE_STATS comment in app/router.py for the mechanism and why
    # route_with_source()'s own signature deliberately stays unchanged.
    start = time.monotonic()
    outcome = route_query(
        request.query, request.source, request.fusion_sources,
        synthesize=request.synthesize, answer_style=request.answer_style,
    )
    latency_ms = int((time.monotonic() - start) * 1000)

    # Nullable synthesis status for /logs/stats — see _log_query's
    # docstring. The honest NOT_IN_SOURCES miss is distinguished from a
    # real served answer by its empty answer_sources (a served answer
    # always carries at least one source tag).
    if not request.synthesize:
        synth_status: int | None = None
    elif outcome.synthesized and outcome.answer_sources:
        synth_status = 1  # served a real grounded answer
    elif outcome.synthesized:
        synth_status = 2  # honest NOT_IN_SOURCES miss
    else:
        synth_status = 0  # requested but skipped / gate-rejected

    _log_query(
        request.query, request.source, outcome.source_used,
        outcome.cached, outcome.success, latency_ms, outcome.fallback_occurred,
        synthesized=synth_status,
    )
    if not outcome.success:
        _LOGGER.error("Search failed for query '%s': %s", request.query, outcome.error)
    return SearchResponse(
        query=request.query,
        source_used=outcome.source_used,
        result=outcome.result,
        success=outcome.success,
        cached=outcome.cached,
        error=outcome.error,
        # The chain is always COLLECTED (see route_query()'s stats dict)
        # but only RETURNED on request — on failure it's the partial
        # trace of what ran before things broke, which is when a trace
        # earns its keep most.
        explanation=outcome.explanation if request.explain else None,
        # Additive synthesis fields — null/[]/false unless synthesize=true
        # produced a real answer; the raw `result` above is untouched
        # regardless (Design Doc 4 constraint #1).
        answer=outcome.answer,
        answer_sources=outcome.answer_sources,
        synthesized=outcome.synthesized,
    )


@app.get("/logs")
def query_logs(limit: int = 50):
    """
    Show recent query log entries. Returns the most recent queries with
    timestamp, source, cached flag, success, and latency in milliseconds.
    """
    # Found via a deliberate "bulletproofing" pass: SQLite treats a
    # negative LIMIT value as "no limit at all" (documented behavior,
    # confirmed directly), so GET /logs?limit=-1 would return the
    # ENTIRE query log, defeating this endpoint's own intent of showing
    # a bounded, recent-entries view. Low real-world severity at
    # realistic homelab scale (tens of thousands of rows over a year,
    # not the millions that would make this a genuine memory concern),
    # but a real correctness gap worth a cheap, simple bound regardless.
    limit = max(1, min(limit, 1000))
    try:
        with closing(_connect(_LOG_DB)) as con:
            rows = con.execute(
                "SELECT timestamp, query, source_requested, source_used, cached, success, latency_ms FROM query_log ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
        entries = [
            {
                "timestamp": r[0],
                "query": r[1],
                "source_requested": r[2],
                "source_used": r[3],
                "cached": bool(r[4]),
                "success": bool(r[5]),
                "latency_ms": r[6],
            }
            for r in rows
        ]
        return {"count": len(entries), "entries": entries}
    except Exception as e:
        return {"count": 0, "entries": [], "error": str(e)}


@app.post("/logs/clear")
def logs_clear():
    """Clear all query log entries."""
    try:
        with closing(_connect(_LOG_DB)) as con:
            cur = con.execute("DELETE FROM query_log")
            count = cur.rowcount
            con.commit()
        return {"status": "cleared", "entries_removed": count}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.get("/backup")
def backup():
    """
    Create a backup of all Mnemolis data — result cache, routing cache,
    query log, and snapshot history — and return it as a downloadable tarball.

    Restore by stopping the container, extracting the tarball into the
    /app/data volume, and restarting. See README for full instructions.
    """
    import tarfile
    import tempfile
    import shutil

    data_files = _BACKUP_DATA_FILES

    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
        tmp_path = tmp.name
        tmp.close()

        # Every Mnemolis SQLite database runs in WAL mode (see
        # _connect()), which means the main .db file on disk is NOT the
        # complete database at an arbitrary moment — recent committed
        # writes live in the -wal sidecar file until the next
        # checkpoint. Found via a deliberate function-by-function audit:
        # tar.add()-ing the bare .db file directly (the previous
        # behavior) silently produced backups missing every
        # not-yet-checkpointed write, and — if a writer was mid-commit
        # at the exact moment of the copy — potentially an internally
        # inconsistent file. SQLite's own online backup API
        # (Connection.backup()) exists for exactly this: it produces a
        # complete, consistent, checkpointed single-file snapshot while
        # concurrent writers keep working, no downtime needed. Each .db
        # is snapshotted into a temp dir first and the snapshot is what
        # goes into the tarball; JSON files (already written atomically
        # via router._atomic_write_json()) are tarred directly as
        # before. If the backup API fails for a given file (e.g. a
        # genuinely corrupted database), fall back to the old raw copy
        # with a warning — a possibly-stale backup of that one file
        # beats silently omitting it.
        snapshot_dir = tempfile.mkdtemp(prefix="mnemolis-backup-")
        try:
            with tarfile.open(tmp_path, "w:gz") as tar:
                included = []
                for f in data_files:
                    if not os.path.exists(f):
                        continue
                    name = os.path.basename(f)
                    if f.endswith(".db"):
                        snap_path = os.path.join(snapshot_dir, name)
                        try:
                            src = sqlite3.connect(f, timeout=10)
                            try:
                                dst = sqlite3.connect(snap_path)
                                try:
                                    src.backup(dst)
                                finally:
                                    dst.close()
                            finally:
                                src.close()
                            tar.add(snap_path, arcname=name)
                        except Exception as e:
                            _LOGGER.warning(
                                "SQLite online backup failed for %s (%s) — "
                                "falling back to a raw file copy, which may "
                                "miss un-checkpointed WAL writes", name, e
                            )
                            tar.add(f, arcname=name)
                    else:
                        tar.add(f, arcname=name)
                    included.append(name)
        finally:
            shutil.rmtree(snapshot_dir, ignore_errors=True)

        # Found via a deliberate "bulletproofing" pass: `included` was
        # built (tracking which data files genuinely existed and got
        # backed up) but never actually used for anything — not
        # returned, not logged, just discarded. Since /backup returns a
        # raw file download rather than JSON, there's no clean way to
        # surface this in the response itself, but logging it costs
        # nothing and gives real diagnostic value for anyone checking
        # container logs after a backup — e.g. confirming snapshots.db
        # genuinely existed and was included, rather than the backup
        # silently succeeding with fewer files than expected.
        missing = [os.path.basename(f) for f in data_files if os.path.basename(f) not in included]
        _LOGGER.info("Backup created: included=%s missing=%s", included, missing)

        timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        filename = f"mnemolis-backup-{timestamp}.tar.gz"

        return FileResponse(
            tmp_path,
            media_type="application/gzip",
            filename=filename,
            background=BackgroundTask(os.unlink, tmp_path),
        )
    except Exception as e:
        _LOGGER.error("Backup failed: %s", e)
        return {"status": "error", "error": str(e)}


@app.get("/backup/info")
def backup_info():
    """
    Show what would be included in a backup without creating one —
    file sizes and last-modified times for each data file.
    """
    data_files = _BACKUP_DATA_FILES
    info = {}
    for f in data_files:
        name = os.path.basename(f)
        if os.path.exists(f):
            stat = os.stat(f)
            info[name] = {
                "exists": True,
                "size_bytes": stat.st_size,
                "modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(stat.st_mtime)),
            }
        else:
            info[name] = {"exists": False}
    return {"files": info}


@app.get("/areas")
def areas():
    """
    List all Home Assistant areas detected via the area registry, with
    entity counts and the natural-language phrases that resolve to each
    one (e.g. "living room", "master bath").

    Returns not_configured if HA_URL/HA_TOKEN are unset, or error if the
    HA area registry could not be reached.
    """
    from app.sources.home_assistant import list_areas
    return list_areas()


@app.get("/history/metrics", dependencies=[Depends(require_api_key)])
def history_metrics():
    """
    The recorded-metric catalog with per-metric sample counts and coverage
    (oldest/newest sample) — the /areas analogue for the History source
    (Design Doc 5 §6). Under require_api_key: unlike /areas (which only
    lists room names), this exposes what sensor data has actually been
    recorded and how much, which is house telemetry worth gating behind
    the same key /search and /changes already use.
    """
    if not settings.history_enabled:
        return {"status": "disabled", "metric_count": 0, "metrics": []}
    return get_metrics_overview()


@app.get("/history/series", dependencies=[Depends(require_api_key)])
def history_series(metric: str, hours: int = Query(default=24, ge=1, le=2160)):
    """
    Raw (value, ts) series for one metric over the last N hours — the
    read endpoint a future NOC/device-registry dashboard renders sparklines
    from (Design Doc 5 §9 names charts a non-goal for the text API but the
    right renderer is a separate app hitting a raw-series endpoint). Added
    in v1 deliberately rather than deferred: it's trivial, and shipping it
    now means the dashboard app doesn't need a follow-up Mnemolis release.

    Returns UTC-ISO timestamps (the storage convention); the caller
    localizes for display, exactly as the text adapter does.
    """
    if not settings.history_enabled:
        return {"status": "disabled", "metric": metric, "hours": hours, "samples": []}
    from datetime import datetime, timezone, timedelta
    from app.timeutil import TIMESTAMP_FORMAT
    now = datetime.now(timezone.utc)
    start = (now - timedelta(hours=hours)).strftime(TIMESTAMP_FORMAT)
    end = now.strftime(TIMESTAMP_FORMAT)
    samples = fetch_samples(metric, start, end)
    return {
        "status": "ok",
        "metric": metric,
        "hours": hours,
        "count": len(samples),
        "samples": [{"value": s.value, "ts": s.ts} for s in samples],
    }


@app.get("/changes", dependencies=[Depends(require_api_key)])
def changes(hours: int = 24):
    """
    Return meaningful changes detected across all snapshot sources
    within the last N hours (default 24).

    Detects:
    - Service outages and recoveries (Uptime Kuma)
    - Meaningful weather forecast changes (Open-Meteo)
    - New news articles (FreshRSS)
    - Lock state changes, door sensor changes, low battery alerts (Home Assistant)
    """
    detected = get_changes(since_hours=hours)
    formatted = format_changes(detected, since_hours=hours)
    return {
        "since_hours": hours,
        "changes_detected": sum(len(v) for v in detected.values()),
        "sources_with_changes": list(detected.keys()),
        "result": formatted,
    }


@app.post("/snapshots/trigger")
def trigger_snapshots():
    """Manually trigger all snapshot jobs immediately."""
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(snapshot_uptime),
            executor.submit(snapshot_forecast),
            executor.submit(snapshot_news),
            executor.submit(snapshot_ha),
        ]
        concurrent.futures.wait(futures)
    return {"status": "ok", "snapshots_triggered": ["uptime", "forecast", "news", "ha"]}


@app.post("/adversarial/trigger")
def trigger_adversarial_test():
    """
    Manually trigger one adversarial self-testing cycle immediately,
    rather than waiting for the next scheduled tick — mirrors
    /snapshots/trigger's exact pattern for the same reason: a person
    actively watching this feature shouldn't need to wait up to
    ADVERSARIAL_TEST_INTERVAL_MINUTES (default 60) just to see it run.

    Returns {"status": "disabled", ...} without running anything if
    ADVERSARIAL_TEST_ENABLED is false — run_adversarial_test_cycle()
    checks this itself too (defense in depth), but surfacing it here
    directly means a person manually triggering this gets an honest,
    immediate answer instead of a misleadingly generic success response.
    """
    return run_adversarial_test_cycle()


@app.get("/adversarial/flagged")
def adversarial_flagged(limit: int = 50, include_dismissed: bool = False):
    """
    Return adversarial self-testing combinations for human review — the
    real, queryable home for the "logs results for periodic review"
    mechanism the roadmap's Adversarial Self-Testing entry called for.

    Returns the UNION of "currently flagged" and "ever flagged, not yet
    dismissed" — not just rows actively anomalous on their most recent
    run. Fixes a real gap a reviewer caught: a combination flagged once
    for an intermittent anomaly could previously vanish from this
    endpoint the moment it happened to be re-rolled and came back
    clean, with no human ever having reviewed or dismissed it. Each row
    includes ever_flagged, first_flagged_reason/timestamp (the ORIGINAL
    anomaly, preserved even after later clean runs), and
    currently_flagged (true only if last_flagged_reason is still set
    right now) so a human can tell "still actively broken" apart from
    "flagged once, currently clean, still needs a look."

    Pass include_dismissed=true to also see rows already reviewed and
    closed via POST /adversarial/dismiss — useful for an audit trail,
    not the default working view.

    Each row also includes last_flagged_result_excerpt — up to 500
    characters of the actual response text that triggered the most
    recent flag (null on a clean run; never populated for combinations
    that have never flagged at all). Added after a real, unresolved
    investigation hit a hard wall: a flag fired once, every code-level
    hypothesis for why was checked and ruled out against real evidence,
    and the schema had no way to ever recover the actual text that
    matched a real anomaly check — only the fact that something did.
    This field exists so the next occurrence is diagnosable from the
    stored evidence directly, not by re-deriving and ruling out every
    possible mechanism after the fact.

    Deliberately left unauthenticated, same as /health and /areas: this
    exposes only synthetic, generated test queries and their structural
    anomaly flags, never real user queries or cache contents, so it sits
    outside API_KEYS' documented scope (POST /search and GET /changes
    only) the same principled way those two endpoints already do. This
    was a deliberate decision, not a default left unconsidered — see the
    design doc's section 8 for the reasoning.

    Each row is a structural anomaly, never a correctness judgment — see
    app/adversarial_testing.py's module docstring for why that distinction
    is the single hardest requirement on this whole feature.
    """
    if not settings.adversarial_test_enabled:
        return {"status": "disabled", "count": 0, "flagged": []}
    flagged = get_flagged_combinations(limit=limit, include_dismissed=include_dismissed)
    return {
        "count": len(flagged),
        "flagged": flagged,
    }


@app.post("/adversarial/dismiss")
def adversarial_dismiss(fingerprint: str):
    """
    Mark a flagged combination as reviewed and dismissed by a human —
    the action that actually closes the loop the ever_flagged tracking
    exists to support. A dismissed combination stops appearing in the
    default GET /adversarial/flagged view (still visible with
    include_dismissed=true), but its history is never deleted — a
    fresh, NEW flag on the same fingerprint later will resurface it
    normally, since a new anomaly is a new event worth a fresh look
    even if an earlier one was already closed out.

    fingerprint is the exact JSON string from a flagged row's own
    "fingerprint" field, copied verbatim from a prior
    GET /adversarial/flagged response — not something to construct by
    hand.

    Returns {"status": "dismissed"} on success, or a 404 if the
    fingerprint doesn't match any known combination.
    """
    success = dismiss_flagged_combination(fingerprint)
    if not success:
        raise HTTPException(status_code=404, detail="No combination found with that fingerprint")
    return {"status": "dismissed", "fingerprint": fingerprint}


@app.post("/adversarial/undismiss")
def adversarial_undismiss(fingerprint: str):
    """
    Reverse a dismissal, putting a combination back into the default
    GET /adversarial/flagged review queue. The real, symmetric
    counterpart to POST /adversarial/dismiss — added after real usage
    showed dismissal had no way back short of editing the database by
    hand: a batch dismissal matched against a stale, previously-seen
    listing rather than a freshly-fetched one closed out combinations
    that were never actually resolved.

    fingerprint is the exact JSON string from a flagged row's own
    "fingerprint" field — use GET /adversarial/flagged?include_dismissed=true
    to find a dismissed row's fingerprint, since the default view no
    longer shows it once dismissed.

    Returns {"status": "undismissed"} on success, or a 404 if the
    fingerprint doesn't match any known combination.
    """
    success = undismiss_flagged_combination(fingerprint)
    if not success:
        raise HTTPException(status_code=404, detail="No combination found with that fingerprint")
    return {"status": "undismissed", "fingerprint": fingerprint}


@app.get("/temporal-patterns")
def temporal_patterns(status: Optional[str] = None, limit: int = 100):
    """
    Return cross-source temporal pattern detection results — reliable
    timing relationships found between structured event types (e.g.
    a front-door lock event reliably preceding a motion event within
    some lag window), each with its own real, raw supporting count and
    the corrected statistical threshold it was compared against.

    Optional `status` filter: "candidate" (found, not yet re-validated
    against later data), "confirmed" (replicated against an
    independent, later window), or "unconfirmed" (failed to replicate
    — kept visible, not deleted, since that's a real, honestly-reported
    finding in its own right, not noise to clean up).

    EVERY row carries a literal "note" field stating this reflects
    observed timing correlation only and does NOT establish a causal
    relationship — not just in documentation a person might not read.
    This is a hard, non-negotiable requirement, not a stylistic choice:
    real, peer-reviewed temporal pattern-mining methods show real,
    measured false-positive rates well into the double digits even
    with correction applied, at data volumes Mnemolis's real homelab
    event history will likely never approach. See the design doc and
    wiki's Cross-Source Temporal Pattern Detection page for the full
    statistical reasoning behind every choice this feature makes.

    Deliberately left unauthenticated, same as /health, /areas, and
    /adversarial/flagged: this exposes only derived statistical
    summaries over Mnemolis's own structured event history, never raw
    query content, so it sits outside API_KEYS' documented scope (POST
    /search and GET /changes only) the same principled way those
    endpoints already do.

    This feature deliberately stays in its own dedicated endpoint,
    never blended into /changes or a real search response — a
    correlation-not-causation caveat is too easy to lose track of once
    folded into a normal conversational answer.
    """
    if not settings.temporal_pattern_detection_enabled:
        return {"status": "disabled", "count": 0, "patterns": []}
    patterns = get_temporal_patterns(status=status, limit=limit)
    return {
        "count": len(patterns),
        "patterns": patterns,
    }


@app.post("/temporal-patterns/trigger")
def trigger_temporal_pattern_mining():
    """
    Manually trigger one temporal pattern mining cycle immediately,
    rather than waiting for the next scheduled tick — mirrors
    /snapshots/trigger's and /adversarial/trigger's exact pattern for
    the same reason: a person actively watching this feature shouldn't
    need to wait up to TEMPORAL_PATTERN_MINING_INTERVAL_HOURS (default
    24) just to see it run.

    Returns {"status": "disabled", ...} without touching any real event
    data if TEMPORAL_PATTERN_DETECTION_ENABLED is false —
    run_temporal_pattern_mining_cycle() checks this itself too (defense
    in depth), but surfacing it here directly means a person manually
    triggering this gets an honest, immediate answer instead of a
    misleadingly generic success response.
    """
    return run_temporal_pattern_mining_cycle()


@app.get("/logs/stats")
def query_log_stats():
    """
    Query log statistics — observability into Mnemolis usage patterns.

    Returns:
    - Total queries, cache hit rate, success rate
    - Fallback count and rate — how often a result was empty enough to
      trigger FALLBACK_CHAIN (e.g. kiwix -> web). Recorded directly at
      the one code point where a fallback genuinely happens
      (router._resolve_single_source(), via the route_query() stats
      channel) rather than inferred after the fact by comparing a
      pre-computed intent against the resolved source — the old
      inference was structurally blind to fallbacks inside decomposed
      sub-queries, which are now counted too. For a decomposed query
      whose sub-query fell back, the row's source_used is the overall
      label ("fusion"), so such rows count toward the total
      fallback_count but not toward the per-target breakdown below,
      which matches rows by source_used.
    - Fallback breakdown by TARGET, not original source — when multiple
      sources share the same fallback target (kiwix and news both fall
      back to web), the boolean alone can't distinguish which one
      triggered a given fallback, so this is reported as a combined,
      honestly-labeled count (e.g. "kiwix_or_news_fallback_to_web")
      rather than guessing at an attribution the data doesn't support
    - Time To First Knowledge (TTFK) — average latency for first-seen queries
    - Average latency by source
    - Top 10 most-asked queries
    - Queries per source breakdown
    - Repeated queries with cache hit rate
    """
    con = None
    try:
        con = _connect(_LOG_DB)

        # Total queries and basic rates
        totals = con.execute("""
            SELECT
                COUNT(*) as total,
                SUM(cached) as cache_hits,
                SUM(success) as successes,
                AVG(latency_ms) as avg_latency_ms,
                SUM(fallback_occurred) as fallbacks
            FROM query_log
        """).fetchone()

        total = totals[0] or 0
        cache_hits = totals[1] or 0
        successes = totals[2] or 0
        avg_latency = round(totals[3] or 0, 1)
        fallbacks = totals[4] or 0

        # TTFK — average latency of first-seen queries (cached=0, first occurrence)
        # A query's "cold" cost is its first appearance in the log.
        # Found via a deliberate function-by-function read: the original
        # SQL selected MIN(id) and MIN(latency_ms) as two independent
        # aggregates in the same GROUP BY — MIN(id) gives the first
        # row's id correctly, but MIN(latency_ms) gives the SMALLEST
        # latency across all non-cached occurrences for that query, not
        # the latency of the row whose id is MIN(id). For a query asked
        # repeatedly without caching (e.g. adversarial test queries),
        # this would report the fastest cold run rather than the genuine
        # first cold hit — consistently under-estimating true TTFK.
        # Fixed with a correlated subquery joining back to the row that
        # actually has min(id), matching the documented intent precisely.
        ttfk_rows = con.execute("""
            SELECT AVG(q.latency_ms) FROM (
                SELECT MIN(id) as first_id
                FROM query_log
                WHERE cached = 0
                GROUP BY LOWER(TRIM(query))
            ) AS first_ids
            JOIN query_log q ON q.id = first_ids.first_id
        """).fetchone()
        ttfk_ms = round(ttfk_rows[0] or 0, 1)

        # Average latency by source — combined across cold AND warm
        # queries, not warm-only. Found via a deliberate complexity-
        # investigation pass: this comment previously claimed "warm
        # queries only," but the SQL below has no cached filter at all
        # and never did. Verified precisely which behavior is actually
        # more useful before deciding which one was wrong: constructing
        # a realistic two-source comparison (one genuinely slow,
        # network-bound source; one genuinely fast, local one) showed
        # that a TRUE warm-only average would mask almost all of the
        # real difference between them (15ms vs 12ms in the test, vs.
        # a real, honest 3000ms vs 80ms cold-only difference) — cache
        # hits are fast regardless of source, so warm-only averaging
        # tells you almost nothing about which source is actually
        # expensive when it has to do real work. The combined number
        # below at least reflects real, paid latency, even though it's
        # sensitive to cache-hit ratio. A cold-only breakdown would be
        # the genuinely most diagnostic version of this metric, but
        # that's a real, deliberate scope decision for a future change,
        # not something this fix took on — ttfk_ms already covers the
        # cold-specific story in aggregate, just not broken out by
        # source the way this field is.
        latency_by_source = {}
        for row in con.execute("""
            SELECT source_used, AVG(latency_ms) as avg_ms, COUNT(*) as count
            FROM query_log
            GROUP BY source_used
            ORDER BY count DESC
        """).fetchall():
            latency_by_source[row[0]] = {
                "avg_latency_ms": round(row[1], 1),
                "query_count": row[2],
            }

        # Fallback breakdown — reported per FALLBACK_CHAIN TARGET, not per
        # original source. A boolean column can't distinguish which
        # original source a fallback came from when multiple sources
        # share the same target (kiwix and news both fall back to web) —
        # querying "fallback_occurred=1 AND source_used='web'" would
        # double-count under separate 'kiwix' and 'news' labels if we
        # tried to attribute it to one or the other, since the same rows
        # would match both. Reporting it honestly as a combined count
        # against the shared target avoids that, at the cost of not being
        # able to say whether kiwix or news specifically struggled more —
        # a real, accepted limitation of using one boolean column rather
        # than recording the original source as text.
        fallback_by_target = {}
        fallback_targets = set(FALLBACK_CHAIN.values())
        for target in fallback_targets:
            sources_falling_back_here = [s for s, t in FALLBACK_CHAIN.items() if t == target]
            row = con.execute("""
                SELECT COUNT(*) FROM query_log
                WHERE fallback_occurred = 1 AND source_used = ?
            """, (target,)).fetchone()
            fallback_count = row[0] or 0
            if fallback_count > 0:
                label = "_or_".join(sorted(sources_falling_back_here))
                fallback_by_target[f"{label}_fallback_to_{target}"] = fallback_count

        # Top 10 most asked queries
        #
        # Found via a deliberate, precise re-read of this function:
        # selecting the bare `source_used` column directly here is
        # genuinely undefined per SQLite's own documentation — its
        # special "take the bare column from the row that produced the
        # aggregate" guarantee ONLY applies when there is exactly one
        # aggregate function and it's specifically MIN() or MAX(). This
        # query has four different aggregates (COUNT, SUM, MIN, AVG), so
        # that guarantee doesn't apply at all; which row's source_used
        # gets reported when the same query text was answered by
        # different sources at different times (a real, reachable case —
        # routing logic itself has changed multiple times over this
        # project's life) was not even reliably consistent, let alone
        # correct. Fixed with a correlated subquery reporting the MOST
        # RECENT source for each query — chosen over "most frequent"
        # because it stays accurate as routing logic evolves, rather
        # than continuing to report a stale answer from before a real
        # routing fix for as long as old log rows happen to outnumber
        # new ones. Verified this has no meaningful performance cost at
        # realistic homelab log volumes (3ms for 5000 rows / 300
        # distinct queries in direct testing).
        top_queries = []
        for row in con.execute("""
            SELECT
                LOWER(TRIM(q1.query)) as q,
                COUNT(*) as times_asked,
                SUM(q1.cached) as cache_hits,
                MIN(q1.latency_ms) as min_latency_ms,
                AVG(q1.latency_ms) as avg_latency_ms,
                (SELECT q2.source_used FROM query_log q2
                 WHERE LOWER(TRIM(q2.query)) = LOWER(TRIM(q1.query))
                 ORDER BY q2.id DESC LIMIT 1) as most_recent_source
            FROM query_log q1
            GROUP BY LOWER(TRIM(q1.query))
            ORDER BY times_asked DESC
            LIMIT 10
        """).fetchall():
            top_queries.append({
                "query": row[0],
                "times_asked": row[1],
                "cache_hits": row[2],
                "cache_hit_rate": round(row[2] / row[1] * 100, 1) if row[1] > 0 else 0,
                "min_latency_ms": row[3],
                "avg_latency_ms": round(row[4], 1),
                "source": row[5],
            })

        # Queries seen more than once — these are the ones the system has "learned"
        learned = con.execute("""
            SELECT COUNT(*) FROM (
                SELECT LOWER(TRIM(query))
                FROM query_log
                GROUP BY LOWER(TRIM(query))
                HAVING COUNT(*) > 1
            )
        """).fetchone()[0] or 0

        # Unique queries total
        unique = con.execute("""
            SELECT COUNT(DISTINCT LOWER(TRIM(query))) FROM query_log
        """).fetchone()[0] or 0

        # Synthesis counters (Design Doc 4) — derived from the nullable
        # `synthesized` status column: NULL = not requested (excluded
        # here), 1 = served a real answer, 2 = honest NOT_IN_SOURCES miss,
        # 0 = requested but skipped/gate-rejected. `requested` is every
        # non-NULL row. Guarded so a database predating the column (its
        # ALTER not yet run) reports zeros rather than raising.
        try:
            srow = con.execute("""
                SELECT
                    SUM(CASE WHEN synthesized IS NOT NULL THEN 1 ELSE 0 END),
                    SUM(CASE WHEN synthesized = 1 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN synthesized = 2 THEN 1 ELSE 0 END),
                    SUM(CASE WHEN synthesized = 0 THEN 1 ELSE 0 END)
                FROM query_log
            """).fetchone()
            synthesis_stats = {
                "requested": srow[0] or 0,
                "served": srow[1] or 0,
                "not_in_sources": srow[2] or 0,
                "rejected_or_skipped": srow[3] or 0,
            }
        except Exception:
            synthesis_stats = {
                "requested": 0, "served": 0, "not_in_sources": 0, "rejected_or_skipped": 0,
            }

        return {
            "total_queries": total,
            "unique_queries": unique,
            "learned_queries": learned,
            "cache_hit_rate_pct": round(cache_hits / total * 100, 1) if total > 0 else 0,
            "success_rate_pct": round(successes / total * 100, 1) if total > 0 else 0,
            "fallback_count": fallbacks,
            "fallback_rate_pct": round(fallbacks / total * 100, 1) if total > 0 else 0,
            "avg_latency_ms": avg_latency,
            "ttfk_ms": ttfk_ms,
            "latency_by_source": latency_by_source,
            "fallback_by_target": fallback_by_target,
            "top_queries": top_queries,
            "synthesis": synthesis_stats,
        }

    except Exception as e:
        return {"error": str(e)}
    finally:
        # Guaranteed close even when one of the queries above raises —
        # the previous inline con.close() on the success path leaked the
        # connection on any mid-function sqlite error (the except above
        # caught the error and returned, but the connection stayed open
        # until GC). Same fix applied across every _connect() call site
        # in this pass; the short ones use contextlib.closing, this one
        # uses try/finally because its body is too long to re-indent
        # under a with block without obscuring the diff.
        if con is not None:
            con.close()
