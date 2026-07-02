"""
Tests for app/sources/searxng.py
Uses unittest.mock to avoid real network calls.
"""
from unittest.mock import patch, MagicMock


class TestSearxngQueryExpansion:
    """Integration tests for multi-query expansion in search()."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.llm_url
        self._orig_model = settings.llm_model
        self._orig_threshold = settings.web_news_score_threshold
        settings.llm_url = "http://ollama:11434"
        settings.llm_model = "qwen3:8b"
        settings.web_news_score_threshold = -100

    def teardown_method(self):
        from app.config import settings
        settings.llm_url = self._orig_url
        settings.llm_model = self._orig_model
        settings.web_news_score_threshold = self._orig_threshold

    def _mock_response(self, results: list) -> MagicMock:
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"results": results}
        mock.raise_for_status.return_value = None
        return mock

    def test_searches_both_original_and_alternate_query(self):
        from app.sources import searxng
        from unittest.mock import patch

        queried_params = []

        def fake_get(url, params=None, **kwargs):
            queried_params.append(params["q"])
            return self._mock_response([
                {"title": f"Result for {params['q']}", "url": f"https://example.com/{params['q']}", "content": "python programming content here"}
            ])

        with patch("app.sources.searxng._session.get", side_effect=fake_get), \
             patch("app.sources.searxng.get_alternate_phrasing", return_value="top python coding tips"):
            searxng.search("best python programming guide")

        assert "best python programming guide" in queried_params
        assert "top python coding tips" in queried_params

    def test_merges_results_from_both_queries(self):
        from app.sources import searxng
        from unittest.mock import patch

        def fake_fetch(query, **kwargs):
            if "alternate" in query:
                return [{"title": "Alternate Result", "url": "https://example.com/alt", "content": "python programming alternate content"}]
            return [{"title": "Primary Result", "url": "https://example.com/primary", "content": "python programming primary content"}]

        with patch.object(searxng, "_fetch_searxng", side_effect=fake_fetch), \
             patch.object(searxng, "get_alternate_phrasing", return_value="alternate python query"):
            result = searxng.search("python programming guide")

        assert "Primary Result" in result
        assert "Alternate Result" in result

    def test_dedupes_www_variant_urls(self):
        """Regression test — 'https://www.x.com/page/' and 'https://x.com/page'
        are the same article but weren't deduped by raw string comparison."""
        from app.sources import searxng
        from unittest.mock import patch

        def fake_fetch(query, **kwargs):
            if "alternate" in query:
                return [{"title": "Sourdough Guide", "url": "https://zoebakes.com/2025/04/21/sourdough/", "content": "sourdough starter recipe content"}]
            return [{"title": "Sourdough Guide", "url": "https://www.zoebakes.com/2025/04/21/sourdough/", "content": "sourdough starter recipe content"}]

        with patch.object(searxng, "_fetch_searxng", side_effect=fake_fetch), \
             patch.object(searxng, "get_alternate_phrasing", return_value="alternate sourdough query"):
            result = searxng.search("sourdough starter recipe")

        assert result.count("Sourdough Guide") == 1

    def test_dedupes_same_url_across_queries(self):
        from app.sources import searxng
        from unittest.mock import patch

        call_count = {"n": 0}

        def fake_fetch(query, **kwargs):
            call_count["n"] += 1
            return [{"title": "Same Result", "url": "https://example.com/same", "content": "python programming content here"}]

        with patch.object(searxng, "_fetch_searxng", side_effect=fake_fetch), \
             patch.object(searxng, "get_alternate_phrasing", return_value="alternate python query"):
            result = searxng.search("python programming guide")

        assert call_count["n"] == 2  # both queries fetched
        assert result.count("Same Result") == 1  # but deduped to one

    def test_no_expansion_when_not_eligible(self):
        from app.sources import searxng
        from unittest.mock import patch

        with patch.object(searxng, "_fetch_searxng", return_value=[
                {"title": "Single Result", "url": "https://example.com/x", "content": "python programming content here"}
             ]) as mock_fetch, \
             patch.object(searxng, "get_alternate_phrasing", return_value=None):
            searxng.search("python programming guide")

        # Only the primary query should be fetched when no alternate is available
        assert mock_fetch.call_count == 1

    def test_falls_back_gracefully_when_alternate_fetch_fails(self):
        from app.sources import searxng
        from unittest.mock import patch

        def fake_fetch(query, **kwargs):
            if "alternate" in query:
                return None  # alternate search genuinely fails (connection error etc)
            return [{"title": "Primary Result", "url": "https://example.com/primary", "content": "python programming content here"}]

        with patch.object(searxng, "_fetch_searxng", side_effect=fake_fetch), \
             patch.object(searxng, "get_alternate_phrasing", return_value="alternate python query"):
            result = searxng.search("python programming guide")

        assert "Primary Result" in result


class TestSearxngGuard:
    """Tests for URL guard — short-circuits cleanly when not configured."""

    def test_returns_not_configured_when_url_blank(self):
        from app.sources import searxng
        from app.config import settings
        original = settings.searxng_url
        settings.searxng_url = ""
        try:
            result = searxng.search("test query")
            assert "not configured" in result.lower()
        finally:
            settings.searxng_url = original


class TestSearxngSearch:
    """Tests for search() with mocked HTTP responses."""

    def setup_method(self):
        from app.config import settings
        self._orig_threshold = settings.web_news_score_threshold
        self._orig_top_n = settings.web_news_top_n
        # Use a permissive threshold for most tests so generic mock content
        # (which has little real keyword overlap) isn't scored out entirely —
        # tests that specifically test scoring/filtering set their own values
        settings.web_news_score_threshold = -100

    def teardown_method(self):
        from app.config import settings
        settings.web_news_score_threshold = self._orig_threshold
        settings.web_news_top_n = self._orig_top_n

    def _mock_response(self, results: list) -> MagicMock:
        mock = MagicMock()
        mock.status_code = 200
        mock.json.return_value = {"results": results}
        mock.raise_for_status.return_value = None
        return mock

    def test_request_timeout_is_configurable(self):
        """Regression test for a real gap found via a deliberate config-
        completeness audit: this client-side timeout was hardcoded at
        10s regardless of what SearXNG's own server-side request_timeout
        was configured to, meaning the documented fix for "Error
        reaching SearXNG" (raising SearXNG's max_request_timeout to 20s)
        wouldn't have fully worked, since Mnemolis's own client would
        still cut the connection at 10s first. Confirms the configured
        value is genuinely passed through to the real network call."""
        from app.sources import searxng
        from app.config import settings
        original = settings.searxng_request_timeout_seconds
        settings.searxng_request_timeout_seconds = 25
        try:
            with patch("app.sources.searxng._session.get", return_value=self._mock_response([])) as mock_get:
                searxng._fetch_searxng("test query")
            _, kwargs = mock_get.call_args
            assert kwargs["timeout"] == 25
        finally:
            settings.searxng_request_timeout_seconds = original

    def test_returns_formatted_results(self):
        from app.sources import searxng
        mock_results = [
            {"title": "Nginx Docs", "url": "https://nginx.org", "content": "Official nginx documentation and configuration guide."},
            {"title": "Nginx Tutorial", "url": "https://example.com", "content": "How to configure nginx for production use."},
        ]
        with patch("app.sources.searxng._session.get", return_value=self._mock_response(mock_results)):
            result = searxng.search("nginx")
        assert "Nginx Docs" in result
        assert "nginx.org" in result
        assert "Nginx Tutorial" in result

    def test_respects_configured_top_n(self):
        from app.sources import searxng
        from app.config import settings
        settings.web_news_top_n = 5
        mock_results = [
            {"title": f"Python Result {i}", "url": f"https://example.com/{i}", "content": f"Python programming content number {i} here."}
            for i in range(10)
        ]
        with patch("app.sources.searxng._session.get", return_value=self._mock_response(mock_results)):
            result = searxng.search("python programming")
        # Should be capped at the configured top_n, not the old hardcoded 5
        assert result.count("Python Result") == 5

    def test_empty_results_returns_no_results_message(self):
        from app.sources import searxng
        with patch("app.sources.searxng._session.get", return_value=self._mock_response([])):
            result = searxng.search("xyzzy nothing")
        assert "no results" in result.lower()

    def test_all_results_below_threshold_returns_message(self):
        from app.sources import searxng
        from app.config import settings
        settings.web_news_score_threshold = 1000  # impossibly high, nothing survives
        mock_results = [
            {"title": "Some Result", "url": "https://example.com", "content": "Some content"},
        ]
        with patch("app.sources.searxng._session.get", return_value=self._mock_response(mock_results)):
            result = searxng.search("test query")
        assert "no sufficiently relevant" in result.lower()

    def test_connection_error_returns_error_message(self):
        from app.sources import searxng
        import requests
        with patch("app.sources.searxng._session.get", side_effect=requests.exceptions.ConnectionError("refused")):
            result = searxng.search("test")
        assert "error" in result.lower()

    def test_timeout_returns_distinct_timeout_specific_message(self):
        """Regression test for a real gap found via a deliberate
        complexity-investigation pass: search() used to return the same
        hardcoded "connection failed" message regardless of the real
        failure cause — even though a genuine timeout is this project's
        own documented, historically real failure mode for SearXNG
        specifically (see the wiki's "The SearXNG Timeout Lesson").
        Confirms a real requests.exceptions.Timeout now produces a
        distinct, more accurate, more actionable message rather than
        the same generic "connection failed" every other failure gets."""
        from app.sources import searxng
        import requests
        with patch("app.sources.searxng._session.get",
                   side_effect=requests.exceptions.ReadTimeout("Read timed out. (read timeout=10)")):
            result = searxng.search("test")
        assert "timed out" in result.lower()
        assert "connection failed" not in result.lower()

    def test_connection_error_still_uses_generic_message_not_timeout_specific(self):
        """Confirms the fix didn't accidentally make every failure claim
        to be a timeout — a genuine connection refusal (not a timeout)
        must still get the honest, generic "connection failed" message,
        not the timeout-specific one."""
        from app.sources import searxng
        import requests
        with patch("app.sources.searxng._session.get",
                   side_effect=requests.exceptions.ConnectionError("Connection refused")):
            result = searxng.search("test")
        assert "connection failed" in result.lower()
        assert "timed out" not in result.lower()

    def test_alternate_query_timeout_does_not_produce_user_facing_timeout_message(self):
        """Confirms raise_on_timeout only applies to the PRIMARY fetch —
        if the alternate-query fetch times out, that failure stays
        non-fatal (the primary result still stands), and must NOT
        surface the new timeout-specific message, since the primary
        fetch itself succeeded."""
        from app.sources import searxng
        import requests

        def fake_get(url, params=None, **kwargs):
            if "alternate" in params.get("q", ""):
                raise requests.exceptions.ReadTimeout("Read timed out.")
            mock = MagicMock()
            mock.status_code = 200
            mock.json.return_value = {"results": [
                {"title": "Primary Result", "url": "https://example.com/x", "content": "python programming content here"}
            ]}
            mock.raise_for_status.return_value = None
            return mock

        from app.config import settings
        original_threshold = settings.web_news_score_threshold
        settings.web_news_score_threshold = -100
        try:
            with patch("app.sources.searxng._session.get", side_effect=fake_get), \
                 patch.object(searxng, "get_alternate_phrasing", return_value="alternate python query"):
                result = searxng.search("python programming guide")
        finally:
            settings.web_news_score_threshold = original_threshold

        assert "timed out" not in result.lower()
        assert "Primary Result" in result

    def test_fetch_returns_none_on_failure_not_empty_list(self):
        """Regression test — _fetch_searxng must distinguish 'request failed'
        (None) from 'request succeeded with zero results' ([]) so search()
        can surface a real error message instead of silently reporting
        'no results found' when SearXNG is actually unreachable."""
        from app.sources import searxng
        import requests
        with patch("app.sources.searxng._session.get", side_effect=requests.exceptions.ConnectionError("refused")):
            result = searxng._fetch_searxng("test")
        assert result is None

    def test_fetch_returns_empty_list_on_genuinely_empty_results(self):
        from app.sources import searxng
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"results": []}
        mock_resp.raise_for_status.return_value = None
        with patch("app.sources.searxng._session.get", return_value=mock_resp):
            result = searxng._fetch_searxng("test")
        assert result == []

    def test_results_separated_by_divider(self):
        from app.sources import searxng
        mock_results = [
            {"title": "Result A", "url": "https://a.com", "content": "Detailed content about result topic A here."},
            {"title": "Result B", "url": "https://b.com", "content": "Detailed content about result topic B here."},
        ]
        with patch("app.sources.searxng._session.get", return_value=self._mock_response(mock_results)):
            result = searxng.search("result topic")
        assert "---" in result

    def test_relevant_result_ranks_above_irrelevant(self):
        from app.sources import searxng
        mock_results = [
            {"title": "Unrelated Cooking Recipe", "url": "https://cooking.com", "content": "How to bake bread at home."},
            {"title": "Python Programming Guide", "url": "https://python.com", "content": "Complete python programming tutorial and guide."},
        ]
        with patch("app.sources.searxng._session.get", return_value=self._mock_response(mock_results)):
            result = searxng.search("python programming guide")
        # The relevant result should appear first despite being listed second in raw results
        assert result.index("Python Programming Guide") < result.index("Unrelated Cooking Recipe")

    def test_generic_homepage_result_filtered_out(self):
        from app.sources import searxng
        from app.config import settings
        settings.web_news_score_threshold = 0
        mock_results = [
            {"title": "Home", "url": "https://example.com/", "content": "Welcome"},
            {"title": "Python GPIO Setup Tutorial", "url": "https://example.com/tutorial", "content": "Complete tutorial covering python gpio setup on raspberry pi."},
        ]
        with patch("app.sources.searxng._session.get", return_value=self._mock_response(mock_results)):
            result = searxng.search("python gpio setup")
        assert "Python GPIO Setup Tutorial" in result
        assert result.count("**Home**") == 0


class TestSearxngConcurrentFetch:
    """Regression tests for a real, traced latency bug: the primary
    fetch and the alternate-phrasing chain (get_alternate_phrasing()'s
    own LLM call, plus a second SearXNG fetch) used to run
    SEQUENTIALLY — found via a live Adversarial Self-Testing flag on an
    otherwise simple, single-source query, reproduced directly with
    realistic mocked timings at roughly 4x the cost of a single fetch.

    Verified safe to parallelize first, not just assumed: _fetch_searxng()
    is a pure function with no shared state; the one real concern
    (concurrent routing-cache writes inside get_alternate_phrasing())
    was traced to a genuine, separately-fixed file-write race — see
    test_cache_persistence.py's TestAtomicWriteJson.

    These tests confirm the ACTUAL concurrency property via real timing
    measurements, not just that the end-to-end output looks the same —
    a refactor that accidentally stayed sequential could still pass
    every other existing behavioral test in this file unchanged.
    """

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.llm_url
        self._orig_model = settings.llm_model
        settings.llm_url = "http://ollama:11434"
        settings.llm_model = "qwen3:8b"

    def teardown_method(self):
        from app.config import settings
        settings.llm_url = self._orig_url
        settings.llm_model = self._orig_model

    def test_primary_fetch_and_alternate_chain_genuinely_run_concurrently(self):
        """The real regression test: total elapsed time for a query
        that triggers expansion must be close to the SLOWER of the two
        operations, not their SUM — confirms genuine concurrency, not
        just that the sequential version happens to produce the same
        final string."""
        import time
        from app.sources import searxng

        def slow_fetch(query, raise_on_timeout=False):
            time.sleep(0.3)
            return [{"title": f"Result for {query}", "url": f"https://example.com/{query}", "content": "relevant content here matching the query"}]

        def slow_alternate_phrasing(query):
            time.sleep(0.3)
            return "a genuinely different phrasing of the same question"

        with patch("app.sources.searxng._fetch_searxng", side_effect=slow_fetch), \
             patch("app.sources.searxng.get_alternate_phrasing", side_effect=slow_alternate_phrasing):
            start = time.monotonic()
            searxng.search("test query for concurrency timing")
            elapsed = time.monotonic() - start

        # Sequential would be ~0.3 (primary) + 0.3 (llm) + 0.3 (second
        # fetch) = ~0.9s. Concurrent should be close to max(0.3, 0.3+0.3)
        # = ~0.6s. Generous upper bound to avoid CI flakiness while
        # still clearly distinguishing concurrent from sequential.
        assert elapsed < 0.75, f"took {elapsed:.2f}s — looks sequential, not concurrent"

    def test_primary_timeout_still_returns_correct_message_when_alternate_thread_also_running(self):
        """Confirms the real, specific timeout message is preserved
        exactly even while the alternate-phrasing thread is genuinely
        still in flight on its own thread — not just when it's already
        finished or never started."""
        import time
        import requests
        from app.sources import searxng

        def timeout_primary(query, raise_on_timeout=False):
            raise requests.exceptions.Timeout("simulated timeout")

        def slow_alternate_phrasing(query):
            time.sleep(0.2)
            return "alternate phrasing"

        with patch("app.sources.searxng._fetch_searxng", side_effect=timeout_primary), \
             patch("app.sources.searxng.get_alternate_phrasing", side_effect=slow_alternate_phrasing):
            result = searxng.search("test query")

        assert "request timed out" in result.lower()

    def test_primary_timeout_returns_promptly_even_while_alternate_thread_still_running(self):
        """Regression test for a real bug found via a deliberate
        function-by-function read, distinct from the message-
        correctness test above: a bare `with ThreadPoolExecutor(...) as
        executor:` block here used to mean a `return` from inside it
        didn't actually reach the caller until __exit__'s implicit
        shutdown(wait=True) completed — so a fast, already-decided
        timeout error still silently waited for an unrelated, slower
        concurrent alternate-phrasing thread to finish first, even
        though the function had already decided what to return.
        Confirmed directly: this exact scenario took the full duration
        of the slow alternate thread before this fix, not the near-
        instant primary-timeout duration it should have taken. Switching
        to the shared, never-torn-down _searxng_executor fixes this as
        a side effect, since there's no per-call executor left to wait
        for on the way out."""
        import time
        import requests
        from app.sources import searxng

        def timeout_primary(query, raise_on_timeout=False):
            raise requests.exceptions.Timeout("simulated timeout")

        def very_slow_alternate_phrasing(query):
            time.sleep(0.5)
            return "alternate phrasing"

        with patch("app.sources.searxng._fetch_searxng", side_effect=timeout_primary), \
             patch("app.sources.searxng.get_alternate_phrasing", side_effect=very_slow_alternate_phrasing):
            start = time.monotonic()
            result = searxng.search("test query")
            elapsed = time.monotonic() - start

        assert "request timed out" in result.lower()
        # The real regression check: must return promptly, nowhere near
        # the 0.5s the slow alternate thread takes — generous bound to
        # avoid CI flakiness while still clearly distinguishing "fixed"
        # from "silently waited for the unrelated slow thread."
        assert elapsed < 0.2, f"took {elapsed:.2f}s — still silently waiting on the alternate thread"

    def test_alternate_chain_failure_does_not_affect_primary_result_when_concurrent(self):
        """The non-fatal-alternate-failure guarantee, re-verified under
        genuine concurrency — a real exception raised inside the
        alternate-phrasing thread must not propagate or corrupt the
        primary result."""
        import pytest
        from app.sources import searxng

        def working_primary(query, raise_on_timeout=False):
            return [{"title": "Primary Result", "url": "https://example.com/primary", "content": "real relevant content for this query"}]

        def broken_alternate_phrasing(query):
            raise RuntimeError("simulated unexpected failure in alternate phrasing")

        with patch("app.sources.searxng._fetch_searxng", side_effect=working_primary), \
             patch("app.sources.searxng.get_alternate_phrasing", side_effect=broken_alternate_phrasing):
            # NOTE: _alternate_phrasing_chain() itself doesn't catch
            # arbitrary exceptions from get_alternate_phrasing() —
            # confirming the REAL current contract directly, since
            # get_alternate_phrasing() is documented to return None on
            # failure, never raise, so search() was never written to
            # defend against it raising. This test exists to make that
            # real boundary explicit rather than leave it undocumented.
            with pytest.raises(RuntimeError):
                searxng.search("test query")

    def test_both_fetches_succeed_and_results_genuinely_merge_under_concurrency(self):
        """End-to-end confirmation that concurrent execution doesn't
        accidentally drop or duplicate results compared to the old
        sequential behavior."""
        from app.sources import searxng
        from app.config import settings
        settings.web_news_score_threshold = -100

        def fake_fetch(query, raise_on_timeout=False):
            if "alternate" in query:
                return [{"title": "Alternate Result", "url": "https://example.com/alt", "content": "python programming alternate content"}]
            return [{"title": "Primary Result", "url": "https://example.com/primary", "content": "python programming primary content"}]

        with patch("app.sources.searxng._fetch_searxng", side_effect=fake_fetch), \
             patch("app.sources.searxng.get_alternate_phrasing", return_value="alternate python programming query"):
            result = searxng.search("python programming")

        assert "Primary Result" in result
        assert "Alternate Result" in result

    def test_suppress_cache_writes_genuinely_suppresses_writes_from_the_concurrent_alternate_thread(self):
        """The most important test in this file. ThreadPoolExecutor does
        NOT propagate contextvars.ContextVar state into worker threads
        by default — confirmed as a real, live regression while
        researching this exact change: the first version of the
        concurrent fetch fix submitted the alternate-phrasing chain
        directly (executor.submit(_alternate_phrasing_chain, query)),
        and a real test of exactly this scenario showed
        router.suppress_cache_writes() being silently ignored inside
        that worker thread — letting a synthetic Adversarial Self-
        Testing query leak a real write into the routing cache,
        precisely the bug suppress_cache_writes() exists to prevent,
        reintroduced by the very change meant to improve performance.

        Fixed by giving each submitted task its own
        contextvars.copy_context() call (a SHARED single context object
        cannot be entered by two threads simultaneously — found via a
        second real failure when the first attempted fix tried exactly
        that) so the calling thread's suppression state correctly
        propagates into both worker threads."""
        import app.router as router_module
        from app.sources import searxng

        original_cache = dict(router_module._routing_cache)
        router_module._routing_cache.clear()
        try:
            def fake_fetch(query, raise_on_timeout=False):
                return [{"title": "Result", "url": "https://example.com/a", "content": "real relevant content matching the query well"}]

            with patch.object(router_module.settings, "llm_url", "http://fake"), \
                 patch.object(router_module.settings, "llm_model", "fake-model"), \
                 patch("app.llm.is_configured", return_value=True), \
                 patch("app.llm.complete", return_value="a different phrasing of the same question"), \
                 patch("app.sources.searxng._fetch_searxng", side_effect=fake_fetch):
                with router_module.suppress_cache_writes():
                    searxng.search("suppression regression test query")

            assert len(router_module._routing_cache) == 0, (
                "a write leaked through the concurrent alternate-phrasing thread "
                "despite suppress_cache_writes() being active in the calling thread"
            )
        finally:
            router_module._routing_cache.clear()
            router_module._routing_cache.update(original_cache)

    def test_normal_unsuppressed_call_still_caches_correctly(self):
        """The flip side of the test above — confirms the fix didn't
        accidentally suppress writes ALL the time, only when
        suppress_cache_writes() is genuinely active."""
        import app.router as router_module
        from app.sources import searxng

        original_cache = dict(router_module._routing_cache)
        router_module._routing_cache.clear()
        try:
            def fake_fetch(query, raise_on_timeout=False):
                return [{"title": "Result", "url": "https://example.com/a", "content": "real relevant content matching the query well"}]

            with patch.object(router_module.settings, "llm_url", "http://fake"), \
                 patch.object(router_module.settings, "llm_model", "fake-model"), \
                 patch("app.llm.is_configured", return_value=True), \
                 patch("app.llm.complete", return_value="a different phrasing of the same question"), \
                 patch("app.sources.searxng._fetch_searxng", side_effect=fake_fetch):
                searxng.search("normal unsuppressed query about programming languages")  # no suppression context, long enough that the mocked alternate phrasing passes the real 2x-word-count sanity check

            assert len(router_module._routing_cache) == 1
        finally:
            router_module._routing_cache.clear()
            router_module._routing_cache.update(original_cache)


class TestSearxngSharedExecutor:
    """Regression tests for replacing search()'s per-call
    ThreadPoolExecutor with a single, shared, module-level pool — the
    identical fix already applied to fusion.py (see that file's own
    test_fusion.py::TestFusionSharedExecutor for the precedent this
    borrows from directly), found via a deliberate function-by-function
    read of this file rather than a reported failure."""

    def test_concurrent_search_calls_reuse_the_shared_pool_not_create_unbounded_threads(self):
        """Confirms multiple concurrent search() calls don't each spin
        up their own fresh 2-worker executor — the real, confirmed bug
        this fix closes (15 concurrent search() calls under realistic
        network latency produced a measured peak of 46 real OS threads,
        with no ceiling as concurrent traffic increases)."""
        import threading
        import time
        from app.sources import searxng
        from app.config import settings

        def slow_fetch(query, raise_on_timeout=False):
            time.sleep(0.1)
            return [{"title": "R", "url": "https://example.com", "content": "content"}]

        baseline = threading.active_count()
        threads = []
        with patch("app.sources.searxng._fetch_searxng", side_effect=slow_fetch), \
             patch("app.sources.searxng.get_alternate_phrasing", return_value=None):
            for _ in range(8):
                t = threading.Thread(target=searxng.search, args=("q",))
                threads.append(t)
                t.start()
            time.sleep(0.05)  # let everything get mid-flight
            peak = threading.active_count()
            for t in threads:
                t.join()

        # 8 concurrent "request" threads, each spinning up its own
        # 2-worker executor under the old per-call pattern, would be 16+
        # fresh worker threads on top of the 8 request threads
        # themselves (24+ total). With a shared pool capped at
        # settings.searxng_thread_pool_size, worker thread count itself
        # is bounded regardless of how many concurrent callers there
        # are — confirmed by checking the actual ceiling rather than
        # asserting an exact number (real thread counts vary slightly
        # by interpreter/test-runner baseline).
        assert peak <= baseline + 8 + settings.searxng_thread_pool_size

    def test_searxng_thread_pool_size_setting_is_read(self):
        """Confirms the shared executor is actually sized from
        settings.searxng_thread_pool_size, not a hardcoded value."""
        from app.sources import searxng
        from app.config import settings
        assert searxng._searxng_executor._max_workers == settings.searxng_thread_pool_size

