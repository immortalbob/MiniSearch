"""
Mnemolis Load Testing — Locust
Tests realistic query patterns across all sources under concurrent load.

For a genuine cold-cache run, clear both caches first — neither
"cold cache" nor anything else here happens automatically:
    curl -X POST http://your-mnemolis-host:8888/cache/clear
    curl -X POST http://your-mnemolis-host:8888/cache/routing/clear

Run:
    locust -f tests/locustfile.py --host http://192.168.1.50:8888

Replace 192.168.1.50 with your actual Mnemolis host's real IP or
hostname — not a placeholder. --host silently accepts anything that
looks like a URL, so a leftover example value doesn't fail loudly; it
fails much later as a DNS error ("Temporary failure in name
resolution") on every single request, which doesn't obviously point
back to the --host flag as the cause.

Then open http://localhost:8089 and configure:
    - Users: 10
    - Spawn rate: 2
    - Run for 60 seconds

Run the identical command again immediately afterward, without
clearing anything in between, for the warm-cache comparison — the
second run's populated caches are the point.

p95 targets:
    Single source, cache hit:      < 100ms
    Single source, cache miss:     < 4s
    Fusion (2 sources):            < 8s
    10 concurrent users:           < 10% error rate
"""
from locust import HttpUser, task, between
import random


KIWIX_QUERIES = [
    "how does photosynthesis work",
    "explain docker networking",
    "what is molybdenum",
    "history of the Roman Empire",
    "what are capacitors",
    "how do resistors work",
    "explain the solar system",
    "what is machine learning",
    "how does wifi work",
]

# cache_hit's own dedicated query — deliberately NOT "what is nitrogen"
# (the original value here, and still the first KIWIX_QUERIES entry
# below until this fix). A real, confirmed bug: cache_hit's whole
# purpose is to always be a cache hit after the first run, but sharing
# its literal query with KIWIX_QUERIES meant the much-more-frequent
# kiwix_search task (weight 4, the highest-weighted task in
# MnemolisSingleSourceUser) could draw the identical, not-yet-cached
# key at nearly the same instant on a cold run — both tasks then
# genuinely miss and both pay the full cold-routing cost concurrently,
# since route_with_source()'s check-then-call-then-write sequence has
# no per-key lock or in-flight-request deduplication (the same
# thundering-herd shape already documented for AUTO_QUERIES/
# CONDITIONAL_QUERIES's own small pools, just newly visible on a task
# that was never meant to be exposed to it). Traced directly to this
# collision after the v3.50.4 benchmark run showed cache_hit's cold p99
# at a genuinely surprising 8000ms — a query this task's own name says
# should never miss at all. CACHE_HIT_QUERY must never appear in
# KIWIX_QUERIES, KIWIX_DISAMBIGUATION_QUERIES, or any other pool any
# other task draws from for the same source — see
# TestResultCacheThunderingHerd in tests/test_router.py for the direct
# reproduction and the regression test enforcing this.
CACHE_HIT_QUERY = "what is the boiling point of tungsten"

# Short, single-word-after-stemming queries that trigger the disambiguation
# path (definitional + single ambiguous word + Wikipedia selected) — the
# original KIWIX_QUERIES above are all multi-word and never exercise this
KIWIX_DISAMBIGUATION_QUERIES = [
    "what is a galaxy",
    "what is mercury",
    "what are batteries",
    "what is a server",
]

WEB_QUERIES = [
    "best practices for home network security",
    "how to reset a forgotten wifi router password",
    "current mortgage interest rates explained",
    "how to make sourdough bread starter from scratch",
    "docker compose syntax",
]

# Semantic-rephrase pool — measures the v3.53.0 semantic routing cache.
# Each entry is a TOPIC with several colloquial rephrasings of the same
# underlying question; a task picks a topic, then a random phrasing of
# it. On a cold run against a deployment with EMBEDDING_MODEL set, the
# first phrasing of each topic pays the full LLM routing call and every
# later phrasing should land an intent_semantic reuse instead — so this
# task's cold p50/p90 vs [auto]'s is the semantic cache's measured win,
# and against a deployment WITHOUT embeddings it degrades to a plain
# extra [auto]-shaped load (harmless, just not informative). Phrasings
# deliberately include the apostrophe-less colloquial forms
# ("whats the story with") that the v3.53.1 term-building fix covers,
# so a regression in EITHER feature shows up here.
SEMANTIC_REPHRASE_TOPICS = [
    [
        "what is molybdenum",
        "whats the story with molybdenum",
        "tell me about molybdenum",
        "explain molybdenum to me",
    ],
    [
        "will it rain this weekend",
        "is rain expected this weekend",
        "any rain coming this weekend",
        "whats the chance of rain this weekend",
    ],
    [
        "are all my services up",
        "is everything up right now",
        "any of my services down",
        "are my services healthy",
    ],
    [
        "latest news headlines",
        "whats in the news today",
        "any big news today",
        "catch me up on the news",
    ],
    [
        "what is quantum computing",
        "whats the deal with quantum computing",
        "explain quantum computing",
        "quantum computing overview",
    ],
    [
        "is the front door locked",
        "did I lock the front door",
        "front door lock status",
        "check whether the front door is locked",
    ],
]


AUTO_QUERIES = [
    "what is the weather this weekend",
    "are all my services up",
    "latest news headlines",
    "what is nitrogen",
    "is anything down on my network",
    "do I need an umbrella tomorrow",
    # Widened from the original 6-entry pool — both the v3.44.0 and
    # v3.50.2 benchmark runs found the same thundering-herd cache-write
    # collision on this pool under 20 concurrent users (auto's p99 hit
    # a full 10 seconds in the v3.50.2 cold run). A small fixed pool
    # means multiple concurrent users can pick the SAME never-yet-cached
    # query before the first one to resolve it has written the cache
    # entry, so several pay the full LLM routing cost concurrently even
    # on a nominally "warm" run. Doubling the pool size doesn't eliminate
    # the collision possibility, but meaningfully dilutes the odds with
    # 20 concurrent users randomly sampling from it.
    "what's the temperature outside right now",
    "any new headlines today",
    "is my network up",
    "are my services running",
    "will it rain this week",
    "do I have any news today",
    # Widened again, 12 -> 24, after the v3.50.7 benchmark run still
    # showed a real, partial collision tail at this size (auto cold
    # p98/p99 still landing multi-second-adjacent). The actual reason
    # the first widening only partially helped, worked out directly
    # rather than assumed: with 20 concurrent Locust users and a pool
    # this small, the math is closer to the classic birthday-paradox
    # problem than a simple "more options dilutes collisions" intuition
    # suggests — the EXPECTED NUMBER of pool entries hit by 2+ of the 20
    # users actually peaks somewhere around pool_size ~= 10-12 (not at
    # the smallest size), then only declines meaningfully once the pool
    # grows well past the concurrent-user count. 24 entries sits past
    # that peak, where widening further genuinely reduces absolute
    # collisions rather than mostly just redistributing them across more
    # entries. Every new entry verified directly against detect_intent()
    # before being added, the same discipline the original widening used.
    "will it rain later today",
    "is everything up right now",
    "give me the latest headlines",
    "high temp for tomorrow",
    "is anything offline right now",
    "whats in my feeds today",
    "going to be hot this weekend",
    "check services status now",
    "low temp tonight",
    "is it down right now",
    "any new headlines for me",
    "is the network up",
]

FUSION_QUERIES = [
    ("what is the weather and are my services up", ["forecast", "uptime"]),
    ("latest news and weather forecast", ["news", "forecast"]),
    ("what is the weather and latest headlines", ["forecast", "news"]),
    ("check my services and what is the forecast", ["uptime", "forecast"]),
]

HA_QUERIES = [
    "house status summary",
    "are the doors locked",
    "battery status",
    "what lights are on in the living room",
    "indoor air quality",
    "security status",
    # The exact query shape behind a real, severe bug found and fixed
    # this release cycle: "on" (a bare keyword for "lights on") matched
    # as a substring inside "front", silently filtering out the actual
    # entity being asked about. Included here specifically so a real
    # benchmark run against live HA data can confirm the fix holds
    # under real, concurrent load, not just in the test suite.
    "is the front door locked",
    "is the download finished yet",
]

# Leading "if X, Y" conditional queries — exercises detect_conditional(),
# _interpret_yes_no()'s structured-source verdict path, and
# _frame_conditional_response(). Mix of structured (ha/uptime/forecast,
# gets a real verdict) and open-ended (kiwix, honest abstention) sources,
# since both paths have genuinely different cost profiles — and that
# difference matters more for THIS pool's sizing than it first appeared,
# see the v3.50.9 comment block below.
#
# Widened from the original 4-entry pool for the same reason AUTO_QUERIES
# was widened above — both the v3.44.0 and v3.50.2 benchmark runs found
# the same thundering-herd cache-write collision here under 20 concurrent
# users (_resolve_conditional() caches on the EXTRACTED condition text,
# not the original "if X, Y" string, so each distinct condition has to
# warm independently, and a small pool means concurrent users can collide
# on the same not-yet-cached condition before the first one finishes).
CONDITIONAL_QUERIES = [
    "if the back door is unlocked, let me know",
    "if any services are down, let me know right away",
    "if it is raining, remind me to bring an umbrella",
    "if mercury is in retrograde, I will be careful with communication",
    "if the garage door is open, let me know",
    "if the network is down, tell me right away",
    "if it is going to snow, remind me to grab a coat",
    "if jupiter is in retrograde, I will be extra careful today",
    # Widened again, 8 -> 20 (v3.50.8), after the v3.50.7 benchmark run
    # still showed a real, partial collision tail at this size, using an
    # "expected number of colliding pool entries" model that correctly
    # identified the relationship between pool size and collision count
    # isn't monotonic, but picked a size that still left this pool's
    # actual benchmark behavior WORSE on the next real run (cold p99 hit
    # 9800ms, the single worst sample this endpoint has ever produced) —
    # see the v3.50.9 block below for what that model was missing.
    "if the front porch light is off, let me know",
    "if the freezer is too warm, let me know right away",
    "if it is going to be windy tomorrow, remind me to secure the patio furniture",
    "if saturn is in retrograde, I will reconsider my plans",
    "if the side gate is unlocked, let me know",
    "if the internet connection drops, tell me right away",
    "if there is a frost warning tonight, remind me to cover the plants",
    "if mars is in retrograde, I will be careful with decisions",
    "if the basement sensor detects water, let me know immediately",
    "if any cameras go offline, let me know",
    "if the humidity gets too high, remind me to run the dehumidifier",
    "if venus is in retrograde, I will think twice about new purchases",
    # Widened again, 20 -> 40 (v3.50.9), this time correcting TWO real
    # mistakes the v3.50.8 sizing made, not just picking a bigger number:
    #
    # 1. Wrong metric. "Expected number of colliding pool entries" isn't
    #    what predicts the benchmark's actual tail — "fraction of the 20
    #    concurrent users whose first pick collides with someone else's"
    #    is. That fraction declines monotonically with pool size (no
    #    peak to worry about), but it declines SLOWLY: 4 entries -> 99.6%
    #    of users collide, 12 -> 80.9%, 20 -> 62.3%. The v3.50.8 sizing
    #    (8->20) only moved this from 92.1% to 62.3% — a real
    #    improvement on paper, small enough to be invisible against
    #    ordinary single-run noise at this benchmark's sample sizes
    #    (35-49 requests/run for this endpoint).
    #
    # 2. Wrong assumption about collision cost. AUTO_QUERIES's widening
    #    worked great at a SIMILAR nominal collision fraction (55.5% at
    #    24 entries) because most of ITS collisions land on a cheap,
    #    structured source (forecast/news/uptime) — confirmed directly:
    #    only 2 of 24 AUTO_QUERIES entries fall through to kiwix's
    #    expensive LLM book-selection path. The original 20-entry
    #    CONDITIONAL_QUERIES pool was the opposite: 17 of 20 conditions
    #    (85%) fell through to kiwix — confirmed by running every one
    #    through detect_intent() directly, not assumed — meaning a
    #    collision here costs far more per occurrence than a collision
    #    on auto's pool, even at a similar collision RATE. Lower
    #    collision rate alone wasn't going to be enough.
    #
    # This pass fixes both: widened to 40 entries (38.2% collision
    # fraction — chosen LOWER than auto's 55.5%, deliberately, given the
    # higher per-collision cost), AND the 20 new entries below were
    # specifically written to hit ha/uptime/forecast/changes keywords in
    # app/router.py's real INTENT_MAP rather than falling through to
    # kiwix — verified directly against detect_intent() before being
    # added, not assumed from "sounds like it should." Brought the
    # pool's overall kiwix-fallback ratio from 85% down to 42%. Every
    # entry (old and new) still verified against detect_conditional() to
    # confirm a real condition/consequence with an empty remainder.
    "if any motion is detected outside, let me know",
    "if the lights status changes, let me know",
    "if any outages today, let me know",
    "if the battery levels are low, let me know",
    "if the security status changes, let me know",
    "if it is going to be cold tomorrow, remind me to dress warm",
    "if the indoor air quality drops, let me know",
    "if the power consumption spikes, let me know",
    "if the server status changes, let me know",
    "if is it down right now, let me know",
    "if the network status changes, let me know",
    "if any new outages appear, let me know",
    "if the house status changes, let me know",
    "if the outdoor conditions get bad, remind me to close the windows",
    "if are the doors locked, let me know",
    "if low battery is detected, let me know",
    "if any new headlines appear, let me know",
    "if the door locked status changes, let me know",
    "if the wind forecast looks bad, remind me to bring a jacket",
    "if the energy usage spikes, let me know",
]

# Conditional queries with a real remainder after the consequence — also
# exercises the remainder extraction/independent-search/merge path added
# alongside detect_conditional(), not just the simple no-remainder case.
# Widened from 2 to 4 entries for the same thundering-herd reason as above.
CONDITIONAL_WITH_REMAINDER_QUERIES = [
    "if any services are down, let me know, and also whats the weather",
    "if the back door is unlocked, let me know, and also check the news",
    "if it is raining, remind me to bring an umbrella, and also is everything up",
    "if the garage door is open, let me know, and also whats happening with bitcoin",
    # Widened again, 4 -> 12 (v3.50.8), after the v3.50.7 benchmark run
    # still showed a real collision tail at this size — but this sizing
    # decision was a real mistake, not just an incomplete fix: the model
    # used at the time ("expected number of colliding pool entries")
    # actually predicted 4->12 would make the absolute collision count
    # WORSE (3.90 -> 6.07 expected colliding entries), and the next real
    # benchmark run confirmed it — cold p99 went from 1300ms to 4200ms,
    # warm p98 from 450ms to 1800ms. See CONDITIONAL_QUERIES's own
    # comment block above and the v3.50.9 block below for the full
    # correction (wrong metric, wrong assumption about collision cost).
    "if the side gate is unlocked, let me know, and also is it going to rain",
    "if the network is down, tell me right away, and also whats the latest news",
    "if mars is in retrograde, I will be careful with decisions, and also check if the doors are locked",
    "if there is a frost warning tonight, remind me to cover the plants, and also whats the forecast for the weekend",
    "if any cameras go offline, let me know, and also what is happening with bitcoin",
    "if the humidity gets too high, remind me to run the dehumidifier, and also check the news",
    "if the freezer is too warm, let me know right away, and also whats the weather tomorrow",
    "if venus is in retrograde, I will think twice about new purchases, and also is everything online",
    # Widened again, 12 -> 30 (v3.50.9), using the corrected metric and
    # deliberately reusing CONDITIONAL_QUERIES's new, structured-source-
    # heavy conditions above rather than writing a third independent set
    # — confirmed earlier in this investigation that sharing a condition
    # across both pools HELPS, not hurts (both tasks cache on the
    # identical extracted condition text, so either one warming it
    # benefits the other). 30 entries gives this pool a 47.5% collision
    # fraction — proportionally similar to CONDITIONAL_QUERIES's own
    # move (62.3% -> 38.2%), scaled down slightly to reflect this pool's
    # lower task weight (1, vs conditional's 2) and therefore lower real
    # request volume per run. Every entry verified directly against
    # detect_conditional() to produce a genuine non-empty remainder.
    "if any motion is detected outside, let me know, and also whats the forecast",
    "if the lights status changes, let me know, and also check the news",
    "if any outages today, let me know, and also is everything up",
    "if the battery levels are low, let me know, and also whats happening with bitcoin",
    "if the security status changes, let me know, and also check the weather",
    "if it is going to be cold tomorrow, remind me to dress warm, and also any news",
    "if the indoor air quality drops, let me know, and also is it raining",
    "if the power consumption spikes, let me know, and also check the headlines",
    "if the server status changes, let me know, and also whats the weather",
    "if is it down right now, let me know, and also check the news",
    "if the network status changes, let me know, and also is it raining",
    "if any new outages appear, let me know, and also whats the forecast",
    "if the house status changes, let me know, and also check the headlines",
    "if the outdoor conditions get bad, remind me to close the windows, and also check the news",
    "if are the doors locked, let me know, and also whats the weather",
    "if low battery is detected, let me know, and also check the headlines",
    "if any new headlines appear, let me know, and also is everything up",
    "if the door locked status changes, let me know, and also whats the forecast",
]

# Discourse-framing queries ("everyone's obsessed with X") — exercises
# _has_discourse_framing()'s routing bias (forcing kiwix into the fusion
# decision) and _strip_discourse_framing()'s search-term cleanup inside
# kiwix.py. Real production queries verified earlier this session.
DISCOURSE_FRAMING_QUERIES = [
    "whats the deal with that whole bitcoin thing everyone is obsessed with",
    "whats the deal with that whole galaxy thing everyones obsessed with right now",
    "whats the deal with that whole black hole thing everyone keeps talking about",
]


class MnemolisSingleSourceUser(HttpUser):
    """Simulates single-source queries — most common usage pattern."""
    wait_time = between(1, 3)

    @task(4)
    def kiwix_search(self):
        """Kiwix encyclopedic queries — highest weight, most common."""
        self.client.post("/search", json={
            "query": random.choice(KIWIX_QUERIES),
            "source": "kiwix"
        }, name="/search [kiwix]")

    @task(2)
    def kiwix_disambiguation(self):
        """Short ambiguous queries — exercises the multi-candidate
        disambiguation + scoring path, not just plain Kiwix lookup."""
        self.client.post("/search", json={
            "query": random.choice(KIWIX_DISAMBIGUATION_QUERIES),
            "source": "kiwix"
        }, name="/search [kiwix_disambiguation]")

    @task(3)
    def auto_routing(self):
        """Auto-routed queries — tests routing intelligence."""
        self.client.post("/search", json={
            "query": random.choice(AUTO_QUERIES),
            "source": "auto"
        }, name="/search [auto]")

    @task(2)
    def semantic_rephrase(self):
        """Rephrasings of shared topics — measures the semantic routing
        cache (see SEMANTIC_REPHRASE_TOPICS' own comment). Kept as its
        own named bucket so its cold-run latency distribution is
        directly comparable against [auto]'s in the results table."""
        topic = random.choice(SEMANTIC_REPHRASE_TOPICS)
        self.client.post("/search", json={
            "query": random.choice(topic),
            "source": "auto"
        }, name="/search [semantic_rephrase]")

    @task(2)
    def conditional(self):
        """Leading 'if X, Y' conditional queries — exercises
        detect_conditional(), the structured-source yes/no verdict path
        (ha/uptime/forecast) and the honest-abstention path (kiwix), and
        _frame_conditional_response(). Only fires under source='auto' —
        conditional detection is skipped entirely for explicit sources."""
        self.client.post("/search", json={
            "query": random.choice(CONDITIONAL_QUERIES),
            "source": "auto"
        }, name="/search [conditional]")

    @task(1)
    def conditional_with_remainder(self):
        """Conditional queries with a real remainder after the
        consequence — exercises the remainder extraction, independent
        search, and merge-back-into-response path, not just the simpler
        no-remainder case."""
        self.client.post("/search", json={
            "query": random.choice(CONDITIONAL_WITH_REMAINDER_QUERIES),
            "source": "auto"
        }, name="/search [conditional_remainder]")

    @task(2)
    def discourse_framing(self):
        """'Everyone's obsessed with X' style queries — exercises
        _has_discourse_framing()'s routing bias (forcing kiwix into the
        fusion decision alongside whatever the LLM already chose) and
        _strip_discourse_framing()'s search-term cleanup. Only fires
        under source='auto', same as conditional detection."""
        self.client.post("/search", json={
            "query": random.choice(DISCOURSE_FRAMING_QUERIES),
            "source": "auto"
        }, name="/search [discourse_framing]")

    @task(3)
    def web_search(self):
        """Web search queries — exercises confidence-aware scoring and,
        for 3+ word queries, multi-query expansion via SearXNG."""
        self.client.post("/search", json={
            "query": random.choice(WEB_QUERIES),
            "source": "web"
        }, name="/search [web]")

    @task(2)
    def forecast(self):
        """Weather forecast queries."""
        self.client.post("/search", json={
            "query": "what is the weather today",
            "source": "forecast"
        }, name="/search [forecast]")

    @task(2)
    def news(self):
        """RSS news queries."""
        self.client.post("/search", json={
            "query": "latest news headlines",
            "source": "news"
        }, name="/search [news]")

    @task(1)
    def uptime(self):
        """Service status queries."""
        self.client.post("/search", json={
            "query": "are all services up",
            "source": "uptime"
        }, name="/search [uptime]")

    @task(1)
    def ha_status(self):
        """Home Assistant entity queries."""
        self.client.post("/search", json={
            "query": random.choice(HA_QUERIES),
            "source": "ha"
        }, name="/search [ha]")

    @task(1)
    def cache_hit(self):
        """Repeated query — should always be a cache hit after first run.

        Uses its own dedicated query (CACHE_HIT_QUERY), never drawn
        from by any other task's pool — see that constant's own
        comment for the real, confirmed collision this fixes."""
        self.client.post("/search", json={
            "query": CACHE_HIT_QUERY,
            "source": "kiwix"
        }, name="/search [cache_hit]")

    @task(1)
    def health_check(self):
        """Health endpoint — lightweight monitoring check."""
        self.client.get("/health", name="/health")


class MnemolisFusionUser(HttpUser):
    """Simulates fusion queries — higher latency per request."""
    wait_time = between(2, 5)

    @task(3)
    def fusion_explicit(self):
        """Explicit fusion with specified sources."""
        query, sources = random.choice(FUSION_QUERIES)
        self.client.post("/search", json={
            "query": query,
            "source": "fusion",
            "fusion_sources": sources
        }, name="/search [fusion_explicit]")

    @task(2)
    def fusion_auto(self):
        """Auto fusion — LLM picks sources."""
        self.client.post("/search", json={
            "query": "what is the weather and are my services up",
            "source": "fusion"
        }, name="/search [fusion_auto]")

    @task(1)
    def fusion_triple(self):
        """Triple source fusion — highest load per request."""
        self.client.post("/search", json={
            "query": "check my services whats the weather and any news headlines",
            "source": "fusion",
            "fusion_sources": ["forecast", "uptime", "news"]
        }, name="/search [fusion_triple]")
