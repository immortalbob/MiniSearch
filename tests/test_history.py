"""
Tests for app/history.py — the History source engine (Design Doc 5).

Covers the sampler's entity filter matrix, single-transaction insert +
catalog upsert/drift, batched retention pruning, the disabled-feature
double-check and failure isolation, the pure aggregation engine
(aggregate_series / compute_trend, including the trend min-samples gate
and the per-window noise floor), the read-only events leg over
temporal_events, and the /health job report.

DB isolation: each test points app.history.HISTORY_DB (and, for the events
leg, _TEMPORAL_DB) at a throwaway file and inits it, so nothing touches a
real /app/data DB and tests never bleed state into each other.
"""
import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import app.history as H
from app.config import settings


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def temp_history_db():
    """A fresh, initialized history.db pointed at a temp file."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "history.db")
    original = H.HISTORY_DB
    H.HISTORY_DB = path
    H.init_history_db()
    yield path
    H.HISTORY_DB = original


@pytest.fixture
def temp_temporal_db():
    """A fresh temporal_patterns.db with the temporal_events table seeded
    empty — the events leg reads this read-only."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "temporal.db")
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE temporal_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT, event_type TEXT NOT NULL,
        timestamp TEXT NOT NULL, raw_detail TEXT)""")
    con.commit()
    con.close()
    original = H._TEMPORAL_DB
    H._TEMPORAL_DB = path
    yield path
    H._TEMPORAL_DB = original


# ---------------------------------------------------------------------------
# Entity filter matrix (§4 step 2)
# ---------------------------------------------------------------------------

class TestEntityKeepable:
    def _entity(self, entity_id, dc="temperature"):
        return {"entity_id": entity_id, "attributes": {"device_class": dc}}

    def test_sensor_with_allowed_device_class_is_kept(self):
        e = self._entity("sensor.office_temp", "temperature")
        assert H._entity_keepable(e, {"temperature"}, set(), set()) is True

    def test_sensor_with_disallowed_device_class_is_dropped(self):
        e = self._entity("sensor.office_temp", "temperature")
        assert H._entity_keepable(e, {"humidity"}, set(), set()) is False

    def test_non_sensor_domain_dropped_even_with_matching_class(self):
        e = self._entity("binary_sensor.motion", "temperature")
        assert H._entity_keepable(e, {"temperature"}, set(), set()) is False

    def test_explicit_allowlist_overrides_missing_class(self):
        e = {"entity_id": "sensor.custom_thing", "attributes": {}}
        assert H._entity_keepable(e, {"temperature"}, {"sensor.custom_thing"}, set()) is True

    def test_exclude_list_wins_over_class_match(self):
        e = self._entity("sensor.button_battery", "battery")
        assert H._entity_keepable(e, {"battery"}, set(), {"sensor.button_battery"}) is False

    def test_blank_entity_id_dropped(self):
        assert H._entity_keepable({"entity_id": ""}, {"temperature"}, set(), set()) is False


class TestParseFloat:
    def test_numeric_string(self):
        assert H._parse_float("72.5") == 72.5

    def test_integer_string(self):
        assert H._parse_float("700") == 700.0

    @pytest.mark.parametrize("bad", ["unavailable", "unknown", "", "on", None])
    def test_non_numeric_returns_none(self, bad):
        assert H._parse_float(bad) is None


class TestCollectHaSamples:
    def test_keeps_numeric_sensor_and_applies_area(self):
        states = [{"entity_id": "sensor.office_co2", "state": "700",
                   "attributes": {"device_class": "carbon_dioxide",
                                  "friendly_name": "Office CO2",
                                  "unit_of_measurement": "ppm"}}]
        with patch.object(settings, "history_device_classes", "carbon_dioxide"):
            samples = H._collect_ha_samples(states, {"sensor.office_co2": "office"})
        assert len(samples) == 1
        s = samples[0]
        assert s.metric_key == "sensor.office_co2"
        assert s.value == 700.0
        assert s.unit == "ppm"
        assert s.area_id == "office"
        assert s.device_class == "carbon_dioxide"

    def test_skips_unavailable_state_silently(self):
        states = [{"entity_id": "sensor.broken", "state": "unavailable",
                   "attributes": {"device_class": "temperature"}}]
        with patch.object(settings, "history_device_classes", "temperature"):
            assert H._collect_ha_samples(states, {}) == []

    def test_missing_friendly_name_falls_back_to_entity_id(self):
        states = [{"entity_id": "sensor.x", "state": "1",
                   "attributes": {"device_class": "power"}}]
        with patch.object(settings, "history_device_classes", "power"):
            samples = H._collect_ha_samples(states, {})
        assert samples[0].friendly_name == "sensor.x"


class TestParseUptimeCounts:
    def test_all_up_form(self):
        assert H._parse_uptime_counts("All 12 monitored services are up.") == (12, 12)

    def test_n_of_m_form(self):
        text = "DOWN (1): plex. 11 of 12 services are up."
        assert H._parse_uptime_counts(text) == (11, 12)

    def test_unparseable_returns_none_pair(self):
        assert H._parse_uptime_counts("some error occurred") == (None, None)

    def test_empty_returns_none_pair(self):
        assert H._parse_uptime_counts("") == (None, None)


# ---------------------------------------------------------------------------
# Storage, catalog upsert/drift, retention
# ---------------------------------------------------------------------------

class TestStoreAndFetch:
    def test_insert_and_fetch_in_window(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(now)):
            H._store_samples([H._Sample("sensor.a", 21.0, "°C", "A", "living_room", "temperature")])
        start = _iso(now - timedelta(hours=1))
        end = _iso(now + timedelta(hours=1))
        rows = H.fetch_samples("sensor.a", start, end)
        assert len(rows) == 1
        assert rows[0].value == 21.0

    def test_fetch_excludes_out_of_window(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(hours=48))):
            H._store_samples([H._Sample("sensor.a", 21.0, "", "A", "", "temperature")])
        start = _iso(now - timedelta(hours=1))
        end = _iso(now)
        assert H.fetch_samples("sensor.a", start, end) == []

    def test_catalog_upsert_refreshes_friendly_name_but_keeps_first_seen(self, temp_history_db):
        t1 = datetime.now(timezone.utc) - timedelta(hours=2)
        t2 = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(t1)):
            H._store_samples([H._Sample("sensor.a", 1.0, "", "Old Name", "", "temperature")])
        with patch.object(H, "_now_iso", return_value=_iso(t2)):
            H._store_samples([H._Sample("sensor.a", 2.0, "", "New Name", "office", "temperature")])
        cat = {c.metric_key: c for c in H.get_catalog()}
        entry = cat["sensor.a"]
        assert entry.friendly_name == "New Name"       # drift picked up
        assert entry.area_id == "office"                # drift picked up
        assert entry.first_seen == _iso(t1)             # preserved
        assert entry.last_seen == _iso(t2)              # refreshed

    def test_fetch_orders_oldest_first(self, temp_history_db):
        now = datetime.now(timezone.utc)
        for i, h in enumerate([3, 1, 2]):
            with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(hours=h))):
                H._store_samples([H._Sample("sensor.a", float(i), "", "A", "", "temperature")])
        rows = H.fetch_samples("sensor.a", _iso(now - timedelta(hours=10)), _iso(now))
        ts = [r.ts for r in rows]
        assert ts == sorted(ts)


class TestRetention:
    def test_prunes_samples_older_than_retention(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(settings, "history_retention_days", 90):
            with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(days=120))):
                H._store_samples([H._Sample("sensor.old", 1.0, "", "Old", "", "temperature")])
            with patch.object(H, "_now_iso", return_value=_iso(now)):
                H._store_samples([H._Sample("sensor.new", 2.0, "", "New", "", "temperature")])
            H._prune_retention(batch_size=1000)
        with sqlite3.connect(temp_history_db) as con:
            keys = [r[0] for r in con.execute("SELECT metric_key FROM metric_samples").fetchall()]
        assert "sensor.new" in keys
        assert "sensor.old" not in keys

    def test_prunes_catalog_rows_unseen_for_retention(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(settings, "history_retention_days", 90):
            with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(days=200))):
                H._store_samples([H._Sample("sensor.gone", 1.0, "", "Gone", "", "temperature")])
            H._prune_retention(batch_size=1000)
        keys = [c.metric_key for c in H.get_catalog()]
        assert "sensor.gone" not in keys

    def test_batched_delete_loop_removes_all_old_rows(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(settings, "history_retention_days", 1):
            old = _iso(now - timedelta(days=5))
            with patch.object(H, "_now_iso", return_value=old):
                # 25 old rows; batch_size 10 forces the LIMIT loop to iterate
                H._store_samples([H._Sample(f"sensor.k{i}", float(i), "", f"K{i}", "", "temperature")
                                  for i in range(25)])
            H._prune_retention(batch_size=10)
        with sqlite3.connect(temp_history_db) as con:
            remaining = con.execute("SELECT COUNT(*) FROM metric_samples").fetchone()[0]
        assert remaining == 0


# ---------------------------------------------------------------------------
# Ingestion — fed by the snapshot jobs (zero fetches of its own)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_area_cache():
    """Reset the module-level area cache between tests so one test's
    refresh countdown can't suppress or leak into another's."""
    H._area_of_cache = {}
    H._area_ticks_until_refresh = 0
    yield
    H._area_of_cache = {}
    H._area_ticks_until_refresh = 0


class TestIngestHaStates:
    STATES = [{"entity_id": "sensor.t", "state": "20.0",
               "attributes": {"device_class": "temperature",
                              "friendly_name": "T", "unit_of_measurement": "°C"}}]

    def test_disabled_is_a_noop(self, temp_history_db):
        with patch.object(settings, "history_enabled", False), \
             patch("app.sources.home_assistant._get_area_entities") as mock_area:
            H.ingest_ha_states(self.STATES)
            mock_area.assert_not_called()
        assert H.get_catalog() == []

    def test_ingests_from_the_handed_payload_without_fetching_states(self, temp_history_db):
        # The whole point of the piggyback: ingestion never calls
        # _get_states() itself — the payload arrives from snapshot_ha.
        with patch.object(settings, "history_enabled", True), \
             patch.object(settings, "history_device_classes", "temperature"), \
             patch("app.sources.home_assistant._get_states") as mock_states, \
             patch("app.sources.home_assistant._get_area_entities", return_value={}):
            H.ingest_ha_states(self.STATES)
            mock_states.assert_not_called()
        assert any(c.metric_key == "sensor.t" for c in H.get_catalog())

    def test_none_or_empty_states_is_safe(self, temp_history_db):
        with patch.object(settings, "history_enabled", True), \
             patch("app.sources.home_assistant._get_area_entities", return_value={}):
            H.ingest_ha_states(None)
            H.ingest_ha_states([])
        assert H.get_catalog() == []

    def test_failure_is_isolated(self, temp_history_db):
        with patch.object(settings, "history_enabled", True), \
             patch.object(H, "_collect_ha_samples", side_effect=RuntimeError("boom")):
            H.ingest_ha_states(self.STATES)  # must not propagate

    def test_area_map_cached_not_refetched_every_tick(self, temp_history_db):
        # 5 ingests, refresh cadence 12 → exactly ONE template call.
        with patch.object(settings, "history_enabled", True), \
             patch.object(settings, "history_device_classes", "temperature"), \
             patch("app.sources.home_assistant._get_area_entities",
                   return_value={"office": ["sensor.t"]}) as mock_area:
            for _ in range(5):
                H.ingest_ha_states(self.STATES)
            assert mock_area.call_count == 1
        assert H.get_catalog()[0].area_id == "office"

    def test_area_refresh_failure_keeps_stale_map(self, temp_history_db):
        with patch.object(settings, "history_enabled", True), \
             patch.object(settings, "history_device_classes", "temperature"):
            with patch("app.sources.home_assistant._get_area_entities",
                       return_value={"office": ["sensor.t"]}):
                H.ingest_ha_states(self.STATES)
            H._area_ticks_until_refresh = 0  # force a refresh attempt
            with patch("app.sources.home_assistant._get_area_entities",
                       side_effect=RuntimeError("ha down")):
                H.ingest_ha_states(self.STATES)
        # Second tick still resolved the area from the stale cache.
        assert H.get_catalog()[0].area_id == "office"


class TestIngestUptimeText:
    def test_disabled_is_a_noop(self, temp_history_db):
        with patch.object(settings, "history_enabled", False):
            H.ingest_uptime_text("All 12 monitored services are up.")
        assert H.get_catalog() == []

    def test_all_up_text_stores_pair(self, temp_history_db):
        with patch.object(settings, "history_enabled", True):
            H.ingest_uptime_text("All 12 monitored services are up.")
        keys = {c.metric_key for c in H.get_catalog()}
        assert keys == {"uptime.services_up", "uptime.services_total"}

    def test_unparseable_text_stores_nothing(self, temp_history_db):
        with patch.object(settings, "history_enabled", True):
            H.ingest_uptime_text("some transient error")
            H.ingest_uptime_text("")
        assert H.get_catalog() == []


# ---------------------------------------------------------------------------
# Aggregation engine — pure functions
# ---------------------------------------------------------------------------

def _series(values, base=None):
    base = base or datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    return [H.Sample(v, _iso(base + timedelta(hours=i))) for i, v in enumerate(values)]


class TestAggregateSeries:
    def test_empty_returns_none(self):
        assert H.aggregate_series([]) is None

    def test_min_max_avg_latest(self):
        s = H.aggregate_series(_series([10, 30, 20]))
        assert s.minimum == 10
        assert s.maximum == 30
        assert s.average == 20
        assert s.latest == 20
        assert s.count == 3

    def test_extreme_timestamps_point_at_the_right_samples(self):
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        s = H.aggregate_series(_series([5, 1, 9], base=base))
        assert s.min_ts == _iso(base + timedelta(hours=1))  # the 1
        assert s.max_ts == _iso(base + timedelta(hours=2))  # the 9

    def test_value_range(self):
        s = H.aggregate_series(_series([4, 8, 6]))
        assert s.value_range == 4

    def test_single_sample(self):
        s = H.aggregate_series(_series([42]))
        assert s.minimum == s.maximum == s.average == s.latest == 42


class TestComputeTrend:
    def test_rising_series_is_rising(self):
        s = _series(list(range(0, 60, 4)))  # 15 samples, steadily up
        t = H.compute_trend(s, min_samples=12, min_delta_fraction=0.1)
        assert t.status == "rising"
        assert t.slope_per_day > 0

    def test_falling_series_is_falling(self):
        s = _series(list(range(60, 0, -4)))
        t = H.compute_trend(s, min_samples=12, min_delta_fraction=0.1)
        assert t.status == "falling"
        assert t.slope_per_day < 0

    def test_constant_series_is_flat(self):
        s = _series([50] * 15)
        t = H.compute_trend(s, min_samples=12, min_delta_fraction=0.1)
        assert t.status == "flat"

    def test_below_min_samples_is_insufficient(self):
        s = _series([1, 2, 3, 4, 5])  # only 5
        t = H.compute_trend(s, min_samples=12, min_delta_fraction=0.1)
        assert t.status == "insufficient"

    def test_tiny_wobble_below_noise_floor_is_flat(self):
        # 15 samples that jitter within a small band but with a huge range
        # elsewhere: a barely-there slope shouldn't clear the noise floor.
        vals = [100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100, 101, 100]
        t = H.compute_trend(_series(vals), min_samples=12, min_delta_fraction=0.5)
        assert t.status == "flat"

    def test_reports_first_and_last_values(self):
        s = _series([10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130])
        t = H.compute_trend(s, min_samples=12, min_delta_fraction=0.1)
        assert t.first_value == 10
        assert t.last_value == 130


class TestFormatValue:
    def test_large_value_is_integer_with_separator(self):
        assert H.format_value(1104.0, "ppm") == "1,104 ppm"

    def test_small_value_one_decimal(self):
        assert H.format_value(72.53, "°F") == "72.5 °F"

    def test_whole_number_no_decimal(self):
        assert H.format_value(688.0, "ppm") == "688 ppm"

    def test_no_unit(self):
        assert H.format_value(5.0) == "5"


# ---------------------------------------------------------------------------
# Events leg
# ---------------------------------------------------------------------------

class TestQueryIsEvent:
    @pytest.mark.parametrize("q", [
        "how many times did the door open today",
        "how often did the internet go down",
        "how many outages this week",
        "any motion in the hallway last night",
    ])
    def test_event_queries_detected(self, q):
        assert H.query_is_event(q) is True

    @pytest.mark.parametrize("q", [
        "average temperature today",
        "how cold did it get last night",
        "office co2 trend",
    ])
    def test_metric_queries_not_events(self, q):
        assert H.query_is_event(q) is False


class TestCountEvents:
    def _seed(self, path, rows):
        con = sqlite3.connect(path)
        con.executemany(
            "INSERT INTO temporal_events (source, event_type, timestamp, raw_detail) "
            "VALUES (?,?,?,?)", rows)
        con.commit()
        con.close()

    def test_counts_matching_events_and_reports_last(self, temp_temporal_db):
        now = datetime.now(timezone.utc)
        self._seed(temp_temporal_db, [
            ("ha", "binary_sensor.front_door:opened", _iso(now - timedelta(hours=3)), ""),
            ("ha", "binary_sensor.front_door:opened", _iso(now - timedelta(hours=1)), ""),
        ])
        r = H.count_events("how many times did the door open today",
                           _iso(now - timedelta(hours=24)), _iso(now))
        assert r.matched is True
        assert r.count == 2
        assert r.last_ts == _iso(now - timedelta(hours=1))

    def test_entity_hint_narrows_to_the_right_sensor(self, temp_temporal_db):
        now = datetime.now(timezone.utc)
        self._seed(temp_temporal_db, [
            ("ha", "binary_sensor.front_door:opened", _iso(now - timedelta(hours=2)), ""),
            ("ha", "binary_sensor.back_door:opened", _iso(now - timedelta(hours=2)), ""),
        ])
        r = H.count_events("door open", _iso(now - timedelta(hours=24)), _iso(now),
                           entity_hint="front")
        assert r.count == 1

    def test_no_event_verb_returns_unmatched(self, temp_temporal_db):
        now = datetime.now(timezone.utc)
        r = H.count_events("how many samples", _iso(now - timedelta(hours=24)), _iso(now))
        assert r.matched is False

    def test_outage_events_matched(self, temp_temporal_db):
        now = datetime.now(timezone.utc)
        self._seed(temp_temporal_db, [
            ("uptime", "uptime:outage", _iso(now - timedelta(hours=5)), ""),
        ])
        r = H.count_events("how often did it go down today",
                           _iso(now - timedelta(hours=24)), _iso(now))
        assert r.matched is True
        assert r.count == 1
        assert r.noun == "service outage"


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

class TestHistoryJobHealth:
    def test_disabled_reports_disabled(self):
        with patch.object(settings, "history_enabled", False):
            assert H.get_history_job_health() == {"status": "disabled"}

    def test_never_ran_when_no_samples(self, temp_history_db):
        with patch.object(settings, "history_enabled", True):
            h = H.get_history_job_health()
        assert h["status"] == "never_ran"
        assert h["metrics_tracked"] == 0

    def test_ok_when_fresh(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(now)):
            H._store_samples([H._Sample("sensor.a", 1.0, "", "A", "", "temperature")])
        with patch.object(settings, "history_enabled", True), \
             patch.dict("app.snapshots.JOB_INTERVALS_MINUTES", {"ha": 5}), \
             patch.object(settings, "history_stale_grace_multiplier", 3):
            h = H.get_history_job_health()
        assert h["status"] == "ok"
        assert h["metrics_tracked"] == 1
        assert h["samples_24h"] == 1

    def test_stale_when_last_sample_old(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(hours=2))):
            H._store_samples([H._Sample("sensor.a", 1.0, "", "A", "", "temperature")])
        with patch.object(settings, "history_enabled", True), \
             patch.dict("app.snapshots.JOB_INTERVALS_MINUTES", {"ha": 5}), \
             patch.object(settings, "history_stale_grace_multiplier", 3):
            h = H.get_history_job_health()
        assert h["status"] == "stale"

    def test_quiet_sensor_counted(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(hours=6))):
            H._store_samples([H._Sample("sensor.quiet", 1.0, "", "Quiet", "", "temperature")])
        with patch.object(settings, "history_enabled", True), \
             patch.dict("app.snapshots.JOB_INTERVALS_MINUTES", {"ha": 5}), \
             patch.object(settings, "history_stale_grace_multiplier", 3):
            h = H.get_history_job_health()
        assert h["quiet_sensors"] == 1


class TestMetricsOverview:
    def test_overview_includes_counts_and_coverage(self, temp_history_db):
        now = datetime.now(timezone.utc)
        with patch.object(H, "_now_iso", return_value=_iso(now - timedelta(hours=2))):
            H._store_samples([H._Sample("sensor.a", 1.0, "ppm", "A", "office", "carbon_dioxide")])
        with patch.object(H, "_now_iso", return_value=_iso(now)):
            H._store_samples([H._Sample("sensor.a", 2.0, "ppm", "A", "office", "carbon_dioxide")])
        ov = H.get_metrics_overview()
        assert ov["metric_count"] == 1
        m = ov["metrics"][0]
        assert m["samples"] == 2
        assert m["oldest_sample"] is not None
        assert m["newest_sample"] is not None


class TestAuditRegressions:
    """Pins for the post-implementation audit findings (v3.56.0)."""

    # --- NaN/inf poisoning ---
    @pytest.mark.parametrize("bad", ["nan", "NaN", "inf", "-inf", "1e999"])
    def test_non_finite_states_rejected(self, bad):
        assert H._parse_float(bad) is None

    # --- event predicate word boundaries ---
    def test_clock_does_not_match_lock(self):
        assert H._event_predicate("how many times did the clock chime") is None

    def test_blocked_does_not_match_lock(self):
        assert H._event_predicate("was the driveway blocked today") is None

    def test_unlocked_still_matches_unlock_not_lock(self):
        assert H._event_predicate("how many times was the door unlocked")[0] == ":unlocked"

    # --- metric-class guard on the events leg ---
    def test_co2_gone_down_is_a_metric_question(self):
        assert H.query_is_event("has the co2 gone down today") is False

    def test_internet_gone_down_is_an_event_question(self):
        assert H.query_is_event("has the internet gone down today") is True
        assert H._event_predicate("has the internet gone down today")[0] == ":outage"

    def test_how_many_degrees_is_a_metric_question(self):
        # bare "how many" was removed from the count phrases
        assert H.query_is_event("how many degrees was it last night") is False

    def test_battery_alert_count_is_still_an_event_question(self):
        # the one deliberate exception to the metric-class guard
        assert H.query_is_event("how many times did we get a low battery alert") is True

    def test_temperature_go_down_is_a_metric_question(self):
        assert H.query_is_event("did the temperature go down today") is False


class TestSnapshotHooks:
    """snapshot_ha/snapshot_uptime hand their payloads to history —
    gated, isolated, and with no fetch added by history itself."""

    def test_snapshot_ha_hands_states_to_history_when_enabled(self, temp_history_db):
        from app import snapshots
        states = [{"entity_id": "sensor.t", "state": "20.0",
                   "attributes": {"device_class": "temperature",
                                  "friendly_name": "T", "unit_of_measurement": "°C"}}]
        with patch.object(settings, "history_enabled", True), \
             patch.object(settings, "history_device_classes", "temperature"), \
             patch("app.sources.home_assistant._get_states", return_value=states) as mock_states, \
             patch("app.sources.home_assistant._get_area_entities", return_value={}), \
             patch.object(snapshots, "_store_snapshot"):
            snapshots.snapshot_ha()
            # ONE states fetch total — snapshot's own. History added none.
            assert mock_states.call_count == 1
        assert any(c.metric_key == "sensor.t" for c in H.get_catalog())

    def test_snapshot_ha_skips_history_when_disabled(self, temp_history_db):
        from app import snapshots
        with patch.object(settings, "history_enabled", False), \
             patch("app.sources.home_assistant._get_states", return_value=[]), \
             patch.object(snapshots, "_store_snapshot"), \
             patch.object(H, "ingest_ha_states") as mock_ingest:
            snapshots.snapshot_ha()
            mock_ingest.assert_not_called()

    def test_history_failure_cannot_fail_the_snapshot(self, temp_history_db):
        from app import snapshots
        with patch.object(settings, "history_enabled", True), \
             patch("app.sources.home_assistant._get_states", return_value=[]), \
             patch.object(snapshots, "_store_snapshot") as mock_store, \
             patch.object(H, "ingest_ha_states", side_effect=RuntimeError("boom")):
            snapshots.snapshot_ha()  # must not raise
            mock_store.assert_called_once()  # snapshot itself completed

    def test_snapshot_uptime_hands_text_to_history(self, temp_history_db):
        from app import snapshots
        with patch.object(settings, "history_enabled", True), \
             patch("app.sources.uptime_kuma.search",
                   return_value="All 12 monitored services are up.") as mock_search, \
             patch.object(snapshots, "_store_snapshot"):
            snapshots.snapshot_uptime()
            # ONE uptime fetch total — the snapshot's own live call is the
            # only one; history parsed the returned text.
            assert mock_search.call_count == 1
        keys = {c.metric_key for c in H.get_catalog()}
        assert "uptime.services_up" in keys
