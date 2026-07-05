"""
Mnemolis shared timezone conversion utility.

Exists because of a real, previously-uncatalogued gap found during research
for two separate, not-yet-built design docs (Predictive Pre-Fetching with
Confidence Calibration, Ambient Intent Disambiguation Through Context): every
timestamp this project writes to a database — query_log.db (app/main.py's
_log_query()), snapshots.db, adversarial_testing.db, temporal_patterns.db —
is hardcoded UTC via time.gmtime()/datetime.now(timezone.utc), confirmed
directly across every one of those call sites. Meanwhile, the project's own
EXISTING "local time" logic — app/router.py's _hours_since(), which resolves
phrases like "this morning" and "while at work" — uses datetime.now() (naive
local time), sourced entirely from the container's OS-level TZ environment
variable (documented in README.md's "Timezone configuration" section), with
no reference to anything in app/config.py at all.

These are two different, previously-unreconciled mechanisms for "what time is
it for this person" already coexisting in this codebase. Any feature that
needs to bucket a STORED, UTC timestamp by local hour-of-day or day-of-week —
which both of the design docs above need to do, for "did you ask this every
morning at 7am"-style pattern mining — was about to either invent its own,
third, independent timezone-handling approach, or (more likely, and far worse)
silently bucket by raw UTC hour-of-day, which is only correct for a deployment
physically in the UTC timezone. For Mnemolis's own real reference deployment
(Kingman, AZ — America/Phoenix, UTC-7, no DST), that mistake would silently
shift every single time-of-day bucket by exactly 7 hours, forever, with no
error or warning anywhere — exactly the class of bug this project's own
bulletproofing-pass culture exists to catch before it ships, not after.

This module is the one, single, shared answer to that gap: settings.local_timezone
(app/config.py) names the SAME timezone concept _hours_since() already
implicitly depends on via the OS's TZ variable — defaulting to read that exact
same environment variable, so a deployment that has already correctly set TZ
per the README gets this conversion capability for free, with zero new
configuration burden. A deployment that explicitly wants this conversion to
use a DIFFERENT zone than whatever TZ happens to be set to can still override
it directly via LOCAL_TIMEZONE, the normal pydantic-settings precedence rule
(confirmed directly: an explicit env var always wins over a Python-level
default expression).

Every future feature that needs to bucket a UTC timestamp by local time
(Predictive Pre-Fetching's mining job, a future query-shape-clustering module
shared with Self-Healing Source Selection, Ambient Intent Disambiguation's
own time-of-day signal) should import from here, not reimplement this.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import settings

_LOGGER = logging.getLogger(__name__)

# The exact timestamp format every database in this project already writes —
# confirmed identical across app/main.py's _log_query(), app/snapshots.py,
# and app/temporal_patterns.py's own _fmt_ts()/_parse_ts(). Defined once here
# rather than re-typed at every call site that needs it.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _resolve_zone() -> ZoneInfo:
    """Resolve settings.local_timezone into a real ZoneInfo, falling back to
    UTC on any invalid/unrecognized zone name rather than crashing.

    A typo in a deployment's TZ (or an explicit LOCAL_TIMEZONE override) is a
    genuinely real, plausible mistake — the same class of risk
    morning_start_hour's own "% 24" defensive fix (app/router.py's
    _hours_since()) already guards against for a different setting. Falling
    back to UTC here means a misconfigured zone degrades to "every bucket is
    computed in UTC" (the same default behavior every part of this project
    already had before this module existed), not a hard crash — confirmed
    directly: zoneinfo.ZoneInfo() raises ZoneInfoNotFoundError, a real,
    catchable exception, for any unrecognized key.

    Not cached across calls deliberately — settings.local_timezone is a
    single, fixed value for the life of one running process (set once at
    container startup, the same as every other env-var-sourced setting in
    this project), so re-resolving it per call costs a cheap, already-fast
    stdlib lookup rather than meaningfully more than that, and avoids a
    separate cache-invalidation question entirely for a value that's already
    documented to come from a tested-instance-friendly Settings field — the
    same simplicity-over-premature-optimization judgment call this project
    already made for _hours_since() itself, which similarly recomputes
    datetime.now() on every call rather than caching it.
    """
    try:
        return ZoneInfo(settings.local_timezone)
    except (ZoneInfoNotFoundError, ValueError) as e:
        _LOGGER.warning(
            "Invalid LOCAL_TIMEZONE/TZ value '%s' (%s) — falling back to UTC. "
            "Check for a typo; valid examples: 'America/New_York', 'Europe/London'.",
            settings.local_timezone, e,
        )
        return ZoneInfo("UTC")


def utc_string_to_local(timestamp: str) -> datetime:
    """Convert a stored UTC timestamp string (this project's universal
    TIMESTAMP_FORMAT) into a real, timezone-aware local datetime.

    This is the one function every future time-of-day/day-of-week bucketing
    pass in this project should call — never datetime.strptime() directly
    against a stored timestamp followed by naive .hour/.weekday() access,
    which would silently bucket by UTC hour-of-day rather than the person's
    own actual local hour-of-day. Confirmed via this module's own dedicated
    test suite that a known UTC timestamp at a known real-world offset
    produces the correct local hour — the single most important test this
    utility has, since every consumer's correctness depends entirely on this
    one conversion being right.

    Raises ValueError if `timestamp` doesn't match TIMESTAMP_FORMAT — this is
    deliberately NOT swallowed the way an invalid timezone name is (see
    _resolve_zone()): a malformed timestamp string is a real bug in whatever
    wrote it, not a plausible deployment misconfiguration, and should fail
    loudly at the call site rather than silently producing a wrong bucket
    under a caught exception. Callers iterating over many real rows from a
    table this project already writes (query_log, snapshots, etc.) should
    not normally hit this — every stored row was written via TIMESTAMP_FORMAT
    in the first place — but a caller reading attacker-controlled or
    otherwise unverified input should catch this explicitly at its own call
    site, not rely on this function to do it silently.
    """
    naive_utc = datetime.strptime(timestamp, TIMESTAMP_FORMAT)
    aware_utc = naive_utc.replace(tzinfo=timezone.utc)
    return aware_utc.astimezone(_resolve_zone())


def local_hour_bucket(timestamp: str, bucket_minutes: int = 30) -> int:
    """Convert a stored UTC timestamp string into a local-time bucket index
    for the day, where bucket 0 is local midnight and each bucket spans
    `bucket_minutes` minutes (default 30, matching the granularity
    Predictive Pre-Fetching's own design doc proposed for its own "5 minutes
    before the expected time" framing without over-fitting to single-minute
    noise).

    Returns an int in [0, ceil(1440 / bucket_minutes) - 1]. Found via a
    deliberate function-by-function read: an earlier version of this
    docstring claimed the upper bound was `(1440 // bucket_minutes) - 1`,
    which is only correct when bucket_minutes evenly divides 1440 (the
    documented default of 30 does, which is why this went unnoticed) — for
    any value that doesn't (7, 13, 17, 25, 50, 100, and most arbitrary
    values), the real last-minute-of-day (23:59) lands in a bucket index
    exactly one past that claimed maximum, confirmed directly across a
    range of non-divisor values. The function's own arithmetic was never
    wrong — `minutes_since_midnight // bucket_minutes` always returns a
    real, correct bucket index for any input — only the documented range
    claim was. No current caller passes a non-default bucket_minutes (this
    module has no consumers yet — see the module docstring), so this had
    zero live impact, but a future caller sizing a fixed-length array or
    list from the old formula would have silently allocated one bucket too
    few. Deliberately returns a plain bucket index rather than an (hour,
    minute) pair — every real consumer of this (pattern mining, clustering)
    wants a single, directly-comparable/groupable key, not a tuple it
    would just immediately flatten back down anyway.
    """
    local_dt = utc_string_to_local(timestamp)
    minutes_since_midnight = local_dt.hour * 60 + local_dt.minute
    return minutes_since_midnight // bucket_minutes


def local_day_of_week(timestamp: str) -> int:
    """Convert a stored UTC timestamp string into a local-time day-of-week
    index (Monday=0 ... Sunday=6, matching Python's own datetime.weekday()
    convention directly, rather than inventing a different numbering this
    project would then need to remember and document separately).
    """
    return utc_string_to_local(timestamp).weekday()


# ---------------------------------------------------------------------------
# Shared natural-language window resolution — the one owner (Design Doc 5)
# ---------------------------------------------------------------------------
#
# This is the payoff this module's own docstring named as the reason it
# exists: "shared groundwork for time-of-day-aware features". Both the
# `changes` source (app/router.py's _resolve_changes_hours(), which
# resolves "this morning" / "while at work" / "yesterday" into a
# hours-since window) and the new `history` source need to turn the same
# family of natural-language phrases into a time window. Before this,
# _resolve_changes_hours() was the only owner and returned a bare float
# (hours-since-now) — enough for `changes`, which only ever looks backward
# from now, but not for `history`, which needs genuinely BOUNDED windows
# ("last night" is 18:00 yesterday → this morning, not "the last N hours").
#
# resolve_window() is that single owner. It returns a real, bounded
# (start, end) pair in UTC plus a human label, and carries the exact
# hours-since float on the same object so _resolve_changes_hours() can
# keep returning byte-identical values for every phrase it already
# supported — the extraction is behavior-preserving by construction (the
# canonical float is stored, never re-derived from the timedelta, so no
# microsecond re-rounding can drift it). New bounded phrases ("last
# night", "yesterday morning/evening", "last N days", "over the weekend",
# weekday names) were then added ON TOP of that preserved core; they
# enrich `history` and, where they co-occur with a `changes` trigger,
# `changes` too, without disturbing any phrase `changes` already resolved.
#
# Deliberately NOT reworked here: bare "this week"/"week" stays 168h
# rolling rather than snapping to the local Monday. Design Doc 5 §5
# floated local-Monday semantics for "this week", but `changes` routes on
# a substring trigger ("what changed") and then resolves the WHOLE query,
# so silently changing what "this week" means would change the shipped,
# regression-pinned behavior of "what changed this week" (168h). Keeping
# it rolling protects that pin; a future local-Monday "this week" for
# history would need its own opt-in path, not a change to this shared
# owner. Recorded here so the next person doesn't re-litigate it.

# The exact "explicit hour count" regex _resolve_changes_hours() used —
# lifted verbatim so the extraction can't drift it. Requires a real
# window phrase (last/past/in) immediately before the number rather than
# matching any number near the word "hour" (the "3 hour delay flight" /
# "24 hour clock display" false positives that pass had already ruled
# out; see test_router.py's TestResolveChangesHours regression cases).
_EXPLICIT_HOURS_RE = re.compile(
    r"(?:last|past|in the last|in the past|within the last)\s*(\d+)\s*hour"
)
# The "last/past N days" analogue — a genuinely new bounded phrase
# `changes` never resolved (it has no "day" branch), so adding it here
# can't regress any pinned `changes` value. "day" is a distinct word from
# "hour", and the explicit-hours check runs first, so "in the last 3
# hours" is never misread as a day count.
_EXPLICIT_DAYS_RE = re.compile(
    r"(?:last|past|in the last|in the past|within the last)\s*(\d+)\s*day"
)

# Weekday names → Python weekday() index (Monday=0). Full names match on
# a bare word boundary; abbreviations match ONLY when prefixed by a real
# window word (since/on/last/this) — an audit fix, because "sat" and
# "sun" are ordinary English words and bare matching made "is the sun out
# today" resolve to "since Sunday", hijacking the query's own "today".
_WEEKDAYS_FULL = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_WEEKDAYS_ABBREV = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}


class WindowResolution(NamedTuple):
    """A resolved natural-language time window.

    start / end are timezone-aware UTC datetimes — the project's storage
    convention, so a consumer querying a UTC-ISO `ts` column can format
    these directly. label is the human phrase to echo back ("past 24
    hours", "last night"). hours is the hours-since-`start` float, carried
    so `changes` (which only ever looks backward from now) gets a value
    byte-identical to the pre-extraction _resolve_changes_hours() for
    every phrase it already handled — it is the CANONICAL value used to
    build a rolling window's start, never re-derived from (end - start),
    precisely so floating-point re-rounding can't drift it.
    """
    start: datetime
    end: datetime
    label: str
    hours: float


def _clamp_hours(hours: float) -> float:
    """Avoid zero/negative windows — the exact guard _hours_since() applied."""
    return max(hours, 0.1)


def resolve_window(query: str, now: datetime | None = None) -> WindowResolution:
    """Resolve a natural-language time-window phrase into a bounded
    (start_utc, end_utc, label) window, plus the hours-since-start float.

    Phrases are checked most-specific-first so a compound phrase ("last
    night", "yesterday morning") is never shadowed by a broader one
    ("yesterday") that happens to be a substring of it — the same
    ordering discipline _resolve_changes_hours() already used.

    `now` is injectable purely so tests can pin a fixed local clock across
    DST boundaries (the test_timeutil.py house style); production callers
    pass nothing and get datetime.now(UTC). Local-time reasoning ("last
    night", "in the evenings") goes through _resolve_zone() /
    LOCAL_TIMEZONE exactly like every other time-of-day feature; storage
    stays UTC.
    """
    zone = _resolve_zone()
    if now is None:
        now_utc = datetime.now(timezone.utc)
    else:
        # Accept either an aware datetime (used as-is) or a naive one
        # (interpreted as UTC — the project's storage convention), so a
        # test can pass a plain datetime without ceremony.
        now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    now_local = now_utc.astimezone(zone)
    q = query.lower()

    def rolling(hours: float, label: str) -> WindowResolution:
        """A window that ends at now and looks back `hours` — the shape
        every phrase `changes` already supported produces. `hours` is the
        canonical value; start is derived from it, so .hours round-trips
        exactly."""
        h = _clamp_hours(hours)
        return WindowResolution(now_utc - timedelta(hours=h), now_utc, label, h)

    def bounded(start_local: datetime, end_local: datetime, label: str) -> WindowResolution:
        """A genuinely bounded window (end may be well before now). hours
        is (now - start) so a `changes`-style caller that co-matched this
        phrase still gets a sensible 'since the window opened' lookback
        rather than a window that mysteriously ends in the past."""
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)
        hours = _clamp_hours((now_utc - start_utc).total_seconds() / 3600)
        return WindowResolution(start_utc, end_utc, label, hours)

    def local_hour_today(hour: int) -> datetime:
        """Local wall-clock `hour` today, as an aware local datetime."""
        return now_local.replace(hour=hour % 24, minute=0, second=0, microsecond=0)

    def hours_since_local_hour(hour: int) -> float:
        """The exact _hours_since() computation, now DST-correct by working
        in aware local time: elapsed hours since `hour` o'clock today, or
        yesterday's occurrence if that hour hasn't happened yet today."""
        target = local_hour_today(hour)
        if target > now_local:
            target -= timedelta(days=1)
        return _clamp_hours((now_local - target).total_seconds() / 3600)

    # 1. Explicit "last/past N hours" — highest priority, wins over any
    #    calendar word also present ("today in the last 2 hours" -> 2h).
    if "hour" in q:
        m = _EXPLICIT_HOURS_RE.search(q)
        if m:
            n = float(m.group(1))
            return rolling(n, f"past {m.group(1)} hours")

    # 2. Explicit "last/past N days" (new bounded-ish rolling phrase).
    if "day" in q:
        m = _EXPLICIT_DAYS_RE.search(q)
        if m:
            n = int(m.group(1))
            return rolling(n * 24.0, f"past {n} day{'s' if n != 1 else ''}")

    # 3. "last night" — the flagship new BOUNDED window: previous local
    #    evening (18:00) through this local morning (morning_start_hour).
    #    Distinct from "tonight"/"this evening" (which look back to 18:00
    #    today). Checked before "yesterday"/"night" so it isn't shadowed.
    if "last night" in q:
        morning = local_hour_today(settings.morning_start_hour)
        evening = local_hour_today(18) - timedelta(days=1)
        return bounded(evening, morning, "last night")

    # 4. "yesterday morning/afternoon/evening" — bounded slices of the
    #    prior local day. Checked before bare "yesterday" (48h) so the
    #    more specific phrase wins; bare "yesterday" is unaffected.
    y_start = local_hour_today(0) - timedelta(days=1)
    if "yesterday morning" in q:
        return bounded(y_start.replace(hour=settings.morning_start_hour), y_start.replace(hour=12), "yesterday morning")
    if "yesterday afternoon" in q:
        return bounded(y_start.replace(hour=12), y_start.replace(hour=18), "yesterday afternoon")
    if "yesterday evening" in q or "yesterday night" in q:
        return bounded(y_start.replace(hour=18), y_start + timedelta(days=1), "yesterday evening")

    # 5. Time-of-day phrases resolved against configured start hours —
    #    unchanged from _resolve_changes_hours(), now DST-correct.
    if "this morning" in q or "since morning" in q or "since this morning" in q:
        return rolling(hours_since_local_hour(settings.morning_start_hour), "since this morning")
    if "at work" in q or "since work" in q or "while at work" in q or "while i was at work" in q or "while i've been at work" in q:
        return rolling(hours_since_local_hour(settings.work_start_hour), "since work")
    if "tonight" in q or "this evening" in q:
        return rolling(hours_since_local_hour(18), "this evening")
    # "this afternoon" anchors at noon, not 18:00 (audit fix: the first
    # cut lumped it in with the evening branch, so a 1 PM "this afternoon"
    # question looked back to YESTERDAY 18:00 under a mislabeled window).
    # Not a `changes` byte-identity concern: the pre-3.56.0 resolver had
    # no afternoon branch at all, so no pinned value existed for it.
    if "this afternoon" in q:
        return rolling(hours_since_local_hour(12), "this afternoon")

    # 6. "over the weekend" — bounded window covering the most recent
    #    Saturday 00:00 through Sunday 24:00 (local). New phrase; no
    #    `changes` value depended on "weekend".
    if "weekend" in q:
        # weekday(): Mon=0 .. Sun=6. Most recent Saturday at-or-before now.
        days_since_sat = (now_local.weekday() - 5) % 7
        sat = (now_local - timedelta(days=days_since_sat)).replace(hour=0, minute=0, second=0, microsecond=0)
        return bounded(sat, sat + timedelta(days=2), "over the weekend")

    # 7. "since {weekday}" / a weekday name — bounded from the most recent
    #    occurrence of that weekday (00:00 local) through now. Full names
    #    match bare; abbreviations need a since/on/last/this prefix (see
    #    _WEEKDAYS_ABBREV's comment). "last {weekday}" when that weekday is
    #    today means a week ago, not this morning — "last friday" asked on
    #    a Friday is unambiguous in English (audit fix); bare/"since"
    #    forms keep meaning today's own 00:00.
    for name, idx in _WEEKDAYS_FULL.items():
        m = re.search(r"(?:\b(last|since|on|this)\s+)?\b" + name + r"\b", q)
        if m:
            days_back = (now_local.weekday() - idx) % 7
            if days_back == 0 and m.group(1) == "last":
                days_back = 7
            day_start = (now_local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
            return bounded(day_start, now_local, f"since {name.capitalize()}")
    for name, idx in _WEEKDAYS_ABBREV.items():
        m = re.search(r"\b(last|since|on|this)\s+" + name + r"\b", q)
        if m:
            days_back = (now_local.weekday() - idx) % 7
            if days_back == 0 and m.group(1) == "last":
                days_back = 7
            day_start = (now_local - timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
            return bounded(day_start, now_local, f"since {name.capitalize()}")

    # 8. Broad calendar windows — byte-identical to _resolve_changes_hours().
    if "yesterday" in q or "since yesterday" in q:
        return rolling(48.0, "past 48 hours")
    if "week" in q:
        return rolling(168.0, "past 7 days")
    if "today" in q:
        return rolling(24.0, "today")

    # 9. Default — no specific window detected.
    return rolling(24.0, "past 24 hours")
