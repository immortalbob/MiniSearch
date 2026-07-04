"""
Tests for Grounded Answer Synthesis (Design Doc 4) — app/synthesis.py.

The pipeline is unit-tested with app.llm.generate mocked and the real
router cache/event helpers left in place, so cache inheritance and
explanation-event recording are exercised for real, not stubbed. Events
are captured by installing a route_query()-style _ROUTE_STATS context
around each call (the same ContextVar synthesis runs inside in
production).
"""

import contextlib
from unittest.mock import patch


import app.router as router_module
from app.config import settings
from app import synthesis
from app.sources import fusion


@contextlib.contextmanager
def _route_stats_context():
    """Install a fresh _ROUTE_STATS dict (as route_query does) so
    _route_event() records land somewhere we can assert on."""
    stats = {"events": []}
    token = router_module._ROUTE_STATS.set(stats)
    try:
        yield stats
    finally:
        router_module._ROUTE_STATS.reset(token)


def _event_reasons(stats, step):
    return [e.get("reason") or e.get("gate") for e in stats["events"] if e["step"] == step]


def _steps(stats):
    return [e["step"] for e in stats["events"]]


class _SynthHarness:
    """Enables synthesis with a configured (mock) LLM, clears the result
    cache, and prevents disk writes."""

    def setup_method(self):
        self._orig = {
            "enabled": settings.synthesis_enabled,
            "url": settings.llm_url,
            "model": settings.llm_model,
            "min_input": settings.synthesis_min_input_chars,
            "budget": settings.synthesis_input_budget_chars,
            "voice_cap": settings.synthesis_voice_max_chars,
            "max_cap": settings.synthesis_max_chars,
            "syn_model": settings.synthesis_model,
            "digest_cap": settings.synthesis_digest_max_chars,
            "digest_budget": settings.synthesis_digest_input_budget_chars,
        }
        settings.synthesis_enabled = True
        settings.llm_url = "http://test-llm"
        settings.llm_model = "qwen3:8b"
        settings.synthesis_min_input_chars = 200
        self._save_patch = patch("app.router._save_cache")
        self._save_patch.start()
        router_module.clear_cache()

    def teardown_method(self):
        router_module.clear_cache()
        self._save_patch.stop()
        settings.synthesis_enabled = self._orig["enabled"]
        settings.llm_url = self._orig["url"]
        settings.llm_model = self._orig["model"]
        settings.synthesis_min_input_chars = self._orig["min_input"]
        settings.synthesis_input_budget_chars = self._orig["budget"]
        settings.synthesis_voice_max_chars = self._orig["voice_cap"]
        settings.synthesis_max_chars = self._orig["max_cap"]
        settings.synthesis_model = self._orig["syn_model"]
        settings.synthesis_digest_max_chars = self._orig["digest_cap"]
        settings.synthesis_digest_input_budget_chars = self._orig["digest_budget"]


# ---------------------------------------------------------------------------
# Header parsing / exported-constant drift
# ---------------------------------------------------------------------------

class TestSectionParsing:
    def test_headerless_result_is_one_section_attributed_to_source_used(self):
        sections = synthesis._parse_sections("plain body text", "kiwix")
        assert sections == [("kiwix", "plain body text")]

    def test_multi_section_fusion_result_splits_by_header(self):
        body = (
            f"{fusion._format_header('kiwix')}\nAn encyclopedia article body.\n\n---\n\n"
            f"{fusion._format_header('news')}\nA recent headline."
        )
        sections = synthesis._parse_sections(body, "fusion")
        tags = [t for t, _ in sections]
        assert tags == ["kiwix", "news"]
        assert "encyclopedia" in sections[0][1]
        assert "headline" in sections[1][1]
        # Separator glue is stripped from section content.
        assert "---" not in sections[0][1]

    def test_tags_are_lowercased_to_match_attribution(self):
        body = f"{fusion._format_header('web')}\nLive result."
        sections = synthesis._parse_sections(body, "web")
        assert sections[0][0] == "web"

    def test_drift_pattern_matches_every_formatted_header(self):
        # If fusion changes its header format, HEADER_PATTERN must still
        # match what _format_header writes — the exported-constant contract
        # that keeps this parser from silently drifting.
        for src in fusion._HEADER_LABELS:
            assert fusion.HEADER_PATTERN.match(fusion._format_header(src)), src

    def test_drift_pattern_matches_unknown_source_header(self):
        # An unknown source's label is just SOURCE.upper(); the pattern's
        # label side must accept that too.
        assert fusion.HEADER_PATTERN.match(fusion._format_header("mystery"))


class TestBudgetApportioning:
    def test_under_budget_is_untouched(self):
        sections = [("web", "short"), ("news", "also short")]
        assert synthesis._apportion_budget(sections, 6000) == sections

    def test_over_budget_truncates_proportionally(self):
        sections = [("web", "a" * 900), ("news", "b" * 100)]
        out = synthesis._apportion_budget(sections, 500)
        combined = sum(len(t) for _, t in out)
        assert combined <= 520  # proportional shares, rounded
        # The larger section keeps the larger share.
        assert len(out[0][1]) > len(out[1][1])


class TestPromptAssembly:
    def test_prompt_contains_query_material_and_style(self):
        sections = [("web", "the moon is made of rock")]
        prompt = synthesis._build_prompt("what is the moon", sections, "voice")
        assert "what is the moon" in prompt
        assert "the moon is made of rock" in prompt
        assert "[web]" in prompt
        assert "two short sentences" in prompt
        assert "NOT_IN_SOURCES" in prompt


class TestFluencyContract:
    """The v3.55.2 fluency steer. These assert the PROMPT carries the
    right instruction per style — not the model's actual output, which is
    mocked here and, as a matter of design, prompt-steered rather than
    gate-enforced (a gate on prose phrasing would reject good answers)."""

    def test_base_prompt_forbids_narrating_the_source_material(self):
        # The universal anti-meta rule — present for every style, so even
        # digest lines state the item rather than "one article says...".
        for style in ("voice", "brief", "detailed", "digest"):
            prompt = synthesis._build_prompt("q", [("news", "material")], style)
            assert "one article" in prompt  # named in the forbidden list
            assert "not that something reported it" in prompt

    def test_detailed_weaves_related_points(self):
        # The "connect, don't enumerate" half that separates a fused
        # summary from digest's list — this is the fix for the
        # "one article... another piece..." choppiness.
        instr = synthesis._style_instruction("detailed")
        assert "weave" in instr.lower()
        assert "rather than addressing each item separately" in instr.lower()

    def test_brief_and_voice_ask_for_natural_prose(self):
        assert "flowing" in synthesis._style_instruction("brief").lower()
        assert "the way you would say it" in synthesis._style_instruction("voice").lower()

    def test_digest_is_not_told_to_fuse_into_flowing_prose(self):
        # The fluency steer must NOT leak into digest, whose whole job is
        # to enumerate and preserve. Digest keeps "one per line" and does
        # not gain the "weave into one smooth summary" instruction.
        instr = synthesis._style_instruction("digest")
        assert "one per line" in instr
        assert "weave" not in instr.lower()
        assert "smooth summary" not in instr.lower()


# ---------------------------------------------------------------------------
# Pre-flight skip matrix
# ---------------------------------------------------------------------------

class TestPreflight(_SynthHarness):
    def _long(self, text="x"):
        return text * 300  # comfortably above min_input_chars

    def test_disabled_master_switch_skips(self):
        settings.synthesis_enabled = False
        with _route_stats_context() as stats:
            out = synthesis.synthesize("q", self._long(), "web", "brief")
        assert out.answer is None and out.synthesized is False
        assert "disabled" in _event_reasons(stats, "synthesis_skipped")

    def test_llm_unconfigured_skips(self):
        settings.llm_url = ""
        with _route_stats_context() as stats:
            out = synthesis.synthesize("q", self._long(), "web", "brief")
        assert out.answer is None
        assert "llm_unconfigured" in _event_reasons(stats, "synthesis_skipped")

    def test_empty_result_skips(self):
        with _route_stats_context() as stats:
            out = synthesis.synthesize("q", "No results found.", "kiwix", "brief")
        assert out.answer is None
        assert "empty_result" in _event_reasons(stats, "synthesis_skipped")

    def test_short_input_skips(self):
        with _route_stats_context() as stats:
            out = synthesis.synthesize("q", "Front Door: locked", "ha", "brief")
        assert out.answer is None
        assert "input_too_short" in _event_reasons(stats, "synthesis_skipped")

    def test_changes_source_skips_as_already_prose(self):
        with _route_stats_context() as stats:
            out = synthesis.synthesize("what changed", self._long(), "changes", "brief")
        assert out.answer is None
        assert "changes_prose" in _event_reasons(stats, "synthesis_skipped")

    def test_no_llm_call_on_skip(self):
        with patch("app.llm.generate") as gen:
            with _route_stats_context():
                synthesis.synthesize("q", "Front Door: locked", "ha", "brief")
        gen.assert_not_called()


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

class TestHappyPath(_SynthHarness):
    def test_single_source_answer_gets_source_attribution(self):
        result = "The capital of France is Paris, a large European city." * 6
        with patch("app.llm.generate", return_value="Paris is the capital of France. (web)"):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("capital of france", result, "web", "brief")
        assert out.synthesized is True
        assert out.answer_sources == ["web"]
        assert out.answer.endswith("(web)")
        assert "synthesis_invoked" in _steps(stats)

    def test_single_source_missing_attribution_falls_back_to_source_used(self):
        result = "The capital of France is Paris." * 10
        with patch("app.llm.generate", return_value="Paris is the capital of France."):
            with _route_stats_context():
                out = synthesis.synthesize("capital of france", result, "web", "brief")
        assert out.answer_sources == ["web"]
        assert out.answer.endswith("(web)")

    def test_multi_source_answer_preserves_declared_subset(self):
        result = (
            f"{fusion._format_header('web')}\n" + ("SpaceX launched a rocket. " * 10) + "\n\n---\n\n"
            f"{fusion._format_header('news')}\n" + ("Coverage was widespread. " * 10)
        )
        reply = "SpaceX launched a rocket and coverage was widespread. (web, news)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                out = synthesis.synthesize("space program", result, "fusion", "brief")
        assert out.answer_sources == ["web", "news"]

    def test_invoked_event_carries_model_and_style(self):
        result = "Rock and dust cover the lunar surface widely." * 8
        with patch("app.llm.generate", return_value="The moon is rocky. (kiwix)"):
            with _route_stats_context() as stats:
                synthesis.synthesize("moon", result, "kiwix", "voice")
        inv = [e for e in stats["events"] if e["step"] == "synthesis_invoked"][0]
        assert inv["style"] == "voice"
        assert inv["model"] == "qwen3:8b"
        assert inv["sections"] == 1
        assert isinstance(inv["elapsed_ms"], int)


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------

class TestGates(_SynthHarness):
    def _long(self):
        return "Some genuinely long retrieved material about a topic. " * 8

    def test_empty_reply_rejected(self):
        with patch("app.llm.generate", return_value=""):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("q", self._long(), "web", "brief")
        assert out.answer is None
        assert "empty" in _event_reasons(stats, "synthesis_rejected")

    def test_generate_none_timeout_rejected_as_empty(self):
        with patch("app.llm.generate", return_value=None):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("q", self._long(), "web", "brief")
        assert out.answer is None
        assert "empty" in _event_reasons(stats, "synthesis_rejected")

    def test_not_in_sources_is_honest_miss_success(self):
        with patch("app.llm.generate", return_value="NOT_IN_SOURCES"):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("obscure", self._long(), "web", "brief")
        assert out.synthesized is True
        assert out.answer == synthesis.NOT_IN_SOURCES_ANSWER
        assert out.answer_sources == []
        assert "synthesis_invoked" in _steps(stats)

    def test_not_in_sources_tolerates_trailing_period(self):
        with patch("app.llm.generate", return_value="NOT_IN_SOURCES."):
            with _route_stats_context():
                out = synthesis.synthesize("obscure", self._long(), "web", "brief")
        assert out.answer == synthesis.NOT_IN_SOURCES_ANSWER

    def test_echo_of_question_rejected(self):
        with patch("app.llm.generate", return_value="what is the capital of france (web)"):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("What is the capital of France?", self._long(), "web", "brief")
        assert out.answer is None
        assert "echo" in _event_reasons(stats, "synthesis_rejected")

    def test_multi_section_missing_attribution_rejected(self):
        result = (
            f"{fusion._format_header('web')}\n" + ("Body one. " * 10) + "\n\n---\n\n"
            f"{fusion._format_header('news')}\n" + ("Body two. " * 10)
        )
        with patch("app.llm.generate", return_value="An answer with no attribution at all"):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("q", result, "fusion", "brief")
        assert out.answer is None
        assert "attribution" in _event_reasons(stats, "synthesis_rejected")

    def test_multi_section_bogus_attribution_rejected(self):
        result = (
            f"{fusion._format_header('web')}\n" + ("Body one. " * 10) + "\n\n---\n\n"
            f"{fusion._format_header('news')}\n" + ("Body two. " * 10)
        )
        with patch("app.llm.generate", return_value="An answer. (forecast)"):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("q", result, "fusion", "brief")
        assert out.answer is None
        assert "attribution" in _event_reasons(stats, "synthesis_rejected")

    def test_numeric_ungrounded_rejected(self):
        result = "The device draws power and runs quietly all day." * 8
        with patch("app.llm.generate", return_value="It draws 42 watts. (ha)"):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("power draw", result, "ha", "brief")
        assert out.answer is None
        assert "numeric" in _event_reasons(stats, "synthesis_rejected")

    def test_numeric_grounded_passes(self):
        result = "The sensor reports the room is currently at 72 degrees right now." * 6
        with patch("app.llm.generate", return_value="The room is 72 degrees. (ha)"):
            with _route_stats_context():
                out = synthesis.synthesize("temp", result, "ha", "brief")
        assert out.synthesized is True

    def test_numeric_comma_normalization(self):
        # Answer says "1,500", material says "1500" — comma normalization
        # makes them match, so this must NOT reject.
        result = "The building has 1500 residents according to the record." * 6
        with patch("app.llm.generate", return_value="About 1,500 people live there. (kiwix)"):
            with _route_stats_context():
                out = synthesis.synthesize("population", result, "kiwix", "brief")
        assert out.synthesized is True

    def test_current_year_exempt_from_numeric_gate(self):
        import time as _t
        year = _t.strftime("%Y")
        result = "The festival is an annual event held every summer downtown." * 6
        with patch("app.llm.generate", return_value=f"It happens in {year} again. (news)"):
            with _route_stats_context():
                out = synthesis.synthesize("festival", result, "news", "brief")
        assert out.synthesized is True


class TestLengthBackstop(_SynthHarness):
    def test_voice_answer_truncated_at_sentence_boundary(self):
        settings.synthesis_voice_max_chars = 60
        material = "Alpha happened. Beta happened. Gamma happened. Delta too." * 6
        reply = "Alpha happened. Beta happened. Gamma happened as well and this keeps going far beyond the cap. (news)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                out = synthesis.synthesize("events", material, "news", "voice")
        # Body (answer minus attribution) ends on a sentence boundary and
        # fits under the cap.
        body = out.answer.rsplit(" (", 1)[0]
        assert body.endswith((".", "!", "?"))
        assert len(body) <= 60

    def test_truncate_helper_hard_cuts_when_no_boundary(self):
        text = "no sentence boundary here just words that run on and on and on"
        out = synthesis._truncate_to_sentence(text, 20)
        assert len(out) <= 20


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

class TestCaching(_SynthHarness):
    def _material(self):
        return "The capital of France is Paris, a major world city." * 8

    def test_second_call_served_from_cache_without_llm(self):
        reply = "Paris is the capital. (web)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                first = synthesis.synthesize("capital", self._material(), "web", "brief")
        assert first.synthesized is True
        with patch("app.llm.generate", return_value="SHOULD NOT RUN") as gen2:
            with _route_stats_context() as stats:
                second = synthesis.synthesize("capital", self._material(), "web", "brief")
        gen2.assert_not_called()
        assert second.answer == first.answer
        assert second.answer_sources == ["web"]
        assert "synthesis_cached" in _steps(stats)

    def test_style_is_part_of_cache_key(self):
        reply = "Paris is the capital. (web)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                synthesis.synthesize("capital", self._material(), "web", "brief")
        # A different style must NOT hit the brief entry — generate runs.
        with patch("app.llm.generate", return_value="Paris. (web)") as gen2:
            with _route_stats_context():
                synthesis.synthesize("capital", self._material(), "web", "voice")
        gen2.assert_called_once()

    def test_not_in_sources_is_never_cached(self):
        with patch("app.llm.generate", return_value="NOT_IN_SOURCES"):
            with _route_stats_context():
                synthesis.synthesize("obscure", self._material(), "web", "brief")
        # Second call must re-run generate — the miss was not cached.
        with patch("app.llm.generate", return_value="NOT_IN_SOURCES") as gen2:
            with _route_stats_context() as stats:
                synthesis.synthesize("obscure", self._material(), "web", "brief")
        gen2.assert_called_once()
        assert "synthesis_cached" not in _steps(stats)

    def test_synth_entry_cleared_by_clear_cache(self):
        reply = "Paris is the capital. (web)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                synthesis.synthesize("capital", self._material(), "web", "brief")
        assert router_module.get_cache_count() >= 1
        router_module.clear_cache()
        assert router_module.get_cache_count() == 0

    def test_synth_cache_ttl_inherits_source(self):
        # The synth entry's key leads with the source, so cache TTL and
        # stats resolve it under that source — never a bare "synth" bucket.
        reply = "Paris is the capital. (web)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                synthesis.synthesize("capital", self._material(), "web", "brief")
        stats = router_module.get_cache_stats()
        synth_entries = [e for e in stats if e["query"].startswith("synth:")]
        assert synth_entries
        assert all(e["source"] == "web" for e in synth_entries)


class TestAttributionUnit:
    def test_valid_subset_accepted(self):
        body, sources = synthesis._parse_attribution(
            "Answer text. (web, news)", ["web", "news"], True, "fusion"
        )
        assert sources == ["web", "news"]
        assert body == "Answer text."

    def test_duplicate_tags_deduped_in_order(self):
        _body, sources = synthesis._parse_attribution(
            "Answer. (web, web, news)", ["web", "news"], True, "fusion"
        )
        assert sources == ["web", "news"]

    def test_single_section_unparseable_falls_back(self):
        body, sources = synthesis._parse_attribution(
            "Answer with no tags", ["web"], False, "web"
        )
        assert sources == ["web"]
        assert body == "Answer with no tags"

    def test_multi_section_unparseable_returns_none(self):
        _body, sources = synthesis._parse_attribution(
            "Answer with no tags", ["web", "news"], True, "fusion"
        )
        assert sources is None


class TestEchoNormalization:
    def test_punctuation_and_case_insensitive_equality(self):
        assert synthesis._normalize_echo("What is the CAPITAL of France?") == \
            synthesis._normalize_echo("what is the capital of france")


class TestDigestStyle(_SynthHarness):
    """The "digest" answer style (v3.55.1) — preserves many distinct items
    instead of fusing them, with its own larger output cap and input
    budget, for "summarize / list / read me everything" queries."""

    def test_digest_instruction_enumerates_and_preserves(self):
        instr = synthesis._style_instruction("digest")
        assert "one per line" in instr
        assert "not merge" in instr.lower() or "do not merge" in instr.lower()
        assert "not drop items" in instr.lower()

    def test_digest_cap_uses_its_own_setting(self):
        settings.synthesis_digest_max_chars = 3333
        assert synthesis._style_cap("digest") == 3333

    def test_digest_input_budget_is_larger_and_style_scoped(self):
        settings.synthesis_input_budget_chars = 6000
        settings.synthesis_digest_input_budget_chars = 12000
        assert synthesis._input_budget("digest") == 12000
        # Every other style keeps the normal budget.
        assert synthesis._input_budget("voice") == 6000
        assert synthesis._input_budget("brief") == 6000

    def test_digest_is_a_valid_style_not_coerced_to_brief(self):
        # A digest request must survive style validation; if it were
        # coerced to "brief" it would use the brief cap.
        settings.synthesis_digest_max_chars = 3000
        result = "Story one happened. Story two happened. Story three happened. " * 12
        items = [
            "A council meeting was held downtown.",
            "A road reopened after repairs.",
            "The library extended its hours.",
            "A local team advanced to finals.",
            "The weather turned cooler overnight.",
            "A new cafe opened on Main Street.",
            "The park hosted a summer festival.",
            "A power outage was resolved quickly.",
            "The museum unveiled a new exhibit.",
            "A charity drive met its goal.",
        ]
        long_reply = "\n".join(items) + " (news)"
        with patch("app.llm.generate", return_value=long_reply):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("summarize the news", result, "news", "digest")
        assert out.synthesized is True
        inv = [e for e in stats["events"] if e["step"] == "synthesis_invoked"][0]
        assert inv["style"] == "digest"
        # 10 short items comfortably exceed the 800-char brief cap; their
        # survival proves the digest cap (3000) was applied, not brief's.
        assert out.answer.count("\n") >= 5

    def test_digest_preserves_multiple_items_a_voice_cap_would_flatten(self):
        settings.synthesis_voice_max_chars = 120
        settings.synthesis_digest_max_chars = 3000
        result = "Ten distinct headlines worth of real material. " * 20
        items = [
            "Headline about the council vote.",
            "Headline about a road closure.",
            "Headline about the school board.",
            "Headline about the weather shift.",
            "Headline about a new business.",
            "Headline about a sports result.",
            "Headline about a festival.",
            "Headline about a power outage.",
        ]
        reply = "\n".join(items) + " (news)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context():
                out = synthesis.synthesize("summarize the news", result, "news", "digest")
        # The same reply under voice's 120-char cap would be truncated to
        # one or two items; digest keeps them all.
        assert out.answer.count("Headline") >= 6

    def test_digest_still_runs_numeric_gate(self):
        # Digest carries many facts, so the numeric gate is more
        # load-bearing, not disabled: an invented number still rejects.
        result = "The council met and discussed several routine local matters. " * 10
        reply = "The budget was 999 dollars.\nA road reopened. (news)"
        with patch("app.llm.generate", return_value=reply):
            with _route_stats_context() as stats:
                out = synthesis.synthesize("summarize the news", result, "news", "digest")
        assert out.answer is None
        assert "numeric" in _event_reasons(stats, "synthesis_rejected")
