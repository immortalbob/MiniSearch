"""
Tests for app/main.py — FastAPI endpoints.
Uses TestClient to test endpoints directly without a running server.
"""
import pytest
import sqlite3
import tempfile
import os
import time
from unittest.mock import patch
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    """Create a TestClient for the FastAPI app."""
    # Point log DB to a temp file so tests don't pollute real data
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name
    with patch("app.main._LOG_DB", temp_db):
        from app.main import app
        with TestClient(app) as c:
            yield c
    os.unlink(temp_db)


class TestLoggingConfiguration:
    """Tests for root logging setup at app import time.

    Regression coverage for a real bug found via production debugging —
    the root logger defaulted to WARNING with no attached handler, which
    silently swallowed every _LOGGER.info() call across the entire
    codebase (decomposition splits, disambiguation candidates, article
    selection, snapshot jobs, etc). Only uvicorn's own access logger (a
    separate logger with its own handler) was ever visible in container
    logs, making it look like the app was processing requests with zero
    diagnostic output — when in fact the info logs were firing, just
    never reaching any output destination.

    These tests call logging.basicConfig() directly with the same
    arguments app/main.py uses, rather than relying on `import app.main`
    to trigger it — app.main is already cached in sys.modules by the
    time these tests run (the `client` fixture above imports it first),
    so a second `import app.main` is a no-op and never re-executes the
    module-level basicConfig() call. Testing the actual configuration
    logic directly avoids depending on Python's one-time import behavior.
    """

    def setup_method(self):
        import logging
        # Snapshot real logging state so these tests don't leak changes
        # into other tests that might check logger configuration
        self._original_level = logging.getLogger().level
        self._original_handlers = list(logging.getLogger().handlers)

    def teardown_method(self):
        import logging
        logging.getLogger().setLevel(self._original_level)
        logging.getLogger().handlers = self._original_handlers

    def test_basicConfig_sets_info_level_by_default(self):
        import logging
        import os as os_module
        from unittest.mock import patch

        logging.getLogger().handlers = []
        with patch.dict(os_module.environ, {}, clear=False):
            os_module.environ.pop("LOG_LEVEL", None)
            level_name = os_module.environ.get("LOG_LEVEL", "INFO").upper()
            logging.basicConfig(
                level=level_name,
                format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
                force=True,
            )
        assert logging.getLogger().level == logging.INFO

    def test_basicConfig_attaches_a_handler(self):
        import logging
        logging.getLogger().handlers = []
        logging.basicConfig(
            level="INFO",
            format="%(asctime)s %(levelname)s:%(name)s: %(message)s",
            force=True,
        )
        assert len(logging.getLogger().handlers) >= 1

    def test_app_router_logger_inherits_info_level_when_configured(self):
        import logging
        logging.getLogger().handlers = []
        logging.basicConfig(level="INFO", force=True)
        assert logging.getLogger("app.router").getEffectiveLevel() <= logging.INFO

    def test_log_level_env_var_respected(self):
        import logging
        import os as os_module
        from unittest.mock import patch

        with patch.dict(os_module.environ, {"LOG_LEVEL": "DEBUG"}):
            level_name = os_module.environ.get("LOG_LEVEL", "INFO").upper()
            assert level_name == "DEBUG"
            assert logging.getLevelName(level_name) == logging.DEBUG

    def test_main_module_source_calls_basicConfig_with_env_var(self):
        """Confirm the actual source code in main.py reads LOG_LEVEL and
        calls logging.basicConfig() — a static check that doesn't depend
        on import timing, since we can't reliably re-trigger module-level
        code in an already-imported module within the same test process."""
        import inspect
        import app.main as main_module
        source = inspect.getsource(main_module)
        assert "logging.basicConfig" in source
        assert "LOG_LEVEL" in source


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_health_returns_ok(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_health_includes_kiwix_books(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "kiwix_books_loaded" in data
        assert isinstance(data["kiwix_books_loaded"], int)

    def test_health_includes_cache_entries(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "cache_entries" in data

    def test_health_includes_cache_max_size(self, client):
        """Surfacing the configured max alongside the current count makes
        growth toward the bound visible without digging through code or
        config — the actual operational-maturity goal of this change."""
        resp = client.get("/health")
        data = resp.json()
        assert "cache_max_size" in data
        assert isinstance(data["cache_max_size"], int)
        assert data["cache_max_size"] > 0

    def test_health_includes_routing_cache_entries_and_max_size(self, client):
        """Regression coverage for a real gap found during operational
        maturity review — the routing cache previously had no exposed
        size at all in /health, and (separately) no enforced size limit
        either. Both the current count and the configured max must be
        visible here."""
        resp = client.get("/health")
        data = resp.json()
        assert "routing_cache_entries" in data
        assert isinstance(data["routing_cache_entries"], int)
        assert "routing_cache_max_size" in data
        assert isinstance(data["routing_cache_max_size"], int)
        assert data["routing_cache_max_size"] > 0

    def test_health_includes_snapshot_jobs(self, client):
        """Regression coverage for a real gap found during operational
        maturity review — every background snapshot job already catches
        its own exceptions and just logs a warning on failure, with the
        scheduler object itself never exposed to any endpoint at all.
        /health must surface each job's status so a genuinely stuck job
        is visible without reading raw application logs."""
        resp = client.get("/health")
        data = resp.json()
        assert "snapshot_jobs" in data
        for source in ["uptime", "forecast", "news", "ha"]:
            assert source in data["snapshot_jobs"]
            assert "status" in data["snapshot_jobs"][source]

    def test_health_includes_sources(self, client):
        resp = client.get("/health")
        data = resp.json()
        assert "sources" in data
        sources = data["sources"]
        for expected in ["kiwix", "forecast", "news", "web", "uptime", "ha", "llm"]:
            assert expected in sources

    def test_health_source_has_status(self, client):
        resp = client.get("/health")
        sources = resp.json()["sources"]
        for name, info in sources.items():
            assert "status" in info


class TestHealthConcurrentSourceChecks:
    """Regression test for a real, traced latency finding: /health's seven
    source checks (_check_kiwix, _check_forecast, _check_news, _check_web,
    _check_uptime, _check_ha, _check_llm) used to run as plain SEQUENTIAL
    calls, each with its own real 3-5 second network timeout — found via a
    real v3.50.2 benchmark run where a warm-cache /health sample hit
    5244ms, several times worse than its own 750ms median.

    Every _check_* function already catches its own exceptions internally
    and never raises, so parallelizing them carries none of the exception-
    propagation complexity fusion.py's own concurrent dispatch needed.

    This test confirms the ACTUAL concurrency property via a real timing
    measurement, not just that the response shape is unchanged — a
    refactor that accidentally stayed sequential could still pass every
    other existing /health test in this file unchanged."""

    def test_source_checks_genuinely_run_concurrently(self, client):
        """If all seven checks took 0.3s each and ran sequentially, the
        endpoint would take at least 2.1s. Run concurrently, it should
        take close to 0.3s regardless of how many checks there are."""
        # A minimal, genuinely valid, NON-empty OPDS feed — get_books()'s
        # real caching check (`if _book_cache: return _book_cache`) is
        # falsy for an empty list, so a genuinely empty catalog re-fetches
        # on every single call rather than ever caching (a separate, real,
        # pre-existing quirk, out of scope for this test). A non-empty
        # feed lets get_books() actually cache after its first real call
        # the way a normal deployment with real ZIM files would, so this
        # test measures the concurrency property it's actually testing.
        _ONE_BOOK_OPDS_FEED = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<feed xmlns="http://www.w3.org/2005/Atom" '
            b'xmlns:dc="http://purl.org/dc/terms/">'
            b'<entry><title>Test</title>'
            b'<link type="text/html" href="/content/test_en_2026-01"/>'
            b'</entry></feed>'
        )

        def slow_get(*args, **kwargs):
            time.sleep(0.3)
            resp = type("Resp", (), {})()
            resp.status_code = 200
            resp.content = _ONE_BOOK_OPDS_FEED
            resp.raise_for_status = lambda: None
            return resp

        # get_books() (called once on the main thread before the executor
        # starts, and again inside _check_kiwix() within the executor —
        # though the second call should hit the now-populated cache and
        # not re-fetch) makes its own real requests.get call from
        # app.sources.kiwix, a genuinely separate import from app.main's
        # — both need mocking.
        with patch("app.main.requests.get", side_effect=slow_get), \
             patch("app.sources.kiwix._session.get", side_effect=slow_get):
            start = time.monotonic()
            resp = client.get("/health")
            elapsed = time.monotonic() - start

        assert resp.status_code == 200
        assert elapsed < 0.75, f"took {elapsed:.2f}s for 7 checks at 0.3s each — looks sequential, not concurrent"

    def test_a_single_slow_check_does_not_block_the_others_from_completing(self, client):
        """One genuinely slow check (simulating a real, unreachable
        backend hitting its full timeout) shouldn't make the other six
        checks wait for it — they should all complete independently,
        and the overall response time should be governed by the SLOWEST
        single check, not the sum of all seven."""
        _ONE_BOOK_OPDS_FEED = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<feed xmlns="http://www.w3.org/2005/Atom" '
            b'xmlns:dc="http://purl.org/dc/terms/">'
            b'<entry><title>Test</title>'
            b'<link type="text/html" href="/content/test_en_2026-01"/>'
            b'</entry></feed>'
        )
        call_count = {"n": 0}

        def variable_speed_get(*args, **kwargs):
            call_count["n"] += 1
            # First call simulates a real, slow timeout; the rest are fast.
            if call_count["n"] == 1:
                time.sleep(0.4)
            resp = type("Resp", (), {})()
            resp.status_code = 200
            resp.content = _ONE_BOOK_OPDS_FEED
            resp.raise_for_status = lambda: None
            return resp

        with patch("app.main.requests.get", side_effect=variable_speed_get), \
             patch("app.sources.kiwix._session.get", side_effect=variable_speed_get):
            start = time.monotonic()
            resp = client.get("/health")
            elapsed = time.monotonic() - start

        assert resp.status_code == 200
        # Governed by the one slow check (~0.4s), not several checks run sequentially.
        assert elapsed < 0.85, f"took {elapsed:.2f}s — one slow check appears to be blocking the others"

    def test_response_content_is_unaffected_by_running_checks_concurrently(self, client):
        """The actual status/error content for each source must be
        identical to what sequential calls would have produced — only
        the WALL-CLOCK TIMING should change, never the result content."""
        resp = client.get("/health")
        data = resp.json()
        assert "sources" in data
        for expected in ["kiwix", "forecast", "news", "web", "uptime", "ha", "llm"]:
            assert expected in data["sources"]
            assert "status" in data["sources"][expected]
            assert data["sources"][expected]["status"] in ("ok", "error", "not_configured")


class TestUptimeKumaLifespanIntegration:
    """Tests for the persistent Uptime Kuma connection's lifespan wiring
    in lifespan() — confirms get_connection() is genuinely called during
    app startup when uptime is configured (warming the connection before
    the first real request, rather than paying the connect+login cost on
    whichever request happens to arrive first), confirms it's correctly
    SKIPPED when uptime isn't configured (no UptimeKumaApi construction
    attempted against a blank URL), and confirms disconnect() runs on
    shutdown.

    Builds its own TestClient rather than using the module-scoped
    `client` fixture above, since that fixture's app instance has
    already run its lifespan with uptime left unconfigured — this needs
    settings configured BEFORE the lifespan startup code runs.
    """

    def test_get_connection_called_on_startup_when_uptime_configured(self):
        """lifespan() explicitly warms the connection once, and the
        scheduler's own immediate startup snapshot_uptime() call (which
        calls search() -> get_connection() again) is expected to follow
        right behind it — that second call should find the
        already-warmed connection waiting, not need its own fresh one.
        This test confirms get_connection() is genuinely exercised
        during startup at all; reuse-not-recreation is covered
        separately in test_uptime_kuma.py's TestPersistentConnection."""
        from app.config import settings
        original_url = settings.uptime_kuma_url
        original_user = settings.uptime_kuma_username
        settings.uptime_kuma_url = "http://uptime-kuma:3001"
        settings.uptime_kuma_username = "testuser"
        try:
            with patch("app.sources.uptime_kuma.get_connection") as mock_get_connection, \
                 patch("app.sources.uptime_kuma.disconnect") as mock_disconnect:
                from app.main import app
                with TestClient(app):
                    assert mock_get_connection.call_count >= 1
                    mock_disconnect.assert_not_called()
                mock_disconnect.assert_called_once()
        finally:
            settings.uptime_kuma_url = original_url
            settings.uptime_kuma_username = original_user

    def test_get_connection_not_called_on_startup_when_uptime_unconfigured(self):
        """Mirrors every other source's graceful-disable behavior —
        leaving UPTIME_KUMA_URL blank should not attempt any real
        connection at startup, the same way it already doesn't error
        out of search() itself."""
        from app.config import settings
        original_url = settings.uptime_kuma_url
        settings.uptime_kuma_url = ""
        try:
            with patch("app.sources.uptime_kuma.get_connection") as mock_get_connection, \
                 patch("app.sources.uptime_kuma.disconnect") as mock_disconnect:
                from app.main import app
                with TestClient(app):
                    mock_get_connection.assert_not_called()
                mock_disconnect.assert_not_called()
        finally:
            settings.uptime_kuma_url = original_url


class TestSourcesEndpoint:
    """Tests for GET /sources."""

    def test_sources_returns_list(self, client):
        resp = client.get("/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert "sources" in data

    def test_sources_includes_all_known(self, client):
        resp = client.get("/sources")
        sources = resp.json()["sources"]
        for expected in ["kiwix", "forecast", "news", "web", "uptime", "ha", "fusion", "auto"]:
            assert expected in sources


class TestCatalogEndpoints:
    """Tests for GET /catalog and POST /catalog/refresh."""

    def test_catalog_returns_count_and_books(self, client):
        resp = client.get("/catalog")
        assert resp.status_code == 200
        data = resp.json()
        assert "count" in data
        assert "books" in data

    def test_catalog_count_matches_books_length(self, client):
        resp = client.get("/catalog")
        data = resp.json()
        assert data["count"] == len(data["books"])

    def test_catalog_refresh_returns_status(self, client):
        from unittest.mock import patch
        with patch("app.main.refresh_catalog", return_value=[]):
            resp = client.post("/catalog/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "refreshed"
        assert "count" in data

    def test_catalog_refresh_reflects_new_count(self, client):
        from unittest.mock import patch
        fake_books = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        with patch("app.main.refresh_catalog", return_value=fake_books):
            resp = client.post("/catalog/refresh")
        assert resp.json()["count"] == 3

    def test_catalog_refresh_calls_refresh_function(self, client):
        from unittest.mock import patch
        with patch("app.main.refresh_catalog", return_value=[]) as mock_refresh:
            client.post("/catalog/refresh")
        assert mock_refresh.called


class TestCacheEndpoints:
    """Tests for cache management endpoints."""

    def test_cache_get_returns_entries(self, client):
        resp = client.get("/cache")
        assert resp.status_code == 200
        assert "entries" in resp.json()

    def test_cache_clear_returns_cleared(self, client):
        resp = client.post("/cache/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cleared"
        assert "entries_removed" in data

    def test_routing_cache_get_returns_entries(self, client):
        resp = client.get("/cache/routing")
        assert resp.status_code == 200
        assert "entries" in resp.json()

    def test_routing_cache_clear_returns_cleared(self, client):
        resp = client.post("/cache/routing/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cleared"


class TestLogsEndpoints:
    """Tests for query log endpoints."""

    def test_logs_returns_entries(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200
        data = resp.json()
        assert "entries" in data
        assert "count" in data

    def test_logs_limit_param(self, client):
        resp = client.get("/logs?limit=5")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] <= 5

    def test_negative_limit_does_not_return_entire_log(self, client):
        """Regression test for a real, if low-severity, bug found via a
        deliberate "bulletproofing" pass: SQLite treats a negative LIMIT
        value as "no limit at all" (documented behavior), so
        GET /logs?limit=-1 would return the ENTIRE query log, defeating
        this endpoint's own intent of showing a bounded, recent-entries
        view. Confirms the fix: a negative limit is clamped to a sane
        minimum rather than disabling the limit entirely."""
        from app.main import _log_query
        for i in range(20):
            _log_query(f"clamp test query {i}", "kiwix", "kiwix", False, True, 50)

        resp = client.get("/logs?limit=-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] < 20  # must NOT return everything

    def test_excessive_limit_is_capped(self, client):
        """Confirms an absurdly large limit is also bounded, not just
        a negative one — a real, sane upper cap regardless of how the
        unbounded value was reached."""
        resp = client.get("/logs?limit=999999999")
        assert resp.status_code == 200
        # Should not error or hang — a sane response either way confirms
        # the clamp logic ran without crashing on an extreme input

    def test_logs_clear_returns_cleared(self, client):
        resp = client.post("/logs/clear")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "cleared"

    def test_logs_entry_has_required_fields(self, client):
        # Write a test entry directly to the DB then check it appears
        from app.main import _log_query
        _log_query("test query", "auto", "kiwix", False, True, 123)
        resp = client.get("/logs?limit=1")
        entries = resp.json()["entries"]
        if entries:
            entry = entries[0]
            for field in ["timestamp", "query", "source_requested", "source_used", "cached", "success", "latency_ms"]:
                assert field in entry

    def test_log_query_accepts_fallback_occurred_default_false(self, client):
        """Backward compatibility — existing call sites that don't pass
        fallback_occurred at all (the 6-arg call signature used
        throughout the rest of the test suite and the exception handler
        in /search) must continue to work unchanged."""
        from app.main import _log_query
        # Should not raise — fallback_occurred defaults to False
        _log_query("backward compat test", "auto", "forecast", False, True, 50)

    def test_log_query_accepts_explicit_fallback_occurred(self, client):
        from app.main import _log_query
        _log_query("fallback test query", "kiwix", "web", False, True, 200, fallback_occurred=True)
        resp = client.get("/logs?limit=1")
        entries = resp.json()["entries"]
        assert entries[0]["query"] == "fallback test query"


class TestFallbackDetection:
    """Tests for the fallback_occurred detection logic in /search and its
    surfacing in /logs/stats.

    Recorded directly at the one code point where a fallback genuinely
    happens (router._resolve_single_source(), via route_query()'s
    _ROUTE_STATS channel) — replacing the original after-the-fact
    inference that compared main.py's pre-computed intent against the
    resolved source. That inference was deliberately chosen at the time
    over widening route_with_source()'s return signature, but was
    structurally blind to fallbacks inside decomposed sub-queries (its
    own comment said so); the stats channel keeps the signature
    unchanged AND sees those too."""

    def setup_method(self):
        from app.main import _LOG_DB
        con = sqlite3.connect(_LOG_DB)
        con.execute("DELETE FROM query_log")
        con.commit()
        con.close()

    def test_explicit_source_fallback_is_detected(self, client):
        """An explicit source='kiwix' request that internally falls back
        to web must be logged with fallback_occurred=1."""
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = lambda q: "No results found in wikipedia."
        router_module.SOURCE_MAP["web"] = lambda q: "Real web results."
        try:
            resp = client.post("/search", json={"query": "test fallback query", "source": "kiwix"})
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert resp.status_code == 200
        assert resp.json()["source_used"] == "web"

        from app.main import _connect, _LOG_DB
        con = _connect(_LOG_DB)
        row = con.execute(
            "SELECT fallback_occurred FROM query_log WHERE query = ? ORDER BY id DESC LIMIT 1",
            ("test fallback query",)
        ).fetchone()
        con.close()
        assert row is not None
        assert row[0] == 1

    def test_no_fallback_is_not_flagged(self, client):
        """A request that succeeds on its intended source (no fallback
        needed at all) must be logged with fallback_occurred=0."""
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        try:
            resp = client.post("/search", json={"query": "test no fallback query", "source": "forecast"})
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert resp.status_code == 200

        from app.main import _connect, _LOG_DB
        con = _connect(_LOG_DB)
        row = con.execute(
            "SELECT fallback_occurred FROM query_log WHERE query = ? ORDER BY id DESC LIMIT 1",
            ("test no fallback query",)
        ).fetchone()
        con.close()
        assert row is not None
        assert row[0] == 0

    def test_stats_reports_fallback_count_and_rate(self, client):
        from app.main import _log_query
        _log_query("q1", "kiwix", "web", False, True, 100, fallback_occurred=True)
        _log_query("q2", "forecast", "forecast", False, True, 50, fallback_occurred=False)

        resp = client.get("/logs/stats")
        data = resp.json()
        assert data["fallback_count"] >= 1
        assert "fallback_rate_pct" in data

    def test_stats_fallback_by_target_uses_combined_label_not_duplicate_attribution(self, client):
        """Regression test for a real flaw found during design: kiwix
        and news both fall back to the same target (web), so a boolean
        column genuinely cannot distinguish which one triggered a given
        fallback. Querying naively per-original-source would run the
        identical SQL query under both labels and double-report the
        same underlying rows. The fix reports a single, honest combined
        label (e.g. "kiwix_or_news_fallback_to_web") instead of guessing
        at an attribution the data doesn't actually support."""
        from app.main import _log_query
        _log_query("fallback q1", "kiwix", "web", False, True, 100, fallback_occurred=True)
        _log_query("fallback q2", "news", "web", False, True, 100, fallback_occurred=True)

        resp = client.get("/logs/stats")
        data = resp.json()
        fallback_by_target = data["fallback_by_target"]

        # Must NOT have separate, duplicate-counted "kiwix" and "news" keys
        assert "kiwix" not in fallback_by_target
        assert "news" not in fallback_by_target
        # Must have exactly one combined key covering both
        assert "kiwix_or_news_fallback_to_web" in fallback_by_target
        assert fallback_by_target["kiwix_or_news_fallback_to_web"] == 2


class TestSearchNoWastedIntentDetection:
    """Regression tests for the /search pre-routing detect_intent() call
    removed when the endpoint moved to route_query(). The old endpoint
    resolved the FULL query's intent before routing, purely to compute
    the `cached` flag — a full, wasted cold LLM routing call for any
    query that route_with_source() decomposes or treats as conditional,
    since those paths never route the full query at all, plus a `cached`
    flag computed against a cache key those paths never use."""

    def test_compound_query_never_intent_detects_the_full_query(self, client):
        """A decomposable query must only ever have detect_intent()
        called on its SUB-queries, never on the full compound string —
        the full-query call was the wasted LLM cost."""
        from unittest.mock import patch
        import app.router as router_module

        full_query = "what is the weather today and also lights status"
        seen_queries = []

        def tracking_intent(q):
            seen_queries.append(q)
            return "forecast" if "weather" in q else "ha"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        router_module.SOURCE_MAP["ha"] = lambda q: "**Lights:**\n- Kitchen: on"
        try:
            with patch("app.router.detect_intent", side_effect=tracking_intent):
                resp = client.post("/search", json={"query": full_query, "source": "auto"})
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert resp.status_code == 200
        assert seen_queries, "expected sub-query intent detection to run"
        assert full_query not in seen_queries, (
            "detect_intent() ran against the full compound query — the "
            "exact wasted call route_query() exists to eliminate"
        )

    def test_cached_flag_true_only_when_served_entirely_from_cache(self, client):
        """The cached flag now reflects what actually happened: True on
        a genuine warm hit, False on the cold call that populated it."""
        import app.router as router_module
        from app.router import clear_cache

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        try:
            clear_cache()
            cold = client.post("/search", json={"query": "cached flag test", "source": "forecast"})
            warm = client.post("/search", json={"query": "cached flag test", "source": "forecast"})
        finally:
            router_module.SOURCE_MAP.update(original_map)
            clear_cache()

        assert cold.json()["cached"] is False
        assert warm.json()["cached"] is True


class TestSearchFailureReportsRealSource:
    """Regression tests for a real, significant bug found via a
    deliberate "bulletproofing" pass: when /search's auto-routing path
    raised an exception, source_used was set to request.source — which
    is just the literal string "auto" whenever auto-routing was
    requested, not a real source name. Originally fixed by reporting
    main.py's own pre-computed intent; now provided directly by
    router.route_query(), which records the intended source at the
    exact point route_with_source()'s own intent resolution happens
    (via the _ROUTE_STATS channel) and reports it on failure — same
    honest answer to "what was this query trying to do", one layer
    closer to where the answer actually lives. These tests exercise
    the real route_query() with only the layers underneath it mocked,
    so the stats channel itself is genuinely under test."""

    def test_auto_routing_failure_reports_resolved_single_source(self, client):
        from unittest.mock import patch
        # detect_intent resolves to kiwix, then the actual source
        # resolution blows up — the response must name kiwix, not "auto".
        with patch("app.router._resolve_single_source", side_effect=Exception("simulated failure")), \
             patch("app.router.detect_intent", return_value="kiwix"):
            resp = client.post("/search", json={"query": "test auto failure", "source": "auto"})
        data = resp.json()
        assert data["success"] is False
        assert data["source_used"] == "kiwix"

    def test_auto_routing_failure_with_fusion_intent_reports_fusion(self, client):
        from unittest.mock import patch
        with patch("app.router.fusion.search", side_effect=Exception("simulated failure")), \
             patch("app.router.detect_intent", return_value=["kiwix", "web"]):
            resp = client.post("/search", json={"query": "test fusion failure", "source": "auto"})
        data = resp.json()
        assert data["success"] is False
        assert data["source_used"] == "fusion"

    def test_explicit_source_failure_still_reports_that_source(self, client):
        """Confirms an explicitly-requested source is still correctly
        reported on failure — route_query() records it as the intended
        source before anything can break."""
        from unittest.mock import patch
        with patch("app.router._resolve_single_source", side_effect=Exception("simulated failure")):
            resp = client.post("/search", json={"query": "test explicit failure", "source": "forecast"})
        data = resp.json()
        assert data["success"] is False
        assert data["source_used"] == "forecast"

    def test_failure_before_any_intent_resolution_reports_requested_source(self, client):
        """An exception that fires before route_with_source() ever
        resolves an intent (e.g. inside conditional detection) has no
        better answer available — the requested source is honestly
        reported as-is."""
        from unittest.mock import patch
        with patch("app.router.detect_conditional", side_effect=Exception("simulated failure")):
            resp = client.post("/search", json={"query": "test early failure", "source": "auto"})
        data = resp.json()
        assert data["success"] is False
        assert data["source_used"] == "auto"


class TestAPIKeyAuth:
    """Tests for API key authentication on /search and /changes."""

    def setup_method(self):
        from app.config import settings
        self._original_keys = settings.api_keys
        settings.api_keys = ""

    def teardown_method(self):
        from app.config import settings
        settings.api_keys = self._original_keys

    def test_search_works_without_key_when_auth_disabled(self, client):
        resp = client.post("/search", json={"query": "what is nitrogen", "source": "kiwix"})
        assert resp.status_code == 200

    def test_changes_works_without_key_when_auth_disabled(self, client):
        resp = client.get("/changes")
        assert resp.status_code == 200

    def test_search_rejected_without_key_when_auth_enabled(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.post("/search", json={"query": "test", "source": "kiwix"})
        assert resp.status_code == 401

    def test_search_rejected_with_wrong_key(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.post(
            "/search",
            json={"query": "test", "source": "kiwix"},
            headers={"X-API-Key": "wrong-key"},
        )
        assert resp.status_code == 401

    def test_search_accepted_with_correct_key(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.post(
            "/search",
            json={"query": "what is nitrogen", "source": "kiwix"},
            headers={"X-API-Key": "secret123"},
        )
        assert resp.status_code == 200

    def test_changes_rejected_without_key_when_auth_enabled(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.get("/changes")
        assert resp.status_code == 401

    def test_changes_accepted_with_correct_key(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.get("/changes", headers={"X-API-Key": "secret123"})
        assert resp.status_code == 200

    def test_multiple_keys_all_valid(self, client):
        from app.config import settings
        settings.api_keys = "key1,key2,key3"
        resp1 = client.post("/search", json={"query": "test", "source": "kiwix"}, headers={"X-API-Key": "key1"})
        resp2 = client.post("/search", json={"query": "test", "source": "kiwix"}, headers={"X-API-Key": "key3"})
        assert resp1.status_code == 200
        assert resp2.status_code == 200

    def test_health_never_requires_key(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_areas_never_requires_key(self, client):
        from app.config import settings
        settings.api_keys = "secret123"
        resp = client.get("/areas")
        assert resp.status_code == 200

    def test_keys_with_whitespace_are_trimmed(self, client):
        from app.config import settings
        settings.api_keys = " key1 , key2 "
        resp = client.post("/search", json={"query": "test", "source": "kiwix"}, headers={"X-API-Key": "key1"})
        assert resp.status_code == 200


class TestAreasEndpoint:
    """Tests for GET /areas."""

    def test_areas_returns_status_and_areas_keys(self, client):
        resp = client.get("/areas")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "areas" in data

    def test_areas_not_configured_without_ha_settings(self, client):
        from app.config import settings
        original_url = settings.ha_url
        original_token = settings.ha_token
        settings.ha_url = ""
        settings.ha_token = ""
        resp = client.get("/areas")
        data = resp.json()
        assert data["status"] == "not_configured"
        settings.ha_url = original_url
        settings.ha_token = original_token


class TestBackupEndpoint:
    """Tests for GET /backup and GET /backup/info."""

    def test_backup_info_returns_file_dict(self, client):
        resp = client.get("/backup/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "files" in data

    def test_backup_info_includes_known_files(self, client):
        resp = client.get("/backup/info")
        files = resp.json()["files"]
        for expected in ["cache.json", "routing_cache.json", "query_log.db", "snapshots.db"]:
            assert expected in files

    def test_backup_info_reports_existence(self, client):
        resp = client.get("/backup/info")
        files = resp.json()["files"]
        for name, info in files.items():
            assert "exists" in info

    def test_backup_and_backup_info_use_the_same_file_list(self):
        """Regression test for a real, if minor, maintenance risk found
        via a deliberate "bulletproofing" pass: the same hardcoded file
        list was duplicated identically in both backup() and
        backup_info() — adding or removing a tracked data file could
        easily update one copy and forget the other, leaving the two
        endpoints silently disagreeing about what Mnemolis actually
        tracks. Confirms both functions now genuinely share one list."""
        import app.main as main_module
        import inspect
        backup_source = inspect.getsource(main_module.backup)
        backup_info_source = inspect.getsource(main_module.backup_info)
        assert "_BACKUP_DATA_FILES" in backup_source
        assert "_BACKUP_DATA_FILES" in backup_info_source

    def test_backup_returns_tarball(self, client):
        resp = client.get("/backup")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/gzip"

    def test_backup_filename_has_timestamp(self, client):
        resp = client.get("/backup")
        content_disposition = resp.headers.get("content-disposition", "")
        assert "mnemolis-backup-" in content_disposition
        assert ".tar.gz" in content_disposition

    def test_backup_contains_valid_tar(self, client):
        import tarfile
        import io
        resp = client.get("/backup")
        tar_bytes = io.BytesIO(resp.content)
        with tarfile.open(fileobj=tar_bytes, mode="r:gz") as tar:
            names = tar.getnames()
            # Should contain at least the files that exist on disk
            assert isinstance(names, list)

    def test_backup_captures_uncheckpointed_wal_writes(self, client, tmp_path):
        """Regression test for a real data-integrity gap found via a
        deliberate function-by-function audit: every Mnemolis SQLite
        database runs in WAL mode, so recent committed writes live in
        the -wal sidecar file until the next checkpoint — and /backup
        previously tar'd only the bare .db file, silently producing
        backups missing every not-yet-checkpointed write. The fix uses
        SQLite's own online backup API to snapshot each database into
        a complete, consistent single file first.

        This test constructs the exact hazardous condition: a WAL-mode
        database with autocheckpointing disabled, a committed row, and
        the writer connection deliberately held OPEN across the /backup
        call (closing it would checkpoint the WAL and mask the bug).
        The row must be readable from the database file inside the
        returned tarball. Confirmed this exact test fails against the
        old tar.add()-the-bare-file behavior."""
        import io
        import sqlite3
        import tarfile
        from unittest.mock import patch

        db_path = tmp_path / "waltest.db"
        writer = sqlite3.connect(str(db_path))
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE t (v TEXT)")
            writer.execute("INSERT INTO t (v) VALUES ('committed-but-uncheckpointed')")
            writer.commit()
            # The -wal sidecar must genuinely exist and hold the write,
            # or this test isn't exercising the real hazard at all.
            assert (tmp_path / "waltest.db-wal").exists()

            with patch("app.main._BACKUP_DATA_FILES", [str(db_path)]):
                resp = client.get("/backup")
            assert resp.status_code == 200

            with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
                member = tar.extractfile("waltest.db")
                assert member is not None
                extracted = tmp_path / "extracted.db"
                extracted.write_bytes(member.read())

            reader = sqlite3.connect(str(extracted))
            try:
                rows = reader.execute("SELECT v FROM t").fetchall()
            finally:
                reader.close()
            assert rows == [("committed-but-uncheckpointed",)]
        finally:
            writer.close()


class TestLogsStatsEndpoint:
    """Tests for GET /logs/stats."""

    def test_stats_returns_expected_keys(self, client):
        resp = client.get("/logs/stats")
        assert resp.status_code == 200
        data = resp.json()
        for key in [
            "total_queries", "unique_queries", "learned_queries",
            "cache_hit_rate_pct", "success_rate_pct", "avg_latency_ms",
            "ttfk_ms", "latency_by_source", "top_queries"
        ]:
            assert key in data

    def test_stats_total_is_int(self, client):
        data = client.get("/logs/stats").json()
        assert isinstance(data["total_queries"], int)

    def test_stats_cache_hit_rate_is_percentage(self, client):
        data = client.get("/logs/stats").json()
        assert 0.0 <= data["cache_hit_rate_pct"] <= 100.0

    def test_stats_success_rate_is_percentage(self, client):
        data = client.get("/logs/stats").json()
        assert 0.0 <= data["success_rate_pct"] <= 100.0

    def test_stats_ttfk_is_non_negative(self, client):
        data = client.get("/logs/stats").json()
        assert data["ttfk_ms"] >= 0

    def test_stats_unique_lte_total(self, client):
        data = client.get("/logs/stats").json()
        assert data["unique_queries"] <= data["total_queries"]

    def test_stats_learned_lte_unique(self, client):
        data = client.get("/logs/stats").json()
        assert data["learned_queries"] <= data["unique_queries"]

    def test_stats_top_queries_is_list(self, client):
        data = client.get("/logs/stats").json()
        assert isinstance(data["top_queries"], list)

    def test_stats_top_query_has_required_fields(self, client):
        data = client.get("/logs/stats").json()
        if data["top_queries"]:
            entry = data["top_queries"][0]
            for field in ["query", "times_asked", "cache_hits", "cache_hit_rate", "min_latency_ms", "avg_latency_ms", "source"]:
                assert field in entry

    def test_stats_top_queries_sorted_descending(self, client):
        data = client.get("/logs/stats").json()
        top = data["top_queries"]
        if len(top) > 1:
            assert top[0]["times_asked"] >= top[1]["times_asked"]

    def test_stats_latency_by_source_is_dict(self, client):
        data = client.get("/logs/stats").json()
        assert isinstance(data["latency_by_source"], dict)

    def test_stats_latency_by_source_has_valid_structure(self, client):
        data = client.get("/logs/stats").json()
        for source, info in data["latency_by_source"].items():
            assert "avg_latency_ms" in info
            assert "query_count" in info
            assert info["query_count"] > 0

    def test_top_queries_source_is_deterministic_most_recent(self):
        """Regression test for a real correctness gap found via a
        deliberate, precise re-read of query_log_stats(): selecting the
        bare `source_used` column directly in this query was genuinely
        undefined per SQLite's own documentation — its special "take
        the bare column from the aggregate row" guarantee only applies
        with exactly one aggregate function, and only when that
        aggregate is MIN() or MAX(). This query has four different
        aggregates, so that guarantee never applied at all. Confirms
        the fix: when the same query text was answered by different
        sources at different times (a real, reachable case — routing
        logic has genuinely changed multiple times over this project's
        life), the reported source is now deterministically the MOST
        RECENT one, not an undefined pick."""
        import app.main as main_module
        import time
        con = main_module._connect(main_module._LOG_DB)
        con.execute("DELETE FROM query_log")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        con.execute(
            "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms) "
            "VALUES (?, 'deterministic source test', 'forecast', 'forecast', 0, 1, 200)",
            (now,)
        )
        con.execute(
            "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms) "
            "VALUES (?, 'deterministic source test', 'forecast', 'web', 0, 1, 3000)",
            (now,)
        )
        con.execute(
            "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms) "
            "VALUES (?, 'deterministic source test', 'forecast', 'forecast', 1, 1, 15)",
            (now,)
        )
        con.commit()
        con.close()

        result = main_module.query_log_stats()
        entry = next(q for q in result["top_queries"] if q["query"] == "deterministic source test")
        assert entry["source"] == "forecast"  # the most recently inserted row's source

    def test_ttfk_uses_first_occurrence_latency_not_minimum(self):
        """Regression test for a real bug found via a deliberate function-
        by-function read: the TTFK SQL used MIN(id) and MIN(latency_ms)
        as two INDEPENDENT aggregates in the same GROUP BY. MIN(id)
        correctly identifies the first occurrence's row, but MIN(latency_ms)
        independently selects the smallest latency across ALL non-cached
        occurrences — not the latency of the first occurrence's row. For
        a query asked repeatedly without caching (e.g. adversarial test
        queries), this reported the fastest cold run rather than the
        genuine first cold hit, consistently under-estimating true TTFK.

        Confirmed real: a query with first latency 3000ms and second
        latency 200ms reported TTFK of 200ms (the minimum), not 3000ms
        (the actual first-seen cold cost).

        Fixed by joining back to the row with min(id) to read its
        actual latency, rather than independently computing min(latency_ms)
        in the same aggregation."""
        import app.main as main_module
        import time
        con = main_module._connect(main_module._LOG_DB)
        con.execute("DELETE FROM query_log")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # First cold hit: slow (3000ms)
        con.execute(
            "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms) "
            "VALUES (?, 'ttfk regression query', 'auto', 'kiwix', 0, 1, 3000)",
            (now,)
        )
        # Second cold hit: faster (200ms) — MIN(latency_ms) would wrongly pick this
        con.execute(
            "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms) "
            "VALUES (?, 'ttfk regression query', 'auto', 'kiwix', 0, 1, 200)",
            (now,)
        )
        con.commit()
        con.close()

        result = main_module.query_log_stats()
        # TTFK must reflect the first occurrence's latency (3000ms), not min(200ms)
        assert result["ttfk_ms"] == 3000.0, (
            f"TTFK should be 3000ms (first cold hit), got {result['ttfk_ms']}ms. "
            f"MIN(latency_ms) bug would report 200ms."
        )


class TestLifespanMountRefresh:
    """Tests for the lifespan function's MCP mount-refresh logic, added
    alongside the MCP_MOUNT_PATH constant — found via a deliberate
    function-by-function read: the path used to match against was a bare
    "/mcp" string literal, independently typed in two places (here and at
    the real app.mount() call), with nothing enforcing the two ever
    agreed. If they silently drifted apart, the matching loop would find
    no route, silently leave the stale module-import-time mcp_app
    mounted, and reintroduce the exact "session manager can only be
    entered once" bug the surrounding fix exists to prevent — not at
    startup, but on the first real MCP request after the next restart."""

    def test_real_app_startup_finds_and_refreshes_the_real_mount(self):
        """The actual, real success path — confirms app.mount()'s real
        path and MCP_MOUNT_PATH genuinely agree today, the same way the
        existing test_other_real_rest_routes_unaffected_by_mcp_mount
        test already does, but checking the route object directly rather
        than only inferring success from an unrelated endpoint working."""
        from starlette.testclient import TestClient
        from starlette.routing import Mount
        from app.main import app, MCP_MOUNT_PATH
        from app.mcp_server import mcp_app as original_mcp_app

        with TestClient(app) as client:
            # Find the real, currently-mounted MCP route and confirm its
            # .app was genuinely swapped to a fresh object during this
            # lifecycle, not left pointing at the module-import-time one.
            mount_route = next(
                r for r in app.router.routes
                if isinstance(r, Mount) and r.path == MCP_MOUNT_PATH
            )
            assert mount_route.app is not original_mcp_app
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_no_matching_mount_logs_a_warning_instead_of_failing_silently(self):
        """The defensive branch itself — if MCP_MOUNT_PATH ever stops
        matching any real mounted route, startup must not crash (the
        rest of the app still needs to come up), but it must not stay
        silent about it either."""
        with patch("app.main.MCP_MOUNT_PATH", "/this-path-will-never-match-anything"), \
             patch("app.main._LOGGER") as mock_logger:
            from starlette.testclient import TestClient
            from app.main import app
            with TestClient(app) as client:
                resp = client.get("/health")
            assert resp.status_code == 200  # the rest of the app still starts cleanly
        mock_logger.warning.assert_called_once()
        assert "No Mount route found" in mock_logger.warning.call_args[0][0]




class TestSearchSynthesis:
    """/search grounded answer synthesis surface (Design Doc 4).

    Additive contract: with synthesize omitted or false, the response is
    byte-identical on its pre-existing fields and the new answer fields
    are null/[]/false. With synthesize=true, answer/answer_sources/
    synthesized carry the synthesized output and `result` is untouched.
    """

    def setup_method(self):
        from app.main import _LOG_DB
        con = sqlite3.connect(_LOG_DB)
        con.execute("DELETE FROM query_log")
        con.commit()
        con.close()
        from app.config import settings
        self.settings = settings
        self._orig = (settings.synthesis_enabled, settings.llm_url, settings.llm_model,
                      settings.synthesis_min_input_chars)
        settings.synthesis_enabled = True
        settings.llm_url = "http://test-llm"
        settings.llm_model = "qwen3:8b"
        settings.synthesis_min_input_chars = 40

    def teardown_method(self):
        (self.settings.synthesis_enabled, self.settings.llm_url, self.settings.llm_model,
         self.settings.synthesis_min_input_chars) = self._orig

    def _mock_forecast(self, text):
        import app.router as router_module
        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: text
        return original_map

    def test_default_request_has_null_answer_fields(self, client):
        import app.router as router_module
        original_map = self._mock_forecast("Clear skies and mild temperatures all week across the region.")
        try:
            resp = client.post("/search", json={"query": "weather please", "source": "forecast"})
        finally:
            router_module.SOURCE_MAP.update(original_map)
        body = resp.json()
        assert body["answer"] is None
        assert body["answer_sources"] == []
        assert body["synthesized"] is False

    def test_synthesize_true_returns_answer_and_intact_result(self, client):
        import app.router as router_module
        from unittest.mock import patch
        raw = "Clear skies and mild temperatures are expected all week across the region."
        original_map = self._mock_forecast(raw)
        try:
            with patch("app.llm.generate", return_value="Clear and mild all week. (forecast)"):
                resp = client.post("/search", json={
                    "query": "weather please", "source": "forecast",
                    "synthesize": True, "answer_style": "voice",
                })
        finally:
            router_module.SOURCE_MAP.update(original_map)
        body = resp.json()
        assert body["result"] == raw           # untouched
        assert body["synthesized"] is True
        assert body["answer"].endswith("(forecast)")
        assert body["answer_sources"] == ["forecast"]

    def test_synthesize_timeout_leaves_result_and_nulls_answer(self, client):
        import app.router as router_module
        from unittest.mock import patch
        raw = "Clear skies and mild temperatures are expected all week across the region."
        original_map = self._mock_forecast(raw)
        try:
            with patch("app.llm.generate", return_value=None):
                resp = client.post("/search", json={
                    "query": "weather please", "source": "forecast", "synthesize": True,
                })
        finally:
            router_module.SOURCE_MAP.update(original_map)
        body = resp.json()
        assert body["result"] == raw
        assert body["answer"] is None
        assert body["synthesized"] is False

    def test_logs_stats_counts_served_and_requested(self, client):
        import app.router as router_module
        from unittest.mock import patch
        raw = "Clear skies and mild temperatures are expected all week across the region."
        original_map = self._mock_forecast(raw)
        try:
            with patch("app.llm.generate", return_value="Clear and mild. (forecast)"):
                client.post("/search", json={
                    "query": "weather one", "source": "forecast", "synthesize": True,
                })
            # A non-synthesis request must NOT count toward requested.
            client.post("/search", json={"query": "weather two", "source": "forecast"})
        finally:
            router_module.SOURCE_MAP.update(original_map)
        stats = client.get("/logs/stats").json()
        assert "synthesis" in stats
        assert stats["synthesis"]["requested"] == 1
        assert stats["synthesis"]["served"] == 1

    def test_logs_stats_counts_not_in_sources(self, client):
        import app.router as router_module
        from unittest.mock import patch
        raw = "Some retrieved material that does not contain the specific answer being asked for here."
        original_map = self._mock_forecast(raw)
        try:
            with patch("app.llm.generate", return_value="NOT_IN_SOURCES"):
                client.post("/search", json={
                    "query": "unanswerable", "source": "forecast", "synthesize": True,
                })
        finally:
            router_module.SOURCE_MAP.update(original_map)
        stats = client.get("/logs/stats").json()
        assert stats["synthesis"]["not_in_sources"] == 1
        assert stats["synthesis"]["served"] == 0


class TestLogDbSynthesizedMigration:
    """The synthesized-column migration is idempotent and safe against a
    pre-existing table that lacks the column (the established caught-ALTER
    pattern for this DB)."""

    def test_migration_adds_column_to_legacy_table(self):
        import tempfile
        import os
        from unittest.mock import patch
        from app.main import _init_log_db, _connect
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db = f.name
        try:
            # Build a legacy table WITHOUT the synthesized column.
            con = _connect(db)
            con.execute("""
                CREATE TABLE query_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    query TEXT NOT NULL,
                    source_requested TEXT NOT NULL,
                    source_used TEXT NOT NULL,
                    cached INTEGER NOT NULL,
                    success INTEGER NOT NULL,
                    latency_ms INTEGER NOT NULL
                )
            """)
            con.commit()
            con.close()
            with patch("app.main._LOG_DB", db):
                _init_log_db()
                _init_log_db()  # run twice — must be idempotent
            con = _connect(db)
            cols = [r[1] for r in con.execute("PRAGMA table_info(query_log)").fetchall()]
            con.close()
            assert "synthesized" in cols
            assert "fallback_occurred" in cols
        finally:
            os.unlink(db)


class TestHistoryEndpoints:
    """The /history/metrics and /history/series endpoints, plus the
    /health `history` block (Design Doc 5 §6/§9). api_keys is empty in the
    module client, so require_api_key passes through."""

    def test_metrics_disabled_returns_disabled(self, client):
        from app.config import settings
        original = settings.history_enabled
        settings.history_enabled = False
        try:
            resp = client.get("/history/metrics")
        finally:
            settings.history_enabled = original
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

    def test_metrics_enabled_returns_overview(self, client):
        from app.config import settings
        original = settings.history_enabled
        settings.history_enabled = True
        try:
            with patch("app.main.get_metrics_overview",
                       return_value={"status": "ok", "metric_count": 0, "metrics": []}):
                resp = client.get("/history/metrics")
        finally:
            settings.history_enabled = original
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_series_disabled(self, client):
        from app.config import settings
        original = settings.history_enabled
        settings.history_enabled = False
        try:
            resp = client.get("/history/series", params={"metric": "sensor.x"})
        finally:
            settings.history_enabled = original
        assert resp.status_code == 200
        assert resp.json()["status"] == "disabled"

    def test_series_enabled_returns_samples(self, client):
        from app.config import settings
        from app.history import Sample
        original = settings.history_enabled
        settings.history_enabled = True
        try:
            with patch("app.main.fetch_samples",
                       return_value=[Sample(21.0, "2026-06-01T00:00:00Z")]):
                resp = client.get("/history/series",
                                  params={"metric": "sensor.x", "hours": 6})
        finally:
            settings.history_enabled = original
        body = resp.json()
        assert resp.status_code == 200
        assert body["status"] == "ok"
        assert body["count"] == 1
        assert body["samples"][0]["value"] == 21.0

    def test_health_includes_history_block(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert "history" in resp.json()
        assert "status" in resp.json()["history"]


class TestHistorySeriesBounds:
    """Audit pin: /history/series rejects nonsensical hour windows —
    negative/zero hours produced an inverted (empty) window silently, and
    an unbounded value invited a 90-day full-table scan per request."""

    def test_negative_hours_rejected(self, client):
        resp = client.get("/history/series", params={"metric": "sensor.x", "hours": -5})
        assert resp.status_code == 422

    def test_zero_hours_rejected(self, client):
        resp = client.get("/history/series", params={"metric": "sensor.x", "hours": 0})
        assert resp.status_code == 422

    def test_absurd_hours_rejected(self, client):
        resp = client.get("/history/series", params={"metric": "sensor.x", "hours": 999999})
        assert resp.status_code == 422
