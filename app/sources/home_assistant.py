"""
Mnemolis Home Assistant Source
Queries Home Assistant entity states for analytical and summary queries
that go beyond HA's built-in single-entity intent handling.

Useful for:
- Multi-entity summaries ("house status", "which lights are on")
- Environmental summaries ("indoor air quality", "outdoor conditions")
- Security status ("are the doors locked", "any recent motion")
- Battery status ("which devices need charging")
- Power consumption ("how much power are the lights using")
"""
import logging
import re
import requests
from datetime import datetime, timezone
from app.config import settings

_LOGGER = logging.getLogger(__name__)

# Persistent HTTP session — connection reuse across every Home Assistant
# call, the same shape as app/llm.py's fix and searxng.py's _session
# (see searxng.py's comment for the shared thread-safety rationale:
# never mutated after creation, only ever `.get()`/`.post()`). HA is
# hit by direct queries, fusion, snapshots (snapshot_ha() reuses
# _get_states() and therefore this same session), and the wall
# dashboard's habits — steady enough traffic that per-call TCP setup
# was pure recurring waste.
_session = requests.Session()

# Entity IDs to always exclude — internal HA entities, unavailable sensors, segments
_EXCLUDE_PATTERNS = [
    "light.backyard_light_2",  # duplicate sub-entity
    "light.shed_light_2",      # duplicate sub-entity
    "processor_temperature",
    "livingroomlilygo_esp32_temperature",
    "living_room_voice_va_temperature",
    "master_bedroom_voice_assistant_va_temperature",
    "bedroom_voice_va_temperature",
    "living_room_voice_motion",
    "master_bedroom_voice_assistant_motion",
    "bedroom_voice_motion",
    "maindash",
    "led_ring",
    "_segment_",  # TV backlight segments
    "_ding",       # doorbell ring events
    "va_temperature",
]

# Query keyword → filter spec
_QUERY_MAP: dict[str, dict] = {
    # Lights
    "light": {"domains": ["light"], "state_filter": None},
    "lights": {"domains": ["light"], "state_filter": None},
    "lamp": {"domains": ["light"], "state_filter": None},
    "bulb": {"domains": ["light"], "state_filter": None},
    "on": {"domains": ["light"], "state_filter": "on"},
    "lights on": {"domains": ["light"], "state_filter": "on"},
    "lights off": {"domains": ["light"], "state_filter": "off"},
    # Locks and doors
    "lock": {"domains": ["lock"], "device_classes": ["door"]},
    "locked": {"domains": ["lock"], "device_classes": ["door"]},
    "door": {"domains": ["lock"], "device_classes": ["door"]},
    "secure": {"domains": ["lock"], "device_classes": ["door"]},
    "security status": {"domains": ["lock"], "device_classes": ["door", "motion", "occupancy"], "include_motion": True},
    "security": {"domains": ["lock"], "device_classes": ["door", "motion", "occupancy"]},
    # Garage door — open/closed state, NOT locked/unlocked. A genuinely
    # different question from the lock-domain "door" entries above (a
    # garage door typically has no lock entity of its own), so this is
    # deliberately a separate key rather than widened "door"/"secure"
    # specs — confirmed via direct testing that widening "door" itself
    # would have been the wrong fix anyway, since router.py's own
    # INTENT_MAP keeps "are the doors locked"-style routing genuinely
    # separate from this new "garage door" trigger (see that file's
    # own comment on the same investigation).
    #
    # Covers BOTH real HA naming conventions for this entity, confirmed
    # via Home Assistant's own documentation and a live, filed core
    # issue (home-assistant/core#91131) describing the inconsistency
    # directly: a `cover` entity (the dedicated domain for openings —
    # garage doors, blinds, gates) uses device_class "garage", while a
    # plain `binary_sensor` reporting the same physical door (a common,
    # simpler integration shape, e.g. a basic reed-switch sensor with
    # no remote control capability) uses "garage_door" instead — same
    # real-world thing, two different device_class strings depending
    # on which integration shape was used to expose it. Domain "cover"
    # itself is also included directly (not just via device_class) so
    # a cover entity that for any reason lacks a device_class
    # attribute (the None/generic cover case HA's own docs describe)
    # still matches.
    "garage door": {"domains": ["cover"], "device_classes": ["garage", "garage_door"]},
    "garage": {"domains": ["cover"], "device_classes": ["garage", "garage_door"]},
    # Motion
    # Motion — both "event" domain (newer HA motion integrations that
    # report discrete motion EVENTS, e.g. "motion detected at 3:42pm")
    # AND "binary_sensor" with device_class "motion" (the more common,
    # older-style "is motion currently active" convention used by many
    # real Zigbee2MQTT/Z-Wave/PIR integrations) are genuine, first-class
    # matches. Found via a deliberate "bulletproofing" pass investigating
    # why dedup logic existed for binary_sensor motion entities at all
    # (see motion_event_names in search()) when this dict only ever
    # listed "event" as a possible domain — that dedup logic only makes
    # sense if binary_sensor motion entities were ALWAYS meant to be
    # reachable, confirmed by "security"/"security status" already
    # correctly including device_classes: ["motion"] elsewhere in this
    # same dict, just never applied consistently to these three entries.
    "motion": {"domains": ["event"], "device_classes": ["motion"], "event_keywords": ["motion"]},
    "camera": {"domains": ["event"], "device_classes": ["motion"], "event_keywords": ["motion"]},
    "activity": {"domains": ["event"], "device_classes": ["motion"], "event_keywords": ["motion"]},
    # Battery
    "battery": {"device_classes": ["battery"]},
    "charging": {"device_classes": ["battery"]},
    "low battery": {"device_classes": ["battery"]},
    "battery levels": {"device_classes": ["battery"]},
    "battery status": {"device_classes": ["battery"]},
    # Environmental — indoor only
    "temperature": {"device_classes": ["temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"]},
    "temp": {"device_classes": ["temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"]},
    "humidity": {"device_classes": ["humidity"], "exclude_entity_keywords": ["cotech"]},
    "co2": {"device_classes": ["carbon_dioxide"]},
    "carbon": {"device_classes": ["carbon_dioxide"]},
    "air quality": {"device_classes": ["carbon_dioxide", "humidity", "temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"]},
    "air": {"device_classes": ["carbon_dioxide", "humidity", "temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"]},
    "indoor": {"device_classes": ["carbon_dioxide", "humidity", "temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"]},
    # Outdoor / weather station
    "outdoor conditions": {"entity_keywords": ["cotech"]},
    "outside conditions": {"entity_keywords": ["cotech"]},
    "outdoor": {"entity_keywords": ["cotech"]},
    "outside": {"entity_keywords": ["cotech"]},
    "weather station": {"entity_keywords": ["cotech"]},
    "wind": {"entity_keywords": ["wind"]},
    "rain": {"entity_keywords": ["rain"]},
    # Power
    "power": {"entity_keywords": ["consumption"]},
    "consumption": {"entity_keywords": ["consumption"]},
    "energy": {"device_classes": ["energy", "power"]},
    # House summary
    "status": {"domains": ["light", "lock"], "device_classes": ["door", "battery", "motion"]},
    "summary": {"domains": ["light", "lock"], "device_classes": ["door", "battery", "carbon_dioxide", "humidity", "temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"], "include_motion": True},
    "house": {"domains": ["light", "lock"], "device_classes": ["door", "battery", "carbon_dioxide", "humidity", "temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"], "include_motion": True},
    "home": {"domains": ["light", "lock"], "device_classes": ["door", "battery", "carbon_dioxide", "humidity", "temperature"], "exclude_entity_keywords": ["cotech", "processor", "esp32", "va_temperature"], "include_motion": True},
}


def _get_states() -> list[dict] | None:
    """Fetch all entity states from HA REST API."""
    if not settings.ha_url or not settings.ha_token:
        return None
    try:
        resp = _session.get(
            f"{settings.ha_url}/api/states",
            headers={"Authorization": f"Bearer {settings.ha_token}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        _LOGGER.error("Failed to fetch HA states: %s", e)
        return None


def _get_area_entities() -> dict[str, list[str]] | None:
    """Fetch area → entity list mapping from HA template API.
    Returns dict of {area_id: [entity_id, ...]} or None on failure.
    """
    if not settings.ha_url or not settings.ha_token:
        return None
    try:
        template = "{% for area in areas() %}{{ area }}|||{{ area_entities(area) | join(',') }}\n{% endfor %}"
        resp = _session.post(
            f"{settings.ha_url}/api/template",
            headers={
                "Authorization": f"Bearer {settings.ha_token}",
                "Content-Type": "application/json",
            },
            json={"template": template},
            timeout=10,
        )
        resp.raise_for_status()
        area_map = {}
        for line in resp.text.strip().splitlines():
            if "|||" not in line:
                continue
            area_id, entities_str = line.split("|||", 1)
            area_id = area_id.strip()
            entities = [e.strip() for e in entities_str.split(",") if e.strip()]
            if area_id and entities:
                area_map[area_id] = entities
        _LOGGER.debug("Loaded %d areas from HA", len(area_map))
        return area_map
    except Exception as e:
        _LOGGER.warning("Failed to fetch HA area registry: %s", e)
        return None


# Natural language area name aliases
_AREA_ALIASES = {
    "living room": "living_room",
    "master bedroom": "master_bedroom",
    "master bath": "master_bathroom",
    "master bathroom": "master_bathroom",
    "guest bedroom": "guest_bedroom",
    "guest room": "guest_bedroom",
    "dining room": "dining_room",
    "outside": "outside",
    "outdoors": "outside",
    "exterior": "outside",
    "patio": "patio",
    "back yard": "back",
    "backyard": "back",
    "front door": "front",
    "front yard": "front",
    "shed": "shed",
    "kitchen": "kitchen",
    "bathroom": "bathroom",
    "bedroom": "bedroom",
    "hallway": "hallway",
    "utility": "utility_room",
    "laundry": "utility_room",
    "display": "display_cases",
}


def _detect_area(query: str) -> str | None:
    """Return area_id if a room/area name is detected in the query, else None."""
    query_lower = query.lower()
    # Found via the same systematic word-boundary check that found a
    # real bug in _QUERY_MAP: "shed" (a real area alias) matched as a
    # bare substring inside "finished," "crashed," "washed," and other
    # common past-tense verbs — "is the download finished yet" (a query
    # with nothing to do with any area at all) incorrectly resolved to
    # area_id="shed", purely from the accidental substring. The
    # longest-match-first checking order only incidentally protects
    # against this when a genuine, longer area phrase ALSO happens to
    # be present in the same query (which masked the bug in an earlier,
    # less careful test) — it does nothing when "shed" is the only
    # thing that happens to match at all. Fixed with the same \b
    # word-boundary regex approach already applied to _build_filter().
    for phrase in sorted(_AREA_ALIASES.keys(), key=len, reverse=True):
        if re.search(r"\b" + re.escape(phrase) + r"\b", query_lower):
            return _AREA_ALIASES[phrase]
    return None


def list_areas() -> dict:
    """
    Return all HA areas with entity counts and the natural-language phrases
    that resolve to each one. Used by the GET /areas endpoint.

    Returns:
        {"status": "ok", "areas": {area_id: {"entity_count": N, "aliases": [...]}}}
        or {"status": "error"/"not_configured", "areas": {}}
    """
    if not settings.ha_url or not settings.ha_token:
        return {"status": "not_configured", "areas": {}}

    area_map = _get_area_entities()
    if area_map is None:
        return {"status": "error", "areas": {}}

    # Build reverse lookup: area_id -> [aliases that map to it]
    aliases_by_area: dict[str, list[str]] = {}
    for phrase, area_id in _AREA_ALIASES.items():
        aliases_by_area.setdefault(area_id, []).append(phrase)

    areas = {}
    for area_id, entities in sorted(area_map.items()):
        areas[area_id] = {
            "entity_count": len(entities),
            "aliases": sorted(aliases_by_area.get(area_id, [])),
        }

    return {"status": "ok", "areas": areas}


def _friendly_name(entity: dict) -> str:
    return entity.get("attributes", {}).get("friendly_name", entity["entity_id"])


def _unit(entity: dict) -> str:
    return entity.get("attributes", {}).get("unit_of_measurement", "")


def _format_value(state: str, unit: str) -> str:
    """Round numeric values for cleaner display."""
    try:
        val = float(state)
        if unit in ("°F", "°C"):
            return f"{round(val, 1)} {unit}"
        if unit in ("%", "ppm", "mph", "in"):
            return f"{round(val, 1)} {unit}"
        if unit == "W":
            return f"{round(val, 1)} {unit}"
        return f"{val} {unit}"
    except ValueError:
        return f"{state} {unit}".strip()


def _format_entity(entity: dict) -> str:
    name = _friendly_name(entity)
    state = entity["state"]
    unit = _unit(entity)
    if unit:
        return f"{name}: {_format_value(state, unit)}"
    return f"{name}: {state}"


def _format_motion_event(entity: dict) -> str:
    """Format a motion event with how long ago it occurred."""
    name = _friendly_name(entity)
    state = entity["state"]
    try:
        dt = datetime.fromisoformat(state.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            age = "just now"
        elif minutes < 60:
            # Found via a deliberate "bulletproofing" pass: hours and
            # days both already correctly handle the singular/plural
            # distinction ("1 hour ago" vs "2 hours ago"), but this
            # exact same pattern was overlooked for minutes, producing
            # "1 minutes ago" — a small, real, cosmetic grammar
            # inconsistency now fixed to match its own established
            # pattern.
            age = f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        elif minutes < 1440:
            hours = minutes // 60
            age = f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            days = minutes // 1440
            age = f"{days} day{'s' if days != 1 else ''} ago"
        return f"{name}: {age}"
    except Exception:
        return f"{name}: {state}"


def _is_excluded(entity: dict) -> bool:
    """Return True if this entity should always be excluded."""
    entity_id = entity["entity_id"]
    state = entity["state"]
    if state in ("unavailable", "unknown"):
        return True
    if any(p in entity_id for p in _EXCLUDE_PATTERNS):
        return True
    return False


def _build_filter(query: str) -> dict:
    """Build a merged filter spec from all matching query keywords."""
    query_lower = query.lower()
    domains = set()
    device_classes = set()
    entity_keywords = set()
    event_keywords = set()
    exclude_entity_keywords = set()
    state_filter = None
    include_motion = False

    # Match longest phrases first to avoid partial matches
    matched_any = False
    consumed_positions: set[int] = set()

    for keyword in sorted(_QUERY_MAP.keys(), key=len, reverse=True):
        # Found via a deliberate "bulletproofing" pass, specifically
        # checking _QUERY_MAP for the same kind of keyword-collision
        # risk already checked systematically in router.py's
        # INTENT_MAP: naive substring matching (query_lower.find) has
        # no word-boundary awareness at all. "on" (a real, bare
        # dictionary key for "lights on") matched as a substring of
        # "front" — meaning "is the front door locked," about as
        # natural a query as this entire source exists to answer, got
        # an incorrect state_filter="on" applied, and the actual front
        # door lock entity (whose real state is "locked"/"unlocked",
        # never "on") was silently rejected by the filter. Confirmed
        # end to end: search("is the front door locked") against a
        # real, correctly-named, correctly-stated front door lock
        # entity returned "No matching entities found in Home
        # Assistant for that query." The existing longest-match-first
        # / consumed_positions logic only protects against a SHORTER
        # key matching the SAME character range a LONGER key already
        # claimed (e.g. "door" correctly losing to "outdoor" when both
        # would match the same letters) — it does nothing for a short
        # key matching INSIDE an entirely different, unrelated word
        # elsewhere in the query. Fixed with proper \b word-boundary
        # regex matching instead of bare substring search — verified
        # against "front"/"on", "training"/"rain", "alone"/"on", and
        # every genuine multi-word phrase key, confirming both the bug
        # cases are now correctly rejected and every real, intended
        # match (including multi-word phrases like "lights on") still
        # works correctly.
        pattern = r"\b" + re.escape(keyword) + r"\b"
        m = re.search(pattern, query_lower)
        if not m:
            continue
        pos = m.start()
        # Skip if this keyword position is already covered by a longer match
        positions = set(range(pos, pos + len(keyword)))
        if positions & consumed_positions:
            continue
        consumed_positions |= positions
        spec = _QUERY_MAP[keyword]
        domains.update(spec.get("domains", []))
        device_classes.update(spec.get("device_classes", []))
        entity_keywords.update(spec.get("entity_keywords", []))
        event_keywords.update(spec.get("event_keywords", []))
        exclude_entity_keywords.update(spec.get("exclude_entity_keywords", []))
        if spec.get("state_filter"):
            state_filter = spec["state_filter"]
        if spec.get("include_motion"):
            include_motion = True
        matched_any = True

    # No match — return summary
    if not matched_any:
        return _build_filter("summary")

    return {
        "domains": domains,
        "device_classes": device_classes,
        "entity_keywords": entity_keywords,
        "event_keywords": event_keywords,
        "exclude_entity_keywords": exclude_entity_keywords,
        "state_filter": state_filter,
        "include_motion": include_motion,
    }


def _matches_filter(entity: dict, f: dict) -> bool:
    """Return True if entity matches the filter spec."""
    entity_id = entity["entity_id"]
    domain = entity_id.split(".")[0]
    dc = entity.get("attributes", {}).get("device_class", "")
    state = entity["state"]

    if _is_excluded(entity):
        return False

    # State filter (e.g. only "on" lights)
    if f["state_filter"] and state != f["state_filter"]:
        return False

    # Exclude entity keywords (indoor sensors should exclude cotech etc)
    if f["exclude_entity_keywords"] and any(kw in entity_id for kw in f["exclude_entity_keywords"]):
        return False

    # Event domain — match by entity name keywords
    if domain == "event" and f["event_keywords"]:
        return any(kw in entity_id for kw in f["event_keywords"])

    # Entity keyword match
    if f["entity_keywords"] and any(kw in entity_id for kw in f["entity_keywords"]):
        return True

    # Domain match
    if domain in f["domains"]:
        return True

    # Device class match
    if dc in f["device_classes"]:
        return True

    return False


def search(query: str) -> str:
    """Query Home Assistant entity states for analytical summaries.
    If a room/area name is detected in the query, filters results to that area.
    """
    if not settings.ha_url or not settings.ha_token:
        return "Home Assistant is not configured. Set HA_URL and HA_TOKEN."

    states = _get_states()
    if states is None:
        return "Could not connect to Home Assistant. Check HA_URL and HA_TOKEN."

    if not states:
        return "No entity states returned from Home Assistant."

    # Build a state lookup dict for fast access
    state_by_id = {e["entity_id"]: e for e in states}

    # Check for area/room reference in query
    area_id = _detect_area(query)
    area_entity_ids: set[str] = set()

    if area_id:
        area_map = _get_area_entities()
        if area_map and area_id in area_map:
            area_entity_ids = set(area_map[area_id])
            _LOGGER.info("Area filter: '%s' has %d entities", area_id, len(area_entity_ids))
        else:
            _LOGGER.warning("Area '%s' not found in registry, falling back to keyword filter", area_id)
            area_id = None

    f = _build_filter(query)

    # Deduplicate by entity_id
    seen_ids = set()
    matched = []

    if area_id and area_entity_ids:
        # Area-filtered search — only consider entities in the specified
        # area, but otherwise apply the EXACT SAME filter logic
        # _matches_filter() already uses for the keyword-based path below.
        #
        # Found via a deliberate complexity-reduction investigation (the
        # same side-by-side comparison discipline that found real bugs
        # in app/router.py's route_with_source() this same release
        # cycle): this branch used to reimplement only a SUBSET of
        # _matches_filter()'s real logic — state_filter and a simplified
        # domain/device_class check — silently missing
        # exclude_entity_keywords, strict mode, entity_keywords, and
        # event_keywords entirely. This was genuinely reachable: queries
        # like "indoor air quality" or "house status summary" set real
        # exclude_entity_keywords (filtering out cotech/processor/esp32
        # sensor-node entities that would otherwise pollute the results),
        # and combining either with a real area name ("indoor air
        # quality in the living room") silently skipped that exclusion
        # entirely, since the area branch never checked for it.
        #
        # An earlier version of this fix also special-cased a "filter is
        # completely empty" leniency, on the theory that a bare area name
        # with no other constraint should still return everything in that
        # area, where bare _matches_filter() would return nothing for a
        # genuinely empty filter spec. Confirmed via three independent
        # checks — a full static trace of _build_filter()'s control flow,
        # 2000 Hypothesis-generated random fuzz inputs, and an exhaustive
        # check of every real entry in _QUERY_MAP — that _build_filter()
        # never actually produces an empty filter spec for any real input:
        # every path either matches something real or falls back to
        # _build_filter("summary"), itself a real, non-empty filter. The
        # leniency branch was genuinely unreachable dead code, not cheap
        # defensive insurance, so it was removed rather than kept.
        for entity_id in area_entity_ids:
            if entity_id in state_by_id:
                e = state_by_id[entity_id]
                if entity_id in seen_ids:
                    continue
                if not _matches_filter(e, f):
                    continue
                seen_ids.add(entity_id)
                matched.append(e)
    else:
        # Standard keyword-based filter
        for e in states:
            if e["entity_id"] not in seen_ids and _matches_filter(e, f):
                seen_ids.add(e["entity_id"])
                matched.append(e)

    # Add motion events for summary queries
    if f["include_motion"]:
        for e in states:
            if e["entity_id"] not in seen_ids and e["entity_id"].split(".")[0] == "event" and "motion" in e["entity_id"]:
                if not _is_excluded(e):
                    seen_ids.add(e["entity_id"])
                    matched.append(e)

    if not matched:
        area_hint = f" in {area_id.replace('_', ' ')}" if area_id else ""
        return f"No matching entities found in Home Assistant{area_hint} for that query."

    _LOGGER.info("HA source: %d entities matched for query '%s'%s",
                 len(matched), query[:50], f" (area: {area_id})" if area_id else "")

    # Track motion event entity names to avoid double-counting with binary_sensors
    motion_event_names = {
        e["entity_id"].replace("event.", "").replace("_motion", "")
        for e in matched
        if e["entity_id"].split(".")[0] == "event"
    }

    # Group by domain
    groups: dict[str, list[str]] = {}
    for entity in matched:
        domain = entity["entity_id"].split(".")[0]
        dc = entity.get("attributes", {}).get("device_class", "")

        # Skip a binary_sensor motion entity ONLY if THIS SPECIFIC
        # physical sensor genuinely has its own matching event entity
        # — found via a deliberate "bulletproofing" pass: the original
        # check (`if domain == "binary_sensor" and dc == "motion" and
        # motion_event_names:`) only tested whether the set was
        # non-empty AT ALL, not whether THIS entity specifically had a
        # real counterpart in it. Confirmed via a direct, constructed
        # test: a home with one motion sensor reporting via an event
        # entity (e.g. "front_door") and a second, completely
        # unrelated motion sensor reporting only via binary_sensor
        # (e.g. "backyard," no event entity of its own) would silently
        # drop the backyard sensor entirely from a "any motion" query
        # — not because it was genuinely deduplicated against anything
        # real, but purely because SOME OTHER sensor in the house
        # happened to have event-based data.
        if domain == "binary_sensor" and dc == "motion":
            this_entity_name = entity["entity_id"].replace("binary_sensor.", "").replace("_motion", "")
            if this_entity_name in motion_event_names:
                continue

        # Found while verifying the binary_sensor-motion fix above:
        # "Door Sensors" was a reasonable hardcoded label when
        # binary_sensor entities were only ever reachable via
        # door-specific keywords, but is genuinely wrong now that
        # binary_sensor MOTION entities are correctly reachable too —
        # a backyard motion sensor shouldn't be labeled "Door Sensors."
        if domain == "binary_sensor" and dc == "motion":
            label = "Motion"
        else:
            label = {
                "light": "Lights",
                "lock": "Locks",
                "binary_sensor": "Door Sensors",
                "sensor": "Sensors",
                "event": "Motion",
                "switch": "Switches",
                # Found alongside the new "garage door"/"garage"
                # _QUERY_MAP entries above — without this, a real
                # cover.* entity would fall through to the generic
                # domain.replace("_", " ").title() fallback ("Cover"),
                # technically correct but less clear than every other
                # domain's dedicated, purpose-built label.
                "cover": "Garage Doors",
            }.get(domain, domain.replace("_", " ").title())

        if label not in groups:
            groups[label] = []

        if domain == "event":
            groups[label].append(_format_motion_event(entity))
        else:
            groups[label].append(_format_entity(entity))

    parts = []
    for label, items in groups.items():
        parts.append(f"**{label}:**\n" + "\n".join(f"- {item}" for item in items))

    return "\n\n".join(parts)
