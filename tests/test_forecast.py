"""
Tests for app/sources/forecast.py
Uses unittest.mock to avoid real network calls.
"""
from unittest.mock import patch, MagicMock


def _mock_forecast_response(
    max_temps=(101, 99, 95),
    min_temps=(77, 75, 73),
    codes=(0, 2, 0),
    precip=(0, 0, 0),
    wind_speeds=(10, 10, 10),
    wind_dirs=(180, 180, 180),
    sunrises=("2026-06-16T05:21:00", "2026-06-17T05:21:00", "2026-06-18T05:21:00"),
    sunsets=("2026-06-16T19:52:00", "2026-06-17T19:52:00", "2026-06-18T19:52:00"),
    times=("2026-06-16", "2026-06-17", "2026-06-18"),
) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.raise_for_status.return_value = None
    mock.json.return_value = {
        "daily": {
            "temperature_2m_max": list(max_temps),
            "temperature_2m_min": list(min_temps),
            "weathercode": list(codes),
            "precipitation_probability_max": list(precip),
            "windspeed_10m_max": list(wind_speeds),
            "winddirection_10m_dominant": list(wind_dirs),
            "sunrise": list(sunrises),
            "sunset": list(sunsets),
            "time": list(times),
        }
    }
    return mock


class TestForecastSearch:
    """Tests for forecast.search() with mocked Open-Meteo responses."""

    def setup_method(self):
        from app.config import settings
        self._original_location_name = settings.forecast_location_name
        self._original_lat = settings.forecast_latitude
        self._original_lon = settings.forecast_longitude
        settings.forecast_location_name = ""
        # Found via a deliberate "bulletproofing" pass: forecast.search()
        # previously had no check for unconfigured (0.0, 0.0) coordinates
        # at all — this entire test class was unknowingly relying on
        # that gap, never setting real coordinates and only "working"
        # because there was no check yet to catch it. Real, valid
        # coordinates are required now that the fix correctly rejects
        # the unconfigured default the same way every other source
        # file's "not configured" check already does.
        settings.forecast_latitude = 35.1894
        settings.forecast_longitude = -114.0530

    def teardown_method(self):
        from app.config import settings
        settings.forecast_location_name = self._original_location_name
        settings.forecast_latitude = self._original_lat
        settings.forecast_longitude = self._original_lon

    def test_unconfigured_coordinates_return_not_configured_message(self):
        """Regression test for a real, significant bug found via a
        deliberate "bulletproofing" pass reading every file in app/ top
        to bottom, specifically looking past complexity scores at
        genuinely small, simple-looking code: forecast_latitude and
        forecast_longitude both default to 0.0 — a falsy value Python
        treats the same way every other source file's config checks do,
        EXCEPT this function never actually had the check. (0.0, 0.0)
        is also a real, valid ocean coordinate off the coast of West
        Africa, so an unconfigured deployment wouldn't error or warn at
        all — it would silently return genuine, real weather data for
        the wrong place on Earth. main.py's own /health endpoint
        already had this exact check; it just never made it to the
        function real user queries actually hit."""
        from app.sources import forecast
        from app.config import settings
        settings.forecast_latitude = 0.0
        settings.forecast_longitude = 0.0
        result = forecast.search("what's the weather")
        assert "not configured" in result.lower()

    def test_returns_today_tomorrow_and_day3(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response()):
            result = forecast.search("weather")
        assert "Today" in result
        assert "Tomorrow" in result
        # Day 3 should be a day name
        assert any(day in result for day in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])

    def test_includes_high_and_low_temps(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response()):
            result = forecast.search("weather")
        assert "101" in result
        assert "77" in result

    def test_precipitation_included_when_20_or_above(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response(precip=(25, 0, 0))):
            result = forecast.search("weather")
        assert "25%" in result

    def test_precipitation_omitted_when_below_20(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response(precip=(10, 10, 10))):
            result = forecast.search("weather")
        assert "10%" not in result

    def test_wind_included_when_15mph_or_above(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response(wind_speeds=(20, 10, 10))):
            result = forecast.search("weather")
        assert "20" in result
        assert "miles per hour" in result

    def test_wind_omitted_when_below_15mph(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response(wind_speeds=(10, 10, 10))):
            result = forecast.search("weather")
        assert "miles per hour" not in result

    def test_sunrise_and_sunset_in_today(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response()):
            result = forecast.search("weather")
        assert "Sunrise" in result or "sunrise" in result
        assert "Sunset" in result or "sunset" in result

    def test_api_error_returns_error_message(self):
        from app.sources import forecast
        import requests
        with patch("app.sources.forecast._session.get", side_effect=requests.exceptions.ConnectionError("refused")):
            result = forecast.search("weather")
        assert "unable to retrieve" in result.lower() or "error" in result.lower()

    def test_partial_daily_data_returns_error_message_not_a_crash(self):
        """Regression test for a real gap found via a deliberate
        function-by-function read: the original try/except only ever
        covered the network call and JSON parse, not the consumption of
        `daily` itself. A genuinely successful HTTP response (status 200,
        valid JSON) that comes back with fewer than 3 days of data —
        forecast_days=3 is a request parameter, not a guarantee — used
        to raise an uncaught IndexError once the code reached day 3's
        indexing, rather than degrading the same honest way a network
        failure already does."""
        from app.sources import forecast
        mock = _mock_forecast_response()
        # Only 2 days of weathercode data, simulating a real partial/
        # degraded API response despite a successful HTTP status.
        mock.json.return_value["daily"]["weathercode"] = [0, 2]
        with patch("app.sources.forecast._session.get", return_value=mock):
            result = forecast.search("weather")
        assert "unable to retrieve" in result.lower()

    def test_missing_daily_field_returns_error_message_not_a_crash(self):
        """The sibling case to the partial-data test above — a response
        missing an entire expected field (not just a short array) must
        also degrade gracefully rather than raising an uncaught
        KeyError."""
        from app.sources import forecast
        mock = _mock_forecast_response()
        del mock.json.return_value["daily"]["sunrise"]
        with patch("app.sources.forecast._session.get", return_value=mock):
            result = forecast.search("weather")
        assert "unable to retrieve" in result.lower()

    def test_clear_weather_described(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response(codes=(0, 0, 0))):
            result = forecast.search("weather")
        assert "clear" in result.lower()

    def test_rain_weather_described(self):
        from app.sources import forecast
        with patch("app.sources.forecast._session.get", return_value=_mock_forecast_response(codes=(63, 63, 63))):
            result = forecast.search("weather")
        assert "rain" in result.lower()


class TestLocationNamePrefix:
    """Tests for location name appearing in forecast output."""

    def setup_method(self):
        from app.config import settings
        self._original_location_name = settings.forecast_location_name
        self._original_lat = settings.forecast_latitude
        self._original_lon = settings.forecast_longitude
        settings.forecast_latitude = 35.1894
        settings.forecast_longitude = -114.0530

    def teardown_method(self):
        from app.config import settings
        settings.forecast_location_name = self._original_location_name
        settings.forecast_latitude = self._original_lat
        settings.forecast_longitude = self._original_lon

    def test_location_name_included_when_configured(self):
        from app.sources import forecast
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.forecast_location_name = "Kingman, Arizona"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "daily": {
                "weathercode": [0, 0, 0],
                "temperature_2m_max": [95, 93, 92],
                "temperature_2m_min": [70, 68, 65],
                "precipitation_probability_max": [0, 0, 0],
                "windspeed_10m_max": [5, 5, 5],
                "winddirection_10m_dominant": [180, 180, 180],
                "sunrise": ["2026-06-19T05:21:00", "2026-06-20T05:21:00", "2026-06-21T05:22:00"],
                "sunset": ["2026-06-19T19:53:00", "2026-06-20T19:53:00", "2026-06-21T19:53:00"],
                "time": ["2026-06-19", "2026-06-20", "2026-06-21"],
            }
        }
        mock_resp.raise_for_status.return_value = None
        with patch("app.sources.forecast._session.get", return_value=mock_resp):
            result = forecast.search("weather")
        assert "Kingman, Arizona" in result

    def test_no_location_prefix_when_not_configured(self):
        from app.sources import forecast
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.forecast_location_name = ""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "daily": {
                "weathercode": [0, 0, 0],
                "temperature_2m_max": [95, 93, 92],
                "temperature_2m_min": [70, 68, 65],
                "precipitation_probability_max": [0, 0, 0],
                "windspeed_10m_max": [5, 5, 5],
                "winddirection_10m_dominant": [180, 180, 180],
                "sunrise": ["2026-06-19T05:21:00", "2026-06-20T05:21:00", "2026-06-21T05:22:00"],
                "sunset": ["2026-06-19T19:53:00", "2026-06-20T19:53:00", "2026-06-21T19:53:00"],
                "time": ["2026-06-19", "2026-06-20", "2026-06-21"],
            }
        }
        mock_resp.raise_for_status.return_value = None
        with patch("app.sources.forecast._session.get", return_value=mock_resp):
            result = forecast.search("weather")
        assert result.startswith("Today will be")


class TestConfigurableThresholds:
    """Tests for configurable precipitation/wind thresholds."""

    def setup_method(self):
        from app.config import settings
        self._orig_precip = settings.forecast_precip_threshold_pct
        self._orig_wind = settings.forecast_wind_threshold_mph
        self._original_lat = settings.forecast_latitude
        self._original_lon = settings.forecast_longitude
        settings.forecast_latitude = 35.1894
        settings.forecast_longitude = -114.0530

    def teardown_method(self):
        from app.config import settings
        settings.forecast_precip_threshold_pct = self._orig_precip
        settings.forecast_wind_threshold_mph = self._orig_wind
        settings.forecast_latitude = self._original_lat
        settings.forecast_longitude = self._original_lon

    def _mock_resp(self, precip=0, wind=0):
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "daily": {
                "weathercode": [0, 0, 0],
                "temperature_2m_max": [90, 90, 90],
                "temperature_2m_min": [70, 70, 70],
                "precipitation_probability_max": [precip, precip, precip],
                "windspeed_10m_max": [wind, wind, wind],
                "winddirection_10m_dominant": [180, 180, 180],
                "sunrise": ["2026-06-19T05:21:00"] * 3,
                "sunset": ["2026-06-19T19:53:00"] * 3,
                "time": ["2026-06-19", "2026-06-20", "2026-06-21"],
            }
        }
        resp.raise_for_status.return_value = None
        return resp

    def test_custom_precip_threshold_higher_suppresses_mention(self):
        from app.sources import forecast
        from app.config import settings
        from unittest.mock import patch
        settings.forecast_precip_threshold_pct = 50
        with patch("app.sources.forecast._session.get", return_value=self._mock_resp(precip=30)):
            result = forecast.search("weather")
        assert "precipitation" not in result.lower()

    def test_custom_precip_threshold_lower_includes_mention(self):
        from app.sources import forecast
        from app.config import settings
        from unittest.mock import patch
        settings.forecast_precip_threshold_pct = 5
        with patch("app.sources.forecast._session.get", return_value=self._mock_resp(precip=10)):
            result = forecast.search("weather")
        assert "precipitation" in result.lower()

    def test_custom_wind_threshold_higher_suppresses_mention(self):
        from app.sources import forecast
        from app.config import settings
        from unittest.mock import patch
        settings.forecast_wind_threshold_mph = 50
        with patch("app.sources.forecast._session.get", return_value=self._mock_resp(wind=20)):
            result = forecast.search("weather")
        assert "winds" not in result.lower()

    def test_custom_wind_threshold_lower_includes_mention(self):
        from app.sources import forecast
        from app.config import settings
        from unittest.mock import patch
        settings.forecast_wind_threshold_mph = 5
        with patch("app.sources.forecast._session.get", return_value=self._mock_resp(wind=10)):
            result = forecast.search("weather")
        assert "winds" in result.lower()


class TestDegreesToCardinal:
    """Tests for _degrees_to_cardinal wind direction conversion."""

    def setup_method(self):
        from app.sources.forecast import _degrees_to_cardinal
        self.convert = _degrees_to_cardinal

    def test_north(self):
        assert self.convert(0) == "north"
        assert self.convert(360) == "north"

    def test_east(self):
        assert self.convert(90) == "east"

    def test_south(self):
        assert self.convert(180) == "south"

    def test_west(self):
        assert self.convert(270) == "west"

    def test_northeast(self):
        assert self.convert(45) == "northeast"

    def test_southwest(self):
        assert self.convert(225) == "southwest"


class TestFmtTime:
    """Tests for _fmt_time ISO time formatting."""

    def setup_method(self):
        from app.sources.forecast import _fmt_time
        self.fmt = _fmt_time

    def test_morning_time(self):
        result = self.fmt("2026-06-18T08:30:00")
        assert "8:30 am" in result

    def test_afternoon_time(self):
        result = self.fmt("2026-06-18T14:00:00")
        assert "2:00 pm" in result

    def test_noon(self):
        result = self.fmt("2026-06-18T12:00:00")
        assert "12:00 pm" in result

    def test_lowercase(self):
        result = self.fmt("2026-06-18T09:00:00")
        assert result == result.lower()
