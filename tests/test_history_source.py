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
