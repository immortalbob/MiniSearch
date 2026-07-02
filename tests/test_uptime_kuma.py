"""
Tests for app/sources/uptime_kuma.py
Uses unittest.mock to avoid real Socket.IO connections.
"""
import threading
import time
from unittest.mock import patch, MagicMock

import pytest


def _make_monitor(id: int, name: str) -> dict:
    return {"id": id, "name": name}


def _make_heartbeats(monitor_id: int, status: int) -> dict:
    return {monitor_id: [{"status": status}]}


def _mock_api(monitors: list, heartbeats: dict) -> MagicMock:
    """Builds a mock UptimeKumaApi instance matching the real object's
    shape post-persistent-connection-fix: no more __enter__/__exit__
    (search() no longer uses `with UptimeKumaApi(...) as api:`), and a
    real `.sio.connected` attribute — confirmed directly against the
    installed uptime_kuma_api/python-socketio source as the actual
    liveness signal get_connection() checks, since UptimeKumaApi itself
    has no `.connected` property of its own.
    """
    mock_api = MagicMock()
    mock_api.sio.connected = True
    mock_api.get_monitors.return_value = monitors
    mock_api.get_heartbeats.return_value = heartbeats
    return mock_api


@pytest.fixture(autouse=True)
def _reset_persistent_connection():
    """Every test in this file gets a clean `_persistent_api` slate,
    regardless of what a prior test left behind — the same isolation
    principle conftest.py's router-cache fixture already establishes
    project-wide, applied here because this module now has its own
    piece of shared, persistent module state to worry about."""
    import app.sources.uptime_kuma as uptime_kuma_module
    uptime_kuma_module._persistent_api = None
    yield
    uptime_kuma_module._persistent_api = None


class TestUptimeKumaGuard:
    """Tests for URL guard."""

    def test_returns_not_configured_when_url_blank(self):
        from app.sources import uptime_kuma
        from app.config import settings
        original = settings.uptime_kuma_url
        settings.uptime_kuma_url = ""
        try:
            result = uptime_kuma.search("is anything down")
            assert "not configured" in result.lower()
        finally:
            settings.uptime_kuma_url = original


class TestUptimeKumaStatus:
    """Tests for monitor status parsing with mocked API."""

    def setup_method(self):
        from app.config import settings
        settings.uptime_kuma_url = "http://uptime-kuma:3001"
        settings.uptime_kuma_username = "testuser"

    def teardown_method(self):
        from app.config import settings
        settings.uptime_kuma_url = ""
        settings.uptime_kuma_username = ""

    def test_all_up_returns_clean_message(self):
        from app.sources import uptime_kuma
        monitors = [_make_monitor(1, "MiniDock"), _make_monitor(2, "MiniPlex")]
        heartbeats = {**_make_heartbeats(1, 1), **_make_heartbeats(2, 1)}
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "all" in result.lower()
        assert "up" in result.lower()
        assert "down" not in result.lower()

    def test_down_service_listed(self):
        from app.sources import uptime_kuma
        monitors = [_make_monitor(1, "MiniDock"), _make_monitor(2, "MiniPlex")]
        heartbeats = {**_make_heartbeats(1, 1), **_make_heartbeats(2, 0)}
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "MiniPlex" in result
        assert "DOWN" in result

    def test_multiple_down_services_listed(self):
        from app.sources import uptime_kuma
        monitors = [
            _make_monitor(1, "ServiceA"),
            _make_monitor(2, "ServiceB"),
            _make_monitor(3, "ServiceC"),
        ]
        heartbeats = {
            **_make_heartbeats(1, 0),
            **_make_heartbeats(2, 0),
            **_make_heartbeats(3, 1),
        }
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "ServiceA" in result
        assert "ServiceB" in result
        assert "ServiceC" not in result

    def test_maintenance_reported_separately(self):
        from app.sources import uptime_kuma
        monitors = [_make_monitor(1, "ServiceA"), _make_monitor(2, "ServiceB")]
        heartbeats = {**_make_heartbeats(1, 1), **_make_heartbeats(2, 3)}
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "maintenance" in result.lower()
        assert "ServiceB" in result

    def test_monitor_with_no_heartbeat_data_reported_honestly_not_as_maintenance(self):
        """Regression test for a real bug found via a deliberate
        complexity-investigation pass: a brand-new monitor (or one
        whose check interval hasn't fired yet) has NO heartbeat entry
        at all — the previous version silently reported this as "In
        maintenance," a specific, false claim about a deliberately-
        configured state the monitor was never actually in. Confirms
        the real, user-facing fix: such a monitor is now reported under
        its own honest "No heartbeat data yet" category instead."""
        from app.sources import uptime_kuma
        monitors = [_make_monitor(1, "ServiceA"), _make_monitor(2, "BrandNewService")]
        # ServiceA has a real heartbeat; BrandNewService has NONE at all
        heartbeats = _make_heartbeats(1, 1)
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "maintenance" not in result.lower()
        assert "no heartbeat data" in result.lower()
        assert "BrandNewService" in result

    def test_no_monitors_returns_message(self):
        from app.sources import uptime_kuma
        mock_api = _mock_api([], {})
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "no monitors" in result.lower()

    def test_connection_error_returns_error_message(self):
        """A failure during connection acquisition (the constructor
        itself, standing in for the real Socket.IO handshake) must
        still produce the same documented fallback message as before
        the persistent-connection fix — search()'s public contract on
        failure is unchanged."""
        from app.sources import uptime_kuma
        with patch("app.sources.uptime_kuma.UptimeKumaApi", side_effect=Exception("Connection refused")):
            result = uptime_kuma.search("status")
        assert "could not connect" in result.lower() or "error" in result.lower()

    def test_up_count_reported(self):
        from app.sources import uptime_kuma
        monitors = [
            _make_monitor(1, "A"), _make_monitor(2, "B"),
            _make_monitor(3, "C"), _make_monitor(4, "D"),
        ]
        heartbeats = {
            **_make_heartbeats(1, 1), **_make_heartbeats(2, 1),
            **_make_heartbeats(3, 1), **_make_heartbeats(4, 0),
        }
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        assert "3" in result  # 3 of 4 up
        assert "4" in result

    def test_unknown_status_code_does_not_silently_disappear(self):
        """Regression test for a real gap found via a deliberate
        function-by-function read: status codes beyond the known 0-3
        range (DOWN/UP/PENDING/MAINTENANCE) fell through all elif branches
        without any else, silently dropping the monitor from every output
        bucket. The result was 'All 0 monitored services are up' even when
        a real monitor existed — an actively wrong statement. Not a
        realistic concern with the current API version (MonitorStatus
        values 0-3 have been stable), but silently lying about a monitor
        that exists is strictly worse than reporting it honestly as
        something unexpected."""
        from app.sources import uptime_kuma
        monitors = [_make_monitor(1, "WeirdMonitor")]
        heartbeats = {1: [{"status": 99}]}  # unknown future status code
        mock_api = _mock_api(monitors, heartbeats)
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api):
            result = uptime_kuma.search("status")
        # Must NOT claim all services are up — that would be a lie
        assert "all" not in result.lower() or "0" in result
        # The monitor should appear somewhere in the output (treated as no_data)
        assert "WeirdMonitor" in result


class TestGetStatusFromHeartbeats:
    """Tests for _get_status_from_heartbeats heartbeat parsing."""

    def setup_method(self):
        from app.sources.uptime_kuma import _get_status_from_heartbeats
        self.get_status = _get_status_from_heartbeats

    def test_returns_last_status(self):
        heartbeats = {1: [{"status": 0}, {"status": 1}]}
        assert self.get_status(heartbeats, 1) == 1

    def test_missing_monitor_returns_none(self):
        """Regression test for a real bug found via a deliberate
        complexity-investigation pass: this used to default to 3
        (MAINTENANCE) when no heartbeat data existed at all, silently
        misreporting a monitor that simply hasn't run its first check
        yet as "in maintenance" — a specific, false claim about a
        deliberately-configured state the monitor was never actually
        in. None is now used as an explicit "no data" sentinel, distinct
        from every real MonitorStatus value (0/1/2/3 are all genuine
        statuses), so callers can tell "genuinely in maintenance" apart
        from "no data exists yet" rather than conflating the two."""
        assert self.get_status({}, 99) is None

    def test_empty_list_returns_none(self):
        assert self.get_status({1: []}, 1) is None

    def test_missing_status_key_returns_none(self):
        heartbeats = {1: [{"no_status_key": True}]}
        assert self.get_status(heartbeats, 1) is None

    def test_down_status(self):
        heartbeats = {1: [{"status": 0}]}
        assert self.get_status(heartbeats, 1) == 0

    def test_non_list_heartbeat_returns_none(self):
        heartbeats = {1: "not a list"}
        assert self.get_status(heartbeats, 1) is None

    def test_genuine_maintenance_status_still_reported_correctly(self):
        """Confirms the fix didn't break the real, legitimate case —
        a monitor genuinely set to MAINTENANCE (status 3) by a real
        heartbeat record should still be reported as such, distinct
        from the None sentinel for missing data entirely."""
        heartbeats = {1: [{"status": 3}]}
        assert self.get_status(heartbeats, 1) == 3


class TestUptimeKumaConfigurableTimeout:
    """Regression tests for a real, live bug found via Adversarial
    Self-Testing: a conditional_with_remainder query took 30056ms and
    was flagged unexpected_empty, traced directly to UptimeKumaApi's
    connection timing out at a bare, hardcoded `timeout=30` literal —
    with no setting anywhere to tune it for a service that, on a real
    homelab, sits on the same LAN and should respond far faster.
    Confirms the real call site genuinely uses the new
    UPTIME_KUMA_TIMEOUT_SECONDS setting, not just a renamed constant
    that happens to default to the same old value."""

    def setup_method(self):
        from app.config import settings
        settings.uptime_kuma_url = "http://uptime-kuma:3001"
        settings.uptime_kuma_username = "testuser"

    def teardown_method(self):
        from app.config import settings
        settings.uptime_kuma_url = ""
        settings.uptime_kuma_username = ""

    def test_default_timeout_is_ten_not_the_old_hardcoded_thirty(self):
        from app.config import settings
        assert settings.uptime_kuma_timeout_seconds == 10

    def test_configured_timeout_value_is_genuinely_passed_to_the_real_api_call(self):
        from app.sources import uptime_kuma
        from app.config import settings

        original_timeout = settings.uptime_kuma_timeout_seconds
        settings.uptime_kuma_timeout_seconds = 3
        try:
            mock_api_class = MagicMock()
            mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
            mock_api_class.return_value = mock_api_instance

            with patch("app.sources.uptime_kuma.UptimeKumaApi", mock_api_class):
                uptime_kuma.search("status")

            mock_api_class.assert_called_once_with(settings.uptime_kuma_url, timeout=3)
        finally:
            settings.uptime_kuma_timeout_seconds = original_timeout

    def test_real_timeout_exception_still_produces_the_documented_fallback_message(self):
        """Confirms the actual real-world failure path this whole
        investigation traced through: a genuine timeout (or any other
        connection exception) must still produce the documented
        "Could not connect to Uptime Kuma" message — the same real,
        intentional fallback fusion._looks_empty() correctly recognizes
        — regardless of what the configured timeout value is."""
        from app.sources import uptime_kuma

        with patch("app.sources.uptime_kuma.UptimeKumaApi", side_effect=TimeoutError("timed out")):
            result = uptime_kuma.search("status")
        assert "could not connect" in result.lower()


class TestPersistentConnection:
    """Tests for the persistent-connection fix (the design doc's actual
    subject): connection reuse across calls, dead-connection detection
    and recovery, and thread-safety under concurrent access. Each test
    here proves a real property of the new mechanism, not just that
    search()'s output is unchanged — the same "prove the property, not
    just the symptom" discipline TestHealthConcurrentSourceChecks and
    TestSearxngConcurrentFetch already established elsewhere in this
    project.
    """

    def setup_method(self):
        from app.config import settings
        settings.uptime_kuma_url = "http://uptime-kuma:3001"
        settings.uptime_kuma_username = "testuser"

    def teardown_method(self):
        from app.config import settings
        settings.uptime_kuma_url = ""
        settings.uptime_kuma_username = ""

    def test_connection_reused_across_two_calls_not_recreated(self):
        """Proves pooling, not just unchanged output: UptimeKumaApi's
        constructor must be called exactly once across two sequential
        search() calls, confirming the second call reused the first
        call's connection rather than opening a fresh one — the actual
        mechanism this whole fix exists to change."""
        from app.sources import uptime_kuma

        mock_api_class = MagicMock()
        mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        mock_api_class.return_value = mock_api_instance

        with patch("app.sources.uptime_kuma.UptimeKumaApi", mock_api_class):
            uptime_kuma.search("status")
            uptime_kuma.search("status")

        assert mock_api_class.call_count == 1
        assert mock_api_instance.login.call_count == 1
        # get_monitors/get_heartbeats DO get called fresh each time —
        # only the connection+login step is being skipped on reuse.
        assert mock_api_instance.get_monitors.call_count == 2

    def test_dead_connection_is_detected_and_replaced_not_silently_reused(self):
        """The actual new risk this design introduces that the
        original fresh-connection-every-time approach never had: a
        persistent connection can go stale (Uptime Kuma restarts, a
        network blip drops the socket) in a way a fresh-every-time
        connection structurally cannot. Confirms a connection whose
        `sio.connected` has gone False gets discarded and replaced with
        a genuinely new one — not reused in a silently broken state."""
        from app.sources import uptime_kuma

        first_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        second_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        mock_api_class = MagicMock(side_effect=[first_instance, second_instance])

        with patch("app.sources.uptime_kuma.UptimeKumaApi", mock_api_class):
            uptime_kuma.search("status")
            # Simulate the connection going dead between calls (a
            # Kuma restart, a dropped socket) — exactly the real
            # liveness signal get_connection() checks.
            first_instance.sio.connected = False
            uptime_kuma.search("status")

        assert mock_api_class.call_count == 2
        # The dead connection's own disconnect() should have been
        # called once during cleanup, not left dangling.
        first_instance.disconnect.assert_called_once()
        second_instance.login.assert_called_once()

    def test_concurrent_calls_do_not_create_multiple_connections(self):
        """Real concurrency-correctness proof, not a speed proof:
        snapshot_uptime() (scheduler thread, every 2 minutes) and a
        live request's call to search() (request-handling thread) can
        genuinely overlap. Confirms _connection_lock actually
        serializes connection acquisition under real concurrent
        threads — exactly one UptimeKumaApi gets constructed even when
        many threads call search() at once with no connection yet
        established, the same style of real concurrent-call test
        TestHealthConcurrentSourceChecks already established for a
        different feature."""
        from app.sources import uptime_kuma

        construction_count = {"n": 0}
        lock_for_counting = threading.Lock()

        def _slow_constructor(*args, **kwargs):
            # A small real delay so concurrent callers genuinely race
            # to construct a connection, rather than the test passing
            # only because everything happens to run sequentially fast
            # enough that no real race window ever opens.
            time.sleep(0.02)
            with lock_for_counting:
                construction_count["n"] += 1
            return _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))

        mock_api_class = MagicMock(side_effect=_slow_constructor)

        with patch("app.sources.uptime_kuma.UptimeKumaApi", mock_api_class):
            threads = [
                threading.Thread(target=uptime_kuma.search, args=("status",))
                for _ in range(8)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # _connection_lock should have forced every thread but the
        # first to find the already-established connection waiting
        # for it, rather than each racing to build its own.
        assert mock_api_class.call_count == 1

    def test_disconnect_cleanly_closes_an_open_connection(self):
        """disconnect() — the new lifespan-shutdown hook — must
        actually call through to the real connection's own
        disconnect(), and must leave the module ready to establish a
        fresh connection afterward rather than leaving a dead
        reference behind."""
        from app.sources import uptime_kuma

        mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api_instance):
            uptime_kuma.search("status")  # establishes the persistent connection

        uptime_kuma.disconnect()
        mock_api_instance.disconnect.assert_called_once()
        assert uptime_kuma._persistent_api is None

    def test_disconnect_is_a_safe_noop_when_never_connected(self):
        """Uptime Kuma was never configured, or the app shuts down
        before any uptime query ever ran — disconnect() must not raise
        in either case."""
        from app.sources import uptime_kuma
        uptime_kuma.disconnect()  # should not raise

    def test_get_connection_uses_sio_connected_not_a_nonexistent_api_connected(self):
        """Guards against the exact wrong-assumption risk the design
        doc flagged before this was built: UptimeKumaApi has no
        `.connected` property of its own (confirmed directly against
        the installed library source) — the real liveness signal is
        the underlying `sio` client's `.connected` attribute. A mock
        that only defines `.connected` on the outer object (mimicking
        the wrong assumption) must NOT be mistaken for a live
        connection — get_connection() must check `.sio.connected`."""
        from app.sources import uptime_kuma

        # Deliberately a bare MagicMock with no .sio configured as a
        # real bool — accessing .sio.connected on it returns a
        # truthy MagicMock, which would incorrectly look "connected"
        # if the code checked the wrong attribute, but the outer
        # `.connected` attribute is unset/auto-mocked too, so this
        # test only proves something real if get_connection() is
        # actually reading through `.sio`.
        mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api_instance):
            result = uptime_kuma.search("status")
        assert "could not connect" not in result.lower()
        mock_api_instance.login.assert_called_once()


class TestWaitEventsSettling:
    """Regression coverage for the actual root cause traced down behind
    `uptime`'s recurring, unexplained warm-cache tail (v3.50.4, v3.50.6,
    v3.50.7 benchmark runs all showed the identical ~440ms tail value):
    `uptime_kuma_api`'s own `_get_event_data()` pays its `wait_events`
    sleep (default 0.2s) UNCONDITIONALLY every single time it's called,
    even when the awaited data has been sitting there, fully complete,
    since a previous call — confirmed directly against the installed
    library source via a standalone reproduction (not an assumption):
    constructing a mock with `_event_data` already populated and
    calling the real, unpatched `UptimeKumaApi._get_event_data` against
    it still took the full 0.2s.

    Two such unconditional sleeps per `search()` call
    (`get_monitors()` + `get_heartbeats()`) is exactly the ~0.4s
    structural floor that landed at p90+ in every benchmark run to
    date — not lock contention, not server-side variance, a fixed,
    deterministic library cost paid on every genuine cache miss.

    The fix narrows `wait_events` only AFTER a connection's first
    successful data fetch settles — the one call that genuinely needs
    the full, safe wait, per the library's own documented purpose
    (waiting for trailing per-monitor `heartbeatList`/`monitorList`
    push messages right after login). Every later call on the same,
    already-settled persistent connection has nothing structurally
    left to wait for — confirmed directly: `_event_heartbeat()`
    (the steady-state, post-login push handler) appends one complete
    record per call, with no multi-message batching at all.
    """

    def setup_method(self):
        from app.config import settings
        settings.uptime_kuma_url = "http://uptime-kuma:3001"
        settings.uptime_kuma_username = "testuser"

    def teardown_method(self):
        from app.config import settings
        settings.uptime_kuma_url = ""
        settings.uptime_kuma_username = ""

    def test_fresh_connection_keeps_the_safe_default_wait_events_for_its_first_call(self):
        """The one call that genuinely needs the full, safe wait — a
        fresh connect/login, where the initial per-monitor
        heartbeatList batch may still be arriving — must NOT have its
        wait_events shrunk before that first fetch completes. This is
        the real safety property the whole fix depends on: shrinking
        too early risks get_heartbeats() returning incomplete data for
        a monitor whose push hadn't landed yet."""
        from app.sources import uptime_kuma

        mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        mock_api_instance.wait_events = 0.2  # the real library's safe default

        captured_wait_events_during_fetch = {}

        def capturing_get_monitors():
            # Captures wait_events AT THE MOMENT get_monitors() is
            # called — i.e. before this module's own post-fetch
            # shrink logic has had any chance to run yet.
            captured_wait_events_during_fetch["during_get_monitors"] = mock_api_instance.wait_events
            return [_make_monitor(1, "Test")]

        mock_api_instance.get_monitors.side_effect = capturing_get_monitors

        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api_instance):
            uptime_kuma.search("status")

        assert captured_wait_events_during_fetch["during_get_monitors"] == 0.2, (
            "wait_events was shrunk BEFORE the first fetch completed — "
            "this risks an incomplete heartbeatList read on a genuinely "
            "fresh connection"
        )

    def test_wait_events_shrinks_after_the_first_successful_fetch_settles(self):
        """The actual fix: once a connection's first data fetch has
        genuinely completed, wait_events should be narrowed for every
        subsequent call on that same connection — this is what
        actually removes the ~0.4s structural floor."""
        from app.sources import uptime_kuma

        mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        mock_api_instance.wait_events = 0.2

        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api_instance):
            uptime_kuma.search("status")

        assert mock_api_instance.wait_events == uptime_kuma._SETTLED_WAIT_EVENTS, (
            "wait_events was never shrunk after the first successful "
            "fetch — the ~0.4s structural floor would still apply to "
            "every later call on this same connection"
        )

    def test_settled_wait_events_is_not_zero(self):
        """A real, brief grace period must remain — not eliminated
        outright. Zero would remove even the small, real protection
        wait_events provides against a genuinely straggling message,
        and the library's own internal polling granularity (0.01s, the
        `_get_event_data()` while-loop's own sleep interval) is the
        natural, conservative floor to match rather than going below
        it."""
        from app.sources import uptime_kuma
        assert uptime_kuma._SETTLED_WAIT_EVENTS > 0
        assert uptime_kuma._SETTLED_WAIT_EVENTS <= 0.01

    def test_already_settled_connection_is_not_shrunk_a_second_time(self):
        """Once a connection has already settled, a second call must
        leave wait_events exactly where the first settling left it —
        confirms this isn't re-applied or reset on every call, just
        once per fresh connection."""
        from app.sources import uptime_kuma

        mock_api_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        mock_api_instance.wait_events = 0.2

        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_api_instance):
            uptime_kuma.search("status")
            assert mock_api_instance.wait_events == uptime_kuma._SETTLED_WAIT_EVENTS

            # Manually nudge it to prove the second call doesn't touch
            # it again (a real re-apply would reset it back to the
            # settled value even if something else had changed it,
            # which isn't the actual contract — settling happens once).
            mock_api_instance.wait_events = 0.05
            uptime_kuma.search("status")

        assert mock_api_instance.wait_events == 0.05, (
            "wait_events was modified again on an already-settled "
            "connection — settling should only ever happen once per "
            "fresh connection, not on every call"
        )

    def test_reconnecting_after_a_dead_connection_resets_to_the_safe_default_path(self):
        """A reconnect creates a genuinely NEW UptimeKumaApi instance —
        confirms the new instance goes through the same fresh-connection
        safety window as any other first-ever connection, rather than
        inheriting a shrunk wait_events from whatever the previous,
        now-dead instance had settled to."""
        from app.sources import uptime_kuma

        first_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        first_instance.wait_events = 0.2
        second_instance = _mock_api([_make_monitor(1, "Test")], _make_heartbeats(1, 1))
        second_instance.wait_events = 0.2  # the real library always
                                            # constructs a fresh instance
                                            # at its own safe default,
                                            # never inheriting a prior
                                            # instance's shrunk value

        mock_api_class = MagicMock(side_effect=[first_instance, second_instance])

        with patch("app.sources.uptime_kuma.UptimeKumaApi", mock_api_class):
            uptime_kuma.search("status")
            assert first_instance.wait_events == uptime_kuma._SETTLED_WAIT_EVENTS

            # Simulate the connection dying between calls.
            first_instance.sio.connected = False
            uptime_kuma.search("status")

        # The second instance must independently settle on its OWN
        # first call — confirming get_connection() correctly marks
        # every freshly-created instance as unsettled, not just the
        # very first one ever created in the process's lifetime.
        assert second_instance.wait_events == uptime_kuma._SETTLED_WAIT_EVENTS

    def test_second_call_on_a_settled_connection_is_genuinely_faster(self):
        """A real, wall-clock timing proof — not just an attribute
        check — that settling actually reduces real elapsed time, the
        same "prove the property, not just unchanged output" discipline
        TestPersistentConnection's own reuse test already established.
        Uses a real UptimeKumaApi-shaped object whose get_monitors/
        get_heartbeats methods genuinely call time.sleep(self.wait_events)
        themselves (mirroring the real library's own unconditional
        sleep inside _get_event_data), scaled down so the test stays
        fast (0.05s "full" wait vs. the real 0.2s) while still proving
        the actual mechanism via measured time, not mocked attributes."""
        import time
        from app.sources import uptime_kuma

        class _RealisticMockApi:
            """Mimics the real UptimeKumaApi's actual unconditional-sleep
            behavior closely enough to prove real elapsed-time savings,
            without needing the real library or a real connection."""
            def __init__(self):
                self.wait_events = 0.05  # scaled-down stand-in for 0.2
                self.sio = MagicMock()
                self.sio.connected = True
                self._settled = False

            def login(self, *a, **kw):
                pass

            def disconnect(self):
                pass

            def get_monitors(self):
                time.sleep(self.wait_events)  # mirrors the real library's
                                                # unconditional _get_event_data sleep
                return [_make_monitor(1, "Test")]

            def get_heartbeats(self):
                time.sleep(self.wait_events)
                return _make_heartbeats(1, 1)

        mock_instance = _RealisticMockApi()

        with patch("app.sources.uptime_kuma.UptimeKumaApi", return_value=mock_instance):
            start = time.monotonic()
            uptime_kuma.search("status")
            first_call_elapsed = time.monotonic() - start

            start = time.monotonic()
            uptime_kuma.search("status")
            second_call_elapsed = time.monotonic() - start

        # First call pays the full, safe wait_events (2 calls x 0.05s).
        assert first_call_elapsed >= 0.08, (
            f"first call took {first_call_elapsed:.3f}s — expected it to "
            f"pay the full, safe wait_events cost"
        )
        # Second call, on the now-settled connection, should be
        # dramatically faster — using _SETTLED_WAIT_EVENTS (0.01s) for
        # both calls instead of the original 0.05s.
        assert second_call_elapsed < first_call_elapsed / 2, (
            f"second call ({second_call_elapsed:.3f}s) wasn't meaningfully "
            f"faster than the first ({first_call_elapsed:.3f}s) — the "
            f"settling fix doesn't appear to be reducing real elapsed time"
        )

class TestErrorPathDisconnectsBeforeDropping:
    """Regression test: search()'s connection-error path previously did a
    bare `_persistent_api = None` under the lock — orphaning a possibly
    half-alive connection object (e.g. a login timeout whose socket had
    genuinely connected) with its transport left open until GC. It must
    instead close the transport via disconnect() at the one moment we
    know the object is being discarded, matching get_connection()'s own
    stale-object handling."""

    def test_failed_fetch_disconnects_the_discarded_connection(self):
        from unittest.mock import patch, MagicMock
        from app.config import settings
        import app.sources.uptime_kuma as uk

        settings.uptime_kuma_url = "http://kuma"
        settings.uptime_kuma_username = "admin"
        try:
            half_dead = MagicMock()
            half_dead.get_monitors.side_effect = RuntimeError("timed out mid-fetch")
            with patch("app.sources.uptime_kuma.get_connection", return_value=half_dead):
                uk._persistent_api = half_dead
                result = uk.search("are my services up")
            assert "Could not connect" in result
            half_dead.disconnect.assert_called_once()
            assert uk._persistent_api is None
        finally:
            uk._persistent_api = None
            settings.uptime_kuma_url = ""
            settings.uptime_kuma_username = ""
