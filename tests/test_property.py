"""
Property-based tests using Hypothesis.

Generates hundreds of random/adversarial inputs automatically to find
edge cases that hand-written fuzz tests wouldn't think to try.

Focuses on pure functions that take arbitrary strings and must never
crash, regardless of input — decomposition, stemming, scoring, diffing,
and HA query filtering.
"""
import json
from hypothesis import given, strategies as st, settings, HealthCheck


# A broad text strategy covering printable text, unicode, and edge whitespace
text_strategy = st.text(min_size=0, max_size=500)

# A stricter strategy mimicking realistic query-like input
query_like_strategy = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),  # exclude surrogates
    min_size=0,
    max_size=300,
)


class TestDecomposeProperties:
    """Property: _decompose must never crash and must always return a list."""

    @given(query_like_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_decompose_never_crashes(self, query):
        from app.router import _decompose
        result = _decompose(query)
        assert isinstance(result, list)

    @given(query_like_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_decompose_always_returns_at_least_one_part(self, query):
        from app.router import _decompose
        result = _decompose(query)
        assert len(result) >= 1

    @given(st.text(alphabet="and ", min_size=0, max_size=200))
    @settings(max_examples=100)
    def test_decompose_handles_repeated_conjunctions(self, query):
        from app.router import _decompose
        result = _decompose(query)
        assert isinstance(result, list)

    @given(st.lists(st.text(min_size=1, max_size=20), min_size=0, max_size=10))
    @settings(max_examples=100)
    def test_decompose_handles_joined_random_words(self, words):
        from app.router import _decompose
        query = " and ".join(words)
        result = _decompose(query)
        assert isinstance(result, list)
        assert len(result) >= 1


class TestStemProperties:
    """Property: _stem must never crash and must be idempotent-ish (stable on repeat)."""

    @given(text_strategy)
    @settings(max_examples=300)
    def test_stem_never_crashes(self, word):
        from app.sources.kiwix import _stem
        result = _stem(word)
        assert isinstance(result, str)

    @given(text_strategy)
    @settings(max_examples=200)
    def test_stem_result_not_longer_than_input(self, word):
        from app.sources.kiwix import _stem
        result = _stem(word)
        # Stemming only removes suffixes, never adds characters
        assert len(result) <= len(word)

    @given(st.text(alphabet=st.characters(whitelist_categories=("Lu", "Ll")), min_size=1, max_size=50))
    @settings(max_examples=200)
    def test_stem_is_deterministic(self, word):
        from app.sources.kiwix import _stem
        result1 = _stem(word)
        result2 = _stem(word)
        assert result1 == result2


class TestScoreResultProperties:
    """Property: _score_result must never crash regardless of query or result content."""

    @given(
        title=text_strategy,
        excerpt=text_strategy,
        query=text_strategy,
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_score_result_never_crashes(self, title, excerpt, query):
        from app.sources.kiwix import _score_result
        result = {"title": title, "excerpt": excerpt, "book": "wikipedia_en_all_maxi_2026-02"}
        score = _score_result(result, query, "wikipedia_en_all_maxi_2026-02")
        assert isinstance(score, int)

    @given(query=text_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_score_result_handles_empty_title_and_excerpt(self, query):
        from app.sources.kiwix import _score_result
        result = {"title": "", "excerpt": "", "book": "wikipedia_en_all_maxi_2026-02"}
        score = _score_result(result, query, "wikipedia_en_all_maxi_2026-02")
        assert isinstance(score, int)


class TestIsDefinitionalQueryProperties:
    """Property: _is_definitional_query must never crash and always returns bool."""

    @given(text_strategy)
    @settings(max_examples=300)
    def test_never_crashes_and_returns_bool(self, query):
        from app.sources.kiwix import _is_definitional_query
        result = _is_definitional_query(query)
        assert isinstance(result, bool)


class TestHAFilterProperties:
    """Property: HA filter building and exclusion checks must never crash."""

    @given(text_strategy)
    @settings(max_examples=300)
    def test_build_filter_never_crashes(self, query):
        from app.sources.home_assistant import _build_filter
        result = _build_filter(query)
        assert isinstance(result, dict)

    @given(text_strategy)
    @settings(max_examples=200)
    def test_detect_area_never_crashes(self, query):
        from app.sources.home_assistant import _detect_area
        result = _detect_area(query)
        assert result is None or isinstance(result, str)

    @given(
        entity_id=st.text(min_size=1, max_size=100),
        state=st.text(min_size=0, max_size=50),
    )
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_is_excluded_never_crashes(self, entity_id, state):
        from app.sources.home_assistant import _is_excluded
        entity = {"entity_id": entity_id, "state": state, "attributes": {}}
        result = _is_excluded(entity)
        assert isinstance(result, bool)


class TestSnapshotDiffProperties:
    """Property: diff functions must never crash regardless of snapshot content."""

    @given(old=text_strategy, new=text_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_diff_uptime_never_crashes(self, old, new):
        from app.snapshots import _diff_uptime
        result = _diff_uptime(old, new)
        assert isinstance(result, list)

    @given(old=text_strategy, new=text_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_diff_forecast_never_crashes(self, old, new):
        from app.snapshots import _diff_forecast
        result = _diff_forecast(old, new)
        assert isinstance(result, list)

    @given(old=text_strategy, new=text_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_diff_news_never_crashes(self, old, new):
        from app.snapshots import _diff_news
        result = _diff_news(old, new)
        assert isinstance(result, list)

    @given(old=text_strategy, new=text_strategy)
    @settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
    def test_diff_ha_never_crashes_on_arbitrary_text(self, old, new):
        from app.snapshots import _diff_ha
        # _diff_ha expects JSON — arbitrary text should fail gracefully
        result = _diff_ha(old, new)
        assert isinstance(result, list)

    @given(
        entities=st.lists(
            st.fixed_dictionaries({
                "entity_id": st.text(min_size=1, max_size=50),
                "state": st.text(min_size=0, max_size=30),
                "friendly_name": st.text(min_size=0, max_size=50),
                "device_class": st.sampled_from(["", "battery", "door", "motion", "window"]),
            }),
            min_size=0,
            max_size=10,
        )
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_diff_ha_never_crashes_on_valid_structure(self, entities):
        from app.snapshots import _diff_ha
        snapshot = json.dumps(entities)
        result = _diff_ha(snapshot, snapshot)
        assert isinstance(result, list)


class TestFusionProperties:
    """Property: fusion helper functions must never crash."""

    @given(text_strategy)
    @settings(max_examples=300)
    def test_looks_empty_never_crashes(self, text):
        from app.sources.fusion import _looks_empty
        result = _looks_empty(text)
        assert isinstance(result, bool)

    @given(text_strategy, st.integers(min_value=1, max_value=10000))
    @settings(max_examples=200)
    def test_truncate_never_crashes(self, text, max_chars):
        from app.sources.fusion import _truncate
        result = _truncate(text, max_chars)
        assert isinstance(result, str)

    @given(
        st.dictionaries(
            keys=st.sampled_from(["kiwix", "web", "news", "forecast"]),
            values=text_strategy,
            min_size=0,
            max_size=4,
        )
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.too_slow])
    def test_deduplicate_never_crashes(self, results):
        from app.sources.fusion import _deduplicate
        result = _deduplicate(results)
        assert isinstance(result, dict)


class TestHistoryAggregationProperties:
    """Property tests for the History aggregation engine (Design Doc 5 §9).

    aggregate_series / compute_trend take arbitrary numeric series and must
    never crash, and their invariants (min ≤ avg ≤ max; a constant series
    is flat, never a fabricated direction) must hold for any input.
    """

    def _series(self, values):
        from app.history import Sample
        from datetime import datetime, timezone, timedelta
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        return [Sample(float(v), (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ"))
                for i, v in enumerate(values)]

    @given(st.lists(st.floats(allow_nan=False, allow_infinity=False,
                              min_value=-1e6, max_value=1e6),
                    min_size=0, max_size=200))
    @settings(max_examples=200)
    def test_aggregate_series_never_crashes(self, values):
        from app.history import aggregate_series
        result = aggregate_series(self._series(values))
        if not values:
            assert result is None
        else:
            # minimum <= maximum always holds exactly; the average can sit
            # one ULP outside [min, max] for a constant large-magnitude
            # series (total/n rounding), so allow a proportional epsilon.
            assert result.minimum <= result.maximum
            tol = abs(result.maximum) * 1e-9 + 1e-9
            assert result.minimum - tol <= result.average <= result.maximum + tol

    @given(st.lists(st.floats(allow_nan=False, allow_infinity=False,
                              min_value=-1e6, max_value=1e6),
                    min_size=0, max_size=200))
    @settings(max_examples=200)
    def test_compute_trend_never_crashes(self, values):
        from app.history import compute_trend
        t = compute_trend(self._series(values), min_samples=12, min_delta_fraction=0.1)
        assert t.status in ("rising", "falling", "flat", "insufficient")

    @given(st.floats(allow_nan=False, allow_infinity=False, min_value=-1e6, max_value=1e6),
           st.integers(min_value=12, max_value=100))
    @settings(max_examples=100)
    def test_constant_series_is_never_a_direction(self, value, n):
        from app.history import compute_trend
        t = compute_trend(self._series([value] * n), min_samples=12, min_delta_fraction=0.1)
        # A perfectly constant series must be flat — never a fabricated
        # rising/falling call.
        assert t.status == "flat"
