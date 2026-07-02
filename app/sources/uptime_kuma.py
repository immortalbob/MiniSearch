import logging
import threading
from uptime_kuma_api import UptimeKumaApi
from app.config import settings

_LOGGER = logging.getLogger(__name__)

# Persistent Socket.IO connection, reused across calls rather than opened
# fresh on every search()/snapshot_uptime() invocation. See
# get_connection() below for why this exists and what was confirmed
# directly against the installed uptime_kuma_api/python-socketio source
# before this was built — the short version: UptimeKumaApi has no
# `.connected` property of its own (an assumption an earlier draft of
# this fix made and would have shipped wrong), but its underlying
# `sio` (a `python-socketio` `Client`) does — `sio.connected` is a real,
# public attribute on `socketio.base_client.BaseClient`, `True` after a
# successful connect, `False` after any disconnect.
_persistent_api: "UptimeKumaApi | None" = None

# uptime_kuma_api's own `wait_events` (default 0.2s) exists for a real,
# documented reason: after a `monitorList`/`heartbeatList` event type
# is first seen, the client has no protocol-level signal for "that was
# the last message of this type" — Uptime Kuma's server emits one
# `heartbeatList` push per monitor, so `wait_events` is a deliberate
# grace period to let any remaining per-monitor pushes land before the
# client treats the batch as complete. Confirmed directly in the
# installed library source: `_get_event_data()` pays this sleep
# UNCONDITIONALLY once the awaited event type has any data at all —
# including on a long-settled, already-warm persistent connection,
# where there is structurally nothing left to wait for (the initial
# post-login batch settled calls ago, and every later update arrives
# as a single, complete `heartbeat` push per monitor — confirmed via
# `_event_heartbeat()`, which appends one complete record per call,
# no multi-message batching at all). Two such unconditional sleeps per
# `search()` call (`get_monitors()` + `get_heartbeats()`) is exactly
# the ~0.4s structural floor traced directly to the recurring,
# unexplained `uptime` warm-cache tail across the v3.50.4/v3.50.6/
# v3.50.7 benchmark runs — not lock contention, not server-side
# variance, a fixed, deterministic library cost paid on every genuine
# cache miss.
#
# The real risk window for shortening `wait_events` is narrow and
# specific: only the FIRST call after a fresh connect/login, while the
# initial per-monitor `heartbeatList` batch may still be arriving.
# Every later call on the SAME, already-settled connection has nothing
# left to wait for. _SETTLED_WAIT_EVENTS is applied only after that
# first call completes — see get_connection()'s `_settled` attribute
# below — never at construction time, so the safe, full 0.2s default
# always covers the one call that genuinely needs it.
_SETTLED_WAIT_EVENTS = 0.01  # matches _get_event_data()'s own internal
                              # poll granularity — a real, brief grace
                              # period, not zero.

# uptime_kuma_api's underlying python-socketio client isn't documented
# as thread-safe for concurrent calls from multiple threads, and
# Mnemolis can genuinely call search() from more than one thread at
# once — a live request and the 2-minute snapshot_uptime() scheduler
# tick landing close together. This lock serializes access to the
# single shared connection rather than risk a real, hard-to-debug race
# inside the socketio client.
_connection_lock = threading.Lock()


def _get_status_from_heartbeats(heartbeats: dict, monitor_id: int) -> int | None:
    """Extract most recent status from heartbeats for a given monitor.

    Returns None — not a status code — when no real heartbeat data
    exists at all. Found via a deliberate complexity-investigation pass:
    the previous version defaulted to 3 (MAINTENANCE) when no heartbeat
    existed, silently misreporting a monitor that simply hasn't run its
    first check yet (a brand-new monitor, or one whose check interval
    hasn't fired since Uptime Kuma's own restart) as "in maintenance" —
    a specific, false claim about a deliberately-configured state the
    monitor was never actually in. None is used as the sentinel
    specifically because 0/1/2/3 are all real, valid MonitorStatus
    values (DOWN/UP/PENDING/MAINTENANCE) — reusing any of them to also
    mean "no data" would create the exact same kind of ambiguity this
    fix is meant to close.
    """
    hb = heartbeats.get(monitor_id, [])
    if not isinstance(hb, list):
        hb = []
    last = None
    for item in hb:
        if isinstance(item, dict):
            last = item
    return last.get("status") if last else None


def get_connection() -> "UptimeKumaApi":
    """Returns the persistent connection, creating and logging in on
    first use, reconnecting if a prior call left it in a dead state.

    Never raises on the dead-connection check itself — that's the
    caller's job, the same way it already is for every other failure
    mode in this module. Callers must hold _connection_lock.

    `UptimeKumaApi(...)` connects in its own `__init__` (confirmed
    directly in the installed library source), so constructing it here
    already performs the Socket.IO handshake — no separate `.connect()`
    call is needed on top of it.
    """
    global _persistent_api
    if _persistent_api is None or not _persistent_api.sio.connected:
        if _persistent_api is not None:
            try:
                _persistent_api.disconnect()
            except Exception:
                pass  # already dead; nothing real to clean up
        _persistent_api = UptimeKumaApi(
            settings.uptime_kuma_url, timeout=settings.uptime_kuma_timeout_seconds
        )
        _persistent_api.login(settings.uptime_kuma_username, settings.uptime_kuma_password)
        # Genuinely fresh connection — the initial post-login
        # monitorList/heartbeatList batch may still be arriving one
        # per-monitor push at a time, so this instance keeps
        # UptimeKumaApi's own safe default `wait_events` (0.2s) until
        # its first real data fetch completes. _settled flips True in
        # search() right after that first fetch succeeds — see the
        # module-level comment above _SETTLED_WAIT_EVENTS for why this
        # specific call is the one that genuinely needs the full wait
        # and every later one on this same instance doesn't.
        _persistent_api._settled = False
    return _persistent_api


def warm_connection() -> None:
    """Eagerly open the persistent connection at startup, so the first
    real request or snapshot tick doesn't pay the Socket.IO handshake
    and login cost. Called from main.py's lifespan before the scheduler
    and request handlers start.

    Distinct from calling get_connection() directly: this acquires
    _connection_lock as required by get_connection()'s documented
    contract ("Callers must hold _connection_lock"), which main.py
    cannot do directly since get_connection() is an internal function
    not designed for bare external calling. In practice, no other thread
    can be calling get_connection() during startup (the scheduler hasn't
    started and the app isn't yet accepting requests when this runs), so
    the race _connection_lock exists to prevent can't actually occur
    here — but the contract violation is real and cheap to close,
    and a future startup-sequence change could expose the latent race
    if left unfixed.
    """
    if not settings.uptime_kuma_url or not settings.uptime_kuma_username:
        return
    try:
        with _connection_lock:
            get_connection()
    except Exception as e:
        _LOGGER.warning("Uptime Kuma warm connection failed: %s — will retry on first real request", e)


def disconnect() -> None:
    """Cleanly closes the persistent connection, if one exists.

    Safe to call when no connection has ever been opened (e.g. Uptime
    Kuma was never configured) — no-ops rather than raising. Intended
    for app shutdown via main.py's lifespan, mirroring the pattern
    already used there for the snapshot scheduler.
    """
    global _persistent_api
    with _connection_lock:
        if _persistent_api is not None:
            try:
                _persistent_api.disconnect()
            except Exception:
                pass  # shutting down anyway; nothing left to do with the error
            _persistent_api = None


def search(query: str) -> str:
    """
    Query Uptime Kuma for monitor status.
    Returns a summary of any down services, or confirmation that all are up.
    """
    global _persistent_api

    if not settings.uptime_kuma_url or not settings.uptime_kuma_username:
        return "Uptime Kuma is not configured. Set UPTIME_KUMA_URL and UPTIME_KUMA_USERNAME."

    try:
        with _connection_lock:
            api = get_connection()
            monitors = api.get_monitors()
            heartbeats = api.get_heartbeats()
            # The two calls above just paid the real, necessary cost of
            # waiting for this connection's initial data batch (if it
            # was fresh) — from here on, this same instance has nothing
            # left to wait for; every later heartbeat/monitor update
            # arrives as a single, complete push (confirmed directly in
            # the installed library source), not a multi-message batch
            # `wait_events` exists to wait out. Shrinking it now is what
            # actually removes the ~0.4s structural floor from every
            # subsequent call on this connection, without skipping the
            # one call that genuinely needed the full, safe wait.
            if not getattr(api, "_settled", True):
                api.wait_events = _SETTLED_WAIT_EVENTS
                api._settled = True
    except Exception as e:
        _LOGGER.error("Failed to connect to Uptime Kuma: %s", e)
        # Force a fresh connection attempt next call — whatever state
        # the current one is in after an exception here is assumed bad.
        # Uses disconnect() (which closes the underlying Socket.IO
        # transport before dropping the reference) rather than bare
        # `_persistent_api = None` — found via a deliberate function-by-
        # function audit: the bare assignment orphaned a possibly
        # half-alive connection object (e.g. a login timeout whose
        # socket had actually connected fine), leaving its transport
        # open until garbage collection rather than closing it at the
        # one moment we know it's being discarded. get_connection()'s
        # own reconnect path already disconnects a stale object before
        # replacing it for exactly this reason; this error path was the
        # one discard site that didn't.
        disconnect()
        return f"Could not connect to Uptime Kuma: {e}"

    if not monitors:
        return "No monitors found in Uptime Kuma."

    down = []
    pending = []
    maintenance = []
    no_data = []
    up_count = 0

    for monitor in monitors:
        mid = monitor.get("id")
        name = monitor.get("name", f"Monitor {mid}")
        status = _get_status_from_heartbeats(heartbeats, mid)

        if status is None:
            no_data.append(name)
        elif status == 1:
            up_count += 1
        elif status == 0:
            down.append(name)
        elif status == 2:
            pending.append(name)
        elif status == 3:
            maintenance.append(name)
        else:
            # Unknown status code — shouldn't occur with the current
            # uptime_kuma_api (v1.2.1, MonitorStatus values 0-3 are all
            # stable), but silently dropping the monitor from all buckets
            # would produce "All 0 monitored services are up" even for a
            # real monitor, which is actively wrong. Treat as no_data so
            # at minimum the monitor is named in output.
            _LOGGER.warning("Uptime Kuma: monitor '%s' has unexpected status %r", name, status)
            no_data.append(name)

    total = len(monitors)
    _LOGGER.info(
        "Uptime Kuma: %d up, %d down, %d pending, %d maintenance, %d no data, of %d total",
        up_count, len(down), len(pending), len(maintenance), len(no_data), total
    )

    if not down and not pending and not no_data:
        parts = [f"All {up_count} monitored services are up."]
        if maintenance:
            parts.append(f"In maintenance: {', '.join(maintenance)}.")
        return " ".join(parts)

    parts = []
    if down:
        parts.append(f"DOWN ({len(down)}): {', '.join(down)}.")
    if pending:
        parts.append(f"PENDING ({len(pending)}): {', '.join(pending)}.")
    if maintenance:
        parts.append(f"In maintenance: {', '.join(maintenance)}.")
    if no_data:
        parts.append(f"No heartbeat data yet ({len(no_data)}): {', '.join(no_data)}.")
    parts.append(f"{up_count} of {total} services are up.")

    return " ".join(parts)
