"""
Mnemolis History Source — time-series memory for the house (Design Doc 5).

The read-side complement to the Snapshot Engine: same data the `ha` and
`changes` sources already speak aloud, new axis (time instead of diff).
The Snapshot Engine already polls every source on a clock, but it stores
formatted text sized for diffing and throws the numeric sensors away
(temperature, CO2, humidity, power — exactly what the Mnemovox satellites
exist to produce). This module keeps what the house is already telling us
and answers aggregate/trend questions over it.

Design constraints, in the doc's priority order, and where each lives here:
  1. Sampling never degrades what exists — ingestion rides INSIDE
     snapshot_ha()/snapshot_uptime(), consuming the payloads those jobs
     already fetched (zero additional HA/uptime load; history makes no
     fetch of its own) and log-and-continues so it can never fail or
     stall a snapshot job.
  2. Answer only what the data supports, and say the coverage — every
     assembled answer states its ACTUAL window when the request exceeds
     recorded history; a trend requires HISTORY_TREND_MIN_SAMPLES; no LLM
     anywhere in aggregation (numbers come from SQL over real samples or
     they don't come).
  3. Bounded on disk — explicit HISTORY_RETENTION_DAYS pruning in the
     sampler tick, batched to keep WAL growth bounded.
  4. Deterministic query parsing — metric/window/aggregation resolve by
     pattern against closed vocabularies (reusing the HA alias machinery
     and timeutil.resolve_window()); anything unresolvable returns an
     honest "couldn't identify which metric you mean" listing, never a
     guess.
  5. Local time is the user's time — windows resolve via timeutil /
     LOCAL_TIMEZONE; storage stays UTC like every other DB.

The aggregation functions (aggregate_series, compute_trend) are module-level
and importable ON PURPOSE (Design Doc 5 §9): a future Sentinel watch kind
("if the office CO2 average this hour is above 1000…") evaluates history
aggregates, so aggregation lives here as importable functions, not buried
inside the source adapter.
"""
import logging
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta
from typing import NamedTuple

from app.config import settings
from app.timeutil import TIMESTAMP_FORMAT

_LOGGER = logging.getLogger(__name__)

HISTORY_DB = "/app/data/history.db"

# Read-only handle to the temporal feature's event store — Design Doc 5's
# "Events are not duplicated" rule: count/when queries over
# door/motion/lock/outage events read temporal_patterns.db's temporal_events
# DIRECTLY (never a second extractor — that would be the snapshot_ha
# duplicate-fetch bug the audit already removed once), via the same
# ?mode=ro discipline router.py uses for query_log.db.
_TEMPORAL_DB = "/app/data/temporal_patterns.db"

def _connect(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and busy timeout — the exact
    _connect() shape snapshots.py and temporal_patterns.py already use."""
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def _connect_temporal_readonly() -> sqlite3.Connection:
    """Read-only connection to temporal_patterns.db — the events leg never
    writes; ?mode=ro enforces that at the connection level, not just by
    convention, exactly like router.py's _connect_log_db_readonly()."""
    uri = f"file:{_TEMPORAL_DB}?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=10)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def init_history_db() -> None:
    """Create the metric_samples and metric_catalog tables if absent."""
    try:
        with closing(_connect(HISTORY_DB)) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS metric_samples (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric_key  TEXT NOT NULL,
                    value       REAL NOT NULL,
                    unit        TEXT NOT NULL DEFAULT '',
                    ts          TEXT NOT NULL
                )
            """)
            con.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_key_ts "
                "ON metric_samples(metric_key, ts)"
            )
            con.execute("""
                CREATE TABLE IF NOT EXISTS metric_catalog (
                    metric_key    TEXT PRIMARY KEY,
                    friendly_name TEXT NOT NULL,
                    area_id       TEXT NOT NULL DEFAULT '',
                    device_class  TEXT NOT NULL DEFAULT '',
                    unit          TEXT NOT NULL DEFAULT '',
                    first_seen    TEXT NOT NULL,
                    last_seen     TEXT NOT NULL
                )
            """)
            con.commit()
        _LOGGER.info("History DB initialized")
    except Exception as e:
        _LOGGER.warning("Could not initialize history DB: %s", e)


# ---------------------------------------------------------------------------
# Config parsing helpers
# ---------------------------------------------------------------------------

def _csv_set(raw: str) -> set[str]:
    """Parse a comma-separated setting into a set of stripped, non-empty
    tokens — the same shape main.py's _valid_api_keys() uses."""
    if not raw:
        return set()
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def _device_classes() -> set[str]:
    return _csv_set(settings.history_device_classes)


def _extra_entities() -> set[str]:
    return _csv_set(settings.history_extra_entities)


def _exclude_entities() -> set[str]:
    return _csv_set(settings.history_exclude_entities)


# ---------------------------------------------------------------------------
# The sampler
# ---------------------------------------------------------------------------

class _Sample(NamedTuple):
    metric_key: str
    value: float
    unit: str
    friendly_name: str
    area_id: str
    device_class: str


def _entity_keepable(entity: dict, device_classes: set[str],
                     extra: set[str], exclude: set[str]) -> bool:
    """Design Doc 5 §4 step 2: keep an entity when its state parses as a
    float AND it's either a `sensor` in an allowed device_class OR an
    explicitly-allowlisted entity_id, AND it's not excluded.
    Unavailable/unknown states are skipped by the float-parse failing —
    gaps are recorded truthfully as absence, never as a fabricated value.
    """
    entity_id = entity.get("entity_id", "")
    if not entity_id or entity_id in exclude:
        return False
    domain = entity_id.split(".")[0]
    dc = entity.get("attributes", {}).get("device_class", "") or ""
    by_class = domain == "sensor" and dc in device_classes
    by_allowlist = entity_id in extra
    return by_class or by_allowlist


def _parse_float(state) -> float | None:
    """Return the state as a finite float, or None for unavailable/unknown/
    blank/non-numeric — the honest "this tick has no reading" signal.

    Non-finite values are rejected explicitly (audit finding): float("nan")
    and float("inf") PARSE successfully, and a template sensor stuck on
    "NaN" would otherwise store a value that silently poisons every
    average and disables every min/max comparison it touches — a wrong
    number wearing a valid one's clothes, the exact failure mode this
    source's honesty constraints exist to rule out.
    """
    if state is None:
        return None
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _collect_ha_samples(states: list[dict], area_of: dict[str, str]) -> list[_Sample]:
    """Turn a raw HA states payload into keepable numeric samples."""
    device_classes = _device_classes()
    extra = _extra_entities()
    exclude = _exclude_entities()
    samples: list[_Sample] = []
    for e in states:
        if not _entity_keepable(e, device_classes, extra, exclude):
            continue
        value = _parse_float(e.get("state"))
        if value is None:
            continue
        entity_id = e["entity_id"]
        attrs = e.get("attributes", {})
        samples.append(_Sample(
            metric_key=entity_id,
            value=value,
            unit=attrs.get("unit_of_measurement", "") or "",
            friendly_name=attrs.get("friendly_name", entity_id) or entity_id,
            area_id=area_of.get(entity_id, ""),
            device_class=attrs.get("device_class", "") or "",
        ))
    return samples


def _parse_uptime_counts(text: str) -> tuple[int | None, int | None]:
    """Extract (up, total) from uptime source text. Two shapes only, both
    produced by uptime_kuma.search():
      - "All N monitored services are up."   -> up=N, total=N
      - "… N of M services are up."           -> up=N, total=M
    Returns (None, None) on anything unparseable (a real error string, an
    empty result) — the honest "no reading this tick" outcome."""
    import re
    if not text:
        return None, None
    m = re.search(r"[Aa]ll\s+(\d+)\s+monitored services are up", text)
    if m:
        n = int(m.group(1))
        return n, n
    m = re.search(r"(\d+)\s+of\s+(\d+)\s+services are up", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


# ---------------------------------------------------------------------------
# Ingestion — fed by the snapshot jobs, never fetching for itself
# ---------------------------------------------------------------------------
#
# Post-implementation audit fix: the first cut of this feature gave the
# sampler its own scheduler job that called _get_states() (a second,
# duplicate fetch of exactly what snapshot_ha had just retrieved),
# _get_area_entities() (an HA template render) EVERY tick, and — worst —
# a live uptime_kuma.search() (a full client login) every 5 minutes when
# snapshot_uptime already fetches identical text on its own 2-minute
# cadence. That violated the doc's own priority-1 constraint ("the same
# _get_states() fetch — zero additional HA API load") and reintroduced
# the exact duplicate-fetch pattern snapshot_ha's own comment records the
# full-audit pass removing once already. The doc also said "register
# sample_metrics() on the scheduler," which conflicts with its own
# constraint #1; the doc's explicit priority ordering says #1 wins.
#
# Now: snapshot_ha() hands its already-fetched states to
# ingest_ha_states(), and snapshot_uptime() hands its already-fetched text
# to ingest_uptime_text(). History adds ZERO fetches of its own. The one
# residual HA call — the area-map template render — is cached and
# refreshed at most once per _AREA_REFRESH_TICKS ingests (~hourly at the
# 5-minute snapshot cadence), because area assignments change on the
# timescale of furniture, not minutes. Cadence is therefore the snapshot
# jobs' own: HA metrics every JOB_INTERVALS_MINUTES["ha"] minutes, uptime
# counts every JOB_INTERVALS_MINUTES["uptime"] minutes.

_AREA_REFRESH_TICKS = 12
_area_of_cache: dict[str, str] = {}
_area_ticks_until_refresh = 0
_db_ready = False


def _ensure_db() -> None:
    """Lazy, idempotent schema guard on the ingest path. Startup init
    covers the normal case; this covers the DB file being removed at
    runtime or any path where ingest runs before lifespan init — CREATE
    IF NOT EXISTS is cheap enough to not be worth racing about."""
    global _db_ready
    if _db_ready:
        return
    init_history_db()
    _db_ready = True


def _refresh_area_map_if_due() -> dict[str, str]:
    """Return the entity→area map, refreshing from HA's template API at
    most every _AREA_REFRESH_TICKS ingests. On refresh failure the stale
    map is kept — a slightly old area assignment beats losing area
    resolution entirely for an hour."""
    global _area_of_cache, _area_ticks_until_refresh
    if _area_ticks_until_refresh > 0:
        _area_ticks_until_refresh -= 1
        return _area_of_cache
    _area_ticks_until_refresh = _AREA_REFRESH_TICKS - 1
    try:
        from app.sources.home_assistant import _get_area_entities
        area_map = _get_area_entities()
    except Exception as e:
        _LOGGER.debug("history area-map refresh failed: %s", e)
        return _area_of_cache
    if area_map:
        rebuilt: dict[str, str] = {}
        for area_id, entity_ids in area_map.items():
            for eid in entity_ids:
                rebuilt.setdefault(eid, area_id)
        _area_of_cache = rebuilt
    return _area_of_cache


def ingest_ha_states(states: list[dict]) -> None:
    """Record one tick of numeric samples from an already-fetched HA
    states payload — called by snapshot_ha() with the very list it just
    retrieved. Gated on HISTORY_ENABLED here as well as at the call site
    (the registration-time + in-function double-check pattern), and
    log-and-continue on any failure so history can never stall or fail a
    snapshot job (constraint #1)."""
    if not settings.history_enabled:
        return
    try:
        _ensure_db()
        samples = _collect_ha_samples(states or [], _refresh_area_map_if_due())
        if samples:
            _store_samples(samples)
        _prune_retention()
        _LOGGER.debug("history ingested %d HA metrics", len(samples))
    except Exception as e:
        _LOGGER.warning("history ingest_ha_states failed: %s", e)


def ingest_uptime_text(text: str) -> None:
    """Record the derived uptime.services_up / uptime.services_total pair
    from already-fetched uptime text — called by snapshot_uptime() with
    the very string it just retrieved and stored. Parses the same two
    shapes _diff_uptime() already trusts; anything unparseable (an error
    string, empty) is honestly no reading this tick. Forecast is
    deliberately NOT sampled anywhere: Open-Meteo is a prediction, not an
    observation (§4's honesty argument)."""
    if not settings.history_enabled:
        return
    try:
        up, total = _parse_uptime_counts(text)
        if up is None or total is None:
            return
        _ensure_db()
        _store_samples([
            _Sample("uptime.services_up", float(up), "", "Services up", "uptime", ""),
            _Sample("uptime.services_total", float(total), "", "Services total", "uptime", ""),
        ])
    except Exception as e:
        _LOGGER.warning("history ingest_uptime_text failed: %s", e)


def _store_samples(samples: list[_Sample]) -> None:
    """Insert all samples for this tick in ONE transaction; upsert the
    catalog rows (friendly names and areas can drift in HA, so last_seen /
    friendly_name / area_id / unit are refreshed each tick; first_seen is
    preserved via ON CONFLICT)."""
    now = _now_iso()
    try:
        with closing(_connect(HISTORY_DB)) as con:
            con.executemany(
                "INSERT INTO metric_samples (metric_key, value, unit, ts) "
                "VALUES (?, ?, ?, ?)",
                [(s.metric_key, s.value, s.unit, now) for s in samples],
            )
            con.executemany(
                """
                INSERT INTO metric_catalog
                    (metric_key, friendly_name, area_id, device_class, unit, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(metric_key) DO UPDATE SET
                    friendly_name = excluded.friendly_name,
                    area_id       = excluded.area_id,
                    device_class  = excluded.device_class,
                    unit          = excluded.unit,
                    last_seen     = excluded.last_seen
                """,
                [(s.metric_key, s.friendly_name, s.area_id, s.device_class,
                  s.unit, now, now) for s in samples],
            )
            con.commit()
    except Exception as e:
        _LOGGER.warning("history _store_samples failed: %s", e)


def _prune_retention(batch_size: int = 5000) -> None:
    """Delete samples older than HISTORY_RETENTION_DAYS, batched (one DELETE
    with a LIMIT loop) to keep WAL growth bounded rather than one giant
    transaction. Also drop catalog rows unseen for the retention window."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=settings.history_retention_days)
              ).strftime(TIMESTAMP_FORMAT)
    try:
        with closing(_connect(HISTORY_DB)) as con:
            while True:
                cur = con.execute(
                    "DELETE FROM metric_samples WHERE id IN ("
                    "SELECT id FROM metric_samples WHERE ts < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                con.commit()
                if cur.rowcount < batch_size:
                    break
            con.execute("DELETE FROM metric_catalog WHERE last_seen < ?", (cutoff,))
            con.commit()
    except Exception as e:
        _LOGGER.warning("history _prune_retention failed: %s", e)


# ---------------------------------------------------------------------------
# Catalog reads
# ---------------------------------------------------------------------------

class CatalogEntry(NamedTuple):
    metric_key: str
    friendly_name: str
    area_id: str
    device_class: str
    unit: str
    first_seen: str
    last_seen: str


def get_catalog() -> list[CatalogEntry]:
    """Return the full metric catalog, ordered by friendly_name."""
    try:
        with closing(_connect(HISTORY_DB)) as con:
            rows = con.execute(
                "SELECT metric_key, friendly_name, area_id, device_class, unit, "
                "first_seen, last_seen FROM metric_catalog ORDER BY friendly_name"
            ).fetchall()
        return [CatalogEntry(*r) for r in rows]
    except Exception as e:
        _LOGGER.warning("history get_catalog failed: %s", e)
        return []


def get_metrics_overview() -> dict:
    """The /history/metrics payload — catalog with per-metric sample counts
    and coverage (oldest/newest sample), the /areas analogue."""
    catalog = get_catalog()
    counts: dict[str, tuple[int, str, str]] = {}
    try:
        with closing(_connect(HISTORY_DB)) as con:
            rows = con.execute(
                "SELECT metric_key, COUNT(*), MIN(ts), MAX(ts) "
                "FROM metric_samples GROUP BY metric_key"
            ).fetchall()
        counts = {r[0]: (r[1], r[2], r[3]) for r in rows}
    except Exception as e:
        _LOGGER.warning("history get_metrics_overview counts failed: %s", e)

    metrics = []
    for c in catalog:
        n, oldest, newest = counts.get(c.metric_key, (0, None, None))
        metrics.append({
            "metric_key": c.metric_key,
            "friendly_name": c.friendly_name,
            "area_id": c.area_id,
            "device_class": c.device_class,
            "unit": c.unit,
            "samples": n,
            "oldest_sample": oldest,
            "newest_sample": newest,
            "last_seen": c.last_seen,
        })
    return {"status": "ok", "metric_count": len(metrics), "metrics": metrics}


# ---------------------------------------------------------------------------
# Sample reads
# ---------------------------------------------------------------------------

class Sample(NamedTuple):
    value: float
    ts: str  # UTC ISO


def fetch_samples(metric_key: str, start_utc: str, end_utc: str) -> list[Sample]:
    """All samples for a metric within [start_utc, end_utc], oldest-first.
    Bounds are UTC ISO strings (the storage convention); comparison is a
    plain string range, correct because TIMESTAMP_FORMAT sorts
    lexicographically in time order."""
    try:
        with closing(_connect(HISTORY_DB)) as con:
            rows = con.execute(
                "SELECT value, ts FROM metric_samples "
                "WHERE metric_key = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC",
                (metric_key, start_utc, end_utc),
            ).fetchall()
        return [Sample(r[0], r[1]) for r in rows]
    except Exception as e:
        _LOGGER.warning("history fetch_samples failed: %s", e)
        return []


def oldest_sample_ts(metric_key: str) -> str | None:
    """The earliest recorded ts for a metric — used for coverage disclosure
    (constraint #2: say the actual window when the request exceeds history)."""
    try:
        with closing(_connect(HISTORY_DB)) as con:
            row = con.execute(
                "SELECT MIN(ts) FROM metric_samples WHERE metric_key = ?",
                (metric_key,),
            ).fetchone()
        return row[0] if row else None
    except Exception as e:
        _LOGGER.warning("history oldest_sample_ts failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Aggregation engine — pure, importable (the Sentinel seam, §9)
# ---------------------------------------------------------------------------

class Summary(NamedTuple):
    count: int
    minimum: float
    maximum: float
    average: float
    latest: float
    min_ts: str
    max_ts: str
    latest_ts: str
    value_range: float


def aggregate_series(samples: list[Sample]) -> Summary | None:
    """Min/max/avg/latest over a series, with the timestamps of the
    extremes. Returns None for an empty series (the caller turns that into
    an honest "no recorded data in that window" message, never a zero)."""
    if not samples:
        return None
    lo = samples[0]
    hi = samples[0]
    total = 0.0
    for s in samples:
        if s.value < lo.value:
            lo = s
        if s.value > hi.value:
            hi = s
        total += s.value
    latest = samples[-1]
    return Summary(
        count=len(samples),
        minimum=lo.value,
        maximum=hi.value,
        average=total / len(samples),
        latest=latest.value,
        min_ts=lo.ts,
        max_ts=hi.ts,
        latest_ts=latest.ts,
        value_range=hi.value - lo.value,
    )


class Trend(NamedTuple):
    status: str          # "rising" | "falling" | "flat" | "insufficient"
    slope_per_day: float
    pct_per_day: float
    first_value: float
    last_value: float
    samples: int


def compute_trend(samples: list[Sample], min_samples: int,
                  min_delta_fraction: float) -> Trend:
    """Least-squares slope over the window, pure Python (the hand-rolled-
    Poisson precedent — no numpy). Direction + magnitude-per-day + first/
    last values, only above min_samples and only when the fitted change
    across the window clears a per-window noise floor (min_delta_fraction
    of the observed value range); otherwise "flat", which is a finding,
    not a shrug. "insufficient" when there aren't enough samples to fit a
    slope honestly."""
    n = len(samples)
    if n < max(min_samples, 2):
        return Trend("insufficient", 0.0, 0.0,
                     samples[0].value if samples else 0.0,
                     samples[-1].value if samples else 0.0, n)

    # x in hours from the first sample; y = value. Least squares.
    t0 = _parse_ts(samples[0].ts)
    xs = [(_parse_ts(s.ts) - t0).total_seconds() / 3600.0 for s in samples]
    ys = [s.value for s in samples]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:  # all samples at one instant — cannot fit a slope
        return Trend("flat", 0.0, 0.0, ys[0], ys[-1], n)
    slope_per_hour = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    slope_per_day = slope_per_hour * 24.0

    span_hours = xs[-1] - xs[0]
    fitted_change = slope_per_hour * span_hours
    value_range = max(ys) - min(ys)
    noise_floor = min_delta_fraction * value_range

    if value_range == 0 or abs(fitted_change) < noise_floor:
        status = "flat"
    elif slope_per_hour > 0:
        status = "rising"
    else:
        status = "falling"

    pct_per_day = (slope_per_day / mean_y * 100.0) if mean_y != 0 else 0.0
    return Trend(status, slope_per_day, pct_per_day, ys[0], ys[-1], n)


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Events leg — read-only over temporal_events (never a second extractor)
# ---------------------------------------------------------------------------

# Natural-language metric words -> HA device_class. Lives in the engine
# (not the adapter) because query_is_event() below needs it too, and the
# adapter importing it from here avoids an import cycle. Closed vocabulary
# (constraint #4): a word not here can still resolve via a catalog
# friendly-name match, but never via a guess.
_CLASS_WORDS: dict[str, str] = {
    "carbon dioxide": "carbon_dioxide",
    "co2": "carbon_dioxide",
    "carbon": "carbon_dioxide",
    "temperature": "temperature",
    "temp": "temperature",
    "humidity": "humidity",
    "power": "power",
    "wattage": "power",
    "energy": "energy",
    "illuminance": "illuminance",
    "light level": "illuminance",
    "brightness": "illuminance",
    "lux": "illuminance",
    "pressure": "pressure",
    "battery": "battery",
}


def _detect_class(query: str) -> str | None:
    """Longest phrase first so 'carbon dioxide' wins over 'carbon'."""
    q = query.lower()
    for phrase in sorted(_CLASS_WORDS, key=len, reverse=True):
        if phrase in q:
            return _CLASS_WORDS[phrase]
    return None


# event verb/phrase -> a predicate on the event_type string. event_type is
# "{entity_id}:{state}" for HA transitions (":opened", ":closed",
# ":unlocked", ":locked"), "{entity_id}:motion_detected" for motion,
# "{entity_id}:battery_low" for battery, and "uptime:outage"/"uptime:recovery"
# /"uptime:pending" for services — the labels the temporal feature's v3.47.1
# fixes made trustworthy.
#
# Phrases match on WORD BOUNDARIES, not raw substring (audit finding: bare
# substring made "clock" and "blocked" match "lock", so "how many times did
# the clock chime" counted lock events). Bare "down" was removed from the
# outage row for the same reason — it matched inside metric questions like
# "has the co2 gone down"; the explicit verb forms carry the outage intent
# without it.
_EVENT_PATTERNS: list[tuple[tuple[str, ...], str, str]] = [
    # (query phrases, event_type substring to match, human noun)
    (("went down", "go down", "gone down", "outage", "outages", "offline"), ":outage", "service outage"),
    (("came back", "back online", "recovered", "restored", "recovery"), ":recovery", "service recovery"),
    (("motion",), ":motion_detected", "motion event"),
    (("unlocked", "unlock", "unlocks"), ":unlocked", "unlock"),
    (("locked", "lock", "locks"), ":locked", "lock"),
    (("opened", "open", "opens"), ":opened", "opening"),
    (("closed", "close", "closes"), ":closed", "closing"),
    (("battery",), ":battery_low", "low-battery alert"),
]

# Phrases that indicate a COUNT/WHEN question over events even without an
# explicit verb. Bare "how many" was removed (audit finding): it dragged
# metric questions like "how many degrees was it last night" onto the
# events leg, which then couldn't answer them.
_EVENT_COUNT_PHRASES = ("how many times", "how often")


class EventResult(NamedTuple):
    matched: bool
    count: int
    last_ts: str | None
    noun: str


def _event_predicate(query: str) -> tuple[str, str] | None:
    """Return (event_type_substring, human_noun) for the first event verb
    found, else None. Word-boundary matching throughout (see the pattern
    table's comment for the substring false positives this rules out)."""
    import re
    q = query.lower()
    for phrases, suffix, noun in _EVENT_PATTERNS:
        for p in phrases:
            if re.search(r"\b" + re.escape(p) + r"\b", q):
                return suffix, noun
    return None


def query_is_event(query: str) -> bool:
    """True when the query is asking about door/motion/lock/outage events
    (the events leg) rather than a numeric metric.

    The metric-class guard (audit finding): an event VERB alone isn't
    enough when the query also names a numeric metric — "has the co2 gone
    down today" is a trend question about a recorded metric, not a request
    to count service outages, even though "gone down" matches the outage
    row. So a detected metric class routes to the metric leg... with one
    deliberate exception: "battery" is both a device class and an event
    family, and a query pairing it with the battery-alert predicate ("how
    many times did we get a low battery alert") is genuinely asking for the
    event count.
    """
    q = query.lower()
    pred = _event_predicate(query)
    cls = _detect_class(query)
    if pred is not None and cls == "battery" and pred[0] == ":battery_low":
        return True
    if cls is not None:
        return False
    if pred is not None:
        return True
    return any(p in q for p in _EVENT_COUNT_PHRASES)


def count_events(query: str, start_utc: str, end_utc: str,
                 entity_hint: str | None = None) -> EventResult:
    """Count matching temporal_events in [start, end] and report the most
    recent one. Requires TEMPORAL_PATTERN_DETECTION_ENABLED — the events
    leg's single owner of extraction is the temporal miner's cycle; when
    it's off there's simply no event history to read.

    entity_hint (an area/entity keyword, e.g. "front", "office") narrows
    the match to event_types whose key contains it — how "how many times
    did the FRONT DOOR open" resolves to the right sensor."""
    pred = _event_predicate(query)
    if pred is None:
        return EventResult(False, 0, None, "")
    suffix, noun = pred

    like = f"%{suffix}"
    params: list = [like, start_utc, end_utc]
    sql = ("SELECT COUNT(*), MAX(timestamp) FROM temporal_events "
           "WHERE event_type LIKE ? AND timestamp >= ? AND timestamp <= ?")
    if entity_hint:
        sql += " AND event_type LIKE ?"
        params.append(f"%{entity_hint}%")

    try:
        with closing(_connect_temporal_readonly()) as con:
            row = con.execute(sql, params).fetchone()
    except Exception as e:
        _LOGGER.warning("history count_events read failed: %s", e)
        return EventResult(True, 0, None, noun)
    count = row[0] if row and row[0] is not None else 0
    last_ts = row[1] if row else None
    return EventResult(True, count, last_ts, noun)


# ---------------------------------------------------------------------------
# Health — the `history` job entry in /health's background-jobs block
# ---------------------------------------------------------------------------

def get_history_job_health() -> dict:
    """Report the sampler's health for /health. {"status": "disabled"} when
    HISTORY_ENABLED is false (the temporal/adversarial precedent), else
    metrics_tracked / samples_24h / oldest_sample / db_mb / quiet_sensors
    plus a stale/ok/never_ran status vs the interval × grace multiplier."""
    if not settings.history_enabled:
        return {"status": "disabled"}

    now = datetime.now(timezone.utc)
    # The cadence is the HA snapshot job's own — history ingests on that
    # job's ticks rather than fetching for itself, so its staleness math
    # references the real interval instead of a knob that could drift
    # from it.
    from app.snapshots import JOB_INTERVALS_MINUTES
    interval = JOB_INTERVALS_MINUTES.get("ha", 5)
    grace = settings.history_stale_grace_multiplier
    try:
        with closing(_connect(HISTORY_DB)) as con:
            metrics_tracked = con.execute(
                "SELECT COUNT(*) FROM metric_catalog"
            ).fetchone()[0]
            since_24h = (now - timedelta(hours=24)).strftime(TIMESTAMP_FORMAT)
            samples_24h = con.execute(
                "SELECT COUNT(*) FROM metric_samples WHERE ts >= ?", (since_24h,)
            ).fetchone()[0]
            oldest = con.execute(
                "SELECT MIN(ts) FROM metric_samples"
            ).fetchone()[0]
            newest = con.execute(
                "SELECT MAX(ts) FROM metric_samples"
            ).fetchone()[0]
            # A "quiet sensor" is a catalog entry whose last_seen is older
            # than the stale threshold — a Mnemovox node going silent
            # becomes visible for free (constraint / §3's staleness note).
            stale_before = (now - timedelta(minutes=interval * grace)).strftime(TIMESTAMP_FORMAT)
            quiet = con.execute(
                "SELECT COUNT(*) FROM metric_catalog WHERE last_seen < ?",
                (stale_before,),
            ).fetchone()[0]
    except Exception as e:
        return {"status": "unknown", "error": str(e)}

    db_mb = 0.0
    try:
        if os.path.exists(HISTORY_DB):
            db_mb = round(os.path.getsize(HISTORY_DB) / (1024 * 1024), 2)
    except OSError:
        pass

    if newest is None:
        status = "never_ran"
        minutes_since = None
    else:
        try:
            last = datetime.strptime(newest, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
            minutes_since = round((now - last).total_seconds() / 60, 1)
            status = "stale" if minutes_since > interval * grace else "ok"
        except Exception:
            status = "unknown"
            minutes_since = None

    return {
        "status": status,
        "metrics_tracked": metrics_tracked,
        "samples_24h": samples_24h,
        "oldest_sample": oldest,
        "newest_sample": newest,
        "minutes_since_last_sample": minutes_since,
        "db_mb": db_mb,
        "quiet_sensors": quiet,
        "expected_interval_minutes": interval,
    }


# ---------------------------------------------------------------------------
# Value formatting — shared by the source adapter and the endpoints
# ---------------------------------------------------------------------------

def format_value(value: float, unit: str = "") -> str:
    """Human-format a numeric value: integers for large/whole magnitudes,
    one decimal otherwise, with a thousands separator, and the unit
    appended when present. Deterministic — no locale surprises."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if abs(value) >= 100 or float(value).is_integer():
        shown = f"{round(value):,}"
    else:
        shown = f"{value:,.1f}"
    return f"{shown} {unit}".strip()
