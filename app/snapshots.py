"""
Mnemolis Snapshot Engine
Periodically captures source states and diffs them to detect meaningful changes.

Phase 1 sources: uptime, forecast, news
Phase 2: HA structured entity snapshots
"""
import sqlite3
import logging
from app.config import settings
from datetime import datetime, timezone, timedelta
from contextlib import closing

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_DB = "/app/data/snapshots.db"

# Each background snapshot job's scheduled interval, matching main.py's
# scheduler.add_job() calls exactly — kept here rather than in main.py
# since this module is what actually needs to reason about "how overdue
# is this job" using its own stored timestamps. Moved earlier in the
# file (was previously defined further down) so _RETENTION_PER_SOURCE
# below can be built from it directly.
JOB_INTERVALS_MINUTES = {
    "uptime": 2,
    "forecast": 30,
    "news": 60,
    "ha": 5,
}

# How many snapshots to retain per source, scaled so EVERY source
# genuinely supports the longest documented time-window phrase
# ("this week" / "since last week" → 168 hours, see
# _resolve_changes_hours() in router.py).
#
# Found via a deliberate "bulletproofing" pass: the original code used
# a single, shared MAX_SNAPSHOTS_PER_SOURCE = 288 constant for every
# source, with a comment claiming "24 hours at 5-minute intervals" —
# true only for `ha` specifically (the source whose interval the
# constant was apparently chosen around). Confirmed directly with a
# constructed scenario: `uptime` (snapshotted every 2 minutes, the most
# frequent of any source) only retained 9.6 real hours of data under
# that shared constant — a real query like "what changed with my
# services since yesterday" (48h) or "any outages this week" (168h)
# would silently return an incomplete picture, missing 80%+ of the
# requested window, with no indication to the user that the underlying
# data simply didn't exist anymore. `news` (60-minute interval), by
# contrast, was retaining 288 real HOURS (12 days) under the same
# shared constant — far more than ever needed, while `uptime` had far
# less than the system's own documented features required.
#
# Scaled per-source from each source's real interval so every source
# consistently supports the full week — `uptime` needs the most rows
# (5040) to cover a week at its 2-minute cadence, still genuinely small
# for SQLite at realistic homelab scale (each row is a short text
# string, not a meaningful storage concern even at this count).
_RETENTION_TARGET_HOURS = 168
_RETENTION_PER_SOURCE = {
    source: int((_RETENTION_TARGET_HOURS * 60) / interval_minutes)
    for source, interval_minutes in JOB_INTERVALS_MINUTES.items()
}


def _connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and busy timeout to reduce lock contention."""
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def init_snapshot_db():
    """Create snapshot tables if they don't exist."""
    try:
        with closing(_connect(SNAPSHOT_DB)) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source TEXT NOT NULL,
                    content TEXT NOT NULL
                )
            """)
            con.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_source_time ON snapshots (source, timestamp DESC)")
            con.commit()
        _LOGGER.info("Snapshot DB initialized")
    except Exception as e:
        _LOGGER.warning("Could not initialize snapshot DB: %s", e)


def _store_snapshot(source: str, content: str):
    """Store a snapshot and prune old entries."""
    try:
        with closing(_connect(SNAPSHOT_DB)) as con:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            con.execute(
                "INSERT INTO snapshots (timestamp, source, content) VALUES (?, ?, ?)",
                (now, source, content)
            )
            # Prune old snapshots — keep only the most recent N per source,
            # scaled per-source (see _RETENTION_PER_SOURCE above) so every
            # source genuinely supports the longest documented time-window
            # phrase. Falls back to the 24-hour-at-5-minute-intervals
            # default (288) for any source not yet in the dict, rather than
            # crashing — defensive against a future new snapshot source
            # being added to JOB_INTERVALS_MINUTES without a corresponding
            # update reaching this lookup, the same kind of two-places-to-
            # update risk already found and fixed elsewhere this release
            # cycle (the duplicated /backup file list in main.py).
            retention = _RETENTION_PER_SOURCE.get(source, 288)
            con.execute("""
                DELETE FROM snapshots WHERE source = ? AND id NOT IN (
                    SELECT id FROM snapshots WHERE source = ?
                    ORDER BY id DESC LIMIT ?
                )
            """, (source, source, retention))
            con.commit()
    except Exception as e:
        _LOGGER.warning("Could not store snapshot for '%s': %s", source, e)


def _get_last_snapshots(source: str, limit: int = 2) -> list[str]:
    """Return the most recent N snapshots for a source."""
    try:
        with closing(_connect(SNAPSHOT_DB)) as con:
            rows = con.execute(
                "SELECT content FROM snapshots WHERE source = ? ORDER BY id DESC LIMIT ?",
                (source, limit)
            ).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        _LOGGER.warning("Could not fetch snapshots for '%s': %s", source, e)
        return []


def get_snapshot_job_health() -> dict[str, dict]:
    """
    Report each background snapshot job's health by comparing its most
    recent successful snapshot timestamp against its expected interval.

    Found via real review, not a reported failure: every snapshot job
    (snapshot_uptime, snapshot_forecast, snapshot_news, snapshot_ha)
    already catches its own exceptions and just logs a warning — meaning
    a job that started failing on every single run would never crash,
    never stop the scheduler, and produce zero externally visible signal
    beyond a log line nobody is necessarily watching. The scheduler
    object itself also has no external visibility at all (it's a local
    variable inside main.py's lifespan context manager, never exposed to
    any endpoint) — so there was previously no way to ask "is the
    background scheduler actually still running and succeeding" without
    reading raw application logs.

    Uses a configurable grace multiplier (default 3x) on each job's
    interval before considering it stale — generous enough to absorb
    normal jitter (job execution time, a slightly delayed scheduler
    start), tight enough to catch a genuinely stuck job within a
    reasonable window at the default (e.g. the 60-minute news job is
    flagged after ~3 hours of silence, not days). Found hardcoded via a
    deliberate config-completeness audit; now SNAPSHOT_STALE_GRACE_MULTIPLIER.
    """
    now = datetime.now(timezone.utc)
    health = {}

    # One connection and one query for ALL four sources, rather than the
    # previous one-connection-per-source loop (four separate opens per
    # /health call for what is a single tiny read) — found via a
    # deliberate function-by-function audit. GROUP BY with MAX(id) is
    # exactly SQLite's documented bare-column guarantee territory: with
    # a single MAX() aggregate, the bare `timestamp` column is
    # guaranteed to come from the row that produced the MAX — the same
    # SQLite rule query_log_stats() in main.py cites for the inverse
    # case (where the guarantee did NOT apply and had to be fixed).
    latest_by_source: dict[str, str] = {}
    db_error: str | None = None
    try:
        with closing(_connect(SNAPSHOT_DB)) as con:
            rows = con.execute(
                "SELECT source, timestamp, MAX(id) FROM snapshots GROUP BY source"
            ).fetchall()
        latest_by_source = {r[0]: r[1] for r in rows}
    except Exception as e:
        db_error = str(e)

    for source, interval_minutes in JOB_INTERVALS_MINUTES.items():
        if db_error is not None:
            health[source] = {"status": "unknown", "error": db_error}
            continue

        timestamp = latest_by_source.get(source)
        if timestamp is None:
            # No snapshot has ever been stored for this source — either
            # the job hasn't run yet (very early after startup) or it
            # has never once succeeded
            health[source] = {
                "status": "never_ran",
                "expected_interval_minutes": interval_minutes,
            }
            continue

        try:
            last_timestamp = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            health[source] = {"status": "unknown", "error": f"unparseable timestamp: {timestamp!r}"}
            continue

        minutes_since = (now - last_timestamp).total_seconds() / 60
        is_stale = minutes_since > interval_minutes * settings.snapshot_stale_grace_multiplier

        health[source] = {
            "status": "stale" if is_stale else "ok",
            "last_snapshot": timestamp,
            "minutes_since_last_snapshot": round(minutes_since, 1),
            "expected_interval_minutes": interval_minutes,
        }

    return health


def _get_snapshots_since(source: str, since_hours: int | float = 24) -> list[tuple[str, str]]:
    """Return all snapshots for a source since N hours ago."""
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with closing(_connect(SNAPSHOT_DB)) as con:
            rows = con.execute(
                "SELECT timestamp, content FROM snapshots WHERE source = ? AND timestamp >= ? ORDER BY id ASC",
                (source, since)
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception as e:
        _LOGGER.warning("Could not fetch snapshots since %dh for '%s': %s", since_hours, source, e)
        return []


# ---------------------------------------------------------------------------
# Diff engines per source
# ---------------------------------------------------------------------------

def _diff_uptime(old: str, new: str) -> list[str]:
    """Detect service status changes between two uptime snapshots."""
    changes = []
    if old == new:
        return changes

    old_lower, new_lower = old.lower(), new.lower()
    old_all_up = "all" in old_lower and "up" in old_lower
    new_all_up = "all" in new_lower and "up" in new_lower

    # Both "all up" — no change
    if old_all_up and new_all_up:
        return changes

    if old_all_up and not new_all_up:
        # Found via a deliberate complexity-investigation pass (checking
        # whether this function or its siblings in this file were
        # genuinely buggy, not just complex): the previous version
        # collapsed any non-"all up" transition into the same
        # "Service outage detected" wording, including a PENDING-only
        # transition — Uptime Kuma's own status model (status code 2)
        # treats "pending" as a distinct, less severe state from a
        # confirmed outage (status code 0, "down"), typically meaning a
        # check is in a retry/grace period, not necessarily a real
        # outage yet. Checking for the literal "down" label explicitly,
        # separately from a generic "not all up" catch-all, means a
        # pending-only transition gets its own, honestly-worded message
        # instead of borrowing the more alarming "outage" wording.
        # Checking "down" before "pending" also means a mixed state
        # (some services down, others pending) correctly keeps the more
        # severe "outage" wording rather than downgrading it.
        if "down" in new_lower:
            changes.append(f"Service outage detected: {new.strip()}")
        elif "pending" in new_lower:
            changes.append(f"Service check pending (possible outage starting): {new.strip()}")
        else:
            changes.append(f"Service status changed: {new.strip()}")
    elif not old_all_up and new_all_up:
        changes.append("All services restored — previously reported outage resolved")
    elif old != new:
        changes.append(f"Service status changed: {new.strip()}")

    return changes


def _diff_forecast(old: str, new: str) -> list[str]:
    """Detect meaningful weather changes between two forecast snapshots."""
    import re
    changes = []
    if old == new:
        return changes

    def extract_high(text: str) -> float | None:
        # Found via a deliberate complexity/correctness investigation:
        # the regex had no support for a negative sign at all, silently
        # returning None for any sub-zero forecast text. Since Mnemolis
        # is explicitly designed to be deployable anywhere, not just in
        # warm climates, a deployment somewhere genuinely cold would
        # have temperature-change detection silently stop working below
        # freezing — no error, just a quiet no-op for that part of the
        # diff. The forecast text comes directly from
        # round(Open-Meteo's temperature_2m_max/min), with no floor
        # applied, so a negative value is a real, reachable case, not a
        # contrived one.
        m = re.search(r"high of (?:about )?(-?\d+)", text)
        return float(m.group(1)) if m else None

    def extract_low(text: str) -> float | None:
        m = re.search(r"low of (-?\d+)", text)
        return float(m.group(1)) if m else None

    old_high = extract_high(old)
    new_high = extract_high(new)
    old_low = extract_low(old)
    new_low = extract_low(new)

    # Found via the same discipline as the negative-sign fix above, one
    # step further: `if old_high and new_high` is a truthiness check,
    # and 0.0 is just as falsy in Python as None is — meaning a forecast
    # high or low of EXACTLY zero degrees was silently indistinguishable
    # from "couldn't extract a value at all", and a real temperature
    # change involving a 0° day would never register, in either
    # direction. Confirmed directly: a high changing from 0° to 15°
    # (a real, 15-degree swing, well above any sane threshold) produced
    # zero detected changes before this fix. 0° is an entirely ordinary
    # winter temperature for a real deployment somewhere genuinely cold
    # — the exact same "Mnemolis is deployable anywhere" reasoning that
    # motivated the negative-sign fix applies here too, just one
    # truthiness check further downstream from where that fix looked.
    # `is not None` checks are required, not `and`/`or` against the
    # extracted values themselves.
    threshold = settings.forecast_temp_change_threshold
    if old_high is not None and new_high is not None and abs(new_high - old_high) >= threshold:
        direction = "up" if new_high > old_high else "down"
        changes.append(f"Forecast high changed {direction} to {int(new_high)}°")

    if old_low is not None and new_low is not None and abs(new_low - old_low) >= threshold:
        direction = "up" if new_low > old_low else "down"
        changes.append(f"Forecast low changed {direction} to {int(new_low)}°")

    # Check for precipitation appearing or disappearing
    old_has_rain = any(w in old.lower() for w in ["rain", "storm", "thunder", "shower", "precipitation"])
    new_has_rain = any(w in new.lower() for w in ["rain", "storm", "thunder", "shower", "precipitation"])
    if not old_has_rain and new_has_rain:
        changes.append("Precipitation now in forecast")
    elif old_has_rain and not new_has_rain:
        changes.append("Precipitation removed from forecast")

    return changes


def _diff_news(old: str, new: str) -> list[str]:
    """Detect new articles between two news snapshots."""
    changes = []
    if old == new:
        return changes

    def extract_headlines(text: str) -> set[str]:
        # Found via a deliberate "bulletproofing" pass: this used to
        # have two branches — one for a bare "**headline**" with
        # nothing after the closing **, one for "**headline** (source)"
        # with a trailing suffix. Confirmed the first branch was
        # genuinely unreachable through any real code path: every
        # format string freshrss.py actually produces is
        # "**{title}** ({source})", always with a parenthetical suffix,
        # never a bare closing "**" with nothing after. More than just
        # dead, it was also redundant — the second branch's own logic
        # (find the closing "**" via .index(), regardless of what
        # follows it) already correctly handles the bare-closing case
        # too, verified directly. Simplified to the one genuinely
        # general check both branches were trying to express.
        headlines = set()
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("**") and "**" in line[2:]:
                headline = line[2:line.index("**", 2)].strip()
                if headline:
                    headlines.add(headline)
        return headlines

    old_headlines = extract_headlines(old)
    new_headlines = extract_headlines(new)
    new_stories = new_headlines - old_headlines

    for story in sorted(new_stories)[:5]:  # cap at 5 new stories
        changes.append(f"New article: {story}")

    return changes


def _iter_ha_entity_changes(old: str, new: str):
    """Shared entity-level comparison core for HA snapshot diffing.

    Yields one dict per real, meaningful change found, with both a
    human-readable description and the structured fields behind it:
    {"kind", "entity_id", "name", "state", "description"}.

    This is the one real source of truth `_diff_ha()` (free-text "what
    changed" output) and `extract_ha_events()` in app/temporal_patterns.py
    (structured events for temporal pattern mining) both build on —
    added specifically so the two never independently re-implement the
    same entity comparison and silently drift apart from each other,
    the same class of bug already found and fixed once in this
    project's history (router.py/fusion.py's "_looks_empty" phrase
    list). `_diff_ha()`'s own existing behavior, including its
    malformed-entity defensive skip below, is preserved exactly here —
    this is a pure extraction, not a behavior change.
    """
    import json

    if old == new:
        return

    try:
        old_entities = {e["entity_id"]: e for e in json.loads(old)}
        new_entities = {e["entity_id"]: e for e in json.loads(new)}
    except (json.JSONDecodeError, TypeError, KeyError):
        return

    for entity_id, new_e in new_entities.items():
        old_e = old_entities.get(entity_id)
        if old_e is None:
            continue  # new entity, not a state change

        # Found via a deliberate complexity/correctness investigation:
        # accessing old_e["state"]/new_e["state"] directly with bracket
        # notation would raise an uncaught KeyError if either entity is
        # missing that field, crashing the whole diff for every OTHER
        # entity in the same snapshot too — not just the malformed one.
        # snapshot_ha() itself always writes a "state" field today, so
        # this isn't reachable through the current writer, but snapshots
        # are persisted in a long-lived SQLite file and read back
        # potentially much later; data written by an older version of
        # this code, or before a future schema change, could genuinely
        # still be sitting there. Skipping a malformed entity instead of
        # crashing keeps every other entity in the same snapshot
        # correctly diffable regardless.
        if "state" not in old_e or "state" not in new_e:
            continue

        name = new_e.get("friendly_name", entity_id)
        domain = entity_id.split(".")[0]
        dc = new_e.get("device_class", "")

        # Lock state changes
        if domain == "lock" and old_e["state"] != new_e["state"]:
            yield {
                "kind": "lock",
                "entity_id": entity_id,
                "name": name,
                "state": new_e["state"],
                "description": f"{name} {new_e['state']}",
            }

        # Door / window / opening sensor state changes — these three
        # device classes share the same real binary semantics (closed
        # vs open), all genuinely captured by snapshot_ha()'s own
        # filter (see is_relevant_binary_sensor there) but, until now,
        # never actually diffed by this function at all.
        elif dc in ("door", "window", "opening") and old_e["state"] != new_e["state"]:
            state_label = "opened" if new_e["state"] == "on" else "closed"
            yield {
                "kind": dc,
                "entity_id": entity_id,
                "name": name,
                "state": state_label,
                "description": f"{name} {state_label}",
            }

        # Motion detected — found via review: snapshot_ha() already
        # captures motion-class binary sensors (is_relevant_binary_sensor
        # includes "motion"), but no branch here ever diffed them,
        # meaning a real motion transition produced zero events from
        # either this function or app/temporal_patterns.py's
        # extract_ha_events(), which is built directly on this same
        # comparison core. This silently meant the wiki's own opening,
        # headline example for the whole temporal-pattern-detection
        # feature — "does a front-door lock event reliably precede a
        # motion event" — was never actually testable, confirmed
        # directly: a real "off" -> "on" motion transition produced an
        # empty event list before this fix.
        #
        # Only the "off" -> "on" edge (motion just started) is reported
        # as an event — the reverse "on" -> "off" transition is the
        # sensor settling back to its resting state once motion stops,
        # not a new, independently meaningful occurrence worth counting
        # for either the free-text "what changed" summary or temporal
        # pattern correlation. This mirrors how a door's "opened" event
        # is the meaningful one for "did someone just walk through
        # here," while a lock/door's own closed/locked side already has
        # its own real, separate meaning unlike motion's "off" state,
        # which has none on its own.
        elif dc == "motion" and old_e["state"] == "off" and new_e["state"] == "on":
            yield {
                "kind": "motion",
                "entity_id": entity_id,
                "name": name,
                "state": "detected",
                "description": f"{name} motion detected",
            }

        # Battery crossing below configured threshold
        elif dc == "battery":
            try:
                old_val = float(old_e["state"])
                new_val = float(new_e["state"])
                threshold = settings.battery_low_threshold_pct
                if old_val >= threshold and new_val < threshold:
                    yield {
                        "kind": "battery_low",
                        "entity_id": entity_id,
                        "name": name,
                        "state": new_val,
                        "description": f"{name} battery low: {new_val:.0f}%",
                    }
            except (ValueError, TypeError):
                pass


def _diff_ha(old: str, new: str) -> list[str]:
    """Detect meaningful entity state changes between two HA snapshots.

    Focuses on:
    - Lock state changes (locked/unlocked)
    - Door/window/opening sensor state changes (open/closed)
    - Battery level crossing below 20%
    - New motion events (the "off" -> "on" detection edge only)

    Ignores lights and switches — too noisy for a "what changed" summary.
    """
    return [c["description"] for c in _iter_ha_entity_changes(old, new)]


# ---------------------------------------------------------------------------
# Snapshot jobs — called by scheduler
# ---------------------------------------------------------------------------

def snapshot_uptime():
    """Capture uptime status snapshot."""
    try:
        from app.sources.uptime_kuma import search
        result = search("are all services up")
        _store_snapshot("uptime", result)
        _LOGGER.debug("Uptime snapshot stored")

        # Same piggyback contract as snapshot_ha: the history sampler
        # derives its services_up/services_total pair from the text this
        # job just fetched, never from a fetch of its own.
        if settings.history_enabled:
            try:
                from app import history
                history.ingest_uptime_text(result)
            except Exception as e:
                _LOGGER.warning("history ingest (uptime) failed: %s", e)
    except Exception as e:
        _LOGGER.warning("Uptime snapshot failed: %s", e)


def snapshot_forecast():
    """Capture forecast snapshot."""
    try:
        from app.sources.forecast import search
        result = search("what is the weather forecast")
        _store_snapshot("forecast", result)
        _LOGGER.debug("Forecast snapshot stored")
    except Exception as e:
        _LOGGER.warning("Forecast snapshot failed: %s", e)


def snapshot_news():
    """Capture news snapshot."""
    try:
        from app.sources.freshrss import search
        result = search("latest news headlines")
        _store_snapshot("news", result)
        _LOGGER.debug("News snapshot stored")
    except Exception as e:
        _LOGGER.warning("News snapshot failed: %s", e)


def snapshot_ha():
    """Capture raw HA entity state snapshot as JSON for structured diffing."""
    try:
        import json
        # Reuses home_assistant._get_states() rather than issuing its own
        # bare requests.get() — found via a deliberate function-by-function
        # audit: this function duplicated _get_states()'s exact fetch
        # (same endpoint, same auth header, same timeout) line for line,
        # meaning the snapshot job was one drift-prone copy of logic that
        # already had a single owner, AND it bypassed home_assistant.py's
        # persistent _session (fresh TCP setup on every scheduled run).
        # _get_states() returns None both when HA is unconfigured and on
        # fetch failure — it logs the failure itself — so one None check
        # covers both of this function's previous early returns.
        from app.sources.home_assistant import _get_states

        states = _get_states()
        if states is None:
            return

        # Only store fields relevant to diffing — keep snapshot size small
        relevant = []
        for e in states:
            domain = e["entity_id"].split(".")[0]
            dc = e.get("attributes", {}).get("device_class", "")
            is_lock = domain == "lock"
            is_relevant_binary_sensor = domain == "binary_sensor" and dc in ("door", "motion", "window", "opening")
            is_battery = dc == "battery"
            if is_lock or is_relevant_binary_sensor or is_battery:
                relevant.append({
                    "entity_id": e["entity_id"],
                    "state": e["state"],
                    "friendly_name": e.get("attributes", {}).get("friendly_name", e["entity_id"]),
                    "device_class": dc,
                })

        _store_snapshot("ha", json.dumps(relevant))
        _LOGGER.debug("HA snapshot stored — %d relevant entities", len(relevant))

        # Hand the SAME states payload to the history sampler (Design Doc
        # 5, constraint #1: history piggybacks on this fetch and adds zero
        # HA load of its own). Gated here AND inside ingest_ha_states —
        # the registration-time + in-function double-check pattern — and
        # isolated so a history failure can never fail the snapshot.
        if settings.history_enabled:
            try:
                from app import history
                history.ingest_ha_states(states)
            except Exception as e:
                _LOGGER.warning("history ingest (ha) failed: %s", e)
    except Exception as e:
        _LOGGER.warning("HA snapshot failed: %s", e)


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

_DIFF_FNS = {
    "uptime": _diff_uptime,
    "forecast": _diff_forecast,
    "news": _diff_news,
    "ha": _diff_ha,
}


def get_changes(since_hours: int | float = 24) -> dict[str, list[dict[str, str]]]:
    """
    Return meaningful changes detected across all snapshot sources
    within the last N hours.

    For "flapping" sources (uptime, forecast) where intermediate state
    changes can round-trip back to the original state within the window,
    only the NET change (first snapshot vs. last snapshot) is reported —
    avoiding noisy alarm/resolved pairs that don't reflect current reality.

    For event-based sources (news, ha) every individual event is reported
    since each one is independently meaningful (a new article, a door
    opening) rather than a state that can "flap" back to baseline.

    Returns dict of {source: [change_description, ...]}
    """
    changes = {}

    # Sources where only the net (first vs last) change matters —
    # intermediate flapping within the window isn't independently meaningful
    NET_CHANGE_SOURCES = {"uptime", "forecast"}

    for source, diff_fn in _DIFF_FNS.items():
        snapshots = _get_snapshots_since(source, since_hours=since_hours)
        if len(snapshots) < 2:
            continue

        source_changes = []

        if source in NET_CHANGE_SOURCES:
            # Compare only first vs last snapshot in the window
            ts_first, content_first = snapshots[0]
            ts_last, content_last = snapshots[-1]
            diffs = diff_fn(content_first, content_last)
            for diff in diffs:
                source_changes.append({"timestamp": ts_last, "change": diff})
        else:
            # Walk consecutive pairs — every event matters.
            #
            # Found via a deliberate function-by-function read: the
            # original code used a `seen_changes` set to deduplicate
            # change descriptions across consecutive pairs, preventing
            # the same description from appearing twice. The intent was
            # correct for UPTIME and FORECAST — if a service stays down
            # across multiple snapshot pairs, the same "outage" message
            # would appear in each pair. But uptime and forecast already
            # use the NET_CHANGE_SOURCES path above, never reaching here.
            #
            # For the actual event-based sources that DO reach here (ha
            # and news), the dedup is both unnecessary AND actively
            # harmful: _diff_ha() only fires on REAL state transitions
            # (old["state"] != new["state"]), so the same change text
            # can only recur in consecutive pairs if the entity genuinely
            # transitioned the same direction AGAIN — a door opened,
            # closed, then opened again. That second "opened" is a
            # separate, real event the user should see. The dedup
            # silently dropped it. Same logic applies to _diff_news():
            # an article is only "new" in pair i→i+1 if it wasn't in
            # snap_i; it can't appear as "new" again in pair i+1→i+2
            # unless it was somehow removed from snap_i+1 and re-added
            # to snap_i+2, which the append-only news structure makes
            # impossible. Confirmed directly: door open→close→open in
            # a window produced only "opened" and "closed" before this
            # fix; the final open state was invisible in the summary.
            for i in range(len(snapshots) - 1):
                ts_old, content_old = snapshots[i]
                ts_new, content_new = snapshots[i + 1]
                diffs = diff_fn(content_old, content_new)
                for diff in diffs:
                    source_changes.append({"timestamp": ts_new, "change": diff})

        if source_changes:
            changes[source] = source_changes

    return changes


def format_changes(changes: dict, since_hours: int | float = 24) -> str:
    """Format changes dict into a human-readable summary.

    Rounds since_hours for display regardless of what the caller
    passed in — found via a deliberate "bulletproofing" pass: this
    function's own type signature (int | float) explicitly invites a
    raw float, and a real caller (router.py's _search_changes(), for
    "this morning"-style natural-language resolution) genuinely
    produces one. Both of this function's current real callers happen
    to already avoid the problem (one passes a REST endpoint's plain
    int parameter, the other already rounds before calling), so this
    wasn't reachable today — but formatting a number reasonably for
    human display is this function's own job, not something it should
    rely on every present and future caller to remember correctly.
    Without this, a future caller passing the unrounded float through
    would display something like "in the last 23.939205609166667
    hours" directly to a real user.
    """
    since_hours = round(since_hours, 1)
    if not changes:
        return f"No significant changes detected in the last {since_hours} hours."

    parts = []
    source_labels = {"uptime": "Services", "forecast": "Weather", "news": "News", "ha": "Home"}

    for source, items in changes.items():
        label = source_labels.get(source, source.upper())
        lines = [f"**{label}:**"]
        for item in items:
            ts = item["timestamp"].replace("T", " ").replace("Z", " UTC")
            lines.append(f"- {item['change']} ({ts})")
        parts.append("\n".join(lines))

    return "\n\n".join(parts)
