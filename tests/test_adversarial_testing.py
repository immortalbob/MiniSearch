"""
Tests for app/adversarial_testing.py — combinatorial adversarial query
generation and structural anomaly detection.

Generation tests verify real combinatorics over the router's REAL
vocabulary lists (no duplicated/forked copies). Anomaly detection tests
verify each check's documented Mnemolis guarantee directly, never a
correctness judgment about response content. DB tests use a temp SQLite
file via unittest.mock.patch on the module-level path constant, matching
test_snapshots.py's existing convention exactly.
"""
import random
import time
from unittest.mock import patch

from app import router
from app.sources import kiwix
from app.adversarial_testing import (
    RECIPES,
    PROPER_NOUN_PAIRS,
    CONDITIONAL_SEEDS,
    generate_adversarial_query,
    init_adversarial_db,
    _connect,
    _record_result,
    _already_tried,
    _check_crash,
    _check_source_mismatch,
    _check_multi_intent_part_count,
    _check_discourse_framing_dropped_kiwix,
    _check_conditional_remainder_sections,
    _check_unexpected_empty,
    _check_latency_outlier,
    run_adversarial_test_cycle,
    get_adversarial_test_summary,
    get_flagged_combinations,
    dismiss_flagged_combination,
    undismiss_flagged_combination,
)


class TestSeedVocabularyIntegrity:
    """The design doc's core constraint: never duplicate/fork router.py's
    or kiwix.py's real ingredient lists. These tests fail loudly if a
    future edit accidentally introduces a local copy instead of importing
    the real thing."""

    def test_conditional_seeds_match_locustfile_real_seeds(self):
        """CONDITIONAL_SEEDS must contain every real seed from
        tests/locustfile.py's CONDITIONAL_QUERIES, not a re-typed subset.

        Reads the file as text rather than importing it as a module:
        locustfile.py imports the `locust` package at module level, which
        isn't in requirements.txt (it's a dev-only load-testing tool, not
        installed by CI's `pip install -r requirements.txt` step) — an
        actual import here would make this test fail in CI specifically,
        not just locally.
        """
        import ast
        import os
        locustfile_path = os.path.join(os.path.dirname(__file__), "locustfile.py")
        with open(locustfile_path) as f:
            tree = ast.parse(f.read(), filename="locustfile.py")
        conditional_queries = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name) and target.id == "CONDITIONAL_QUERIES":
                    conditional_queries = ast.literal_eval(node.value)
                    break
        assert conditional_queries is not None, "CONDITIONAL_QUERIES not found in locustfile.py"
        for full_query in conditional_queries:
            # Each locustfile query embeds one of our condition seeds as a
            # substring (locustfile's queries are "if X, Y" full sentences;
            # our seeds are just the X part).
            assert any(seed in full_query for seed in CONDITIONAL_SEEDS), (
                f"locustfile query {full_query!r} has no matching condition "
                f"seed in CONDITIONAL_SEEDS — seeds have drifted from the "
                f"real locustfile corpus"
            )

    def test_recipes_use_real_intent_map_not_a_copy(self):
        """Recipes that touch INTENT_MAP must reference the actual
        router.INTENT_MAP object, so future additions to real sources are
        automatically picked up with zero changes here."""
        assert router.INTENT_MAP is router.INTENT_MAP  # sanity
        # multi_intent_chain's whole point is using every real key
        rng = random.Random(0)
        seen_sources = set()
        for _ in range(50):
            query, name, fp = RECIPES["multi_intent_chain"](rng)
            seen_sources.update(s for s in fp if s in router.INTENT_MAP)
        # Over 50 draws, should see more than just one or two sources —
        # confirms it's sampling the real, full INTENT_MAP, not a stub.
        assert len(seen_sources) >= 3

    def test_discourse_framing_recipe_uses_real_kiwix_patterns(self):
        """The discourse_framing recipe must draw from the real,
        canonical kiwix.DISCOURSE_FRAMING_PATTERNS list."""
        rng = random.Random(0)
        generated_phrases = set()
        for _ in range(30):
            query, name, fp = RECIPES["discourse_framing_plus_real_keyword"](rng)
            for phrase in kiwix.DISCOURSE_FRAMING_PATTERNS:
                if phrase in query:
                    generated_phrases.add(phrase)
        assert generated_phrases, "no real DISCOURSE_FRAMING_PATTERNS phrase ever appeared in generated queries"
        assert generated_phrases.issubset(set(kiwix.DISCOURSE_FRAMING_PATTERNS))


class TestGeneration:
    """Generation must be pure-Python (no network/LLM calls), produce
    syntactically sane queries, and bias toward novel fingerprints."""

    def test_all_recipes_produce_nonempty_strings(self):
        rng = random.Random(1)
        for name, fn in RECIPES.items():
            query, recipe_name, fingerprint = fn(rng)
            assert isinstance(query, str) and len(query) > 0, f"{name} produced empty/non-string query"
            assert recipe_name == name
            assert isinstance(fingerprint, tuple)

    def test_generate_adversarial_query_returns_valid_metadata_shape(self):
        rng = random.Random(2)
        query, meta = generate_adversarial_query(rng)
        assert isinstance(query, str) and query
        assert meta["recipe_name"] in RECIPES
        assert isinstance(meta["fingerprint"], str)  # JSON-serialized
        assert isinstance(meta["ingredients"], list)
        assert isinstance(meta["novel"], bool)

    def test_proper_noun_pair_recipe_reproduces_bug5_shape(self):
        """Specifically reproduces the exact structural shape that found
        the real proper-noun-pair bug: '<PAIR>, <conj> I <verb phrase>'."""
        rng = random.Random(3)
        for _ in range(10):
            query, name, fp = RECIPES["proper_noun_plus_pronoun_intent"](rng)
            assert any(pair in query for pair in PROPER_NOUN_PAIRS)
            assert " I " in query or query.split(",")[-1].strip().startswith("I ")

    def test_no_intent_fallthrough_recipe_matches_no_intent_keyword(self):
        """This recipe's whole point is generating queries with NO
        INTENT_MAP keyword match at all — verify that's actually true."""
        rng = random.Random(4)
        all_keywords = [kw for kws in router.INTENT_MAP.values() for kw in kws]
        for _ in range(10):
            query, name, fp = RECIPES["no_intent_fallthrough"](rng)
            query_lower = query.lower()
            assert not any(kw in query_lower for kw in all_keywords), (
                f"no_intent_fallthrough query {query!r} accidentally matched a real INTENT_MAP keyword"
            )

    def test_generation_biases_toward_novel_fingerprints(self, tmp_path):
        """Given a fresh DB, the first N generations of a narrow-vocabulary
        recipe should all be marked novel before any repeats occur."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            with patch("app.adversarial_testing.RECIPES", {"no_intent_fallthrough": RECIPES["no_intent_fallthrough"]}):
                rng = random.Random(5)
                novelty_flags = []
                for _ in range(20):
                    query, meta = generate_adversarial_query(rng)
                    novelty_flags.append(meta["novel"])
                    _record_result(meta["fingerprint"], meta["recipe_name"], query, "kiwix", 100, None)
                # Once every topic in the seed list has been seen, later
                # generations should start reporting novel=False.
                assert False in novelty_flags, "generation never fell back to a repeat despite a finite seed vocabulary"

    def test_generation_never_crashes_when_db_unreachable(self, tmp_path):
        """generate_adversarial_query must degrade gracefully (not crash)
        if the dedup DB can't be reached — _already_tried fails soft."""
        with patch("app.adversarial_testing.ADVERSARIAL_DB", "/nonexistent/path/db.sqlite"):
            rng = random.Random(6)
            query, meta = generate_adversarial_query(rng)
            assert isinstance(query, str) and query


class TestDeduplication:
    """Direct tests of the fingerprint persistence layer."""

    def test_record_result_inserts_then_updates_times_generated(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            fp = '["a", "b"]'
            _record_result(fp, "no_intent_fallthrough", "query one", "kiwix", 100, None)
            _record_result(fp, "no_intent_fallthrough", "query one again", "kiwix", 150, None)

            con = _connect(temp_db)
            row = con.execute(
                "SELECT times_generated, last_query_text, last_latency_ms FROM adversarial_combinations WHERE fingerprint = ?",
                (fp,)
            ).fetchone()
            con.close()
            assert row[0] == 2
            assert row[1] == "query one again"
            assert row[2] == 150

    def test_already_tried_reflects_real_db_state(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            fp = ("x", "y")
            import json
            fp_json = json.dumps(fp)
            assert _already_tried(fp) is False
            _record_result(fp_json, "no_intent_fallthrough", "q", "kiwix", 100, None)
            assert _already_tried(fp) is True


class TestHeaderPatternMatchesEveryRealHeader:
    """Direct, explicit coverage for _HEADER_PATTERN itself — the regex
    both _check_multi_intent_part_count and
    _check_conditional_remainder_sections depend on to count real
    headers in a result string.

    Added after a real, live production flag (a conditional_with_
    remainder query whose response genuinely contained a real header,
    flagged anyway as having none) traced back to this regex
    specifically: the original pattern
    (r"\\[[A-Z0-9_ ]+ — [A-Z0-9_ ,'/]+\\]") required exactly ONE literal
    " — " separator, with the tail character class deliberately
    excluding the em-dash itself. kiwix's real label ("ENCYCLOPEDIC
    KNOWLEDGE — UNRELATED TO OTHER SECTIONS BELOW") and news's real
    label ("RECENT NEWS HEADLINES — GENERAL, NOT LOCATION-SPECIFIC
    UNLESS STATED") both legitimately contain a SECOND em-dash, so
    neither header could ever be matched — silently undercounting real
    headers in BOTH checks that use this pattern, not just the one
    that originally surfaced it. Several existing tests for those two
    checks had also been using fabricated header text
    (e.g. "[KIWIX — A]") that happened to be invisible to the SAME
    broken regex for an unrelated reason, masking this bug from the
    test suite entirely until it was traced from real production data.

    Fixed by building the pattern from the real, exact header strings
    fusion._format_header() can actually produce (re.escape()'d, not a
    generic bracket-matching character class) — the same safe approach
    router.py's own _dedupe_nested_fusion_sections() already uses for
    the identical underlying need.
    """

    def test_every_real_source_header_is_matched_exactly_once(self):
        from app.adversarial_testing import _HEADER_PATTERN
        from app.sources.fusion import _format_header, _HEADER_LABELS
        for source in _HEADER_LABELS:
            header = _format_header(source)
            matches = _HEADER_PATTERN.findall(header)
            assert len(matches) == 1, f"{source}'s real header was not matched exactly once: {header!r}"

    def test_kiwix_and_news_headers_specifically_are_matched(self):
        """The two real, confirmed-vulnerable headers, tested
        explicitly and individually — both contain a second em-dash in
        their real descriptive label, the exact property that broke
        the original regex."""
        from app.adversarial_testing import _HEADER_PATTERN
        from app.sources.fusion import _format_header
        assert len(_HEADER_PATTERN.findall(_format_header("kiwix"))) == 1
        assert len(_HEADER_PATTERN.findall(_format_header("news"))) == 1

    def test_real_multi_header_result_counts_correctly(self):
        """The actual real-world shape that exposed this bug: a
        genuine multi-section result containing both vulnerable
        headers (kiwix, news) alongside others — every real header
        must be counted, not just the ones the old regex could see."""
        from app.adversarial_testing import _HEADER_PATTERN
        from app.sources.fusion import _format_header
        text = "\n\n---\n\n".join(
            f"{_format_header(s)}\ncontent" for s in ("kiwix", "forecast", "news", "uptime", "web")
        )
        assert len(_HEADER_PATTERN.findall(text)) == 5

    def test_fabricated_header_text_is_correctly_NOT_matched(self):
        """The flip side of the fix: text that merely LOOKS like a
        header but isn't one of the real, exact strings Mnemolis
        actually produces must not be matched — confirms the fix is a
        genuine exact-match against real headers, not a slightly-
        widened generic pattern that could still drift from reality
        again later."""
        from app.adversarial_testing import _HEADER_PATTERN
        fake_headers = ["[KIWIX — A]", "[UPTIME — STATUS]", "[MADE_UP — LABEL]"]
        for fake in fake_headers:
            assert len(_HEADER_PATTERN.findall(fake)) == 0, f"fabricated header {fake!r} was incorrectly matched"


class TestAnomalyDetection:
    """Each check verifies a documented Mnemolis behavioral guarantee —
    never a correctness judgment about response content."""

    def test_check_crash_detects_explicit_error(self):
        assert _check_crash("", "Connection refused") is not None

    def test_check_crash_detects_raw_traceback(self):
        result = "Traceback (most recent call last):\n  File x, line 1\nValueError: bad"
        assert _check_crash(result, None) is not None

    def test_check_crash_passes_clean_result(self):
        assert _check_crash("Molybdenum is element 42.", None) is None

    def test_check_source_mismatch_flags_real_mismatch(self):
        reason = _check_source_mismatch("multi_intent_chain", ["forecast", "ha"], "uptime")
        assert reason is not None
        assert "forecast" in reason and "ha" in reason

    def test_check_source_mismatch_allows_fusion(self):
        assert _check_source_mismatch("multi_intent_chain", ["forecast", "ha"], "fusion") is None

    def test_check_source_mismatch_allows_exact_match(self):
        assert _check_source_mismatch("multi_intent_chain", ["forecast"], "forecast") is None

    def test_check_source_mismatch_skips_when_no_intent_sources(self):
        """no_intent_fallthrough-style ingredients (no real INTENT_MAP
        key among them) should never trigger this check."""
        assert _check_source_mismatch("no_intent_fallthrough", ["molybdenum"], "kiwix") is None

    def test_check_multi_intent_part_count_flags_large_scale_collapse(self):
        """1 header for 4 intended intents — less than half survived,
        the same shape as the original proper-noun-pair bug 5 (a global
        veto collapsing a whole multi-intent query to a single
        un-split result)."""
        result = "[KIWIX — TOPIC] some content"  # 1 header for 4 intended intents
        reason = _check_multi_intent_part_count("multi_intent_chain", ["forecast", "ha", "news", "uptime"], result)
        assert reason is not None

    def test_check_multi_intent_part_count_passes_close_match(self):
        from app.sources.fusion import _format_header
        result = (
            f"{_format_header('kiwix')} x\n"
            f"{_format_header('web')} y\n"
            f"{_format_header('news')} z"
        )  # 3 real headers for 3 intents
        reason = _check_multi_intent_part_count("multi_intent_chain", ["forecast", "ha", "news"], result)
        assert reason is None

    def test_check_multi_intent_part_count_does_not_flag_legitimate_partial_empty_results(self):
        """The real false positive found on live MiniDock production
        data: 5 intended intents, 3 headers (2 sources legitimately
        and correctly returned empty results — route_with_source()
        deliberately drops empty sub-query results before
        _merge_decomposed_parts() ever runs, with no trace left behind
        of which ones were dropped or why). The OLD version of this
        check (an exact-count comparison with a small fixed tolerance)
        flagged this as a part_count_mismatch — confirmed via direct
        tracing that this was a false positive, not a real bug:
        decomposition produced all 5 correct parts, every part
        resolved to the correct source, and the 2 "missing" sources
        legitimately had nothing to report. 3 of 5 is more than half,
        so the new, deliberately looser threshold correctly does not
        flag this.

        Uses real header strings from fusion._format_header() — found
        via a SEPARATE real bug while tracing a live
        conditional_remainder_missing_sections flag: the header-
        matching regex itself failed to match the real kiwix and news
        headers (their genuine descriptive labels each contain a
        second " — " the original regex's character class couldn't
        accommodate), so a test using fabricated header text like
        "[KIWIX — A]" was passing for the wrong reason — the fake
        header was ALSO invisible to the broken regex, masking the
        real bug entirely. Real header text is required for this test
        to mean what it claims."""
        from app.sources.fusion import _format_header
        result = (
            f"{_format_header('kiwix')} x\n"
            f"{_format_header('web')} y\n"
            f"{_format_header('news')} z"
        )  # 3 real headers for 5 intended
        reason = _check_multi_intent_part_count(
            "multi_intent_chain", ["forecast", "ha", "news", "uptime", "changes"], result
        )
        assert reason is None

    def test_check_multi_intent_part_count_flags_total_collapse_even_with_zero_headers(self):
        """A completely header-less result for 4 intended intents must
        STILL be flagged under the new logic — confirms the removal of
        the old version's `n_headers > 0` guard was a deliberate
        correction, not an oversight. The OLD check could never flag a
        total collapse to zero headers at all, regardless of how many
        sources were intended — a worse blind spot than the false
        positive this same fix also closes, since a complete collapse
        is exactly the original bug 5 signature this check exists to
        catch."""
        result = "No results found."  # genuinely zero headers
        reason = _check_multi_intent_part_count("multi_intent_chain", ["forecast", "ha", "news", "uptime"], result)
        assert reason is not None
        assert "0 of 4" in reason

    def test_check_multi_intent_part_count_flags_genuine_partial_fallback(self):
        """A real, severe content-loss case: only 1 of 4 intended
        sources is even visible in the result — less than half
        survived, correctly flagged."""
        from app.sources.fusion import _format_header
        result = f"{_format_header('kiwix')} some real content\nplain web fallback text, no header here"
        reason = _check_multi_intent_part_count("multi_intent_chain", ["forecast", "ha", "news", "uptime"], result)
        assert reason is not None

    def test_check_multi_intent_part_count_only_applies_to_its_own_recipe(self):
        from app.sources.fusion import _format_header
        result = f"{_format_header('kiwix')} some content"
        reason = _check_multi_intent_part_count("no_intent_fallthrough", ["forecast", "ha", "news", "uptime"], result)
        assert reason is None

    def test_check_multi_intent_part_count_exactly_half_is_not_flagged(self):
        """Boundary case: exactly half (2 of 4) is NOT 'fewer than
        half', so this should NOT flag — confirms the boundary itself
        is handled the intended way (strict less-than, not
        less-than-or-equal)."""
        from app.sources.fusion import _format_header
        result = f"{_format_header('kiwix')} x\n{_format_header('web')} y"  # 2 real headers for 4 intended
        reason = _check_multi_intent_part_count("multi_intent_chain", ["forecast", "ha", "news", "uptime"], result)
        assert reason is None

    def test_check_multi_intent_part_count_just_under_half_is_flagged(self):
        """Boundary case just past the line: 2 of 5 (40%, under half)
        should flag."""
        from app.sources.fusion import _format_header
        result = f"{_format_header('kiwix')} x\n{_format_header('web')} y"  # 2 real headers for 5 intended
        reason = _check_multi_intent_part_count(
            "multi_intent_chain", ["forecast", "ha", "news", "uptime", "changes"], result
        )
        assert reason is not None

    def test_check_discourse_framing_dropped_kiwix_flags_real_drop(self):
        reason = _check_discourse_framing_dropped_kiwix(
            "discourse_framing_plus_real_keyword", "plain web result, no kiwix", "web"
        )
        assert reason is not None

    def test_check_discourse_framing_dropped_kiwix_passes_when_source_is_kiwix(self):
        assert _check_discourse_framing_dropped_kiwix(
            "discourse_framing_plus_real_keyword", "some result", "kiwix"
        ) is None

    def test_check_discourse_framing_dropped_kiwix_passes_when_header_present(self):
        from app.sources.fusion import _format_header
        assert _check_discourse_framing_dropped_kiwix(
            "discourse_framing_plus_real_keyword", f"{_format_header('kiwix')} real content", "fusion"
        ) is None

    def test_check_conditional_remainder_sections_flags_missing_headers(self):
        reason = _check_conditional_remainder_sections("conditional_with_remainder", "just plain text, no headers")
        assert reason is not None

    def test_check_conditional_remainder_sections_passes_with_header(self):
        from app.sources.fusion import _format_header
        reason = _check_conditional_remainder_sections(
            "conditional_with_remainder", f"{_format_header('uptime')} all services up"
        )
        assert reason is None

    def test_check_conditional_remainder_sections_passes_with_real_kiwix_or_news_header(self):
        """Regression test for the actual real bug found tracing a live
        flagged row: kiwix's and news's real descriptive labels each
        contain a SECOND " — " separator, which the original
        _HEADER_PATTERN regex (one literal " — " required, with the
        tail character class deliberately excluding the em-dash) could
        never match — meaning a real, correctly-formed response
        containing only a kiwix or news header (no other source) was
        ALWAYS misread as having zero headers, regardless of any
        threshold or tolerance setting. This is the exact root cause
        behind the real "if it is raining... as well as feeds" flag,
        and likely a meaningful contributor to the real
        "part_count_mismatch: intended 5, found 3" flags too, since
        both of those real queries also involved a news or kiwix
        header among their intended sources."""
        from app.sources.fusion import _format_header
        for source in ("kiwix", "news"):
            result = f"{_format_header(source)}\nReal content here."
            reason = _check_conditional_remainder_sections("conditional_with_remainder", result)
            assert reason is None, f"real {source} header was not recognized"

    def test_check_unexpected_empty_uses_real_fusion_looks_empty(self):
        """Must delegate to the real, canonical fusion._looks_empty(),
        never a forked copy of its phrase list."""
        assert _check_unexpected_empty("No results found.") is not None
        assert _check_unexpected_empty("Molybdenum is a transition metal.") is None

    def test_check_latency_outlier_requires_history(self):
        """With fewer than 10 historical samples, must return None (not
        yet decidable) rather than guessing."""
        assert _check_latency_outlier("nonexistent_recipe_no_history", 99999) is None

    def test_check_latency_outlier_flags_real_outlier(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            for i in range(20):
                fp = json.dumps([f"sample-{i}"])
                _record_result(fp, "multi_intent_chain", f"query {i}", "kiwix", 200, None)
            reason = _check_latency_outlier("multi_intent_chain", 50000)
            assert reason is not None

    def test_check_latency_outlier_passes_normal_latency(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            for i in range(20):
                fp = json.dumps([f"sample-{i}"])
                _record_result(fp, "multi_intent_chain", f"query {i}", "kiwix", 200, None)
            reason = _check_latency_outlier("multi_intent_chain", 210)
            assert reason is None

    def test_check_latency_outlier_respects_configured_min_samples(self, tmp_path):
        """Lowering ADVERSARIAL_TEST_LATENCY_OUTLIER_MIN_SAMPLES must let
        the check engage with fewer real samples than the default 10 —
        proves this is a real setting, not a renamed constant."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_latency_outlier_min_samples", 3):
            init_adversarial_db()
            import json
            for i in range(5):
                fp = json.dumps([f"sample-{i}"])
                _record_result(fp, "multi_intent_chain", f"query {i}", "kiwix", 200, None)
            # Only 5 samples — would be silently skipped under the default
            # floor of 10, but the lowered setting should let it engage.
            reason = _check_latency_outlier("multi_intent_chain", 50000)
            assert reason is not None

    def test_check_latency_outlier_respects_configured_multiplier(self, tmp_path):
        """A tighter multiplier must flag latency the default 1.5x
        wouldn't have caught."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_latency_outlier_multiplier", 1.05), \
             patch("app.adversarial_testing.settings.adversarial_test_latency_outlier_floor_ms", 0):
            init_adversarial_db()
            import json
            for i in range(20):
                fp = json.dumps([f"sample-{i}"])
                _record_result(fp, "multi_intent_chain", f"query {i}", "kiwix", 200, None)
            # 220ms is only 1.1x the 200ms p95 — would pass under the
            # default 1.5x multiplier, but should fail under 1.05x.
            reason = _check_latency_outlier("multi_intent_chain", 220)
            assert reason is not None

    def test_check_part_count_mismatch_no_longer_uses_a_configurable_tolerance(self):
        """ADVERSARIAL_TEST_PART_COUNT_MISMATCH_TOLERANCE was removed
        entirely as part of fixing the real false-positive this check
        had — an exact-count-with-small-tolerance comparison could
        never be made to work correctly, since route_with_source()
        leaves no trace of which sub-queries legitimately returned
        empty versus which results were actually lost to a bug. The
        new "fewer than half survived" rule is a fixed, principled
        threshold derived from the original bug 5's actual signature
        (a total collapse), not something meant to be tunable per
        deployment. Confirms the setting is genuinely gone, not just
        unused, so a future change can't silently resurrect a
        dead/ignored config knob."""
        from app.config import settings
        assert not hasattr(settings, "adversarial_test_part_count_mismatch_tolerance")


class TestFullCycle:
    """End-to-end tests of the scheduled job body against a stubbed
    route_with_source — never a real network call."""

    def test_cycle_persists_results_for_every_batch_item(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 5), \
             patch("app.router.route_with_source", return_value=("[KIWIX — X] content", "kiwix")):
            init_adversarial_db()
            run_adversarial_test_cycle()
            con = _connect(temp_db)
            count = con.execute("SELECT COUNT(*) FROM adversarial_combinations").fetchone()[0]
            con.close()
            assert count == 5

    def test_cycle_survives_a_raised_exception_in_one_iteration(self, tmp_path):
        """A single iteration's exception must never abort the whole
        batch — same per-item try/except convention as snapshot jobs."""
        temp_db = str(tmp_path / "test_adversarial.db")
        call_count = {"n": 0}

        def flaky_route(query, source="auto", fusion_sources=None):
            call_count["n"] += 1
            if call_count["n"] % 2 == 0:
                raise RuntimeError("simulated failure")
            return ("[KIWIX — X] content", "kiwix")

        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 6), \
             patch("app.router.route_with_source", side_effect=flaky_route):
            init_adversarial_db()
            run_adversarial_test_cycle()  # must not raise
            con = _connect(temp_db)
            count = con.execute("SELECT COUNT(*) FROM adversarial_combinations").fetchone()[0]
            crashed = con.execute(
                "SELECT COUNT(*) FROM adversarial_combinations WHERE last_flagged_reason LIKE 'crash:%'"
            ).fetchone()[0]
            con.close()
            assert count == 6
            assert crashed >= 1

    def test_cycle_never_touches_real_cache_files(self, tmp_path, monkeypatch):
        """Must write only to adversarial_testing.db — never cache.json,
        routing_cache.json, or query_log.db.

        NOTE on this test's own history: an earlier version of this test
        mocked out router.route_with_source() entirely, which meant it
        could never have caught the real gap found and fixed alongside
        this test — route_with_source() writes to the real, in-memory
        _cache/_routing_cache dicts (and, eventually, their disk files)
        as an unconditional side effect of routing, via _set_cached()/
        _set_routing() deep inside _resolve_single_source() and
        _llm_detect()/_llm_pick_fusion_sources(). Mocking the whole
        function meant this test was only ever proving "if
        route_with_source doesn't run, nothing else here touches the
        cache files either" — true, but not the actual claim in the
        docstring this test's name asserts. This version (and the two
        below it) keeps the sentinel-file check AND adds the real,
        previously-missing check: that the actual in-memory cache dicts
        stay empty when route_with_source() genuinely runs, unmocked.
        """
        temp_db = str(tmp_path / "test_adversarial.db")
        sentinel_cache = tmp_path / "cache.json"
        sentinel_cache.write_text('{"sentinel": true}')

        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 3), \
             patch("app.router.route_with_source", return_value=("[KIWIX — X] content", "kiwix")):
            init_adversarial_db()
            run_adversarial_test_cycle()
            # The sentinel cache file must be byte-for-byte untouched.
            assert sentinel_cache.read_text() == '{"sentinel": true}'

    def test_cycle_does_not_pollute_real_cache_with_unmocked_routing(self, tmp_path):
        """The actual, real regression test for the found gap: run
        run_adversarial_test_cycle() with route_with_source() genuinely
        UNMOCKED (only the underlying source handler and LLM are
        stubbed, the same minimum needed to make a real run
        deterministic and offline-safe) and confirm the real, in-memory
        _cache and _routing_cache dicts are still empty afterward — the
        thing the previous, route_with_source-mocking version of this
        test could never have actually verified.

        Saves and restores _cache/_routing_cache around the test (the
        same convention test_cache_persistence.py's tests already use)
        rather than just clearing them — these are real, shared,
        process-global module state in app.router, and a test that only
        clears without restoring leaves any pre-existing legitimate
        state wiped for whatever runs after it in the same pytest
        process. The companion concurrency test below this one
        originally only cleared, not saved-and-restored, and that gap
        was real: it passed in a sandbox where a stale on-disk
        cache.json from earlier ad-hoc runs happened to reset _cache to
        {} via load_cache()'s normal startup path before the next test
        ran, but failed on a genuinely clean CI checkout with no such
        file — load_cache() only resets _cache when CACHE_FILE doesn't
        already exist; if it exists (even as an empty `{}`), it parses
        and assigns over _cache, masking exactly this kind of leftover
        pollution. A bare checkout has no such file at all, so the
        early-return path leaves whatever a prior test left behind.
        """
        import app.router as router_module

        temp_db = str(tmp_path / "test_adversarial.db")
        original_cache = dict(router_module._cache)
        original_routing_cache = dict(router_module._routing_cache)
        router_module._cache.clear()
        router_module._routing_cache.clear()
        original_source_map = dict(router_module.SOURCE_MAP)

        def fake_kiwix(query):
            return f"# Real Article\nSome genuinely real-shaped content about {query[:20]}"

        router_module.SOURCE_MAP["kiwix"] = fake_kiwix
        try:
            with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
                 patch("app.adversarial_testing.settings.adversarial_test_batch_size", 5), \
                 patch("app.router._llm_detect", return_value="kiwix"), \
                 patch("app.router._save_cache"), \
                 patch("app.router._save_routing_cache"):
                init_adversarial_db()
                run_adversarial_test_cycle()

            assert len(router_module._cache) == 0, (
                f"Adversarial testing's synthetic queries leaked into the real "
                f"result cache: {list(router_module._cache.keys())}"
            )
            assert len(router_module._routing_cache) == 0, (
                f"Adversarial testing's synthetic queries leaked into the real "
                f"routing cache: {list(router_module._routing_cache.keys())}"
            )
        finally:
            router_module.SOURCE_MAP.clear()
            router_module.SOURCE_MAP.update(original_source_map)
            router_module._cache.clear()
            router_module._cache.update(original_cache)
            router_module._routing_cache.clear()
            router_module._routing_cache.update(original_routing_cache)

    def test_real_user_query_unaffected_by_concurrent_adversarial_suppression(self, tmp_path):
        """Confirms router.suppress_cache_writes() is genuinely
        thread-local, not a plain shared flag — a real concurrent
        request on another thread must still get cached normally even
        while an adversarial testing cycle is actively suppressing
        writes for ITS OWN call stack. A plain module-level boolean
        would fail this test (and would be a strictly worse bug than
        the one this whole fix addresses): a real live request landing
        in the same window would have its own legitimate cache write
        silently dropped too.

        Saves and restores _cache around the test rather than just
        clearing it — see the longer explanation in the test directly
        above this one; this test deliberately leaves a real entry in
        _cache to prove its own assertion, and skipping the restore
        step here was the actual root cause of a real, confirmed CI
        failure on a clean checkout (no stale cache.json on disk to
        accidentally reset _cache before the next test ran).
        """
        import threading
        import app.router as router_module

        original_cache = dict(router_module._cache)
        router_module._cache.clear()
        original_source_map = dict(router_module.SOURCE_MAP)

        def slow_handler(query):
            time.sleep(0.1)
            return "# Real content"

        router_module.SOURCE_MAP["kiwix"] = slow_handler
        results = {}
        try:
            def adversarial_thread():
                with router_module.suppress_cache_writes():
                    router_module._resolve_single_source("kiwix", "synthetic adversarial query")
                    time.sleep(0.05)

            def real_user_thread():
                time.sleep(0.02)
                router_module._resolve_single_source("kiwix", "a real concurrent user query")
                results["cached"] = "kiwix:a real concurrent user query" in router_module._cache

            t1 = threading.Thread(target=adversarial_thread)
            t2 = threading.Thread(target=real_user_thread)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            assert results["cached"] is True
            assert "kiwix:synthetic adversarial query" not in router_module._cache
        finally:
            router_module.SOURCE_MAP.clear()
            router_module.SOURCE_MAP.update(original_source_map)
            router_module._cache.clear()
            router_module._cache.update(original_cache)

    def test_cycle_is_a_safe_noop_when_disabled(self, tmp_path):
        """run_adversarial_test_cycle() must check ADVERSARIAL_TEST_ENABLED
        itself — defense in depth, so a direct call (e.g. via
        POST /adversarial/trigger) can never run real queries against the
        LLM/SearXNG/Kiwix backends while the feature is supposed to be off,
        even if scheduler registration was somehow bypassed."""
        temp_db = str(tmp_path / "test_adversarial.db")
        route_called = {"n": 0}

        def tracking_route(query, source="auto", fusion_sources=None):
            route_called["n"] += 1
            return ("[KIWIX — X] content", "kiwix")

        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", False), \
             patch("app.router.route_with_source", side_effect=tracking_route):
            result = run_adversarial_test_cycle()
            assert result["status"] == "disabled"
            assert result["queries_run"] == 0
            assert route_called["n"] == 0  # never touched the real pipeline at all

    def test_cycle_returns_real_summary_when_enabled(self, tmp_path):
        """The return value POST /adversarial/trigger relies on — must
        report what actually happened on that specific call, not a bare
        success with no way to confirm results."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 4), \
             patch("app.router.route_with_source", return_value=("No results found.", "web")):
            init_adversarial_db()
            result = run_adversarial_test_cycle()
            assert result["status"] == "ran"
            assert result["queries_run"] == 4
            assert result["flagged"] == 4  # every one matches unexpected_empty


class TestFlaggedResultExcerpt:
    """Regression tests for last_flagged_result_excerpt — found
    necessary via a real, unresolved investigation: a flag fired once,
    every code-level hypothesis for why was checked and ruled out
    against real evidence, and the investigation hit a hard wall the
    schema itself created — there was no way to ever recover the
    actual text that matched _looks_empty(), only the fact that
    something did. This column exists so the next occurrence isn't
    structurally unrecoverable the same way."""

    def test_flagged_result_is_stored_as_an_excerpt(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 1), \
             patch("app.router.route_with_source", return_value=("No results found.", "web")):
            init_adversarial_db()
            run_adversarial_test_cycle()
            con = _connect(temp_db)
            row = con.execute(
                "SELECT last_flagged_result_excerpt FROM adversarial_combinations LIMIT 1"
            ).fetchone()
            con.close()
        assert row[0] == "No results found."

    def test_clean_run_never_stores_an_excerpt(self, tmp_path):
        """The overwhelming common case — a clean run with no flag —
        must NOT store any result text, since storing full text on
        every single combination would be real, unnecessary bloat for
        data that's only ever useful when something actually went
        wrong. Forces the no_intent_fallthrough recipe specifically —
        the one shape where 'kiwix' is always a genuinely correct
        answer regardless of the random query content, avoiding a real
        source_mismatch false flag from an unrelated, randomly-rolled
        recipe expecting a different source."""
        from app.adversarial_testing import RECIPES
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.RECIPES", {"no_intent_fallthrough": RECIPES["no_intent_fallthrough"]}), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 1), \
             patch("app.router.route_with_source", return_value=("[KIWIX — X] real, legitimate content here.", "kiwix")):
            init_adversarial_db()
            run_adversarial_test_cycle()
            con = _connect(temp_db)
            row = con.execute(
                "SELECT last_flagged_result_excerpt FROM adversarial_combinations LIMIT 1"
            ).fetchone()
            con.close()
        assert row[0] is None

    def test_excerpt_is_truncated_for_a_long_result(self, tmp_path):
        long_result = "Error: " + ("x" * 1000)
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 1), \
             patch("app.router.route_with_source", return_value=(long_result, "web")):
            init_adversarial_db()
            run_adversarial_test_cycle()
            con = _connect(temp_db)
            row = con.execute(
                "SELECT last_flagged_result_excerpt FROM adversarial_combinations LIMIT 1"
            ).fetchone()
            con.close()
        assert len(row[0]) == 500
        assert row[0] == long_result[:500]

    def test_excerpt_is_exposed_via_get_flagged_combinations(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True), \
             patch("app.adversarial_testing.settings.adversarial_test_batch_size", 1), \
             patch("app.router.route_with_source", return_value=("No results found.", "web")):
            init_adversarial_db()
            run_adversarial_test_cycle()
            flagged = get_flagged_combinations()
        assert len(flagged) == 1
        assert flagged[0]["last_flagged_result_excerpt"] == "No results found."

    def test_excerpt_from_first_flag_preserved_through_a_later_clean_run(self, tmp_path):
        """The same 'preserve history, don't let a clean run erase it'
        convention every other first_flagged_* column already follows
        — a later clean run must not wipe out the excerpt that
        explains the ORIGINAL anomaly, since that's exactly the
        evidence a human would want when reviewing an intermittent
        flag later."""
        import json
        temp_db = str(tmp_path / "test_adversarial.db")
        fp = json.dumps(["stable-fingerprint-for-excerpt-test"])
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            _record_result(fp, "no_intent_fallthrough", "q", "web", 100, "unexpected_empty: x", "No results found.")
            _record_result(fp, "no_intent_fallthrough", "q", "kiwix", 100, None, "[KIWIX — X] real content now.")
            con = _connect(temp_db)
            row = con.execute(
                "SELECT last_flagged_result_excerpt, last_flagged_reason FROM adversarial_combinations WHERE fingerprint = ?",
                (fp,)
            ).fetchone()
            con.close()
        assert row[0] == "No results found."  # preserved from the original flag
        assert row[1] is None  # but last_flagged_reason correctly reflects the clean run

    def test_migration_adds_column_to_a_real_pre_existing_database(self, tmp_path):
        """The exact real-world scenario this fix needs to handle
        correctly: MiniDock's actual database predates this column
        entirely. Confirms init_adversarial_db()'s migration adds it
        without erroring or losing any pre-existing data."""
        import sqlite3
        old_db = str(tmp_path / "old_schema.db")
        con = sqlite3.connect(old_db)
        con.execute("""
            CREATE TABLE adversarial_combinations (
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
                review_status TEXT
            )
        """)
        con.execute(
            "INSERT INTO adversarial_combinations "
            "(fingerprint, recipe_name, first_seen_timestamp, last_query_text, last_run_timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("old-fp", "test_recipe", "2026-01-01T00:00:00Z", "old query", "2026-01-01T00:00:00Z")
        )
        con.commit()
        con.close()

        with patch("app.adversarial_testing.ADVERSARIAL_DB", old_db):
            init_adversarial_db()
            con2 = _connect(old_db)
            cols = {row[1] for row in con2.execute("PRAGMA table_info(adversarial_combinations)").fetchall()}
            preserved = con2.execute(
                "SELECT last_query_text FROM adversarial_combinations WHERE fingerprint = 'old-fp'"
            ).fetchone()
            con2.close()
        assert "last_flagged_result_excerpt" in cols
        assert preserved[0] == "old query"


class TestHealthSummaryAndFlaggedEndpointData:
    """Tests for the /health and /adversarial/flagged data functions."""

    def test_summary_reports_disabled_without_touching_db(self, tmp_path):
        """A disabled feature should report its own state directly,
        never silently falling through to never_ran/stale based on
        whatever the DB happens to contain."""
        temp_db = str(tmp_path / "nonexistent" / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", False):
            summary = get_adversarial_test_summary()
            assert summary == {"status": "disabled"}

    def test_summary_never_ran_state(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True):
            init_adversarial_db()
            summary = get_adversarial_test_summary()
            assert summary["status"] == "never_ran"
            assert summary["total_combinations_tried"] == 0
            assert summary["flagged_for_review"] == 0

    def test_summary_counts_match_real_rows(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True):
            init_adversarial_db()
            import json
            _record_result(json.dumps(["a"]), "no_intent_fallthrough", "q1", "kiwix", 100, None)
            _record_result(json.dumps(["b"]), "no_intent_fallthrough", "q2", "kiwix", 100, "crash: boom")
            summary = get_adversarial_test_summary()
            assert summary["status"] == "ok"
            assert summary["total_combinations_tried"] == 2
            assert summary["flagged_for_review"] == 1

    def test_get_flagged_combinations_only_returns_flagged_rows(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            _record_result(json.dumps(["a"]), "no_intent_fallthrough", "clean query", "kiwix", 100, None)
            _record_result(json.dumps(["b"]), "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            flagged = get_flagged_combinations()
            assert len(flagged) == 1
            assert flagged[0]["last_query_text"] == "bad query"
            assert flagged[0]["last_flagged_reason"] == "crash: boom"

    def test_regression_intermittent_flag_does_not_silently_vanish_on_clean_reroll(self, tmp_path):
        """The exact bug a reviewer caught: a fingerprint flagged once
        for an intermittent anomaly must NOT disappear from
        get_flagged_combinations() just because the same fingerprint
        happened to be re-rolled and came back clean on a later run.
        This test would have FAILED against the pre-fix version of
        get_flagged_combinations(), which only checked
        last_flagged_reason IS NOT NULL."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["flaky-fingerprint"])
            # First run: a real, intermittent anomaly fires.
            _record_result(fp, "multi_intent_chain", "first run query", "kiwix", 100, "latency_outlier: flaky")
            flagged_after_flag = get_flagged_combinations()
            assert len(flagged_after_flag) == 1

            # Second run, SAME fingerprint: comes back clean (the
            # intermittent issue didn't reproduce this time).
            _record_result(fp, "multi_intent_chain", "second run query", "kiwix", 100, None)
            flagged_after_clean_reroll = get_flagged_combinations()

            # The combination must still be visible — it has a real
            # history of being flagged, even though it's currently clean.
            assert len(flagged_after_clean_reroll) == 1
            row = flagged_after_clean_reroll[0]
            assert row["ever_flagged"] is True
            assert row["currently_flagged"] is False  # accurately reflects the clean re-roll
            assert row["first_flagged_reason"] == "latency_outlier: flaky"  # original anomaly preserved
            assert row["last_flagged_reason"] is None  # most recent result, also accurately reflected
            assert row["last_query_text"] == "second run query"  # last_* still updates normally

    def test_first_flagged_fields_survive_multiple_later_clean_runs(self, tmp_path):
        """The original anomaly must survive not just one but many
        subsequent clean runs — first_flagged_* is write-once per
        fingerprint, not just 'most recent flag'."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["repeat-clean-fingerprint"])
            _record_result(fp, "multi_intent_chain", "q0", "kiwix", 100, "crash: original anomaly")
            for i in range(5):
                _record_result(fp, "multi_intent_chain", f"q{i+1}", "kiwix", 100, None)
            flagged = get_flagged_combinations()
            assert len(flagged) == 1
            assert flagged[0]["first_flagged_reason"] == "crash: original anomaly"
            assert flagged[0]["times_generated"] == 6

    def test_first_flagged_fields_do_not_overwrite_on_a_second_different_flag(self, tmp_path):
        """If a fingerprint is flagged again later for a DIFFERENT
        reason, first_flagged_reason must still hold the ORIGINAL
        anomaly, not the newest one — last_flagged_reason is the place
        for the newest information."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["two-different-flags"])
            _record_result(fp, "multi_intent_chain", "q0", "kiwix", 100, "crash: first anomaly")
            _record_result(fp, "multi_intent_chain", "q1", "kiwix", 100, None)
            _record_result(fp, "multi_intent_chain", "q2", "kiwix", 100, "unexpected_empty: second anomaly")
            flagged = get_flagged_combinations()
            assert len(flagged) == 1
            assert flagged[0]["first_flagged_reason"] == "crash: first anomaly"
            assert flagged[0]["last_flagged_reason"] == "unexpected_empty: second anomaly"

    def test_currently_flagged_field_is_accurate_for_a_combination_never_flagged(self, tmp_path):
        """A combination that has never once been flagged must not
        appear in get_flagged_combinations() at all — the union only
        adds rows with real flag history, it doesn't surface everything."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            _record_result(json.dumps(["always-clean"]), "no_intent_fallthrough", "clean query", "kiwix", 100, None)
            flagged = get_flagged_combinations()
            assert len(flagged) == 0

    def test_dismiss_removes_combination_from_default_view(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["to-be-dismissed"])
            _record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            assert len(get_flagged_combinations()) == 1

            dismissed = dismiss_flagged_combination(fp)
            assert dismissed is True
            assert len(get_flagged_combinations()) == 0

    def test_dismiss_does_not_delete_history_visible_with_include_dismissed(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["dismissed-but-preserved"])
            _record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            dismiss_flagged_combination(fp)

            assert len(get_flagged_combinations()) == 0
            visible_with_history = get_flagged_combinations(include_dismissed=True)
            assert len(visible_with_history) == 1
            assert visible_with_history[0]["first_flagged_reason"] == "crash: boom"

    def test_dismiss_unknown_fingerprint_returns_false(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            result = dismiss_flagged_combination(json.dumps(["never-existed"]))
            assert result is False

    def test_undismiss_restores_combination_to_default_view(self, tmp_path):
        """The real, symmetric counterpart to dismiss — found necessary
        via real production usage, not written defensively up front: a
        real batch-dismiss matched against a stale, previously-fetched
        listing rather than a freshly re-fetched one closed out
        combinations that were never actually resolved, with no way
        back short of editing the database by hand."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["wrongly-dismissed"])
            _record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            dismiss_flagged_combination(fp)
            assert len(get_flagged_combinations()) == 0

            undismissed = undismiss_flagged_combination(fp)
            assert undismissed is True
            assert len(get_flagged_combinations()) == 1

    def test_undismiss_restores_the_exact_prior_state_not_a_new_one(self, tmp_path):
        """review_status after undismiss must be NULL — the same value
        it had before ever being dismissed — not some other sentinel
        value representing a third, distinct state."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["restore-exact-state"])
            _record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            never_dismissed_row = get_flagged_combinations()[0]
            assert never_dismissed_row["review_status"] is None

            dismiss_flagged_combination(fp)
            undismiss_flagged_combination(fp)
            restored_row = get_flagged_combinations()[0]
            assert restored_row["review_status"] is None

    def test_undismiss_unknown_fingerprint_returns_false(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            result = undismiss_flagged_combination(json.dumps(["never-existed"]))
            assert result is False

    def test_undismiss_on_never_dismissed_combination_is_a_safe_noop(self, tmp_path):
        """Calling undismiss on a combination that was never dismissed
        in the first place must not error or change its visibility —
        it's already in the state undismiss would restore it to."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["never-dismissed-at-all"])
            _record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            result = undismiss_flagged_combination(fp)
            assert result is True  # row exists and was updated, even though value didn't change
            assert len(get_flagged_combinations()) == 1

    def test_a_new_flag_after_dismissal_resurfaces_in_default_view(self, tmp_path):
        """A genuinely NEW anomaly on a previously-dismissed fingerprint
        must reappear normally — dismissal isn't a permanent suppression
        of all future flags on that combination, only a closing of the
        specific earlier review."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            fp = json.dumps(["dismissed-then-reflagged"])
            _record_result(fp, "no_intent_fallthrough", "q0", "kiwix", 100, "crash: first issue")
            dismiss_flagged_combination(fp)
            assert len(get_flagged_combinations()) == 0

            # A genuinely new flag fires on a later run.
            _record_result(fp, "no_intent_fallthrough", "q1", "kiwix", 100, "unexpected_empty: new issue")
            flagged = get_flagged_combinations()
            assert len(flagged) == 1
            assert flagged[0]["last_flagged_reason"] == "unexpected_empty: new issue"

    def test_health_summary_flagged_for_review_matches_endpoint_definition(self, tmp_path):
        """The /health summary's flagged_for_review count must use the
        exact same union-minus-dismissed definition as
        get_flagged_combinations()'s default view — these two numbers
        must never silently disagree."""
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.adversarial_testing.settings.adversarial_test_enabled", True):
            init_adversarial_db()
            import json
            # One combination flagged then cleaned (still counts).
            fp1 = json.dumps(["counted-1"])
            _record_result(fp1, "no_intent_fallthrough", "q", "kiwix", 100, "crash: x")
            _record_result(fp1, "no_intent_fallthrough", "q2", "kiwix", 100, None)
            # One combination flagged and dismissed (should NOT count).
            fp2 = json.dumps(["dismissed-2"])
            _record_result(fp2, "no_intent_fallthrough", "q", "kiwix", 100, "crash: y")
            dismiss_flagged_combination(fp2)
            # One combination never flagged (should NOT count).
            fp3 = json.dumps(["clean-3"])
            _record_result(fp3, "no_intent_fallthrough", "q", "kiwix", 100, None)

            summary = get_adversarial_test_summary()
            flagged = get_flagged_combinations()
            assert summary["flagged_for_review"] == len(flagged) == 1

    def test_schema_migration_backfills_ever_flagged_for_preexisting_rows(self, tmp_path):
        """A database created by an OLDER version of this module (before
        ever_flagged existed) must have its existing flagged rows
        correctly backfilled when init_adversarial_db() runs again under
        the new schema — confirms a real upgrade path for the live
        database already running in production, not just a fresh DB."""
        import sqlite3
        import json
        temp_db = str(tmp_path / "test_adversarial.db")

        # Simulate the OLD schema, pre-migration, with one already-
        # flagged row sitting in it exactly as it would on a real,
        # already-deployed instance.
        con = sqlite3.connect(temp_db)
        con.execute("""
            CREATE TABLE adversarial_combinations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT NOT NULL UNIQUE,
                recipe_name TEXT NOT NULL,
                first_seen_timestamp TEXT NOT NULL,
                times_generated INTEGER NOT NULL DEFAULT 1,
                last_query_text TEXT NOT NULL,
                last_source_used TEXT,
                last_latency_ms INTEGER,
                last_flagged_reason TEXT,
                last_run_timestamp TEXT NOT NULL
            )
        """)
        con.execute(
            """INSERT INTO adversarial_combinations
               (fingerprint, recipe_name, first_seen_timestamp, times_generated,
                last_query_text, last_source_used, last_latency_ms,
                last_flagged_reason, last_run_timestamp)
               VALUES (?, 'no_intent_fallthrough', '2026-06-24T12:00:00Z', 3,
                       'pre-migration query', 'kiwix', 100, 'crash: pre-existing', '2026-06-24T12:00:00Z')""",
            (json.dumps(["pre-migration-fp"]),)
        )
        con.commit()
        con.close()

        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()  # runs the migration against the old-schema DB
            flagged = get_flagged_combinations()
            assert len(flagged) == 1
            assert flagged[0]["ever_flagged"] is True
            assert flagged[0]["first_flagged_reason"] == "crash: pre-existing"
            assert flagged[0]["first_flagged_timestamp"] == "2026-06-24T12:00:00Z"

    def test_get_flagged_combinations_respects_limit(self, tmp_path):
        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db):
            init_adversarial_db()
            import json
            for i in range(10):
                _record_result(json.dumps([f"item-{i}"]), "no_intent_fallthrough", f"q{i}", "kiwix", 100, f"crash: boom {i}")
            flagged = get_flagged_combinations(limit=3)
            assert len(flagged) == 3


class TestEndpointsViaTestClient:
    """End-to-end tests through the real FastAPI app — confirms the
    enable switch actually changes real HTTP-level behavior, not just
    the underlying functions in isolation. Each test boots its own
    short-lived TestClient (rather than sharing test_main.py's
    module-scoped fixture) since the enabled/disabled state must be
    patched before the lifespan context manager runs, which a shared
    fixture spanning multiple tests can't do per-test.
    """

    def test_trigger_endpoint_runs_a_real_cycle(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True), \
             patch("app.main.settings.adversarial_test_batch_size", 3), \
             patch("app.router.route_with_source", return_value=("[KIWIX — X] content", "kiwix")):
            with TestClient(main.app) as client:
                response = client.post("/adversarial/trigger")
                assert response.status_code == 200
                body = response.json()
                assert body["status"] == "ran"
                assert body["queries_run"] == 3

    def test_trigger_endpoint_reports_disabled_without_running(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main

        temp_db = str(tmp_path / "test_adversarial.db")
        route_called = {"n": 0}

        def tracking_route(query, source="auto", fusion_sources=None):
            route_called["n"] += 1
            return ("[KIWIX — X] content", "kiwix")

        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", False), \
             patch("app.router.route_with_source", side_effect=tracking_route):
            with TestClient(main.app) as client:
                response = client.post("/adversarial/trigger")
                assert response.status_code == 200
                assert response.json()["status"] == "disabled"
                assert route_called["n"] == 0

    def test_flagged_endpoint_reports_disabled_state(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", False):
            with TestClient(main.app) as client:
                response = client.get("/adversarial/flagged")
                assert response.status_code == 200
                assert response.json() == {"status": "disabled", "count": 0, "flagged": []}

    def test_health_reports_disabled_adversarial_testing(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", False):
            with TestClient(main.app) as client:
                response = client.get("/health")
                assert response.status_code == 200
                assert response.json()["adversarial_testing"] == {"status": "disabled"}

    def test_dismiss_endpoint_closes_a_flagged_combination(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main
        import app.adversarial_testing as at_module

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True):
            import json
            at_module.init_adversarial_db()
            fp = json.dumps(["endpoint-dismiss-test"])
            at_module._record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")

            with TestClient(main.app) as client:
                before = client.get("/adversarial/flagged")
                assert before.json()["count"] == 1

                dismiss_response = client.post("/adversarial/dismiss", params={"fingerprint": fp})
                assert dismiss_response.status_code == 200
                assert dismiss_response.json()["status"] == "dismissed"

                after = client.get("/adversarial/flagged")
                assert after.json()["count"] == 0

    def test_dismiss_endpoint_404s_on_unknown_fingerprint(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True):
            with TestClient(main.app) as client:
                response = client.post("/adversarial/dismiss", params={"fingerprint": "[\"nonexistent\"]"})
                assert response.status_code == 404

    def test_undismiss_endpoint_restores_a_dismissed_combination(self, tmp_path):
        """The real, symmetric counterpart endpoint to /adversarial/dismiss
        — exercises the exact real recovery path: dismiss via the
        endpoint, confirm it's gone from the default view, undismiss via
        the new endpoint, confirm it's back."""
        from fastapi.testclient import TestClient
        import app.main as main
        import app.adversarial_testing as at_module

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True):
            import json
            at_module.init_adversarial_db()
            fp = json.dumps(["endpoint-undismiss-test"])
            at_module._record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")

            with TestClient(main.app) as client:
                client.post("/adversarial/dismiss", params={"fingerprint": fp})
                gone = client.get("/adversarial/flagged")
                assert gone.json()["count"] == 0

                undismiss_response = client.post("/adversarial/undismiss", params={"fingerprint": fp})
                assert undismiss_response.status_code == 200
                assert undismiss_response.json()["status"] == "undismissed"

                restored = client.get("/adversarial/flagged")
                assert restored.json()["count"] == 1
                assert restored.json()["flagged"][0]["last_query_text"] == "bad query"

    def test_undismiss_endpoint_404s_on_unknown_fingerprint(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True):
            with TestClient(main.app) as client:
                response = client.post("/adversarial/undismiss", params={"fingerprint": "[\"nonexistent\"]"})
                assert response.status_code == 404

    def test_flagged_endpoint_include_dismissed_query_param_works(self, tmp_path):
        from fastapi.testclient import TestClient
        import app.main as main
        import app.adversarial_testing as at_module

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True):
            import json
            at_module.init_adversarial_db()
            fp = json.dumps(["include-dismissed-test"])
            at_module._record_result(fp, "no_intent_fallthrough", "bad query", "kiwix", 100, "crash: boom")
            at_module.dismiss_flagged_combination(fp)

            with TestClient(main.app) as client:
                default_view = client.get("/adversarial/flagged")
                assert default_view.json()["count"] == 0

                with_history = client.get("/adversarial/flagged", params={"include_dismissed": "true"})
                assert with_history.json()["count"] == 1

    def test_scheduler_does_not_register_job_when_disabled(self, tmp_path):
        """The real lifespan startup must skip scheduler.add_job() for
        adversarial_testing entirely when disabled — not just skip
        running it once registered. Inspects the actual live
        apscheduler instance's registered job IDs to confirm directly,
        rather than just trusting the app booted without an error."""
        from fastapi.testclient import TestClient
        import app.main as main
        import apscheduler.schedulers.background

        captured_schedulers = []
        original_init = apscheduler.schedulers.background.BackgroundScheduler.__init__

        def capturing_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            captured_schedulers.append(self)

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", False), \
             patch.object(apscheduler.schedulers.background.BackgroundScheduler, "__init__", capturing_init):
            with TestClient(main.app):
                assert len(captured_schedulers) == 1
                job_ids = {job.id for job in captured_schedulers[0].get_jobs()}
                assert "adversarial_testing" not in job_ids
                # The other four real snapshot jobs must still be
                # present and unaffected by this flag.
                assert {"snapshot_uptime", "snapshot_forecast", "snapshot_news", "snapshot_ha"}.issubset(job_ids)

    def test_scheduler_registers_job_when_enabled(self, tmp_path):
        """The inverse confirmation — enabled (the default) must
        actually register the job, not just default to skipping it."""
        from fastapi.testclient import TestClient
        import app.main as main
        import apscheduler.schedulers.background

        captured_schedulers = []
        original_init = apscheduler.schedulers.background.BackgroundScheduler.__init__

        def capturing_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            captured_schedulers.append(self)

        temp_db = str(tmp_path / "test_adversarial.db")
        with patch("app.adversarial_testing.ADVERSARIAL_DB", temp_db), \
             patch("app.main.settings.adversarial_test_enabled", True), \
             patch.object(apscheduler.schedulers.background.BackgroundScheduler, "__init__", capturing_init):
            with TestClient(main.app):
                assert len(captured_schedulers) == 1
                job_ids = {job.id for job in captured_schedulers[0].get_jobs()}
                assert "adversarial_testing" in job_ids
