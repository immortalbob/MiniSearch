# The Answer Synthesis Voice Soak

Answer synthesis ([reference](Answer-Synthesis)) shipped as a mechanism in v3.55.0, but three of its four answer styles and one of its behavioral rules exist because of what a real voice deployment did with it afterward. This page is the development record of that soak — what the mechanism did on real hardware and real voice, what each finding changed, and why the fixes were the shape they were. The reference page describes how synthesis works today; this page is why it works that way.

## The mechanism, then the hardware

The engine landed with pre-flight gates, a grounding stack (numeric, attribution, echo, honest `NOT_IN_SOURCES` miss), source-led caching, and a `voice`/`brief`/`detailed` style set, all validated by tests against a mocked LLM. Tests can prove the gates fire and the contract holds; they cannot tell you what an 8B model actually writes, or what it sounds like read aloud. That only comes from the deployment the feature was built for: a satellite, real STT, the retrieved material of an actual day.

The first thing the hardware confirmed was latency, not quality. A cold synthesis of a short article measured ~6.4s — almost entirely the model load, since the generation itself came back at ~0.64s once the model was resident. The lesson was `LLM_KEEP_ALIVE`: the model reload was the only thing that would make a satellite feel broken, and keeping the model warm removed it. A benchmark run confirmed the shape under load — a cold-cache generation averaged ~0.6s and tailed to ~2.7s at p90, the tail driven by longer source material and by contention when the single LLM backend serves routing picks and synthesis generations at once. See [Benchmarks](Benchmarks#answer-synthesis-v3550--the-first-feature-that-adds-a-second-llm-call).

## Finding one: "summarize the news" returned a single story

The first substantive voice query — *"summarize the news"* in `answer_style=voice` — spoke one story out of ten. This was the mechanism working exactly as told: voice caps the answer at ~2 sentences, and the model faithfully compressed ten headlines into two, leading with the top item and gesturing vaguely at the rest. Nothing was hallucinated. The length budget was destroying nine-tenths of the point.

The root cause was a mismatch between query *intent* and answer *style*, not a bug. The three existing styles all ask the model to *answer the question* by fusing the material into one reply — correct for a fact question, wrong for a breadth request. A two-sentence budget structurally cannot hold ten items.

The fix was a fourth style, `digest` (v3.55.1), which inverts the framing: list each distinct item as its own line, preserve names/numbers/dates, do not merge or drop. It carries its own larger output cap (so many items survive) and, critically, its own larger *input* budget (so a wide result reaches the model before per-section apportioning trims it — the input budget is the real ceiling on how many items can survive at all). Digest is deliberately forbidden from combining items, because "these are the same story" is a judgment a prompt cannot make reliably: a model told to group related items will sometimes fold genuinely distinct stories together, which is a grounding failure. So digest errs toward preservation.

Reachability was the other half. The conversation agent picks the answer style per query, so `digest` was only useful once the [`mnemolis_intents`](https://github.com/immortalbob/mnemolis_intents) tool exposed `answer_style` as an agent-selectable parameter (v1.4.1), with guidance to choose `digest` for "list / read me everything" requests and keep `voice` for single-answer questions.

## Finding two: digest was the wrong tool for "summarize"

Digest worked — a voice query for the news now enumerated all ten items. But the news feed that day contained four articles about the same event (an Iran funeral), and digest, doing exactly its job, kept them as four separate lines. For a *list* request that is correct. For *"summarize the news"* spoken aloud, it was too literal: the intent there is a combined overview, not an enumeration.

The fix required no new mechanism — it was a style-selection correction. `detailed` is the fusing style; it writes flowing prose that naturally collapses four related articles into one mention. So the real fix was splitting the agent's guidance: `detailed` for "summarize / what's the news" (combine into a narrative), `digest` for "list / read me every headline" (preserve each). That split shipped in `mnemolis_intents` v1.4.2 alongside the change below.

## Finding three: the prose narrated the feed instead of the news

With the agent now choosing `detailed` for "summarize the news," the content and grouping were right — but the prose read *"One article discusses… Another piece reflects… There is also coverage of…"* The model was narrating the *structure of the material* instead of just telling you the news, because the material arrives as discrete sections and nothing told it to dissolve them.

The fix (v3.55.2) was two prompt steers, both deliberately prompt-level rather than gates — a gate on prose phrasing would reject good answers, and prompts steer well enough here that the occasional slip isn't worth that cost:

- A **universal rule** in every synthesis prompt: state the information directly, never describe the source material itself ("one article", "another piece", "the sources say", "there is coverage of"). This applies to every style, including `digest`, whose lines should state the item, not narrate it.
- The **`detailed` and `brief` styles** additionally ask the model to weave related points into one smooth summary rather than addressing each item separately — the "connect, don't enumerate" half that distinguishes a fused summary from `digest`'s list. The steer is kept out of `digest`.

## What the soak established

By the end of the arc the counters told the reassuring part: across everything thrown at the deployment — voice, digest, cold, warm, a forced honest miss — the grounding gates had rejected zero legitimate answers, and the one `NOT_IN_SOURCES` miss (a sports-scores question forced at Kiwix) fired correctly. The gates catch what should be caught and pass real traffic. The honest limits are also clear: the prose steers *steer*, they do not guarantee, so an occasional "one report mentions" will still slip through; and the default flip to on (v3.55.2) was made only after the feature had demonstrably behaved on real hardware, not on schedule.

The through-line: every one of these fixes came from a spoken query on real hardware, and none of them could have come from a test against a mocked LLM. Tests proved the contract; the soak found the behavior.
