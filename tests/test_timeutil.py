"""
Tests for app/timeutil.py — UTC-to-local-time conversion, the shared
groundwork two not-yet-built design docs (Predictive Pre-Fetching, Ambient
Intent Disambiguation) both identified as a real, common dependency.

test_known_offset_produces_correct_local_hour is the single most important
test in this file: every consumer of this module's correctness depends
entirely on this one conversion being right. A wrong conversion here would
silently shift every time-of-day bucket by a fixed offset, with no error or
warning anywhere — exactly the class of bug this project's own
bulletproofing-pass culture exists to catch before it ships, not after.
"""
from unittest.mock import patch


class TestUtcStringToLocal:
    """Tests for utc_string_to_local() — the core conversion every other
    function in this module builds on."""

    def test_known_offset_produces_correct_local_hour(self):
        """The single most important test in this file. America/Phoenix is
        UTC-7 with no DST — a fixed, simple, real-world offset, and this
        project's own actual reference deployment's timezone. UTC noon must
        convert to exactly 5:00 AM local."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "America/Phoenix"
            import app.timeutil as timeutil
            result = timeutil.utc_string_to_local("2026-01-15T12:00:00Z")
        assert result.hour == 5
        assert result.minute == 0

    def test_utc_zone_is_a_no_op(self):
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            result = timeutil.utc_string_to_local("2026-01-15T12:00:00Z")
        assert result.hour == 12

    def test_result_is_timezone_aware_not_naive(self):
        """A naive datetime with no tzinfo would silently compare/sort
        incorrectly against other aware datetimes elsewhere in a real
        pipeline — confirming the result actually carries real tzinfo,
        not just numerically-correct-looking fields."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "America/Phoenix"
            import app.timeutil as timeutil
            result = timeutil.utc_string_to_local("2026-01-15T12:00:00Z")
        assert result.tzinfo is not None

    def test_dst_transition_handled_automatically_winter(self):
        """America/New_York observes DST. January is EST (UTC-5) — confirms
        zoneinfo's real calendar-aware conversion is actually being used,
        not a naive, always-wrong-half-the-year fixed offset."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "America/New_York"
            import app.timeutil as timeutil
            result = timeutil.utc_string_to_local("2026-01-15T12:00:00Z")
        assert result.hour == 7

    def test_dst_transition_handled_automatically_summer(self):
        """Same zone, July — EDT (UTC-4), one hour different from the winter
        case purely due to DST. A naive fixed-offset implementation would
        get one of these two tests right and the other wrong; zoneinfo gets
        both right automatically."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "America/New_York"
            import app.timeutil as timeutil
            result = timeutil.utc_string_to_local("2026-07-15T12:00:00Z")
        assert result.hour == 8

    def test_invalid_timezone_falls_back_to_utc_not_a_crash(self):
        """A typo in TZ/LOCAL_TIMEZONE is a real, plausible deployment
        mistake — the same class of risk morning_start_hour's own "% 24"
        defensive fix already guards against for a different setting.
        Must degrade gracefully, never raise."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "Not/A/Real/Zone"
            import app.timeutil as timeutil
            result = timeutil.utc_string_to_local("2026-01-15T12:00:00Z")
        assert result.hour == 12  # fell back to UTC, where noon stays noon

    def test_malformed_timestamp_raises_rather_than_silently_misparsing(self):
        """Deliberately NOT swallowed the way an invalid timezone name is —
        a malformed timestamp string is a real bug in whatever wrote it
        (every real row in this project's own databases is written via
        TIMESTAMP_FORMAT in the first place), not a plausible deployment
        misconfiguration, and should fail loudly rather than silently
        producing a wrong bucket under a caught exception."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            try:
                timeutil.utc_string_to_local("not-a-real-timestamp")
                assert False, "should have raised ValueError"
            except ValueError:
                pass


class TestLocalHourBucket:
    """Tests for local_hour_bucket() — the time-of-day bucketing function
    Predictive Pre-Fetching's own mining step would depend on directly."""

    def test_bucket_boundary_is_exclusive_on_the_high_end(self):
        """5:00-5:29 AM local should be one bucket, 5:30 AM should be the
        next one — confirms the // integer-division boundary is exactly
        where it's supposed to be, not off by one in either direction."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "America/Phoenix"
            import app.timeutil as timeutil
            # UTC 12:00 = 5:00 AM Phoenix
            bucket_at_exactly_5am = timeutil.local_hour_bucket("2026-01-15T12:00:00Z", bucket_minutes=30)
            # UTC 12:29 = 5:29 AM Phoenix -- still the same bucket
            bucket_just_before_530 = timeutil.local_hour_bucket("2026-01-15T12:29:00Z", bucket_minutes=30)
            # UTC 12:30 = 5:30 AM Phoenix -- the NEXT bucket
            bucket_at_exactly_530 = timeutil.local_hour_bucket("2026-01-15T12:30:00Z", bucket_minutes=30)
        assert bucket_at_exactly_5am == bucket_just_before_530
        assert bucket_at_exactly_530 == bucket_at_exactly_5am + 1

    def test_default_bucket_width_is_30_minutes(self):
        """Confirms the documented default actually matches what the
        function does when bucket_minutes isn't explicitly passed."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            assert timeutil.local_hour_bucket("2026-01-15T00:00:00Z") == 0
            assert timeutil.local_hour_bucket("2026-01-15T00:29:00Z") == 0
            assert timeutil.local_hour_bucket("2026-01-15T00:30:00Z") == 1

    def test_midnight_local_is_bucket_zero(self):
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            assert timeutil.local_hour_bucket("2026-01-15T00:00:00Z", bucket_minutes=30) == 0

    def test_last_bucket_of_the_day_is_correct(self):
        """23:59 local with a 30-minute bucket width should be the very
        last bucket of the day (47), not wrap around to 0 or overflow."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            assert timeutil.local_hour_bucket("2026-01-15T23:59:00Z", bucket_minutes=30) == 47

    def test_last_bucket_with_non_divisor_bucket_minutes(self):
        """Regression test for a real docstring/documentation gap found
        via a deliberate function-by-function read: the function's own
        arithmetic (`minutes_since_midnight // bucket_minutes`) has always
        been correct for any bucket_minutes value, but an earlier version
        of the docstring claimed the maximum return value was
        `(1440 // bucket_minutes) - 1` — only true when bucket_minutes
        evenly divides 1440 (which the documented default of 30 happens
        to do, masking the gap). For a non-divisor value like 7 minutes,
        the real last-minute-of-day (23:59) lands in bucket 205, one past
        the old docstring's claimed maximum of 204. This test locks in the
        function's own correct, real behavior directly, independent of
        whatever the docstring says, so any future change to the
        arithmetic itself would be caught here."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            assert timeutil.local_hour_bucket("2026-01-15T23:59:00Z", bucket_minutes=7) == 205
            assert timeutil.local_hour_bucket("2026-01-15T00:00:00Z", bucket_minutes=7) == 0


class TestLocalDayOfWeek:
    """Tests for local_day_of_week()."""

    def test_known_date_matches_real_calendar_weekday(self):
        """January 15, 2026 is a real, verifiable Thursday — weekday()
        convention means Monday=0, so Thursday=3."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "UTC"
            import app.timeutil as timeutil
            assert timeutil.local_day_of_week("2026-01-15T12:00:00Z") == 3

    def test_timezone_conversion_can_shift_the_calendar_day(self):
        """A UTC timestamp just after midnight UTC can be the PREVIOUS
        calendar day in a timezone west of UTC — confirms day-of-week
        genuinely reflects the converted local time, not the original UTC
        date's own weekday."""
        with patch("app.timeutil.settings") as mock_settings:
            mock_settings.local_timezone = "America/Phoenix"
            import app.timeutil as timeutil
            # 2026-01-16T01:00:00Z (a Friday in UTC) is 2026-01-15 18:00
            # Phoenix time -- still Thursday locally.
            result = timeutil.local_day_of_week("2026-01-16T01:00:00Z")
        assert result == 3  # Thursday, not Friday


class TestLocalTimezoneSetting:
    """Tests for the local_timezone setting itself (app/config.py) — its
    default-resolution behavior, isolated from whatever TZ the real test
    runner's own environment happens to have set.

    Deliberately loads app/config.py as a genuinely separate module
    instance via importlib.util, under a different name, rather than
    reloading the real app.config module in place. Found via a real,
    confirmed test-isolation bug in this exact file's own first draft:
    importlib.reload(app.config) creates a brand-new Settings class and a
    brand-new module-level `settings` object, but every OTHER already-
    imported module in this codebase (confirmed: essentially all of them)
    did `from app.config import settings`, which binds that object
    reference at import time — after the reload, app.config.settings
    points to the new object, but every other module's own `settings`
    name still points to the OLD, pre-reload object. Confirmed directly:
    this silently broke 8 unrelated tests in test_uptime_kuma.py, which
    all passed individually but failed when run after these three tests
    in the same process, because uptime_kuma.py's own `settings` reference
    was left pointing at a stale instance that doesn't see settings.TZ
    changes test fixtures in this file restore afterward. The fix:
    importlib.util.spec_from_file_location() loads config.py as a
    genuinely independent module object under a different name, never
    touching sys.modules['app.config'] at all — every other module's
    existing reference to the real, shared settings singleton is
    completely unaffected, confirmed directly.
    """

    def _load_fresh_config_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("app_config_isolated_for_test", "app/config.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_defaults_to_utc_when_tz_env_var_is_unset(self, monkeypatch):
        monkeypatch.delenv("TZ", raising=False)
        monkeypatch.delenv("LOCAL_TIMEZONE", raising=False)
        fresh = self._load_fresh_config_module()
        s = fresh.Settings(_env_file=None)
        assert s.local_timezone == "UTC"

    def test_inherits_tz_env_var_when_set(self, monkeypatch):
        monkeypatch.setenv("TZ", "America/Phoenix")
        monkeypatch.delenv("LOCAL_TIMEZONE", raising=False)
        fresh = self._load_fresh_config_module()
        s = fresh.Settings(_env_file=None)
        assert s.local_timezone == "America/Phoenix"

    def test_explicit_local_timezone_overrides_tz(self, monkeypatch):
        """An explicit LOCAL_TIMEZONE always wins over whatever TZ happens
        to be set to — confirms a deployment that wants this conversion to
        use a different zone than the container's own TZ genuinely can."""
        monkeypatch.setenv("TZ", "America/Phoenix")
        monkeypatch.setenv("LOCAL_TIMEZONE", "Europe/London")
        fresh = self._load_fresh_config_module()
        s = fresh.Settings(_env_file=None)
        assert s.local_timezone == "Europe/London"

    def test_can_override_via_constructor_like_every_other_setting(self):
        from app.config import Settings
        s = Settings(local_timezone="Asia/Tokyo")
        assert s.local_timezone == "Asia/Tokyo"


class TestResolveWindow:
    """Tests for resolve_window() — the single owner of natural-language
    window resolution shared by `changes` and `history` (Design Doc 5).

    The first block PINS the changes-compatible byte-identity: the exact
    floats _resolve_changes_hours() returned for every phrase it handled
    before the extraction must survive on WindowResolution.hours. The
    second block exercises the genuinely new BOUNDED phrases the extraction
    added for `history`, including DST correctness via an injected `now`.
    """
    from datetime import datetime, timezone, timedelta

    def _rw(self, q, now=None):
        from app.timeutil import resolve_window
        return resolve_window(q, now=now)

    # --- byte-identity of the phrases changes already resolved ---

    def test_explicit_hours(self):
        assert self._rw("changes in the last 3 hours").hours == 3.0

    def test_today_is_24(self):
        assert self._rw("what changed today").hours == 24.0

    def test_yesterday_is_48(self):
        assert self._rw("since yesterday").hours == 48.0

    def test_week_is_168(self):
        assert self._rw("this week").hours == 168.0

    def test_default_is_24(self):
        assert self._rw("any new outages").hours == 24.0

    def test_explicit_hours_beats_calendar_word(self):
        assert self._rw("today in the last 2 hours").hours == 2.0

    def test_this_morning_positive(self):
        assert self._rw("what changed this morning").hours > 0

    # --- new bounded phrases ---

    def test_last_n_days(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        assert self._rw("over the last 3 days", now=now).hours == 72.0

    def test_last_night_is_bounded(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
        r = self._rw("how cold did it get last night", now=now)
        assert r.label == "last night"
        # end (this morning) is strictly before now; start is the prior evening
        assert r.start < r.end <= now

    def test_yesterday_morning_bounded_not_48(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        r = self._rw("yesterday morning", now=now)
        assert r.label == "yesterday morning"
        # the more specific phrase must NOT fall through to bare-yesterday 48h
        assert r.hours != 48.0

    def test_over_the_weekend_label(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)  # a Monday
        r = self._rw("what happened over the weekend", now=now)
        assert r.label == "over the weekend"

    def test_since_weekday_label(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 18, 12, 0, 0, tzinfo=timezone.utc)  # Thursday
        r = self._rw("since monday", now=now)
        assert r.label.startswith("since")

    def test_all_windows_return_utc_aware_datetimes(self):
        from datetime import timezone
        r = self._rw("office co2 today")
        assert r.start.tzinfo is not None
        assert r.end.tzinfo is not None
        # normalized to UTC
        assert r.start.utcoffset() == timezone.utc.utcoffset(None)

    def test_dst_correctness_across_spring_forward(self):
        # US spring-forward 2026: 2026-03-08 02:00 local. Query "this
        # morning" just after the transition should give a positive,
        # sane hours value regardless of the skipped hour.
        from datetime import datetime, timezone
        from unittest.mock import patch
        with patch("app.timeutil.settings") as msettings:
            msettings.local_timezone = "America/Los_Angeles"
            msettings.morning_start_hour = 6
            msettings.work_start_hour = 9
            now = datetime(2026, 3, 8, 17, 0, 0, tzinfo=timezone.utc)  # 09:00 PST-ish
            r = self._rw("what changed this morning", now=now)
        assert r.hours > 0


class TestResolveWindowAuditRegressions:
    """Pins for the post-implementation audit findings in resolve_window."""

    def _rw(self, q, now=None):
        from app.timeutil import resolve_window
        return resolve_window(q, now=now)

    def test_bare_sun_does_not_mean_sunday(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc)  # Wednesday
        r = self._rw("is the sun out today", now=now)
        assert r.label == "today"
        assert r.hours == 24.0

    def test_bare_sat_does_not_mean_saturday(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc)
        r = self._rw("the cat sat on the sensor", now=now)
        assert "Sat" not in r.label

    def test_prefixed_abbreviation_still_works(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 17, 18, 0, 0, tzinfo=timezone.utc)  # Wednesday
        r = self._rw("what happened since mon", now=now)
        assert r.label == "since Mon"

    def test_last_friday_on_a_friday_means_a_week_ago(self):
        from datetime import datetime, timezone
        fri = datetime(2026, 6, 19, 18, 0, 0, tzinfo=timezone.utc)
        r = self._rw("how cold was it last friday", now=fri)
        assert r.hours > 150  # ~7 days back, not this morning

    def test_since_friday_on_a_friday_means_today(self):
        from datetime import datetime, timezone
        fri = datetime(2026, 6, 19, 18, 0, 0, tzinfo=timezone.utc)
        r = self._rw("what happened since friday", now=fri)
        assert r.hours < 30

    def test_this_afternoon_anchors_at_noon(self):
        from datetime import datetime, timezone
        # 15:00 UTC-as-local -> 3 hours since noon, and its own label.
        now = datetime(2026, 6, 17, 15, 0, 0, tzinfo=timezone.utc)
        r = self._rw("what changed this afternoon", now=now)
        assert r.label == "this afternoon"
        assert 2.5 < r.hours < 3.5

    def test_this_evening_still_anchors_at_18(self):
        from datetime import datetime, timezone
        now = datetime(2026, 6, 17, 20, 0, 0, tzinfo=timezone.utc)
        r = self._rw("what changed this evening", now=now)
        assert r.label == "this evening"
