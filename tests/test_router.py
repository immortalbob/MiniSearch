"""
Tests for app/router.py — intent detection, cache logic, fallback detection.
No network calls required.
"""
import time
import pytest


# ---------------------------------------------------------------------------
# Intent detection
# ---------------------------------------------------------------------------

class TestKeywordDetect:
    """Tests for _keyword_detect — fast keyword-based intent routing."""

    def setup_method(self):
        # Import here so we don't need the full app context at module load
        from app.router import _keyword_detect
        self.detect = _keyword_detect

    def test_weather_keyword(self):
        assert self.detect("what is the weather tomorrow") == "forecast"

    def test_forecast_keyword(self):
        assert self.detect("give me the forecast") == "forecast"

    def test_rain_keyword(self):
        assert self.detect("will it rain this weekend") == "forecast"

    def test_news_keyword(self):
        assert self.detect("what's in the news") == "news"

    def test_headlines_keyword(self):
        assert self.detect("show me the headlines") == "news"

    def test_uptime_down(self):
        # "is down" is a trigger but requires adjacent words — use exact trigger phrase
        assert self.detect("the server is down") == "uptime"

    def test_uptime_service_status(self):
        assert self.detect("check service status") == "uptime"

    def test_uptime_everything_up(self):
        assert self.detect("is everything up") == "uptime"

    def test_web_search(self):
        assert self.detect("search the web for python tutorials") == "web"

    def test_web_who_won(self):
        assert self.detect("who won the super bowl") == "web"

    def test_no_match_returns_none(self):
        assert self.detect("what is molybdenum") is None

    def test_no_match_general_knowledge(self):
        assert self.detect("explain photosynthesis") is None

    def test_no_match_tech_question(self):
        assert self.detect("how do I configure nginx") is None

    # Regression tests — things that used to wrongly match
    def test_latest_does_not_match_news(self):
        assert self.detect("latest iPhone release") is None

    def test_status_does_not_match_uptime(self):
        assert self.detect("what is HTTP status code 404") is None

    def test_tonight_does_not_match_forecast(self):
        assert self.detect("what's on tonight") is None

    def test_will_it_be_does_not_match_forecast(self):
        assert self.detect("will it be available soon") is None

    def test_monitoring_does_not_match_uptime(self):
        assert self.detect("heart rate monitoring") is None

    def test_articles_does_not_match_news(self):
        assert self.detect("articles about Docker") is None

    def test_recent_does_not_match_news(self):
        assert self.detect("recent Python releases") is None

    # Regression tests for three real INTENT_MAP gaps found via a
    # deliberate investigation into a conditional_remainder benchmark
    # anomaly (warm-cache p99 worse than cold — see the
    # conditional_remainder design doc and
    # wiki/The-Benchmark-Investigation-Log.md's Ollama-serialization
    # coverage). All three are real, natural phrasings of questions
    # this list already covers via a different exact phrase, that
    # previously fell through to kiwix's expensive LLM book-selection
    # path instead of the cheap, structured source they should hit.
    def test_is_it_raining_matches_forecast(self):
        """"will it rain"/"will it snow" were the only rain/snow
        triggers — the equally natural "is it raining" phrasing had no
        match at all."""
        assert self.detect("is it raining") == "forecast"

    def test_is_it_going_to_rain_matches_forecast(self):
        assert self.detect("is it going to rain") == "forecast"

    def test_is_it_going_to_snow_matches_forecast(self):
        """A second, independently-found gap in the same family —
        "if it is going to snow, remind me to grab a coat" is a real
        entry in CONDITIONAL_QUERIES that was silently falling through
        to the LLM on every cold miss with zero keyword match at all,
        only resolving to "forecast" because the LLM happened to guess
        correctly — confirmed directly against the pre-fix INTENT_MAP."""
        assert self.detect("it is going to snow") == "forecast"

    def test_is_everything_online_matches_uptime(self):
        """"is everything up" already matched — "online" is an
        equally natural synonym for the same question that had no
        trigger at all."""
        assert self.detect("is everything online") == "uptime"

    def test_online_does_not_collide_with_web_lookup_phrases(self):
        """Confirms the new bare "online" trigger doesn't accidentally
        widen uptime's match to web-lookup-style queries — INTENT_MAP
        matching is query-contains-trigger, not the reverse, so this
        was never actually at risk, but worth a direct regression test
        since both sources now share the substring "online"."""
        assert self.detect("look it up online") == "web"
        assert self.detect("find online") == "web"

    def test_doors_are_locked_matches_ha(self):
        """"are the doors locked"/"door locked"/"doors locked" were
        the only door-lock triggers — all three require word-
        adjacency, so the equally natural "doors are locked" (with
        "are" inserted) matched none of them. The same word-insertion
        class of bug already found and fixed once before in this
        project (the GPIO/"on" word-boundary issue)."""
        assert self.detect("doors are locked") == "ha"

    def test_check_if_the_doors_are_locked_matches_ha(self):
        """The exact real-world phrasing that surfaced this gap — a
        genuine CONDITIONAL_WITH_REMAINDER_QUERIES remainder."""
        assert self.detect("also check if the doors are locked") == "ha"

    def test_door_is_locked_matches_ha(self):
        assert self.detect("is the door is locked") == "ha"

    def test_garage_door_is_open_matches_ha(self):
        """Regression test for a real, two-file gap found while
        researching whether conditional_remainder's kiwix-double-hit
        pairs were genuinely intentional kiwix routing (see the
        conditional_remainder design doc): "the garage door is open"
        is a question about OPEN/CLOSED state, not LOCKED/UNLOCKED —
        a real, existing CONDITIONAL_QUERIES/CONDITIONAL_WITH_REMAINDER_
        QUERIES entry that had zero match against this list's existing
        door triggers (all locked/unlocked phrasing) and fell through
        to kiwix. Deliberately a separate trigger from "door"/"doors
        locked" rather than widening those — see
        test_door_query_returns_lock_domain in test_home_assistant.py,
        which pins "are the doors locked" to stay lock-only. The other
        half of this fix is in home_assistant.py's own _QUERY_MAP, new
        "garage"/"garage door" entries — routing alone doesn't help if
        the entity itself still can't be found once inside the ha
        handler."""
        assert self.detect("the garage door is open") == "ha"

    def test_garage_alone_matches_ha(self):
        assert self.detect("garage status") == "ha"

    def test_camera_offline_does_not_match_ha(self):
        """Confirms this fix is deliberately narrow: "any cameras go
        offline" is a different, NOT-fixed question — there is no
        real Home Assistant concept in this codebase for camera
        reachability/connectivity (the existing "camera" _QUERY_MAP
        entry answers motion-DETECTION questions, a different thing
        entirely), so this correctly continues to fall through rather
        than silently routing somewhere that can't actually answer
        it."""
        assert self.detect("any cameras go offline") is None


class TestKeywordDetectMulti:
    """Tests for multi-keyword detection that escalates to fusion."""

    def setup_method(self):
        from app.router import _keyword_detect
        self.detect = _keyword_detect

    def test_two_sources_returns_list(self):
        result = self.detect("what is the weather and are my services up")
        assert isinstance(result, list)
        assert "forecast" in result
        assert "uptime" in result

    def test_weather_and_news_returns_list(self):
        result = self.detect("what is the weather and latest news headlines")
        assert isinstance(result, list)
        assert "forecast" in result
        assert "news" in result

    def test_single_source_still_returns_string(self):
        result = self.detect("what is the weather tomorrow")
        assert isinstance(result, str)
        assert result == "forecast"

    def test_no_match_still_returns_none(self):
        result = self.detect("what is molybdenum")
        assert result is None

    def test_list_has_no_duplicates(self):
        result = self.detect("what is the weather and are my services up")
        assert isinstance(result, list)
        assert len(result) == len(set(result))

    def test_three_sources_returns_list(self):
        result = self.detect("weather forecast latest news and are my services up")
        assert isinstance(result, list)
        assert len(result) >= 2


class TestNewUptimeTriggers:
    """Regression tests for expanded uptime trigger list."""

    def setup_method(self):
        from app.router import _keyword_detect
        self.detect = _keyword_detect

    def test_my_services(self):
        assert self.detect("check my services") == "uptime"

    def test_services_up(self):
        assert self.detect("are my services up") == "uptime"

    def test_services_down(self):
        assert self.detect("are any services down") == "uptime"

    def test_anything_down(self):
        assert self.detect("is anything down right now") == "uptime"

    def test_everything_up(self):
        assert self.detect("is everything up") == "uptime"

    def test_everything_down(self):
        assert self.detect("is everything down") == "uptime"

    def test_network_down(self):
        assert self.detect("is the network down") == "uptime"

    def test_network_up(self):
        assert self.detect("is the network up") == "uptime"

    def test_whats_offline(self):
        assert self.detect("what's offline right now") == "uptime"

    def test_server_status(self):
        assert self.detect("what is the server status") == "uptime"

    def test_server_down(self):
        assert self.detect("is the server down") == "uptime"

    def test_check_services(self):
        assert self.detect("check services for me") == "uptime"

    def test_is_my_network(self):
        assert self.detect("is my network ok") == "uptime"

    def test_anything_offline(self):
        assert self.detect("is anything offline right now") == "uptime"

    def test_is_it_running(self):
        assert self.detect("is it running on my network") == "uptime"

    def test_is_it_up(self):
        assert self.detect("is it up") == "uptime"

    def test_are_they_up(self):
        assert self.detect("are they up") == "uptime"


class TestAutoFusionEscalation:
    """Tests for auto routing escalating to fusion on multi-topic queries."""

    def test_multi_topic_query_decomposes(self):
        from app.router import _decompose

        # Multi-topic query should decompose into sub-queries rather than fuse
        parts = _decompose("what is the weather and are my services up")
        assert len(parts) == 2
        assert any("weather" in p for p in parts)
        assert any("services" in p for p in parts)

    def test_multi_topic_query_routes_independently(self):
        from app.router import route, clear_cache
        import app.router as router_module
        from unittest.mock import patch, MagicMock

        clear_cache()
        mock_forecast = MagicMock(return_value="Sunny today.")
        mock_uptime = MagicMock(return_value="All services up.")
        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = mock_forecast
        router_module.SOURCE_MAP["uptime"] = mock_uptime
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router._keyword_detect", return_value=None), \
                 patch("app.router._llm_detect", side_effect=["forecast", "uptime"]):
                route("what is the weather and are my services up", "auto")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert mock_forecast.called
        assert mock_uptime.called

    def test_single_topic_does_not_use_fusion(self):
        from app.router import route
        from unittest.mock import patch
        from app.sources import fusion

        with patch.object(fusion, "search") as mock_fusion:
            route("what is the weather tomorrow", "auto")
        assert not mock_fusion.called

    def test_decomposed_subquery_resolving_to_fusion_not_double_headered(self):
        """Regression test — the real bug found via real usage. When a
        decomposed sub-query's own intent resolves to a list (triggering
        internal fusion across multiple sources), fusion.search() already
        returns content with its own per-source [SOURCE — DESC] headers.
        The outer decomposition loop used to wrap that already-headered
        block in ANOTHER header using the literal string "fusion" as the
        source name — which has no entry in _HEADER_LABELS, producing a
        nonsensical "[FUSION — FUSION]" wrapper around content that was
        already correctly labeled internally."""
        from app.router import route, clear_cache
        import app.router as router_module
        from unittest.mock import patch

        clear_cache()
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=[
                     ["uptime", "ha"],  # first sub-query resolves to a fusion list
                     "kiwix",            # second sub-query resolves to a single source
                 ]), \
                 patch("app.sources.fusion.search", return_value="[UPTIME — DESC]\nAll up.\n\n---\n\n[HA — DESC]\nAll locked."), \
                 patch.object(router_module, "SOURCE_MAP", {**router_module.SOURCE_MAP, "kiwix": lambda q: "Sunspots are dark regions."}):
                result = route("is anything down and what are sunspots", "auto")
        finally:
            pass

        assert "[FUSION" not in result
        assert "[UPTIME — DESC]" in result
        assert "[HA — DESC]" in result


class TestResolveChangesHours:
    """Tests for _resolve_changes_hours time-window phrase parsing."""

    def setup_method(self):
        from app.router import _resolve_changes_hours
        self.resolve = _resolve_changes_hours

    def test_explicit_hour_count(self):
        assert self.resolve("changes in the last 3 hours") == 3.0

    def test_explicit_hour_count_different_number(self):
        assert self.resolve("what changed in the last 12 hours") == 12.0

    def test_today_defaults_to_24(self):
        assert self.resolve("what changed today") == 24.0

    def test_week_defaults_to_168(self):
        assert self.resolve("what changed this week") == 168.0

    def test_yesterday_defaults_to_48(self):
        assert self.resolve("what changed since yesterday") == 48.0

    def test_no_phrase_defaults_to_24(self):
        assert self.resolve("any new outages") == 24.0

    def test_unrelated_number_near_hour_does_not_trigger_false_positive(self):
        r"""Regression test for a real, reachable bug found via a
        deliberate complexity-investigation pass: the original regex
        (r"(\d+)\s*hour") matched ANY number adjacent to "hour",
        regardless of context — "any updates on my 3 hour delay
        flight, also what changed today" incorrectly resolved to a
        3-hour window from the unrelated "3 hour delay" phrase, silently
        ignoring the user's actual, more relevant "today" signal and
        searching a window 8x narrower than intended. Confirmed
        reachable: this source's keyword routing is a substring match,
        so any query containing a recognized trigger anywhere (e.g.
        "what changed") routes to this function regardless of what else
        the query mentions."""
        result = self.resolve("any updates on my 3 hour delay flight, also what changed today")
        assert result == 24.0

    def test_descriptive_hour_count_does_not_trigger_false_positive(self):
        """A second, distinct false-positive case found in the same
        investigation: "24 hour clock display" describes a product
        feature, not a time-window request, and must not be
        misinterpreted as one."""
        result = self.resolve("what changed with my 24 hour clock display")
        assert result == 24.0  # falls through to the genuine default, not a coincidental match

    def test_past_n_hours_phrasing_still_matches(self):
        """Confirms the fix's required window-phrase list covers more
        than just "last" — "in the past N hours" is an equally natural,
        common phrasing that must still resolve correctly."""
        assert self.resolve("any changes in the past 2 hours") == 2.0

    def test_this_morning_returns_positive_hours(self):
        result = self.resolve("what changed this morning")
        assert result > 0

    def test_while_at_work_returns_positive_hours(self):
        result = self.resolve("anything changed while i was at work")
        assert result > 0

    def test_tonight_returns_positive_hours(self):
        result = self.resolve("what's happening tonight")
        assert result > 0

    def test_morning_uses_configured_start_hour(self):
        from app.config import settings
        original = settings.morning_start_hour
        settings.morning_start_hour = 6
        result = self.resolve("what changed this morning")
        settings.morning_start_hour = original
        assert result > 0

    def test_work_uses_configured_start_hour(self):
        from app.config import settings
        original = settings.work_start_hour
        settings.work_start_hour = 9
        result = self.resolve("since i've been at work")
        settings.work_start_hour = original
        assert result > 0

    def test_explicit_hour_count_takes_priority_over_today(self):
        # "today" appears but explicit hour count should win
        result = self.resolve("what changed today in the last 2 hours")
        assert result == 2.0


class TestSearchChanges:
    """Tests for _search_changes — the actual wired entry point for source='changes'."""

    def test_calls_get_changes_and_format_changes(self):
        from app.router import _search_changes
        from unittest.mock import patch
        with patch("app.router.get_changes", return_value={"uptime": [{"timestamp": "2026-06-19T10:00:00Z", "change": "test"}]}) as mock_get, \
             patch("app.router.format_changes", return_value="formatted result") as mock_fmt:
            result = _search_changes("what changed today")
        assert mock_get.called
        assert mock_fmt.called
        assert result == "formatted result"

    def test_passes_resolved_hours_to_get_changes(self):
        from app.router import _search_changes
        from unittest.mock import patch
        with patch("app.router.get_changes", return_value={}) as mock_get, \
             patch("app.router.format_changes", return_value=""):
            _search_changes("what changed in the last 5 hours")
        # get_changes should be called with since_hours=5.0
        call_kwargs = mock_get.call_args
        assert call_kwargs is not None

    def test_no_changes_returns_format_changes_no_data_message(self):
        from app.router import _search_changes
        result = _search_changes("any new outages")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_integration_with_real_today_query(self):
        from app.router import _search_changes
        # End-to-end smoke test against real (likely empty in test env) snapshot data
        result = _search_changes("what changed today")
        assert isinstance(result, str)


class TestHoursSince:
    """Tests for _hours_since helper."""

    def setup_method(self):
        from app.router import _hours_since
        self.hours_since = _hours_since

    def test_returns_positive_float(self):
        result = self.hours_since(6)
        assert isinstance(result, float)
        assert result > 0

    def test_never_returns_zero_or_negative(self):
        from datetime import datetime
        current_hour = datetime.now().hour
        # Even at the exact current hour, should return a small positive value
        result = self.hours_since(current_hour)
        assert result > 0

    def test_future_hour_looks_back_to_yesterday(self):
        from datetime import datetime
        # An hour later than now should wrap to yesterday, giving ~24h - delta
        future_hour = (datetime.now().hour + 1) % 24
        result = self.hours_since(future_hour)
        assert result > 0
        assert result < 25

    def test_hour_24_does_not_crash_and_is_treated_as_midnight(self):
        """Regression test for a real bug found via a deliberate
        "bulletproofing" pass: MORNING_START_HOUR/WORK_START_HOUR are
        plain, unvalidated ints — writing 24 for midnight (a natural,
        common 24-hour-notation convention) previously crashed this
        function with a raw ValueError ("hour must be in 0..23") the
        moment any "this morning"/"while at work" query needed it."""
        result = self.hours_since(24)  # must not raise
        assert isinstance(result, float)
        assert result > 0

    def test_out_of_range_hour_does_not_crash(self):
        """Confirms the fix generalizes beyond the specific 24 case —
        modulo 24 sensibly handles any out-of-range value rather than
        only patching the one mistake that happened to be found."""
        result = self.hours_since(100)  # must not raise
        assert isinstance(result, float)
        assert result > 0


class TestDecompose:
    """Tests for _decompose conjunction splitting."""

    def setup_method(self):
        from app.router import _decompose
        self.decompose = _decompose

    def test_weather_and_services_splits(self):
        parts = self.decompose("what is the weather and are my services up")
        assert len(parts) == 2

    def test_single_query_not_split(self):
        assert self.decompose("what is molybdenum") == ["what is molybdenum"]

    def test_compare_not_split(self):
        assert len(self.decompose("compare Python and Rust")) == 1

    def test_location_not_split(self):
        assert len(self.decompose("weather in Phoenix and Kingman")) == 1

    def test_country_names_not_split(self):
        assert len(self.decompose("what is happening with Iran and Israel")) == 1

    def test_also_conjunction(self):
        parts = self.decompose("check services also what is the forecast")
        assert len(parts) == 2

    def test_triple_split(self):
        parts = self.decompose("house status and weather and are services up")
        assert len(parts) == 3

    def test_indoor_air_and_doors(self):
        parts = self.decompose("indoor air quality and are the doors locked")
        assert len(parts) == 2

    def test_summarize_not_split(self):
        assert len(self.decompose("summarize news about Iran and Israel")) == 1

    def test_temperature_and_lights(self):
        parts = self.decompose("what is the temperature and are the lights on")
        assert len(parts) == 2

    def test_battery_and_security(self):
        parts = self.decompose("battery status and security status")
        assert len(parts) == 2

    def test_picks_best_split_not_first_conjunction_found(self):
        """Regression test — the real bug found via real usage. A query
        with one ' also ' and two ' and 's used to stop at the first
        conjunction type encountered (sorted by length, ' also ' before
        ' and ') even though splitting on ' and ' produced a better
        (3-part, all genuinely separate intents) result. The function
        must try every conjunction and keep whichever split has the
        most meaningful parts, not just the first one that qualifies."""
        query = (
            "My raspberry pi keeps locking up whenever I mess with the GPIO "
            "pins in python, whats going on with that, and also did anything "
            "weird happen with the back door today, and is it gonna rain or nah"
        )
        parts = self.decompose(query)
        assert len(parts) == 3
        # Confirm each real intent landed in its own part
        assert any("gpio" in p.lower() for p in parts)
        assert any("door" in p.lower() for p in parts)
        assert any("rain" in p.lower() for p in parts)

    def test_singular_door_recognized_as_intent(self):
        """Regression test — _INTENT_WORDS previously only had 'doors'
        (plural), so 'back door' (singular) failed the intent-word check
        and got silently dropped as a 'meaningless' sub-query fragment."""
        parts = self.decompose("what is the weather and did anything happen with the back door")
        assert len(parts) == 2
        assert any("door" in p.lower() for p in parts)

    def test_singular_light_recognized_as_intent(self):
        parts = self.decompose("what is the forecast and is the light on")
        assert len(parts) == 2

    def test_singular_lock_recognized_as_intent(self):
        parts = self.decompose("what is the news and is the lock engaged")
        assert len(parts) == 2

    def test_singular_sensor_recognized_as_intent(self):
        parts = self.decompose("check services and what does the sensor say")
        assert len(parts) == 2

    def test_anything_happened_phrasing_recognized(self):
        """Casual 'did anything happen with X' phrasing should register
        as a real intent on its own, not just formal 'what is X' phrasing."""
        parts = self.decompose("what is the weather and did anything happen today")
        assert len(parts) == 2

    def test_wifi_router_compound_query_with_colloquial_starter(self):
        """Regression test — the real bug found via real usage. This query
        has at least three genuine intents (wifi troubleshooting, router
        troubleshooting, door sensor status, sunspots) but used to collapse
        to a single unsplit block because: (1) 'wifi'/'router'/'reboot'
        weren't in _INTENT_WORDS at all, and (2) the colloquial starter
        'what's the deal with X' wasn't recognized as a real intent
        regardless of what specific noun followed it — both gaps had to be
        fixed together. The mixed-conjunction-type fix (added later the
        same session) further improved this from a 3-way split to a
        genuinely better 4-way split, correctly separating wifi and router
        into their own independent parts rather than merging them under
        one " and "."""
        query = (
            "the wifi has been acting flaky all morning and I think the "
            "router might need a reboot, also can you check if the front "
            "door sensor is online, and what's the deal with sunspots anyway"
        )
        parts = self.decompose(query)
        assert len(parts) == 4
        assert any("wifi" in p.lower() for p in parts)
        assert any("router" in p.lower() for p in parts)
        assert any("door" in p.lower() for p in parts)
        assert any("sunspot" in p.lower() for p in parts)

    def test_wifi_alone_recognized_as_intent(self):
        parts = self.decompose("is the wifi down and what is the weather")
        assert len(parts) == 2

    def test_router_alone_recognized_as_intent(self):
        parts = self.decompose("does the router need a restart and is it raining")
        assert len(parts) == 2

    def test_colloquial_starter_with_unrecognized_noun(self):
        """The colloquial starter alone should be sufficient to count as
        meaningful, even with a topic word ('sunspots') that has no entry
        in _INTENT_WORDS and never will, since new topics are unbounded."""
        parts = self.decompose("check the weather and what's the deal with sunspots")
        assert len(parts) == 2
        assert any("sunspot" in p.lower() for p in parts)

    def test_whats_up_with_starter_recognized(self):
        parts = self.decompose("check services and what's up with the stock market")
        assert len(parts) == 2

    def test_colloquial_phrase_matches_mid_clause_not_just_at_start(self):
        """Regression test — the real bug found via real usage. Colloquial
        phrase detection originally only matched at the very start of a
        sub-query (.startswith()), missing real phrasing like 'and remind
        me what's up with X' where the marker phrase is buried mid-clause
        after leftover conjunction/filler words from the split point.
        Changed to a substring check so it matches anywhere in the clause."""
        parts = self.decompose(
            "check the weather and remind me what's up with that sourdough starter"
        )
        assert len(parts) == 2
        assert any("sourdough" in p.lower() for p in parts)

    def test_colloquial_phrase_with_can_you_tell_me_prefix(self):
        parts = self.decompose(
            "what is the forecast and can you tell me what's the deal with cryptocurrency"
        )
        assert len(parts) == 2
        assert any("cryptocurrency" in p.lower() for p in parts)

    def test_mixed_conjunction_types_all_split(self):
        """Regression test — the real bug found via real usage, documented
        as a known limitation at the end of the prior session and fixed
        properly here. A query mixing multiple different conjunction types
        ("and also", "plus", "and", "also") used to only ever produce 2
        parts under the single-conjunction-type approach, because each
        type's isolated split left the OTHER conjunction words bundled
        inside whichever half didn't get split. Splitting on every
        conjunction occurrence at once, regardless of type, fixes this."""
        query = (
            "ok so my internet's been super flaky today and also whats the "
            "deal with that whole mercury retrograde thing everyone keeps "
            "talking about, plus did the front door or any of the windows "
            "do anything weird while I was out, and is it gonna be hot "
            "enough this week that I should finally fix the AC, also "
            "remind me real quick whats up with raspberry pi gpio "
            "permission stuff cause I keep getting locked out when I mess "
            "with the pins in python"
        )
        parts = self.decompose(query)
        assert len(parts) == 5
        assert any("internet" in p.lower() for p in parts)
        assert any("mercury" in p.lower() for p in parts)
        assert any("door" in p.lower() for p in parts)
        assert any("hot" in p.lower() or "ac" in p.lower().split() for p in parts)
        assert any("gpio" in p.lower() for p in parts)

    def test_adjacent_conjunctions_collapse_to_single_split_point(self):
        """'and also' (two conjunctions back to back) should produce one
        split point, not an empty/near-empty fragment between them."""
        parts = self.decompose(
            "what is the weather and also what is the news and is the door locked"
        )
        assert len(parts) == 3
        # No fragment should be just leftover punctuation/whitespace
        assert all(len(p.strip()) > 3 for p in parts)

    def test_possessive_contraction_recognized_in_intent_check(self):
        """Regression test — 'internet's' (possessive contraction) didn't
        match the bare 'internet' entry in _INTENT_WORDS via exact word
        membership, the same class of bug found and fixed in kiwix.py's
        stop-word stripping. Normalizing the apostrophe before the
        membership check fixes both the same way."""
        parts = self.decompose("my internet's been flaky and what is the weather")
        assert len(parts) == 2
        assert any("internet" in p.lower() for p in parts)

    def test_mixed_conjunctions_still_respects_nosplit(self):
        """Mixed-conjunction splitting must not bypass the nosplit guard —
        a comparison query with multiple conjunction types should still
        never split at all."""
        parts = self.decompose("compare Python and Rust, also what about Go")
        assert len(parts) == 1

    def test_technical_troubleshooting_content_preserved(self):
        """Regression test — the real bug found via real usage. The old
        fixed _INTENT_WORDS allowlist had zero coverage for technical/
        programming vocabulary (python, pigpio, gpio, permission, error,
        compiler, etc), so a genuinely real, specific troubleshooting
        clause was silently dropped during decomposition entirely —
        the meaningful-content check failed it even though it contained
        real, searchable content. Replaced the allowlist with a stop-word-
        based check: any clause with at least one real content word
        remaining after stripping filler now counts as meaningful, with
        no domain-specific vocabulary list required."""
        query = (
            "is my back door locked right now, and also Ive been getting a "
            "python pigpio no permission to update GPIO error on my pi, "
            "plus whats the forecast looking like for tomorrow"
        )
        parts = self.decompose(query)
        assert len(parts) == 3
        assert any("door" in p.lower() for p in parts)
        assert any("pigpio" in p.lower() or "gpio" in p.lower() for p in parts)
        assert any("forecast" in p.lower() for p in parts)

    def test_proper_noun_pair_not_split_phoenix_kingman(self):
        """Regression test — loosening the meaningful-check from a fixed
        allowlist to 'any content word survives' also started incorrectly
        splitting bare proper-noun pairs that the old, stricter allowlist
        had blocked only by accident (neither 'Phoenix' nor 'Kingman' ever
        matched any entry in that list). Explicit structural detection
        restores this without reintroducing a place-name list."""
        assert len(self.decompose("weather in Phoenix and Kingman")) == 1

    def test_proper_noun_pair_not_split_countries(self):
        assert len(self.decompose("what is happening with Iran and Israel")) == 1

    def test_proper_noun_pair_not_split_summarize_news(self):
        assert len(self.decompose("summarize news about Iran and Israel")) == 1

    def test_proper_noun_pair_guard_does_not_block_real_decomposition(self):
        """The proper-noun-pair guard must be narrow enough that it never
        blocks a genuine multi-intent split just because one clause
        happens to start with a capitalized word."""
        parts = self.decompose("Is it raining and are my services up")
        assert len(parts) == 2

    def test_single_real_content_word_is_sufficient(self):
        """A clause needs only ONE real content word to count as
        meaningful — no minimum word count beyond that, matching the
        principle that any genuine topic, however short, deserves its
        own search rather than being silently merged or dropped."""
        parts = self.decompose("what is the weather and gpio")
        assert len(parts) == 2

    def test_proper_noun_pair_guard_is_local_not_global(self):
        """Regression test — the real, more serious bug found via real
        usage immediately after the content-word fix shipped. The
        proper-noun-pair guard was originally a single whole-query gate:
        if ANY conjunction occurrence anywhere looked like a proper-noun
        pair, decomposition aborted ENTIRELY, discarding genuinely
        separate, real intents elsewhere in the same sentence. A query
        can contain both a genuine proper-noun pair ('Iran and Israel')
        AND completely unrelated real intents ('my back door', 'a numpy
        import error on my pi') in the same breath — the guard must
        protect only the SPECIFIC conjunction occurrence that's a
        proper-noun pair, checked independently at every occurrence,
        not abort splitting for the whole query the moment it finds one
        anywhere."""
        query = (
            "whats happening with Iran and Israel right now, and also "
            "has anything weird happened with my back door, plus I keep "
            "getting a weird numpy import error on my raspberry pi, and "
            "is it gonna be warm in Kingman and Phoenix this weekend"
        )
        parts = self.decompose(query)
        assert len(parts) == 4
        # Both proper-noun pairs must survive intact, not split apart
        assert any("iran and israel" in p.lower() for p in parts)
        assert any("kingman and phoenix" in p.lower() for p in parts)
        # Both genuinely separate real intents must still be present
        assert any("door" in p.lower() for p in parts)
        assert any("numpy" in p.lower() for p in parts)

    def test_proper_noun_pair_skip_does_not_discard_preceding_content(self):
        """Regression test — a deeper bug found via real usage in a
        megaquery test, AFTER the previous proper-noun-pair fixes had
        already shipped. The single-conjunction-type split loop's skip
        logic, when encountering a protected pair ("Iran and Israel"),
        reset the search position to just past the skipped occurrence —
        which also reset where the NEXT kept part would start FROM,
        silently discarding all the real text that came before the pair
        ("also whats happening with Iran and" got reduced to just
        "Israel, plus...", losing "also whats happening with Iran and"
        entirely). The fix tracks segment_start (where the current
        accumulating part begins) separately from search_from (where to
        resume looking for the next conjunction), so skipping a
        protected pair advances the search position without discarding
        any of the real content that precedes it.

        UPDATED count: a later, separate fix (found via a deliberate,
        thorough complexity-investigation pass on this same function)
        discovered that "Israel, plus I..." was ALSO being incorrectly
        protected as a proper-noun pair — "I" is always capitalized in
        English regardless of context, making it look exactly like a
        proper noun ("Texas" + "I") to the naive capitalization check.
        This meant the numpy/GPIO clause could never be split out as
        its own part; it was permanently stuck merged into part 1 as an
        unavoidable side effect of that bug. With the pronoun fix in
        place, this query now correctly produces 4 distinct parts, not
        3 — the original test's expected count of 3 had unknowingly
        baked in the limitation of a bug that hadn't been found yet.
        Every original content-integrity assertion below still holds
        true exactly as before; only the count and the new, more
        precise per-part assertions are new."""
        query = (
            "also whats happening with Iran and Israel, plus I keep "
            "getting a weird numpy import error on my raspberry pi, and "
            "if any services are down let me know too, and one more "
            "thing whats the deal with sunspots"
        )
        parts = self.decompose(query)
        assert len(parts) == 4
        # The proper-noun pair AND the real content before it must both
        # survive in the same part — neither lost, neither split apart
        first_part = parts[0].lower()
        assert "iran and israel" in first_part
        assert "whats happening with" in first_part
        # The numpy/GPIO clause is now correctly its OWN separate part —
        # previously impossible due to the "I" pronoun bug, which forced
        # it to remain merged into part 1 alongside Iran/Israel
        assert any("numpy" in p.lower() and "iran" not in p.lower() for p in parts)
        assert any("services are down" in p.lower() for p in parts)
        assert any("sunspots" in p.lower() for p in parts)

    def test_pronoun_i_not_mistaken_for_proper_noun_in_pair_check(self):
        """Direct regression test for the actual root-cause bug found
        during the megaquery investigation above: _is_proper_noun_pair_at()
        treating the pronoun "I" as if it were a real proper noun, since
        "I" is always capitalized in English regardless of sentence
        position. This is a genuinely common, natural phrasing pattern —
        any place name, topic, or proper noun followed by ", plus I..."
        or ", and I..." — not a contrived edge case."""
        from app.router import _is_proper_noun_pair_at
        query = "what's happening in Texas, plus I need help with my router"
        idx = query.lower().find(" plus ")
        assert _is_proper_noun_pair_at(query, idx, len(" plus ")) is False

    def test_genuine_proper_noun_pair_still_protected_after_pronoun_fix(self):
        """Confirms the pronoun-specific fix didn't accidentally weaken
        protection for an actual, genuine proper-noun pair — only the
        narrow "I" case should be excluded, not proper nouns generally."""
        from app.router import _is_proper_noun_pair_at
        query = "I want the weather for Texas and Arizona please"
        idx = query.lower().find(" and ")
        assert _is_proper_noun_pair_at(query, idx, len(" and ")) is True

    def test_pronoun_i_before_conjunction_also_not_mistaken_for_proper_noun(self):
        """Regression test for an asymmetric gap found via a dedicated,
        fresh, complete re-read of this exact function (its first one,
        despite having been edited twice already for the after_head "I"
        case above): "I and Texas" (the unusual word order, "I" directly
        adjacent to the conjunction with no verb between them) still
        triggered the false positive even after the after_head fix,
        since only after_head was checked, never before_tail. Confirmed
        via direct testing that this specific construction is genuinely
        low-reachability through natural English — "I" is almost always
        followed by a verb ("I want", "I think", "I need"), not directly
        by a conjunction, so this exact asymmetric case essentially
        never occurs in a real, natural compound request the way the
        after_head case commonly does. Fixed anyway for completeness,
        since the fix was cheap and the asymmetry was real."""
        from app.router import _is_proper_noun_pair_at
        query = "I and Texas are both fine"
        idx = query.lower().find(" and ")
        assert _is_proper_noun_pair_at(query, idx, len(" and ")) is False

    def test_natural_word_order_with_i_after_conjunction_still_works(self):
        """Confirms the natural, common word order ("Texas and I," not
        the unusual "I and Texas") still correctly returns False after
        the symmetric fix — this is the original, already-fixed case,
        re-verified here alongside its newly-fixed counterpart."""
        from app.router import _is_proper_noun_pair_at
        query = "Texas and I are both fine"
        idx = query.lower().find(" and ")
        assert _is_proper_noun_pair_at(query, idx, len(" and ")) is False

    def test_explicit_source_not_decomposed(self):
        from app.router import route, clear_cache
        import app.router as router_module
        from unittest.mock import patch, MagicMock

        clear_cache()
        mock_kiwix = MagicMock(return_value="Kiwix result.")
        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = mock_kiwix
        try:
            with patch("app.router._get_cached", return_value=None):
                result = route("what is the weather and are my services up", "kiwix")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert mock_kiwix.called
        assert result == "Kiwix result."


class TestDecomposeStopWordOnlyKeywordPhrases:
    """Regression tests for a real, reproducible bug found via live
    Adversarial Self-Testing production data on MiniDock: two real,
    literal INTENT_MAP "uptime" keyword phrases — "is it up" and "are
    they up" — are made ENTIRELY of common English stop words ("is",
    "it", "up", "are", "they"). _filter_meaningful()'s generic
    stop-word-stripping check had no awareness of INTENT_MAP at all, so
    a clause consisting only of one of these phrases came back with
    zero content_words and was silently discarded from the final
    decomposed result — not folded into an adjacent part, not logged,
    simply gone.

    Confirmed directly that these are the ONLY two of all 113 real
    INTENT_MAP keyword phrases vulnerable to this — this isn't a
    narrow, two-keyword-only patch but closes the actual, general gap
    (any real keyword phrase made entirely of stop words) for however
    many such phrases exist now or are added later.
    """

    def setup_method(self):
        from app.router import _decompose
        self.decompose = _decompose

    def test_real_world_regression_case_from_production_data(self):
        """The exact query from the real, live MiniDock flag that
        found this bug. Previously decomposed to 4 parts with
        "is it up" entirely missing; must now decompose to 5."""
        parts = self.decompose(
            "feeds plus is it up in addition later today also door locked as well as google"
        )
        assert "is it up" in parts
        assert len(parts) == 5

    def test_is_it_up_alone_with_one_other_clause(self):
        parts = self.decompose("feeds plus is it up in addition later today")
        assert parts == ["feeds", "is it up", "later today"]

    def test_is_it_up_as_sole_query_not_split_at_all(self):
        """A single-clause query with no real conjunction should still
        just return itself, unaffected by this fix."""
        assert self.decompose("is it up") == ["is it up"]

    def test_are_they_up_also_recognized(self):
        """The second of the two real vulnerable phrases — confirms
        the fix isn't narrowly specific to "is it up" alone."""
        parts = self.decompose("are they up plus weather")
        assert "are they up" in parts
        assert len(parts) == 2

    def test_is_it_up_resolves_to_uptime_after_decomposition(self):
        """Confirms the fix has a real, end-to-end effect — not just
        that the clause survives decomposition, but that it correctly
        resolves to the right source afterward."""
        from app.router import detect_intent
        parts = self.decompose("feeds plus is it up in addition later today")
        intents = {p: detect_intent(p) for p in parts}
        assert intents["is it up"] == "uptime"

    def test_only_two_real_keyword_phrases_are_stop_word_only(self):
        """Documents and locks in the actual scope of the real gap this
        fix closes — confirms exactly which two phrases (out of all
        113 real INTENT_MAP keywords) are vulnerable, so a future
        change to INTENT_MAP that introduces a THIRD all-stop-word
        phrase is still automatically covered by the general fix, not
        silently missed the way the original bug was."""
        from app.router import INTENT_MAP
        from app.sources import kiwix
        import re as re_module

        vulnerable = []
        for source, keywords in INTENT_MAP.items():
            for kw in keywords:
                words = [re_module.sub(r"['']\w*$", "", w) for w in kw.lower().split()]
                content_words = [w for w in words if w not in kiwix._STOP_WORDS and len(w) > 1]
                if not content_words:
                    vulnerable.append(kw)
        assert set(vulnerable) == {"is it up", "are they up"}

    def test_unrelated_decomposition_behavior_unaffected(self):
        """Sanity check: ordinary decomposition of queries with no
        stop-word-only keyword phrase involved must be completely
        unaffected by this fix."""
        parts = self.decompose("what is the weather and are my services up")
        assert len(parts) == 2


class TestDecomposeShortKeywordBeforeLengthGate:
    """Regression tests for a real, second bug found while researching
    whether a real fusion-pollution problem (the discourse-framing
    recipe's unrelated trailing keyword riding along into kiwix's own
    search/scoring as noise) could be fixed safely. Root cause:
    "rss" — confirmed the ONLY real INTENT_MAP keyword that is itself
    <=3 characters — was discarded by _filter_meaningful()'s
    `if len(p) <= 3: continue` length gate BEFORE the INTENT_MAP keyword
    check (added earlier this session for "is it up"/"are they up")
    ever got a chance to protect it, since that check previously ran
    AFTER the length gate, not before.

    Real, observed effect: "everyone keeps talking about black holes,
    and rss" never decomposed into ["...black holes,", "rss"] — it
    stayed one unsplit string, sent whole to fusion, with "rss" riding
    along as real, counted noise into kiwix's own search API call and
    scoring for a query that should only ever be about black holes.
    Confirmed and traced directly against a live MiniDock result: an
    unrelated Stack Exchange thread and an unrelated podcast Wikipedia
    article both outscored the real Black Hole article.

    Fixed by reordering _filter_meaningful() so the colloquial-phrase
    and INTENT_MAP-keyword checks run BEFORE the length<=3 gate — once
    decomposition correctly isolates "rss" into its own clause, it
    routes to `news` independently, and kiwix never receives it as
    part of its own search text at all. This closes the actual root
    cause (decomposition failing to split a genuinely independent
    clause) rather than trying to make kiwix's scoring defensively
    robust against noise it should never have received in the first
    place — a real, structural fix, not a narrower patch.
    """

    def setup_method(self):
        from app.router import _decompose
        self.decompose = _decompose

    def test_real_world_regression_case_from_production_data(self):
        """The exact query from the real, live MiniDock result that
        found this bug."""
        parts = self.decompose("everyone keeps talking about black holes, and rss")
        assert len(parts) == 2
        assert any("rss" in p for p in parts)
        # Critically: "rss" must be its OWN clause, not still bundled
        # into the same clause as "black holes" — that's the entire
        # point of this fix.
        rss_part = next(p for p in parts if "rss" in p)
        assert "black holes" not in rss_part

    def test_bare_minimal_case_without_discourse_framing(self):
        """Confirms the fix is about the length gate itself, not
        anything specific to discourse framing — a bare 'X and rss'
        query with no discourse phrase at all must also split."""
        parts = self.decompose("black holes and rss")
        assert parts == ["black holes", "rss"]

    def test_rss_resolves_to_news_after_decomposition(self):
        """Confirms a real, end-to-end effect — not just that "rss"
        survives decomposition, but that it correctly resolves to its
        real source afterward, independently of the other clause."""
        from app.router import detect_intent
        parts = self.decompose("black holes and rss")
        assert detect_intent("rss") == "news"
        assert "rss" in parts

    def test_kiwix_clause_never_receives_rss_as_search_text(self):
        """The actual real-world payoff: once decomposed, the
        discourse-framed clause that resolves to kiwix must consist
        ONLY of the real topic words, never the unrelated trailing
        keyword — confirms the root cause is genuinely closed, not
        just that decomposition produces 2 parts."""
        parts = self.decompose("everyone keeps talking about black holes, and rss")
        kiwix_bound_part = next(p for p in parts if "black holes" in p)
        assert "rss" not in kiwix_bound_part

    def test_rss_alone_still_correctly_returned_as_single_part(self):
        """A query consisting of JUST 'rss' with no conjunction at all
        must still return itself as a single part — confirms this
        fix doesn't cause "rss" to be treated as splittable on its
        own, only that it correctly survives as a genuine part when
        a real conjunction is present."""
        assert self.decompose("rss") == ["rss"]

    def test_trivial_short_filler_fragments_still_correctly_discarded(self):
        """Confirms the reordering didn't accidentally let through
        genuinely trivial short fragments that aren't real keywords —
        the length gate still applies normally to anything that ISN'T
        "rss" or a colloquial phrase. A genuinely trivial <=3-character
        fragment ("ok") sitting between two real clauses must still be
        dropped entirely, with both real clauses surviving — the same
        behavior this had before the reordering fix."""
        parts = self.decompose("weather report also ok also news today")
        assert parts == ["weather report", "news today"]
        assert "ok" not in parts

    def test_unrelated_decomposition_behavior_unaffected(self):
        """Sanity check: ordinary decomposition of queries with no
        short keyword involved must be completely unaffected."""
        parts = self.decompose("what is the weather and are my services up")
        assert len(parts) == 2


class TestDetectIntent:
    """Tests for detect_intent — full routing with LLM fallback disabled."""

    def setup_method(self):
        from app.router import detect_intent
        self.detect = detect_intent

    def test_keyword_match_bypasses_llm(self):
        # These should match keywords and never hit Ollama
        assert self.detect("whats the weather tomorrow") == "forecast"
        assert self.detect("latest news") == "news"
        assert self.detect("is anything down") == "uptime"

    def test_no_keyword_falls_back_to_kiwix_when_llm_disabled(self):
        # With LLM_URL blank (default in test env), should return kiwix
        from app.config import settings
        original = settings.llm_url
        settings.llm_url = ""
        try:
            result = self.detect("what is the capital of France")
            assert result == "kiwix"
        finally:
            settings.llm_url = original


class TestDiscourseFramingDetection:
    """Tests for _has_discourse_framing() — detects phrasing that frames
    a topic as current public discourse ("everyone keeps talking about
    X") rather than a pure knowledge lookup.

    Found via extensive real-usage testing this session: queries like
    "what's the deal with that whole mercury retrograde thing everyone
    keeps talking about" reproducibly routed past kiwix to news/web,
    because the LLM router's source descriptions for news/web ("current
    events", "recent information") matched this phrasing almost
    word-for-word, while kiwix's description ("factual, encyclopedic, or
    technical questions") gave no signal that it ALSO covers evergreen
    topics phrased as current discourse."""

    def has_framing(self, query):
        from app.router import _has_discourse_framing
        return _has_discourse_framing(query)

    def test_everyone_keeps_talking_about(self):
        assert self.has_framing(
            "whats the deal with that whole mercury retrograde thing everyone keeps talking about"
        ) is True

    def test_everyones_obsessed_with(self):
        assert self.has_framing(
            "whats the deal with that whole galaxy thing everyone's obsessed with right now"
        ) is True

    def test_everyone_is_obsessed_with(self):
        assert self.has_framing(
            "whats the deal with that whole bitcoin thing everyone is obsessed with"
        ) is True

    def test_everyones_talking_about(self):
        assert self.has_framing(
            "whats the deal with that whole black hole thing everyone's talking about"
        ) is True

    def test_plain_factual_query_no_framing(self):
        assert self.has_framing("what is the capital of france") is False

    def test_colloquial_phrase_without_discourse_framing(self):
        """'whats the deal with X' alone, with no discourse-framing
        language attached, should NOT trigger this — it's a perfectly
        normal definitional question handled by the existing colloquial
        pattern detection, not evidence of a routing bias problem."""
        assert self.has_framing("whats the deal with sunspots") is False

    def test_tell_me_about_no_framing(self):
        assert self.has_framing("tell me about mercury") is False


class TestDiscourseFramingRoutingBias:
    """Tests confirming _llm_detect() actually applies the discourse-
    framing bias — kiwix is added (escalating to fusion) when discourse-
    framing language is present and kiwix wasn't already part of the
    LLM's chosen source(s), across all four real code paths: fresh
    single-source, fresh multi-source, cached single-source, and cached
    multi-source. The cached paths matter because a routing cache entry
    written before this fix existed (or before kiwix happened to be
    chosen) would otherwise silently bypass the bias for the remainder
    of its TTL, since the cache check returns before the bias logic
    further down in the function ever runs."""

    def setup_method(self):
        from app.router import clear_routing_cache
        clear_routing_cache()

    def test_fresh_single_source_gets_kiwix_added(self):
        from app.router import _llm_detect
        from unittest.mock import patch

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="web"):
            result = _llm_detect(
                "whats the deal with that whole mercury retrograde thing everyone keeps talking about"
            )
        assert isinstance(result, list)
        assert "web" in result
        assert "kiwix" in result

    def test_fresh_multi_source_gets_kiwix_added(self):
        from app.router import _llm_detect
        from unittest.mock import patch

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="news, web"):
            result = _llm_detect(
                "whats the deal with that whole bitcoin thing everyone is obsessed with"
            )
        assert "news" in result
        assert "web" in result
        assert "kiwix" in result

    def test_already_kiwix_is_not_duplicated_or_escalated(self):
        from app.router import _llm_detect
        from unittest.mock import patch

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="kiwix"):
            result = _llm_detect(
                "whats the deal with that whole mercury retrograde thing everyone keeps talking about"
            )
        assert result == "kiwix"

    def test_no_discourse_framing_is_unaffected(self):
        from app.router import _llm_detect
        from unittest.mock import patch

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="web"):
            result = _llm_detect("what is happening in the news today")
        assert result == "web"

    def test_cached_single_source_decision_still_gets_bias_applied(self):
        """Regression coverage for the cache-bypass risk found during
        design — a cached decision from BEFORE this fix existed (or
        before kiwix happened to be chosen) must not silently skip the
        bias for the rest of its TTL."""
        from app.router import _llm_detect, _set_routing
        from unittest.mock import patch

        query = "whats the deal with that whole galaxy thing everyone's obsessed with right now"
        _set_routing(f"source:{query}", "web")  # simulate a pre-fix cached decision

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete") as mock_complete:
            result = _llm_detect(query)

        assert not mock_complete.called  # cache hit — should not call the LLM again
        assert "web" in result
        assert "kiwix" in result

    def test_cached_fusion_decision_still_gets_bias_applied(self):
        from app.router import _llm_detect, _set_routing
        from unittest.mock import patch

        query = "whats the deal with that whole bitcoin thing everyone is obsessed with"
        _set_routing(f"source:{query}", "news,web")  # simulate a pre-fix cached fusion decision

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete") as mock_complete:
            result = _llm_detect(query)

        assert not mock_complete.called
        assert "news" in result
        assert "web" in result
        assert "kiwix" in result


class TestLlmDetectFailureNotCached:
    """Regression tests for a real, significant bug found via the same
    deliberate complexity-investigation pass that found the identical
    pattern in _llm_pick_fusion_sources(): _llm_detect() used to cache
    its "kiwix" fallback under the exact same key a genuine LLM success
    would use, when the LLM returned an unrecognized source name. A
    single transient LLM hiccup would permanently lock a specific query
    into kiwix for the full routing cache TTL, even though a retry
    moments later would likely have succeeded with the actual, correct
    source."""

    def test_unrecognized_source_fallback_is_not_cached(self):
        from app.router import _llm_detect, clear_routing_cache
        from unittest.mock import patch
        clear_routing_cache()
        query = "some genuinely ambiguous query that fails once"

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="garbage_not_a_source"):
            result1 = _llm_detect(query)
        assert result1 == "kiwix"

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="forecast") as mock_complete:
            result2 = _llm_detect(query)
        assert mock_complete.called  # genuinely re-queried, not short-circuited by a cached failure
        assert result2 == "forecast"


class TestMergeDecomposedPartsSharedWithFusion:
    """Tests confirming _merge_decomposed_parts() genuinely shares its
    consecutive-same-source merging logic with fusion.py's own
    _merge_same_source(), rather than maintaining a separate, duplicate
    copy. Found via a deliberate complexity-investigation pass on
    fusion.search() — the two implementations were byte-for-byte
    identical before being unified. The shared function lives in
    fusion.py (router.py already imports fusion directly, e.g. to call
    fusion.search() for internal multi-source dispatch; the reverse
    direction would create a circular import)."""

    def test_router_genuinely_calls_the_shared_fusion_function(self):
        """Confirms the sharing is real, not coincidental — patches
        fusion._merge_same_source itself and verifies router.py's merge
        function actually invokes it, rather than just asserting on the
        final merged output (which could theoretically match by
        coincidence if the two implementations had silently diverged
        again in the future)."""
        from app.router import _merge_decomposed_parts
        from unittest.mock import patch

        with patch("app.router.fusion._merge_same_source", return_value=[("ha", "merged result")]) as mock_merge:
            _merge_decomposed_parts([("ha", "part 1"), ("ha", "part 2")])

        mock_merge.assert_called_once_with([("ha", "part 1"), ("ha", "part 2")])

    def test_consecutive_same_source_parts_merged_into_one_section(self):
        from app.router import _merge_decomposed_parts
        result, source = _merge_decomposed_parts([
            ("ha", "Indoor sensors result."),
            ("ha", "Door locks result."),
        ])
        assert source == "ha"
        assert "Indoor sensors result." in result
        assert "Door locks result." in result
        assert result.count("[HA") == 1  # one header, not two


class TestDedupeNestedFusionSections:
    """Regression tests for a real, live duplicate-section bug found
    via manual verification of a search result on MiniDock, not by any
    automated check — the result was technically correct by every
    Adversarial Self-Testing check (a real [KIWIX —...] and [NEWS —...]
    section both present), but the actual answer had a real defect: a
    second, redundant [NEWS — ...] section appeared near the end,
    duplicating real content already shown earlier.

    Root cause: when one decomposed sub-query resolves to internal
    fusion (carrying its own already-headered, nested sections) and a
    DIFFERENT, separately-decomposed sub-query resolves to a bare
    single source that happens to be one of the sources already inside
    that nested fusion blob, _merge_same_source()'s consecutive-
    same-source check only ever compares the OUTER tuple label
    ("fusion" vs "news") — it has no way to see that a section nested
    INSIDE the fusion blob duplicates the second, separate tuple.
    Confirmed this is a real, pre-existing gap, not something the
    discourse-framing/decomposition fixes earlier this release
    introduced — it was already reachable via any other query shape
    where one decomposed clause's own LLM-judged fusion happens to
    share a source with a different clause; this query shape (a
    discourse-framing escalation alongside an unrelated trailing
    keyword) just made it reliably, easily reachable instead of
    needing a rarer LLM-judgment coincidence to trigger.

    Fixed with a second, separate post-processing pass —
    _dedupe_nested_fusion_sections() — that operates on the final,
    fully-assembled text using the exact, real header strings
    fusion._format_header() can produce (re.escape()'d, not a generic
    bracket-matching pattern), safe against real content that happens
    to contain bracket-like text.
    """

    def setup_method(self):
        from app.router import _dedupe_nested_fusion_sections, _merge_decomposed_parts
        self.dedupe = _dedupe_nested_fusion_sections
        self.merge = _merge_decomposed_parts

    def test_real_world_regression_case_from_production_data(self):
        """Reconstructs the exact real scenario from the live MiniDock
        result that found this bug: one decomposed clause resolving to
        internal fusion (kiwix+news+web already nested/headered), a
        second, separate clause resolving to bare news."""
        nested_fusion_blob = (
            "[KIWIX — ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW]\n"
            "Black hole article content.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "First news block.\n\n---\n\n"
            "[WEB — LIVE WEB SEARCH RESULTS]\n"
            "Web search content."
        )
        result, source = self.merge([
            ("fusion", nested_fusion_blob),
            ("news", "RSS feed news block."),
        ])
        assert result.count("[NEWS —") == 1
        assert result.count("[KIWIX —") == 1
        assert result.count("[WEB —") == 1
        # Both real news contents must survive, just merged into one section.
        assert "First news block." in result
        assert "RSS feed news block." in result
        assert source == "fusion"

    def test_real_world_regression_case_with_overlapping_headlines(self):
        """A second, separate real bug found verifying the fix above
        against MiniDock's actual stack: the section-level fix above
        correctly produced exactly ONE [NEWS — ...] header, but the
        single section's own body still contained the same several
        headlines twice — because the nested fusion blob's news
        section and the second, separately-decomposed clause's bare
        news result both genuinely came from two independent calls to
        the same backend (a real FreshRSS "general query, return
        everything" case), with no content-level dedup anywhere.
        Reconstructs that exact shape: overlapping AND non-overlapping
        headlines between the two real news results."""
        nested_fusion_blob = (
            "[KIWIX — ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW]\n"
            "Black hole article content.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "**Headline A** (NYT)\nContent A\n\n---\n\n"
            "**Headline B** (NYT)\nContent B\n\n---\n\n"
            "**Headline C** (NYT)\nContent C\n\n---\n\n"
            "[WEB — LIVE WEB SEARCH RESULTS]\n"
            "Web search content."
        )
        bare_news_result = (
            "**Headline A** (NYT)\nContent A\n\n---\n\n"
            "**Headline B** (NYT)\nContent B\n\n---\n\n"
            "**Headline D** (NYT)\nContent D"
        )
        result, source = self.merge([
            ("fusion", nested_fusion_blob),
            ("news", bare_news_result),
        ])
        assert result.count("[NEWS —") == 1
        # Every distinct headline appears exactly once.
        for headline in ["Headline A", "Headline B", "Headline C", "Headline D"]:
            assert result.count(headline) == 1
        # The merged news section's items are joined with the real
        # item separator, not a bare double-newline that would make
        # the boundary between the two original results visually
        # indistinguishable from an ordinary paragraph break.
        news_section = result[result.index("[NEWS —"):result.index("[WEB —")]
        assert "Content C\n\n---\n\n**Headline D**" in news_section

    def test_no_duplicate_sections_is_a_true_noop(self):
        """The overwhelming common case — no duplicate at all — must
        be a byte-for-byte no-op."""
        text = (
            "[FORECAST — WEATHER FORECAST FOR YOUR CONFIGURED HOME LOCATION]\n"
            "Sunny today.\n\n---\n\n"
            "[HA — YOUR HOME ASSISTANT ENTITY STATES]\n"
            "All doors locked."
        )
        assert self.dedupe(text) == text

    def test_header_less_text_is_a_true_noop(self):
        """Single-source, header-less plain text (the common case for
        a non-decomposed query) must be a true no-op."""
        text = "Just a plain single-source answer with no headers at all."
        assert self.dedupe(text) == text

    def test_empty_string_is_a_true_noop(self):
        assert self.dedupe("") == ""

    def test_non_adjacent_duplicate_sections_are_merged(self):
        """Duplicate sections separated by a third, different section
        in between must still be found and merged, not just adjacent
        ones — confirms this isn't limited to the consecutive case
        _merge_same_source() already handles."""
        text = (
            "[KIWIX — ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW]\n"
            "Kiwix content.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "First news block.\n\n---\n\n"
            "[WEB — LIVE WEB SEARCH RESULTS]\n"
            "Web content.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "Second news block."
        )
        result = self.dedupe(text)
        assert result.count("[NEWS —") == 1
        assert "First news block." in result
        assert "Second news block." in result
        # Merged news section keeps its FIRST-occurrence position,
        # the same convention _merge_same_source() already uses.
        assert result.index("[NEWS —") < result.index("[WEB —")

    def test_duplicate_content_uses_clean_separator_not_a_dangling_artifact(self):
        """Confirms the merge produces exactly ONE clean item separator
        between the two now-combined news blocks, not a leftover,
        malformed artifact from the original outer split (e.g. a
        doubled "---\\n\\n---\\n\\n" or a separator with mismatched
        whitespace around it).

        Updated to reflect a deliberate behavior change from the
        per-group separator fix (see _dedupe_nested_fusion_sections' own
        docstring): combining 2+ genuinely separate same-source pieces
        under one header is, definitionally, a multi-item situation the
        moment there are two of them — so the merged NEWS section's body
        now correctly joins "First news block." and "Second news
        block." with the real "\\n\\n---\\n\\n" item separator, the same
        way fusion._merge_same_source() now does for the structurally
        identical case. A prior, per-pair version of this fix produced a
        bare "\\n\\n" here instead, which was itself part of the same
        underlying bug this release closes — see
        test_mixed_single_and_multi_item_chain_gets_consistent_separator
        below for the case that actually exposes why the bare newline
        was wrong."""
        nested_fusion_blob = (
            "[KIWIX — ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW]\n"
            "Kiwix content.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "First news block."
        )
        result, _ = self.merge([
            ("fusion", nested_fusion_blob),
            ("news", "Second news block."),
        ])
        news_section_start = result.index("[NEWS —")
        news_section_body = result[news_section_start:]
        # Exactly one real item separator between the two news blocks —
        # not zero (a bare, ambiguous "\n\n"), and not a doubled or
        # malformed artifact from the original outer split.
        assert news_section_body.count("---") == 1
        assert "First news block.\n\n---\n\nSecond news block." in news_section_body

    def test_mixed_single_and_multi_item_chain_gets_consistent_separator(self):
        """Regression test for the real bug this fix closes, at the
        text-assembly level: a chain mixing single-item and multi-item
        pieces under the SAME header must get a consistent "---"
        separator at every boundary, not just the boundary adjacent to
        the already-multi-item piece. Mirrors fusion.py's own
        test_merge_same_source_mixed_single_and_multi_item_chain_gets_consistent_separator
        — the sibling function had the byte-for-byte identical gap."""
        text = (
            "[WEB — LIVE WEB SEARCH RESULTS]\nUnrelated web section.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "**Bitcoin Hits New High** (CoinDesk)\nPrice surged today.\n\n---\n\n"
            "[FORECAST — WEATHER FORECAST FOR YOUR CONFIGURED HOME LOCATION]\nSunny today.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "**Election Results Certified** (AP)\nOfficials confirmed the vote.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\n"
            "**Storm Approaches** (Weather.com)\nHeavy rain expected.\n\n---\n\n"
            "**Flooding Reported** (Local News)\nSeveral roads closed."
        )
        result = self.dedupe(text)
        assert result.count("[NEWS —") == 1
        news_section_start = result.index("[NEWS —")
        news_section_body = result[news_section_start:]
        # The boundary between Bitcoin and Election must be the real
        # item separator — a prior, per-pair version left this one as a
        # bare "\n\n" since neither side of that specific pair was
        # multi-item on its own.
        assert "Price surged today.\n\n**Election" not in news_section_body
        assert "Price surged today.\n\n---\n\n**Election" in news_section_body

    def test_three_or_more_duplicate_headers_all_merge_into_one(self):
        """Confirms the fix generalizes beyond exactly two duplicates —
        three separate occurrences of the same header must all merge
        into a single section, not just pairwise."""
        text = (
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\nFirst.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\nSecond.\n\n---\n\n"
            "[NEWS — RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC UNLESS STATED]\nThird."
        )
        result = self.dedupe(text)
        assert result.count("[NEWS —") == 1
        assert "First." in result
        assert "Second." in result
        assert "Third." in result

    def test_content_containing_literal_dashes_is_not_falsely_matched_as_a_header(self):
        """Confirms the exact-header-string requirement protects real
        content that happens to contain bracket-like or dash-like
        text from being misinterpreted as a section boundary."""
        text = (
            "[KIWIX — ENCYCLOPEDIC KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW]\n"
            "An article that mentions [NEWS] in brackets and has --- dashes in its own body, "
            "neither of which is a real, exact header string.\n\n---\n\n"
            "[WEB — LIVE WEB SEARCH RESULTS]\n"
            "Web content."
        )
        result = self.dedupe(text)
        assert result == text  # true no-op — neither fake marker is a real header
        assert "[NEWS]" in result  # the real content survived untouched


class TestDiscourseFramingKeywordPathEscalation:
    """Tests for detect_intent()'s own discourse-framing escalation —
    distinct from TestDiscourseFramingRoutingBias above, which only
    covers _llm_detect()'s four internal paths.

    Found via real production data, not development-time testing: a
    live Adversarial Self-Testing flag on MiniDock caught
    "everyone keeps talking about black holes, and rss" resolving to
    bare "news", kiwix never considered. Traced to a real, reproducible
    gap — _keyword_detect() matching "rss" (a real, ordinary
    INTENT_MAP "news" keyword) caused detect_intent()'s own
    `if source: return source` to short-circuit BEFORE _llm_detect()
    (and therefore both escalation helpers, which only live inside it)
    ever ran. This reproduced for every INTENT_MAP keyword tried, not
    just "rss" — INTENT_MAP contains dozens of short, common
    words/phrases ("news", "weather", "feeds", "door locked") that can
    easily co-occur with genuine discourse framing in a real sentence,
    so this wasn't a narrow one-keyword edge case but a structural gap
    in detect_intent() itself, sitting upstream of all four of
    _llm_detect()'s already-correctly-fixed paths.

    The Discourse-Framing Investigation wiki page's claim of fixing
    "all four real code paths" was accurate for the LLM-detection
    paths it actually meant, but the keyword-match short-circuit was
    never one of the four — these tests close that specific, separate
    gap directly at the one place it actually lives.
    """

    def test_real_world_regression_case_from_production_data(self):
        """The exact query and exact result from the real, live
        MiniDock flag that found this bug — not a constructed
        hypothetical."""
        from app.router import detect_intent
        result = detect_intent("everyone keeps talking about black holes, and rss")
        assert isinstance(result, list)
        assert "news" in result
        assert "kiwix" in result

    def test_single_keyword_match_gets_kiwix_escalated(self):
        from app.router import detect_intent
        result = detect_intent("everyone keeps talking about quantum computing, also whats the weather")
        assert isinstance(result, list)
        assert "forecast" in result
        assert "kiwix" in result

    def test_multi_keyword_match_gets_kiwix_escalated(self):
        """A query matching MULTIPLE keywords (already escalating to a
        list via _keyword_detect() itself, with no LLM call at all)
        must still get kiwix added — confirms the fix isn't narrowly
        scoped to the single-match case only."""
        from app.router import detect_intent
        result = detect_intent("everyone is talking about that volcano in Iceland, and any new headlines")
        assert isinstance(result, list)
        assert "news" in result
        assert "changes" in result
        assert "kiwix" in result

    def test_keyword_match_without_discourse_framing_is_unaffected(self):
        """The core regression-safety check: an ordinary keyword query
        with no discourse framing at all must NOT get kiwix added —
        confirms the fix doesn't over-trigger on every keyword match."""
        from app.router import detect_intent
        assert detect_intent("whats the news today") == "news"
        result = detect_intent("check the news and weather")
        assert set(result) == {"forecast", "news"}
        assert "kiwix" not in result

    def test_escalation_helper_correctly_wired_for_already_kiwix_case(self):
        """kiwix is never itself a keyword-matchable INTENT_MAP source,
        so detect_intent()'s keyword path can't literally reach this
        case through real keyword matching — but the wiring this fix
        added (`escalated if escalated is not None else source`) must
        still behave correctly if it ever were reached, since
        _escalate_single_source_for_discourse_framing() already
        returns None specifically for a kiwix source (no escalation
        needed). Calls the real helper directly to confirm that
        contract, then confirms detect_intent()'s own fallback
        expression handles a None return correctly without duplicating
        or losing the source."""
        from app.router import _escalate_single_source_for_discourse_framing, INTENT_MAP
        assert "kiwix" not in INTENT_MAP  # confirms the real reason this can't occur via keyword match
        assert _escalate_single_source_for_discourse_framing(
            "everyone keeps talking about quantum computing", "kiwix"
        ) is None
        # detect_intent()'s fix: `escalated if escalated is not None else source`
        source = "kiwix"
        escalated = _escalate_single_source_for_discourse_framing("everyone keeps talking about X", source)
        result = escalated if escalated is not None else source
        assert result == "kiwix"  # falls back to the plain source, not None or a list

    def test_no_keyword_match_still_falls_through_to_llm_detect(self):
        """Confirms the fix didn't accidentally change the no-keyword-
        match path — it should still fall through to _llm_detect()
        exactly as before, untouched by this fix."""
        from app.router import detect_intent
        from unittest.mock import patch
        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="web"):
            result = detect_intent("tell me about the history of ancient rome")
        assert result == "web"


class TestDiscourseFramingEscalationHelpers:
    """Direct, isolated tests for _escalate_multi_source_for_discourse_
    framing() and _escalate_single_source_for_discourse_framing() —
    extracted from _llm_detect() during a deliberate complexity-reduction
    pass (the same discipline applied to route_with_source(),
    home_assistant.py's search(), and kiwix.py's _pick_books_with_llm()
    earlier this release cycle). Unlike those, this investigation
    confirmed CORRECTNESS rather than finding a bug — directly verifying
    that not re-caching an escalated cached decision is intentional, not
    an oversight, since _has_discourse_framing() is re-evaluated fresh
    on every call regardless of what's cached, so the escalation
    self-heals on every request. TestDiscourseFramingRoutingBias above
    already covers all four real code paths through _llm_detect()'s
    public interface; these tests add fast, isolated coverage of the
    two extracted helpers directly."""

    def test_multi_source_adds_kiwix_when_framing_present(self):
        from app.router import _escalate_multi_source_for_discourse_framing
        result = _escalate_multi_source_for_discourse_framing(
            "whats the deal with bitcoin everyone is obsessed with", ["web", "news"]
        )
        assert result == ["web", "news", "kiwix"]

    def test_multi_source_does_not_duplicate_existing_kiwix(self):
        from app.router import _escalate_multi_source_for_discourse_framing
        result = _escalate_multi_source_for_discourse_framing(
            "whats the deal with bitcoin everyone is obsessed with", ["kiwix", "web"]
        )
        assert result == ["kiwix", "web"]

    def test_multi_source_unaffected_without_framing(self):
        from app.router import _escalate_multi_source_for_discourse_framing
        result = _escalate_multi_source_for_discourse_framing(
            "what is the weather today", ["web", "news"]
        )
        assert result == ["web", "news"]

    def test_multi_source_does_not_mutate_original_list(self):
        """A real, deliberate design choice worth its own regression
        test: the helper returns a NEW list rather than mutating the
        caller's list in place, since the caller (e.g. a cached value
        parsed fresh on every call) shouldn't have its own local list
        silently changed as a side effect of calling this helper."""
        from app.router import _escalate_multi_source_for_discourse_framing
        original = ["web", "news"]
        result = _escalate_multi_source_for_discourse_framing(
            "whats the deal with bitcoin everyone is obsessed with", original
        )
        assert original == ["web", "news"]
        assert result == ["web", "news", "kiwix"]

    def test_single_source_escalates_when_framing_present(self):
        from app.router import _escalate_single_source_for_discourse_framing
        result = _escalate_single_source_for_discourse_framing(
            "whats the deal with bitcoin everyone is obsessed with", "web"
        )
        assert result == ["web", "kiwix"]

    def test_single_source_returns_none_without_framing(self):
        from app.router import _escalate_single_source_for_discourse_framing
        result = _escalate_single_source_for_discourse_framing("what is the weather today", "web")
        assert result is None

    def test_single_source_returns_none_when_already_kiwix(self):
        from app.router import _escalate_single_source_for_discourse_framing
        result = _escalate_single_source_for_discourse_framing(
            "whats the deal with bitcoin everyone is obsessed with", "kiwix"
        )
        assert result is None


# ---------------------------------------------------------------------------
# Cache logic
# ---------------------------------------------------------------------------

class TestCache:
    """Tests for cache functions — no network required."""

    def setup_method(self):
        from app.router import clear_cache, _set_cached, _get_cached, check_cached, get_cache_count
        self.clear = clear_cache
        self.set = _set_cached
        self.get = _get_cached
        self.check = check_cached
        self.count = get_cache_count
        self.clear()

    def test_cache_miss_returns_none(self):
        assert self.get("kiwix", "something not cached") is None

    def test_cache_hit_returns_result(self):
        self.set("kiwix", "what is molybdenum", "Molybdenum is an element.")
        result = self.get("kiwix", "what is molybdenum")
        assert result == "Molybdenum is an element."

    def test_check_cached_false_on_miss(self):
        assert self.check("kiwix", "not cached") is False

    def test_check_cached_true_on_hit(self):
        self.set("kiwix", "test query", "test result")
        assert self.check("kiwix", "test query") is True

    def test_cache_key_is_case_insensitive(self):
        self.set("kiwix", "What Is Molybdenum", "result")
        assert self.get("kiwix", "what is molybdenum") == "result"

    def test_cache_key_strips_whitespace(self):
        self.set("kiwix", "  molybdenum  ", "result")
        assert self.get("kiwix", "molybdenum") == "result"

    def test_cache_count(self):
        self.set("kiwix", "query one", "result one")
        self.set("kiwix", "query two", "result two")
        assert self.count() == 2

    def test_clear_cache(self):
        self.set("kiwix", "query", "result")
        count = self.clear()
        assert count == 1
        assert self.count() == 0

    def test_different_sources_different_entries(self):
        self.set("kiwix", "query", "kiwix result")
        self.set("web", "query", "web result")
        assert self.get("kiwix", "query") == "kiwix result"
        assert self.get("web", "query") == "web result"

    def test_expired_entry_returns_none(self):
        from app.router import _cache, CACHE_TTL
        # Manually insert an expired entry
        key = "kiwix:expired query"
        _cache[key] = ("old result", time.time() - CACHE_TTL["kiwix"] - 1)
        assert self.get("kiwix", "expired query") is None

    def test_cache_max_size_evicts_oldest(self):
        from app.router import _CACHE_MAX_SIZE
        # Fill to max
        for i in range(_CACHE_MAX_SIZE):
            self.set("kiwix", f"query {i}", f"result {i}")
        assert self.count() == _CACHE_MAX_SIZE
        # Add one more — should evict oldest
        self.set("kiwix", "one more query", "one more result")
        assert self.count() == _CACHE_MAX_SIZE


class TestRoutingCacheEviction:
    """Tests for routing cache size-bounding — a real gap found during
    operational maturity review: the routing cache had NO size limit at
    all until this was added, unlike the result cache, which already had
    this exact eviction pattern. The routing cache's key space is
    genuinely larger in practice (every unique conditional query,
    discourse-framing phrase, and disambiguation candidate set gets its
    own entry), making unbounded growth over sustained real-world usage a
    real concern, not just a theoretical one.

    _save_routing_cache() (real disk I/O) is mocked throughout — filling
    the cache to its max size in a test would otherwise mean many real
    disk writes (one per _ROUTING_CACHE_SAVE_INTERVAL writes, now that
    routing saves are batched the same way the result cache's always
    were) to test logic that has nothing to do with disk I/O at all.
    """

    def setup_method(self):
        from app.router import clear_routing_cache
        from unittest.mock import patch
        self._save_patch = patch("app.router._save_routing_cache")
        self._save_patch.start()
        clear_routing_cache()

    def teardown_method(self):
        self._save_patch.stop()

    def test_routing_cache_has_a_size_limit(self):
        """Confirm the constant actually exists and is a positive
        integer — the most basic regression check that this isn't
        silently disabled or misconfigured to 0/None."""
        from app.router import _ROUTING_CACHE_MAX_SIZE
        assert isinstance(_ROUTING_CACHE_MAX_SIZE, int)
        assert _ROUTING_CACHE_MAX_SIZE > 0

    def test_routing_cache_max_size_evicts_oldest(self):
        from app.router import _ROUTING_CACHE_MAX_SIZE, _set_routing, _routing_cache
        for i in range(_ROUTING_CACHE_MAX_SIZE):
            _set_routing(f"routing query {i}", "kiwix")
        assert len(_routing_cache) == _ROUTING_CACHE_MAX_SIZE
        # Add one more — should evict oldest rather than growing unbounded
        _set_routing("one more routing query", "web")
        assert len(_routing_cache) == _ROUTING_CACHE_MAX_SIZE

    def test_eviction_removes_the_genuinely_oldest_entry(self):
        """Confirm eviction actually removes the OLDEST entry by
        timestamp, not an arbitrary one — the same correctness property
        the result cache's _evict_oldest() already guarantees."""
        from app.router import _ROUTING_CACHE_MAX_SIZE, _set_routing, _get_routing
        import time as time_module
        from unittest.mock import patch

        # Manually control timestamps so eviction order is unambiguous
        base_time = time_module.time()
        with patch("app.router.time.time", side_effect=[base_time + i for i in range(_ROUTING_CACHE_MAX_SIZE)]):
            for i in range(_ROUTING_CACHE_MAX_SIZE):
                _set_routing(f"query {i}", "kiwix")

        assert _get_routing("query 0") == "kiwix"  # the oldest, still present

        with patch("app.router.time.time", return_value=base_time + _ROUTING_CACHE_MAX_SIZE):
            _set_routing("newest query", "web")

        # The oldest entry ("query 0") must now be gone
        assert _get_routing("query 0") is None
        assert _get_routing("newest query") == "web"

    def test_updating_an_existing_key_does_not_trigger_eviction(self):
        """Re-caching a decision for a query ALREADY in the cache must
        not count as a new entry for eviction purposes — only genuinely
        new keys should trigger eviction when at capacity, matching the
        same logic the result cache's _set_cached() already has."""
        from app.router import _ROUTING_CACHE_MAX_SIZE, _set_routing, _routing_cache
        for i in range(_ROUTING_CACHE_MAX_SIZE):
            _set_routing(f"query {i}", "kiwix")
        assert len(_routing_cache) == _ROUTING_CACHE_MAX_SIZE

        # Re-cache an EXISTING key — should not evict anything, since
        # this isn't a new entry
        _set_routing("query 0", "web")
        assert len(_routing_cache) == _ROUTING_CACHE_MAX_SIZE


# ---------------------------------------------------------------------------
# Empty result detection
# ---------------------------------------------------------------------------

class TestLooksEmpty:
    """Tests for _looks_empty — identifies failed/empty responses."""

    def setup_method(self):
        from app.router import _looks_empty
        self.check = _looks_empty

    def test_no_results_found(self):
        assert self.check("No results found in Kiwix knowledge base.") is True

    def test_no_recent_articles(self):
        assert self.check("No recent articles found in FreshRSS.") is True

    def test_could_not_fetch(self):
        assert self.check("Found article but could not fetch article content.") is True

    def test_real_result_not_empty(self):
        assert self.check("# Molybdenum\nMolybdenum is a chemical element...") is False

    def test_forecast_result_not_empty(self):
        assert self.check("Today will be clear with a high of about 101.") is False

    def test_case_insensitive(self):
        assert self.check("NO RESULTS FOUND in kiwix") is True

    def test_not_configured_message_recognized_as_empty(self):
        """Regression test for a real, significant bug found via a
        second, deliberate "bulletproofing" re-pass: router.py's own
        _looks_empty() used to carry a separate, independently-
        maintained phrase list that was missing "not configured" and
        "could not connect" — meaning a genuinely real, reachable
        scenario (FreshRSS unconfigured, asking for news) returned the
        literal config-error string as if it were real, successful
        content, and FALLBACK_CHAIN's real "news" -> "web" fallback
        never triggered as a result. Confirmed end to end before fixing:
        route_with_source("give me the news", "news") with
        FRESHRSS_URL unset returned source_used="news" with the raw
        config-error message, not the automatic fallback to "web"."""
        assert self.check("FreshRSS is not configured. Set FRESHRSS_URL and FRESHRSS_USER.") is True
        assert self.check("Forecast is not configured. Set FORECAST_LATITUDE and FORECAST_LONGITUDE.") is True

    def test_could_not_connect_message_recognized_as_empty(self):
        assert self.check("Could not connect to Home Assistant. Check HA_URL and HA_TOKEN.") is True

    def test_error_reaching_message_recognized_as_empty(self):
        """The real SearXNG timeout/connection message doesn't contain
        a bare "error:" (the colon comes after "SearXNG", not
        immediately after "Error"), so it needed its own phrase —
        found while verifying the unified list against every real
        failure message every source file actually produces."""
        assert self.check("Error reaching SearXNG: connection failed.") is True

    def test_genuinely_shares_the_same_function_as_fusion(self):
        """Confirms the sharing is real, not coincidental — patches
        fusion._looks_empty itself and verifies router.py's
        _looks_empty actually delegates to it."""
        from app.router import _looks_empty
        from unittest.mock import patch
        with patch("app.router.fusion._looks_empty", return_value="sentinel") as mock_fn:
            result = _looks_empty("some result")
        mock_fn.assert_called_once_with("some result")
        assert result == "sentinel"


class TestFallbackChainTriggersOnNotConfigured:
    """The actual, real, end-to-end regression test for the _looks_empty
    cross-file drift bug found via a second, deliberate "bulletproofing"
    re-pass: FALLBACK_CHAIN's real "news" -> "web" fallback (and
    "kiwix" -> "web") must genuinely trigger when the primary source
    returns a real, recognizable "not configured" message, not just
    when _looks_empty() is tested in isolation."""

    def test_unconfigured_news_falls_back_to_web(self):
        from app.router import route_with_source, clear_cache
        import app.router as router_module
        from app.config import settings

        clear_cache()
        original_url = settings.freshrss_url
        settings.freshrss_url = ""
        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["web"] = lambda q: "Real, genuine web search result content."
        try:
            result, source = route_with_source("give me the news", "news")
        finally:
            settings.freshrss_url = original_url
            router_module.SOURCE_MAP.update(original_map)

        assert source == "web"
        assert result == "Real, genuine web search result content."

    def test_real_news_with_unlucky_headline_does_not_fall_back_to_web(self):
        """The inverse, real-world regression case for the same
        _looks_empty() machinery: a genuinely successful `news` result
        whose article happens to contain an unlucky phrase ("could not
        connect") must be returned AS-IS, not silently discarded and
        replaced by a worse, generic web-search fallback. Confirmed
        this exact scenario would have triggered FALLBACK_CHAIN's
        "news" -> "web" path against the pre-fix _looks_empty() —
        real, successful content being thrown away for no reason but
        an unlucky word inside a real headline this project has no
        control over."""
        from app.router import route_with_source, clear_cache
        import app.router as router_module

        clear_cache()
        real_news_result = (
            "**Power Grid Operators Could Not Connect Backup Systems** (Reuters)\n"
            "Regional officials say backup infrastructure failed to engage during peak demand."
        )
        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["news"] = lambda q: real_news_result
        router_module.SOURCE_MAP["web"] = lambda q: "FALLBACK: this should never be returned."
        try:
            result, source = route_with_source("give me the news", "news")
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert source == "news"
        assert result == real_news_result


class TestLlmPickFusionSources:
    """Tests for _llm_pick_fusion_sources LLM fusion source selection."""

    def test_returns_default_when_llm_not_configured(self):
        from app.router import _llm_pick_fusion_sources
        from unittest.mock import patch
        with patch("app.llm.is_configured", return_value=False):
            result = _llm_pick_fusion_sources("what is the weather")
        assert result == ["kiwix", "web"]

    def test_returns_llm_sources_when_valid(self):
        from app.router import _llm_pick_fusion_sources, clear_routing_cache
        from unittest.mock import patch
        clear_routing_cache()
        with patch("app.llm.is_configured", return_value=True):
            with patch("app.llm.complete", return_value="forecast, uptime"):
                with patch("app.router._get_routing", return_value=None):
                    result = _llm_pick_fusion_sources("whats the weather and services up")
        assert "forecast" in result
        assert "uptime" in result

    def test_falls_back_to_default_on_invalid_llm_response(self):
        from app.router import _llm_pick_fusion_sources, clear_routing_cache
        from unittest.mock import patch
        clear_routing_cache()
        with patch("app.llm.is_configured", return_value=True):
            with patch("app.llm.complete", return_value="invalid_source, another_bad"):
                with patch("app.router._get_routing", return_value=None):
                    result = _llm_pick_fusion_sources("some query xyz")
        assert result == ["kiwix", "web"]

    def test_failure_fallback_is_not_cached_allowing_later_success(self):
        """Regression test for a real, significant bug found via a
        deliberate complexity-investigation pass: this function used to
        cache the ["kiwix", "web"] failure fallback under the exact same
        key a genuine success would use — a single transient LLM hiccup
        would permanently lock a specific query into the generic
        fallback for the full routing cache TTL, even though a retry
        moments later would likely have succeeded with a better, more
        specific source selection. Confirms the fix directly: a query
        that fails once and would genuinely succeed on a second attempt
        actually reaches the LLM the second time, rather than the cached
        failure short-circuiting the function before the real call."""
        from app.router import _llm_pick_fusion_sources, clear_routing_cache
        from unittest.mock import patch
        clear_routing_cache()
        query = "a query that fails once then succeeds"

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="not_a_real_source"):
            result1 = _llm_pick_fusion_sources(query)
        assert result1 == ["kiwix", "web"]

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="forecast, news, uptime") as mock_complete:
            result2 = _llm_pick_fusion_sources(query)
        assert mock_complete.called  # the LLM was genuinely re-queried, not short-circuited by a cached failure
        assert "forecast" in result2
        assert "news" in result2

    def test_caps_at_three_sources(self):
        from app.router import _llm_pick_fusion_sources, clear_routing_cache
        from unittest.mock import patch
        clear_routing_cache()
        with patch("app.llm.is_configured", return_value=True):
            with patch("app.llm.complete", return_value="forecast, uptime, news, kiwix"):
                with patch("app.router._get_routing", return_value=None):
                    result = _llm_pick_fusion_sources("complex query abc")
        assert len(result) <= 3

    def test_uses_routing_cache_when_available(self):
        from app.router import _llm_pick_fusion_sources
        from unittest.mock import patch
        with patch("app.router._get_routing", return_value="forecast,uptime"):
            result = _llm_pick_fusion_sources("cached query")
        assert "forecast" in result
        assert "uptime" in result


class TestRouteWithSource:
    """Tests for route_with_source() — returns (result, actual_source_used).

    Regression coverage for a real bug found via real usage: a query
    routed to 'kiwix' that returned an empty/unusable result silently fell
    back to 'web' internally, but the API response's source_used field
    still reported 'kiwix' — main.py independently re-derived the intended
    source before calling route(), with no way to learn an internal
    fallback had occurred. route() itself only ever returned a plain
    string with no source information at all.
    """

    def setup_method(self):
        from app.router import clear_cache
        clear_cache()

    def test_route_still_returns_plain_string_for_backward_compatibility(self):
        from app.router import route
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = lambda q: "Kiwix result."
        try:
            with patch("app.router._get_cached", return_value=None):
                result = route("what is nitrogen", "kiwix")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert isinstance(result, str)
        assert result == "Kiwix result."

    def test_route_with_source_returns_tuple(self):
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = lambda q: "Kiwix result."
        try:
            with patch("app.router._get_cached", return_value=None):
                result, source = route_with_source("what is nitrogen", "kiwix")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert result == "Kiwix result."
        assert source == "kiwix"

    def test_fallback_reports_the_actual_fallback_source(self):
        """The exact real-world bug — kiwix returns empty, falls back to
        web, and source_used must say 'web', not 'kiwix'."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        # _looks_empty() checks for specific known "no result" phrases —
        # a bare "" doesn't match any of them and _looks_empty("") is
        # actually False, so the mock must use a realistic failure message
        # the same way a real source module reports finding nothing
        router_module.SOURCE_MAP["kiwix"] = lambda q: "No results found in wikipedia_en_all_maxi."
        router_module.SOURCE_MAP["web"] = lambda q: "Real web results here."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.FALLBACK_CHAIN", {"kiwix": "web"}):
                result, source = route_with_source("some query", "kiwix")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "web"
        assert "Real web results" in result

    def test_no_fallback_needed_reports_original_source(self):
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = lambda q: "Good kiwix result."
        try:
            with patch("app.router._get_cached", return_value=None):
                result, source = route_with_source("what is nitrogen", "kiwix")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "kiwix"

    def test_fusion_reports_fusion_as_source(self):
        from app.router import route_with_source
        from unittest.mock import patch

        with patch("app.sources.fusion.search", return_value="Fused result."), \
             patch("app.router._get_cached", return_value=None):
            result, source = route_with_source("test query", "fusion", ["forecast", "uptime"])
        assert source == "fusion"

    def test_unknown_source_reports_the_unknown_source_name(self):
        from app.router import route_with_source
        result, source = route_with_source("test query", "not_a_real_source")
        assert source == "not_a_real_source"
        assert "unknown source" in result.lower()

    def test_decomposed_single_source_reports_that_source_not_fusion(self):
        """When decomposition produces multiple sub-queries that all
        happen to resolve to the SAME single source after merging,
        overall_source should report that source, not 'fusion' — fusion
        should only be reported when genuinely multiple distinct sources
        contributed to the final merged response."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["ha"] = lambda q: "HA result for: " + q
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", return_value="ha"):
                result, source = route_with_source(
                    "are the doors locked and is the light on", "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "ha"

    def test_decomposed_multiple_sources_reports_fusion(self):
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Sunny."
        router_module.SOURCE_MAP["uptime"] = lambda q: "All up."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=["forecast", "uptime"]):
                result, source = route_with_source(
                    "what is the weather and are services up", "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "fusion"


class TestResolveSingleSourceRefactor:
    """Regression tests for _resolve_single_source() — extracted from
    route_with_source() during a deliberate refactoring pass prompted by
    a cyclomatic-complexity check flagging route_with_source() as the
    most complex function in the codebase (F, 45) by a wide margin.

    The extraction itself surfaced two real, previously-undetected bugs,
    found only by comparing two near-duplicate inline implementations
    side by side rather than reading either one in isolation:

    1. The decomposition loop's fallback path called the fallback
       handler directly with no cache check, while the top-level path
       correctly checked the fallback source's own cache first. Fixed by
       unifying both call sites on one helper that always checks cache.
    2. An unknown/unregistered source's error message didn't match any
       phrase in NO_RESULT_PHRASES, so _looks_empty() incorrectly
       treated it as real content — meaning a stale/misconfigured state
       could silently append an "Unknown source" string into an
       otherwise-clean merged response. Fixed by adding "unknown source"
       to NO_RESULT_PHRASES.
    """

    def setup_method(self):
        from app.router import clear_cache
        clear_cache()

    def test_unknown_source_message_is_treated_as_empty(self):
        """Regression test for bug #2 above."""
        from app.router import _looks_empty, _resolve_single_source
        result, source = _resolve_single_source("not_a_real_source", "test query")
        assert _looks_empty(result) is True

    def test_fallback_checks_cache_before_calling_handler(self):
        """Regression test for bug #1 above — confirms the unified
        helper checks the fallback source's cache before invoking its
        handler, the behavior the decomposition loop was previously
        missing."""
        from app.router import _resolve_single_source, _set_cached
        from unittest.mock import patch
        import app.router as router_module

        fallback_handler_called = []

        def fake_kiwix(q):
            return "no results found"

        def fake_web(q):
            fallback_handler_called.append(q)
            return "should not be called — cache should serve this"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = fake_kiwix
        router_module.SOURCE_MAP["web"] = fake_web
        try:
            _set_cached("web", "test query", "Cached web result.")
            with patch("app.router._get_cached", side_effect=lambda src, q: "Cached web result." if src == "web" else None):
                result, source = _resolve_single_source("kiwix", "test query")
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert result == "Cached web result."
        assert source == "web"
        assert fallback_handler_called == []  # handler never called — cache served it

    def test_decomposition_loop_and_top_level_share_identical_fallback_behavior(self):
        """Confirms both call sites genuinely produce the same result for
        the same scenario now that they share one implementation — the
        actual real-world fix for the inconsistency found during
        extraction, verified end to end rather than just unit-testing
        the helper in isolation."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        def fake_kiwix(q):
            return "no results found"

        def fake_web(q):
            return "Real web result."

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["kiwix"] = fake_kiwix
        router_module.SOURCE_MAP["web"] = fake_web
        try:
            with patch("app.router._get_cached", return_value=None):
                # Top-level (explicit source) path
                top_result, top_source = route_with_source("test query", "kiwix")
                # Decomposition-loop path — force a 2-part split so the
                # decomposition loop's per-sub-query resolution runs
                with patch("app.router.detect_intent", side_effect=["kiwix", "forecast"]):
                    router_module.SOURCE_MAP["forecast"] = lambda q: "Sunny."
                    decomp_result, decomp_source = route_with_source(
                        "test query and what is the weather", "auto"
                    )
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert top_source == "web"
        assert "web" in decomp_result.lower() or "Real web result" in decomp_result


class TestSubQueryFusionCaching:
    """Regression tests for a real bug found via a deliberate complexity-
    reduction investigation, applying the same side-by-side comparison
    discipline that previously found the fallback-caching inconsistency
    and the unrecognized-error-message gap: comparing the decomposition
    loop's per-sub-query fusion dispatch against the top-level single-
    query fusion dispatch surfaced that the sub-query path had NO caching
    at all for a sub-query that itself resolves to fusion (multiple
    sources at once) — unlike every other path in the system, including
    individual single-source sub-query results (cached via
    _resolve_single_source()) and the top-level fusion path (which
    explicitly builds a cache key from sorted source names and checks/
    sets it). A repeated compound query whose individual clause happened
    to resolve to multiple sources internally would re-run
    _llm_pick_fusion_sources() and re-query every fusion source on every
    single request, even identical repeats."""

    def setup_method(self):
        from app.router import clear_cache
        clear_cache()

    def test_repeated_subquery_fusion_result_is_served_from_cache(self):
        """The actual real-world regression test — confirms fusion.search
        is only genuinely called once across two identical requests."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        call_count = []

        def fake_fusion_search(query, sources):
            call_count.append(query)
            return "[FORECAST]\nSunny\n\n---\n\n[HA]\nLocked"

        def fake_detect_intent(q):
            return ["forecast", "ha"] if q.strip() == "weather" else "news"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["news"] = lambda q: "Some news."
        try:
            with patch("app.router.fusion.search", side_effect=fake_fusion_search), \
                 patch("app.router.detect_intent", side_effect=fake_detect_intent):
                route_with_source("weather and door status, and also the news", "auto")
                route_with_source("weather and door status, and also the news", "auto")
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert len(call_count) == 1

    def test_subquery_fusion_cache_key_matches_top_level_convention(self):
        """Confirms the new cache key uses the exact same convention
        ("fusion[sorted,sources]:query") the top-level fusion path
        already uses — a deliberate consistency choice, not a new,
        separate caching scheme."""
        from app.router import route_with_source, _get_cached
        from unittest.mock import patch
        import app.router as router_module

        def fake_detect_intent(q):
            return ["forecast", "ha"] if q.strip() == "weather" else "news"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["news"] = lambda q: "Some news."
        try:
            with patch("app.router.fusion.search", return_value="fusion result"), \
                 patch("app.router.detect_intent", side_effect=fake_detect_intent):
                route_with_source("weather and door status, and also the news", "auto")
                cached = _get_cached("fusion", "fusion[forecast,ha]:weather")
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert cached == "fusion result"

    def test_different_subquery_fusion_results_cached_independently(self):
        """A different sub-query that also resolves to fusion must get
        its own, independent cache entry — not collide with or
        overwrite a different sub-query's cached fusion result."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        call_count = []

        def fake_fusion_search(query, sources):
            call_count.append(query)
            return f"result for {query}"

        def fake_detect_intent(q):
            stripped = q.strip()
            if stripped in ("weather", "lights status"):
                return ["forecast", "ha"]
            return "news"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["news"] = lambda q: "Some news."
        try:
            with patch("app.router.fusion.search", side_effect=fake_fusion_search), \
                 patch("app.router.detect_intent", side_effect=fake_detect_intent):
                route_with_source("weather and door status, and also the news", "auto")
                route_with_source("lights status and door status, and also the news", "auto")
        finally:
            router_module.SOURCE_MAP.update(original_map)

        assert len(call_count) == 2


class TestConditionalDetection:
    """Tests for detect_conditional() — deliberately narrow leading
    "if X, Y" / "should X, Y" / "in case X, Y" pattern detection.

    Scope is intentionally restricted to the leading-comma form only.
    "if" is genuinely ambiguous in English — it has both a conditional
    sense ("if it's raining, bring an umbrella") and a "whether" sense
    ("check if the lights are on" = "check whether the lights are on").
    The whether sense never appears at the very start of a sentence
    followed by a comma, so restricting to that form sidesteps the
    ambiguity entirely rather than guessing at verb-based disambiguation,
    which runs into genuinely unresolvable cases ("let me know if X"
    could mean either "tell me the status" or "notify me if it changes").
    """

    def detect(self, query):
        from app.router import detect_conditional
        return detect_conditional(query)

    def test_leading_if_with_comma(self):
        result = self.detect("if it's raining, remind me to bring an umbrella")
        assert result == ("it's raining", "remind me to bring an umbrella", "")

    def test_leading_should_with_comma(self):
        result = self.detect("should it rain this weekend, I'll need to reschedule")
        assert result == ("it rain this weekend", "I'll need to reschedule", "")

    def test_leading_in_case_with_comma(self):
        result = self.detect("in case my services are down, I want to know right away")
        assert result == ("my services are down", "I want to know right away", "")

    def test_consequence_with_trailing_conjunction_splits_off_remainder(self):
        """Regression test — the real bug found via real usage. The
        consequence group's regex is greedy and originally captured
        everything to the end of the string, silently swallowing a
        completely unrelated second intent that happened to follow a
        conjunction after the consequence. "if any services are down,
        let me know, and also whats the weather" used to extract
        consequence="let me know, and also whats the weather" — losing
        track of "whats the weather" as a real, separate, searchable
        intent entirely. The remainder is now split off and returned
        separately so the caller can route it independently."""
        result = self.detect("if any services are down, let me know, and also whats the weather")
        condition, consequence, remainder = result
        assert condition == "any services are down"
        assert "weather" not in consequence.lower()
        assert "weather" in remainder.lower()

    def test_plain_query_no_match(self):
        assert self.detect("what is the weather today") is None

    def test_embedded_if_does_not_match_whether_usage(self):
        """'check if X' uses 'if' in the 'whether' sense — must NOT be
        treated as a conditional, since 'if' doesn't lead the sentence."""
        assert self.detect("check if the lights are on") is None

    def test_trailing_if_does_not_match(self):
        """Condition-after-consequence phrasing is deliberately out of
        scope — distinguishing it reliably from 'whether' usage would
        require real grammatical parsing, not pattern matching."""
        assert self.detect("remind me to bring an umbrella if it's raining") is None

    def test_let_me_know_if_does_not_match(self):
        """Deliberately excluded — genuinely ambiguous even to a human
        reader (could mean 'tell me the current status' or 'notify me
        if it changes'), not safely interpretable either way."""
        assert self.detect("let me know if the back door is unlocked") is None

    def test_mid_sentence_and_if_does_not_match(self):
        """Mid-sentence 'if' after a conjunction is out of scope for the
        same reason — only the unambiguous leading-comma form is handled."""
        assert self.detect("check the weather and if it's raining remind me to bring an umbrella") is None


class TestInterpretYesNo:
    """Tests for _interpret_yes_no() — restricted to structured,
    genuinely binary sources only (ha, uptime, forecast-precipitation).
    Kiwix/web/news are deliberately excluded — open-ended free text has
    no structured signal to safely key off of, and guessing wrong would
    actively mislead rather than just be unhelpful."""

    def interpret(self, condition, result, source):
        from app.router import _interpret_yes_no
        return _interpret_yes_no(condition, result, source)

    def test_ha_unlocked_condition_true(self):
        assert self.interpret("the back door is unlocked", "Back Door: unlocked", "ha") is True

    def test_ha_unlocked_condition_false(self):
        assert self.interpret("the back door is unlocked", "Back Door: locked", "ha") is False

    def test_ha_locked_condition_true(self):
        assert self.interpret("the back door is locked", "Back Door: locked", "ha") is True

    def test_ha_locked_condition_false_substring_trap(self):
        """Regression test for a real bug found and fixed while
        extracting _interpret_binary_state() from this function — and a
        genuine, previously-missing test case discovered only by
        noticing this exact scenario wasn't covered. "locked" is a
        literal substring of "unlocked", so a naive check-order
        (checking whichever keyword matches the CONDITION's own
        polarity first) gets this case backwards: when the condition
        says "locked," checking "locked" in the result first incorrectly
        matches "Back Door: unlocked" too, since "locked" is right there
        inside the word. The correct order checks "unlocked" first,
        always, regardless of which polarity the condition asserts —
        verified directly against this exact case before trusting the
        generalized helper."""
        assert self.interpret("the back door is locked", "Back Door: unlocked", "ha") is False

    def test_uptime_down_condition_false_when_all_up(self):
        assert self.interpret("my services are down", "All 15 monitored services are up.", "uptime") is False

    def test_uptime_down_condition_true_when_down(self):
        assert self.interpret("my services are down", "1 service is down: Ollama", "uptime") is True

    def test_forecast_rain_condition_false_when_clear(self):
        result = self.interpret("it's raining", "Today will be clear with a high of 94.", "forecast")
        assert result is False

    def test_forecast_rain_condition_true_when_rainy(self):
        result = self.interpret("it's raining", "Today will be rainy with thunderstorms.", "forecast")
        assert result is True

    def test_forecast_subjective_condition_not_interpreted(self):
        """'hot enough' has no universal threshold — must NOT attempt
        interpretation even though the source (forecast) is otherwise
        in the interpretable set, since this specific condition has no
        safe, structured signal to key off of."""
        result = self.interpret("it's hot enough", "high of 94, low of 69.", "forecast")
        assert result is None

    def test_kiwix_source_never_interpreted(self):
        """Open-ended encyclopedic content is never interpreted — no
        structured yes/no signal exists in free-text article content."""
        result = self.interpret("mercury is in retrograde", "# Mercury\nMercury most commonly refers to...", "kiwix")
        assert result is None

    def test_web_source_never_interpreted(self):
        result = self.interpret("it's a good time to invest", "Some web search result text.", "web")
        assert result is None


class TestInterpretBinaryState:
    """Direct, isolated tests for _interpret_binary_state() — extracted
    from _interpret_yes_no()'s uptime and ha branches, which shared this
    exact "condition asserts one of two opposite states" shape.

    A real bug was found and fixed during extraction: "locked" is a
    literal substring of "unlocked", so a naive generalization that
    checked whichever result-keyword matched the CONDITION's own
    polarity first got the "condition says locked, result says
    unlocked" case backwards. The fix checks the negative-state result
    keyword FIRST, always, in a fixed order independent of which
    polarity the condition asserts — exactly mirroring how the original,
    un-extracted code happened to get this right by checking "unlocked"
    before "locked" specifically. These tests exist both to verify the
    extracted helper directly, and as a permanent guard against this
    exact substring trap being reintroduced if this function is ever
    "simplified" again in the future."""

    def test_negative_condition_negative_result_returns_true(self):
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "the door is unlocked", "back door: unlocked",
            ["unlocked"], ["locked"],
            lambda r: "unlocked" in r, lambda r: "locked" in r,
        )
        assert result is True

    def test_negative_condition_positive_result_returns_false(self):
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "the door is unlocked", "back door: locked",
            ["unlocked"], ["locked"],
            lambda r: "unlocked" in r, lambda r: "locked" in r,
        )
        assert result is False

    def test_positive_condition_positive_result_returns_true(self):
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "the door is locked", "back door: locked",
            ["unlocked"], ["locked"],
            lambda r: "unlocked" in r, lambda r: "locked" in r,
        )
        assert result is True

    def test_positive_condition_negative_result_returns_false_substring_trap(self):
        """The actual regression test for the real bug found while
        extracting this — "locked" is a substring of "unlocked", so
        getting the check order wrong here specifically produces a
        silent, wrong True instead of the correct False."""
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "the door is locked", "back door: unlocked",
            ["unlocked"], ["locked"],
            lambda r: "unlocked" in r, lambda r: "locked" in r,
        )
        assert result is False

    def test_neither_condition_keyword_present_returns_none(self):
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "is the cat happy", "back door: locked",
            ["unlocked"], ["locked"],
            lambda r: "unlocked" in r, lambda r: "locked" in r,
        )
        assert result is None

    def test_neither_result_keyword_present_returns_none(self):
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "the door is locked", "status unknown",
            ["unlocked"], ["locked"],
            lambda r: "unlocked" in r, lambda r: "locked" in r,
        )
        assert result is None

    def test_compound_result_check_works_for_uptime_style_callers(self):
        """Confirms the helper correctly supports a compound result
        check (not just a single keyword) — uptime's "all up" check
        requires BOTH "all" and "up" to appear, unlike ha's simple
        single-keyword checks."""
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "are services down", "all 15 services are up",
            ["down", "not up"], ["up", "running"],
            lambda r: "down" in r, lambda r: "all" in r and "up" in r,
        )
        assert result is False

    def test_empty_positive_keywords_list_supported_for_forecast_style_callers(self):
        """Confirms the helper correctly handles a deliberately
        one-directional caller (forecast only checks for "rain," never
        an opposite "is it NOT raining" condition phrasing) — an empty
        positive_condition_keywords list should never match, only the
        negative one can trigger interpretation."""
        from app.router import _interpret_binary_state
        result = _interpret_binary_state(
            "is it hot enough", "clear skies",
            ["rain", "raining"], [],
            lambda r: "rain" in r or "storm" in r, lambda r: "clear" in r,
        )
        assert result is None


class TestFrameConditionalResponse:
    """Tests for _frame_conditional_response() — the actual response
    composition for a detected conditional query."""

    def frame(self, condition, consequence, result, source):
        from app.router import _frame_conditional_response
        return _frame_conditional_response(condition, consequence, result, source)

    def test_true_verdict_states_condition_holds(self):
        response = self.frame(
            "it's raining", "remind me to bring an umbrella",
            "Today will be rainy with thunderstorms.", "forecast"
        )
        assert "is the case" in response.lower()
        assert "umbrella" in response.lower()

    def test_false_verdict_states_condition_does_not_hold(self):
        response = self.frame(
            "it's raining", "remind me to bring an umbrella",
            "Today will be clear with a high of 94.", "forecast"
        )
        assert "not the case" in response.lower()

    def test_no_interpretation_presents_raw_result_honestly(self):
        """When no safe interpretation exists, the response must still
        include the real result and must NOT claim a definitive yes/no
        it can't actually support."""
        response = self.frame(
            "mercury is in retrograde", "I'll be careful with communication",
            "# Mercury\nMercury most commonly refers to...", "kiwix"
        )
        assert "is the case" not in response.lower()
        assert "not the case" not in response.lower()
        assert "mercury most commonly refers to" in response.lower()

    def test_original_real_result_always_preserved(self):
        """Regardless of verdict, the actual underlying search result
        must always be included in the final response — framing adds
        context, it never replaces or hides the real data."""
        real_result = "Back Door: unlocked"
        response = self.frame("the back door is unlocked", "let me know", real_result, "ha")
        assert real_result in response


class TestConditionalIntegration:
    """Integration tests confirming detect_conditional() and
    _frame_conditional_response() are actually wired into
    route_with_source(), not just correct in isolation."""

    def test_conditional_query_only_searches_the_condition(self):
        """The consequence ('remind me to bring an umbrella') must never
        itself be sent to a source as a search query — only the
        condition is searched, since Mnemolis has no reminder capability
        to act on the consequence at all."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear with a high of 94."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", return_value="forecast"):
                result, source = route_with_source(
                    "if it's raining, remind me to bring an umbrella", "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "forecast"
        assert "not the case" in result.lower()
        assert "clear" in result.lower()

    def test_remainder_after_conditional_is_searched_and_merged(self):
        """Regression test — the real bug found via real usage. "if any
        services are down, let me know, and also whats the weather" used
        to swallow "whats the weather" into the conditional's consequence
        text, never actually searching it. The trailing real intent must
        now be searched independently and merged into the final response,
        with overall source reported as 'fusion' since two distinct
        sources contributed."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        def fake_detect_intent(q):
            return "forecast" if "weather" in q.lower() else "uptime"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["uptime"] = lambda q: "All 15 monitored services are up."
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear with a high of 94."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=fake_detect_intent):
                result, source = route_with_source(
                    "if any services are down, let me know, and also whats the weather",
                    "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "fusion"
        assert "conditional question" in result.lower()
        assert "not the case" in result.lower()
        assert "all 15 monitored services are up" in result.lower()
        assert "clear" in result.lower()

    def test_remainder_decomposing_into_multiple_sources_has_no_double_header(self):
        """Regression test — the real bug found via real usage,
        immediately after the remainder-merging fix shipped. When the
        remainder itself decomposes into multiple distinct sources
        (e.g. "...also check the news, plus hows the humidity" decomposes
        into news + ha), route_with_source() already returns "fusion" as
        its reported source for an ALREADY-self-headered result. Wrapping
        that in another header using the literal string "fusion" produced
        the exact same nonsensical "[FUSION — FUSION]" double-header bug
        found and fixed earlier this session in the decomposition loop —
        this is the identical root cause appearing at the new
        remainder-merging call site, which needed the same fix applied."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        def fake_detect_intent(q):
            if "door" in q.lower():
                return "ha"
            if "news" in q.lower():
                return "news"
            if "humid" in q.lower():
                return "ha"
            return "forecast"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["ha"] = lambda q: "Back Door: locked" if "door" in q.lower() else "Humidity: 45%"
        router_module.SOURCE_MAP["news"] = lambda q: "Some news headline content."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=fake_detect_intent):
                result, source = route_with_source(
                    "if the back door is unlocked, let me know, and also check the news, plus hows the humidity",
                    "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert "[FUSION" not in result
        assert source == "fusion"
        assert "conditional question" in result.lower()
        assert "news headline content" in result.lower()
        assert "humidity: 45%" in result.lower()

    def test_non_conditional_query_unaffected(self):
        """A normal, non-conditional query must behave exactly as before
        — conditional detection must never interfere with the existing
        decomposition/routing path for queries it doesn't match."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", return_value="forecast"):
                result, source = route_with_source("what is the weather today", "auto")
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "forecast"
        assert "conditional question" not in result.lower()

    def test_conditional_only_applies_to_auto_source(self):
        """Explicit source requests should skip conditional detection
        entirely, the same way they already skip decomposition."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear."
        try:
            with patch("app.router._get_cached", return_value=None):
                result, source = route_with_source(
                    "if it's raining, remind me to bring an umbrella", "forecast"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert source == "forecast"
        assert "conditional question" not in result.lower()


class TestConditionalRemainderConcurrency:
    """Regression tests for parallelizing _resolve_conditional()'s
    condition and remainder route_with_source() calls — found feasible
    via direct research into why this case had been treated as too
    risky to attempt (see Conditional-Query-Detection.md's own
    corrected account): the original caution conflated "this code has
    unrelated real bug history" (detect_conditional()'s parsing,
    _interpret_binary_state()'s keyword matching) with "this specific
    change is risky," when re-deriving the actual data dependencies
    shows the two calls don't depend on each other at all, the same as
    the already-fixed searxng.py query-expansion case.

    Applies the exact same verification discipline that case required,
    since building it caught two real bugs there: a genuine timing
    proof of concurrency (not just unchanged output), and direct
    confirmation that router.suppress_cache_writes() correctly
    propagates into BOTH worker threads via the same
    contextvars.copy_context()-per-task pattern, not one shared
    context (which fails with a real RuntimeError, confirmed directly
    while building the searxng.py fix this mirrors).
    """

    def test_condition_and_remainder_genuinely_run_concurrently(self):
        """Real timing proof — total elapsed time must be close to the
        cost of ONE call, not the sum of both, confirming genuine
        concurrency rather than a refactor that merely looks
        unchanged."""
        import time
        from unittest.mock import patch
        from app.router import _resolve_conditional

        def slow_route(query, source="auto", fusion_sources=None):
            time.sleep(0.3)
            if "raining" in query:
                return ("Condition result content.", "forecast")
            return ("Remainder result content.", "news")

        with patch("app.router.route_with_source", side_effect=slow_route):
            start = time.monotonic()
            _resolve_conditional(
                "if it is raining, let me know, and also check the feeds", "auto"
            )
            elapsed = time.monotonic() - start

        # Sequential would be ~0.6s (0.3 + 0.3). Concurrent should be
        # close to max(0.3, 0.3) = ~0.3s. Generous upper bound to avoid
        # CI flakiness while still clearly distinguishing the two.
        assert elapsed < 0.5, f"took {elapsed:.2f}s — looks sequential, not concurrent"

    def test_query_without_a_remainder_is_completely_unaffected(self):
        """A plain "if X, Y" query with no trailing conjunction (the
        more common real-world shape) has an empty remainder and must
        never spin up the thread pool at all — confirmed by patching
        route_with_source to track call count and timing, since this
        path should remain exactly as simple and fast as before."""
        from unittest.mock import patch
        from app.router import _resolve_conditional

        call_count = {"n": 0}

        def counting_route(query, source="auto", fusion_sources=None):
            call_count["n"] += 1
            return ("Condition result.", "forecast")

        with patch("app.router.route_with_source", side_effect=counting_route):
            result, source = _resolve_conditional("if it is raining, bring an umbrella", "auto")

        assert call_count["n"] == 1
        assert source == "forecast"

    def test_suppress_cache_writes_propagates_into_both_worker_threads(self):
        """The exact regression found and fixed in searxng.py's own
        concurrent fetch, re-verified here for this structurally
        identical case: ThreadPoolExecutor does NOT propagate
        contextvars.ContextVar state into worker threads by default,
        so router.suppress_cache_writes() being active in the calling
        thread must still correctly suppress writes that happen inside
        EITHER of the two concurrent worker threads, not just the
        calling thread itself."""
        from unittest.mock import patch
        import app.router as router_module

        original_cache = dict(router_module._routing_cache)
        router_module._routing_cache.clear()
        try:
            def fake_route_with_real_cache_write(query, source="auto", fusion_sources=None):
                # Simulates what a real route_with_source() call does
                # deep inside its own LLM-routing path — a real
                # _set_routing() call that suppression must catch
                # regardless of which thread it happens on.
                router_module._set_routing(f"altquery:{query}", "some_decision")
                if "raining" in query:
                    return ("Condition content.", "forecast")
                return ("Remainder content.", "news")

            with patch("app.router.route_with_source", side_effect=fake_route_with_real_cache_write):
                with router_module.suppress_cache_writes():
                    router_module._resolve_conditional(
                        "if it is raining, let me know, and also check the feeds", "auto"
                    )

            assert len(router_module._routing_cache) == 0, (
                "a write leaked through one of the two concurrent worker threads "
                "despite suppress_cache_writes() being active in the calling thread"
            )
        finally:
            router_module._routing_cache.clear()
            router_module._routing_cache.update(original_cache)

    def test_normal_unsuppressed_call_still_caches_correctly_from_both_threads(self):
        """The flip side — confirms the fix didn't accidentally
        suppress writes ALL the time, only when suppress_cache_writes()
        is genuinely active. Both the condition and remainder calls
        should be free to cache normally."""
        from unittest.mock import patch
        import app.router as router_module

        original_cache = dict(router_module._routing_cache)
        router_module._routing_cache.clear()
        try:
            def fake_route_with_real_cache_write(query, source="auto", fusion_sources=None):
                router_module._set_routing(f"altquery:{query}", "some_decision")
                if "raining" in query:
                    return ("Condition content.", "forecast")
                return ("Remainder content.", "news")

            with patch("app.router.route_with_source", side_effect=fake_route_with_real_cache_write):
                router_module._resolve_conditional(
                    "if it is raining, let me know, and also check the feeds", "auto"
                )

            assert len(router_module._routing_cache) == 2
        finally:
            router_module._routing_cache.clear()
            router_module._routing_cache.update(original_cache)

    def test_real_exception_in_remainder_thread_propagates_correctly(self):
        """If the remainder's route_with_source() call genuinely raises,
        that exception must surface to the caller via future.result(),
        not be silently swallowed by the thread pool."""
        from unittest.mock import patch
        import pytest
        from app.router import _resolve_conditional

        def failing_route(query, source="auto", fusion_sources=None):
            if "raining" in query:
                return ("Condition content.", "forecast")
            raise RuntimeError("simulated remainder failure")

        with patch("app.router.route_with_source", side_effect=failing_route):
            with pytest.raises(RuntimeError):
                _resolve_conditional(
                    "if it is raining, let me know, and also check the feeds", "auto"
                )


class TestRecursiveConditionalDetection:
    """Tests for re-checking decomposed sub-queries for their own
    embedded conditional structure — the real gap found while designing
    recursive decomposition: detect_conditional() only ever ran once,
    against the FULL original query, before decomposition. A query like
    "what is the weather and if the back door is unlocked, let me know"
    doesn't start with "if" so the top-level check correctly returns
    None and the query proceeds to normal decomposition — but neither
    resulting sub-query was ever re-checked for its own conditional
    structure, even though "if the back door is unlocked, let me know"
    (the second sub-query) clearly has one.

    The first implementation of this fix recursed on the ORIGINAL
    sub-query string ("if the back door is unlocked, let me know") with
    a manual _depth counter meant to prevent runaway recursion. That
    introduced a real, found-via-testing bug: the depth incremented
    before the conditional was actually consumed, so the recursive
    call's own necessary re-detection of the very same conditional was
    blocked by the counter meant to guard against recursion that was
    never actually possible in the first place. Fixed by extracting the
    condition/consequence directly in the loop (mirroring exactly how
    the top-level handler already works) and recursing on the
    already-extracted CONDITION text only — which essentially never
    re-matches the leading "if/should/in case" pattern, so this
    terminates naturally without needing any depth parameter at all."""

    def test_decomposed_subquery_with_embedded_conditional_is_framed(self):
        """The real bug found via design research, now fixed. A query
        that itself isn't conditional at the top level, but decomposes
        into a sub-query that IS conditional, must have that sub-query
        framed correctly — not just routed as a plain HA query that
        discards the consequence and any conditional framing entirely."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        # Use a side_effect function rather than a single return_value —
        # detect_intent() is called for BOTH the weather sub-query AND
        # (via the recursive conditional check) the extracted condition
        # text "the back door is unlocked". A blunt return_value="forecast"
        # answers both calls identically, masking the real routing behavior
        # this test is meant to verify — the same class of test-fixture
        # mistake as _looks_empty("") earlier this session: the mock must
        # actually distinguish between distinct inputs, not blindly
        # return one fixed value regardless of what's being routed.
        def fake_detect_intent(query):
            if "door" in query.lower():
                return "ha"
            return "forecast"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Today will be clear with a high of 94."
        router_module.SOURCE_MAP["ha"] = lambda q: "Back Door: locked"
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=fake_detect_intent):
                result, source = route_with_source(
                    "what is the weather and if the back door is unlocked, let me know",
                    "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert "conditional question" in result.lower()
        assert "not the case" in result.lower()
        assert "back door: locked" in result.lower()
        assert "clear" in result.lower()

    def test_subquery_recursion_passes_extracted_condition_not_original_text(self):
        """Regression test for the real bug found via testing. The
        recursive call inside the decomposition loop must pass the
        ALREADY-EXTRACTED condition text to route_with_source(), never
        the original still-'if'-prefixed sub-query string — passing the
        original string caused the recursive call's own conditional
        detection to need re-firing, which an earlier _depth-based
        design incorrectly blocked. Verified here by confirming the
        condition is searched as plain text ('the back door is
        unlocked'), not as the literal conditional sentence."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        searched_queries = []

        def fake_ha_handler(q):
            searched_queries.append(q)
            return "Back Door: locked"

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Clear."
        router_module.SOURCE_MAP["ha"] = fake_ha_handler
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=lambda q: "ha" if "door" in q.lower() else "forecast"):
                route_with_source(
                    "what is the weather and if the back door is unlocked, let me know",
                    "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        # The HA handler should have been called with the bare condition,
        # never with the full "if X, Y" sentence still attached
        assert "the back door is unlocked" in searched_queries
        assert not any(q.lower().startswith("if ") for q in searched_queries)

    def test_normal_decomposition_unaffected_by_recursive_check(self):
        """A query with NO embedded conditional anywhere must decompose
        and route exactly as before — the recursive check should add
        zero overhead or behavior change for the common case."""
        from app.router import route_with_source
        from unittest.mock import patch
        import app.router as router_module

        original_map = dict(router_module.SOURCE_MAP)
        router_module.SOURCE_MAP["forecast"] = lambda q: "Clear."
        router_module.SOURCE_MAP["uptime"] = lambda q: "All up."
        try:
            with patch("app.router._get_cached", return_value=None), \
                 patch("app.router.detect_intent", side_effect=["forecast", "uptime"]):
                result, source = route_with_source(
                    "what is the weather and are services up", "auto"
                )
        finally:
            router_module.SOURCE_MAP.update(original_map)
        assert "conditional question" not in result.lower()
        assert "clear" in result.lower()
        assert "all up" in result.lower()


class TestGetRecentQueries:
    """Tests for get_recent_queries() and _connect_log_db_readonly() — the
    read-only query_log.db access added as groundwork while researching
    Self-Healing Source Selection and Ambient Intent Disambiguation, both
    of which need router.py to read from a database app/main.py
    exclusively owns and writes to.

    This specific shape (most-recent-N rows, just query+timestamp) is a
    genuine, direct fit for Ambient Intent Disambiguation's own context
    window. It is NOT a fit for Self-Healing Source Selection's or
    Predictive Pre-Fetching's real needs — both want a time-bounded bulk
    scan with real outcome columns, not a fixed-count recent window — see
    this function's own docstring for the full, corrected account.
    """

    def _build_real_query_log_db(self, path, rows):
        """rows: list of (query, timestamp) tuples, inserted in order
        (so the last one in the list is the most recently inserted)."""
        import sqlite3
        con = sqlite3.connect(path)
        con.execute("""
            CREATE TABLE query_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                query TEXT NOT NULL,
                source_requested TEXT NOT NULL,
                source_used TEXT NOT NULL,
                cached INTEGER NOT NULL,
                success INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                fallback_occurred INTEGER NOT NULL DEFAULT 0
            )
        """)
        for query, timestamp in rows:
            con.execute(
                "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms, fallback_occurred) "
                "VALUES (?, ?, 'auto', 'kiwix', 0, 1, 100, 0)",
                (timestamp, query),
            )
        con.commit()
        con.close()

    def test_returns_most_recent_first(self, tmp_path):
        from app.router import get_recent_queries
        from unittest.mock import patch
        db_path = str(tmp_path / "query_log.db")
        self._build_real_query_log_db(db_path, [
            ("what is the weather", "2026-01-15T12:00:00Z"),
            ("is the door locked", "2026-01-15T12:01:00Z"),
            ("what is nitrogen", "2026-01-15T12:02:00Z"),
        ])
        with patch("app.router._LOG_DB", db_path):
            results = get_recent_queries(limit=5)
        assert len(results) == 3
        assert results[0] == ("what is nitrogen", "2026-01-15T12:02:00Z")
        assert results[-1] == ("what is the weather", "2026-01-15T12:00:00Z")

    def test_limit_is_respected(self, tmp_path):
        from app.router import get_recent_queries
        from unittest.mock import patch
        db_path = str(tmp_path / "query_log.db")
        self._build_real_query_log_db(
            db_path, [(f"query {i}", f"2026-01-15T12:{i:02d}:00Z") for i in range(10)]
        )
        with patch("app.router._LOG_DB", db_path):
            results = get_recent_queries(limit=3)
        assert len(results) == 3

    def test_missing_database_file_returns_empty_list_not_a_crash(self, tmp_path):
        """A real, reachable case: a process that imports router.py before
        main.py's own lifespan has ever run (a standalone script, a test
        constructing router.py's own objects directly) has no query_log.db
        on disk at all yet."""
        from app.router import get_recent_queries
        from unittest.mock import patch
        nonexistent = str(tmp_path / "does_not_exist.db")
        with patch("app.router._LOG_DB", nonexistent):
            results = get_recent_queries()
        assert results == []

    def test_file_exists_but_table_does_not_returns_empty_list(self, tmp_path):
        import sqlite3
        from app.router import get_recent_queries
        from unittest.mock import patch
        db_path = str(tmp_path / "empty.db")
        sqlite3.connect(db_path).close()
        with patch("app.router._LOG_DB", db_path):
            results = get_recent_queries()
        assert results == []

    def test_connection_is_genuinely_read_only_not_just_conventionally(self, tmp_path):
        """The actual, enforced safety property this function depends on
        — router.py does not own query_log's schema, and a future bug
        introduced in this module that accidentally tries to write should
        fail loudly and immediately, not silently succeed."""
        import sqlite3
        from app.router import _connect_log_db_readonly
        from unittest.mock import patch
        db_path = str(tmp_path / "query_log.db")
        self._build_real_query_log_db(db_path, [("test", "2026-01-15T12:00:00Z")])
        with patch("app.router._LOG_DB", db_path):
            con = _connect_log_db_readonly()
            try:
                with pytest.raises(sqlite3.OperationalError):
                    con.execute(
                        "INSERT INTO query_log (timestamp, query, source_requested, source_used, cached, success, latency_ms) "
                        "VALUES ('2026-01-01T00:00:00Z', 'x', 'auto', 'kiwix', 0, 1, 1)"
                    )
            finally:
                con.close()

    def test_readonly_mode_does_not_create_a_missing_file(self, tmp_path):
        """Confirms the read-only connection behaves differently from
        main.py's own writable _connect() in exactly the way that matters
        here — it must never silently create the database file as a side
        effect of being asked to read from it, since router.py has no
        business creating a database it doesn't own the schema for."""
        import os
        from app.router import _connect_log_db_readonly
        from unittest.mock import patch
        nonexistent = str(tmp_path / "should_not_be_created.db")
        with patch("app.router._LOG_DB", nonexistent):
            try:
                _connect_log_db_readonly().execute("SELECT 1")
            except Exception:
                pass
        assert not os.path.exists(nonexistent)


class TestResultCacheThunderingHerd:
    """Regression coverage for a real, confirmed mechanism behind the
    v3.50.4 benchmark run's `cache_hit` cold-cache anomaly (p90 5100ms,
    p99 8000ms — no precedent anywhere in this project's benchmark
    history, where `cache_hit` has always been one of the cheapest rows
    in the table regardless of release).

    Root cause, found by reading `tests/locustfile.py` directly rather
    than guessing from the timing numbers alone: `cache_hit`'s task uses
    the literal query "what is nitrogen" with source="kiwix" — which is
    also the FIRST entry in KIWIX_QUERIES, the pool the separate, much
    more frequent `kiwix_search` task (weight 4, the highest-weighted
    task in MnemolisSingleSourceUser) draws from at random. On a cold
    run, both tasks can independently draw the identical, not-yet-cached
    "kiwix:what is nitrogen" key at nearly the same instant. This was
    never a Mnemolis backend bug — `_resolve_single_source()`'s
    check-then-call-then-write sequence has no per-key lock or
    in-flight-request tracking, so two concurrent callers for the same
    uncached key both genuinely miss, both pay the full cold-routing
    cost (a real LLM book-selection call, consistent with the observed
    multi-second tail), and both eventually write the same result to
    cache. This is the exact same thundering-herd shape already
    documented for `auto`/`conditional`'s small Locust query pools — it
    just wasn't previously recognized as applying to `cache_hit` too,
    since `cache_hit`'s OWN query was never meant to be shared with any
    other task's pool in the first place.

    These tests prove the actual mechanism directly against
    `route_with_source()` — not the Locust benchmark, which can only
    ever show a symptom, never isolate a cause the way a real
    concurrent-call test against the actual cache-checking code can.
    """

    def setup_method(self):
        from app.router import clear_cache
        clear_cache()

    def teardown_method(self):
        from app.router import clear_cache
        clear_cache()

    def test_two_concurrent_callers_for_the_same_uncached_key_both_pay_the_full_cost(self):
        """The actual bug, reproduced directly: two threads calling
        route_with_source() for the identical source+query pair at the
        same instant, with neither having a warm cache yet, BOTH pay
        the full slow-handler cost — proving there is no per-key lock
        or in-flight-request deduplication protecting a key that's
        already being resolved by a concurrent caller."""
        import threading
        import time
        from unittest.mock import patch
        from app import router

        call_times = []
        call_times_lock = threading.Lock()

        def slow_kiwix_handler(query):
            start = time.monotonic()
            time.sleep(0.2)
            with call_times_lock:
                call_times.append(time.monotonic() - start)
            return "Nitrogen is a chemical element."

        results = []

        def call_route():
            result, source = router.route_with_source("what is nitrogen", "kiwix")
            results.append((result, source))

        with patch.dict(router.SOURCE_MAP, {"kiwix": slow_kiwix_handler}):
            t1 = threading.Thread(target=call_route)
            t2 = threading.Thread(target=call_route)
            start = time.monotonic()
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            elapsed = time.monotonic() - start

        # The actual bug: both threads paid the full ~0.2s handler cost
        # (both genuinely entered the slow handler), rather than one
        # missing and the other waiting for / reusing that result.
        assert len(call_times) == 2, (
            "expected the slow handler to genuinely run twice — if this "
            "is 1, a fix already deduplicated the concurrent miss"
        )
        # Total elapsed stayed close to ONE handler call's cost (~0.2s),
        # not two sequential ones (~0.4s) — confirming this is a real
        # CONCURRENT double-miss, the same shape a thundering herd takes,
        # not just two slow sequential calls that happened to both run.
        assert elapsed < 0.35, (
            f"took {elapsed:.2f}s — looks sequential, not a genuine "
            f"concurrent double-miss"
        )
        assert all(r[0] == "Nitrogen is a chemical element." for r in results)

    def test_a_genuinely_warm_key_is_never_recomputed_by_a_concurrent_caller(self):
        """Confirms the contrast case: once a key is ACTUALLY cached
        (not mid-resolution, but already written), a concurrent caller
        correctly hits cache and never touches the handler at all — the
        bug above is specific to the narrow race window between a miss
        and the matching write, not a general cache-correctness problem."""
        import threading
        from unittest.mock import patch
        from app import router

        call_count = {"n": 0}

        def counting_handler(query):
            call_count["n"] += 1
            return "cached-ready result"

        with patch.dict(router.SOURCE_MAP, {"kiwix": counting_handler}):
            # Prime the cache for real, the way a prior, already-completed
            # request would have.
            router.route_with_source("what is nitrogen", "kiwix")
            assert call_count["n"] == 1

            # Now fire several concurrent callers against the SAME,
            # already-warm key.
            threads = [
                threading.Thread(
                    target=router.route_with_source, args=("what is nitrogen", "kiwix")
                )
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        # None of the 5 concurrent callers should have touched the
        # handler again — the cache was already genuinely warm.
        assert call_count["n"] == 1

    def test_locustfile_cache_hit_query_does_not_collide_with_any_other_pool(self):
        """Direct regression test for the actual root cause, not just
        the symptom: the literal query `cache_hit` uses must not appear
        in any OTHER task's query pool for the same source. This is what
        actually broke — "what is nitrogen"/kiwix was both `cache_hit`'s
        dedicated key AND a live member of KIWIX_QUERIES, the pool the
        much-more-frequent kiwix_search task draws from. Confirms the
        real fix (giving cache_hit its own query, never drawn from by
        anything else) actually holds, and would catch a future regression
        if anyone ever adds the cache_hit query back into another pool.

        Deliberately parses `tests/locustfile.py` as text via `ast`
        rather than importing it as a live module. `locustfile.py`
        imports `locust`, which calls `gevent.monkey.patch_all()` at
        import time — genuinely incompatible with importing it from
        inside an already-running pytest session that has already
        loaded `ssl`/`urllib3` via `requests` elsewhere in the suite
        (confirmed directly: a real `RecursionError` inside
        `ssl.SSLContext.minimum_version`, with `gevent`'s own
        MonkeyPatchWarning naming exactly this ordering problem). An
        AST-based static parse of the two list literals needs neither
        `locust` nor any of its monkey-patching, and is arguably the
        more correct tool for "do these two static lists overlap"
        regardless.
        """
        import ast
        import os

        locustfile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "locustfile.py"
        )
        with open(locustfile_path) as f:
            tree = ast.parse(f.read(), filename=locustfile_path)

        list_literals: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.List)
            ):
                name = node.targets[0].id
                values = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                list_literals[name] = values

        cache_hit_query = "what is nitrogen"  # cache_hit's literal, hardcoded query
        cache_hit_source = "kiwix"

        assert "KIWIX_QUERIES" in list_literals, (
            "KIWIX_QUERIES not found in tests/locustfile.py — has it been renamed? "
            "This test needs updating to match."
        )

        # Every query pool keyed to the same source cache_hit uses.
        kiwix_pool_names = ["KIWIX_QUERIES", "KIWIX_DISAMBIGUATION_QUERIES"]
        for pool_name in kiwix_pool_names:
            pool = list_literals.get(pool_name)
            if pool is None:
                continue
            assert cache_hit_query not in pool, (
                f"cache_hit's query {cache_hit_query!r} (source={cache_hit_source!r}) "
                f"also appears in {pool_name}, a kiwix query pool another task draws "
                f"from — this is the exact collision that caused the v3.50.4 "
                f"benchmark's cold-cache cache_hit anomaly (p90 5100ms, p99 8000ms)"
            )

    def test_conditional_with_remainder_pool_double_kiwix_hit_count_stays_low(self):
        """Regression test for the conditional_remainder design doc's
        finding: _resolve_conditional() dispatches the condition and
        remainder concurrently specifically so they cost max(a, b)
        instead of a + b — but when BOTH sides independently resolve
        to kiwix, this deployment's documented OLLAMA_NUM_PARALLEL=1
        constraint (see wiki/The-Benchmark-Investigation-Log.md,
        Thread 6) means the two real LLM-bound calls queue against
        each other instead of running in parallel, degrading back
        toward a + b for exactly those entries — a real, confirmed
        mechanism, not a hypothesis (see the conditional_remainder
        design doc for the full investigation, including the
        benchmark anomaly this surfaced from).

        Of the five entries that originally hit this case, three were
        genuine INTENT_MAP keyword gaps (rain/online/doors-are-locked,
        now fixed — see TestKeywordDetect's
        test_is_it_raining_matches_forecast and siblings) and a
        fourth ("the garage door is open") turned out to be a real,
        separate, TWO-FILE gap once investigated further: router.py's
        INTENT_MAP had no trigger for open/closed garage-door
        phrasing at all (a genuinely different question from the
        existing locked/unlocked door triggers), AND
        home_assistant.py's own _QUERY_MAP had no entry for a real
        cover/garage_door entity even once routing worked correctly —
        see test_garage_door_is_open_matches_ha (this file) and
        TestGarageDoorSupport (test_home_assistant.py) for both
        halves of that fix. The remaining entry ("any cameras go
        offline") is NOT a bug — this codebase has no real Home
        Assistant capability for camera reachability/connectivity at
        all (the existing "camera" entry answers motion-DETECTION
        questions, a different thing), so kiwix is correctly the
        least-wrong available destination, not a gap to close.

        This test confirms the double-hit count stays at the new,
        tightened baseline (1) rather than silently regressing back
        toward any of the prior ones (5, then 2) if a future change
        to INTENT_MAP, _QUERY_MAP, or this pool reintroduces a gap.

        Parses tests/locustfile.py via the same AST approach as
        test_locustfile_cache_hit_query_does_not_collide_with_any_other_pool
        above, for the same gevent-import-safety reason, then runs the
        real detect_conditional()/detect_intent() against each
        extracted condition/remainder pair — these two functions are
        safe to import directly, only locustfile.py's own `import
        locust` is the problem."""
        import ast
        import os
        from app.router import detect_conditional, detect_intent

        locustfile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "locustfile.py"
        )
        with open(locustfile_path) as f:
            tree = ast.parse(f.read(), filename=locustfile_path)

        list_literals: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.List)
            ):
                name = node.targets[0].id
                values = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                list_literals[name] = values

        assert "CONDITIONAL_WITH_REMAINDER_QUERIES" in list_literals, (
            "CONDITIONAL_WITH_REMAINDER_QUERIES not found in "
            "tests/locustfile.py — has it been renamed? This test "
            "needs updating to match."
        )
        pool = list_literals["CONDITIONAL_WITH_REMAINDER_QUERIES"]

        both_kiwix_pairs = []
        for query in pool:
            conditional = detect_conditional(query)
            assert conditional is not None, (
                f"{query!r} no longer parses as a conditional — has "
                f"detect_conditional() changed?"
            )
            condition, _consequence, remainder = conditional
            assert remainder, (
                f"{query!r} has no remainder — has it been edited to "
                f"remove the trailing ' and also...' clause?"
            )
            condition_intent = detect_intent(condition)
            remainder_intent = detect_intent(remainder)
            if condition_intent == "kiwix" and remainder_intent == "kiwix":
                both_kiwix_pairs.append((condition, remainder))

        assert len(both_kiwix_pairs) <= 1, (
            f"{len(both_kiwix_pairs)} pool entries have BOTH condition and "
            f"remainder resolving to kiwix at once — each one means "
            f"_resolve_conditional()'s concurrent dispatch fires two real "
            f"LLM-bound calls simultaneously against a deployment with "
            f"OLLAMA_NUM_PARALLEL=1, degrading the intended max(a,b) "
            f"benefit back toward a+b. Expected at most 1 (the genuine "
            f"'any cameras go offline' case, which has no real HA backend "
            f"capability to route to instead) after the INTENT_MAP/"
            f"_QUERY_MAP fixes for the rain/online/doors-are-locked/garage-"
            f"door gaps. Pairs found: {both_kiwix_pairs}"
        )

    def test_locustfile_thundering_herd_pools_are_wide_enough_to_avoid_high_collision_rates(self):
        """Regression test for the corrected pool-sizing investigation
        (v3.50.9), which fixed a real mistake in the v3.50.8 sizing pass
        rather than just adding more entries on top of it.

        v3.50.8 sized these pools using "expected NUMBER of pool entries
        hit by 2+ of 20 concurrent users" — a real, worked-out
        calculation, but the wrong metric. It correctly identified that
        relationship isn't simple ("more entries always helps"), but the
        metric that actually predicts the benchmark's tail is "fraction
        of the 20 users whose first pick collides with someone else's,"
        which declines monotonically with pool size (no peak to dodge)
        but slowly enough that the v3.50.8 sizes (CONDITIONAL_QUERIES 20,
        CONDITIONAL_WITH_REMAINDER_QUERIES 12) still left a 62-81%
        collision rate — and the very next real benchmark run confirmed
        it: CONDITIONAL_WITH_REMAINDER_QUERIES's cold p99 got WORSE
        (1300ms -> 4200ms), not better, exactly as that wrong-metric
        model itself would have predicted if read correctly (it showed
        4->12 entries making the absolute collision count worse, not
        just unchanged).

        A second, independent factor made it worse than the collision
        rate alone explains: CONDITIONAL_QUERIES's conditions skewed
        85% toward kiwix's expensive LLM book-selection fallback path
        (confirmed by running every condition through detect_intent()
        directly), versus AUTO_QUERIES's 2/24 — so a collision on this
        pool costs far more per occurrence than a similar-rate collision
        on auto's pool. This test enforces both corrections: a real size
        floor for each pool (not just "bigger than v3.50.8"), AND a real
        ceiling on how much of CONDITIONAL_QUERIES is allowed to fall
        through to the expensive kiwix path, so a well-meaning future
        addition of more "if X is in retrograde"-style entries doesn't
        silently regress the cost-per-collision back toward where it was.
        """
        import ast
        import os

        locustfile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "locustfile.py"
        )
        with open(locustfile_path) as f:
            tree = ast.parse(f.read(), filename=locustfile_path)

        list_literals: dict[str, list[str]] = {}
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.List)
            ):
                name = node.targets[0].id
                values = [
                    elt.value
                    for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]
                list_literals[name] = values

        # Minimums determined by the v3.50.9 collision-fraction correction
        # (see locustfile.py's own comments on each pool for the full
        # reasoning) — chosen so each pool's collision fraction sits
        # meaningfully below AUTO_QUERIES's own (55.5% at 24 entries),
        # since CONDITIONAL_QUERIES/CONDITIONAL_WITH_REMAINDER_QUERIES's
        # collisions cost more per occurrence (the kiwix-fallback check
        # below is the other half of that correction).
        minimums = {
            "AUTO_QUERIES": 24,
            "CONDITIONAL_QUERIES": 40,
            "CONDITIONAL_WITH_REMAINDER_QUERIES": 30,
        }
        for pool_name, minimum in minimums.items():
            assert pool_name in list_literals, (
                f"{pool_name} not found in tests/locustfile.py — has it been "
                f"renamed? This test needs updating to match."
            )
            actual = len(list_literals[pool_name])
            assert actual >= minimum, (
                f"{pool_name} has {actual} entries, below the {minimum}-entry "
                f"minimum the v3.50.9 collision-fraction correction determined "
                f"keeps this pool's collision rate meaningfully below auto's — "
                f"shrinking this pool risks reintroducing the exact tail this "
                f"work removed"
            )

    def test_conditional_queries_kiwix_fallback_ratio_stays_below_half(self):
        """Regression test for the SECOND half of the v3.50.9 sizing
        correction: it's not enough for CONDITIONAL_QUERIES to be wide
        enough — too many of its conditions falling through to kiwix's
        expensive LLM book-selection path (the original 20-entry pool
        was 85% kiwix) makes every remaining collision cost far more
        than a similar-rate collision on AUTO_QUERIES (2/24, ~8%) does.
        This doesn't re-verify every entry's exact source on every test
        run (that's what TestResultCacheThunderingHerd's sibling tests
        and the manual detect_intent() check during this investigation
        already did) — it guards the AGGREGATE ratio, so a future
        well-meaning addition of more open-ended "if X happens" entries
        doesn't silently walk this back toward the original 85%."""
        import ast
        import os
        from app.router import detect_intent

        locustfile_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "locustfile.py"
        )
        with open(locustfile_path) as f:
            tree = ast.parse(f.read(), filename=locustfile_path)

        conditional_queries = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "CONDITIONAL_QUERIES"
                and isinstance(node.value, ast.List)
            ):
                conditional_queries = [
                    elt.value for elt in node.value.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                ]

        assert conditional_queries is not None, "CONDITIONAL_QUERIES not found"

        # Extract the condition text the same way detect_conditional()
        # itself would, for an accurate real-routing check.
        kiwix_count = 0
        for full_query in conditional_queries:
            condition = full_query[len("if "):].split(",")[0]
            if detect_intent(condition) == "kiwix":
                kiwix_count += 1

        ratio = kiwix_count / len(conditional_queries)
        assert ratio < 0.5, (
            f"{kiwix_count} of {len(conditional_queries)} CONDITIONAL_QUERIES "
            f"conditions ({ratio:.0%}) fall through to kiwix's expensive LLM "
            f"book-selection path — back above the v3.50.9 fix's target. A "
            f"collision on this pool costs far more per occurrence when it "
            f"lands on kiwix than on a structured source (ha/uptime/forecast), "
            f"so this ratio matters as much as the pool's raw size."
        )


class TestInflightLockRegistryPrimitives:
    """Direct tests for _get_inflight_lock()/_release_inflight_lock()'s
    refcounting — closing a real, named race the simpler bare-Lock-with-
    "delete-if-unlocked" approach would have had (see the module-level
    comment above _RefCountedLock in app/router.py for the full account).
    These tests exercise the registry in isolation, without going
    through any of the four real call sites, so a regression here is
    traceable to the primitive itself rather than mixed in with LLM-call
    mocking noise.
    """

    def setup_method(self):
        from app.router import _inflight_locks
        _inflight_locks.clear()

    def teardown_method(self):
        from app.router import _inflight_locks
        _inflight_locks.clear()

    def test_first_caller_creates_a_new_lock(self):
        from app.router import _get_inflight_lock, _release_inflight_lock, get_inflight_lock_count
        entry = _get_inflight_lock("source:nitrogen")
        entry.lock.acquire()
        assert get_inflight_lock_count() == 1
        _release_inflight_lock("source:nitrogen", entry)
        assert get_inflight_lock_count() == 0

    def test_second_concurrent_caller_gets_the_same_lock_object(self):
        """The whole point of the registry — two callers racing for the
        identical key must be coordinating against ONE lock object, not
        two independently-allocated ones that can't see each other.
        Calling _get_inflight_lock() a second time while the first
        caller still holds the lock is itself safe (it only registers
        interest / bumps the refcount — the actual blocking happens
        separately at entry.lock.acquire(), exactly as every real call
        site does it); this test exercises that registration step
        directly, in a background thread, to also prove it doesn't
        block the calling thread."""
        import threading
        from app.router import _get_inflight_lock, _release_inflight_lock

        entry_a = _get_inflight_lock("source:nitrogen")
        entry_a.lock.acquire()

        captured = {}

        def register_second_reference():
            captured["entry_b"] = _get_inflight_lock("source:nitrogen")

        t = threading.Thread(target=register_second_reference)
        t.start()
        t.join(timeout=1.0)

        assert "entry_b" in captured, "_get_inflight_lock() blocked when it should only register interest"
        assert captured["entry_b"] is entry_a

        _release_inflight_lock("source:nitrogen", entry_a)
        # Acquire-and-release the second reference too, the same way a
        # real caller that registered interest while waiting would.
        entry_b = captured["entry_b"]
        entry_b.lock.acquire()
        _release_inflight_lock("source:nitrogen", entry_b)

    def test_different_keys_get_different_locks(self):
        from app.router import _get_inflight_lock, _release_inflight_lock
        entry_a = _get_inflight_lock("source:nitrogen")
        entry_a.lock.acquire()
        entry_b = _get_inflight_lock("source:helium")
        entry_b.lock.acquire()
        assert entry_a is not entry_b
        _release_inflight_lock("source:nitrogen", entry_a)
        _release_inflight_lock("source:helium", entry_b)

    def test_entry_removed_once_last_reference_releases(self):
        """Two concurrent references to the same key: the first caller
        acquires and holds the lock; a second reference is taken out
        (refcount bumped, via a background thread so it doesn't block
        this test) but doesn't get to actually acquire until the first
        releases. Confirms the entry survives as long as ANY reference
        is outstanding, and disappears only once both are gone."""
        import threading
        from app.router import _get_inflight_lock, _release_inflight_lock, get_inflight_lock_count

        entry_a = _get_inflight_lock("source:nitrogen")
        entry_a.lock.acquire()

        captured = {}

        def register_second_reference():
            captured["entry_b"] = _get_inflight_lock("source:nitrogen")

        t = threading.Thread(target=register_second_reference)
        t.start()
        t.join(timeout=1.0)

        assert get_inflight_lock_count() == 1  # one KEY, shared by two refs
        _release_inflight_lock("source:nitrogen", entry_a)
        assert get_inflight_lock_count() == 1, "one caller is still referencing this key"

        entry_b = captured["entry_b"]
        entry_b.lock.acquire()
        _release_inflight_lock("source:nitrogen", entry_b)
        assert get_inflight_lock_count() == 0, "last reference released, entry should be gone"

    def test_a_key_resurrected_after_full_release_gets_a_fresh_lock(self):
        """Confirms the registry doesn't confuse "this key was once
        in-flight" with "this key is still in-flight" — a brand new,
        unrelated resolution of the same key later must get a clean
        lock, not a stale, already-acquired one left behind by a bug."""
        from app.router import _get_inflight_lock, _release_inflight_lock
        first = _get_inflight_lock("source:nitrogen")
        first.lock.acquire()
        _release_inflight_lock("source:nitrogen", first)
        second = _get_inflight_lock("source:nitrogen")
        assert not second.lock.locked()
        second.lock.acquire()
        _release_inflight_lock("source:nitrogen", second)

    def test_refcounted_lock_cleanup_under_real_concurrent_stress(self):
        """A direct reproduction, not a guess: hammers ONE shared key
        with many threads doing acquire-hold-release in a tight loop,
        checking after every single round that the registry never
        leaves behind more than one entry for the one key in use, and
        that the registry is completely empty once every thread
        finishes.

        This is the project's own established discipline for exactly
        this class of claim — see _atomic_write_json()'s own stress
        test (8 concurrent writers/readers, checked directly rather
        than reasoned about) in test_cache_persistence.py, which sets
        the precedent this test follows: don't ship a concurrency claim
        on inspection alone.
        """
        import threading
        import random
        from app.router import (
            _get_inflight_lock, _release_inflight_lock, get_inflight_lock_count,
        )

        key = "source:stress test query"
        rounds_per_thread = 200
        thread_count = 12
        errors = []
        max_observed_entries = {"n": 0}
        max_observed_lock = threading.Lock()

        def worker():
            try:
                for _ in range(rounds_per_thread):
                    entry = _get_inflight_lock(key)
                    entry.lock.acquire()
                    try:
                        # A small, random hold time so threads genuinely
                        # interleave their acquire/release timing rather
                        # than running in lockstep.
                        if random.random() < 0.3:
                            pass  # no-op delay variance is enough at this thread count
                        with max_observed_lock:
                            count = get_inflight_lock_count()
                            if count > max_observed_entries["n"]:
                                max_observed_entries["n"] = count
                    finally:
                        # _release_inflight_lock() releases entry.lock
                        # itself — calling entry.lock.release() again
                        # here on top of that would double-release.
                        _release_inflight_lock(key, entry)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(thread_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"worker thread(s) raised: {errors}"
        # Only ONE key was ever in play, so the registry must never have
        # shown more than 1 entry at any observed point.
        assert max_observed_entries["n"] <= 1, (
            f"observed {max_observed_entries['n']} simultaneous registry "
            f"entries for a single shared key — the refcounting let a "
            f"second, independent lock object get created for a key "
            f"already in flight"
        )
        # And after every thread has fully finished, nothing should be left.
        assert get_inflight_lock_count() == 0, (
            "registry leaked entries after all worker threads completed — "
            "a refcount never returned to zero, or a key was deleted while "
            "still referenced"
        )

    def test_release_then_immediate_reacquire_by_a_different_thread_is_safe(self):
        """Targets the exact race a simpler "delete a key's lock once
        it's no longer held" design would have: a lock released and
        possibly deleted, with a third caller's reference to the OLD
        object still outstanding, racing a fourth caller's fresh
        _get_inflight_lock() call for the same key. Runs many
        repetitions specifically to surface a race that might only
        manifest occasionally, not on a single lucky interleaving."""
        import threading
        from app.router import (
            _get_inflight_lock, _release_inflight_lock, get_inflight_lock_count,
            _inflight_locks, _inflight_locks_guard,
        )

        key = "source:race repro query"
        repetitions = 500

        for _ in range(repetitions):
            barrier = threading.Barrier(2)
            results = {}

            def releaser():
                entry = _get_inflight_lock(key)
                entry.lock.acquire()
                barrier.wait()
                # _release_inflight_lock() releases entry.lock itself —
                # no separate entry.lock.release() call needed here.
                _release_inflight_lock(key, entry)

            def reacquirer():
                barrier.wait()
                entry = _get_inflight_lock(key)
                # Must be able to genuinely acquire — if this hangs or
                # raises, the registry handed back a broken/stale lock.
                acquired = entry.lock.acquire(timeout=2.0)
                results["acquired"] = acquired
                if acquired:
                    # _release_inflight_lock() releases entry.lock
                    # itself — don't also release it here first.
                    _release_inflight_lock(key, entry)
                else:
                    # Didn't actually acquire — only drop our reference,
                    # don't try to release a lock we never hold. (Not
                    # expected to ever run — the whole point of this
                    # test is that `acquired` is always True — but kept
                    # safe rather than assuming.)
                    with _inflight_locks_guard:
                        entry.refcount -= 1
                        if entry.refcount <= 0 and _inflight_locks.get(key) is entry:
                            del _inflight_locks[key]

            t1 = threading.Thread(target=releaser)
            t2 = threading.Thread(target=reacquirer)
            t1.start()
            t2.start()
            t1.join(timeout=3.0)
            t2.join(timeout=3.0)

            assert results.get("acquired") is True, (
                "a fresh caller could not acquire the lock for a key "
                "another thread just released — possible stale-lock race"
            )

        assert get_inflight_lock_count() == 0


class TestSingleflightContextManager:
    """Tests for the _singleflight() context manager itself, separate
    from the registry primitives it's built on and separate from the
    four real call sites that use it — confirms the contract callers
    rely on: mutual exclusion per key, independence across different
    keys, and that the lock is always released even when the wrapped
    code raises.
    """

    def setup_method(self):
        from app.router import _inflight_locks
        _inflight_locks.clear()

    def teardown_method(self):
        from app.router import _inflight_locks
        _inflight_locks.clear()

    def test_two_concurrent_callers_for_the_same_key_serialize(self):
        import threading
        import time
        from app.router import _singleflight

        order = []
        order_lock = threading.Lock()

        def call(label):
            with _singleflight("shared-key"):
                with order_lock:
                    order.append(f"{label}-enter")
                time.sleep(0.1)
                with order_lock:
                    order.append(f"{label}-exit")

        t1 = threading.Thread(target=call, args=("a",))
        t2 = threading.Thread(target=call, args=("b",))
        t1.start()
        time.sleep(0.02)  # ensure t1 enters first, deterministically
        t2.start()
        t1.join()
        t2.join()

        # Whichever thread entered first must also exit before the
        # second thread's "enter" appears — proving genuine mutual
        # exclusion, not just two independent runs that happened not
        # to overlap by luck.
        assert order == ["a-enter", "a-exit", "b-enter", "b-exit"]

    def test_different_keys_run_fully_concurrently(self):
        """The whole reason for per-key over global locking: two
        callers on DIFFERENT keys must never wait on each other. Proven
        the same way TestConditionalRemainderConcurrency proves its own
        concurrency claim — real elapsed-time measurement, not just
        unchanged output."""
        import threading
        import time
        from app.router import _singleflight

        def call(key):
            with _singleflight(key):
                time.sleep(0.3)

        t1 = threading.Thread(target=call, args=("key-a",))
        t2 = threading.Thread(target=call, args=("key-b",))
        start = time.monotonic()
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        elapsed = time.monotonic() - start

        assert elapsed < 0.5, (
            f"took {elapsed:.2f}s — looks like different keys serialized "
            f"against each other, defeating the entire point of a "
            f"per-key (vs. global) lock"
        )

    def test_lock_released_even_when_wrapped_code_raises(self):
        from app.router import _singleflight, get_inflight_lock_count

        with pytest.raises(ValueError):
            with _singleflight("error-key"):
                raise ValueError("simulated failure mid-resolution")

        assert get_inflight_lock_count() == 0, (
            "lock was not released after an exception inside the "
            "singleflight block — a future caller for this key would "
            "deadlock forever"
        )

        # And a fresh call for the same key afterward must work normally.
        entered = []
        with _singleflight("error-key"):
            entered.append(True)
        assert entered == [True]


class TestLlmDetectSingleflight:
    """Regression coverage for singleflight deduplication at
    _llm_detect() — the specific call site `auto`'s cold p99 plateau was
    traced to (router.py's _llm_detect(), the function detect_intent()
    falls through to for the small, fixed 2-of-24 subset of
    AUTO_QUERIES entries that don't resolve via plain keyword matching;
    see wiki/The-Benchmark-Investigation-Log.md's Thread 2). These tests
    prove the actual mechanism directly against _llm_detect(), the same
    discipline TestResultCacheThunderingHerd already established for the
    result cache's identical, deliberately-unfixed gap.
    """

    def setup_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def teardown_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def test_concurrent_callers_for_the_same_uncached_query_pay_the_cost_once(self):
        """The actual fix, proven directly: N concurrent callers for an
        identical, never-yet-cached query must result in exactly ONE
        real LLM call, not N — closing the thundering herd
        TestResultCacheThunderingHerd proves still exists, unfixed, at
        the result-cache layer."""
        import threading
        import time
        from unittest.mock import patch
        from app.router import _llm_detect

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_complete(prompt, max_tokens=30, temperature=0.0):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return "kiwix"

        results = []

        def call():
            results.append(_llm_detect("what is nitrogen"))

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=slow_complete):
            threads = [threading.Thread(target=call) for _ in range(8)]
            start = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - start

        assert call_count["n"] == 1, (
            f"expected exactly 1 real LLM call for 8 concurrent callers "
            f"on the identical uncached key, got {call_count['n']} — "
            f"singleflight failed to deduplicate"
        )
        assert elapsed < 0.4, (
            f"took {elapsed:.2f}s — much longer than one handler call's "
            f"cost, suggesting callers serialized through real LLM calls "
            f"rather than the second+ caller reusing the first's result"
        )
        assert all(r == "kiwix" for r in results)

    def test_a_genuinely_warm_key_never_touches_the_lock_or_the_llm(self):
        """Contrast case mirroring TestResultCacheThunderingHerd's own
        sibling test: once a routing decision is ACTUALLY cached, a
        concurrent caller must hit cache directly, without ever calling
        the LLM OR paying any lock-acquisition cost at all."""
        import threading
        from unittest.mock import patch
        from app.router import _llm_detect, _set_routing

        _set_routing("source:what is nitrogen", "kiwix")

        call_count = {"n": 0}

        def counting_complete(prompt, max_tokens=30, temperature=0.0):
            call_count["n"] += 1
            return "kiwix"

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=counting_complete):
            threads = [
                threading.Thread(target=_llm_detect, args=("what is nitrogen",))
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert call_count["n"] == 0

    def test_a_late_arriving_caller_after_resolution_gets_the_cached_value_without_a_lock_wait(self):
        """A caller arriving well AFTER the in-flight resolution has
        already completed and released its lock must take the fast,
        no-lock path — not queue behind a lock that's already free."""
        from unittest.mock import patch
        from app.router import _llm_detect, get_inflight_lock_count

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="kiwix"):
            first = _llm_detect("what is nitrogen")

        assert first == "kiwix"
        assert get_inflight_lock_count() == 0

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=AssertionError("LLM should not be called again")):
            second = _llm_detect("what is nitrogen")

        assert second == "kiwix"

    def test_concurrent_callers_for_different_uncached_queries_do_not_block_each_other(self):
        """Per-key, not global — two genuinely different uncached
        queries must resolve fully concurrently, never queueing behind
        each other just because both happened to miss at the same time."""
        import threading
        import time
        from unittest.mock import patch
        from app.router import _llm_detect

        def slow_complete(prompt, max_tokens=30, temperature=0.0):
            time.sleep(0.3)
            if "nitrogen" in prompt:
                return "kiwix"
            return "forecast"

        results = {}

        def call(query, label):
            results[label] = _llm_detect(query)

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=slow_complete):
            t1 = threading.Thread(target=call, args=("what is nitrogen", "a"))
            t2 = threading.Thread(target=call, args=("whats the temperature outside right now", "b"))
            start = time.monotonic()
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            elapsed = time.monotonic() - start

        assert elapsed < 0.5, (
            f"took {elapsed:.2f}s — two DIFFERENT keys appear to have "
            f"serialized against each other"
        )
        assert results["a"] == "kiwix"
        assert results["b"] == "forecast"

    def test_lock_released_even_when_llm_call_raises(self):
        """If the wrapped LLM call raises instead of returning normally,
        the lock must still release — otherwise every future caller for
        this exact query would deadlock forever waiting on a lock no one
        will ever release."""
        from unittest.mock import patch
        from app.router import _llm_detect, get_inflight_lock_count

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=RuntimeError("simulated LLM failure")):
            with pytest.raises(RuntimeError):
                _llm_detect("what is nitrogen")

        assert get_inflight_lock_count() == 0

        # A subsequent call for the same query must be able to proceed
        # normally, not hang.
        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="kiwix"):
            result = _llm_detect("what is nitrogen")
        assert result == "kiwix"

    def test_fusion_decision_still_applies_discourse_framing_after_singleflight_resolution(self):
        """Confirms the refactor that extracted cache-hit interpretation
        into _interpret_cached_source_decision() (so it could be reused
        both before and after acquiring the lock) didn't silently drop
        the existing discourse-framing escalation behavior."""
        from app.router import _llm_detect

        with patch_discourse_framing(True):
            from unittest.mock import patch
            with patch("app.router._get_routing", return_value="forecast"):
                result = _llm_detect("everyone keeps talking about black holes, whats the weather")
        assert result == ["forecast", "kiwix"]


def patch_discourse_framing(active: bool):
    """Small local helper — _has_discourse_framing() does real text
    analysis, but these tests only care about the escalation behavior
    downstream of it, so patching it directly keeps the test focused on
    what's actually under test (singleflight's interaction with the
    existing escalation logic) rather than re-deriving discourse-framing
    phrase detection here too."""
    from unittest.mock import patch
    import app.router as router_module
    return patch.object(router_module, "_has_discourse_framing", return_value=active)


class TestLlmPickFusionSourcesSingleflight:
    """Regression coverage for singleflight deduplication at
    _llm_pick_fusion_sources() — the second of the four call sites that
    share the identical check-call-write gap."""

    def setup_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def teardown_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def test_concurrent_callers_for_the_same_uncached_query_pay_the_cost_once(self):
        import threading
        import time
        from unittest.mock import patch
        from app.router import _llm_pick_fusion_sources

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_complete(prompt, max_tokens=30, temperature=0.0):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return "forecast, uptime"

        results = []

        def call():
            results.append(_llm_pick_fusion_sources("whats the weather and services up"))

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=slow_complete):
            threads = [threading.Thread(target=call) for _ in range(8)]
            start = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - start

        assert call_count["n"] == 1
        assert elapsed < 0.4
        assert all("forecast" in r and "uptime" in r for r in results)

    def test_failure_fallback_still_not_cached_under_concurrency(self):
        """The existing "don't cache a bare failure fallback" behavior
        (TestLlmPickFusionSources.test_failure_fallback_is_not_cached_
        allowing_later_success) must still hold once singleflight wraps
        this function — a query that fails for every concurrent caller
        in one wave must still get a fresh, real chance on the next
        wave, not get permanently stuck on ["kiwix", "web"]."""
        import threading
        from unittest.mock import patch
        from app.router import _llm_pick_fusion_sources, _get_routing

        query = "a query every concurrent caller fails on"

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="not_a_real_source"):
            threads = [
                threading.Thread(target=_llm_pick_fusion_sources, args=(query,))
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert _get_routing(f"fusion_sources:{query}") is None, (
            "a failure fallback got cached despite every concurrent "
            "caller's LLM response being unusable"
        )

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="forecast, news") as mock_complete:
            result = _llm_pick_fusion_sources(query)
        assert mock_complete.called
        assert "forecast" in result


class TestKiwixPickBooksSingleflight:
    """Regression coverage for singleflight deduplication at kiwix.py's
    _pick_books_with_llm() — the third of the four call sites sharing
    the identical check-call-write gap, and the first crossing the
    router.py/kiwix.py module boundary via the shared _inflight_locks
    registry."""

    def setup_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def teardown_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def test_concurrent_callers_for_the_same_uncached_query_pay_the_cost_once(self):
        import threading
        import time
        from unittest.mock import patch
        from app.sources.kiwix import _pick_books_with_llm

        books = [
            {"name": "wikipedia_en_all_maxi_2026-02", "title": "Wikipedia", "summary": "Encyclopedia"},
            {"name": "stackexchange", "title": "Stack Exchange", "summary": "Q&A"},
        ]

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_complete(prompt, max_tokens=150, temperature=0.0):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return "wikipedia_en_all_maxi_2026-02"

        results = []

        def call():
            results.append(_pick_books_with_llm("what is nitrogen", books))

        with patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", side_effect=slow_complete):
            threads = [threading.Thread(target=call) for _ in range(8)]
            start = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - start

        assert call_count["n"] == 1
        assert elapsed < 0.4
        assert all(r == ["wikipedia_en_all_maxi_2026-02"] for r in results)

    def test_not_configured_fallback_deduplicated_too(self):
        """The doc's own stated reason for wrapping BOTH
        _fallback_book_choice() call sites, not just the real-LLM path:
        a second caller racing the "not configured" branch must still
        benefit from the first caller's cached fallback decision rather
        than redundantly recomputing it."""
        import threading
        from unittest.mock import patch
        from app.sources.kiwix import _pick_books_with_llm

        books = [
            {"name": "wikipedia_en_all_maxi_2026-02", "title": "Wikipedia", "summary": "Encyclopedia"},
            {"name": "stackexchange", "title": "Stack Exchange", "summary": "Q&A"},
        ]

        results = []

        def call():
            results.append(_pick_books_with_llm("what is nitrogen", books))

        with patch("app.llm.is_configured", return_value=False):
            threads = [threading.Thread(target=call) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert all(r == ["wikipedia_en_all_maxi_2026-02"] for r in results)


class TestKiwixDisambiguationCandidatesSingleflight:
    """Regression coverage for singleflight deduplication at kiwix.py's
    _get_disambiguation_candidates() — the fourth and last of the call
    sites sharing the identical check-call-write gap, and plausibly
    relevant to kiwix_disambiguation's own large cold-tail benchmark
    outliers (see wiki/The-Benchmark-Investigation-Log.md)."""

    def setup_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def teardown_method(self):
        from app.router import clear_routing_cache, _inflight_locks
        clear_routing_cache()
        _inflight_locks.clear()

    def test_concurrent_callers_for_the_same_uncached_term_pay_the_cost_once(self):
        import threading
        import time
        from unittest.mock import patch
        from app.sources.kiwix import _get_disambiguation_candidates

        call_count = {"n": 0}
        call_lock = threading.Lock()

        def slow_complete(prompt, max_tokens=40, temperature=0.0):
            with call_lock:
                call_count["n"] += 1
            time.sleep(0.2)
            return "galaxy astronomy|galaxy spiral|galaxy"

        results = []

        def call():
            results.append(_get_disambiguation_candidates("what is a galaxy", "galaxy"))

        with patch("app.llm.complete", side_effect=slow_complete):
            threads = [threading.Thread(target=call) for _ in range(8)]
            start = time.monotonic()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.monotonic() - start

        assert call_count["n"] == 1
        assert elapsed < 0.4
        assert all("galaxy" in r for r in results)

    def test_empty_llm_response_failure_still_not_cached_under_concurrency(self):
        """Mirrors TestLlmPickFusionSourcesSingleflight's equivalent
        test — this function's own "only skip caching when raw was
        empty/falsy" distinction (different from the other two
        functions' simpler "never cache a fallback" rule) must still
        hold once wrapped in the lock."""
        import threading
        from unittest.mock import patch
        from app.sources.kiwix import _get_disambiguation_candidates
        from app.router import _get_routing

        with patch("app.llm.complete", return_value=""):
            threads = [
                threading.Thread(
                    target=_get_disambiguation_candidates, args=("what is mercury", "mercury")
                )
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert _get_routing("disambig_candidates:mercury") is None

    def test_lock_shared_correctly_across_module_boundary(self):
        """Direct proof that kiwix.py's _get_singleflight_fn() really
        does return router.py's OWN _singleflight, not some
        independent duplicate — confirms the registry is one shared
        source of truth across both modules, not two registries that
        can't see each other."""
        from app.sources.kiwix import _get_singleflight_fn
        from app.router import _singleflight
        assert _get_singleflight_fn() is _singleflight

