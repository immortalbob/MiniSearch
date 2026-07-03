"""
Tests for Explanation Chains — the ordered trace of routing events
route_query() collects via _route_event() and /search returns when the
request sets explain=true.

The core design property under test: the explanation is the SAME
recording as the boolean stats (both write into route_query()'s one
_ROUTE_STATS dict at the same authoritative code points), never a
parallel reconstruction — so it structurally cannot disagree with what
actually ran.
"""

from unittest.mock import patch

import app.router as router_module
from app.router import route_query, clear_cache, clear_routing_cache, _set_cached


class _RouterHarness:
    def setup_method(self):
        self._save_patch = patch("app.router._save_cache")
        self._save_routing_patch = patch("app.router._save_routing_cache")
        self._save_patch.start()
        self._save_routing_patch.start()
        clear_cache()
        clear_routing_cache()
        self._original_map = dict(router_module.SOURCE_MAP)

    def teardown_method(self):
        router_module.SOURCE_MAP.clear()
        router_module.SOURCE_MAP.update(self._original_map)
        clear_cache()
        clear_routing_cache()
        self._save_patch.stop()
        self._save_routing_patch.stop()

    def steps(self, outcome):
        return [e["step"] for e in outcome.explanation]


class TestSingleSourceChains(_RouterHarness):
    def test_cold_explicit_source_records_a_timed_invocation(self):
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        outcome = route_query("explain cold test", "forecast")
        invoked = [e for e in outcome.explanation if e["step"] == "source_invoked"]
        assert len(invoked) == 1
        assert invoked[0]["source"] == "forecast"
        assert invoked[0]["query"] == "explain cold test"
        # elapsed_ms is a real measurement around the handler call — an
        # int, and non-negative. Its exact value is timing-dependent and
        # deliberately not pinned.
        assert isinstance(invoked[0]["elapsed_ms"], int)
        assert invoked[0]["elapsed_ms"] >= 0

    def test_warm_query_records_cache_hit_and_no_invocation(self):
        router_module.SOURCE_MAP["forecast"] = lambda q: "should not run"
        _set_cached("forecast", "explain warm test", "Today will be clear.")
        outcome = route_query("explain warm test", "forecast")
        assert "result_cache_hit" in self.steps(outcome)
        assert "source_invoked" not in self.steps(outcome)
        assert outcome.cached is True

    def test_keyword_intent_resolution_is_recorded_with_its_decision(self):
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        with patch("app.router.detect_conditional", return_value=None):
            outcome = route_query("what is the weather today", "auto")
        keyword_events = [e for e in outcome.explanation if e["step"] == "intent_keyword"]
        assert len(keyword_events) == 1
        assert keyword_events[0]["decision"] == "forecast"


class TestFallbackChains(_RouterHarness):
    def test_fallback_produces_fallback_event_between_two_invocations(self):
        """The chain must show the full story: kiwix invoked, came back
        empty, fallback declared, web invoked — in that order, since
        within one thread events append in genuine execution order."""
        router_module.SOURCE_MAP["kiwix"] = lambda q: "No results found in wikipedia."
        router_module.SOURCE_MAP["web"] = lambda q: "**Real web result**\nContent."
        outcome = route_query("explain fallback test", "kiwix")
        steps = self.steps(outcome)
        assert steps.count("source_invoked") == 2
        fallback_idx = steps.index("fallback")
        first_invoke = steps.index("source_invoked")
        last_invoke = len(steps) - 1 - steps[::-1].index("source_invoked")
        assert first_invoke < fallback_idx < last_invoke
        fallback_event = [e for e in outcome.explanation if e["step"] == "fallback"][0]
        assert fallback_event["from_source"] == "kiwix"
        assert fallback_event["to_source"] == "web"


class TestCompoundChains(_RouterHarness):
    def _fake_intent(self, q):
        return "forecast" if "weather" in q else "ha"

    def test_decomposed_query_records_the_split_and_attributes_sub_events(self):
        """Concurrent sub-query events interleave nondeterministically —
        attribution must come from each event's own query field, never
        list position, which is exactly what these assertions rely on."""
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        router_module.SOURCE_MAP["ha"] = lambda q: "**Lights:**\n- Kitchen: on"
        with patch("app.router.detect_intent", side_effect=self._fake_intent):
            outcome = route_query(
                "what is the weather today and also lights status", "auto"
            )
        decomposed = [e for e in outcome.explanation if e["step"] == "decomposed"]
        assert len(decomposed) == 1
        assert len(decomposed[0]["parts"]) == 2
        invoked_queries = {
            e["query"] for e in outcome.explanation if e["step"] == "source_invoked"
        }
        assert any("weather" in q for q in invoked_queries)
        assert any("lights" in q for q in invoked_queries)

    def test_conditional_query_records_the_extraction(self):
        router_module.SOURCE_MAP["ha"] = lambda q: "**Front door:** unlocked"
        with patch("app.router.detect_intent", return_value="ha"):
            outcome = route_query("if the front door is unlocked, let me know", "auto")
        conditional = [e for e in outcome.explanation if e["step"] == "conditional_detected"]
        assert len(conditional) == 1
        assert conditional[0]["condition"] == "the front door is unlocked"

    def test_fusion_dispatch_records_its_source_list(self):
        router_module.SOURCE_MAP["kiwix"] = lambda q: "# Article\nContent."
        router_module.SOURCE_MAP["web"] = lambda q: "**Web**\nContent."
        outcome = route_query("explain fusion test", "fusion", ["kiwix", "web"])
        fusion_events = [e for e in outcome.explanation if e["step"] == "fusion"]
        assert len(fusion_events) == 1
        assert set(fusion_events[0]["sources"]) == {"kiwix", "web"}


class TestExplanationOverTheApi(_RouterHarness):
    def _client(self):
        from fastapi.testclient import TestClient
        import app.main as main
        return TestClient(main.app)

    def test_explain_true_returns_the_chain(self):
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        resp = self._client().post(
            "/search",
            json={"query": "api explain test", "source": "forecast", "explain": True},
        )
        data = resp.json()
        assert data["success"] is True
        assert isinstance(data["explanation"], list)
        assert any(e["step"] == "source_invoked" for e in data["explanation"])

    def test_explain_defaults_off_and_returns_null(self):
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        resp = self._client().post(
            "/search", json={"query": "api no explain test", "source": "forecast"}
        )
        assert resp.json()["explanation"] is None

    def test_failure_returns_the_partial_chain(self):
        """A trace earns its keep most on the failure path — the events
        recorded BEFORE the exception must come back with the error."""
        def exploding(q):
            raise RuntimeError("handler boom")

        router_module.SOURCE_MAP["kiwix"] = lambda q: "No results found in wikipedia."
        router_module.SOURCE_MAP["web"] = exploding
        resp = self._client().post(
            "/search",
            json={"query": "api failure explain test", "source": "kiwix", "explain": True},
        )
        data = resp.json()
        assert data["success"] is False
        steps = [e["step"] for e in data["explanation"]]
        # kiwix ran and the fallback was declared before web blew up —
        # both must be visible in the partial chain.
        assert "source_invoked" in steps
        assert "fallback" in steps
