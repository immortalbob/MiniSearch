"""
Tests for app/sources/history.py — the routable History adapter (Design
Doc 5 §5).

Covers the three deterministic resolutions (class-word detection,
aggregation keyword bucketing, metric resolution priority) and the
assembled response shapes: the summary line, coverage disclosure when the
window exceeds recorded history, the honest "couldn't identify" / "no
readings" messages, the events leg, and the HISTORY_ENABLED gate.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import app.history as H
import app.sources.history as HS
from app.config import settings


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def enabled():
    with patch.object(settings, "history_enabled", True):
        yield


@pytest.fixture
def temp_history_db():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "history.db")
    original = H.HISTORY_DB
    H.HISTORY_DB = path
    H.init_history_db()
    yield path
    H.HISTORY_DB = original


@pytest.fixture
def temp_temporal_db():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "temporal.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE temporal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT,
        event_type TEXT NOT NULL, timestamp TEXT NOT NULL, raw_detail TEXT)""")
    con.commit()
    con.close()
    original = H._TEMPORAL_DB
    H._TEMPORAL_DB = path
    yield path
    H._TEMPORAL_DB = original


def _seed(db, samples, when=None):
    """samples: list of (metric_key, value, unit, friendly, area, dclass)."""
    when = when or datetime.now(timezone.utc)
    with patch.object(H, "_now_iso", return_value=_iso(when)):
        H._store_samples([H._Sample(*s) for s in samples])


# ---------------------------------------------------------------------------
# Deterministic resolution units
# ---------------------------------------------------------------------------

class TestDetectClass:
    def test_co2(self):
        assert HS._detect_class("office co2 today") == "carbon_dioxide"

    def test_carbon_dioxide_longest_first(self):
        # "carbon dioxide" must win over the shorter "carbon"
        assert HS._detect_class("carbon dioxide levels") == "carbon_dioxide"

    def test_temp_abbrev(self):
        assert HS._detect_class("what's the temp") == "temperature"

    def test_no_class(self):
        assert HS._detect_class("how loud was it") is None


class TestResolveAggregation:
    @pytest.mark.parametrize("q,expected", [
        ("how cold did it get", "min"),
        ("lowest reading", "min"),
        ("how hot did it get", "max"),
        ("peak power", "max"),
        ("average temperature", "avg"),
        ("typical humidity", "avg"),
        ("has it been rising", "trend"),
        ("co2 trend", "trend"),
        ("office co2 today", "summary"),
    ])
    def test_buckets(self, q, expected):
        assert HS._resolve_aggregation(q) == expected

    def test_trend_beats_extreme(self):
        # "rising to a peak" — trend is checked first
        assert HS._resolve_aggregation("has it been rising to a peak") == "trend"


class TestResolveMetric:
    def _cat(self, *entries):
        # entries: (key, friendly, area, dclass)
        return [H.CatalogEntry(k, fn, a, dc, "", "", "") for (k, fn, a, dc) in entries]

    def test_friendly_name_match(self):
        cat = self._cat(("sensor.a", "Office CO2", "office", "carbon_dioxide"))
        r = HS._resolve_metric("what's the office co2 now", cat)
        assert r.metric.metric_key == "sensor.a"

    def test_friendly_name_longest_first(self):
        cat = self._cat(("sensor.a", "Temperature", "", "temperature"),
                        ("sensor.b", "Living Room Temperature", "living_room", "temperature"))
        r = HS._resolve_metric("how warm is the living room temperature", cat)
        assert r.metric.metric_key == "sensor.b"

    def test_area_plus_class_combo(self):
        with patch("app.sources.home_assistant._detect_area", return_value="office"):
            cat = self._cat(("sensor.a", "A", "office", "carbon_dioxide"),
                            ("sensor.b", "B", "living_room", "carbon_dioxide"))
            r = HS._resolve_metric("office co2", cat)
        assert r.metric.metric_key == "sensor.a"

    def test_bare_class_single_candidate(self):
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            cat = self._cat(("sensor.a", "A", "", "humidity"))
            r = HS._resolve_metric("what's the humidity", cat)
        assert r.metric.metric_key == "sensor.a"

    def test_bare_class_multiple_candidates_is_ambiguous(self):
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            cat = self._cat(("sensor.a", "A", "office", "temperature"),
                            ("sensor.b", "B", "bedroom", "temperature"))
            r = HS._resolve_metric("what's the temperature", cat)
        assert r.metric is None
        assert len(r.candidates) == 2

    def test_unresolved(self):
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            cat = self._cat(("sensor.a", "A", "office", "temperature"))
            r = HS._resolve_metric("how loud was it", cat)
        assert r.metric is None
        assert r.candidates == []


# ---------------------------------------------------------------------------
# Assembled responses — metric leg
# ---------------------------------------------------------------------------

class TestSearchMetric:
    def test_summary_shape_and_unit(self, enabled, temp_history_db):
        now = datetime.now(timezone.utc)
        for i in range(15):
            _seed(temp_history_db,
                  [("sensor.office_co2", 500 + i, "ppm", "Office CO2", "office", "carbon_dioxide")],
                  when=now - timedelta(hours=15 - i))
        out = HS.search("office co2 today")
        assert "Office CO2" in out
        assert "Low" in out and "High" in out and "Average" in out and "Now" in out
        assert "ppm" in out

    def test_coverage_disclosure_when_window_exceeds_history(self, enabled, temp_history_db):
        now = datetime.now(timezone.utc)
        # Only 3h of data, but ask for the week
        for i in range(6):
            _seed(temp_history_db,
                  [("sensor.hum", 40 + i, "%", "Hallway Humidity", "hallway", "humidity")],
                  when=now - timedelta(hours=3) + timedelta(minutes=30 * i))
        out = HS.search("hallway humidity this week")
        assert "only the past" in out and "recorded data" in out

    def test_no_samples_in_window_is_honest(self, enabled, temp_history_db):
        now = datetime.now(timezone.utc)
        _seed(temp_history_db,
              [("sensor.p", 1000, "Pa", "Barometer", "office", "pressure")],
              when=now - timedelta(days=10))
        out = HS.search("barometer today")
        assert "No Barometer readings recorded in" in out

    def test_unresolved_lists_recorded_metrics(self, enabled, temp_history_db):
        _seed(temp_history_db,
              [("sensor.a", 1, "ppm", "Office CO2", "office", "carbon_dioxide")])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            out = HS.search("how loud was it today")
        assert "couldn't identify" in out.lower()
        assert "Office CO2" in out

    def test_empty_catalog_message(self, enabled, temp_history_db):
        out = HS.search("office co2 today")
        assert "hasn't captured" in out or "No recorded metrics" in out

    def test_explicit_trend_line_present(self, enabled, temp_history_db):
        now = datetime.now(timezone.utc)
        for i in range(15):
            _seed(temp_history_db,
                  [("sensor.co2", 400 + i * 20, "ppm", "CO2", "office", "carbon_dioxide")],
                  when=now - timedelta(hours=15 - i))
        out = HS.search("has the co2 been rising today")
        assert "Trend:" in out


# ---------------------------------------------------------------------------
# Assembled responses — events leg
# ---------------------------------------------------------------------------

class TestSearchEvents:
    def _seed_events(self, path, rows):
        con = sqlite3.connect(path)
        con.executemany(
            "INSERT INTO temporal_events (source, event_type, timestamp, raw_detail) "
            "VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_count_response(self, enabled, temp_temporal_db):
        now = datetime.now(timezone.utc)
        self._seed_events(temp_temporal_db, [
            ("ha", "binary_sensor.front_door:opened", _iso(now - timedelta(hours=2)), ""),
            ("ha", "binary_sensor.front_door:opened", _iso(now - timedelta(hours=1)), ""),
        ])
        with patch.object(settings, "temporal_pattern_detection_enabled", True):
            out = HS.search("how many times did the door open today")
        assert out.startswith("2 openings")
        assert "most recently" in out

    def test_disabled_temporal_message(self, enabled, temp_temporal_db):
        with patch.object(settings, "temporal_pattern_detection_enabled", False):
            out = HS.search("how many times did the door open today")
        assert "temporal pattern detection" in out.lower()

    def test_no_matching_event_verb(self, enabled, temp_temporal_db):
        with patch.object(settings, "temporal_pattern_detection_enabled", True):
            # count phrasing routes to events, but no event verb resolves
            out = HS.search("how many times today")
        assert "couldn't identify which kind of event" in out.lower()

    def test_zero_events_reads_naturally(self, enabled, temp_temporal_db):
        with patch.object(settings, "temporal_pattern_detection_enabled", True):
            out = HS.search("how many times did the door open today")
        assert out.startswith("No openings recorded")


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

class TestSearchDisabled:
    def test_disabled_returns_disabled_message(self):
        with patch.object(settings, "history_enabled", False):
            out = HS.search("office co2 today")
        assert "disabled" in out.lower()


class TestWindowPhrase:
    def test_past_labels_get_in_the(self):
        assert HS._window_phrase("past 7 days") == "in the past 7 days"

    def test_adverbial_labels_stay_bare(self):
        assert HS._window_phrase("today") == "today"
        assert HS._window_phrase("last night") == "last night"


class TestCosmeticsAuditRegressions:
    def test_humanize_under_an_hour(self):
        assert HS._humanize_hours(0.3) == "under an hour"

    def test_humanize_singular_hour(self):
        assert HS._humanize_hours(1.2) == "1 hour"

    def test_single_sample_header_is_singular(self, enabled, temp_history_db):
        _seed(temp_history_db, [("sensor.x", 11.0, "", "Services up", "uptime", "")])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            out = HS.search("services up today")
        assert "(1 sample)" in out


class TestFieldFindingsFirstDeployment:
    """Regression pins for the five issues found on the first real
    deployment (MiniDock, v3.56.0 soak) — behaviors no test pinned
    because no test data had a device named after a class word, a
    partially-covered area, or a window crossing local midnight."""

    def test_cold_and_hot_imply_temperature(self):
        from app.history import _detect_class
        assert _detect_class("how cold did it get last night") == "temperature"
        assert _detect_class("how hot did it get today") == "temperature"

    def test_class_detection_is_word_bounded(self):
        from app.history import _detect_class
        assert _detect_class("show me my photos from today") is None      # not "hot"
        assert _detect_class("how many attempts were made") is None       # not "temp"

    def test_sensor_named_after_class_word_does_not_hijack(self, enabled, temp_history_db):
        # A device literally named "Temperature" plus other temperature
        # sensors: "average temperature today" must ASK, not silently
        # pick the name-collision sensor.
        _seed(temp_history_db, [
            ("sensor.mystery", 99.9, "°F", "Temperature", "", "temperature"),
            ("sensor.lr", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            out = HS.search("average temperature today")
        assert "which did you mean" in out
        assert "99.9" not in out

    def test_named_sensor_still_matches_when_name_is_not_a_class_word(self, enabled, temp_history_db):
        _seed(temp_history_db, [
            ("sensor.lr", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
            ("sensor.cpu", 99.9, "°F", "CPU Temp", "", "temperature"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            out = HS.search("cpu temp today")
        assert "CPU Temp" in out and "99.9" in out

    def test_area_substitution_is_disclosed(self, enabled, temp_history_db):
        # "office co2" with no office CO2 sensor and exactly one CO2
        # sensor anywhere: answer it, but say so.
        _seed(temp_history_db, [
            ("sensor.lr_co2", 451.0, "ppm", "LivingRoom CO2", "living_room", "carbon_dioxide"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value="office"):
            out = HS.search("office co2 today")
        assert "No carbon dioxide sensor recorded in office" in out
        assert "LivingRoom CO2" in out and "451" in out

    def test_area_substitution_with_several_candidates_asks(self, enabled, temp_history_db):
        _seed(temp_history_db, [
            ("sensor.lr_co2", 451.0, "ppm", "LivingRoom CO2", "living_room", "carbon_dioxide"),
            ("sensor.br_co2", 520.0, "ppm", "Bedroom CO2", "bedroom", "carbon_dioxide"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value="office"):
            out = HS.search("office co2 today")
        assert "which did you mean" in out

    def test_sub_hour_coverage_grammar(self, enabled, temp_history_db):
        _seed(temp_history_db, [
            ("sensor.lr", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
        ], when=datetime.now(timezone.utc) - timedelta(minutes=5))
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            out = HS.search("living room temperature this week")
        assert "only under an hour of recorded data" in out
        assert "the past under an hour" not in out

    def test_rolling_window_crossing_midnight_gets_dated_times(self):
        # "today" is the pinned rolling-24h changes semantic; at any time
        # other than just-past-midnight it crosses local midnight, so a
        # bare "5:25 PM" could be yesterday (in the field, it was).
        with patch.object(settings, "local_timezone", "UTC"):
            assert HS._window_dated("2026-07-04T14:00:00Z", "2026-07-05T14:00:00Z") is True

    def test_bounded_single_day_window_stays_undated(self):
        # "yesterday" (00:00 -> 24:00 local) is one calendar day; times
        # inside it need no date. The end-boundary midnight must not
        # count as a second day.
        with patch.object(settings, "local_timezone", "UTC"):
            assert HS._window_dated("2026-07-04T00:00:00Z", "2026-07-05T00:00:00Z") is False

    def test_class_word_named_candidate_is_area_qualified_in_ask(self, enabled, temp_history_db):
        # Field finding: an outdoor sensor named just "Temperature"
        # appeared in the ambiguity ask as an option that its own name
        # could never select. The ask must teach the working phrasing.
        _seed(temp_history_db, [
            ("sensor.cotech", 99.9, "°F", "Temperature", "outside", "temperature"),
            ("sensor.lr", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            out = HS.search("average temperature today")
        assert "Temperature (outside)" in out
        assert "Living Room Temperature" in out

    def test_outdoor_sensor_resolves_via_area_class(self, enabled, temp_history_db):
        _seed(temp_history_db, [
            ("sensor.cotech", 99.9, "°F", "Temperature", "outside", "temperature"),
            ("sensor.lr", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value="outside"):
            out = HS.search("how cold did it get outside today")
        assert "99.9" in out and "which did you mean" not in out

    def test_abstention_messages_are_looks_empty_for_fusion(self, enabled, temp_history_db):
        # Field finding: LLM fusion routing pulled `history` in for
        # "latest AI trends" / "history of ancient rome" (the source's
        # own name attracts it) and fused the plain-prose abstention in
        # as a trailing noise block. The canonical _looks_empty filter
        # must recognize all five abstentions — and must NOT catch a
        # real answer, whose header's `**` is the structural guard.
        from app.sources.fusion import _looks_empty
        # Empty catalog -> "No recorded metrics yet" abstention.
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            assert _looks_empty(HS.search("what are the latest AI trends today")) is True

        _seed(temp_history_db, [
            ("sensor.lr", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            # Populated catalog, unresolvable query -> "couldn't identify".
            assert _looks_empty(HS.search("what are the latest AI trends today")) is True
            # A real answer must survive.
            assert _looks_empty(HS.search("living room temperature today")) is False

    def test_ambiguity_ask_is_looks_empty_for_fusion(self, enabled, temp_history_db):
        from app.sources.fusion import _looks_empty
        _seed(temp_history_db, [
            ("sensor.a", 74.0, "°F", "Living Room Temperature", "living_room", "temperature"),
            ("sensor.b", 99.9, "°F", "Temperature", "outside", "temperature"),
        ])
        with patch("app.sources.home_assistant._detect_area", return_value=None):
            ask = HS.search("average temperature today")
        assert "which did you mean" in ask
        assert _looks_empty(ask) is True

    def test_event_count_answer_is_not_looks_empty(self, enabled, temp_temporal_db):
        # "1 opening today, most recently …" is plain prose with no `**`;
        # it must survive the new phrases. Same for the honest zero.
        from app.sources.fusion import _looks_empty
        now = datetime.now(timezone.utc)
        con = sqlite3.connect(temp_temporal_db)
        con.execute("INSERT INTO temporal_events (source, event_type, timestamp, raw_detail) "
                    "VALUES (?,?,?,?)",
                    ("ha", "binary_sensor.front_door:opened", _iso(now - timedelta(hours=1)), ""))
        con.commit()
        con.close()
        with patch.object(settings, "temporal_pattern_detection_enabled", True):
            out = HS.search("how many times did the front door open today")
        assert out.startswith("1 opening")
        assert _looks_empty(out) is False
