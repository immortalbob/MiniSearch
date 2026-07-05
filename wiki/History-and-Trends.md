# History & Trends

The `history` source is time-series memory for the house. Where [`changes`](Snapshot-Engine-and-Changes) answers *"what's different since X"* by diffing snapshots, `history` answers *"what were the actual values over X"* — highs, lows, averages, counts, and trends over the numeric sensors and service state the house has been reporting all along.

It shipped in **v3.56.0** (Design Doc 5), off by default. Turn it on with `HISTORY_ENABLED=true`.

```
Q: has the office CO2 been rising today?

**Office CO2 — today** (289 samples)
Low 512 ppm (3:40 AM) · High 1,104 ppm (2:15 PM) · Average 731 ppm · Now 688 ppm
Trend: rising ≈ +6%/day over this window.
```

---

## Why this exists, and why it's cheap

The [Snapshot Engine](Snapshot-Engine-and-Changes) already polls Home Assistant every few minutes and formats the result as text sized for *diffing* — then throws the underlying numbers away. Every one of those polls carried a live office-CO2 reading, a living-room temperature, a battery percentage; the diff only cared whether they *changed enough to mention*, so the actual values were discarded the moment the diff was computed.

`history` keeps them — and it has **no fetch of its own to make**. Ingestion happens *inside* the snapshot jobs: `snapshot_ha()` hands the very states list it just retrieved to `history.ingest_ha_states()`, and `snapshot_uptime()` hands its just-fetched status text to `history.ingest_uptime_text()`. There is no separate sampler job, no second `_get_states()` call, no independent poll — the design constraint (Design Doc 5, constraint #1) is that turning `history` on must never degrade the sources it reads from, and the way that's guaranteed is structural: history consumes payloads other jobs already paid for. Its cadence is therefore theirs: HA metrics arrive every 5 minutes (the HA snapshot interval), uptime counts every 2 (the uptime snapshot interval). There is deliberately no interval setting, because a knob couldn't change when data actually arrives.

Worth being candid about, since this page documents a real project: the *first cut* of this feature (earlier in the same v3.56.0 development cycle) violated exactly this constraint — it gave history its own scheduler job that re-fetched `_get_states()` (a duplicate of what `snapshot_ha` had just retrieved), rendered the HA area-map template **every tick**, and performed a live Uptime Kuma client login every 5 minutes. The post-implementation audit caught all three and replaced them with the ingest hooks described above — the same duplicate-fetch pattern the full-audit pass once removed from `snapshot_ha` itself, caught a second time by re-reading the constraint instead of the code's own comments. The one residual HA call history makes — the area-map template render that maps `sensor.office_co2` → `office` — is cached and refreshed at most once every 12 ingests (~hourly), because area assignments change on the timescale of furniture, not minutes; on a refresh failure the stale map is kept rather than losing area resolution for an hour.

The one derived pair that isn't a raw HA sensor is service uptime: the ingest parses the same text `snapshot_uptime` just stored (`"All N monitored services are up."` / `"N of M services are up."`) into `uptime.services_up` / `uptime.services_total`, so *"how many services were down this week"* has real data behind it — at the uptime job's 2-minute resolution. Forecast is deliberately **not** sampled — Open-Meteo is a prediction, not an observation, and recording predictions as if they were history would be exactly the kind of quiet dishonesty the rest of the project works to avoid.

---

## What gets recorded

An entity is sampled on a tick when **all** of these hold:

- its state parses as a **finite** real number (an `unavailable` / `unknown` / blank state is skipped, and so is a sensor stuck on `NaN`/`inf` — which Python's `float()` happily parses, and which would otherwise silently poison every average it touched; a gap is recorded truthfully as absence, never backfilled with a fabricated value);
- it's a `sensor` whose `device_class` is in `HISTORY_DEVICE_CLASSES`, **or** its entity ID is explicitly named in `HISTORY_EXTRA_ENTITIES`;
- its entity ID is **not** in `HISTORY_EXCLUDE_ENTITIES`.

The default device-class set is `temperature, humidity, carbon_dioxide, power, energy, illuminance, pressure, battery`. `battery` is in that list on purpose — it's already in the snapshot path for low-battery alerts, so recording it gives *"how fast is the T7S3 battery draining"* for free. The cost is catalog noise from every button-cell device in the house, and the escape hatch for that is `HISTORY_EXCLUDE_ENTITIES`, not dropping a genuinely useful class for everyone.

Two tables, both WAL-mode SQLite at `/app/data/history.db`:

- **`metric_samples`** — `(metric_key, value, unit, ts)`, one row per sensor per tick.
- **`metric_catalog`** — one row per distinct metric: `friendly_name`, `area_id`, `device_class`, `unit`, `first_seen`, `last_seen`. Refreshed every tick so a renamed or re-homed entity in HA is reflected here, with `first_seen` preserved across the update so *"since when have we tracked this"* stays honest.

Events (door openings, motion, locks, service outages) are **not** duplicated into this database. The count/when leg reads the temporal feature's `temporal_events` table directly, read-only — see below.

---

## Retention and on-disk cost

Pruning happens inside the sampler tick, batched (a `DELETE … LIMIT` loop) so a large purge never balloons the WAL in one transaction. Anything older than `HISTORY_RETENTION_DAYS` (default 90) goes, and catalog rows unseen for the same window are dropped too.

Back-of-envelope for sizing: at the HA snapshot's 5-minute cadence a sensor produces **288 samples/day** (the two derived uptime metrics, riding the 2-minute uptime job, produce 720 each). A house with ~40 numeric entities is therefore ~11,500 rows/day, ~1M rows at 90 days — on the order of **60–80 MB**, comfortable on MiniDock. The cost scales *linearly* with tracked-entity count, so a 400-entity deployment should either lower `HISTORY_RETENTION_DAYS` or lean on `HISTORY_EXCLUDE_ENTITIES` / `HISTORY_DEVICE_CLASSES` to bound the row count rather than being surprised by a multi-gigabyte database.

One honesty note worth stating plainly: **`history` only knows what it recorded while it was turned on.** Enabling the feature does not retroactively populate the past. A query whose window reaches back further than the oldest recorded sample says so explicitly (see coverage disclosure below) rather than quietly answering over a shorter span than you asked for.

---

## How a query is answered

The source adapter (`app/sources/history.py`) does three deterministic resolutions — no LLM anywhere in the path. Numbers come from SQL over real samples, or the answer honestly says it couldn't identify what was asked.

### 1. The window — one shared owner

Both `history` and `changes` turn phrases like *"today," "this week," "last night," "yesterday morning"* into a time window, so that logic lives in exactly one place: `timeutil.resolve_window()`. `changes`' own `_resolve_changes_hours()` is now a thin adapter over it, and its behavior is pinned **byte-identical** for every phrase it resolved before the extraction (regression-tested first, then the shared owner built underneath it — the same single-source-of-truth discipline the discourse-framing and stop-word lists follow).

The extraction also added genuinely new **bounded** windows that `changes` never had — *"last night"* (previous evening 18:00 → this morning), *"yesterday morning/evening,"* *"over the weekend,"* *"since Monday,"* *"the last 3 days"* — which enrich both sources. One phrase from the design doc was deliberately **not** implemented: *"this week"* stays a rolling 168-hour window rather than snapping to the local Monday. `changes` routes on a substring trigger and then resolves the whole query, so redefining *"this week"* would have silently changed the shipped, tested behavior of *"what changed this week."* Protecting that pin won over the nicer calendar semantics; a future local-Monday *"this week"* for `history` would need its own opt-in path, not a change to the shared owner.

### 2. The metric — which sensor

Resolved against the catalog in priority order, most specific first:

1. **friendly-name phrase match** (longest name first, on word boundaries so a short name like `AC` can't false-match inside another word);
2. **area + device-class** (*"office CO2"* → the office's carbon-dioxide sensor);
3. **bare device-class word** — one candidate uses it, several ask which you meant;
4. **area alone** — same one-vs-many handling.

Nothing resolvable → an honest *"I couldn't identify which sensor or metric you mean"* followed by the list of what's actually recorded. It never guesses a sensor.

### 3. The aggregation — what to compute

Keyword-bucketed into `min` (*"how cold did it get"*), `max` (*"how hot," "peak"*), `avg` (*"average," "typical"*), `trend` (*"rising," "falling," "trend"*), or the default full summary. Counts (*"how many times," "how often"*) route to the events leg instead.

Every metric answer leads with the same summary line — **Low / High / Average / Now**, each with the sensor's real unit and the local clock time of the extreme — because that line already contains the answer to any of the min/max/avg questions, and showing all four is more useful than hiding three of them.

---

## Trends: a direction is a claim, and claims have a bar

The trend line is the one place `history` makes an assertion beyond "here are the numbers," so it's gated twice:

- **`HISTORY_TREND_MIN_SAMPLES`** (default 12) — below this many real samples in the window, no direction is asserted at all. An explicit trend question gets *"not enough recorded samples yet to call a trend"*; a plain summary just omits the line. Fitting a slope to three points and calling it "rising" is exactly the false confidence this guard exists to prevent.
- **`HISTORY_TREND_MIN_DELTA`** (default 0.1) — the fitted change across the window must clear 10% of the window's *own observed value range* before it counts as a trend rather than "roughly flat." This is a per-window, unit-free noise floor: it adapts to whatever the sensor's natural spread is, so the same setting behaves sensibly for CO2 in ppm and temperature in degrees without hardcoding a threshold per unit.

The slope itself is a plain least-squares fit in pure Python (the same no-numpy discipline as the temporal miner's hand-rolled statistics), reported as a direction plus an approximate percent-per-day. "Roughly flat" is a finding, not a shrug — a sensor that genuinely isn't moving should say so, not be tortured into a direction.

---

## The events leg — read-only, never a second extractor

Count and when-was-the-last questions about *events* (door openings, motion, locks/unlocks, low-battery alerts, service outages/recoveries) don't come from `metric_samples` — they come from the [temporal feature's](Cross-Source-Temporal-Pattern-Detection) `temporal_events` table, read **directly and read-only** (`?mode=ro`, the same discipline the query log uses).

Resolution honesty rules, all field-tested on the first deployment: a catalog friendly name that *is* a bare class word (a device someone named just "Temperature") never wins the name-match step — the class logic owns that question and asks when several sensors qualify, because the alternative was a plausible number from an unspecified device. A query that names an area with no matching sensor (*"office CO2"* in a house whose only CO2 sensor lives in the living room) is answered from the unambiguous class match **with a leading disclosure** — *"No carbon dioxide sensor recorded in office — showing …"* — and asks instead when several rooms could claim it. And "how cold/hot did it get" resolves to temperature directly, since the intent trigger implies the class. Timestamps inside an answer carry the local date whenever the window crosses local midnight — the rolling "today" window (the pinned 24-hour `changes` semantic) almost always does, so *"most recently 5:25 PM"* would otherwise be silently ambiguous about which day.

Deciding *which leg* a question belongs to is itself guarded: an event verb alone isn't enough when the query also names a numeric metric — *"has the CO2 gone down today"* is a trend question about a recorded metric, not a request to count service outages, even though "gone down" matches the outage vocabulary. A detected metric class routes to the metric leg, with one deliberate exception: "battery" is both a device class and an event family, and *"how many times did we get a low battery alert"* is genuinely asking for the event count.

This is a deliberate architectural line: there is exactly one thing in Mnemolis that extracts events from raw state, and it's the temporal miner. `history` reading that table rather than running its own extractor is what keeps the two from drifting apart — the duplicate-fetch class of bug the audit already removed once from `snapshot_ha` isn't reintroduced here. The practical consequence: **event counts require `TEMPORAL_PATTERN_DETECTION_ENABLED=true`.** With temporal detection off there's simply no event history to read, and the answer says exactly that instead of returning a confident zero.

```
Q: how many times did the front door open today?
   → 7 openings today, most recently 9:20 PM.

Q: how often did the internet go down this week?
   → 2 service outages in the past 7 days, most recently 6:20 PM.
```

---

## Coverage disclosure

The single most important honesty property: when the requested window reaches back further than the oldest recorded sample for a metric, the answer states its **actual** coverage.

```
Q: what was the humidity this week?   (feature enabled 3 days ago)

**Hallway Humidity — this week (only the past 3 days of recorded data)** (864 samples)
Low 38% (…) · High 61% (…) · Average 47% · Now 44%
```

You asked for a week; you're told you're getting three days, because that's all there is. This is constraint #2 of the design doc made visible in every response, rather than a footnote nobody reads.

---

## Endpoints

Two read endpoints, both behind `require_api_key` (unlike `/areas`, these expose what sensor data has actually been recorded and how much — house telemetry worth gating behind the same key `/search` and `/changes` use):

- **`GET /history/metrics`** — the recorded-metric catalog with per-metric sample counts and coverage (oldest/newest sample). The `/areas` analogue for `history`.
- **`GET /history/series?metric=<key>&hours=<N>`** — the raw `(value, ts)` series for one metric over the last N hours, UTC-ISO timestamps. This is the endpoint a future NOC/device-registry dashboard renders sparklines from; it was shipped in v1 deliberately rather than deferred, so that dashboard won't need a follow-up Mnemolis release to get its data.

Both return `{"status": "disabled"}` when `HISTORY_ENABLED` is false rather than an error.

---

## Health

`/health` gains a `history` block (see [Health & Observability](Health-and-Observability)). When the feature is off it reads `{"status": "disabled"}`; when on, it reports `metrics_tracked`, `samples_24h`, oldest/newest sample, `db_mb`, and a `quiet_sensors` count — catalog entries whose `last_seen` has gone stale, so a Mnemovox node that goes silent becomes visible for free. The job is flagged `stale` on the same interval-×-grace-multiplier logic every other background job uses (`HISTORY_STALE_GRACE_MULTIPLIER`, default 3, against the HA snapshot interval — the cadence ingestion actually rides).

---

## Configuration

Full descriptions live in the [Configuration Reference](Configuration-Reference#history-source). In brief:

| Variable | Default | Role |
|----------|---------|------|
| `HISTORY_ENABLED` | `false` | Master switch |
| `HISTORY_RETENTION_DAYS` | `90` | Prune older samples |
| `HISTORY_DEVICE_CLASSES` | *(8 classes)* | Which sensors to keep |
| `HISTORY_EXTRA_ENTITIES` | *(blank)* | Allowlist for unclassified sensors |
| `HISTORY_EXCLUDE_ENTITIES` | *(blank)* | Denylist (e.g. battery noise) |
| `HISTORY_TREND_MIN_SAMPLES` | `12` | Below this, no trend is asserted |
| `HISTORY_TREND_MIN_DELTA` | `0.1` | Noise floor for calling a trend |
| `HISTORY_STALE_GRACE_MULTIPLIER` | `3` | `/health` staleness threshold (in multiples of the HA snapshot interval) |

The database (`/app/data/history.db`) is included in [Backup & Restore](Backup-and-Restore).
