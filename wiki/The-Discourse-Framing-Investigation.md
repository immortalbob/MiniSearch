# The Discourse-Framing Investigation

*"What's the deal with that whole mercury retrograde thing everyone keeps talking about"* is a genuinely encyclopedic question, phrased the way an actual person would ask it out loud — and for a long stretch of this project's life, it reliably routed past [Kiwix](Kiwix-Disambiguation) entirely, landing on generic news and web results instead. This was the longest-standing known limitation in the whole project, and fixing it properly took **four** separate, sequential discoveries — each one found only after the previous fix had already shipped and seemed, on its own, complete. This page covers the first two, which together fixed the LLM-assisted routing path. The third and fourth — a real gap in the *keyword-matching* path specifically, and two further bugs in how a fixed query actually got merged once decomposition was involved — are covered in [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs#discourse-framing-escalation-never-ran-on-the-keyword-match-path), since that's where real production data surfaced both of them.

## Finding the actual root cause

The instinct going in was that this was probably a routing judgment call gone slightly wrong — maybe Kiwix's description just needed better wording. Reading the actual prompt the LLM sees made the real mechanism obvious instead: `news` and `web`'s own descriptions say things like *"current events"* and *"recent information,"* and *"everyone keeps talking about"* matches that phrasing almost word-for-word. Kiwix's description — *"factual, encyclopedic, or technical questions"* — gives the model no reason to think an evergreen topic can *also* be currently trending in conversation. From the LLM's own perspective, given only those descriptions, picking news/web for this phrasing isn't a mistake at all — it's a reasonable reading of the words it was given.

Two ways to fix this were on the table: reword Kiwix's description to nudge the LLM's judgment, or detect the pattern explicitly and bias the routing decision directly. The first is an indirect, genuinely unverifiable lever — there's no way to know in advance whether new wording would actually shift behavior, or shift it too far in the other direction and start over-triggering Kiwix for queries that really are just current events. The second is explicit, testable in isolation, and doesn't depend on guessing how an 8B model will weigh adjectives in a prompt. The explicit approach won.

## The first fix — routing bias

`_has_discourse_framing()` checks for a small set of literal phrases (`"everyone keeps talking about"`, `"everyone's obsessed with"`, and close variants). If one is present and Kiwix wasn't already part of the LLM's chosen source(s), Kiwix gets added and the result escalates to [fusion](Fusion) — without discarding whatever the LLM already picked, since web/news content was often genuinely relevant too.

Applied across all four real code paths where a routing decision can be made: fresh single-source, fresh multi-source, and — importantly — the *cached* version of each, since a routing cache entry written before this fix existed would otherwise silently bypass it for the remainder of its TTL.

This shipped, was verified against several real queries (mercury, bitcoin, galaxy), and looked complete: Kiwix was now reliably included in the fusion decision for this phrasing.

## The second fix — the first one wasn't actually enough

Continued real-production testing after the routing fix shipped kept surfacing nonsensical Kiwix results even with Kiwix correctly included in the fusion list. *"Bitcoin"* landed on a Big Bang Theory character. *"Black holes"* landed on a Thai horror film. Both with very low relevance scores — not close misses, genuinely unrelated content winning anyway.

The actual cause: routing was fixed, but [Kiwix's search terms](Kiwix-Scoring) were never cleaned up. The literal words *"everyone,"* *"obsessed,"* *"talking,"* *"keep"* all survived stop-word stripping untouched, because they're real English words, not filler in the traditional sense — just pure noise in *this specific* discourse-framing context. The actual string sent to Kiwix for the bitcoin query was `"what whole bitcoin everyone obsessed"` — and Kiwix was scoring matches against "everyone" and "obsessed" exactly as seriously as it was scoring matches against "bitcoin," which explains the nonsensical winners.

`_strip_discourse_framing()` removes the whole matched phrase as a single unit before search terms get built — not by adding "everyone," "obsessed," etc. individually to the general stop-word list, which would risk treating those words as meaningless filler in some *other*, unrelated query where they might carry real meaning. Stripping the exact matched phrase only ever affects queries that actually contain this specific pattern.

```text
   Before fix 1 (routing):
   "what's the deal with that whole bitcoin everyone is obsessed with"
                         │
                         ▼
              Routes to news/web only.
              Kiwix never even searched.

   After fix 1, before fix 2 (search terms):
                         │
                         ▼
              Kiwix correctly included — but
              scored against the RAW query,
              "everyone"/"obsessed"/"whole" all
              still counted as real signal
                         │
                         ▼
              An unrelated sitcom character
              (sharing no real overlap with
              "bitcoin" at all) can still beat
              the real article on noise alone.

   After both fixes:
                         │
                         ▼
              Kiwix included AND searched with
              the cleaned term "whole bitcoin" —
              discourse words stripped from both
              the search AND the scoring
                         │
                         ▼
              Wins clearly: the actual Bitcoin
              article, real margin over any
              unrelated noise match.
```

(Exact search terms and scores shift slightly release to release as scoring and stop-word handling get refined — verified directly against the current code while writing this page: the real search term for this query is `"whole bitcoin"`, and the real Bitcoin article scores meaningfully higher than an unrelated result with no real topical overlap. The qualitative shape — noise-polluted before, clean after — is the durable part; treat any specific number here as illustrative of the mechanism, not a number to expect byte-for-byte in your own logs.)

## A real, deliberate single source of truth

The discourse-framing pattern list now lives in `kiwix.py` — not `router.py` — even though `router.py` needs it too, for the routing bias. `router.py` imports it from there rather than each module keeping its own copy. The direction matters: `router.py` already imports the `kiwix` module elsewhere, so importing the pattern list the same direction is safe; the reverse (`kiwix.py` importing from `router.py`) would create a circular import, since `router.py` is what calls into `kiwix.py` in the first place. One list, two genuinely different uses (bias a routing decision; strip noise from a search term), zero risk of the two copies drifting apart from each other over time — because there's only ever one copy.

That guarantee only holds because it was built this way from the start. A separate, similar-shaped mechanism elsewhere in this same codebase (`router.py` and `fusion.py`'s shared "does this result look empty" phrase list) was built the opposite way — two independently-maintained copies, each presumably written with the same care — and genuinely did drift apart over time, missing different real failure phrases in each direction, found and unified the same way this one already was. Worth remembering as the actual lesson: a single shared definition isn't just tidier, it's the only version of this pattern that can't silently drift, no matter how carefully each copy is written.

## What the fix actually proved, end to end

Verified against real production data after both fixes above shipped: *"bitcoin"* went from a nonsensical, noise-driven match to the correct article scoring clearly higher than any unrelated candidate. *"Black holes"* went from a horror film to a real historical topic genuinely named "Black Hole" (Black Hole of Calcutta) — at the time, this looked like a legitimate, defensible match rather than a nonsensical one, even if not the astrophysics article one might have expected for this specific bare word. *"Galaxy"* improved but didn't fully resolve, since the word itself remains genuinely ambiguous between astronomy and pop-culture senses in the index — a separate, smaller, accepted limitation, not evidence either fix fell short. (The exact point values behind these wins shift slightly as scoring itself gets refined release to release — re-verified directly against the current code while auditing this page, the qualitative outcome held up even though the specific numbers from the original verification no longer reproduce byte-for-byte.)

**That "Black Hole of Calcutta" result did not hold up as the final word on this query.** Later real-world testing (see [The Adversarial Testing Production Bugs](The-Adversarial-Testing-Production-Bugs#discourse-framing-escalation-never-ran-on-the-keyword-match-path)) found the *keyword-matching* routing path — a separate path from the LLM-assisted one this page's two fixes cover — had never received the discourse-framing bias at all, and a real query containing both discourse framing and an ordinary keyword (`"everyone keeps talking about black holes, and rss"`) bypassed Kiwix entirely. Fixing that third gap, plus two further bugs in how the result actually got merged once decomposition was correctly involved, produced a different and more clearly correct winner: the real Black Hole astrophysics disambiguation article, not Calcutta. The "Calcutta" result documented above wasn't wrong given what was being tested at the time — it was a genuinely defensible match for the narrower LLM-path case this page covers — but it's no longer the actual real-world outcome for this query today, and shouldn't be read as the saga's final chapter.

## Postscript: the same lesson, one word list over (v3.53.1)

The day [explanation chains](Explanation-Chains) shipped, their very first real trace caught a live descendant of this saga: *"whats the story with molybdenum"* returned **Love Story (1944 film)** — a movie whose plot happens to involve molybdenum mining — instead of the element. The chain made the diagnosis take about a minute: routing was correct (`intent_llm → kiwix`), so the failure was inside Kiwix's own search, exactly where this page's history says to look.

Two independent leaks, both confirmed by direct execution. First, `"whats"` — as really typed, no apostrophe — slipped a literal `"what"` into the search terms: the contraction-normalizing regex only fires on an actual apostrophe, `"whats"` wasn't a stop word, and `_stem()` runs *after* stop-word filtering, so it stemmed the survivor into `"what"` on the way out. Second, `"story"` was never stripped at all. The colloquial-question handling had been done at the *word* level — `"deal"`, `"keep"`, `"hearing"`, `"up"` were bare `_STOP_WORDS` entries — and `"story"` simply wasn't among them. The search Kiwix actually received was `what story molybdenum`, and the film earned its winning title-match points from `"story"` in *both* the search and the scoring pass.

The fix is the exact lesson this page already documented, re-applied to the one list that hadn't learned it: **strip whole phrases, not individual meaningful words.** `COLLOQUIAL_QUESTION_PHRASES` now removes `"what's the story with"` / `"whats the deal with"` / `"what is up with"` and their variants as units, in both `_build_search_terms()` and `_score_result()`'s scoring set — while `_is_definitional_query()` still deliberately sees the original phrasing, since those leading phrases are precisely what make such queries definitional. And the word-level entries came out of `_STOP_WORDS`, because auditing them found they'd been silently breaking every query where those words *were* the topic, confirmed directly: *"what is the New Deal"* had been searching Kiwix for the literal term `new`, and *"what is a hearing aid"* for `aid`. The apostrophe-less contraction forms (`whats`, `hows`, `thats`, `dont`, …) became stop words in their own right, closing the first leak for every framing rather than just the stripped phrases.

One knock-on worth knowing operationally: a bad result produced by this bug lives in the **result cache** under its exact query until the kiwix TTL expires — the film sat there after the incident, and re-asking the identical phrasing would have served it again. `POST /cache/clear` after deploying the fix, or wait out the TTL.
