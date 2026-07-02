"""
Mnemolis Adversarial Self-Testing
Periodically generates structurally-novel queries via combinatorial mutation
over the router's own real ingredient vocabulary, runs them through the real
production pipeline, and flags structural anomalies for human review.

See wiki/Adversarial-Self-Testing.md and the design doc this module
implements for the full rationale. The short version, since it's the single
most load-bearing constraint on everything in this file:

NOTHING HERE EVER JUDGES WHETHER A RESPONSE WAS CORRECT. An LLM-as-judge
approach to this exact task shape (generate a test input AND an expected
answer, then trust the LLM's own judgment) was measured at 6.3% precision in
real research (Liu et al. 2024, arxiv.org/html/2404.10304v1) — 93.7% of
"failures" were the judge's own invented expected-answer being wrong, not
the system under test. This module instead checks Mnemolis's own documented,
stated behavioral guarantees (does a discourse-framing query include kiwix
the way the README says it will; does a query built from N intents produce
something close to N decomposed parts) against what the real pipeline
actually did — a fundamentally different, reliable kind of check than "is
this answer right," requiring no ground truth and no human-replacement
judgment call from a model.

Generation is pure-Python combinatorics over real, already-existing
vocabulary lists (router.INTENT_MAP, router._CONJUNCTIONS,
router._NOSPLIT_PATTERNS, kiwix.DISCOURSE_FRAMING_PATTERNS) plus a small
hardcoded seed corpus of proper-noun pairs and conditional phrases — never
an LLM call per generated query. This mirrors the one part of the AID paper
(the same source above) directly reusable here: generate a script/resource
once, run it many times, rather than paying an LLM call per test case.
"""
import json
import logging
import random
import re
import sqlite3
from contextlib import closing
import time
from datetime import datetime, timezone

from app import router
from app.config import settings
from app.sources import kiwix
from app.sources import fusion
from app.sources.fusion import _looks_empty

_LOGGER = logging.getLogger(__name__)

ADVERSARIAL_DB = "/app/data/adversarial_testing.db"


def _connect(db_path: str) -> sqlite3.Connection:
    """Mirrors main.py's _connect() exactly — WAL mode, busy timeout, same
    as every other Mnemolis SQLite database, since this is a genuinely
    separate DB file but should behave identically under concurrent access
    from the scheduler and from GET /adversarial/flagged."""
    con = sqlite3.connect(db_path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    return con


def init_adversarial_db():
    """Create the adversarial_combinations table if it doesn't exist.
    Mirrors snapshots.init_snapshot_db()'s exact pattern."""
    con = None
    try:
        con = _connect(ADVERSARIAL_DB)
        con.execute("""
            CREATE TABLE IF NOT EXISTS adversarial_combinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                recipe_name TEXT NOT NULL,
                first_seen_timestamp TEXT NOT NULL,
                times_generated INTEGER NOT NULL DEFAULT 1,
                last_query_text TEXT NOT NULL,
                last_source_used TEXT,
                last_latency_ms INTEGER,
                last_flagged_reason TEXT,
                last_run_timestamp TEXT NOT NULL,
                ever_flagged INTEGER NOT NULL DEFAULT 0,
                first_flagged_reason TEXT,
                first_flagged_timestamp TEXT,
                review_status TEXT,
                last_flagged_result_excerpt TEXT
            )
        """)
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_adversarial_flagged
                ON adversarial_combinations (last_flagged_reason)
                WHERE last_flagged_reason IS NOT NULL
        """)

        # Schema migration for databases created before ever_flagged
        # tracking existed (every real deployment running 3.46.x before
        # this fix, including the live one on MiniDock with two real
        # days of history already in it) — CREATE TABLE IF NOT EXISTS
        # above is a no-op on an existing table, so the four new
        # columns need to be added explicitly to any table that
        # predates them. Each ALTER is independently guarded since
        # SQLite has no "ADD COLUMN IF NOT EXISTS" — only run the ALTER
        # for a column genuinely missing from THIS table, never for a
        # fresh table the CREATE TABLE above already covered.
        #
        # MUST run before the ever_flagged index below — found via a
        # real failing test: creating an index on a column before an
        # old-schema table has had that column added via ALTER raises
        # "no such column: ever_flagged", since CREATE TABLE IF NOT
        # EXISTS is correctly a no-op on a pre-existing table and never
        # retroactively adds the new columns by itself.
        existing_columns = {row[1] for row in con.execute("PRAGMA table_info(adversarial_combinations)").fetchall()}
        migrations = [
            ("ever_flagged", "ALTER TABLE adversarial_combinations ADD COLUMN ever_flagged INTEGER NOT NULL DEFAULT 0"),
            ("first_flagged_reason", "ALTER TABLE adversarial_combinations ADD COLUMN first_flagged_reason TEXT"),
            ("first_flagged_timestamp", "ALTER TABLE adversarial_combinations ADD COLUMN first_flagged_timestamp TEXT"),
            ("review_status", "ALTER TABLE adversarial_combinations ADD COLUMN review_status TEXT"),
            # Found via a real, unresolved investigation: a flag fired
            # once (unexpected_empty on a "changes" source query, real
            # production data), and every code-level hypothesis for WHY
            # was checked and ruled out against real evidence — LLM
            # outage, timezone misconfiguration, cold-start snapshots,
            # a swallowed exception, stale message text, all confirmed
            # NOT the cause. The investigation hit a hard wall: the
            # schema never stored the actual text that matched
            # _looks_empty(), only the fact that something did. Without
            # the real bytes, a real, live anomaly was structurally
            # unrecoverable after the fact — this column exists so the
            # NEXT one isn't.
            ("last_flagged_result_excerpt", "ALTER TABLE adversarial_combinations ADD COLUMN last_flagged_result_excerpt TEXT"),
        ]
        for column_name, ddl in migrations:
            if column_name not in existing_columns:
                con.execute(ddl)

        # Now safe to index ever_flagged — guaranteed to exist on every
        # table at this point, whether freshly created above or just
        # migrated by the loop immediately preceding this.
        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_adversarial_ever_flagged
                ON adversarial_combinations (ever_flagged)
                WHERE ever_flagged = 1
        """)

        # Backfill ever_flagged for any pre-existing row that already
        # has a last_flagged_reason from before this migration ran —
        # without this, a row flagged yesterday (before this fix
        # shipped) that happens to come back clean on its very next
        # run today would still vanish from history, the exact gap
        # this fix exists to close. Only backfills rows where
        # ever_flagged is still the freshly-migrated default of 0.
        con.execute("""
            UPDATE adversarial_combinations
            SET ever_flagged = 1,
                first_flagged_reason = last_flagged_reason,
                first_flagged_timestamp = last_run_timestamp
            WHERE last_flagged_reason IS NOT NULL AND ever_flagged = 0
        """)

        con.commit()
        _LOGGER.info("Adversarial testing DB initialized")
    except Exception as e:
        _LOGGER.warning("Could not initialize adversarial testing DB: %s", e)
    finally:
        # Guaranteed close even when a statement above raises — see
        # query_log_stats() in main.py for the pass-wide rationale.
        if con is not None:
            con.close()


# ---------------------------------------------------------------------------
# Seed vocabulary — section 4.3's "generate a resource once, reuse many
# times" LLM-use boundary. These are hardcoded starting points; periodic
# (weekly, not per-cycle) LLM-assisted expansion of these specific lists
# would be the one place an LLM call is worth its cost for this feature —
# NOT YET BUILT. No expand_seed_vocabulary() function exists anywhere in
# this module; the seed lists below are currently expanded by hand only.
# See the wiki's Adversarial Self-Testing page for this as a documented,
# deliberate follow-up, not something already wired in here.
# ---------------------------------------------------------------------------

# Real proper-noun pairs — the exact one that found bug 5, plus other
# genuinely capitalized two-entity pairs a person might plausibly ask about
# together. Kept verbatim per the design doc's instruction.
PROPER_NOUN_PAIRS = [
    "Iran and Israel",
    "Mercury and Venus",
    "Python and JavaScript",
    "the Beatles and the Rolling Stones",
]

# Real seed conditions — reused directly from tests/locustfile.py's
# CONDITIONAL_QUERIES / CONDITIONAL_WITH_REMAINDER_QUERIES rather than
# reinvented, per the design doc's explicit instruction not to duplicate
# this list. Verified against the real file: it actually contains FOUR
# conditions, not the three the design doc named — the fourth ("if mercury
# is in retrograde...") is folded in here too, since there's no reason to
# leave a real, already-existing seed on the table.
CONDITIONAL_SEEDS = [
    "if the back door is unlocked",
    "if any services are down",
    "if it is raining",
    "if mercury is in retrograde",
    # Added alongside locustfile.py's CONDITIONAL_QUERIES widening
    # (v3.50.2 benchmark pass) — kept in sync per
    # TestSeedVocabularyIntegrity's real, enforced requirement that every
    # locustfile CONDITIONAL_QUERIES entry have a matching seed here.
    "if the garage door is open",
    "if the network is down",
    "if it is going to snow",
    "if jupiter is in retrograde",
    # Added alongside locustfile.py's second CONDITIONAL_QUERIES widening
    # (8 -> 20 entries, v3.50.8 benchmark-driven pool sizing pass) — kept
    # in sync for the same reason as the block above.
    "if the front porch light is off",
    "if the freezer is too warm",
    "if it is going to be windy tomorrow",
    "if saturn is in retrograde",
    "if the side gate is unlocked",
    "if the internet connection drops",
    "if there is a frost warning tonight",
    "if mars is in retrograde",
    "if the basement sensor detects water",
    "if any cameras go offline",
    "if the humidity gets too high",
    "if venus is in retrograde",
    # Added alongside locustfile.py's THIRD CONDITIONAL_QUERIES widening
    # (20 -> 40 entries, v3.50.9 — this pass also corrected a real
    # sizing mistake from the v3.50.8 widening above, not just adding
    # more entries; see that file's own comment block for the full
    # story) — kept in sync for the same reason as the blocks above.
    "if any motion is detected outside",
    "if the lights status changes",
    "if any outages today",
    "if the battery levels are low",
    "if the security status changes",
    "if it is going to be cold tomorrow",
    "if the indoor air quality drops",
    "if the power consumption spikes",
    "if the server status changes",
    "if is it down right now",
    "if the network status changes",
    "if any new outages appear",
    "if the house status changes",
    "if the outdoor conditions get bad",
    "if are the doors locked",
    "if low battery is detected",
    "if any new headlines appear",
    "if the door locked status changes",
    "if the wind forecast looks bad",
    "if the energy usage spikes",
]

# Consequence phrases to pair with conditions — plain text, since Mnemolis
# has no reminder/trigger capability and never acts on these; they only
# exercise detect_conditional()'s parsing of the consequence segment.
CONSEQUENCE_SEEDS = [
    "let me know",
    "let me know right away",
    "remind me to bring an umbrella",
    "I will be careful with communication",
]

# Verb phrases for the proper_noun_plus_pronoun_intent recipe — paired with
# a leading pronoun "I" to reproduce bug 5's exact shape (a proper-noun pair
# immediately followed by a conjunction and the pronoun "I").
PRONOUN_VERB_PHRASES = [
    "I keep getting a weird numpy import error on my raspberry pi",
    "I need to know if any services are down",
    "I was wondering what the deal with sunspots is",
    "I forgot to ask about the weather this weekend",
]

# Generic discourse-bait topics — genuinely encyclopedic things people
# plausibly frame as "everyone's talking about X", used to give the
# discourse_framing_plus_real_keyword recipe a real noun phrase to attach
# the discourse pattern to, rather than letting the pattern run directly
# into an unrelated keyword as one ungrammatical fragment.
_DISCOURSE_TOPICS = [
    "sunspots", "quantum computing", "the James Webb telescope",
    "that volcano in Iceland", "the new tariffs", "black holes",
]


def _stable_hash_ingredients(*parts: str) -> tuple:
    """Build the dedup key for a combination — the tuple of ingredient
    identifiers actually used, not the literal generated string. Sorted so
    ingredient order never produces a spurious distinct fingerprint."""
    return tuple(sorted(parts))


# ---------------------------------------------------------------------------
# Recipes — each one pure Python, no LLM call. Each returns
# (query_text, recipe_name, fingerprint).
# ---------------------------------------------------------------------------

def _recipe_proper_noun_plus_pronoun_intent(rng: random.Random) -> tuple[str, str, tuple]:
    """'<pair>, <conjunction> I <verb phrase>' — the exact bug-5 shape."""
    pair = rng.choice(PROPER_NOUN_PAIRS)
    conj = rng.choice(router._CONJUNCTIONS)
    verb_phrase = rng.choice(PRONOUN_VERB_PHRASES)
    query = f"also whats happening with {pair},{conj}{verb_phrase}"
    fingerprint = _stable_hash_ingredients("proper_noun_plus_pronoun_intent", pair, conj, verb_phrase)
    return query, "proper_noun_plus_pronoun_intent", fingerprint


def _recipe_multi_intent_chain(rng: random.Random) -> tuple[str, str, tuple]:
    """3-5 independent intents joined by DIFFERENT conjunctions, drawn from
    DIFFERENT INTENT_MAP sources each time."""
    sources = list(router.INTENT_MAP.keys())
    n = rng.randint(3, min(5, len(sources)))
    chosen_sources = rng.sample(sources, n)
    phrases = [rng.choice(router.INTENT_MAP[s]) for s in chosen_sources]
    conjunctions = rng.sample(router._CONJUNCTIONS, min(n - 1, len(router._CONJUNCTIONS)))
    # If we need more conjunctions than distinct ones exist, allow repeats
    # for the remainder rather than crashing on a short sample.
    while len(conjunctions) < n - 1:
        conjunctions.append(rng.choice(router._CONJUNCTIONS))

    query_parts = [phrases[0]]
    for i in range(1, n):
        query_parts.append(conjunctions[i - 1])
        query_parts.append(phrases[i])
    query = "".join(query_parts)
    fingerprint = _stable_hash_ingredients(
        "multi_intent_chain", *chosen_sources, *conjunctions
    )
    return query, "multi_intent_chain", fingerprint


def _recipe_conditional_with_remainder(rng: random.Random) -> tuple[str, str, tuple]:
    """'if <condition>, <consequence>, <conjunction> <unrelated second
    intent>' — novel condition/remainder combinations, not the same 2 fixed
    strings tests/locustfile.py already covers."""
    condition = rng.choice(CONDITIONAL_SEEDS)
    consequence = rng.choice(CONSEQUENCE_SEEDS)
    remainder_source = rng.choice(list(router.INTENT_MAP.keys()))
    remainder_phrase = rng.choice(router.INTENT_MAP[remainder_source])
    conj = rng.choice(router._CONJUNCTIONS)
    query = f"{condition}, {consequence},{conj}{remainder_phrase}"
    fingerprint = _stable_hash_ingredients(
        "conditional_with_remainder", condition, consequence, remainder_source, conj
    )
    return query, "conditional_with_remainder", fingerprint


def _recipe_nosplit_adjacent_to_real_conjunction(rng: random.Random) -> tuple[str, str, tuple]:
    """A _NOSPLIT_PATTERNS phrase placed adjacent to a DIFFERENT, unrelated
    real conjunction elsewhere in the query — tests whether the nosplit
    veto is scoped correctly (per-occurrence) rather than vetoing the whole
    query (the real, found, and fixed global-veto bug)."""
    nosplit_phrase = rng.choice(router._NOSPLIT_PATTERNS)
    pair = rng.choice(PROPER_NOUN_PAIRS)
    other_source = rng.choice(list(router.INTENT_MAP.keys()))
    other_phrase = rng.choice(router.INTENT_MAP[other_source])
    conj = rng.choice(router._CONJUNCTIONS)
    query = f"{nosplit_phrase} {pair},{conj}{other_phrase}"
    fingerprint = _stable_hash_ingredients(
        "nosplit_adjacent_to_real_conjunction", nosplit_phrase, pair, other_source, conj
    )
    return query, "nosplit_adjacent_to_real_conjunction", fingerprint


def _recipe_discourse_framing_plus_real_keyword(rng: random.Random) -> tuple[str, str, tuple]:
    """A DISCOURSE_FRAMING_PATTERNS phrase immediately followed by a
    DIFFERENT source's INTENT_MAP keyword — tests whether the
    discourse-escalation bias still adds kiwix even when a clean keyword
    match for a different source exists.

    A topic noun is inserted between the discourse phrase and the
    keyword (e.g. "everyone keeps talking about sunspots, and also
    forecast change") so the keyword genuinely reads as its own clean,
    separate clause rather than running directly into the discourse
    phrase as a single ungrammatical fragment — a run-on like "everyone
    keeps talking going to be hot" wouldn't actually test the thing this
    recipe is meant to test, since a router failing on it could just as
    easily mean "nonsense input handled reasonably" as a real bug.
    """
    discourse_phrase = rng.choice(kiwix.DISCOURSE_FRAMING_PATTERNS)
    topic = rng.choice(_DISCOURSE_TOPICS)
    other_source = rng.choice(list(router.INTENT_MAP.keys()))
    other_phrase = rng.choice(router.INTENT_MAP[other_source])
    conj = rng.choice(router._CONJUNCTIONS)
    query = f"{discourse_phrase} {topic},{conj}{other_phrase}"
    fingerprint = _stable_hash_ingredients(
        "discourse_framing_plus_real_keyword", discourse_phrase, topic, other_source, conj
    )
    return query, "discourse_framing_plus_real_keyword", fingerprint


def _recipe_nested_proper_noun_pairs(rng: random.Random) -> tuple[str, str, tuple]:
    """Two distinct proper-noun pairs joined by a conjunction, with a third
    real intent after — stress-tests whether the per-occurrence proper-
    noun-pair guard protects BOTH pairs independently."""
    pair_a, pair_b = rng.sample(PROPER_NOUN_PAIRS, 2)
    inner_conj = rng.choice(router._CONJUNCTIONS)
    outer_conj = rng.choice(router._CONJUNCTIONS)
    third_source = rng.choice(list(router.INTENT_MAP.keys()))
    third_phrase = rng.choice(router.INTENT_MAP[third_source])
    query = f"whats the deal with {pair_a}{inner_conj}{pair_b},{outer_conj}{third_phrase}"
    fingerprint = _stable_hash_ingredients(
        "nested_proper_noun_pairs", pair_a, pair_b, inner_conj, outer_conj, third_source
    )
    return query, "nested_proper_noun_pairs", fingerprint


def _recipe_no_intent_fallthrough(rng: random.Random) -> tuple[str, str, tuple]:
    """A query with no recognized INTENT_MAP keyword at all — per section
    8's flagged sub-question, this is itself a real, valid test (does it
    correctly fall through to Kiwix/LLM-assisted routing?) and should be a
    named recipe rather than an accidental gap, since kiwix/fusion aren't
    themselves INTENT_MAP keys."""
    topics = [
        "molybdenum", "the history of the printing press", "tectonic plates",
        "how transformers work in machine learning", "the Roman aqueducts",
    ]
    topic = rng.choice(topics)
    query = f"what is the deal with {topic}"
    fingerprint = _stable_hash_ingredients("no_intent_fallthrough", topic)
    return query, "no_intent_fallthrough", fingerprint


# Recipe registry — name -> generator function. Used both for generation
# and for per-recipe latency-history lookups in _check_latency_outlier().
RECIPES = {
    "proper_noun_plus_pronoun_intent": _recipe_proper_noun_plus_pronoun_intent,
    "multi_intent_chain": _recipe_multi_intent_chain,
    "conditional_with_remainder": _recipe_conditional_with_remainder,
    "nosplit_adjacent_to_real_conjunction": _recipe_nosplit_adjacent_to_real_conjunction,
    "discourse_framing_plus_real_keyword": _recipe_discourse_framing_plus_real_keyword,
    "nested_proper_noun_pairs": _recipe_nested_proper_noun_pairs,
    "no_intent_fallthrough": _recipe_no_intent_fallthrough,
}


def _already_tried(fingerprint: tuple) -> bool:
    """Check whether this fingerprint has already been recorded."""
    try:
        with closing(_connect(ADVERSARIAL_DB)) as con:
            row = con.execute(
                "SELECT 1 FROM adversarial_combinations WHERE fingerprint = ?",
                (json.dumps(fingerprint),)
            ).fetchone()
        return row is not None
    except Exception as e:
        _LOGGER.warning("Could not check fingerprint history: %s", e)
        return False


def generate_adversarial_query(rng: random.Random | None = None) -> tuple[str, dict]:
    """Generate one adversarial query, biasing toward fingerprints never
    seen before per section 5's dedup strategy.

    Returns (query_text, metadata) where metadata includes recipe_name,
    fingerprint (as a JSON string, the actual dedup key stored), and the
    raw ingredient tuple for logging/debugging.
    """
    if rng is None:
        rng = random.Random()

    recipe_names = list(RECIPES.keys())
    rng.shuffle(recipe_names)

    # Try each recipe (in shuffled order) up to a few times each, biasing
    # toward a never-seen fingerprint before falling back to whatever was
    # generated last — guarantees termination without an unbounded loop,
    # since the seed vocabulary is finite and combinations can run out.
    attempts_per_recipe = 5
    fallback: tuple[str, str, tuple] | None = None

    for recipe_name in recipe_names:
        recipe_fn = RECIPES[recipe_name]
        for _ in range(attempts_per_recipe):
            query, name, fingerprint = recipe_fn(rng)
            fallback = (query, name, fingerprint)
            if not _already_tried(fingerprint):
                return query, {
                    "recipe_name": name,
                    "fingerprint": json.dumps(fingerprint),
                    "ingredients": list(fingerprint),
                    "novel": True,
                }

    # Every recipe's sampled attempts were already-seen fingerprints —
    # genuinely fine per section 5 ("deprioritize", not "forbid"); return
    # the last one generated rather than failing the whole cycle.
    query, name, fingerprint = fallback
    return query, {
        "recipe_name": name,
        "fingerprint": json.dumps(fingerprint),
        "ingredients": list(fingerprint),
        "novel": False,
    }


# ---------------------------------------------------------------------------
# Section 6 — structural anomaly detection. No correctness judgment, ever.
# Every check here verifies a documented, stated Mnemolis behavioral
# guarantee against what actually happened — never "is this answer right."
# ---------------------------------------------------------------------------

# Built from the real, exact header strings fusion._format_header() can
# produce — re.escape()'d, not a generic bracket-matching character
# class. Found via tracing a real, live conditional_remainder_missing_
# sections flag: the original generic pattern
# (r"\[[A-Z0-9_ ]+ — [A-Z0-9_ ,'/]+\]") required exactly ONE literal
# " — " separator, with the tail character class deliberately excluding
# the em-dash itself — but kiwix's real label ("ENCYCLOPEDIC KNOWLEDGE
# — UNRELATED TO OTHER SECTIONS BELOW") and news's real label ("RECENT
# NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED") both
# legitimately contain a SECOND em-dash, breaking the match entirely.
# Confirmed this silently undercounted real headers in BOTH places this
# pattern is used: _check_conditional_remainder_sections (the original
# real-world flag this was traced from) and _check_multi_intent_part_count
# (where it explains the two real "intended 5, found 3" flags far more
# completely than the threshold-looseness fix in 3.48.1 alone did — that
# fix made the check tolerant of undercounting without ever finding why
# the undercount was happening in the first place). Same safe pattern
# router.py's own _dedupe_nested_fusion_sections() already uses for the
# identical underlying need.
_HEADER_PATTERN = re.compile(
    "(" + "|".join(re.escape(fusion._format_header(src)) for src in fusion._HEADER_LABELS) + ")"
)


def _check_crash(result: str, error: str | None) -> str | None:
    """Highest priority: a literal crash. The only category here that
    genuinely doesn't need human judgment at all."""
    if error:
        return f"crash: {error[:200]}"
    if result and "Traceback (most recent call last)" in result:
        return "crash: raw traceback in result body"
    return None


def _check_source_mismatch(recipe_name: str, ingredients: list, source_used: str) -> str | None:
    """source_used doesn't match any source whose INTENT_MAP keywords or
    recipe-intended source appear in the generated query at all."""
    intended_sources = {
        ing for ing in ingredients if ing in router.INTENT_MAP
    }
    if not intended_sources:
        return None
    if source_used not in intended_sources and source_used != "fusion":
        return f"source_mismatch: intended {sorted(intended_sources)}, got '{source_used}'"
    return None


def _check_multi_intent_part_count(recipe_name: str, ingredients: list, result: str) -> str | None:
    """A multi_intent_chain query where FEWER THAN HALF of its intended
    intents produced any header in the final result — the same kind of
    signal that originally caught the real proper-noun-pair bug 5, but
    deliberately loosened from an earlier, tighter version that
    compared header count almost exactly against intended count.

    Found via real production data on MiniDock, not theoretical: that
    earlier version flagged "5 intended, 3 headers found" as a
    part_count_mismatch — traced directly and confirmed this was a
    FALSE POSITIVE, not a real bug. Decomposition produced all 5
    correct parts, every part resolved to the correct source, and
    route_with_source() correctly and intentionally dropped 2 of the 5
    sub-query results because they came back genuinely empty (see its
    own `if not _looks_empty(sub_result): parts.append(...)` —
    deliberate, correct behavior; nobody wants an answer cluttered with
    empty sections). By the time this check sees the final merged
    result string, there is NO trace anywhere of how many sub-queries
    were tried and legitimately came back empty versus how many
    results were silently lost to a real bug — that information is
    gone before _merge_decomposed_parts() is ever called, and this
    check has no way to recover it without re-running every sub-query's
    real backend call a second time, which would double real load on
    every single test cycle just to validate the check itself.

    Given that real, structural blind spot, an exact-count comparison
    fundamentally cannot distinguish "2 of 5 random, unrelated topics
    legitimately had nothing to report" from "2 of 5 results vanished
    due to a bug" — both produce an identical signature. What it CAN
    still meaningfully catch: the original bug 5 itself was a global
    veto that collapsed an entire multi-intent query down to a single
    un-split string — n_headers of 0 or 1 against 4+ intended sources,
    not a partial 2-of-5 gap. "Fewer than half survived" is loose
    enough to never fire on ordinary empty-result variance across this
    recipe's real range (3-5 intended sources), while still catching a
    genuine large-scale collapse with the same shape as the original
    bug this check exists to guard against.
    """
    if recipe_name != "multi_intent_chain":
        return None
    intended_sources = [ing for ing in ingredients if ing in router.INTENT_MAP]
    n_intended = len(intended_sources)
    n_headers = len(_HEADER_PATTERN.findall(result or ""))
    # A single-source result legitimately has zero headers (no attribution
    # needed when nothing was merged) — only flag a REAL discrepancy once
    # more than one source was actually expected to appear.
    if n_intended >= 2 and n_headers < (n_intended / 2):
        return (
            f"part_count_mismatch: only {n_headers} of {n_intended} intended "
            f"intents produced a header (less than half) — possible large-scale "
            f"content loss, not just ordinary empty-result variance"
        )
    return None


def _check_discourse_framing_dropped_kiwix(recipe_name: str, result: str, source_used: str) -> str | None:
    """A discourse_framing_plus_real_keyword query did NOT result in kiwix
    being part of the chosen source(s) — direct, mechanical check against
    the documented discourse-framing bias's own stated guarantee.

    Only trusts source_used and the structural "[KIWIX — ..." header
    marker fusion.py actually emits — never a freeform substring search
    for the word "kiwix" anywhere in the response body. A naive substring
    check incorrectly passes a result that explicitly states kiwix was
    NOT used (e.g. "plain web result, no kiwix involved" contains the
    literal substring "kiwix" while describing the exact failure this
    check exists to catch) — found via a real, failing unit test, not a
    hypothetical concern.
    """
    if recipe_name != "discourse_framing_plus_real_keyword":
        return None
    if source_used == "kiwix":
        return None
    if "[KIWIX —" in (result or ""):
        return None
    return f"discourse_framing_dropped_kiwix: source_used='{source_used}'"


def _check_conditional_remainder_sections(recipe_name: str, result: str) -> str | None:
    """A conditional_with_remainder query's response doesn't contain two
    distinct, separately-headered sections."""
    if recipe_name != "conditional_with_remainder":
        return None
    n_headers = len(_HEADER_PATTERN.findall(result or ""))
    if n_headers < 1:
        return "conditional_remainder_missing_sections: no [SOURCE — LABEL] headers found"
    return None


def _check_unexpected_empty(result: str) -> str | None:
    """The response contains one of fusion._looks_empty()'s own canonical
    phrases when the generated query was specifically constructed to have
    a real, answerable intent."""
    if _looks_empty(result or ""):
        return "unexpected_empty: result matched a known empty/error phrase"
    return None


def _check_latency_outlier(recipe_name: str, latency_ms: int) -> str | None:
    """A generated query taking meaningfully longer than the same recipe's
    own historical p95 — independent of content correctness."""
    try:
        with closing(_connect(ADVERSARIAL_DB)) as con:
            rows = con.execute(
                "SELECT last_latency_ms FROM adversarial_combinations "
                "WHERE recipe_name = ? AND last_latency_ms IS NOT NULL "
                "ORDER BY id DESC LIMIT 50",
                (recipe_name,)
            ).fetchall()
    except Exception:
        return None

    samples = [r[0] for r in rows if r[0] is not None]
    min_samples = settings.adversarial_test_latency_outlier_min_samples
    if len(samples) < min_samples:
        # Not enough history yet to call anything an outlier — this is a
        # genuine "not yet decidable" state, not a clean pass.
        return None
    samples_sorted = sorted(samples)
    p95_index = max(0, int(len(samples_sorted) * 0.95) - 1)
    p95 = samples_sorted[p95_index]
    multiplier = settings.adversarial_test_latency_outlier_multiplier
    floor_ms = settings.adversarial_test_latency_outlier_floor_ms
    if latency_ms > p95 * multiplier and latency_ms > floor_ms:
        return f"latency_outlier: {latency_ms}ms vs recipe p95 of {p95}ms"
    return None


def _detect_anomalies(
    recipe_name: str,
    ingredients: list,
    query: str,
    result: str,
    source_used: str,
    error: str | None,
    latency_ms: int,
) -> str | None:
    """Run every section-6 check in priority order, returning the first
    (highest-priority) match. Crash detection always runs first since it's
    the one category that needs no judgment at all."""
    checks = [
        lambda: _check_crash(result, error),
        lambda: _check_source_mismatch(recipe_name, ingredients, source_used),
        lambda: _check_multi_intent_part_count(recipe_name, ingredients, result),
        lambda: _check_discourse_framing_dropped_kiwix(recipe_name, result, source_used),
        lambda: _check_conditional_remainder_sections(recipe_name, result),
        lambda: _check_unexpected_empty(result),
        lambda: _check_latency_outlier(recipe_name, latency_ms),
    ]
    for check in checks:
        reason = check()
        if reason:
            return reason
    return None


# ---------------------------------------------------------------------------
# Cycle execution and persistence
# ---------------------------------------------------------------------------

def _record_result(
    fingerprint_json: str,
    recipe_name: str,
    query: str,
    source_used: str | None,
    latency_ms: int | None,
    flagged_reason: str | None,
    result: str | None = None,
):
    """Upsert one combination's result. INSERT on first sighting, UPDATE
    (incrementing times_generated, overwriting the 'last_*' columns) on
    every subsequent sighting of the same fingerprint.

    last_flagged_reason still gets overwritten to NULL on a clean run,
    same as before — GET /adversarial/flagged's "currently flagged"
    view should reflect the most recent real result, not an artificially
    preserved stale flag. But a fingerprint that has EVER been flagged,
    even once, now stays marked via ever_flagged (sticky — never reset
    back to 0) with the ORIGINAL anomaly preserved in
    first_flagged_reason/first_flagged_timestamp, regardless of how many
    later clean runs overwrite the last_* columns.

    Fixes a real gap a reviewer caught: the previous version only ever
    tracked "currently flagged," so an intermittent anomaly (a flaky
    latency outlier, a transient bug that doesn't reproduce on every
    run) could be flagged once, then silently vanish from
    /adversarial/flagged the moment the same fingerprint happened to be
    re-rolled and came back clean — with no human ever having reviewed
    or dismissed it. "Currently flagged" and "ever flagged" are now
    genuinely separate, queryable facts; GET /adversarial/flagged
    defaults to showing the union of both (see get_flagged_combinations)
    rather than only the narrower, disappearing "currently flagged" set.

    `result` is the real response text — only ever truncated and stored
    when flagged_reason is genuinely set, never on a clean run, so this
    doesn't bloat the database with full result text for the
    overwhelming majority of combinations that never need it. Found
    necessary via a real, unresolved investigation: a flag fired once,
    every code-level hypothesis for why was checked and ruled out
    against real evidence, and the investigation hit a hard wall the
    schema itself created — there was no way to ever see the actual
    text that matched _looks_empty(), only the fact that something did.
    Truncated to 500 chars — enough to identify which real phrase
    matched and see the surrounding context, not a full replacement for
    the real result, which can be considerably longer.
    """
    EXCERPT_MAX_CHARS = 500
    excerpt = (result or "")[:EXCERPT_MAX_CHARS] if flagged_reason else None

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = None
    try:
        con = _connect(ADVERSARIAL_DB)
        existing = con.execute(
            "SELECT times_generated, ever_flagged FROM adversarial_combinations WHERE fingerprint = ?",
            (fingerprint_json,)
        ).fetchone()
        if existing is None:
            ever_flagged = 1 if flagged_reason else 0
            con.execute(
                """INSERT INTO adversarial_combinations
                   (fingerprint, recipe_name, first_seen_timestamp, times_generated,
                    last_query_text, last_source_used, last_latency_ms,
                    last_flagged_reason, last_run_timestamp,
                    ever_flagged, first_flagged_reason, first_flagged_timestamp,
                    last_flagged_result_excerpt)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (fingerprint_json, recipe_name, now, query, source_used, latency_ms,
                 flagged_reason, now,
                 ever_flagged, flagged_reason if ever_flagged else None, now if ever_flagged else None,
                 excerpt)
            )
        else:
            already_ever_flagged = bool(existing[1])
            if flagged_reason and not already_ever_flagged:
                # First time this specific fingerprint has ever been
                # flagged — record the original anomaly permanently.
                con.execute(
                    """UPDATE adversarial_combinations
                       SET times_generated = times_generated + 1,
                           last_query_text = ?,
                           last_source_used = ?,
                           last_latency_ms = ?,
                           last_flagged_reason = ?,
                           last_run_timestamp = ?,
                           ever_flagged = 1,
                           first_flagged_reason = ?,
                           first_flagged_timestamp = ?,
                           last_flagged_result_excerpt = ?
                       WHERE fingerprint = ?""",
                    (query, source_used, latency_ms, flagged_reason, now,
                     flagged_reason, now, excerpt, fingerprint_json)
                )
            elif flagged_reason and already_ever_flagged:
                # A NEW flag firing on a fingerprint that's been flagged
                # before — including one a human already dismissed.
                # Clears review_status back to NULL so it resurfaces in
                # get_flagged_combinations()'s default view: a fresh
                # anomaly is a new event genuinely worth a fresh look,
                # not something an earlier, unrelated dismissal should
                # keep permanently suppressed.
                #
                # Found via a real failing test, not written defensively
                # up front: the first version of this function only ever
                # SET review_status via dismiss_flagged_combination(),
                # and nothing anywhere ever cleared it back — so a
                # dismissed-then-reflagged combination stayed invisible
                # forever, the opposite of the intended behavior. The
                # first_flagged_* columns are still deliberately left
                # untouched here — those preserve the ORIGINAL anomaly,
                # not the newest one.
                con.execute(
                    """UPDATE adversarial_combinations
                       SET times_generated = times_generated + 1,
                           last_query_text = ?,
                           last_source_used = ?,
                           last_latency_ms = ?,
                           last_flagged_reason = ?,
                           last_run_timestamp = ?,
                           review_status = NULL,
                           last_flagged_result_excerpt = ?
                       WHERE fingerprint = ?""",
                    (query, source_used, latency_ms, flagged_reason, now, excerpt, fingerprint_json)
                )
            else:
                # A clean run — whether on a never-flagged combination
                # or one with flag history. review_status is left
                # completely untouched here: a clean result is not a
                # reason to un-dismiss something a human already closed.
                # last_flagged_result_excerpt is likewise left untouched
                # on a clean run — it should keep showing whatever text
                # actually caused the LAST real flag, not be wiped just
                # because a later, unrelated clean run happened.
                con.execute(
                    """UPDATE adversarial_combinations
                       SET times_generated = times_generated + 1,
                           last_query_text = ?,
                           last_source_used = ?,
                           last_latency_ms = ?,
                           last_flagged_reason = ?,
                           last_run_timestamp = ?
                       WHERE fingerprint = ?""",
                    (query, source_used, latency_ms, flagged_reason, now, fingerprint_json)
                )
        con.commit()
    except Exception as e:
        _LOGGER.warning("Could not record adversarial test result: %s", e)
    finally:
        # Guaranteed close even when a statement above raises — see
        # query_log_stats() in main.py for the pass-wide rationale.
        if con is not None:
            con.close()


def run_adversarial_test_cycle() -> dict:
    """The scheduled job body. Generates a small batch of queries, routes
    each through the real route_with_source() pipeline exactly the way a
    real user query would, checks for structural anomalies, and persists
    results to adversarial_testing.db only.

    Wraps the route_with_source() call in router.suppress_cache_writes()
    so these synthetic, generated queries can exercise the genuinely real
    routing/fallback/fusion pipeline (the entire point of this feature)
    without writing into cache.json or routing_cache.json — the real,
    found gap this docstring used to claim didn't exist. Confirmed
    directly, before this fix: route_with_source() writes to both files
    as an unconditional side effect of any successful query, synthetic or
    not, since caching happens deep inside _resolve_single_source() and
    _llm_detect()/_llm_pick_fusion_sources(), several calls below
    anything this function controls directly. See router.py's own
    module-level comment next to _SUPPRESS_CACHE_WRITES for the full
    account, including why this needed a context-local mechanism rather
    than a plain flag.

    Returns a small summary dict so POST /adversarial/trigger has
    something real to report back, rather than a bare 200 with no way
    to confirm what actually happened on that specific call.

    Checks ADVERSARIAL_TEST_ENABLED itself, not just at scheduler
    registration time in main.py's lifespan — defense in depth, so a
    direct call (e.g. POST /adversarial/trigger, or a future caller)
    can never accidentally run real queries against the LLM/SearXNG/
    Kiwix backends while the feature is supposed to be off.
    """
    if not settings.adversarial_test_enabled:
        _LOGGER.info("Adversarial testing is disabled (ADVERSARIAL_TEST_ENABLED=false); skipping cycle")
        return {"status": "disabled", "queries_run": 0, "flagged": 0}

    rng = random.Random()
    batch_size = settings.adversarial_test_batch_size
    flagged_count = 0

    for _ in range(batch_size):
        try:
            query, meta = generate_adversarial_query(rng)
            recipe_name = meta["recipe_name"]
            fingerprint_json = meta["fingerprint"]
            ingredients = meta["ingredients"]

            start = time.monotonic()
            result = None
            source_used = None
            error = None
            try:
                with router.suppress_cache_writes():
                    result, source_used = router.route_with_source(query, source="auto")
            except Exception as e:
                error = str(e)
            latency_ms = int((time.monotonic() - start) * 1000)

            flagged_reason = _detect_anomalies(
                recipe_name, ingredients, query, result or "", source_used or "", error, latency_ms
            )

            _record_result(fingerprint_json, recipe_name, query, source_used, latency_ms, flagged_reason, result)

            if flagged_reason:
                flagged_count += 1
                _LOGGER.warning(
                    "Adversarial test flagged: recipe=%s query=%r reason=%s",
                    recipe_name, query[:100], flagged_reason
                )
        except Exception as e:
            # A failure generating or processing ONE query in the batch
            # should never abort the whole cycle — each iteration is
            # independent, same as every snapshot job's own per-source
            # try/except convention.
            _LOGGER.warning("Adversarial test cycle iteration failed: %s", e)

    return {"status": "ran", "queries_run": batch_size, "flagged": flagged_count}


def get_adversarial_test_summary() -> dict:
    """Summary for /health. Mirrors get_snapshot_job_health()'s naming
    convention and overall shape — status, last_run, and counts that make
    growth and review backlog visible without digging through logs.

    Reports "disabled" up front when ADVERSARIAL_TEST_ENABLED is false,
    rather than letting an intentionally-turned-off feature eventually
    read as "stale" — a deliberate off-switch shouldn't look like a
    silent failure the way a job that stopped running unexpectedly
    should.
    """
    if not settings.adversarial_test_enabled:
        return {"status": "disabled"}

    try:
        with closing(_connect(ADVERSARIAL_DB)) as con:
            total_row = con.execute("SELECT COUNT(*) FROM adversarial_combinations").fetchone()
            # Matches get_flagged_combinations()'s default (include_dismissed=
            # False) definition exactly — the count here and the actual rows
            # returned by GET /adversarial/flagged must never silently
            # disagree about what "flagged for review" means.
            flagged_row = con.execute(
                "SELECT COUNT(*) FROM adversarial_combinations "
                "WHERE (last_flagged_reason IS NOT NULL OR ever_flagged = 1) "
                "AND (review_status IS NULL OR review_status != 'dismissed')"
            ).fetchone()
            last_run_row = con.execute(
                "SELECT MAX(last_run_timestamp) FROM adversarial_combinations"
            ).fetchone()
    except Exception as e:
        return {"status": "unknown", "error": str(e)}

    total = total_row[0] if total_row else 0
    flagged = flagged_row[0] if flagged_row else 0
    last_run = last_run_row[0] if last_run_row else None

    if last_run is None:
        return {
            "status": "never_ran",
            "total_combinations_tried": 0,
            "flagged_for_review": 0,
        }

    try:
        last_run_dt = datetime.strptime(last_run, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        minutes_since = (datetime.now(timezone.utc) - last_run_dt).total_seconds() / 60
    except Exception:
        minutes_since = None

    interval = settings.adversarial_test_interval_minutes
    is_stale = (
        minutes_since is not None
        and minutes_since > interval * settings.snapshot_stale_grace_multiplier
    )

    return {
        "status": "stale" if is_stale else "ok",
        "last_run": last_run,
        "minutes_since_last_run": round(minutes_since, 1) if minutes_since is not None else None,
        "total_combinations_tried": total,
        "flagged_for_review": flagged,
    }


def get_flagged_combinations(limit: int = 50, include_dismissed: bool = False) -> list[dict]:
    """Return rows for GET /adversarial/flagged for human review.

    Returns the UNION of "currently flagged" (last_flagged_reason IS NOT
    NULL) and "ever flagged, not yet dismissed" (ever_flagged = 1 AND
    review_status IS NOT 'dismissed') — not just the narrower "currently
    flagged" set a previous version of this function used.

    Fixes a real gap a reviewer caught: under the old query, a
    fingerprint flagged once for an intermittent anomaly (a flaky
    latency outlier, a transient bug) would silently vanish from this
    endpoint the moment it happened to be re-rolled and came back clean
    — with no human ever having reviewed or dismissed it. A row now only
    leaves this list once a human explicitly dismisses it via
    dismiss_flagged_combination(), regardless of how many later clean
    runs overwrite last_flagged_reason back to NULL.

    Set include_dismissed=True to also see rows a human has already
    reviewed and closed out — useful for an audit trail, not the
    default working view.
    """
    try:
        with closing(_connect(ADVERSARIAL_DB)) as con:
            if include_dismissed:
                where_clause = "WHERE last_flagged_reason IS NOT NULL OR ever_flagged = 1"
            else:
                where_clause = (
                    "WHERE (last_flagged_reason IS NOT NULL OR ever_flagged = 1) "
                    "AND (review_status IS NULL OR review_status != 'dismissed')"
                )
            rows = con.execute(
                f"""SELECT fingerprint, recipe_name, first_seen_timestamp, times_generated,
                          last_query_text, last_source_used, last_latency_ms,
                          last_flagged_reason, last_run_timestamp,
                          ever_flagged, first_flagged_reason, first_flagged_timestamp,
                          review_status, last_flagged_result_excerpt
                   FROM adversarial_combinations
                   {where_clause}
                   ORDER BY last_run_timestamp DESC
                   LIMIT ?""",
                (limit,)
            ).fetchall()
    except Exception as e:
        _LOGGER.warning("Could not fetch flagged adversarial combinations: %s", e)
        return []

    columns = [
        "fingerprint", "recipe_name", "first_seen_timestamp", "times_generated",
        "last_query_text", "last_source_used", "last_latency_ms",
        "last_flagged_reason", "last_run_timestamp",
        "ever_flagged", "first_flagged_reason", "first_flagged_timestamp",
        "review_status", "last_flagged_result_excerpt",
    ]
    results = [dict(zip(columns, row)) for row in rows]
    for r in results:
        r["ever_flagged"] = bool(r["ever_flagged"])
        # currently_flagged is the same condition the OLD version of
        # this function used as its only filter — kept as an explicit
        # field so a caller can still distinguish "actively anomalous
        # right now" from "has a history but currently clean."
        r["currently_flagged"] = r["last_flagged_reason"] is not None
    return results


def dismiss_flagged_combination(fingerprint: str) -> bool:
    """Mark a fingerprint as reviewed and dismissed by a human — the
    real action that actually closes the loop the ever_flagged tracking
    exists to support. Returns True if a matching row was found and
    updated, False otherwise (unknown fingerprint, or a DB error).

    Deliberately does NOT clear ever_flagged or first_flagged_* — the
    historical fact that this combination was once flagged should
    survive a dismissal, for the same reason court records aren't
    deleted when a case is closed. review_status is the layer that
    actually controls visibility in get_flagged_combinations()'s
    default view, not the underlying history.
    """
    try:
        with closing(_connect(ADVERSARIAL_DB)) as con:
            cursor = con.execute(
                "UPDATE adversarial_combinations SET review_status = 'dismissed' WHERE fingerprint = ?",
                (fingerprint,)
            )
            con.commit()
            updated = cursor.rowcount > 0
        return updated
    except Exception as e:
        _LOGGER.warning("Could not dismiss flagged combination %r: %s", fingerprint, e)
        return False


def undismiss_flagged_combination(fingerprint: str) -> bool:
    """Reverse a dismissal, putting a combination back into the default
    review queue. The real, symmetric counterpart to
    dismiss_flagged_combination() — found necessary via real usage, not
    written defensively up front: dismissal previously had no way back
    short of editing the database by hand, and a real batch-dismiss by
    the wrong set of indices (matching against a stale, previously-seen
    listing rather than a fresh one) needed an honest way to recover
    without raw fingerprint-JSON surgery.

    Returns True if a matching row was found and updated, False
    otherwise (unknown fingerprint, or a DB error) — the same contract
    as dismiss_flagged_combination().

    Sets review_status back to NULL rather than some other sentinel —
    NULL is what a combination's review_status already is before it's
    ever dismissed for the first time, so undismissing genuinely
    restores the exact prior state, not a new, third state.
    """
    try:
        with closing(_connect(ADVERSARIAL_DB)) as con:
            cursor = con.execute(
                "UPDATE adversarial_combinations SET review_status = NULL WHERE fingerprint = ?",
                (fingerprint,)
            )
            con.commit()
            updated = cursor.rowcount > 0
        return updated
    except Exception as e:
        _LOGGER.warning("Could not undismiss flagged combination %r: %s", fingerprint, e)
        return False
