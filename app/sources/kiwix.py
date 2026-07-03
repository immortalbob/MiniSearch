import re
import logging
import requests
from defusedxml import ElementTree as ET
from bs4 import BeautifulSoup
from app.config import settings

_LOGGER = logging.getLogger(__name__)

# Persistent HTTP session — connection reuse across every Kiwix call,
# the same shape as app/llm.py's fix and searxng.py's _session (see
# searxng.py's comment for the shared thread-safety rationale: never
# mutated after creation, only ever `.get()`). Kiwix is the single
# heaviest HTTP consumer in the project: one query can issue a catalog
# page fetch, per-book /search requests during multi-book fusion, AND
# an article fetch per selected result — each of which previously paid
# its own fresh TCP setup/teardown with zero reuse.
_session = requests.Session()

# Lazy imports to avoid circular imports
def _get_routing_fns():
    from app.router import _get_routing, _set_routing
    return _get_routing, _set_routing


def _get_singleflight_fn():
    """Lazy-imported accessor for router.py's _singleflight() context
    manager, mirroring _get_routing_fns()'s existing pattern.

    Deliberately the SAME registry router.py's own _llm_detect()/
    _llm_pick_fusion_sources() use (_inflight_locks lives in router.py,
    not duplicated here) — a query that happens to share a cache key
    shape across modules should still genuinely coordinate against one
    lock, not two independent ones that can't see each other. In
    practice the cache key PREFIXES this module uses ("books:",
    "disambig_candidates:") never collide with router.py's own
    ("source:", "fusion_sources:"), so today this is more about having
    one obviously-correct shared registry than about a key collision
    that's actually possible right now — but a single source of truth
    is the right shape regardless,
    and costs nothing extra to set up.
    """
    from app.router import _singleflight
    return _singleflight

# ---------------------------------------------------------------------------
# Dynamic book discovery
# ---------------------------------------------------------------------------

_book_cache: list[dict] = []


def _fetch_catalog_page(url: str) -> list[dict]:
    """Fetch one page of the Kiwix OPDS catalog and return book entries."""
    try:
        resp = _session.get(url, timeout=5)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        ns = {
            "atom": "http://www.w3.org/2005/Atom",
            "dc": "http://purl.org/dc/terms/",
        }
        books = []
        for entry in root.findall("atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)

            # Full versioned name lives in the text/html link href
            # e.g. /content/wikipedia_en_all_maxi_2026-02
            full_name = ""
            for link in entry.findall("atom:link", ns):
                if link.get("type") == "text/html":
                    href = link.get("href", "")
                    full_name = href.rstrip("/").split("/content/")[-1]
                    break

            if full_name and title_el is not None:
                books.append({
                    "name": full_name,
                    "title": title_el.text.strip() if title_el.text else "",
                    "summary": summary_el.text.strip() if summary_el is not None and summary_el.text else "",
                })
        return books
    except Exception as e:
        _LOGGER.warning("Kiwix catalog fetch failed for %s: %s", url, e)
        return []


def get_books() -> list[dict]:
    """Return cached book list, fetching from Kiwix catalog if needed."""
    global _book_cache
    if _book_cache:
        return _book_cache

    _LOGGER.info("Fetching Kiwix catalog...")
    all_books = []
    start = 0
    page_size = 10  # Kiwix default page size

    while True:
        url = f"{settings.kiwix_url}/catalog/v2/entries?lang=eng&start={start}&count={page_size}"
        page = _fetch_catalog_page(url)
        if not page:
            break
        all_books.extend(page)
        if len(page) < page_size:
            break
        start += page_size

    if all_books:
        _book_cache = all_books
        _LOGGER.info("Loaded %d books from Kiwix catalog", len(all_books))
    else:
        _LOGGER.warning("No books found in Kiwix catalog")

    return _book_cache


def refresh_catalog() -> list[dict]:
    """Force refresh the book cache."""
    global _book_cache
    _book_cache = []
    return get_books()


# ---------------------------------------------------------------------------
# LLM-assisted book selection
# ---------------------------------------------------------------------------

def _fallback_book_choice(books: list[dict], cache_key: str, set_routing) -> list[str]:
    """
    Pick a single fallback book — Wikipedia if available, otherwise the
    first book in the list — and cache the decision.

    Extracted from _pick_books_with_llm(), where this exact logic was
    duplicated byte-for-byte in two places: once for "no LLM configured
    at all," once for "the LLM responded but returned nothing usable."
    Found via the same side-by-side comparison discipline applied to
    app/router.py's route_with_source() (3.20.0/3.21.0) and
    app/sources/home_assistant.py's search() (3.22.0) this release
    cycle — unlike those two, which surfaced real behavioral bugs once
    compared carefully, this one is a genuine, exact, mechanical
    duplicate with no hidden divergence to find.
    """
    book_names_list = [b["name"] for b in books]
    for name in book_names_list:
        if "wikipedia" in name:
            result = [name]
            set_routing(cache_key, ",".join(result))
            return result
    result = [book_names_list[0]] if book_names_list else []
    if result:
        set_routing(cache_key, ",".join(result))
    return result


def _pick_books_with_llm(query: str, books: list[dict], max_books: int | None = None) -> list[str]:
    """Ask Ollama to pick the best books for the query. Returns ranked list of book names.
    Checks routing cache first to avoid redundant Ollama calls.
    Defaults to settings.kiwix_max_books when max_books is not specified.

    Singleflight: see router.py's _llm_detect() docstring and the
    module-level comment above router.py's _RefCountedLock/
    _inflight_locks for the full mechanism — this is the third of four
    call sites sharing the identical check-call-write gap (the others
    are router.py's _llm_detect()/_llm_pick_fusion_sources() and this
    module's own _get_disambiguation_candidates()). Both of
    _fallback_book_choice()'s call sites below (no LLM configured; LLM
    responded but nothing usable) sit inside the lock too, not just the
    real LLM-call path — a second caller racing the "not configured"
    branch would otherwise still skip the cache re-check and redundantly
    recompute the same cheap, but not free, fallback.
    """
    if max_books is None:
        max_books = settings.kiwix_max_books
    if not books:
        return []

    get_routing, set_routing = _get_routing_fns()
    singleflight = _get_singleflight_fn()
    cache_key = f"books:{query}"
    book_names = {b["name"] for b in books}

    def _interpret_cached_books(cached: str) -> list[str] | None:
        # Stored as comma-separated book names
        cached_books = [b.strip() for b in cached.split(",") if b.strip()]
        valid = [b for b in cached_books if b in book_names]
        if valid:
            _LOGGER.info("Routing cache hit for book selection: '%s' -> %s", query[:50], valid)
            return valid
        return None

    # Check routing cache first — cheap read, stays outside any lock so
    # a warm hit never pays any lock overhead at all.
    cached = get_routing(cache_key)
    if cached:
        result = _interpret_cached_books(cached)
        if result is not None:
            return result

    from app.llm import complete, is_configured

    with singleflight(cache_key):
        # Re-check now that we hold the lock — a concurrent caller may
        # have already resolved and cached this exact query (via either
        # the real LLM path or a fallback) while we were waiting.
        cached = get_routing(cache_key)
        if cached:
            result = _interpret_cached_books(cached)
            if result is not None:
                return result

        if not is_configured():
            return _fallback_book_choice(books, cache_key, set_routing)

        is_definitional = _is_definitional_query(query)
        intent_hint = (
            "This is a definitional or overview query — prefer encyclopedic sources like Wikipedia over Q&A threads."
            if is_definitional else
            "This is a specific or technical query — Stack Exchange and technical references may be appropriate."
        )

        book_list = "\n".join(
            f"- {b['name']}: {b['title']} — {b['summary'][:100]}"
            for b in books
        )

        prompt = (
            f"You are a search router. Given a user query and a list of available Kiwix offline "
            f"knowledge bases, return up to {max_books} book names that best match the query, "
            f"ranked by relevance, as a comma-separated list. "
            f"Return ONLY the exact book names separated by commas. No explanation, no punctuation other than commas.\n\n"
            f"Query: {query}\n\n"
            f"Intent: {intent_hint}\n\n"
            f"Available books:\n{book_list}\n\n"
            f"Best book names (comma-separated, most relevant first):"
        )

        raw = complete(prompt, max_tokens=150) or ""

        chosen = []

        for candidate in raw.split(","):
            candidate = candidate.strip().strip(".")
            if not candidate:
                continue
            if candidate in book_names:
                chosen.append(candidate)
            else:
                # Found via a deliberate "bulletproofing" pass: book_names
                # is a set, and Python's set iteration order is not
                # guaranteed to be stable across different process runs
                # (depends on hash randomization, which this project never
                # pins via PYTHONHASHSEED). When an LLM's response is
                # ambiguous enough to fuzzy-match more than one real book
                # (e.g. a truncated "wikipedia_en_all" matching both
                # "wikipedia_en_all_maxi" and "wikipedia_en_all_nopic"),
                # the SAME query could resolve to a DIFFERENT real book
                # purely due to container restart timing — a real,
                # reproducibility-breaking gap, even though the practical
                # harm is mild (both candidates are genuinely
                # Wikipedia-related, not a wrong-topic book). Sorting
                # before iterating makes the choice deterministic and
                # reproducible across restarts, even for a genuinely
                # ambiguous candidate.
                for name in sorted(book_names):
                    if candidate in name or name in candidate:
                        if name not in chosen:
                            chosen.append(name)
                        break

        chosen = chosen[:max_books]
        if chosen:
            _LOGGER.info("LLM selected books: %s", chosen)
            set_routing(cache_key, ",".join(chosen))
            return chosen

        if raw:
            _LOGGER.warning("LLM returned no valid books from '%s', falling back", raw)

        # Fallback — Wikipedia first
        return _fallback_book_choice(books, cache_key, set_routing)


# ---------------------------------------------------------------------------
# Search and fetch
# ---------------------------------------------------------------------------

def _search_book(query: str, book: str, limit: int | None = None) -> list:
    if limit is None:
        limit = settings.kiwix_search_limit
    try:
        response = _session.get(
            f"{settings.kiwix_url}/search",
            params={"pattern": query, "books.name": book, "limit": limit},
            timeout=5,
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        results_div = soup.find("div", class_="results")
        if not results_div:
            _LOGGER.debug("No results div in Kiwix response for book=%s query=%s", book, query[:50])
            return []
        results = []
        for item in results_div.find_all("li"):
            a_tag = item.find("a")
            cite_tag = item.find("cite")
            if not a_tag:
                continue
            url = f"{settings.kiwix_url}{a_tag.get('href', '')}"
            if "/questions/tagged/" in url:
                continue
            results.append({
                "title": a_tag.get_text(strip=True),
                "excerpt": cite_tag.get_text(strip=True) if cite_tag else "",
                "url": url,
                "book": book,
            })
        _LOGGER.debug("Kiwix search returned %d results from %s", len(results), book)
        return results
    except Exception as e:
        _LOGGER.warning("Kiwix search failed for book=%s: %s", book, e)
        return []


def _fetch_article(url: str, max_chars: int | None = None) -> str:
    # Found via a deliberate config-completeness audit: every real call
    # site relied on the same hardcoded default (3000), with no override
    # anywhere — now configurable via KIWIX_ARTICLE_MAX_CHARS. The
    # default can't simply be `= settings.kiwix_article_max_chars` in the
    # function signature, since Python evaluates parameter defaults once
    # at import time, before any test or runtime config change to
    # `settings` would ever be picked up — reading it inside the
    # function body instead means every call always sees the current,
    # real setting.
    if max_chars is None:
        max_chars = settings.kiwix_article_max_chars
    try:
        response = _session.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        # Found via a deliberate "bulletproofing" pass: ".toc" and
        # "#toc" were CSS-selector syntax passed to soup([...]), which
        # only matches literal HTML tag names (e.g. looking for a tag
        # literally named "<.toc>", which doesn't exist) — confirmed
        # directly that table-of-contents boxes were never actually
        # being stripped from any fetched article, despite the code's
        # clear intent. "table" in the same original list is a real,
        # valid bare tag name and was already working correctly;
        # only the two CSS-selector-style entries needed soup.select()
        # instead.
        for tag in soup(["script", "style", "nav", "header", "footer", "table"]):
            tag.decompose()
        for tag in soup.select(".toc, #toc"):
            tag.decompose()
        content = (
            soup.find("div", class_="mw-parser-output")
            or soup.find("div", id="mw-content-text")
            or soup.find("article")
            or soup.find("div", class_="post-content")
            or soup.find("div", id="question")
            or soup.find("body")
        )
        if not content:
            _LOGGER.warning("Could not find article content at %s", url)
            return ""
        text = content.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text[:max_chars].strip()
    except Exception as e:
        _LOGGER.warning("Failed to fetch article from %s: %s", url, e)
        return ""


# Stop words to strip before scoring — same set as freshrss for consistency
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "about", "what", "which", "who", "whom",
    "this", "that", "these", "those", "i", "me", "my", "we", "our",
    "you", "your", "he", "she", "it", "they", "them", "their",
    "tell", "explain", "describe", "define", "give", "show", "find",
    "get", "how", "why", "when", "where",
    # Colloquial filler — only words that are SAFE as unconditional
    # filler stay here. "deal", "keep", "hear/heard/hearing", and "up"
    # used to be in this set (added for "what's the deal with X" / "X
    # thing I keep hearing about"), and that word-level approach was a
    # real bug factory, confirmed by direct execution: "what is the New
    # Deal" reduced to the literal search "new", and "what is a hearing
    # aid" reduced to "aid" — each losing the one word that carried the
    # topic. Those phrasings are now handled by whole-phrase stripping
    # (COLLOQUIAL_QUESTION_PHRASES below), the exact surgical approach
    # DISCOURSE_FRAMING_PATTERNS' own comment already argued for; only
    # the genuinely-never-a-topic fillers remain as bare words.
    "thing", "things", "stuff", "going",
    # Apostrophe-LESS contractions — found via real usage (the "Love
    # Story (1944 film)" incident, see The Discourse-Framing
    # Investigation's postscript): the contraction-normalizing regex in
    # _build_search_terms() only fires on an actual apostrophe, so
    # "whats" (as really typed) survived the "what" stop word, then
    # _stem() — which runs AFTER stop-word filtering — turned it into a
    # literal "what" search term. Same class for the rest of these:
    # each is the apostrophe-less spelling of a contraction whose
    # apostrophe form already normalizes to an existing stop word.
    # Deliberately excludes real-topic homographs ("id" — Freud, id
    # Software; "im" — instant messaging).
    "whats", "hows", "whos", "wheres", "whens", "whys", "thats", "its",
    "theres", "heres", "lets", "youre", "theyre",
    "dont", "doesnt", "didnt", "isnt", "arent", "wasnt", "werent",
    "cant", "wont", "couldnt", "shouldnt", "wouldnt",
}

# Phrases signaling that a query frames its (possibly encyclopedic) topic
# as current public discourse rather than a pure knowledge lookup — "what's
# the deal with X everyone keeps talking about". This is the CANONICAL
# definition; app/router.py imports this list rather than keeping its own
# copy, since router.py uses it to decide WHICH sources to route to
# (adding kiwix to a fusion decision that would otherwise exclude it), while
# this module uses it to decide what to STRIP before building search terms.
#
# Found via real usage, in two separate passes: first, these phrasings
# reproducibly routed past kiwix to news/web entirely (fixed in router.py
# by detecting the pattern and biasing routing toward fusion). Then, even
# once kiwix was correctly included, "everyone", "obsessed", "talking",
# "keep" all survived _STOP_WORDS untouched and were sent to Kiwix as
# literal search terms — "what whole bitcoin everyone obsessed" matched
# scattered, irrelevant content ("Howard Wolowitz") far more than the
# actual topic word ("bitcoin") could win against that noise. Stripping
# the whole matched PHRASE (not just adding individual words to
# _STOP_WORDS) is more surgical — it only affects queries that actually
# contain this exact discourse-framing pattern, rather than risking
# "everyone" or "keep" being treated as filler in some other, unrelated
# query where they might carry real meaning.
DISCOURSE_FRAMING_PATTERNS = [
    "everyone keeps talking about", "everyone's talking about", "everyones talking about",
    "everyone is talking about", "everyone keeps talking",
    "everyone's obsessed with", "everyones obsessed with", "everyone is obsessed with",
]


def _strip_discourse_framing(query: str) -> str:
    """Remove any matched discourse-framing phrase from the query before
    building search terms, so words like "everyone" and "obsessed" never
    reach Kiwix as literal search noise. See DISCOURSE_FRAMING_PATTERNS."""
    result = query
    result_lower = result.lower()
    for phrase in DISCOURSE_FRAMING_PATTERNS:
        idx = result_lower.find(phrase)
        if idx != -1:
            result = result[:idx] + result[idx + len(phrase):]
            result_lower = result.lower()
    return result


# Colloquial question framings stripped as WHOLE PHRASES before search
# terms are built and before result scoring — "what's the story with X"
# is a question about X, and "story" must never become a literal search
# or scoring term. Found via real usage on day one of explanation
# chains: "whats the story with molybdenum" returned "Love Story (1944
# film)" — a movie whose plot happens to mention molybdenum mining —
# because "story" leaked into the search terms AND earned the film
# title-match points during scoring, out-competing the actual
# Molybdenum article.
#
# Phrase-level, not word-level, deliberately: this is the identical
# lesson DISCOURSE_FRAMING_PATTERNS' own comment documents, re-learned
# the hard way. The previous approach put "deal", "keep", "hearing",
# and "up" into _STOP_WORDS as bare words, which silently broke every
# query where those words WERE the topic — confirmed directly: "what is
# the New Deal" searched Kiwix for the literal term "new", and "what is
# a hearing aid" searched for "aid". A phrase only fires when the whole
# colloquial framing is genuinely present.
#
# Three spelling variants per framing (apostrophe, apostrophe-less,
# spelled-out "what is") because all three occur in real typed queries
# — the apostrophe-less form is what actually triggered the film
# incident. Overlaps with router.py's _COLLOQUIAL_PHRASES and this
# module's own _DEFINITIONAL_PATTERNS are intentional but the lists
# serve different jobs (decomposition meaningfulness, definitional
# detection, term stripping) and are deliberately not unified — see
# each list's own comment.
COLLOQUIAL_QUESTION_PHRASES = (
    "what's the story with", "whats the story with", "what is the story with",
    "what's the story behind", "whats the story behind", "what is the story behind",
    "what's the deal with", "whats the deal with", "what is the deal with",
    "what's up with", "whats up with", "what is up with",
    "what's this about", "whats this about", "what is this about",
    "what's going on with", "whats going on with", "what is going on with",
    "i keep hearing about", "keeps hearing about", "keep hearing about",
)


def _strip_colloquial_phrases(query: str) -> str:
    """Remove any matched colloquial question framing from the query —
    same mechanism as _strip_discourse_framing(), same reason. Applied
    in BOTH _build_search_terms() (what gets sent to Kiwix) and
    _score_result()'s scoring query (what results get graded against);
    the film incident above needed the phrase gone from both places,
    since "story" polluted the search AND the title scoring."""
    result = query
    result_lower = result.lower()
    for phrase in COLLOQUIAL_QUESTION_PHRASES:
        idx = result_lower.find(phrase)
        if idx != -1:
            result = result[:idx] + result[idx + len(phrase):]
            result_lower = result.lower()
    return result


def _stem(word: str) -> str:
    """Reduce a word to its approximate stem by stripping common suffixes.
    Handles plural and verb forms — safe suffixes only, no semantic changes.
    Examples: marsupials→marsupial, foxes→fox, batteries→battery,
              computing→comput, computed→comput

    Real-world impact of this exception list is genuinely minimal — this
    function is always used to compare two complete strings against each
    other (never an isolated stop word for its own sake), and a
    consistent mis-stem applied identically to both sides of a
    comparison wouldn't typically flip a real match into a false one.
    Found via a deliberate, precise re-read of this exact function: the
    plain "s"-suffix rule below has no way to tell a genuine plural
    ("foxes" → fox) apart from a common, non-plural word that happens to
    end in "s" — "this"→"thi", "less"→"les", "across"→"acros",
    "always"→"alway", "towards"→"toward" were all confirmed via direct
    testing to be real, if narrow, inaccuracies.
    """
    if word in {"this", "less", "across", "always", "towards"}:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ing") and len(word) > 5:
        return word[:-3]
    if word.endswith("ed") and len(word) > 4:
        return word[:-2]
    if word.endswith("es") and len(word) > 3:
        return word[:-2]
    if word.endswith("s") and len(word) > 3:
        return word[:-1]
    return word


def _is_definitional_query(query: str) -> bool:
    """Return True if the query is asking for a definition or overview.
    These queries benefit from encyclopedic sources like Wikipedia over Q&A threads.
    """
    q = query.lower().strip()
    # `p in q` alone — the previous `q.startswith(p) or p in q` was
    # redundant, since any prefix match is by definition also a
    # substring match; startswith could never change the outcome.
    return any(p in q for p in _DEFINITIONAL_PATTERNS)


# Hoisted from _is_definitional_query()'s body — the check runs once per
# scored result during search (see _score_result()), and rebuilding a
# 20-element list per call is pure per-call allocation for a set of
# phrases that never changes at runtime. A tuple: immutable module state.
_DEFINITIONAL_PATTERNS = (
    "what is", "what are", "what was", "what were",
    "tell me about", "explain", "describe", "define",
    "how does", "how do", "how did",
    "who is", "who was", "who were",
    "history of", "overview of", "introduction to",
    # Colloquial equivalents — "what's the deal with X", "what's up with
    # X", "X thing I keep hearing about" are all definitional asks
    # phrased casually rather than formally
    "what's the deal with", "whats the deal with",
    "what's up with", "whats up with",
    "what's this about", "whats this about",
    "what's the story with", "whats the story with",
)


def _score_result(result: dict, query: str, primary_book: str) -> int:
    """Score a search result by relevance to the query.

    Scoring breakdown:
    - Exact title match (case-insensitive, against the full query OR its
      content words — "what is the new deal" matches the title
      "New Deal"): +20
    - Stemmed title match (plural/suffix variations, full/content/word
      forms): +15
    - Title LEADS WITH a meaningful query term (first token,
      punctuation-stripped and stemmed — never a raw string prefix): +10
    - Each query word in title (stop words removed, tokens
      punctuation-stripped): +5 each
    - Query word overlap in excerpt (stop words removed): up to +10 total,
      normalized by excerpt length so a short, precisely on-topic excerpt
      competes fairly against a long, loosely-related one — NOT a flat
      "+1 per matching word." Found via a deliberate, precise re-read of
      this exact function: the docstring previously claimed "+1 each,"
      which never matched the real formula (hit_count / excerpt_length *
      10, then truncated to an int) — a real documentation error, not a
      runtime bug, since the inline comment next to the actual code
      ("normalize by excerpt length to avoid bias") was already
      accurate, and the wiki's own Kiwix Scoring page was independently
      correct too, apparently written from the real code rather than
      this stale docstring summary.
    - Wikipedia bonus: +8 for definitional queries, +3 for all others
    - Primary book bonus: +2
    - List/index article penalty: -10 for titles starting with "List of", "Lists of", "Index of", etc.
    """
    query_lower = query.lower().strip()
    title_lower = result["title"].lower().strip()
    excerpt_lower = result["excerpt"].lower()
    book = result.get("book", "")

    # Strip discourse-framing phrases ("everyone keeps talking about",
    # "everyone's obsessed with") AND colloquial question framings
    # ("what's the story with", "whats the deal with") from the words
    # actually used for keyword-overlap scoring below — NOT from
    # query_lower itself, which stays the full, original phrasing for
    # the exact/whole-string match checks just below and for
    # _is_definitional_query() further down (genuinely needs the real
    # leading phrase structure, e.g. "what's the deal with", which
    # these strips would remove and search_terms's stop-word stripping
    # would destroy). The colloquial strip was added here alongside its
    # _build_search_terms() twin after the "Love Story (1944 film)"
    # incident — "whats the story with molybdenum" needed "story" gone
    # from BOTH the search terms and this scoring set, since the film
    # earned its winning title-match points right here.
    #
    # Found via tracing a real, live bad result: The Discourse-Framing
    # Investigation's own fix only ever called _strip_discourse_framing()
    # inside _build_search_terms() — cleaning what gets SENT to Kiwix's
    # search API, but never what gets used HERE, in scoring, to rank
    # whatever comes back. "everyone", "keeps", "talking" all survived
    # as real, counted words in query_words below, scored identically to
    # genuine topic words — confirmed directly even for the original
    # bitcoin case this page's own wiki documents as fully fixed: the
    # literal words "everyone"/"obsessed" are STILL real members of
    # query_words there too. That case's real winner happened not to
    # change only because the real Bitcoin article's title overlap with
    # "bitcoin" was dominant enough to win anyway — a real, live "black
    # holes" query without that same lopsided signal-to-noise ratio
    # surfaced the actual gap: an unrelated Stack Exchange thread and an
    # unrelated podcast Wikipedia article both outscored the real,
    # correct Black Hole article, in part because "everyone"/"keeps"/
    # "talking" never got excluded from real scoring at all.
    scoring_query_lower = _strip_colloquial_phrases(_strip_discourse_framing(query_lower))

    # Strip stop words from query for word-level scoring
    query_words = set(scoring_query_lower.split()) - _STOP_WORDS

    # Tokenize titles and excerpts with surrounding punctuation stripped
    # — found by tracing the real "what is the new deal" -> "Deal, New
    # Jersey" bad result: "Deal, New Jersey".split() yields the token
    # "deal," (comma attached), whose stem is not "deal", so the borough
    # was only ever credited with ONE title hit for a two-word overlap;
    # "(New Zealand" and "show)" in game-show titles had the same
    # problem. Word-level hits below must count real words, not
    # punctuation-glued fragments — this fix raises some wrong-ish
    # candidates' scores too (that borough now honestly has both words),
    # which is exactly why the content-words exact match below exists to
    # dominate them.
    _PUNCT = ".,;:!?()[]{}\"'"
    title_tokens = [t for t in (w.strip(_PUNCT) for w in title_lower.split()) if t]
    title_words = set(title_tokens) - _STOP_WORDS
    excerpt_words = {t for t in (w.strip(_PUNCT) for w in excerpt_lower.split()) if t} - _STOP_WORDS

    # The query's content words, in order — what the person is actually
    # asking about once question framing is gone. "what is the new deal"
    # -> "new deal".
    content_query = " ".join(w for w in scoring_query_lower.split() if w not in _STOP_WORDS)

    score = 0

    # Exact title match — strongest signal. Compared against BOTH the
    # full query and its content words: the full-query comparison alone
    # (the previous behavior) could effectively never fire for a
    # naturally-phrased question, since "what is the new deal" is never
    # a Wikipedia title — confirmed by tracing that exact real query,
    # where the "New Deal" article (a title that IS the query's content
    # words) earned nothing here and lost to "Deal, New Jersey", which
    # collected the starts-with bonus below for merely leading with
    # "deal". A title equal to what the person is asking about is the
    # single strongest relevance signal this function has, and it must
    # actually be reachable.
    if query_lower == title_lower or (content_query and content_query == title_lower):
        score += 20

    # Stemmed match — catches plural/suffix variations
    # Check full query AND individual meaningful query words against title stem
    # e.g. "what are galaxies" → "galaxies" → stems to "galaxy" → matches "Galaxy" title
    elif _stem(query_lower) == _stem(title_lower):
        score += 15
    elif content_query and " ".join(_stem(w) for w in content_query.split()) == " ".join(
        _stem(t) for t in title_tokens
    ):
        score += 15
    elif any(_stem(w) == _stem(title_lower) for w in query_words if len(w) > 3):
        score += 15

    # Title LEADS WITH a meaningful query term — first token, stemmed,
    # not a raw string prefix. The previous title_lower.startswith(w)
    # check had a real false-positive class: "dealership" starts with
    # "deal" as a string but is not the word "deal", and the check is
    # what handed "Deal, New Jersey" a +10 the exact-title "New Deal"
    # article couldn't earn (its leading word "new" fails the len>3
    # filter). Token-exact keeps the bonus's actual intent — the title
    # opens with the person's topic word — without rewarding prefixes.
    if title_tokens and any(
        _stem(title_tokens[0]) == _stem(w) for w in query_words if len(w) > 3
    ):
        score += 10

    # Word-level title hits with stemming
    stemmed_query_words = {_stem(w) for w in query_words}
    stemmed_title_words = {_stem(w) for w in title_words}
    title_hits = len(stemmed_query_words & stemmed_title_words)
    score += title_hits * 5

    # Word-level excerpt hits — normalize by excerpt length to avoid bias
    stemmed_excerpt_words = {_stem(w) for w in excerpt_words}
    excerpt_hits = len(stemmed_query_words & stemmed_excerpt_words)
    excerpt_len = max(len(excerpt_words), 1)
    score += int((excerpt_hits / excerpt_len) * 10)

    # Penalize list and index articles — these are navigation pages not content
    list_prefixes = ("list of", "lists of", "index of", "outline of", "category:")
    if any(title_lower.startswith(p) for p in list_prefixes):
        score -= 10

    # Wikipedia bonus — the docstring above has always documented this
    # (+8 definitional / +3 otherwise), but the bonus was previously
    # nested INSIDE the list-article penalty block just above, with no
    # `"wikipedia" in book` condition at all. Two real consequences,
    # both confirmed directly: (1) Wikipedia results never received any
    # bonus — the only thing separating an identical-title Wikipedia
    # result from a Stack Exchange one was the +2 primary-book bonus,
    # so whenever the LLM picked a non-Wikipedia book as primary, the
    # Stack Exchange result WON on an identical definitional query;
    # (2) list/index articles in ANY book had their -10 penalty
    # silently softened to -2 (definitional) or -7 (other), which is
    # what the "nets -2 or -7 after its own partial offset" comment in
    # search() below was unknowingly describing. The existing
    # test_wikipedia_bonus_* tests passed only via the +2 primary-book
    # bonus, since both used Wikipedia as the primary book — see the
    # new primary-book-flipped regression test pinning the real bonus.
    if "wikipedia" in book:
        score += 8 if _is_definitional_query(query) else 3

    # Primary book bonus
    if book == primary_book:
        score += 2

    return score


def _get_disambiguation_candidates(query: str, search_terms: str) -> list[str]:
    """
    Ask the LLM for 2-3 candidate disambiguation terms for an ambiguous word.
    Returns just the candidate word lists — actual searching and scoring
    happens in the caller, since blind LLM term selection has proven
    unreliable (it can't see Kiwix's actual index, so it guesses wrong
    in unpredictable ways — broad terms get drowned by unrelated category
    pages, narrow terms can collide with completely different topics).

    Result is cached in the routing cache so repeated queries skip the LLM call.

    Singleflight: see router.py's _llm_detect() docstring and the
    module-level comment above router.py's _RefCountedLock/
    _inflight_locks for the full mechanism — this is the fourth and
    last of the four call sites sharing the identical check-call-write
    gap. Plausibly relevant to kiwix_disambiguation's own large
    cold-tail outliers noted in recent benchmark runs (see
    wiki/The-Benchmark-Investigation-Log.md) — worth re-checking once
    this fix is benchmarked, not assumed in advance.
    """
    from app.llm import complete

    get_routing, set_routing = _get_routing_fns()
    singleflight = _get_singleflight_fn()
    cache_key = f"disambig_candidates:{search_terms}"

    def _interpret_cached_candidates(cached: str) -> list[str] | None:
        candidates = [c.strip() for c in cached.split("|") if c.strip()]
        if candidates:
            _LOGGER.info("Routing cache hit for disambiguation candidates: '%s' -> %s", search_terms, candidates)
            return candidates
        return None

    cached = get_routing(cache_key)
    if cached:
        result = _interpret_cached_candidates(cached)
        if result is not None:
            return result

    with singleflight(cache_key):
        # Re-check now that we hold the lock — see _llm_detect()'s
        # identical re-check for why this is necessary, not redundant.
        cached = get_routing(cache_key)
        if cached:
            result = _interpret_cached_candidates(cached)
            if result is not None:
                return result

        prompt = (
            f"The word '{search_terms}' could refer to multiple unrelated things. "
            f"Given the full question \"{query}\", list 3 different candidate search "
            f"phrases that might find the article the user actually means — each "
            f"phrase should be the original word plus ONE additional clarifying word, "
            f"and the 3 candidates should try genuinely different angles (e.g. a broad "
            f"field name, a more specific synonym, and the word alone with no qualifier). "
            f"Respond with ONLY the 3 phrases separated by '|', no explanation. "
            f"Example for 'galaxy': 'galaxy astronomy|galaxy spiral|galaxy'\n\n"
            f"Candidates for '{search_terms}':"
        )

        raw = complete(prompt, max_tokens=40) or ""
        candidates = [c.strip().strip(".").strip('"') for c in raw.split("|") if c.strip()]

        # Always include the bare original term as a guaranteed fallback candidate
        if search_terms not in candidates:
            candidates.append(search_terms)

        # Sanity filter — drop any candidate that's empty, too long, or doesn't
        # contain the original word at all
        #
        # Found while verifying this filter's behavior for single-character
        # search terms (e.g. "c," now genuinely reachable through
        # _should_disambiguate after fixing _build_search_terms() to stop
        # dropping single alphanumeric characters): a bare substring check
        # is far too loose for a one-character original word, since almost
        # any English phrase coincidentally contains the letter "c"
        # somewhere — the filter would provide meaningfully less protection
        # for short search terms than it does for longer ones. Word-boundary
        # matching (the same discipline already applied to
        # home_assistant.py's keyword matching) makes the check genuinely
        # meaningful regardless of how short the original word is.
        original_word = search_terms.lower()
        original_pattern = r"\b" + re.escape(original_word) + r"\b"
        valid_candidates = []
        for c in candidates:
            if not c or len(c.split()) > 3:
                continue
            if not re.search(original_pattern, c.lower()):
                continue
            valid_candidates.append(c)

        if not valid_candidates:
            valid_candidates = [search_terms]

        # Cap at 3 candidates to bound the number of extra Kiwix searches
        valid_candidates = valid_candidates[:3]

        # Found via a deliberate complexity-investigation pass — the same
        # bug already found and fixed in _llm_pick_fusion_sources() and
        # _llm_detect(): caching a pure-fallback result under the same key
        # a genuine success would use means a single transient LLM hiccup
        # permanently locks the query into the bare, unhelpful fallback
        # (just the original ambiguous word, no real disambiguation at all)
        # for the full routing cache TTL. Confirmed directly via the same
        # test pattern used for the other two fixes.
        #
        # A real, deliberate distinction from those two fixes: this
        # function can reach the same bare-fallback OUTCOME for two
        # genuinely different REASONS — `raw` itself being empty/falsy (a
        # real call failure, where a retry is likely to succeed) versus the
        # LLM genuinely responding with something that simply didn't
        # survive the sanity filter (e.g. none of its 3 phrases contained
        # the original word at all). The second case isn't really a
        # transient hiccup — the same prompt would likely produce a
        # similarly unusable answer again, so caching that outcome is the
        # more sensible default rather than re-querying the LLM on every
        # repeat of a query it has already genuinely struggled with. Only
        # skip caching when `raw` was empty/falsy specifically.
        if raw:
            set_routing(cache_key, "|".join(valid_candidates))
        _LOGGER.info("Disambiguation candidates for '%s': %s", search_terms, valid_candidates)
        return valid_candidates


def _should_disambiguate(query: str, search_terms: str, selected_books: list[str]) -> bool:
    """
    Decide whether a query is eligible for multi-candidate disambiguation.

    Only triggers when:
    - The query is definitional ("what is X", "tell me about X")
    - Wikipedia was the selected book (encyclopedic ambiguity, not Q&A)
    - The search term is a single word — multi-word terms already carry
      enough context to disambiguate themselves
    - An LLM backend is configured
    """
    if not settings.llm_url or not settings.llm_model:
        return False
    if not _is_definitional_query(query):
        return False
    if not any("wikipedia" in b for b in selected_books):
        return False
    if len(search_terms.split()) != 1:
        return False
    return True


def _build_search_terms(query: str) -> str:
    """
    Strip stop words and stem remaining words to build Kiwix search terms.

    Stemming fixes disambiguation — "galaxies" → "galaxy", "batteries" →
    "battery" — so Kiwix's search engine finds the right articles instead
    of brand name matches. The original query is kept separately for
    scoring so context is preserved there.

    Contractions are normalized before stop-word matching — "what's"
    otherwise survives as "what'" (a stray apostrophe left over after
    stemming strips the trailing "s"), which never matches the "what"
    stop word and pollutes the search term with leftover punctuation.

    Discourse-framing phrases ("everyone keeps talking about", "everyone's
    obsessed with") are stripped as whole units before tokenizing — found
    via real usage: once router.py was fixed to correctly include kiwix
    for these phrasings, the words "everyone", "obsessed", "talking",
    "keep" still survived stop-word stripping as literal search terms,
    polluting the actual query enough that Kiwix matched scattered noise
    ("Howard Wolowitz") far more readily than the real topic word
    ("bitcoin") could compete against.

    Single alphanumeric characters ("c", "r") are kept rather than
    filtered by length alone — found via a deliberate "bulletproofing"
    pass independently re-discovering the exact same bug already found
    and fixed in scoring.py's _keywords(): "what is r programming used
    for" reduced to the literal Kiwix search query "programm," losing
    the one word that actually distinguishes this from any other
    programming language. Bare punctuation residue (e.g. a stray "-"
    surviving the apostrophe-stripping regex above) is still excluded
    via the isalnum() check, the same way scoring.py's fix avoids
    reintroducing that noise.
    """
    query = _strip_discourse_framing(query)
    query = _strip_colloquial_phrases(query)
    normalized_words = [
        re.sub(r"['']\w*$", "", w) for w in query.lower().split()
    ]
    return " ".join(
        _stem(w) for w in normalized_words
        if w not in _STOP_WORDS and (len(w) > 1 or (len(w) == 1 and w.isalnum()))
    ) or query


def search(query: str) -> str:
    books = get_books()

    if not books:
        return "No books available in Kiwix catalog."

    if settings.llm_url and settings.llm_model:
        selected_books = _pick_books_with_llm(query, books)
    else:
        # Fallback — Wikipedia first
        fallback = next((b["name"] for b in books if "wikipedia" in b["name"]), books[0]["name"])
        selected_books = [fallback]

    if not selected_books:
        # Defensive — currently unreachable: when books is non-empty and LLM
        # is unconfigured, the inline fallback above always produces a non-empty
        # list; when LLM IS configured, _pick_books_with_llm's own internal
        # fallback (_fallback_book_choice) also always returns at least one book.
        # Kept deliberately: _pick_books_with_llm already has an early `return []`
        # for empty books (which can't reach here), and its internal logic could
        # change — this guard surfaces a clean, specific, already-documented
        # error message instead of a confusing downstream crash if that contract
        # is ever violated by a future refactor. Same rationale as the
        # `if not found_mount` warning in main.py's lifespan function.
        return "Could not determine which Kiwix book to search."

    search_terms = _build_search_terms(query)

    # For short definitional queries resolved to Wikipedia where the search
    # term is a single ambiguous word, try multiple disambiguation candidates
    # and let real Kiwix results + scoring decide which one actually works —
    # rather than trusting a single blind LLM guess about Kiwix's index
    #
    # Eligibility is checked against the FULL search_terms, not primary_term
    # — primary_term is always exactly one word by construction (it's the
    # longest word picked OUT of search_terms), so checking its word count
    # is trivially always true and defeats the entire point of restricting
    # disambiguation to genuinely short/ambiguous queries. Found via real
    # usage: "raspberry pi gpio permission errors in python" (5+ real
    # content words, completely unambiguous) was still triggering
    # disambiguation on "permission" alone, discarding "raspberry"/"pi"/
    # "gpio"/"python" — landing on an unrelated macOS permissions article.
    primary_term = max(search_terms.split(), key=len) if search_terms else search_terms
    disambiguating = _should_disambiguate(query, search_terms, selected_books)
    if disambiguating:
        disambiguation_candidates = _get_disambiguation_candidates(query, primary_term)
    else:
        disambiguation_candidates = None

    _LOGGER.info(
        "Kiwix search terms: %s (disambiguating=%s, from query: '%s')",
        disambiguation_candidates if disambiguating else search_terms, disambiguating, query[:50]
    )

    # Search each selected book, collect all results, deduplicate by URL —
    # scoring below picks the actual winner across everything.
    #
    # Found via a deliberate complexity-investigation pass: disambiguation
    # candidates are specifically Wikipedia-oriented phrasings (built to
    # resolve encyclopedic ambiguity), but the search loop previously
    # applied them to EVERY selected book when multiple books were chosen
    # — including a non-Wikipedia secondary book the disambiguation
    # mechanism was never designed for. This never produced a wrong final
    # answer (scoring still picks the genuine best result across
    # everything, so an irrelevant secondary-book result from a
    # mismatched disambiguation term would simply score low and lose),
    # but it meant real, unnecessary extra Kiwix requests against a book
    # disambiguation has no actual business searching with those specific
    # terms. Each book now searches with the term list that's actually
    # appropriate for it — disambiguation candidates for a Wikipedia
    # book, the plain search_terms for anything else.
    all_results = []
    seen_urls = set()
    for book in selected_books:
        if disambiguating and "wikipedia" in book:
            # The PLAIN search terms are pooled alongside the LLM's
            # candidate phrasings, never replaced by them — found live
            # via an explanation-chain trace of "what is a galaxy"
            # returning "Galaxy morphological classification": the
            # candidates (specific sub-sense phrasings by design) each
            # searched fine, but the bare term "galaxy" itself was
            # never searched at all, so the plain "Galaxy" article
            # never even entered the pool — and scoring can only rank
            # what was fetched. The candidates exist to ADD senses the
            # bare term under-serves, not to bet everything on the LLM
            # having guessed the right ones; the plain sense is the one
            # candidate that's always legitimate, and for a definitional
            # ask it's usually the answer. One extra same-LAN Kiwix
            # request per disambiguating Wikipedia book; the URL dedup
            # below already absorbs any overlap.
            terms_for_book = [search_terms] + [
                c for c in disambiguation_candidates if c != search_terms
            ]
        else:
            terms_for_book = [search_terms]
        for term in terms_for_book:
            for r in _search_book(term, book):
                if r["url"] not in seen_urls:
                    seen_urls.add(r["url"])
                    all_results.append(r)

    if not all_results:
        return f"No results found in {', '.join(selected_books)}."

    # Decorate-sort-once — each result's score is computed exactly one
    # time, up front. Found via a deliberate function-by-function audit:
    # the previous shape recomputed _score_result() for the same result
    # up to three separate times (once inside the sort key, once more
    # for `top`, and once more per best-per-book candidate during
    # multi-book fusion). Not a correctness issue — the function is
    # pure — but _score_result() does real per-call work (discourse-
    # framing stripping, stop-word set algebra, per-word stemming), so
    # paying it once per result is strictly cheaper for the identical
    # outcome. Keyed by URL for the fusion lookup below — URLs are
    # already the dedup identity for all_results (see seen_urls above),
    # so they're guaranteed unique here.
    scored_pairs = sorted(
        ((_score_result(r, query, selected_books[0]), r) for r in all_results),
        key=lambda pair: pair[0],
        reverse=True,
    )
    score_by_url = {r["url"]: s for s, r in scored_pairs}
    scored = [r for _, r in scored_pairs]
    top = scored[0]
    top_score = scored_pairs[0][0]
    _LOGGER.info(
        "Selected article '%s' from %s (score=%d)",
        top["title"], top["book"], top_score
    )

    # Multi-book fusion — when multiple books were selected, check if more
    # than one book has a strong, relevant result (not just the LLM picking
    # a tangentially-related book). Only fuse when results from different
    # books are genuinely competitive in relevance, not when one book
    # dominates and the rest are noise.
    if len(selected_books) > 1 and top_score > 0:
        # Found via a deliberate complexity-investigation pass: a result
        # can legitimately score negative (a non-Wikipedia list/index
        # article nets -10 with zero other matches; a Wikipedia one
        # nets -2 or -7 after the Wikipedia bonus partially offsets the
        # penalty).
        # If the OVERALL best result across every book happens to be
        # negative — every candidate is genuinely poor, not just one
        # book's — "score >= top_score * 0.5" silently breaks down for
        # a negative top_score (e.g. -10 >= -5 is False), meaning even
        # the top result itself wouldn't pass its own bar. This never
        # produced a wrong final ANSWER — when a genuinely good result
        # exists anywhere, it becomes `top` by construction, so the
        # bug can only manifest when literally every candidate is
        # already poor, in which case falling through to the single
        # best (still poor) result is the same, correct outcome this
        # accidentally produced anyway. The explicit `top_score > 0`
        # guard makes that intent clear and correct by construction,
        # rather than relying on the threshold math breaking down to
        # accidentally reach the right answer.
        best_per_book: dict[str, dict] = {}
        for r in scored:
            book = r["book"]
            if book not in best_per_book:
                best_per_book[book] = r

        relevant_books = []
        for book, result in best_per_book.items():
            score = score_by_url[result["url"]]
            # Found via a deliberate config-completeness audit: this is
            # the actual, central "should a second book be fused in, or
            # dropped as noise" decision, documented in the README and
            # wiki as the real mechanism behind multi-book fusion — but
            # previously hardcoded with no way to tune it.
            if score >= top_score * settings.kiwix_multi_book_fusion_threshold_pct:
                relevant_books.append((book, result, score))

        if len(relevant_books) > 1:
            return _fuse_multi_book_results(relevant_books)

    article_text = _fetch_article(top["url"])
    if not article_text:
        # Found via a deliberate "bulletproofing" pass: this loop
        # previously tried EVERY remaining scored result with no upper
        # bound — a realistic worst case (multiple books, disambiguation
        # active, up to ~59 total results) could mean up to 59
        # sequential article-fetch attempts at a real 10s timeout each,
        # nearly 10 minutes for one search request, if Kiwix's search
        # endpoint stayed healthy but the specific article-content path
        # kept failing (a malformed page, a broken link, a transient
        # timeout). Capped at 5 — generous enough to recover from a
        # realistic cluster of a few broken links near the top of the
        # results without trying every one, narrow enough to bound the
        # worst case to under a minute.
        for candidate in scored[1:6]:
            article_text = _fetch_article(candidate["url"])
            if article_text:
                top = candidate
                break

    if not article_text:
        return f"Found {top['title']} but could not fetch article content.\nURL: {top['url']}"

    return f"# {top['title']}\nSource: {top['book']}\n\n{article_text}"


def _fuse_multi_book_results(relevant_books: list[tuple[str, dict, int]]) -> str:
    """
    Merge the best result from each relevant book into one response,
    mirroring the cross-source fusion pattern in fusion.py — truncated
    per-book sections with attribution headers, sorted by relevance.
    """
    from app.sources.fusion import _truncate

    # Sort by score descending so the most relevant book's result leads
    relevant_books = sorted(relevant_books, key=lambda x: x[2], reverse=True)

    sections = []
    for book, result, score in relevant_books:
        article_text = _fetch_article(result["url"])
        if not article_text:
            continue
        truncated = _truncate(article_text)
        sections.append(f"[{book.upper()}]\n# {result['title']}\n\n{truncated}")

    if not sections:
        return "Found multiple relevant books but could not fetch article content."

    if len(sections) == 1:
        # Only one book's article actually fetched successfully — return plainly
        return sections[0].split("\n", 1)[1].lstrip()

    _LOGGER.info("Multi-book fusion: merged %d books", len(sections))
    return "\n\n---\n\n".join(sections)
