"""
Mnemolis Grounded Answer Synthesis (Design Doc 4)

An opt-in post-processing stage that has the local LLM compose a short,
grounded answer FROM THE RETRIEVED MATERIAL ONLY, returned alongside the
raw result — never instead of it. The primary consumer is the voice
pipeline, whose TTS otherwise reads fused headers and article boilerplate
aloud; the strongest hardware in the stack (the Beast running qwen3:8b)
sits one hop away doing nothing but routing.

Design constraints this module enforces, in the design doc's priority
order:

  1. Additive, never substitutive. Any failure — timeout, empty reply,
     gate rejection, LLM unconfigured — yields answer=None plus a
     synthesis_skipped/synthesis_rejected event. The caller keeps exactly
     the raw `result` it would have had today.
  2. Grounded or silent. The model answers only from the material. When
     the material doesn't contain the answer, the correct output is the
     honest miss "The retrieved sources don't answer this." — a real
     answer, because "the sources don't say" is a grounded answer.
  3. Never on the path of clients that don't want it. Gated behind the
     per-request `synthesize` flag and the SYNTHESIS_ENABLED master
     switch; a client that doesn't ask pays zero tokens of latency.
  4. Attribution survives synthesis. Multi-source answers must name which
     source they drew on; single-source answers carry a one-line source
     tag.
  5. Budgeted for voice. Length is a first-class request parameter,
     enforced by sentence-boundary truncation as a backstop.

Events land in the current route_query() explanation chain for free: this
module runs inside route_query()'s own _ROUTE_STATS context, so
router._route_event() records here reach the same trace every other
routing event does — see the _ROUTE_STATS comment in app/router.py.
"""

import logging
import re
import time

from app.config import settings
from app.sources import fusion

_LOGGER = logging.getLogger(__name__)

# The honest-miss answer — a success, not a failure (constraint #2). The
# model is prompted to reply exactly NOT_IN_SOURCES; gate 2 turns that
# sentinel into this human-facing sentence with empty attribution.
_NOT_IN_SOURCES_SENTINEL = "NOT_IN_SOURCES"
NOT_IN_SOURCES_ANSWER = "The retrieved sources don't answer this."

# "brief" is fixed at 800 chars — an internal middle point between the two
# real deployment-tunable extremes (SYNTHESIS_VOICE_MAX_CHARS /
# SYNTHESIS_MAX_CHARS), per the config-audit judgment that not every
# internal sizing constant is a genuine user preference.
_BRIEF_MAX_CHARS = 800

# Number tokens for the numeric-grounding gate: an integer or decimal,
# with optional thousands-commas and an optional trailing percent sign.
# Matches "9", "1,500", "72.5", "40%", "2026".
_NUMBER_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?%?")

# Trailing attribution parenthetical, e.g. "... (web, news)".
_TRAILING_PARENS = re.compile(r"\(([^()]*)\)\s*$")

# Sentence-ending punctuation, for the length backstop's truncation point.
_SENTENCE_END = re.compile(r"[.!?]")


class SynthesisOutcome:
    """Result of one synthesize() call.

    - answer is None on every skip/failure (constraint #1); the caller
      falls back to the raw result.
    - answer is a real string on success, including the honest
      NOT_IN_SOURCES miss (answer_sources is [] for that case).
    - synthesized is True iff answer is not None — the response's
      `synthesized` field maps to exactly this.
    """

    __slots__ = ("answer", "answer_sources", "synthesized")

    def __init__(self, answer: str | None, answer_sources: list[str]):
        self.answer = answer
        self.answer_sources = answer_sources
        self.synthesized = answer is not None

    @classmethod
    def skipped(cls) -> "SynthesisOutcome":
        return cls(None, [])


def _style_instruction(style: str) -> str:
    if style == "voice":
        return (
            "Answer in at most two short sentences, phrased naturally for "
            "reading aloud. No lists, headers, or markdown."
        )
    if style == "detailed":
        return "Answer in a few short paragraphs of plain prose."
    # brief (default)
    return "Answer in one short paragraph of plain prose."


def _style_cap(style: str) -> int:
    if style == "voice":
        return settings.synthesis_voice_max_chars
    if style == "detailed":
        return settings.synthesis_max_chars
    return _BRIEF_MAX_CHARS


def _parse_sections(result: str, source_used: str) -> list[tuple[str, str]]:
    """Split a retrieved result into (tag, text) sections.

    Uses fusion.HEADER_PATTERN — the exact, exported pattern
    fusion._format_header() writes with, never a re-derived one, so a
    header-format change can't drift this parser away from the producer
    (the _looks_empty() cross-file-drift bug is the cautionary tale;
    fusion.py's own drift test pins the pattern against a formatted
    header). A headerless result is one section attributed to
    source_used. Tags are lowercased so they match the "(web, news)"
    lowercase attribution the prompt asks the model to emit.
    """
    matches = list(fusion.HEADER_PATTERN.finditer(result))
    if not matches:
        return [(source_used.lower(), result.strip())]

    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        tag = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(result)
        text = result[start:end].strip()
        # Strip the "\n\n---\n\n" section separators fusion joins with,
        # so the model sees clean per-source content, not join glue.
        text = text.strip("-").strip()
        if text:
            sections.append((tag, text))
    if not sections:
        return [(source_used.lower(), result.strip())]
    return sections


def _apportion_budget(sections: list[tuple[str, str]], total_budget: int) -> list[tuple[str, str]]:
    """Truncate each section proportionally so the combined material fits
    total_budget characters — keeps synthesis latency predictable and is
    configurable down for smaller-context models."""
    combined = sum(len(text) for _, text in sections)
    if combined <= total_budget or combined == 0:
        return sections
    out: list[tuple[str, str]] = []
    for tag, text in sections:
        share = max(1, int(total_budget * (len(text) / combined)))
        out.append((tag, text[:share]))
    return out


def _build_prompt(query: str, sections: list[tuple[str, str]], style: str) -> str:
    material_lines = "\n\n".join(f"[{tag}] {text}" for tag, text in sections)
    return (
        "Answer the question using ONLY the material below. Rules:\n"
        f"- If the material does not contain the answer, reply exactly: {_NOT_IN_SOURCES_SENTINEL}\n"
        "- Never add facts, numbers, names, or dates that are not in the material.\n"
        f"- {_style_instruction(style)}\n"
        "- End with the source tags you used, in parentheses, e.g. (web, news).\n\n"
        f"Question: {query}\n\n"
        "Material:\n"
        f"{material_lines}\n\n"
        "Answer:"
    )


def _normalize_echo(text: str) -> str:
    """Lowercase, drop non-alphanumerics, collapse — for the echo guard's
    normalized-equality check between answer body and question."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _truncate_to_sentence(text: str, cap: int) -> str:
    """Truncate at the last sentence boundary at or before `cap`, never
    mid-sentence (constraint #5 voice backstop). Falls back to a hard cut
    only when there is no sentence boundary to land on."""
    if len(text) <= cap:
        return text
    window = text[:cap]
    ends = list(_SENTENCE_END.finditer(window))
    if ends:
        return window[: ends[-1].end()].strip()
    return window.strip()


def _extract_number_tokens(text: str) -> list[str]:
    return _NUMBER_TOKEN.findall(text)


def _numeric_grounded(answer_body: str, material: str) -> str | None:
    """Return the first number token in answer_body that does NOT appear
    in material (after normalizing commas), or None if every number is
    grounded. The four-digit current year, standing alone, is exempt —
    it's the one number a model can legitimately supply unprompted.
    Numbers are where confident invention does the most damage
    (temperatures, versions, scores), so a miss here rejects the answer.
    """
    material_norm = material.replace(",", "")
    current_year = time.strftime("%Y")
    for token in _extract_number_tokens(answer_body):
        digits = token.rstrip("%").replace(",", "")
        if digits == current_year:
            continue
        if digits not in material_norm:
            return token
    return None


def _parse_attribution(
    body: str, offered_tags: list[str], multi_section: bool, source_used: str
) -> tuple[str, list[str] | None]:
    """Extract and validate the trailing "(tag, tag)" attribution.

    Returns (body_without_attribution, answer_sources) on acceptance, or
    (body, None) to signal REJECT for a multi-section input whose
    attribution is missing or unparseable — a multi-source answer that
    can't say who said what doesn't ship (constraint #4). Single-section
    inputs never reject here: a missing/garbled tag falls back to
    [source_used].
    """
    offered = set(offered_tags)
    m = _TRAILING_PARENS.search(body)
    if m:
        parsed = [t.strip().lower() for t in m.group(1).split(",") if t.strip()]
        stripped_body = body[: m.start()].strip()
        if parsed and all(t in offered for t in parsed):
            # Preserve order, dedupe.
            seen: list[str] = []
            for t in parsed:
                if t not in seen:
                    seen.append(t)
            return stripped_body, seen
        # Parenthetical present but not a clean subset of offered tags.
        if multi_section:
            return body, None
        return stripped_body, [source_used.lower()]
    # No trailing parenthetical at all.
    if multi_section:
        return body, None
    return body, [source_used.lower()]


def _format_answer(body: str, answer_sources: list[str]) -> str:
    if answer_sources:
        return f"{body} ({', '.join(answer_sources)})"
    return body


def _synth_cache_query(style: str, query: str) -> str:
    """The query component of the synthesis cache key.

    Stored via router._get_cached/_set_cached under source=source_used, so
    the effective key is "{source_used}:synth:{style}:{query}". Ordering
    source FIRST (rather than the design doc's illustrative
    "synth:{style}:{source}:{query}") is what makes TTL inheritance
    actually work: router's cache resolves a key's TTL from its leading
    "source:" segment, so a synth answer grounded in `web` correctly
    expires on the web TTL and can never outlive the retrieval it grounds.
    Every existing clear path (/cache/clear, eviction, reload) keys off
    the same dict, so invalidation needs nothing new.
    """
    return f"synth:{style}:{query.lower().strip()}"


def synthesize(query: str, result: str, source_used: str, style: str = "brief") -> SynthesisOutcome:
    """Compose a grounded answer from `result`, or skip cleanly.

    Never raises: llm.generate() already returns None on any failure, and
    every gate returns a SynthesisOutcome. The caller (route_query) always
    has the raw result to fall back on.
    """
    # Lazy import — router imports synthesis (via route_query), so a
    # top-level import here would be circular; same pattern
    # query_expansion.py uses for its router accessors.
    from app.router import _get_cached, _set_cached, _looks_empty, _route_event

    if style not in ("voice", "brief", "detailed"):
        style = "brief"

    # --- feature-level gates (can't synthesize at all) --------------------
    if not settings.synthesis_enabled:
        _route_event("synthesis_skipped", reason="disabled")
        return SynthesisOutcome.skipped()
    if not (settings.llm_url and settings.llm_model):
        _route_event("synthesis_skipped", reason="llm_unconfigured")
        return SynthesisOutcome.skipped()

    # --- cache check (before content pre-flight, ahead of any LLM call) ---
    cache_q = _synth_cache_query(style, query)
    cached = _get_cached(source_used, cache_q)
    if cached is not None:
        _route_event("synthesis_cached", style=style, source=source_used)
        # Reconstruct answer_sources from the cached answer's own trailing
        # attribution — the honest miss is never cached, so a cached answer
        # always carries at least one source tag to read back.
        m = _TRAILING_PARENS.search(cached)
        sources = (
            [t.strip().lower() for t in m.group(1).split(",") if t.strip()] if m else [source_used.lower()]
        )
        return SynthesisOutcome(cached, sources)

    # --- content pre-flight (cheap rejections before the LLM call) --------
    if _looks_empty(result):
        _route_event("synthesis_skipped", reason="empty_result")
        return SynthesisOutcome.skipped()
    if source_used == "changes":
        # format_changes() output is already synthesized prose at any
        # length; re-synthesizing prose into prose is pure invention risk
        # with no retrieval benefit, and voice can read a changes digest
        # directly. (Deviation from the design's literal "changes with a
        # short result" — see the delivery note; skipping unconditionally
        # avoids inventing an undocumented second length threshold.)
        _route_event("synthesis_skipped", reason="changes_prose")
        return SynthesisOutcome.skipped()
    if len(result) < settings.synthesis_min_input_chars:
        _route_event("synthesis_skipped", reason="input_too_short")
        return SynthesisOutcome.skipped()

    # --- prompt assembly --------------------------------------------------
    sections = _apportion_budget(
        _parse_sections(result, source_used), settings.synthesis_input_budget_chars
    )
    offered_tags = [tag for tag, _ in sections]
    multi_section = len({tag for tag in offered_tags}) > 1
    prompt = _build_prompt(query, sections, style)
    material = "\n".join(text for _, text in sections)

    cap = _style_cap(style)
    # Room for the answer plus its trailing attribution, sized from the
    # character cap (~4 chars/token) with margin.
    max_tokens = max(128, cap // 3 + 96)
    model = settings.synthesis_model or None

    invoke_start = time.monotonic()
    from app.llm import generate

    raw = generate(
        prompt,
        max_tokens=max_tokens,
        temperature=0.1,
        timeout=settings.synthesis_timeout_seconds,
        model=model,
    )
    elapsed_ms = int((time.monotonic() - invoke_start) * 1000)

    # --- gates ------------------------------------------------------------
    # Gate 1: empty / whitespace (also covers timeout → generate() None).
    if not raw or not raw.strip():
        _route_event("synthesis_rejected", gate="empty")
        return SynthesisOutcome.skipped()
    reply = raw.strip()

    # Gate 2: NOT_IN_SOURCES → honest miss (a success, not a rejection).
    if reply.strip(".").strip().upper() == _NOT_IN_SOURCES_SENTINEL:
        _route_event(
            "synthesis_invoked", elapsed_ms=elapsed_ms, model=model or settings.llm_model,
            sections=len(sections), style=style,
        )
        # Deliberately NOT cached: the underlying result may improve within
        # its TTL via fallback variance, and a cached "don't know" is the
        # Cached Failure Bug (found three times — its wiki page is why this
        # line exists).
        return SynthesisOutcome(NOT_IN_SOURCES_ANSWER, [])

    # Gate 4: attribution parse (before length, so truncation can't eat the
    # trailing tags). Multi-section with unparseable/missing attribution is
    # a hard reject; single-section falls back to [source_used].
    body, answer_sources = _parse_attribution(reply, offered_tags, multi_section, source_used)
    if answer_sources is None:
        _route_event("synthesis_rejected", gate="attribution")
        return SynthesisOutcome.skipped()

    # Gate 6: echo — the answer body (attribution stripped) must not be the
    # question restated. Checked on the body, not the raw reply, so a
    # trailing "(web)" can't mask an otherwise-verbatim echo.
    if _normalize_echo(body) == _normalize_echo(query):
        _route_event("synthesis_rejected", gate="echo")
        return SynthesisOutcome.skipped()

    # Gate 5: numeric grounding — every number in the body must appear in
    # the material. A miss rejects; the offending token is logged at
    # WARNING (the live-verification hook the incident pattern shows will
    # find the real bugs).
    offender = _numeric_grounded(body, material)
    if offender is not None:
        _LOGGER.warning(
            "Synthesis rejected: ungrounded number %r not in material for query %r",
            offender, query[:80],
        )
        _route_event("synthesis_rejected", gate="numeric")
        return SynthesisOutcome.skipped()

    # Gate 3: length ceiling — truncate the body at a sentence boundary,
    # leaving room for the attribution suffix, then recombine.
    suffix = f" ({', '.join(answer_sources)})" if answer_sources else ""
    body = _truncate_to_sentence(body, max(1, cap - len(suffix)))
    answer = _format_answer(body, answer_sources)

    _route_event(
        "synthesis_invoked", elapsed_ms=elapsed_ms, model=model or settings.llm_model,
        sections=len(sections), style=style,
    )
    _set_cached(source_used, cache_q, answer)
    return SynthesisOutcome(answer, answer_sources)
