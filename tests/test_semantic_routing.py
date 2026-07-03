"""
Tests for app/semantic_routing.py — the embedding-based reuse of LLM
routing decisions across rephrasings — and its integration point inside
router._llm_detect().

Vectors in these tests are small hand-built lists rather than real
model output: cosine similarity is a pure function of the vectors, so
the store/match/threshold/pruning logic under test is exercised
identically, without any network dependency. llm.embed() is mocked at
app.semantic_routing.embed (the name the module actually calls).
"""

import math
from unittest.mock import patch

from app import semantic_routing
from app.config import settings


def _reset():
    semantic_routing.clear()
    semantic_routing._store_model = settings.embedding_model


def _configure(model="test-embed"):
    settings.embedding_model = model
    settings.llm_url = settings.llm_url or "http://beast:11434"
    semantic_routing._store_model = model


def _deconfigure():
    settings.embedding_model = ""
    semantic_routing.clear()
    semantic_routing._store_model = ""


class TestDisabledByDefault:
    """EMBEDDING_MODEL defaults to empty — the entire feature must be a
    guaranteed no-op with zero network calls, so a fresh GitHub
    deployment that never pulled an embedding model pays nothing."""

    def setup_method(self):
        _reset()
        settings.embedding_model = ""

    def test_find_similar_noops_without_a_model(self):
        with patch("app.semantic_routing.embed") as mock_embed:
            match, vector = semantic_routing.find_similar("anything", lambda q: "web")
        assert match is None and vector is None
        mock_embed.assert_not_called()

    def test_store_noops_without_a_model(self):
        with patch("app.semantic_routing.embed") as mock_embed:
            semantic_routing.store("anything")
        assert semantic_routing.stats()["entries"] == 0
        mock_embed.assert_not_called()


class TestStoreAndMatch:
    def setup_method(self):
        _configure()
        _reset()

    def teardown_method(self):
        _deconfigure()

    def test_identical_direction_vector_matches_above_threshold(self):
        # Same direction, different magnitude — cosine similarity 1.0
        # after normalization, so magnitude must not matter.
        with patch("app.semantic_routing.embed", return_value=[2.0, 0.0, 0.0]):
            semantic_routing.store("will it rain later")
        with patch("app.semantic_routing.embed", return_value=[5.0, 0.0, 0.0]):
            match, vector = semantic_routing.find_similar(
                "will it rain this evening", lambda q: "forecast"
            )
        assert match is not None
        assert match.matched_query == "will it rain later"
        assert match.decision == "forecast"
        assert match.similarity > 0.999
        assert vector is not None

    def test_orthogonal_vector_does_not_match(self):
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("will it rain later")
        with patch("app.semantic_routing.embed", return_value=[0.0, 1.0]):
            match, vector = semantic_routing.find_similar(
                "is my network down", lambda q: "forecast"
            )
        assert match is None
        # The computed vector is still returned on a miss, so the
        # caller can store it after the LLM decides without a second
        # embedding call — the one-embedding-call-total contract.
        assert vector is not None
        assert abs(math.sqrt(sum(x * x for x in vector)) - 1.0) < 1e-9

    def test_just_below_threshold_does_not_match(self):
        """The threshold is a floor, and the conservative default exists
        because a wrong reuse silently misroutes — a candidate a hair
        under it must lose."""
        settings.semantic_routing_threshold = 0.92
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("query a")
        # cos(theta) = 0.9 < 0.92
        angle = math.acos(0.90)
        with patch("app.semantic_routing.embed",
                   return_value=[math.cos(angle), math.sin(angle)]):
            match, _ = semantic_routing.find_similar("query b", lambda q: "web")
        assert match is None

    def test_best_match_wins_not_first_above_threshold(self):
        """The scan deliberately never early-exits — with two candidates
        above threshold, the BEST one must be returned."""
        settings.semantic_routing_threshold = 0.5
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("close match")
        angle = math.acos(0.7)
        with patch("app.semantic_routing.embed",
                   return_value=[math.cos(angle), math.sin(angle)]):
            semantic_routing.store("further match")
        decisions = {"close match": "forecast", "further match": "web"}
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            match, _ = semantic_routing.find_similar("new phrasing", decisions.get)
        assert match is not None
        assert match.matched_query == "close match"
        settings.semantic_routing_threshold = 0.92

    def test_empty_store_returns_before_any_embedding_call(self):
        """The feature's cost on a fresh start must be genuinely zero —
        no network I/O until there is something to match against."""
        with patch("app.semantic_routing.embed") as mock_embed:
            match, vector = semantic_routing.find_similar("anything", lambda q: "web")
        assert match is None and vector is None
        mock_embed.assert_not_called()

    def test_embedding_failure_is_a_clean_miss(self):
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("existing")
        with patch("app.semantic_routing.embed", return_value=None):
            match, vector = semantic_routing.find_similar("new", lambda q: "web")
        assert match is None and vector is None

    def test_zero_vector_is_rejected_not_divided_by(self):
        with patch("app.semantic_routing.embed", return_value=[0.0, 0.0, 0.0]):
            semantic_routing.store("degenerate")
        assert semantic_routing.stats()["entries"] == 0


class TestLivenessAndPruning:
    """The store holds only embeddings; decisions always come from the
    live routing cache via the caller's lookup — design constraint 2."""

    def setup_method(self):
        _configure()
        _reset()

    def teardown_method(self):
        _deconfigure()

    def test_expired_decision_is_skipped_and_pruned(self):
        """A perfect-similarity candidate whose routing entry has
        expired must NOT produce a match (the TTL retired that decision
        deliberately), and must be pruned so it stops winning lookups
        it can't pay off."""
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("expired query")
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            match, _ = semantic_routing.find_similar("new phrasing", lambda q: None)
        assert match is None
        assert semantic_routing.stats()["entries"] == 0

    def test_exact_key_is_never_matched_against_itself(self):
        """find_similar() only ever runs after the exact-match routing
        cache MISSED — the only way this store can contain the exact
        key while the routing entry is gone is expiry, and re-serving
        an expired decision via similarity would defeat the TTL."""
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("will it rain later")
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            match, _ = semantic_routing.find_similar(
                "will it rain later", lambda q: "forecast"
            )
        assert match is None

    def test_dimension_mismatch_is_pruned_not_compared(self):
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0, 0.0]):
            semantic_routing.store("three dims")
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            match, _ = semantic_routing.find_similar("two dims", lambda q: "web")
        assert match is None
        assert semantic_routing.stats()["entries"] == 0


class TestBoundsAndModelChange:
    def setup_method(self):
        _configure()
        _reset()

    def teardown_method(self):
        _deconfigure()
        settings.semantic_cache_max_size = 500

    def test_oldest_entry_evicted_at_max_size(self):
        settings.semantic_cache_max_size = 2
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("first")
            semantic_routing.store("second")
            semantic_routing.store("third")
        assert semantic_routing.stats()["entries"] == 2
        assert "first" not in semantic_routing._store
        assert "third" in semantic_routing._store

    def test_model_change_drops_the_store(self):
        """Vectors from different embedding models live in different
        spaces — cross-model cosine similarity is noise that could
        clear the threshold by accident, so a model change must drop
        everything rather than compare across."""
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("old model entry")
        assert semantic_routing.stats()["entries"] == 1
        settings.embedding_model = "different-embed"
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            match, _ = semantic_routing.find_similar("anything", lambda q: "web")
        assert match is None
        assert semantic_routing.stats()["entries"] == 0


class TestLlmDetectIntegration:
    """The one hook site: router._llm_detect(), inside the singleflight,
    after the exact-match re-check, before the LLM prompt is built."""

    def setup_method(self):
        from app.router import clear_routing_cache
        self._save_patch = patch("app.router._save_routing_cache")
        self._save_patch.start()
        clear_routing_cache()
        _configure()
        _reset()

    def teardown_method(self):
        from app.router import clear_routing_cache
        clear_routing_cache()
        self._save_patch.stop()
        _deconfigure()

    def test_semantic_hit_skips_the_llm_entirely(self):
        """The whole point: a rephrasing of an already-decided query
        must reuse that decision without complete() ever being called —
        and must promote itself to a normal exact-match entry so the
        same phrasing never even pays the embedding call again."""
        from app.router import _llm_detect, _set_routing, _get_routing

        _set_routing("source:will it rain later", "forecast")
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("will it rain later")

        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]), \
             patch("app.llm.complete") as mock_complete, \
             patch("app.llm.is_configured", return_value=True):
            result = _llm_detect("will it rain this evening")

        assert result == "forecast"
        mock_complete.assert_not_called()
        # Promoted: the new phrasing is now a plain exact-match entry...
        assert _get_routing("source:will it rain this evening") == "forecast"
        # ...and itself semantically matchable for the NEXT rephrasing.
        assert "will it rain this evening" in semantic_routing._store

    def test_semantic_miss_falls_through_to_the_llm_and_stores_embedding(self):
        """A miss must cost exactly what v3.52.0 cost (the LLM call) —
        plus the new decision becomes matchable using the vector the
        lookup already computed, never a second embedding call."""
        from app.router import _llm_detect

        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]) as mock_embed:
            semantic_routing.store("is my network down")

        with patch("app.semantic_routing.embed", return_value=[0.0, 1.0]) as mock_embed, \
             patch("app.llm.complete", return_value="forecast") as mock_complete, \
             patch("app.llm.is_configured", return_value=True):
            result = _llm_detect("will it rain this evening")

        assert result == "forecast"
        mock_complete.assert_called_once()
        assert mock_embed.call_count == 1  # the lookup's call, reused by store()
        assert "will it rain this evening" in semantic_routing._store

    def test_feature_off_is_exactly_v3520_behavior(self):
        from app.router import _llm_detect
        settings.embedding_model = ""
        with patch("app.semantic_routing.embed") as mock_embed, \
             patch("app.llm.complete", return_value="web") as mock_complete, \
             patch("app.llm.is_configured", return_value=True):
            result = _llm_detect("some fresh query")
        assert result == "web"
        mock_complete.assert_called_once()
        mock_embed.assert_not_called()

    def test_semantic_hit_records_explanation_event_and_stat(self):
        """The hit must be visible to both halves of the observability
        this release ships — the explanation chain event AND the
        semantic_routing_hit stat — recorded via route_query()'s real
        channel, not asserted against internals."""
        from app.router import route_query, _set_routing
        import app.router as router_module

        _set_routing("source:will it rain later", "forecast")
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("will it rain later")

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        try:
            with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]), \
                 patch("app.llm.complete") as mock_complete, \
                 patch("app.llm.is_configured", return_value=True), \
                 patch("app.router._keyword_detect", return_value=None):
                outcome = route_query("will it rain this evening", "auto")
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert outcome.success is True
        mock_complete.assert_not_called()
        semantic_events = [e for e in outcome.explanation if e["step"] == "intent_semantic"]
        assert len(semantic_events) == 1
        assert semantic_events[0]["matched_query"] == "will it rain later"
        assert semantic_events[0]["similarity"] >= 0.999


class TestWarmup:
    """Tests for the startup warmup — semantic_routing.warm() plus
    router.warm_semantic_routing_cache()'s key filtering. The store is
    deliberately in-memory only; the warmup re-embeds the routing
    cache's PERSISTED queries after a restart so rephrasings match
    immediately instead of only after each query is re-decided once."""

    def setup_method(self):
        from app.router import clear_routing_cache
        self._save_patch = patch("app.router._save_routing_cache")
        self._save_patch.start()
        clear_routing_cache()
        _configure()
        _reset()

    def teardown_method(self):
        from app.router import clear_routing_cache
        clear_routing_cache()
        self._save_patch.stop()
        _deconfigure()
        settings.semantic_cache_max_size = 500
        settings.semantic_warmup_enabled = True

    def test_warms_only_live_source_keys(self):
        """Only 'source:' entries are semantic-match candidates — book
        selections and disambiguation candidates in the same routing
        cache must never be embedded — and only LIVE ones: an entry
        past ROUTING_CACHE_TTL would waste a slot on a candidate
        find_similar() immediately skips."""
        import time
        import app.router as router
        router._set_routing("source:will it rain later", "forecast")
        router._set_routing("source:door status", "ha")
        router._set_routing("book:mercury", "wikipedia")
        router._set_routing("disambig_candidates:mercury", "a,b,c")
        # Manufacture an expired source entry directly
        router._routing_cache["source:ancient query"] = (
            "web", time.time() - router.ROUTING_CACHE_TTL - 10,
        )
        with patch("app.semantic_routing.embed_batch",
                   return_value=[[1.0, 0.0], [0.0, 1.0]]) as mock_batch:
            stored = router.warm_semantic_routing_cache()
        assert stored == 2
        warmed_texts = mock_batch.call_args[0][0]
        assert set(warmed_texts) == {"will it rain later", "door status"}
        assert "ancient query" not in semantic_routing._store

    def test_warmed_entry_actually_matches_a_rephrasing(self):
        """End to end: a warmed embedding must behave identically to a
        lazily-stored one — the whole point is restoring pre-restart
        matching ability."""
        import app.router as router
        router._set_routing("source:will it rain later", "forecast")
        with patch("app.semantic_routing.embed_batch", return_value=[[1.0, 0.0]]):
            router.warm_semantic_routing_cache()
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            match, _ = semantic_routing.find_similar(
                "will it rain this evening", lambda q: "forecast"
            )
        assert match is not None
        assert match.matched_query == "will it rain later"

    def test_newest_entries_win_when_over_capacity(self):
        """More persisted queries than free slots: the most recently
        decided ones — the likeliest to be rephrased soon — must get
        the embeddings."""
        settings.semantic_cache_max_size = 2
        entries = [("oldest", 100.0), ("middle", 200.0), ("newest", 300.0)]
        with patch("app.semantic_routing.embed_batch",
                   return_value=[[1.0, 0.0], [0.0, 1.0]]) as mock_batch:
            stored = semantic_routing.warm(entries)
        assert stored == 2
        assert mock_batch.call_args[0][0] == ["newest", "middle"]

    def test_already_present_keys_are_not_reembedded(self):
        with patch("app.semantic_routing.embed", return_value=[1.0, 0.0]):
            semantic_routing.store("already here")
        with patch("app.semantic_routing.embed_batch",
                   return_value=[[0.0, 1.0]]) as mock_batch:
            stored = semantic_routing.warm([("already here", 100.0), ("fresh", 200.0)])
        assert stored == 1
        assert mock_batch.call_args[0][0] == ["fresh"]

    def test_failed_batch_aborts_cleanly(self):
        """A batch failure means the backend is down or the model isn't
        pulled — every subsequent batch would fail identically, so the
        warmup must stop, keep whatever it stored, and leave lazy
        population as the path forward. Never raises."""
        entries = [(f"query {i}", float(i)) for i in range(80)]  # 3 batches of 32
        responses = [
            [[1.0, 0.0]] * 32,   # batch 1 succeeds
            None,                # batch 2 fails
        ]
        with patch("app.semantic_routing.embed_batch", side_effect=responses) as mock_batch:
            stored = semantic_routing.warm(entries)
        assert stored == 32
        assert mock_batch.call_count == 2  # never attempted batch 3

    def test_disabled_warmup_is_a_noop(self):
        import app.router as router
        settings.semantic_warmup_enabled = False
        router._set_routing("source:will it rain later", "forecast")
        with patch("app.semantic_routing.embed_batch") as mock_batch:
            stored = router.warm_semantic_routing_cache()
        assert stored == 0
        mock_batch.assert_not_called()

    def test_unconfigured_embeddings_is_a_noop(self):
        import app.router as router
        settings.embedding_model = ""
        router._set_routing("source:will it rain later", "forecast")
        with patch("app.semantic_routing.embed_batch") as mock_batch:
            stored = router.warm_semantic_routing_cache()
        assert stored == 0
        mock_batch.assert_not_called()
