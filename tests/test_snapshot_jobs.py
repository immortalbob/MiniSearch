"""
Tests for app/snapshots.py scheduled job functions —
snapshot_uptime, snapshot_forecast, snapshot_news, snapshot_ha.

These are the functions APScheduler actually calls on a timer. Each wraps
a source module's search() and stores the result. snapshot_ha is more
complex — it bypasses the home_assistant source module entirely and
queries /api/states directly, applying its own entity relevance filter.
"""
import pytest
import json
import tempfile
import os
from unittest.mock import patch, MagicMock
import requests as req


@pytest.fixture
def temp_snapshot_db():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name
    with patch("app.snapshots.SNAPSHOT_DB", temp_db):
        from app.snapshots import init_snapshot_db
        init_snapshot_db()
        yield temp_db
    os.unlink(temp_db)


class TestSnapshotUptime:
    """Tests for snapshot_uptime() job wrapper."""

    def test_stores_result_from_uptime_search(self, temp_snapshot_db):
        from app.snapshots import snapshot_uptime, _get_last_snapshots
        with patch("app.sources.uptime_kuma.search", return_value="All 15 services are up."):
            snapshot_uptime()
        snapshots = _get_last_snapshots("uptime", limit=1)
        assert snapshots == ["All 15 services are up."]

    def test_does_not_raise_on_search_failure(self, temp_snapshot_db):
        from app.snapshots import snapshot_uptime
        with patch("app.sources.uptime_kuma.search", side_effect=Exception("connection refused")):
            snapshot_uptime()  # should not raise

    def test_does_not_store_anything_on_failure(self, temp_snapshot_db):
        from app.snapshots import snapshot_uptime, _get_last_snapshots
        with patch("app.sources.uptime_kuma.search", side_effect=Exception("boom")):
            snapshot_uptime()
        snapshots = _get_last_snapshots("uptime", limit=1)
        assert snapshots == []


class TestSnapshotForecast:
    """Tests for snapshot_forecast() job wrapper."""

    def test_stores_result_from_forecast_search(self, temp_snapshot_db):
        from app.snapshots import snapshot_forecast, _get_last_snapshots
        with patch("app.sources.forecast.search", return_value="Today will be sunny."):
            snapshot_forecast()
        snapshots = _get_last_snapshots("forecast", limit=1)
        assert snapshots == ["Today will be sunny."]

    def test_does_not_raise_on_search_failure(self, temp_snapshot_db):
        from app.snapshots import snapshot_forecast
        with patch("app.sources.forecast.search", side_effect=Exception("timeout")):
            snapshot_forecast()


class TestSnapshotNews:
    """Tests for snapshot_news() job wrapper."""

    def test_stores_result_from_news_search(self, temp_snapshot_db):
        from app.snapshots import snapshot_news, _get_last_snapshots
        with patch("app.sources.freshrss.search", return_value="**Headline** (World)\nContent."):
            snapshot_news()
        snapshots = _get_last_snapshots("news", limit=1)
        assert snapshots == ["**Headline** (World)\nContent."]

    def test_does_not_raise_on_search_failure(self, temp_snapshot_db):
        from app.snapshots import snapshot_news
        with patch("app.sources.freshrss.search", side_effect=Exception("auth failed")):
            snapshot_news()


class TestSnapshotHA:
    """Tests for snapshot_ha() — direct HA API capture with entity filtering."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.ha_url
        self._orig_token = settings.ha_token
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = self._orig_url
        settings.ha_token = self._orig_token

    def _mock_states_response(self, states):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = states
        resp.raise_for_status.return_value = None
        return resp

    def test_skips_when_not_configured(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        from app.config import settings
        settings.ha_url = ""
        snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        assert snapshots == []

    def test_captures_lock_entities(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert len(stored) == 1
        assert stored[0]["entity_id"] == "lock.front_door"

    def test_captures_relevant_binary_sensors(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "binary_sensor.front_door", "state": "off",
             "attributes": {"device_class": "door", "friendly_name": "Front Door"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert len(stored) == 1

    def test_excludes_irrelevant_binary_sensors(self, temp_snapshot_db):
        """The bug we caught in production — kiosk/dark-mode toggles shouldn't be captured."""
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "binary_sensor.dark_mode", "state": "on",
             "attributes": {"device_class": "", "friendly_name": "Dark Mode"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert stored == []

    def test_captures_battery_sensors(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "sensor.phone_battery", "state": "85",
             "attributes": {"device_class": "battery", "friendly_name": "Phone Battery"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert len(stored) == 1
        assert stored[0]["device_class"] == "battery"

    def test_excludes_lights_and_switches(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "light.bedroom", "state": "on", "attributes": {"friendly_name": "Bedroom Light"}},
            {"entity_id": "switch.fan", "state": "off", "attributes": {"friendly_name": "Fan"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert stored == []

    def test_stores_friendly_name_and_device_class(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "lock.back_door", "state": "locked",
             "attributes": {"friendly_name": "Back Door"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert stored[0]["friendly_name"] == "Back Door"
        assert stored[0]["device_class"] == ""

    def test_does_not_raise_on_connection_error(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha
        with patch("app.sources.home_assistant._session.get", side_effect=req.exceptions.ConnectionError()):
            snapshot_ha()  # should not raise

    def test_does_not_raise_on_http_error(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.exceptions.HTTPError("401")
        with patch("app.sources.home_assistant._session.get", return_value=mock_resp):
            snapshot_ha()  # should not raise

    def test_mixed_relevant_and_irrelevant_entities(self, temp_snapshot_db):
        from app.snapshots import snapshot_ha, _get_last_snapshots
        states = [
            {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
            {"entity_id": "light.bedroom", "state": "on", "attributes": {"friendly_name": "Bedroom"}},
            {"entity_id": "sensor.battery1", "state": "50",
             "attributes": {"device_class": "battery", "friendly_name": "Battery 1"}},
            {"entity_id": "binary_sensor.dark_mode", "state": "on", "attributes": {"friendly_name": "Dark Mode"}},
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states_response(states)):
            snapshot_ha()
        snapshots = _get_last_snapshots("ha", limit=1)
        stored = json.loads(snapshots[0])
        assert len(stored) == 2  # only lock and battery sensor
        entity_ids = {e["entity_id"] for e in stored}
        assert "lock.front_door" in entity_ids
        assert "sensor.battery1" in entity_ids
        assert "light.bedroom" not in entity_ids
        assert "binary_sensor.dark_mode" not in entity_ids
