# Kiwix Scoring

Every candidate result Kiwix returns — whether from a [single search](Kiwix-Catalog-and-Article-Fetching#searching-a-book) or pooled across several [disambiguation](Kiwix-Disambiguation) candidates and, when relevant, several books — gets run through the same scoring function before one is picked as the actual answer. This page documents the exact point values, because "scored against the query" without the real numbers isn't actually verifiable by anyone reading it.

## The full scoring breakdown

| Signal | Points | Condition |
|--------|--------|-----------|
| Exact title match | **+20** | The title equals the full query OR the query's **content words** (case-insensitive, whitespace-trimmed) — `"what is the new deal"` matches the title `"New Deal"`. The content-words comparison was added in v3.54.1 after tracing a real bad result: the full-query comparison alone could effectively never fire for a naturally-phrased question, since nobody's question is verbatim a Wikipedia title — the strongest signal in this whole table was unreachable in practice |
| Stemmed title match | **+15** | The whole query, its content-word sequence, or any individual meaningful query word, stems to the same root as the title (`"galaxies"` → `"galaxy"` matching a `"Galaxy"` title) |
| Title leads with a query term | **+10** | The title's **first token** (punctuation stripped, stemmed) equals a meaningful (4+ letter) query word — deliberately not a raw string prefix, which had a real false-positive class: `"Dealership"` starts with `"deal"` as a string but is not the word, and the prefix version is what handed `"Deal, New Jersey"` a bonus the exact-title `"New Deal"` article couldn't earn (v3.54.1) |
| Per-word title hit | **+5 each** | Each stemmed query word that also appears, stemmed, in the title — title tokens are punctuation-stripped first (v3.54.1: `"Deal,"` with its comma attached previously never counted as a hit for `"deal"`) |
| Per-word excerpt hit | **up to +10 total** | Stemmed overlap between query and excerpt, normalized by excerpt length so a long excerpt doesn't win purely by having more words to coincidentally match |
| List/index article penalty | **−10** | Title starts with `"list of"`, `"lists of"`, `"index of"`, `"outline of"`, or `"category:"` — these are navigation pages, not real content. Applies at full strength to every list article regardless of book |
| Wikipedia bonus | **+8 or +3** | The result's book contains `"wikipedia"` — +8 if the query is [definitional](Kiwix-Disambiguation#when-this-actually-triggers), +3 otherwise. Applies to every Wikipedia result, unconditionally |
| Primary book bonus | **+2** | The result came from the book the LLM originally selected, not a secondary book pulled in only for disambiguation pooling |

A correction worth being explicit about, because an earlier version of this page got the story exactly backwards. Before v3.52.0, the +8/+3 was nested *inside* the list-penalty branch with no `"wikipedia" in book` check at all — and this page rationalized that as an intentional "list-article partial offset," treating the function's own docstring (which has always described a standalone Wikipedia bonus) as the misleading party. A function-by-function audit confirmed by direct execution that the docstring was right and the code was wrong: the documented Wikipedia bonus never actually applied to anything, so on an identical-title definitional query, the only thing separating a Wikipedia result from a Stack Exchange one was the +2 primary-book bonus — whenever the LLM picked a non-Wikipedia book as primary, the Stack Exchange result genuinely won (measured 32 vs 30). Meanwhile list articles in *any* book had their −10 quietly softened to −2/−7. Both halves are now as documented: the Wikipedia bonus is real and unconditional, the list penalty is undiluted, and two regression tests pin each half in a way that fails against the old code (the pre-existing Wikipedia-bonus tests passed the whole time, but only via the +2 primary-book bonus — both happened to use Wikipedia as the primary book).

## Why excerpt scoring is normalized, not raw

A raw per-word excerpt match count would systematically favor longer excerpts — more words means more chances to coincidentally overlap with the query, regardless of actual relevance. Dividing hit count by excerpt length (then scaling to a max of 10 points) keeps a short, precisely on-topic excerpt competitive against a long, loosely-related one.

## Stemming, and why it matters here specifically

`_stem()` is a lightweight, rule-based stemmer (strip trailing "s," "es," "ies" with length guards to avoid mangling short words) — not a full linguistic stemming library, since the actual goal is narrow: catch the specific plural/suffix mismatches that would otherwise cost a correct article real points for no good reason. `"what are galaxies"` needs to score well against a title of `"Galaxy"`; without stemming, the singular/plural mismatch would silently cost both the +15 stemmed-match bonus and the +5 per-word title hit, even though the search obviously found the right thing.

A small, explicit exception list (`this`, `less`, `across`, `always`, `towards`) keeps a handful of common, non-plural English words that happen to end in "s" from being incorrectly suffix-stripped. The real-world scoring impact of getting this wrong is genuinely minimal — this function always compares two complete strings against each other, never an isolated stop word for its own sake — but it's a known inaccuracy worth closing rather than leaving in.

## How this feeds into the rest of Kiwix's behavior

- [Kiwix Disambiguation](Kiwix-Disambiguation) pools results from multiple candidate search terms and lets this exact scoring function pick the real winner across all of them — disambiguation only generates candidates, it never decides between them directly
- [Multi-Book Fusion](Multi-Book-Fusion) compares each book's *best-scored* result against the overall top score to decide whether more than one book's content is genuinely worth merging together, rather than just picking whichever book the LLM happened to select first

## Where scoring still has a real ceiling

Scoring rewards genuine textual overlap and structural signals (title match, list-article detection) — it has no actual world knowledge of its own. A single ambiguous word with multiple, comparably well-represented senses in your index (astronomy "galaxy" vs. a film called *Galaxy Quest*) can still land on the wrong one if both genuinely score similarly well by these exact criteria. That's an honest, accepted limit of keyword-and-structure scoring, not a bug waiting to be fixed with a slightly different weight.
