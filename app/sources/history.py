"""
Mnemolis History Source — routable adapter (Design Doc 5 §5).

The thin `search(query) -> str` layer over app/history.py's engine, the
snapshots.py + _search_changes precedent exactly. Three deterministic
resolutions, each with an honest failure message (constraint #4): a Metric
(which sensor), a Window (timeutil.resolve_window — the shared owner), and
an Aggregation (min/max/avg/trend/summary, or the events leg for counts).
No LLM anywhere in this path: numbers come from SQL over real samples, or
the answer honestly says it can't identify what was asked.
"""
import logging

from app.config import settings
from app.timeutil import resolve_window, utc_string_to_local, TIMESTAMP_FORMAT
from app import history

_LOGGER = logging.getLogger(__name__)

# The metric-word vocabulary and its detector live in app/history.py (the
# engine's query_is_event() needs them for the metric-class guard, and a
# module-level import in that direction would be a cycle). Re-exported
# here because this module is their natural home from a reader's
# perspective and the tests exercise them via this name.
from app.history import _CLASS_WORDS, _detect_class  # noqa: F401,E402

# Aggregation keyword table — closed, deterministic. Order within a bucket
# doesn't matter (any hit selects the bucket); bucket priority is set by
# the checking order in _resolve_aggregation().
_AGG_MIN = ("lowest", "how cold", "coldest", "minimum", "min ", "driest")
_AGG_MAX = ("highest", "how hot", "hottest", "peak", "maximum", "warmest", "most")
_AGG_AVG = ("average", "typical", "usually", "mean", "on average")
_AGG_TREND = ("rising", "falling", "trend", "trending", "going up", "going down",
              "been rising", "been falling", "increasing", "decreasing",
              "climbing", "dropping", "gone up", "gone down")


def _resolve_aggregation(query: str) -> str:
    """Return one of min/max/avg/trend/summary. Trend is checked before the
    extremes so 'has the temperature been rising to a peak' reads as a trend
    question; unresolved defaults to the full summary (min/max/avg/latest)."""
    q = query.lower()
    if any(k in q for k in _AGG_TREND):
        return "trend"
    if any(k in q for k in _AGG_MIN):
        return "min"
    if any(k in q for k in _AGG_MAX):
        return "max"
    if any(k in q for k in _AGG_AVG):
        return "avg"
    return "summary"


class _MetricResolution:
    """metric: the single resolved entry; candidates: entries to offer when
    ambiguous; neither set means fully unresolved."""
    __slots__ = ("metric", "candidates")

    def __init__(self, metric=None, candidates=None):
        self.metric = metric
        self.candidates = candidates or []


def _resolve_metric(query: str, catalog: list) -> _MetricResolution:
    """Deterministic metric resolution (§5), in priority order:
      1. friendly-name phrase match, longest-first (the _detect_area sort
         discipline);
      2. area + device-class combo ("office CO2");
      3. bare device-class word (one candidate -> use it; several -> ask);
      4. area alone (one candidate -> use it; several -> ask).
    """
    from app.sources.home_assistant import _detect_area

    q = query.lower()

    # 1. Friendly-name phrase match, longest friendly_name first. Matched
    #    on word boundaries (the _detect_area \b discipline) rather than
    #    raw substring, so a short friendly name like "AC" or "TV" can't
    #    false-match inside an unrelated word ("A" inside "was").
    import re
    for entry in sorted(catalog, key=lambda c: len(c.friendly_name), reverse=True):
        fn = (entry.friendly_name or "").lower()
        if fn and re.search(r"\b" + re.escape(fn) + r"\b", q):
            return _MetricResolution(metric=entry)

    area = _detect_area(query)
    cls = _detect_class(query)

    # 2. area + class combo.
    if area and cls:
        combo = [c for c in catalog if c.area_id == area and c.device_class == cls]
        if len(combo) == 1:
            return _MetricResolution(metric=combo[0])
        if len(combo) > 1:
            return _MetricResolution(candidates=combo)

    # 3. bare device-class word.
    if cls:
        by_class = [c for c in catalog if c.device_class == cls]
        if len(by_class) == 1:
            return _MetricResolution(metric=by_class[0])
        if len(by_class) > 1:
            return _MetricResolution(candidates=by_class)

    # 4. area alone.
    if area:
        by_area = [c for c in catalog if c.area_id == area]
        if len(by_area) == 1:
            return _MetricResolution(metric=by_area[0])
        if len(by_area) > 1:
            return _MetricResolution(candidates=by_area)

    return _MetricResolution()


def _local_clock(ts: str) -> str:
    """UTC ISO -> local '3:40 AM', leading zero stripped."""
    try:
        dt = utc_string_to_local(ts)
    except Exception:
        return ts
    return dt.strftime("%I:%M %p").lstrip("0")


def _local_when(ts: str, span_hours: float) -> str:
    """Span-aware timestamp: a bare clock time is unambiguous inside a
    single day, but "most recently 9:20 PM" over a 7-day window doesn't
    say WHICH day (audit finding) — so past ~26h the local date rides
    along: 'Jul 3, 9:20 PM'."""
    if span_hours <= 26:
        return _local_clock(ts)
    try:
        dt = utc_string_to_local(ts)
    except Exception:
        return ts
    return f"{dt.strftime('%b')} {dt.day}, " + dt.strftime("%I:%M %p").lstrip("0")


def _span_hours(start_utc: str, end_utc: str) -> float:
    from datetime import datetime, timezone
    try:
        s0 = datetime.strptime(start_utc, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        s1 = datetime.strptime(end_utc, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        return (s1 - s0).total_seconds() / 3600
    except Exception:
        return 0.0


def _humanize_hours(hours: float) -> str:
    if hours < 1:
        # A brand-new metric's coverage can be minutes old; "0 hours"
        # reads like an error (audit finding from the smoke run).
        return "under an hour"
    if hours < 48:
        h = int(round(hours))
        return f"{h} hour{'s' if h != 1 else ''}"
    d = int(round(hours / 24))
    return f"{d} day{'s' if d != 1 else ''}"


def _entity_hint(query: str) -> str | None:
    """A narrowing keyword for the events leg — the first token of any
    detected area (so "front door" narrows door events to the front
    sensor). None for service-level questions (uptime events carry no
    room)."""
    from app.sources.home_assistant import _detect_area
    area = _detect_area(query)
    if not area:
        return None
    return area.split("_")[0]


def _catalog_listing(catalog: list, limit: int = 25) -> str:
    """The honest 'recorded metrics: …' listing (constraint #4)."""
    names = [c.friendly_name for c in catalog[:limit]]
    more = "" if len(catalog) <= limit else f", and {len(catalog) - limit} more"
    return ", ".join(names) + more


def _emit_event(metric_key: str, window_label: str, aggregation: str, samples: int) -> None:
    """Record a history_query explanation-chain event, if a route_query()
    call is in progress. Lazy import so this module and router.py don't
    form an import cycle (router imports the sources at module load); by
    the time search() actually runs, router is fully loaded. A no-op for
    every caller not on the /search explain path — same contract as every
    other _route_event() point."""
    try:
        from app.router import _route_event
        _route_event("history_query", metric_key=metric_key,
                     window_label=window_label, aggregation=aggregation,
                     samples=samples)
    except Exception:  # pragma: no cover - never let telemetry break a result
        pass


def _answer_events(query: str, start_utc: str, end_utc: str, window_label: str) -> str:
    """Events leg — counts/last-occurrence over temporal_events."""
    if not settings.temporal_pattern_detection_enabled:
        _emit_event("events", window_label, "count", 0)
        return ("Event history requires temporal pattern detection to be enabled "
                "(TEMPORAL_PATTERN_DETECTION_ENABLED=true).")

    result = history.count_events(query, start_utc, end_utc, entity_hint=_entity_hint(query))
    _emit_event("events", window_label, "count", result.count)
    span = _span_hours(start_utc, end_utc)

    if not result.matched:
        return ("I couldn't identify which kind of event you mean. I can count "
                "openings, closings, motion, locks/unlocks, low-battery alerts, "
                "and service outages/recoveries over a time window.")

    noun = result.noun if result.count == 1 else result.noun + "s"
    phrase = _window_phrase(window_label)
    if result.count == 0:
        return f"No {result.noun}s recorded {phrase}."
    when = f", most recently {_local_when(result.last_ts, span)}." if result.last_ts else "."
    return f"{result.count} {noun} {phrase}{when}"


def _window_phrase(label: str) -> str:
    """Read a window label naturally in a sentence. 'past 24 hours' /
    'past 7 days' want a leading 'in the'; bounded/adverbial labels
    ('today', 'last night', 'over the weekend', 'since Monday') already
    read correctly bare."""
    return f"in the {label}" if label.startswith("past ") else label


def _answer_metric(query: str, start_utc: str, end_utc: str, window_label: str) -> str:
    """Metric leg — summary/min/max/avg/trend over metric_samples."""
    catalog = history.get_catalog()
    if not catalog:
        _emit_event("", window_label, "summary", 0)
        return ("No recorded metrics yet — the history sampler hasn't captured "
                "any sensor data. It records what it observes from the moment "
                "the feature is enabled; give it a few sampling intervals.")

    resolution = _resolve_metric(query, catalog)
    if resolution.metric is None:
        if resolution.candidates:
            names = ", ".join(c.friendly_name for c in resolution.candidates)
            _emit_event("", window_label, "ambiguous", 0)
            return (f"Several recorded sensors match that — which did you mean? {names}.")
        _emit_event("", window_label, "summary", 0)
        return ("I couldn't identify which sensor or metric you mean. Recorded "
                f"metrics: {_catalog_listing(catalog)}.")

    entry = resolution.metric
    samples = history.fetch_samples(entry.metric_key, start_utc, end_utc)
    aggregation = _resolve_aggregation(query)
    _emit_event(entry.metric_key, window_label, aggregation, len(samples))

    if not samples:
        oldest = history.oldest_sample_ts(entry.metric_key)
        if oldest:
            return (f"No {entry.friendly_name} readings recorded in the {window_label}. "
                    f"Recorded data for it starts {_local_clock(oldest)} "
                    f"({utc_string_to_local(oldest).strftime('%b %d')}).")
        return f"No {entry.friendly_name} readings recorded yet."

    # Coverage disclosure (constraint #2): if the requested window reaches
    # further back than the oldest recorded sample, say the actual span.
    coverage_label = window_label
    oldest = history.oldest_sample_ts(entry.metric_key)
    if oldest and oldest > start_utc:
        from datetime import datetime, timezone
        end_dt = datetime.strptime(end_utc, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        old_dt = datetime.strptime(oldest, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
        covered = (end_dt - old_dt).total_seconds() / 3600
        coverage_label = f"{window_label} (only the past {_humanize_hours(covered)} of recorded data)"

    summary = history.aggregate_series(samples)
    unit = entry.unit
    n = summary.count
    header = f"**{entry.friendly_name} — {coverage_label}** ({n} sample{'s' if n != 1 else ''})"
    span = _span_hours(start_utc, end_utc)
    line = (f"Low {history.format_value(summary.minimum, unit)} ({_local_when(summary.min_ts, span)}) · "
            f"High {history.format_value(summary.maximum, unit)} ({_local_when(summary.max_ts, span)}) · "
            f"Average {history.format_value(summary.average, unit)} · "
            f"Now {history.format_value(summary.latest, unit)}")
    lines = [header, line]

    trend = history.compute_trend(samples, settings.history_trend_min_samples,
                                  settings.history_trend_min_delta)
    trend_line = _trend_line(trend, aggregation)
    if trend_line:
        lines.append(trend_line)
    return "\n".join(lines)


def _trend_line(trend, aggregation: str) -> str | None:
    """The trend sentence. Always shown for an explicit trend question;
    for a plain summary, shown only when there's a real, non-flat trend to
    report (a flat/insufficient result on a summary isn't worth a line)."""
    if trend.status == "insufficient":
        if aggregation == "trend":
            return (f"Not enough recorded samples yet to call a trend "
                    f"(need ≥{settings.history_trend_min_samples}, have {trend.samples}).")
        return None
    if trend.status == "flat":
        return "Trend: roughly flat over this window." if aggregation == "trend" else None
    # rising/falling: shown on explicit trend questions AND plain
    # summaries — a clearly-directional trend is worth a line either way.
    sign = "+" if trend.pct_per_day >= 0 else "-"
    return (f"Trend: {trend.status} ≈ {sign}{abs(trend.pct_per_day):.0f}%/day over this window.")


def search(query: str) -> str:
    """Route a history query: resolve window, decide event-vs-metric leg,
    answer deterministically. Gated on HISTORY_ENABLED (defensive — the
    source isn't registered when disabled, but a direct call shouldn't
    fabricate an answer either)."""
    if not settings.history_enabled:
        return "The history source is disabled (HISTORY_ENABLED=false)."

    window = resolve_window(query)
    start_utc = window.start.strftime(TIMESTAMP_FORMAT)
    end_utc = window.end.strftime(TIMESTAMP_FORMAT)

    if history.query_is_event(query):
        return _answer_events(query, start_utc, end_utc, window.label)
    return _answer_metric(query, start_utc, end_utc, window.label)
