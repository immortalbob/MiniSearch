# Conditional Query Detection

Conditional detection recognizes a leading **"if X, Y"** structure — *"if the back door is unlocked, let me know"* — and answers it honestly: it searches the condition, and either gives a real yes/no verdict (when the underlying source genuinely supports one) or presents the raw result and says so plainly when it doesn't. Mnemolis has no reminder or trigger capability, so it can never actually act on the consequence — what it can do is make sure the response is framed around the condition's real, current answer instead of just restating the question back.

## Why the pattern is this narrow

The detector only matches a leading `"if "` / `"should "` / `"in case "` followed eventually by a comma:

```regex
^(if|should|in case)\s+(.+?),\s*(.+)$
```

This is deliberately restrictive, and the restriction is the actual design decision worth understanding. **"If" is genuinely ambiguous in English** — it has a conditional sense (*"if it's raining, bring an umbrella"*) and a "whether" sense (*"check if the lights are on"* means *"check whether the lights are on,"* not a real condition at all). The whether sense never shows up at the very start of a sentence followed by a comma; it's always embedded after a verb like "check," "see," or "tell me." Restricting to the leading-comma form sidesteps the ambiguity entirely, rather than trying to guess at it from surrounding verbs.

A few phrasings are deliberately, permanently out of scope as a result:

- **Mid-sentence or trailing "if"** — *"remind me to bring an umbrella if it's raining"* doesn't match. Out of scope for the same reason as above.
- **"Let me know if X"** — genuinely ambiguous even to a human reader. Could mean "tell me the current status" or "notify me if it changes." Not safely interpretable either way, so it's left alone.
- **No comma at all** — *"if the front door is unlocked tell me"* (missing the comma) doesn't match. This is a real, accepted limitation, not an oversight — distinguishing this reliably from "whether" usage would require actual grammatical parsing, not pattern matching, and that's a different kind of project.

## Honest abstention — the actual point of this feature

Once a condition is extracted, the real question is whether Mnemolis can say anything meaningful about whether it's *true*. This is restricted to exactly three sources with a genuinely structured, binary signal:

```text
_YES_NO_INTERPRETABLE_SOURCES = {"ha", "uptime", "forecast"}
```

- **`ha`** — checks for "locked"/"unlocked" keywords in the condition, then in the result. The result-side check specifically looks for "unlocked" before "locked," in that fixed order, regardless of which state the condition asserted — "locked" is a literal substring of "unlocked," so checking for it first would incorrectly match an "unlocked" result as if it said "locked."
- **`uptime`** — checks `"down"`/`"not up"` (the condition implying something's broken) or `"up"`/`"running"`/`"working"` (implying it should be fine) in the condition, against `"down"` or `"all"`+`"up"` together in the result
- **`forecast`** — checks for `"rain"`/`"raining"` in the condition specifically (deliberately narrow, never a broader "bad weather" guess); if found, checks the result for `"rain"`, `"storm"`, or `"shower"` language to confirm, or `"clear"` to deny. The condition-side and result-side keyword sets are deliberately asymmetric — `"storm"`/`"shower"` are real, valid ways the *result* might describe rain happening, but a *condition* phrased as "if there's a storm" (no mention of rain) isn't matched at all, since there's no `positive_condition_keywords` or storm-specific condition check for this source. Not a bug — see the next paragraph for what's actually interpretable here and what isn't.

Every other source — Kiwix, web, news — is **never** interpreted, on purpose. There's no structured signal to check against in free text, and guessing wrong would actively mislead rather than just be unhelpful. Even within the three interpretable sources, a genuinely subjective condition like *"if it's hot enough this week"* correctly returns no verdict, because there's no universal threshold for "hot enough" to check against — `_interpret_yes_no` returns `None` here, not a guess.

The `ha` source's specific "check 'unlocked' before 'locked', regardless of context" ordering (above) is a real, deliberate guard against a substring trap, not an arbitrary implementation detail — worth knowing if this logic is ever refactored or generalized, since a unified version that instead checks whichever keyword matches the condition's own stated polarity first gets the "condition says locked, result says unlocked" case backwards. This exact regression was caught once already, before shipping, specifically by testing that scenario rather than trusting a generalization the existing test suite had never actually covered — see [The Complexity Refactor Campaign](The-Complexity-Refactor-Campaign#_interpret_yes_no-a-real-self-introduced-bug-caught-before-it-shipped) for the full account of how this was found.

```text
                 Condition extracted, searched
                              │
                              ▼
                  Was the answer source one of
                  ha / uptime / forecast?
                              │
                  ┌───────────┴───────────┐
                  ▼ no                     ▼ yes
        Present the real result      Does the condition's language
        honestly, note it's          map to a recognizable keyword
        conditional, let the         pattern for THIS source?
        person judge for                     │
        themselves                  ┌────────┴────────┐
                                     ▼ no               ▼ yes
                          Same honest        State an explicit verdict:
                          presentation        "It IS / IS NOT the case
                          as the "no"         that {condition} — so the
                          branch              suggested action may or
                                              may not apply"
```

Wrong is worse than uncertain. That's the entire design principle behind this feature, and it's the reason the interpretable-sources set is a short, explicit allowlist rather than something that tries to generalize.

## Re-applying to decomposed sub-queries

A query can be conditional *without starting with "if"* — *"what is the weather and if the back door is unlocked, let me know"* doesn't match the leading pattern at all, but [Query Decomposition](Query-Decomposition) will still split it into two sub-queries, and the second one (*"if the back door is unlocked, let me know"*) absolutely is conditional. Conditional detection is re-applied to every decomposed sub-query for exactly this reason, self-limiting by construction rather than needing an explicit recursion-depth counter — see [The Recursion Design Bug](The-Recursion-Design-Bug) for the simpler design this replaced and the real bug that motivated the change.

## The remainder after a conditional's consequence

*"if any services are down, let me know, and also what's the weather"* has a genuine second intent hiding after the conditional's consequence. The extracted consequence is checked for a trailing conjunction and, if found, the remainder is split off and searched independently, merging it back into the final response with its own source attribution.

As of v3.52.0 the remainder split applies the same per-occurrence proper-noun-pair guard [decomposition](Query-Decomposition#what-stops-something-from-being-split-when-it-shouldnt-be) has always used. A function-by-function audit found this split was the one conjunction-cutting site without it: *"if the front door is unlocked, tell me the latest on Iran and Israel"* previously cleaved *"Israel"* off as a supposedly separate intent and mangled the consequence down to *"tell me the latest on Iran"*. The judgment is per-occurrence, via the same `_is_proper_noun_pair_at()` helper — a protected pair early in the consequence doesn't block a genuine separate intent behind a *later* conjunction (*"…on Iran and Israel and also what's the weather"* still splits off the weather remainder), the exact lesson [The Proper-Noun-Pair Saga](The-Proper-Noun-Pair-Saga) already taught about global vetoes.

## The condition and remainder are resolved concurrently

When a remainder exists, the condition and remainder are each routed with their own `route_with_source()` call, run concurrently rather than sequentially — a plain `"if X, Y"` query with no trailing conjunction (the more common real-world shape) has an empty remainder and never needed a second call in the first place, so that path is unaffected. See [The Latency Parallelization Investigation](The-Latency-Parallelization-Investigation#the-conditionalremainder-case-was-initially-wrongly-left-alone) for why this wasn't always concurrent, and the real regression a structurally similar earlier fix avoided repeating.

## What it looks like end to end

```text
Query:  "if the back door is unlocked, let me know"
                          │
                          ▼
        condition = "the back door is unlocked"
        consequence = "let me know"
                          │
                          ▼
        Search the condition → routes to `ha`
        Real result: "Back Door: locked"
                          │
                          ▼
        ha is interpretable. Condition mentions
        "unlocked". Result says "locked" — the
        opposite. Verdict: NOT the case.
                          │
                          ▼
"This was a conditional question: 'if the back door
is unlocked, let me know.'

It is NOT the case that the back door is unlocked —
so the suggested action (let me know) may not apply.

Back Door: locked"
```

The real underlying result is always preserved in the response, regardless of the verdict — framing adds context, it never replaces or hides the actual data.
