"""
Mnemolis Cross-Source Temporal Pattern Detection

A background job, on the same apscheduler infrastructure the snapshot
engine and adversarial self-testing already run on, that looks for
reliable timing relationships between structured event types — does a
front-door lock event reliably precede a motion event within some lag
window, does an HA event reliably precede a service outage — and
reports anything that survives a real statistical bar as a candidate,
never a causal claim.

See the design doc and wiki/Cross-Source-Temporal-Pattern-Detection.md
for the full rationale. The short version, since it's the single most
load-bearing constraint on everything in this file:

CORRELATION FOUND ONCE, EVEN STATISTICALLY CORRECTED, IS NOT THE SAME
THING AS A VALIDATED PATTERN, AND NEITHER IS CAUSATION. Real, peer-
reviewed temporal pattern-mining methods show double-digit false-
positive rates at data volumes far above what Mnemolis will ever
realistically see (Raajay, Sastry, Unnikrishnan 2010,
arxiv.org/pdf/1006.1543 — their own best-performing method, explicitly
engineered to bound false positives, still showed 15-48% false-positive
rates at 50k-200k events; Mnemolis's real event volume in any practical
window is tens to low hundreds). Testing many (event type A, event type
B, lag bucket) combinations without correction produces spurious
"discoveries" as a mathematical certainty, independent of data quality
(arxiv.org/pdf/1504.06896 — 4 uncorrected hypotheses alone produced a
15% false-positive rate, matching 1 - 0.95^4 almost exactly). Every
design decision below — the fixed lag window, the per-comparison
Bonferroni correction, the hard minimum-occurrence floor, the
mandatory out-of-sample re-validation before anything is called
"confirmed" — exists to keep this feature honest about what it can
and cannot actually claim, not to make it look more rigorous than it
is.

Scope, deliberately narrow for this first version: `ha`-internal event
pairs (lock/door/battery transitions against each other) and `ha`-to-
coarse-`uptime` pairs (any ha event against a source-level uptime
outage/recovery signal). `forecast` and `news` event extraction is
explicitly deferred — neither source's current snapshot shape
(free-text, not structured) supports clean event typing without new
groundwork that's out of scope here. See the design doc section 4 for
the full reasoning on why this scope line was drawn where it was.
"""
import logging
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone, timedelta

from app.config import settings
from app.snapshots import _iter_ha_entity_changes, _diff_uptime, _get_snapshots_since

_LOGGER = logging.getLogger(__name__)

TEMPORAL_PATTERNS_DB = "/app/data/temporal_patterns.db"

# Sources this first version actually extracts structured events from.
# Deliberately not a general "every snapshot source" loop — see the
# module docstring's scope note. Listed here, not inferred from
# snapshots.JOB_INTERVALS_MINUTES, since extraction logic per source is
# genuinely different (ha is structured JSON; uptime needs its own
# coarse outage/recovery framing) and a third source joining this list
# later needs its own real extractor, not an automatic opt-in.
_EXTRACTABLE_SOURCES = ("ha", "uptime")


def _connect(db_path: str) -> sqlite3.Connection:
    """Mirrors main.py's / snapshots.py's / adversarial_testing.py's
    _connect() exactly — WAL mode, busy timeout, consistent with every
    other Mnemolis SQLite database."""
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def init_temporal_patterns_db():
    """Create the temporal_events and temporal_patterns tables if they
    don't exist. Mirrors snapshots.init_snapshot_db() / adversarial_
    testing.init_adversarial_db()'s exact pattern."""
    try:
        with closing(_connect(TEMPORAL_PATTERNS_DB)) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS temporal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    raw_detail TEXT
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_temporal_events_type_time
                    ON temporal_events (event_type, timestamp)
            """)

            # Patterns table — one row per (event_type_a, event_type_b,
            # lag_bucket_minutes) combination ever found as a candidate.
            # Status transitions: candidate -> confirmed (passed out-of-
            # sample re-check) or candidate -> unconfirmed (failed it).
            # History is never deleted on a failed re-check, the same
            # "status changes, rows don't disappear" philosophy already
            # established for adversarial self-testing's dismiss mechanism
            # — an honestly-reported "didn't replicate" is real, useful
            # information, not noise to clean up.
            con.execute("""
                CREATE TABLE IF NOT EXISTS temporal_patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type_a TEXT NOT NULL,
                    event_type_b TEXT NOT NULL,
                    lag_window_minutes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    raw_count INTEGER NOT NULL,
                    expected_count_null REAL NOT NULL,
                    p_value REAL NOT NULL,
                    corrected_threshold REAL NOT NULL,
                    num_comparisons_in_pass INTEGER NOT NULL,
                    discovery_window_start TEXT NOT NULL,
                    discovery_window_end TEXT NOT NULL,
                    validation_window_start TEXT,
                    validation_window_end TEXT,
                    validation_raw_count INTEGER,
                    first_found_timestamp TEXT NOT NULL,
                    last_checked_timestamp TEXT NOT NULL,
                    UNIQUE(event_type_a, event_type_b, lag_window_minutes, discovery_window_start)
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_temporal_patterns_status
                    ON temporal_patterns (status)
            """)

            # Tracks the mining cycle's own run history, separately from
            # individual pattern rows, the same reason
            # get_snapshot_job_health() and get_adversarial_test_summary()
            # both need their own "when did this job last actually run"
            # signal distinct from the data it produced — a cycle that ran
            # and genuinely found zero candidates needs a different status
            # than a cycle that never ran at all, and neither is visible
            # from the temporal_patterns table alone if a given run found
            # nothing.
            con.execute("""
                CREATE TABLE IF NOT EXISTS temporal_mining_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_timestamp TEXT NOT NULL,
                    events_considered INTEGER NOT NULL,
                    comparisons_run INTEGER NOT NULL,
                    candidates_found INTEGER NOT NULL
                )
            """)

            con.commit()
        _LOGGER.info("Temporal patterns DB initialized")
    except Exception as e:
        _LOGGER.warning("Could not initialize temporal patterns DB: %s", e)


def _store_event(source: str, event_type: str, timestamp: str, raw_detail: str = ""):
    """Store one structured event. Never raises — mirrors every other
    background-job storage function in this codebase (_store_snapshot,
    adversarial_testing's combination upserts): a storage failure here
    should log and move on, not take down the calling extraction loop
    or, worse, the scheduler itself."""
    try:
        with closing(_connect(TEMPORAL_PATTERNS_DB)) as con:
            con.execute(
                "INSERT INTO temporal_events (source, event_type, timestamp, raw_detail) VALUES (?, ?, ?, ?)",
                (source, event_type, timestamp, raw_detail),
            )
            con.commit()
    except Exception as e:
        _LOGGER.warning("Could not store temporal event '%s' for '%s': %s", event_type, source, e)


# ---------------------------------------------------------------------------
# Structured event extraction
# ---------------------------------------------------------------------------

def extract_ha_events(old_json: str, new_json: str) -> list[dict]:
    """Extract structured (entity_id, event_type) events from a pair of
    HA snapshots.

    Deliberately built on _iter_ha_entity_changes() — the same shared
    comparison core app/snapshots.py's _diff_ha() uses for its own
    free-text "what changed" output — rather than a second, independent
    re-implementation of the same entity comparison, or worse, regex-
    parsing _diff_ha()'s already-formatted English sentences. Two
    independently-maintained copies of "what counts as a meaningful HA
    change" is exactly the kind of drift risk that already bit this
    project once (router.py/fusion.py's "_looks_empty" phrase list) —
    there's deliberately only one definition here.

    Each returned dict has: {"event_type", "timestamp" (caller fills
    this in — a single diff pair doesn't carry its own timestamp),
    "raw_detail"}. event_type is built as "{entity_id}:{state}" for
    lock/door/window/opening events (e.g. "lock.front_door:locked",
    "binary_sensor.back_door:opened"), "{entity_id}:motion_detected"
    for motion events, and "{entity_id}:battery_low" for battery-
    crossing events — fine-grained enough to distinguish which specific
    entity changed, which matters for finding a genuinely reliable
    (door, motion) relationship rather than conflating every door in
    the house into one event type.

    Explicit per-kind branches rather than a two-way if/else — found
    via direct testing after _iter_ha_entity_changes() gained motion/
    window/opening branches: the original if/else here only
    distinguished "lock or door" from everything else (assumed to mean
    battery_low), so every new kind silently fell into the
    battery_low branch and got mislabeled (a real motion event was
    reported as ":battery_low" instead of ":motion_detected"). Listing
    every real kind explicitly means a future new kind added to
    _iter_ha_entity_changes() without a matching update here fails
    loudly (KeyError on the unhandled match, surfaced as a logged
    warning by run_event_extraction_cycle()'s own per-change try/except)
    rather than silently mislabeling itself as whatever kind happened
    to be the previous catch-all.
    """
    events = []
    for change in _iter_ha_entity_changes(old_json, new_json):
        if change["kind"] in ("lock", "door", "window", "opening"):
            event_type = f"{change['entity_id']}:{change['state']}"
        elif change["kind"] == "motion":
            event_type = f"{change['entity_id']}:motion_detected"
        elif change["kind"] == "battery_low":
            event_type = f"{change['entity_id']}:battery_low"
        else:
            # A real, future kind added to _iter_ha_entity_changes()
            # without a matching branch here — fail loudly rather than
            # silently mislabeling, per the reasoning above.
            raise ValueError(f"Unhandled HA change kind in extract_ha_events: {change['kind']!r}")
        events.append({"event_type": event_type, "raw_detail": change["description"]})
    return events


def extract_uptime_events(old: str, new: str) -> list[dict]:
    """Extract a coarse, source-level uptime event from a pair of
    uptime snapshots.

    Deliberately reuses _diff_uptime() directly rather than
    re-implementing its own "down"/"pending"/"all up" text parsing —
    same one-source-of-truth reasoning as extract_ha_events() above.
    This is coarse by design (see the design doc section 4.2): per-
    monitor structured events would require extending snapshot_uptime()
    to also store Uptime Kuma's per-monitor API data, which is real,
    separate, not-yet-built groundwork, explicitly out of scope for
    this first version. "Something, somewhere, went down" is the
    honest ceiling of what's extractable from today's free-text uptime
    snapshots without that groundwork.

    Found via direct testing: a first version classified by checking
    `"outage" in d_lower` first — but _diff_uptime()'s OWN recovery
    message ("All services restored — previously reported outage
    resolved") and its OWN pending message ("Service check pending
    (possible outage starting): ...") both genuinely contain the
    literal substring "outage", so checking for it before the more
    specific "pending"/"restored"/"resolved" phrases misclassified both
    as plain outages. Fixed by matching each message's own distinct,
    unambiguous leading phrase (confirmed exhaustively against every
    real message _diff_uptime() can produce, listed in its own
    docstring/source directly) instead of a shared substring multiple
    message types happen to contain.
    """
    diffs = _diff_uptime(old, new)
    events = []
    for d in diffs:
        d_lower = d.lower()
        if d_lower.startswith("service check pending"):
            events.append({"event_type": "uptime:pending", "raw_detail": d})
        elif d_lower.startswith("service outage detected"):
            events.append({"event_type": "uptime:outage", "raw_detail": d})
        elif d_lower.startswith("all services restored"):
            events.append({"event_type": "uptime:recovery", "raw_detail": d})
        else:
            events.append({"event_type": "uptime:status_change", "raw_detail": d})
    return events


_EXTRACTORS = {
    "ha": extract_ha_events,
    "uptime": extract_uptime_events,
}


def run_event_extraction_cycle():
    """Walk every consecutive snapshot pair for each extractable source
    since the last extraction run, extract structured events, and store
    them in temporal_events.

    Run on the same schedule as the mining cycle (see
    run_temporal_pattern_mining_cycle()) rather than on every individual
    snapshot — events are cheap to extract in a single batched pass and
    this avoids adding per-snapshot overhead to the snapshot engine's
    own, separately-tuned scheduler jobs. Walking ALL consecutive pairs
    since the last run (not just the two most recent snapshots) means a
    24-hour gap between mining cycles never silently drops events that
    occurred between cycles — every event-based snapshot source already
    keeps a full week of history (see app/snapshots.py's
    _RETENTION_PER_SOURCE), more than enough headroom for this.
    """
    total_extracted = 0
    for source in _EXTRACTABLE_SOURCES:
        extractor = _EXTRACTORS[source]
        try:
            # 8 days of lookback, not 7 — a deliberate one-day overlap
            # with the retention window's own 7-day floor, so a mining
            # cycle that's running slightly behind schedule (or
            # catching up after downtime) doesn't lose events sitting
            # right at the retention boundary to an off-by-a-little gap
            # between "how far back retention guarantees data exists"
            # and "how far back this extraction pass actually looks".
            snapshots = _get_snapshots_since(source, since_hours=8 * 24)
        except Exception as e:
            _LOGGER.warning("Temporal event extraction: could not fetch snapshots for '%s': %s", source, e)
            continue

        if len(snapshots) < 2:
            continue

        for i in range(len(snapshots) - 1):
            ts_old, content_old = snapshots[i]
            ts_new, content_new = snapshots[i + 1]
            try:
                events = extractor(content_old, content_new)
            except Exception as e:
                _LOGGER.warning("Temporal event extraction failed for '%s' at %s: %s", source, ts_new, e)
                continue

            for event in events:
                # Deduplicated downstream by the UNIQUE-less but
                # naturally idempotent nature of re-extracting the same
                # snapshot pair producing the same (event_type,
                # timestamp) — accepted as a real, bounded cost rather
                # than adding a uniqueness constraint that would need
                # its own conflict-handling logic; re-running extraction
                # over the same already-processed pairs is the actual
                # steady-state behavior given the 8-day lookback above,
                # see _dedupe_events_table() below for how this gets
                # cleaned up.
                _store_event(source, event["event_type"], ts_new, event["raw_detail"])
                total_extracted += 1

    if total_extracted:
        _dedupe_events_table()

    _LOGGER.info("Temporal event extraction: %d events stored (post-dedup)", total_extracted)
    return total_extracted


def _dedupe_events_table():
    """Collapse exact-duplicate (source, event_type, timestamp,
    raw_detail) rows down to one, keeping the lowest id.

    Needed because run_event_extraction_cycle() deliberately re-walks
    an 8-day lookback window on every run rather than tracking a
    fragile "last processed snapshot id" cursor (which would need its
    own crash-recovery story) — the simplicity tradeoff is that the
    same real event gets re-extracted every cycle until it ages out of
    the lookback window, and this cleans that up after the fact rather
    than preventing it up front.
    """
    try:
        with closing(_connect(TEMPORAL_PATTERNS_DB)) as con:
            con.execute("""
                DELETE FROM temporal_events WHERE id NOT IN (
                    SELECT MIN(id) FROM temporal_events
                    GROUP BY source, event_type, timestamp, raw_detail
                )
            """)
            con.commit()
    except Exception as e:
        _LOGGER.warning("Could not dedupe temporal_events: %s", e)


# ---------------------------------------------------------------------------
# Statistical mining
# ---------------------------------------------------------------------------

def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _get_events_in_window(con: sqlite3.Connection, start: datetime, end: datetime) -> list[tuple[str, datetime]]:
    """Return every (event_type, timestamp) pair in [start, end), sorted
    by timestamp ascending."""
    rows = con.execute(
        "SELECT event_type, timestamp FROM temporal_events WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC",
        (_fmt_ts(start), _fmt_ts(end)),
    ).fetchall()
    return sorted(((r[0], _parse_ts(r[1])) for r in rows), key=lambda x: x[1])


def _count_nonoverlapping_occurrences(
    events: list[tuple[str, datetime]], type_a: str, type_b: str, lag_minutes: int
) -> int:
    """Count non-overlapping occurrences of "B within lag_minutes after
    A" — per the design doc's section 2.3 frequent-episode-mining
    lesson: once a B has been claimed by some A as a real occurrence of
    this pair, that same B can never be claimed again by a different A.
    Without this, a burst of 5 A's followed by 5 B's within the lag
    window would inflate the apparent count to as many as 25 (every
    A-B combination), wildly overstating how often this relationship
    actually fires versus how many raw events happened to occur close
    together.

    Found via a real bug in this function's own first draft, caught by
    deliberately testing a harder, more realistic scenario than the
    simple single-pair case: a first version advanced the scan
    position to just past whichever B got claimed, which correctly
    prevented that B from being double-claimed but ALSO silently
    skipped over any genuine, not-yet-evaluated A occurrences sitting
    between the claiming A and the B it claimed (e.g. A,A,A,B,B,B all
    within the lag window — the first A claims the first B, then the
    scan jumps straight to the second B, never giving the second and
    third A's their own chance to be counted at all). The fix tracks
    which B INDICES have already been claimed in a separate set, and
    advances the outer scan one A at a time regardless of what any
    earlier A claimed — every real A occurrence gets evaluated exactly
    once, and every real B occurrence gets claimed at most once, the
    two guarantees this function actually needs, independently of each
    other.

    `events` must already be sorted by timestamp ascending — the
    caller (_get_events_in_window) guarantees this.
    """
    lag = timedelta(minutes=lag_minutes)
    claimed_b_indices = set()
    count = 0
    n = len(events)

    for i in range(n):
        et, ts = events[i]
        if et != type_a:
            continue
        # Look forward for the first genuine, not-yet-claimed B within
        # the lag window.
        for j in range(i + 1, n):
            b_et, b_ts = events[j]
            if b_ts - ts > lag:
                break
            if b_et == type_b and j not in claimed_b_indices:
                claimed_b_indices.add(j)
                count += 1
                break

    return count


def _base_rate_per_minute(events: list[tuple[str, datetime]], event_type: str, window_minutes: float) -> float:
    """Each event type's own real, observed base rate (events per
    minute) over the window actually being mined — not an assumed
    uniform rate across all event types. A source that fires
    constantly needs a much higher raw co-occurrence count to be
    meaningfully non-random than a source that almost never fires; this
    is the number that lets the null-hypothesis expected count below
    actually reflect that asymmetry."""
    if window_minutes <= 0:
        return 0.0
    n = sum(1 for et, _ in events if et == event_type)
    return n / window_minutes


def _expected_count_under_null(
    events: list[tuple[str, datetime]], type_a: str, type_b: str, lag_minutes: int, window_minutes: float
) -> float:
    """Expected number of "B within lag_minutes after A" occurrences if
    A and B were independent — i.e. if B's occurrences were uniformly
    scattered through time at its own real observed rate, with no
    actual relationship to where A happened to occur.

    For each real occurrence of A, the expected number of independent B
    events landing in the following lag_minutes window is simply
    (B's base rate) * lag_minutes — a direct application of a Poisson-
    process expectation. Summed across every real A occurrence gives
    the total expected count under the null. This deliberately ignores
    the non-overlapping-counting correction that the REAL observed
    count uses (section 2.3) — the null-hypothesis expectation is a
    simpler, more conservative approximation (it doesn't account for
    the fact that two A's close together can't both independently
    "claim" the same B), which biases toward UNDER-estimating the null
    expectation, i.e. toward being more conservative about claiming
    something is non-random, the safer direction for this exact
    feature's stated low-stakes-with-real-claims framing.
    """
    rate_b = _base_rate_per_minute(events, type_b, window_minutes)
    n_a = sum(1 for et, _ in events if et == type_a)
    return n_a * rate_b * lag_minutes


def _poisson_sf(k: int, mean: float) -> float:
    """P(X >= k) for X ~ Poisson(mean) — the right-tailed p-value for
    "is the real observed count significantly higher than the null-
    hypothesis expected count." Computed directly from the Poisson PMF
    rather than pulling in scipy, since this is the one specific tail
    probability this module needs and the dependency isn't otherwise
    justified. Numerically fine for the realistic count/mean ranges
    this feature will ever see (single-to-low-triple-digit counts,
    given the design doc's own data-volume findings) — this is not
    intended as a general-purpose statistics utility.
    """
    if mean <= 0:
        return 0.0 if k > 0 else 1.0
    if k <= 0:
        return 1.0
    # P(X >= k) = 1 - P(X <= k-1), computed by summing the PMF directly
    # rather than via the regularized incomplete gamma function, to
    # avoid a scipy dependency for this one call site.
    log_mean = math.log(mean)
    log_term = -mean  # log(P(X=0)) = -mean
    cumulative = math.exp(log_term)
    for i in range(1, k):
        log_term += log_mean - math.log(i)
        cumulative += math.exp(log_term)
    return max(0.0, min(1.0, 1.0 - cumulative))


def run_temporal_pattern_mining_cycle() -> dict:
    """The scheduled mining job body.

    Checks TEMPORAL_PATTERN_DETECTION_ENABLED itself, not just at
    scheduler-registration time — mirrors run_adversarial_test_cycle()'s
    exact defense-in-depth pattern, so a direct call (including a
    future manual-trigger endpoint) can never accidentally mine real
    event data while the feature is supposed to be off.

    Returns {"status": "disabled"} or a real summary dict — never
    raises; every internal step is wrapped so one bad comparison or one
    DB hiccup can't take down the whole cycle, the same defensive
    posture every other background job in this codebase already uses.
    """
    if not settings.temporal_pattern_detection_enabled:
        _LOGGER.info("Temporal pattern detection is disabled (TEMPORAL_PATTERN_DETECTION_ENABLED=false); skipping cycle")
        return {"status": "disabled", "events_considered": 0, "comparisons_run": 0, "candidates_found": 0}

    try:
        run_event_extraction_cycle()
    except Exception as e:
        _LOGGER.warning("Temporal pattern mining: event extraction failed, mining skipped this cycle: %s", e)
        return {"status": "error", "error": str(e)}

    now = datetime.now(timezone.utc)
    lag_minutes = settings.temporal_pattern_lag_window_minutes
    min_occurrences = settings.temporal_pattern_min_occurrences
    alpha = settings.temporal_pattern_significance_level

    # First, re-validate any existing candidates whose validation
    # window has now closed — done before mining for new candidates so
    # a candidate found in a previous cycle gets checked against
    # genuinely new data, never data this same cycle is about to fold
    # into a future discovery window.
    try:
        _revalidate_due_candidates(now)
    except Exception as e:
        _LOGGER.warning("Temporal pattern mining: out-of-sample re-validation step failed: %s", e)

    discovery_window_hours = settings.temporal_pattern_mining_interval_hours
    window_start = now - timedelta(hours=discovery_window_hours)

    try:
        con = _connect(TEMPORAL_PATTERNS_DB)
        events = _get_events_in_window(con, window_start, now)
    except Exception as e:
        _LOGGER.warning("Temporal pattern mining: could not load events for mining window: %s", e)
        return {"status": "error", "error": str(e)}

    distinct_types = sorted(set(et for et, _ in events))
    window_minutes = (now - window_start).total_seconds() / 60

    # Every distinct ORDERED pair (A, B) with A != B that has actually
    # occurred — not the full combinatorial space of every type that
    # could theoretically exist (design doc section 5.2, step 1).
    # Order matters: "A precedes B" and "B precedes A" are different,
    # independently-testable claims about the same two event types.
    pairs = [(a, b) for a in distinct_types for b in distinct_types if a != b]
    num_comparisons = len(pairs)  # one lag bucket only in this version — see note below

    candidates_found = 0
    if num_comparisons > 0:
        corrected_alpha = alpha / num_comparisons  # Bonferroni correction (design doc section 2.2/5.2 step 5)

        for type_a, type_b in pairs:
            try:
                raw_count = _count_nonoverlapping_occurrences(events, type_a, type_b, lag_minutes)
                if raw_count < min_occurrences:
                    # Hard floor — never even run the significance test
                    # below this, regardless of what it would say.
                    # Design doc section 2.4 / requirement: a pattern
                    # from 2-3 raw occurrences isn't a pattern yet, no
                    # matter how it'd score.
                    continue

                expected = _expected_count_under_null(events, type_a, type_b, lag_minutes, window_minutes)
                p_value = _poisson_sf(raw_count, expected)

                if p_value < corrected_alpha:
                    _store_candidate(
                        con, type_a, type_b, lag_minutes, raw_count, expected,
                        p_value, corrected_alpha, num_comparisons, window_start, now,
                    )
                    candidates_found += 1
            except Exception as e:
                _LOGGER.warning("Temporal pattern mining: comparison (%s -> %s) failed: %s", type_a, type_b, e)
                continue

    try:
        con.execute(
            "INSERT INTO temporal_mining_runs (run_timestamp, events_considered, comparisons_run, candidates_found) VALUES (?, ?, ?, ?)",
            (_fmt_ts(now), len(events), num_comparisons, candidates_found),
        )
        con.commit()
    except Exception as e:
        _LOGGER.warning("Temporal pattern mining: could not record run summary: %s", e)
    finally:
        # Guaranteed close even when the summary INSERT raises — the
        # previous inline con.close() on the success path leaked the
        # connection whenever recording the run summary failed. See
        # query_log_stats() in main.py for the pass-wide rationale.
        con.close()

    _LOGGER.info(
        "Temporal pattern mining cycle complete: %d events, %d comparisons, %d candidates found",
        len(events), num_comparisons, candidates_found,
    )
    return {
        "status": "ran",
        "events_considered": len(events),
        "comparisons_run": num_comparisons,
        "candidates_found": candidates_found,
    }


def _store_candidate(
    con: sqlite3.Connection, type_a: str, type_b: str, lag_minutes: int,
    raw_count: int, expected: float, p_value: float, corrected_threshold: float,
    num_comparisons: int, window_start: datetime, window_end: datetime,
):
    """Insert a new candidate pattern, or update an existing row for the
    exact same (type_a, type_b, lag, discovery_window_start) — the
    UNIQUE constraint on temporal_patterns means re-running mining
    against an identical discovery window (e.g. a manually-triggered
    re-run) updates rather than duplicates. A genuinely NEW discovery
    window (the next scheduled cycle) always produces a new row, since
    discovery_window_start is part of the uniqueness key and advances
    every cycle.
    """
    now_str = _fmt_ts(datetime.now(timezone.utc))
    con.execute(
        """
        INSERT INTO temporal_patterns (
            event_type_a, event_type_b, lag_window_minutes, status,
            raw_count, expected_count_null, p_value, corrected_threshold,
            num_comparisons_in_pass, discovery_window_start, discovery_window_end,
            first_found_timestamp, last_checked_timestamp
        ) VALUES (?, ?, ?, 'candidate', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_type_a, event_type_b, lag_window_minutes, discovery_window_start)
        DO UPDATE SET
            raw_count = excluded.raw_count,
            expected_count_null = excluded.expected_count_null,
            p_value = excluded.p_value,
            corrected_threshold = excluded.corrected_threshold,
            num_comparisons_in_pass = excluded.num_comparisons_in_pass,
            last_checked_timestamp = excluded.last_checked_timestamp
        """,
        (
            type_a, type_b, lag_minutes, raw_count, expected, p_value, corrected_threshold,
            num_comparisons, _fmt_ts(window_start), _fmt_ts(window_end), now_str, now_str,
        ),
    )
    con.commit()


def _revalidate_due_candidates(now: datetime):
    """Out-of-sample re-validation (design doc section 5.3): any
    'candidate' row whose discovery window closed at least
    TEMPORAL_PATTERN_VALIDATION_WINDOW_HOURS ago gets re-checked against
    a genuinely later, non-overlapping window of that same length,
    starting exactly where the discovery window left off — never the
    same data it was originally found in.

    A pattern that still clears the SAME corrected threshold it was
    originally judged against in the new window is promoted to
    'confirmed'. One that doesn't is marked 'unconfirmed' — recorded,
    not deleted, per requirement #6/#8's "history isn't silently
    dropped" philosophy already established for adversarial self-
    testing's dismiss mechanism.

    Deliberately re-uses the ORIGINAL pattern's own corrected_threshold
    rather than recomputing a fresh one against the validation window's
    own (likely different) comparison count — the claim being tested
    here is narrow and specific: "does the EXACT SAME finding replicate
    against new data," not "would this also be found fresh, today, on
    its own." Recomputing the threshold would conflate those two
    different questions.
    """
    validation_hours = settings.temporal_pattern_validation_window_hours
    con = _connect(TEMPORAL_PATTERNS_DB)
    try:
        due = con.execute(
            "SELECT id, event_type_a, event_type_b, lag_window_minutes, corrected_threshold, discovery_window_end "
            "FROM temporal_patterns WHERE status = 'candidate'"
        ).fetchall()

        for row in due:
            pattern_id, type_a, type_b, lag_minutes, corrected_threshold, discovery_end_str = row
            discovery_end = _parse_ts(discovery_end_str)
            validation_start = discovery_end
            validation_end = validation_start + timedelta(hours=validation_hours)

            if now < validation_end:
                continue  # validation window hasn't closed yet

            validation_events = _get_events_in_window(con, validation_start, validation_end)
            validation_window_minutes = (validation_end - validation_start).total_seconds() / 60
            validation_count = _count_nonoverlapping_occurrences(validation_events, type_a, type_b, lag_minutes)
            expected = _expected_count_under_null(validation_events, type_a, type_b, lag_minutes, validation_window_minutes)
            p_value = _poisson_sf(validation_count, expected)

            new_status = "confirmed" if (
                validation_count >= settings.temporal_pattern_min_occurrences
                and p_value < corrected_threshold
            ) else "unconfirmed"

            con.execute(
                """
                UPDATE temporal_patterns SET
                    status = ?, validation_window_start = ?, validation_window_end = ?,
                    validation_raw_count = ?, last_checked_timestamp = ?
                WHERE id = ?
                """,
                (new_status, _fmt_ts(validation_start), _fmt_ts(validation_end),
                 validation_count, _fmt_ts(now), pattern_id),
            )
            _LOGGER.info(
                "Temporal pattern re-validation: %s -> %s (lag %dm) %s -> %s (validation count=%d)",
                type_a, type_b, lag_minutes, "candidate", new_status, validation_count,
            )
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

_CORRELATION_DISCLAIMER = (
    "This reflects observed timing correlation only and does not establish a causal relationship."
)


def get_temporal_patterns(status: str | None = None, limit: int = 100) -> list[dict]:
    """Return pattern rows for GET /temporal-patterns, each with the
    literal correlation-not-causation disclaimer attached directly on
    every row (requirement #5 / #8 in §8's definition of done) — not
    just in documentation a person might not read.
    """
    try:
        with closing(_connect(TEMPORAL_PATTERNS_DB)) as con:
            if status:
                rows = con.execute(
                    "SELECT * FROM temporal_patterns WHERE status = ? ORDER BY first_found_timestamp DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM temporal_patterns ORDER BY first_found_timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            columns = [d[0] for d in con.execute("SELECT * FROM temporal_patterns LIMIT 0").description]
    except Exception as e:
        _LOGGER.warning("Could not fetch temporal patterns: %s", e)
        return []

    results = []
    for row in rows:
        d = dict(zip(columns, row))
        d["note"] = _CORRELATION_DISCLAIMER
        results.append(d)
    return results


def get_temporal_pattern_summary() -> dict:
    """Summary for /health. Mirrors get_snapshot_job_health() and
    get_adversarial_test_summary()'s naming convention and overall
    shape exactly — status, last_run, and counts that make "has this
    found anything yet" answerable at a glance.

    Reports "disabled" up front when TEMPORAL_PATTERN_DETECTION_ENABLED
    is false, the same reason get_adversarial_test_summary() does — a
    deliberate off-switch shouldn't eventually read as "stale" the way
    a job that silently stopped running should.

    Per the design doc's section 7 open question #1 and the resulting
    decision: reports a real, distinct "insufficient_data" status —
    separate from "never_ran" (the job hasn't fired at all yet) — for
    the realistic, expected case where the job HAS run, correctly, but
    genuinely hasn't found enough raw events yet for
    TEMPORAL_PATTERN_MIN_OCCURRENCES to be satisfied for any pattern.
    This is the honest, expected steady state for the first weeks of
    this feature's life (design doc section 2.1's volume estimate), not
    a bug — and deserves its own visible label rather than being
    indistinguishable from "ran and found genuinely nothing after a
    real, meaningful amount of data" or "is broken."
    """
    if not settings.temporal_pattern_detection_enabled:
        return {"status": "disabled"}

    try:
        with closing(_connect(TEMPORAL_PATTERNS_DB)) as con:
            last_run_row = con.execute(
                "SELECT run_timestamp, events_considered FROM temporal_mining_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            total_candidates = con.execute(
                "SELECT COUNT(*) FROM temporal_patterns"
            ).fetchone()[0]
            confirmed = con.execute(
                "SELECT COUNT(*) FROM temporal_patterns WHERE status = 'confirmed'"
            ).fetchone()[0]
            unconfirmed = con.execute(
                "SELECT COUNT(*) FROM temporal_patterns WHERE status = 'unconfirmed'"
            ).fetchone()[0]
            pending_candidates = con.execute(
                "SELECT COUNT(*) FROM temporal_patterns WHERE status = 'candidate'"
            ).fetchone()[0]
    except Exception as e:
        return {"status": "unknown", "error": str(e)}

    if last_run_row is None:
        return {"status": "never_ran"}

    last_run, events_considered = last_run_row

    try:
        last_run_dt = _parse_ts(last_run)
        minutes_since = (datetime.now(timezone.utc) - last_run_dt).total_seconds() / 60
    except Exception:
        minutes_since = None

    interval_minutes = settings.temporal_pattern_mining_interval_hours * 60
    is_stale = (
        minutes_since is not None
        and minutes_since > interval_minutes * settings.temporal_pattern_stale_grace_multiplier
    )

    if is_stale:
        status = "stale"
    elif total_candidates == 0 and events_considered < settings.temporal_pattern_min_occurrences:
        # The job ran correctly, but the real, observed event volume
        # in the most recent mining window was itself below the floor
        # this feature requires before ANY pair could ever be
        # considered — the honest "running, but not enough data yet"
        # state from design doc section 7's open question #1, distinct
        # from "ran on real data and found nothing significant."
        status = "insufficient_data"
    else:
        status = "ok"

    return {
        "status": status,
        "last_run": last_run,
        "minutes_since_last_run": round(minutes_since, 1) if minutes_since is not None else None,
        "events_in_last_window": events_considered,
        "total_candidates": total_candidates,
        "confirmed_patterns": confirmed,
        "unconfirmed_patterns": unconfirmed,
        "pending_candidates": pending_candidates,
    }
