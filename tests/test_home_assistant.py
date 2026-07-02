"""
Tests for app/sources/home_assistant.py
Uses unittest.mock to avoid real network calls.
"""
from unittest.mock import patch, MagicMock


def _make_entity(entity_id: str, state: str, device_class: str = "", friendly_name: str = "", unit: str = "") -> dict:
    """Helper to build a minimal HA entity dict."""
    attrs = {}
    if device_class:
        attrs["device_class"] = device_class
    if friendly_name:
        attrs["friendly_name"] = friendly_name
    if unit:
        attrs["unit_of_measurement"] = unit
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def _mock_states(entities: list) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = entities
    mock_resp.raise_for_status.return_value = None
    return mock_resp


SAMPLE_STATES = [
    _make_entity("light.living_room", "on", friendly_name="Living Room"),
    _make_entity("light.bedroom", "off", friendly_name="Bedroom"),
    _make_entity("light.tv_backlight_segment_1", "on", friendly_name="TV Segment 1"),
    _make_entity("lock.front_door", "locked", friendly_name="Front Door"),
    _make_entity("lock.back_door", "locked", friendly_name="Back Door"),
    _make_entity("binary_sensor.front_door_door", "off", device_class="door", friendly_name="Front Door"),
    _make_entity("binary_sensor.back_door_motion", "off", device_class="motion", friendly_name="Back Door Motion"),
    _make_entity("sensor.room_temperature", "72.5", device_class="temperature", friendly_name="Room Temperature", unit="°F"),
    _make_entity("sensor.room_humidity", "45.2", device_class="humidity", friendly_name="Room Humidity", unit="%"),
    _make_entity("sensor.room_co2", "650", device_class="carbon_dioxide", friendly_name="Room CO2", unit="ppm"),
    _make_entity("sensor.cotech_temperature", "98.6", device_class="temperature", friendly_name="Outdoor Temp", unit="°F"),
    _make_entity("sensor.phone_battery", "85", device_class="battery", friendly_name="Phone Battery", unit="%"),
    _make_entity("sensor.camera_battery", "100", device_class="battery", friendly_name="Camera Battery", unit="%"),
    _make_entity("event.front_yard_motion", "2026-06-17T18:00:00+00:00", friendly_name="Front Yard Motion"),
    _make_entity("sensor.processor_temperature", "45.0", device_class="temperature", friendly_name="CPU Temp", unit="°C"),
    _make_entity("sensor.unavailable_sensor", "unavailable", device_class="temperature", friendly_name="Broken Sensor"),
]


class TestHAGuard:
    """Tests for URL/token guard."""

    def test_returns_not_configured_when_url_blank(self):
        from app.sources import home_assistant
        from app.config import settings
        original_url = settings.ha_url
        original_token = settings.ha_token
        settings.ha_url = ""
        settings.ha_token = ""
        try:
            result = home_assistant.search("house status")
            assert "not configured" in result.lower()
        finally:
            settings.ha_url = original_url
            settings.ha_token = original_token

    def test_returns_error_on_connection_failure(self):
        from app.sources import home_assistant
        from app.config import settings
        import requests
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"
        try:
            with patch("app.sources.home_assistant._session.get", side_effect=requests.exceptions.ConnectionError("refused")):
                result = home_assistant.search("house status")
            assert "could not connect" in result.lower()
        finally:
            settings.ha_url = ""
            settings.ha_token = ""


class TestExclusions:
    """Tests for entity exclusion logic."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_excludes_tv_segments(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("which lights are on")
        assert "TV Segment" not in result

    def test_excludes_unavailable_entities(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("indoor air quality")
        assert "Broken Sensor" not in result

    def test_excludes_processor_temperature(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("indoor air quality")
        assert "CPU Temp" not in result

    def test_excludes_outdoor_from_indoor_query(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("indoor air quality")
        assert "Outdoor Temp" not in result

    def test_no_duplicate_entities(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("house status summary")
        # Count occurrences of a known entity name
        assert result.count("Front Door") <= 3  # lock + binary sensor + motion at most


class TestLightQueries:
    """Tests for light-related queries."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_lights_on_returns_only_on_lights(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("which lights are on")
        assert "Living Room" in result
        assert "Bedroom" not in result

    def test_all_lights_returned_without_state_filter(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("lights status")
        assert "Living Room" in result
        assert "Bedroom" in result


class TestLockQueries:
    """Tests for lock and door queries."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_locked_doors_returned(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("are the doors locked")
        assert "Front Door" in result
        assert "locked" in result

    def test_no_sensor_bleed_in_lock_query(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("are the doors locked")
        assert "CO2" not in result
        assert "Battery" not in result


class TestEnvironmentalQueries:
    """Tests for indoor environmental queries."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_indoor_air_returns_co2_temp_humidity(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("indoor air quality")
        assert "Room CO2" in result
        assert "Room Temperature" in result
        assert "Room Humidity" in result

    def test_temperature_rounded(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("indoor air quality")
        assert "72.5" in result
        assert "72.500000" not in result

    def test_no_locks_in_air_quality(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("indoor air quality")
        assert "locked" not in result


class TestBatteryQueries:
    """Tests for battery status queries."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_battery_returns_battery_sensors(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("battery status")
        assert "Phone Battery" in result
        assert "Camera Battery" in result

    def test_no_lights_in_battery_query(self):
        from app.sources import home_assistant
        with patch("app.sources.home_assistant._session.get", return_value=_mock_states(SAMPLE_STATES)):
            result = home_assistant.search("battery status")
        assert "Living Room" not in result
        assert "Bedroom" not in result


class TestMotionFormatting:
    """Tests for motion event time-ago formatting."""

    def test_format_motion_recent(self):
        from app.sources.home_assistant import _format_motion_event
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=5)).isoformat()
        entity = {"entity_id": "event.front_yard_motion", "state": recent, "attributes": {"friendly_name": "Front Yard"}}
        result = _format_motion_event(entity)
        assert "5 minutes ago" in result

    def test_format_motion_hours(self):
        from app.sources.home_assistant import _format_motion_event
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        two_hours_ago = (now - timedelta(hours=2)).isoformat()
        entity = {"entity_id": "event.front_yard_motion", "state": two_hours_ago, "attributes": {"friendly_name": "Front Yard"}}
        result = _format_motion_event(entity)
        assert "2 hours ago" in result

    def test_format_motion_days(self):
        from app.sources.home_assistant import _format_motion_event
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        three_days_ago = (now - timedelta(days=3)).isoformat()
        entity = {"entity_id": "event.front_yard_motion", "state": three_days_ago, "attributes": {"friendly_name": "Front Yard"}}
        result = _format_motion_event(entity)
        assert "3 days ago" in result

    def test_format_motion_singular_minute(self):
        """Regression test for a real, small grammar inconsistency found
        via a deliberate "bulletproofing" pass: hours and days both
        already correctly handled the singular/plural distinction
        ("1 hour ago" vs "2 hours ago"), but this exact same pattern was
        overlooked for minutes, producing "1 minutes ago" instead of
        "1 minute ago"."""
        from app.sources.home_assistant import _format_motion_event
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        one_minute_ago = (now - timedelta(minutes=1)).isoformat()
        entity = {"entity_id": "event.front_yard_motion", "state": one_minute_ago, "attributes": {"friendly_name": "Front Yard"}}
        result = _format_motion_event(entity)
        assert "1 minute ago" in result
        assert "1 minutes ago" not in result


class TestAreaDetection:
    """Tests for _detect_area area/room name detection."""

    def setup_method(self):
        from app.sources.home_assistant import _detect_area
        self.detect = _detect_area

    def test_living_room_detected(self):
        assert self.detect("what lights are in the living room") == "living_room"

    def test_master_bedroom_detected(self):
        assert self.detect("temperature in the master bedroom") == "master_bedroom"

    def test_bedroom_detected(self):
        assert self.detect("lights in the bedroom") == "bedroom"

    def test_kitchen_detected(self):
        assert self.detect("what lights are on in the kitchen") == "kitchen"

    def test_outside_detected(self):
        assert self.detect("what are the outdoor conditions outside") == "outside"

    def test_outdoors_alias(self):
        assert self.detect("what is the temperature outdoors") == "outside"

    def test_master_bathroom_detected(self):
        assert self.detect("is the light on in the master bathroom") == "master_bathroom"

    def test_guest_bedroom_detected(self):
        assert self.detect("lights in the guest bedroom") == "guest_bedroom"

    def test_no_area_returns_none(self):
        assert self.detect("house status summary") is None

    def test_no_area_for_generic_query(self):
        assert self.detect("what is the temperature") is None

    def test_longest_match_wins(self):
        # "master bedroom" should match over "bedroom"
        assert self.detect("temperature in the master bedroom") == "master_bedroom"

    def test_shed_does_not_match_inside_finished(self):
        """Regression test for a real, significant bug found via a
        deliberate "bulletproofing" pass: "shed" (a real area alias) is
        a genuine substring of "finished," "crashed," "washed," and
        other common past-tense verbs. "Is the download finished yet"
        — a query with nothing to do with any area at all — incorrectly
        resolved to area_id="shed" before this fix, purely from the
        accidental substring. The existing longest-match-first checking
        order only incidentally protected against this when a genuine,
        longer area phrase ALSO happened to be present in the same
        query — it did nothing when "shed" was the only thing that
        happened to match at all."""
        assert self.detect("is the download finished yet") is None

    def test_shed_does_not_match_inside_washed(self):
        assert self.detect("did you wash the dishes") is None

    def test_genuine_shed_area_still_detected(self):
        """Confirms the word-boundary fix didn't accidentally break the
        real, intended "shed" match — only the false-positive substring
        case should be excluded, not the genuine standalone word."""
        assert self.detect("any motion in the shed") == "shed"


class TestAreaSearch:
    """Tests for area-filtered search behavior."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def _mock_states(self, entities):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = entities
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def _mock_area_template(self, area_map: dict) -> MagicMock:
        """Build a mock response for the area template API."""
        lines = []
        for area_id, entity_ids in area_map.items():
            lines.append(f"{area_id}|||{','.join(entity_ids)}")
        mock_resp = MagicMock()
        mock_resp.text = "\n".join(lines)
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def test_area_filter_limits_results(self):
        from app.sources import home_assistant
        states = [
            _make_entity("light.bedroom_light", "on", friendly_name="Bedroom Light"),
            _make_entity("light.living_room_light", "on", friendly_name="Living Room Light"),
        ]
        area_map = {"bedroom": ["light.bedroom_light"]}

        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)),              patch("app.sources.home_assistant._session.post", return_value=self._mock_area_template(area_map)):
            result = home_assistant.search("lights in the bedroom")

        assert "Bedroom Light" in result
        assert "Living Room Light" not in result

    def test_area_filter_with_state_filter(self):
        from app.sources import home_assistant
        states = [
            _make_entity("light.bedroom_light", "on", friendly_name="Bedroom Light"),
            _make_entity("light.bedroom_lamp", "off", friendly_name="Bedroom Lamp"),
        ]
        area_map = {"bedroom": ["light.bedroom_light", "light.bedroom_lamp"]}

        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)),              patch("app.sources.home_assistant._session.post", return_value=self._mock_area_template(area_map)):
            result = home_assistant.search("which lights are on in the bedroom")

        assert "Bedroom Light" in result
        assert "Bedroom Lamp" not in result

    def test_area_filter_respects_exclude_entity_keywords(self):
        """Regression test for a real, significant bug found via a
        deliberate complexity-reduction investigation: the area-filtered
        branch of search() used to reimplement only a SUBSET of
        _matches_filter()'s real logic (state_filter and a simplified
        domain/device_class check), silently missing
        exclude_entity_keywords entirely. This was genuinely reachable —
        queries like "temperature", "humidity", and "indoor air quality"
        all set real exclude_entity_keywords (filtering out cotech/
        processor/esp32 sensor-node entities), and combining any of them
        with a real area name silently skipped that exclusion. Fixed by
        deferring to _matches_filter() for any genuinely non-empty
        filter, rather than maintaining a second, incomplete
        reimplementation."""
        from app.sources import home_assistant
        states = [
            _make_entity("sensor.living_room_temperature", "72", device_class="temperature",
                        friendly_name="Living Room Temp"),
            _make_entity("sensor.living_room_cotech_temperature", "70", device_class="temperature",
                        friendly_name="Cotech Temp"),
        ]
        area_map = {"living_room": [
            "sensor.living_room_temperature", "sensor.living_room_cotech_temperature"
        ]}

        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)),              patch("app.sources.home_assistant._session.post", return_value=self._mock_area_template(area_map)):
            result = home_assistant.search("temperature in the living room")

        assert "Living Room Temp" in result
        assert "Cotech Temp" not in result

    def test_area_filter_with_default_summary_filter_returns_broad_results(self):
        """A bare area-only query with no specific keyword match falls
        back to _build_filter("summary") (real, broad domains/device
        classes, not an empty filter — confirmed via static trace, 2000
        Hypothesis-generated fuzz inputs, and an exhaustive check of
        every _QUERY_MAP entry that _build_filter() never actually
        produces a genuinely empty filter spec for any real input). This
        test confirms that realistic, broad-but-real filter correctly
        returns multiple different entity types within the area, the
        same way a "house status" query would."""
        from app.sources import home_assistant
        states = [
            _make_entity("light.living_room_lamp", "on", friendly_name="Living Room Lamp"),
            _make_entity("sensor.living_room_temperature", "72", device_class="temperature",
                        friendly_name="Living Room Temp"),
        ]
        area_map = {"living_room": [
            "light.living_room_lamp", "sensor.living_room_temperature"
        ]}

        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)),              patch("app.sources.home_assistant._session.post", return_value=self._mock_area_template(area_map)):
            result = home_assistant.search("what's in the living room")

        assert "Living Room Lamp" in result
        assert "Living Room Temp" in result

    def test_unknown_area_falls_back_to_keyword(self):
        from app.sources import home_assistant
        states = [
            _make_entity("sensor.room_temperature", "72.5", device_class="temperature",
                        friendly_name="Room Temp", unit="°F"),
        ]
        # Empty area map — area not found
        area_map = {}

        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)),              patch("app.sources.home_assistant._session.post", return_value=self._mock_area_template(area_map)):
            result = home_assistant.search("temperature in the attic")

        # Should fall back to keyword filter and find temperature sensor
        assert "Room Temp" in result

    def test_no_area_uses_keyword_filter(self):
        from app.sources import home_assistant
        states = [
            _make_entity("lock.front_door", "locked", friendly_name="Front Door"),
            _make_entity("lock.back_door", "locked", friendly_name="Back Door"),
        ]

        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("are the doors locked")

        assert "Front Door" in result
        assert "Back Door" in result


class TestValueFormatting:
    """Tests for numeric value formatting."""

    def test_temperature_rounded(self):
        from app.sources.home_assistant import _format_value
        assert _format_value("79.9060211181641", "°F") == "79.9 °F"

    def test_humidity_rounded(self):
        from app.sources.home_assistant import _format_value
        assert _format_value("39.117431640625", "%") == "39.1 %"

    def test_co2_rounded(self):
        from app.sources.home_assistant import _format_value
        assert _format_value("694.0", "ppm") == "694.0 ppm"

    def test_non_numeric_passthrough(self):
        from app.sources.home_assistant import _format_value
        assert _format_value("locked", "") == "locked"


class TestHAHelperFunctions:
    """Tests for HA helper functions — friendly name, unit, exclusion."""

    def test_friendly_name_from_attributes(self):
        from app.sources.home_assistant import _friendly_name
        entity = {"entity_id": "light.bedroom", "attributes": {"friendly_name": "Bedroom Light"}}
        assert _friendly_name(entity) == "Bedroom Light"

    def test_friendly_name_falls_back_to_entity_id(self):
        from app.sources.home_assistant import _friendly_name
        entity = {"entity_id": "light.bedroom", "attributes": {}}
        assert _friendly_name(entity) == "light.bedroom"

    def test_unit_from_attributes(self):
        from app.sources.home_assistant import _unit
        entity = {"entity_id": "sensor.temp", "attributes": {"unit_of_measurement": "°F"}}
        assert _unit(entity) == "°F"

    def test_unit_empty_when_missing(self):
        from app.sources.home_assistant import _unit
        entity = {"entity_id": "lock.door", "attributes": {}}
        assert _unit(entity) == ""

    def test_excluded_when_unavailable(self):
        from app.sources.home_assistant import _is_excluded
        entity = {"entity_id": "sensor.temp", "state": "unavailable", "attributes": {}}
        assert _is_excluded(entity) is True

    def test_excluded_when_unknown(self):
        from app.sources.home_assistant import _is_excluded
        entity = {"entity_id": "sensor.temp", "state": "unknown", "attributes": {}}
        assert _is_excluded(entity) is True

    def test_excluded_tv_segment(self):
        from app.sources.home_assistant import _is_excluded
        entity = {"entity_id": "light.tv_backlight_segment_1", "state": "on", "attributes": {}}
        assert _is_excluded(entity) is True

    def test_excluded_doorbell_ding(self):
        from app.sources.home_assistant import _is_excluded
        entity = {"entity_id": "binary_sensor.front_door_ding", "state": "off", "attributes": {}}
        assert _is_excluded(entity) is True

    def test_not_excluded_normal_entity(self):
        from app.sources.home_assistant import _is_excluded
        entity = {"entity_id": "light.bedroom", "state": "on", "attributes": {}}
        assert _is_excluded(entity) is False


class TestGetStates:
    """Tests for _get_states() — raw HA REST API fetch."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_returns_none_when_not_configured(self):
        from app.sources import home_assistant
        from app.config import settings
        settings.ha_url = ""
        result = home_assistant._get_states()
        assert result is None

    def test_returns_states_list_on_success(self):
        from app.sources import home_assistant
        from unittest.mock import patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"entity_id": "light.test", "state": "on", "attributes": {}}]
        mock_resp.raise_for_status.return_value = None
        with patch("app.sources.home_assistant._session.get", return_value=mock_resp):
            result = home_assistant._get_states()
        assert result == [{"entity_id": "light.test", "state": "on", "attributes": {}}]

    def test_returns_none_on_connection_error(self):
        from app.sources import home_assistant
        import requests as req
        from unittest.mock import patch
        with patch("app.sources.home_assistant._session.get", side_effect=req.exceptions.ConnectionError()):
            result = home_assistant._get_states()
        assert result is None

    def test_returns_none_on_http_error(self):
        from app.sources import home_assistant
        import requests as req
        from unittest.mock import patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.exceptions.HTTPError("401")
        with patch("app.sources.home_assistant._session.get", return_value=mock_resp):
            result = home_assistant._get_states()
        assert result is None

    def test_sends_bearer_token_header(self):
        from app.sources import home_assistant
        from unittest.mock import patch, MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_resp.raise_for_status.return_value = None
        with patch("app.sources.home_assistant._session.get", return_value=mock_resp) as mock_get:
            home_assistant._get_states()
        headers = mock_get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer fake-token"


class TestFormatEntity:
    """Tests for _format_entity() — entity → display string formatting."""

    def test_formats_with_unit(self):
        from app.sources.home_assistant import _format_entity
        entity = {
            "entity_id": "sensor.temp",
            "state": "72.5",
            "attributes": {"friendly_name": "Room Temp", "unit_of_measurement": "°F"},
        }
        result = _format_entity(entity)
        assert "Room Temp" in result
        assert "72.5" in result
        assert "°F" in result

    def test_formats_without_unit(self):
        from app.sources.home_assistant import _format_entity
        entity = {
            "entity_id": "lock.front_door",
            "state": "locked",
            "attributes": {"friendly_name": "Front Door"},
        }
        result = _format_entity(entity)
        assert result == "Front Door: locked"

    def test_falls_back_to_entity_id_without_friendly_name(self):
        from app.sources.home_assistant import _format_entity
        entity = {"entity_id": "light.unnamed", "state": "on", "attributes": {}}
        result = _format_entity(entity)
        assert "light.unnamed" in result


class TestMatchesFilter:
    """Tests for _matches_filter() — the core entity matching engine."""

    def _entity(self, entity_id, state="on", device_class="", friendly_name=""):
        return {
            "entity_id": entity_id,
            "state": state,
            "attributes": {
                "device_class": device_class,
                "friendly_name": friendly_name or entity_id,
            },
        }

    def _filter(self, **kwargs):
        base = {
            "domains": set(), "device_classes": set(), "entity_keywords": set(),
            "exclude_entity_keywords": set(), "event_keywords": set(),
            "state_filter": None, "include_motion": False,
        }
        base.update(kwargs)
        return base

    def test_excluded_entity_never_matches(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("sensor.x", state="unavailable")
        f = self._filter(domains={"sensor"})
        assert _matches_filter(entity, f) is False

    def test_domain_match(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("light.bedroom")
        f = self._filter(domains={"light"})
        assert _matches_filter(entity, f) is True

    def test_domain_mismatch(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("lock.front_door")
        f = self._filter(domains={"light"})
        assert _matches_filter(entity, f) is False

    def test_device_class_match(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("sensor.battery1", device_class="battery")
        f = self._filter(device_classes={"battery"})
        assert _matches_filter(entity, f) is True

    def test_state_filter_matches(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("light.bedroom", state="on")
        f = self._filter(domains={"light"}, state_filter="on")
        assert _matches_filter(entity, f) is True

    def test_state_filter_excludes_non_matching_state(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("light.bedroom", state="off")
        f = self._filter(domains={"light"}, state_filter="on")
        assert _matches_filter(entity, f) is False

    def test_exclude_entity_keywords(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("sensor.cotech_outdoor_temp", device_class="temperature")
        f = self._filter(device_classes={"temperature"}, exclude_entity_keywords={"cotech"})
        assert _matches_filter(entity, f) is False

    def test_strict_parameter_removed_after_being_found_dead(self):
        """Regression test documenting a real finding from a deliberate
        complexity-investigation pass: `_matches_filter()` used to take
        a `strict` flag in its filter spec, with a comment claiming
        strict mode should "only match domain OR device_class, not
        entity keywords bleeding in." In practice, the strict and
        non-strict code branches were byte-for-byte behaviorally
        identical — verified with a comprehensive sweep across all 13
        real `_QUERY_MAP` entries that set `strict: True` and 9 varied
        test entities (117 combinations), finding zero behavioral
        differences anywhere. Removed entirely — both from
        `_matches_filter()`'s filter-spec handling and from every
        `_QUERY_MAP` entry that set it, since the flag carried no actual
        meaning.

        Two PRE-EXISTING tests (test_strict_mode_blocks_entity_keyword_bleed,
        test_strict_mode_allows_domain_match) claimed to verify strict
        mode's behavior but never actually could have — both constructed
        filters with NO entity_keywords set at all, meaning there was
        nothing for entity_keywords to "bleed" from regardless of the
        strict flag's value. Both tests passed before this change for
        the wrong reason (the scenario they tested never exercised the
        claimed behavior) and continue to pass after this change for
        the right reason (the filter dict no longer has a strict key at
        all, and `_matches_filter()` correctly ignores any extra keys a
        caller's dict might still contain)."""
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("light.bedroom")
        # A filter dict with no "strict" key at all — confirms
        # _matches_filter() doesn't require the key to be present
        f = {
            "domains": {"light"}, "device_classes": set(), "entity_keywords": set(),
            "exclude_entity_keywords": set(), "event_keywords": set(),
            "state_filter": None, "include_motion": False,
        }
        assert _matches_filter(entity, f) is True

    def test_event_domain_matches_by_keyword(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("event.front_yard_motion")
        f = self._filter(event_keywords={"motion"})
        assert _matches_filter(entity, f) is True

    def test_event_domain_no_match_without_keyword(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("event.button_press")
        f = self._filter(event_keywords={"motion"})
        assert _matches_filter(entity, f) is False

    def test_entity_keyword_match(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("sensor.outdoor_weather_station")
        f = self._filter(entity_keywords={"outdoor"})
        assert _matches_filter(entity, f) is True

    def test_no_criteria_matches_nothing(self):
        from app.sources.home_assistant import _matches_filter
        entity = self._entity("light.bedroom")
        f = self._filter()
        assert _matches_filter(entity, f) is False


class TestListAreas:
    """Tests for list_areas() — GET /areas endpoint backing function."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def test_not_configured_when_no_url(self):
        from app.sources import home_assistant
        from app.config import settings
        settings.ha_url = ""
        result = home_assistant.list_areas()
        assert result["status"] == "not_configured"
        assert result["areas"] == {}

    def test_not_configured_when_no_token(self):
        from app.sources import home_assistant
        from app.config import settings
        settings.ha_token = ""
        result = home_assistant.list_areas()
        assert result["status"] == "not_configured"

    def test_error_when_area_fetch_fails(self):
        from app.sources import home_assistant
        from unittest.mock import patch
        with patch("app.sources.home_assistant._get_area_entities", return_value=None):
            result = home_assistant.list_areas()
        assert result["status"] == "error"
        assert result["areas"] == {}

    def test_returns_entity_counts(self):
        from app.sources import home_assistant
        from unittest.mock import patch
        area_map = {
            "kitchen": ["light.kitchen_1", "light.kitchen_2", "sensor.kitchen_temp"],
            "bedroom": ["light.bedroom_1"],
        }
        with patch("app.sources.home_assistant._get_area_entities", return_value=area_map):
            result = home_assistant.list_areas()
        assert result["status"] == "ok"
        assert result["areas"]["kitchen"]["entity_count"] == 3
        assert result["areas"]["bedroom"]["entity_count"] == 1

    def test_includes_matching_aliases(self):
        from app.sources import home_assistant
        from unittest.mock import patch
        area_map = {"living_room": ["light.lr_1"]}
        with patch("app.sources.home_assistant._get_area_entities", return_value=area_map):
            result = home_assistant.list_areas()
        assert "living room" in result["areas"]["living_room"]["aliases"]

    def test_area_with_no_aliases_returns_empty_list(self):
        from app.sources import home_assistant
        from unittest.mock import patch
        area_map = {"unmapped_area_xyz": ["light.x"]}
        with patch("app.sources.home_assistant._get_area_entities", return_value=area_map):
            result = home_assistant.list_areas()
        assert result["areas"]["unmapped_area_xyz"]["aliases"] == []

    def test_areas_sorted_alphabetically(self):
        from app.sources import home_assistant
        from unittest.mock import patch
        area_map = {"zebra_room": ["light.z"], "alpha_room": ["light.a"]}
        with patch("app.sources.home_assistant._get_area_entities", return_value=area_map):
            result = home_assistant.list_areas()
        area_names = list(result["areas"].keys())
        assert area_names == sorted(area_names)

    def test_multiple_aliases_for_same_area(self):
        from app.sources import home_assistant
        from unittest.mock import patch
        area_map = {"master_bathroom": ["light.mb_1"]}
        with patch("app.sources.home_assistant._get_area_entities", return_value=area_map):
            result = home_assistant.list_areas()
        aliases = result["areas"]["master_bathroom"]["aliases"]
        assert "master bath" in aliases
        assert "master bathroom" in aliases


class TestBuildFilter:
    """Tests for _build_filter query keyword matching."""

    def test_lights_query_returns_light_domain(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("which lights are on")
        assert "light" in f["domains"]

    def test_battery_query_returns_battery_device_class(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("battery status")
        assert "battery" in f["device_classes"]

    def test_temperature_query_returns_temperature_class(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("indoor air quality")
        assert "temperature" in f["device_classes"] or "carbon_dioxide" in f["device_classes"]

    def test_door_query_returns_lock_domain(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("are the doors locked")
        assert "lock" in f["domains"]

    def test_garage_door_query_returns_cover_domain_not_lock(self):
        """Regression test for a real, two-file gap found while
        researching whether conditional_remainder's kiwix-double-hit
        pairs were genuinely intentional kiwix routing (see the
        conditional_remainder design doc): "garage door"/"garage" is
        a deliberately SEPARATE _QUERY_MAP entry from "door"/"lock"
        above, not a widening of them — a garage door's open/closed
        state is a genuinely different question from a lock's
        locked/unlocked state (most garage doors have no lock entity
        of their own), and test_door_query_returns_lock_domain above
        already pins "are the doors locked" to stay lock-domain-only,
        so this needed its own trigger rather than touching that
        one."""
        from app.sources.home_assistant import _build_filter
        f = _build_filter("the garage door is open")
        assert "cover" in f["domains"]
        assert "lock" not in f["domains"]

    def test_garage_device_classes_cover_both_real_ha_naming_conventions(self):
        """Confirms the filter covers BOTH real Home Assistant
        device_class strings for the same physical entity — a `cover`
        entity uses "garage" (confirmed via Home Assistant's own
        developer docs and CoverDeviceClass enum), while a plain
        `binary_sensor` reporting the same door uses "garage_door"
        instead (confirmed via a live, filed Home Assistant core
        issue, home-assistant/core#91131, describing the naming
        inconsistency directly). Without both, this fix would only
        work for half of Mike's possible real hardware/integration
        shape, which can't be verified directly from outside the
        actual deployment."""
        from app.sources.home_assistant import _build_filter
        f = _build_filter("garage status")
        assert "garage" in f["device_classes"]
        assert "garage_door" in f["device_classes"]

    def test_word_boundary_on_does_not_match_inside_front(self):
        """Regression test for a real, severe bug found via a deliberate
        "bulletproofing" pass: naive substring matching (no word-boundary
        awareness) meant "on" (a real, bare dictionary key for "lights
        on") matched as a substring of "front" — "is the front door
        locked," about as natural a query as this entire source exists
        to answer, got an incorrect state_filter="on" applied, and the
        real, correctly-named, correctly-stated front door lock entity
        was silently rejected by the filter. Confirmed end to end:
        search("is the front door locked") against a real front door
        lock entity returned "No matching entities found" before this
        fix."""
        from app.sources.home_assistant import _build_filter
        f = _build_filter("is the front door locked")
        assert f["state_filter"] is None

    def test_word_boundary_rain_does_not_match_inside_training(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("is anyone training right now")
        assert "rain" not in f["entity_keywords"]

    def test_word_boundary_on_does_not_match_inside_alone(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("is everyone home alone")
        assert f["state_filter"] is None

    def test_genuine_lights_on_query_still_works(self):
        """Confirms the word-boundary fix didn't accidentally break the
        real, intended "on" match — only the false-positive substring
        case should be excluded, not the genuine standalone word."""
        from app.sources.home_assistant import _build_filter
        f = _build_filter("are the lights on right now")
        assert f["state_filter"] == "on"

    def test_outdoor_still_correctly_excludes_door_keyword(self):
        """Confirms the longest-match-first protection (already
        existing, unrelated to the new word-boundary fix) still
        correctly handles a genuine overlapping case — "outdoor"
        winning over "door" when both would match the same letters."""
        from app.sources.home_assistant import _build_filter
        f = _build_filter("check the outdoor conditions")
        assert "lock" not in f["domains"]

    def test_unmatched_query_falls_back_to_summary(self):
        from app.sources.home_assistant import _build_filter
        f = _build_filter("xyzzy nonsense query")
        # Falls back to summary which includes lights and sensors
        assert len(f["domains"]) > 0 or len(f["device_classes"]) > 0


class TestBinarySensorMotionSupport:
    """Regression tests for two real, related bugs found via a
    deliberate "bulletproofing" pass: investigating why dedup logic
    (motion_event_names) existed at all for binary_sensor motion
    entities, when _QUERY_MAP's "motion"/"camera"/"activity" keywords
    only ever listed "event" as a possible domain — meaning
    binary_sensor motion entities (the more common convention used by
    many real Zigbee2MQTT/Z-Wave/PIR integrations) were never actually
    reachable through those keywords at all. The dedup logic only makes
    sense if binary_sensor motion entities were always meant to be
    reachable, confirmed by "security"/"security status" already
    correctly including device_classes: ["motion"] elsewhere in the
    same dict. Fixed by adding device_classes: ["motion"] to these
    three entries too, and fixing the dedup check itself (which was
    global rather than per-entity — suppressing EVERY binary_sensor
    motion entity in the house if ANY motion sensor anywhere had
    event-based data, even completely unrelated ones with no event
    entity of their own)."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def _mock_states(self, entities):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = entities
        return mock_resp

    def test_binary_sensor_motion_with_no_event_counterpart_is_included(self):
        """The actual real-world regression test: a motion sensor
        reporting ONLY via binary_sensor (no event entity of its own)
        must not be silently dropped just because a DIFFERENT, unrelated
        sensor elsewhere in the house happens to have event-based data."""
        from app.sources import home_assistant
        states = [
            _make_entity("event.front_door_motion", "2026-06-24T10:00:00Z"),
            _make_entity("binary_sensor.front_door_motion", "on", device_class="motion"),
            _make_entity("binary_sensor.backyard_motion", "on", device_class="motion"),
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("any motion")
        assert "backyard_motion" in result

    def test_binary_sensor_motion_with_genuine_event_counterpart_is_suppressed(self):
        """Confirms the fix is still genuinely per-entity, not simply
        disabled — a binary_sensor that DOES have a real event
        counterpart for the same physical sensor should still be
        correctly suppressed, avoiding a duplicate entry for the same
        real-world motion detection."""
        from app.sources import home_assistant
        states = [
            _make_entity("event.front_door_motion", "2026-06-24T10:00:00Z"),
            _make_entity("binary_sensor.front_door_motion", "on", device_class="motion"),
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("any motion")
        assert result.count("front_door") == 1

    def test_binary_sensor_motion_labeled_motion_not_door_sensors(self):
        """Regression test for a related labeling bug found while
        verifying the fix above: binary_sensor entities were
        unconditionally labeled "Door Sensors" regardless of their
        actual device_class — a reasonable assumption when binary_sensor
        entities were never reachable except via door-specific
        keywords, but genuinely wrong now that binary_sensor motion
        entities are correctly reachable too."""
        from app.sources import home_assistant
        states = [_make_entity("binary_sensor.backyard_motion", "on", device_class="motion")]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("any motion")
        assert "**Motion:**" in result
        assert "Door Sensors" not in result

    def test_door_binary_sensors_still_correctly_labeled(self):
        """Confirms the labeling fix didn't break the genuine, intended
        case — a real binary_sensor door entity should still be labeled
        "Door Sensors," not "Motion."""
        from app.sources import home_assistant
        states = [_make_entity("binary_sensor.front_door_open", "off", device_class="door")]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("are the doors locked")
        assert "**Door Sensors:**" in result


class TestGarageDoorSupport:
    """Regression tests for a real, two-file gap found while
    researching whether conditional_remainder's kiwix-double-hit pool
    entries (see the conditional_remainder design doc and
    app/router.py's own INTENT_MAP comment on the same investigation)
    were genuinely intentional kiwix routing or a real gap. "the
    garage door is open" is a question about OPEN/CLOSED state — a
    different question from every existing door trigger in this file,
    which are all locked/unlocked phrasing (most garage doors have no
    lock entity of their own).

    Two coordinated fixes were needed, in two different files:
    router.py's INTENT_MAP needed a new "garage door"/"garage" trigger
    so the query reaches the ha handler at all (see
    TestKeywordDetect.test_garage_door_is_open_matches_ha in
    test_router.py — routing was failing BEFORE this entity-level fix
    even had a chance to matter), and this file's own _QUERY_MAP needed
    a matching "garage" entry, since neither a real `cover` domain
    entity nor a `binary_sensor` with device_class "garage_door" was
    reachable here either, even once routing worked.

    Covers both real Home Assistant naming conventions for the same
    physical entity, confirmed via Home Assistant's own developer docs
    (CoverDeviceClass.GARAGE = "garage") and a live, filed core issue
    (home-assistant/core#91131) describing the cover-vs-binary_sensor
    naming inconsistency directly."""

    def setup_method(self):
        from app.config import settings
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "fake-token"

    def teardown_method(self):
        from app.config import settings
        settings.ha_url = ""
        settings.ha_token = ""

    def _mock_states(self, entities):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = entities
        mock_resp.raise_for_status.return_value = None
        return mock_resp

    def test_real_cover_entity_matches_and_is_labeled_garage_doors(self):
        """The `cover` domain shape — the dedicated, purpose-built HA
        domain for openings (garage doors, blinds, gates), confirmed
        via Home Assistant's own developer documentation."""
        from app.sources import home_assistant
        states = [_make_entity("cover.garage_door", "open", device_class="garage", friendly_name="Garage Door")]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("is the garage door open")
        assert "**Garage Doors:**" in result
        assert "Garage Door: open" in result

    def test_binary_sensor_garage_door_variant_also_matches(self):
        """The simpler, common alternate shape — a plain binary_sensor
        with device_class "garage_door" (NOT "garage" — confirmed via
        a live, filed Home Assistant core issue describing this exact
        naming inconsistency between the cover and binary_sensor
        domains for the same physical entity type), the integration
        shape a basic reed-switch sensor with no remote-control
        capability typically uses."""
        from app.sources import home_assistant
        states = [_make_entity("binary_sensor.garage_door", "on", device_class="garage_door", friendly_name="Garage Door")]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("garage door status")
        assert "Garage Door: on" in result

    def test_garage_door_query_does_not_pull_in_unrelated_locks(self):
        """Confirms the new "garage door" trigger stays scoped to
        cover/garage entities and doesn't also widen to pull in
        unrelated lock entities — a genuinely different question
        (open/closed vs locked/unlocked) should get a genuinely
        separate answer."""
        from app.sources import home_assistant
        states = [
            _make_entity("cover.garage_door", "closed", device_class="garage", friendly_name="Garage Door"),
            _make_entity("lock.front_door", "locked", friendly_name="Front Door"),
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("the garage door is open")
        assert "Garage Door" in result
        assert "Front Door" not in result

    def test_existing_door_lock_query_unaffected_by_garage_addition(self):
        """The flip side of the test above — confirms "are the doors
        locked" still stays lock-domain-only and doesn't accidentally
        widen to pull in a garage door's cover entity, now that both
        share the word "door"."""
        from app.sources import home_assistant
        states = [
            _make_entity("cover.garage_door", "closed", device_class="garage", friendly_name="Garage Door"),
            _make_entity("lock.front_door", "locked", friendly_name="Front Door"),
        ]
        with patch("app.sources.home_assistant._session.get", return_value=self._mock_states(states)):
            result = home_assistant.search("are the doors locked")
        assert "Front Door" in result
        assert "Garage Door" not in result
