import time
import logging
import requests
from bs4 import BeautifulSoup
from app.config import settings
from app.scoring import filter_and_rank

_LOGGER = logging.getLogger(__name__)

# Words that indicate a general "give me everything" news request
# — skip filtering for these so Jarvis gets a full feed summary
_GENERAL_QUERIES = {
    "news", "headlines", "feeds",
    "what's happening", "whats happening", "my feeds", "rss",
}

# Stop words to ignore when checking for a general query
#
# Found via a deliberate complexity-investigation pass: the original
# set only handled formal/grammatical filler ("the", "is", "about"),
# missing the common REQUEST verbs and modifiers that show up in how
# people actually phrase a general news ask out loud — "tell me the
# news", "give me the headlines", "any news today" all failed to be
# recognized as general queries, since "tell"/"give"/"any"/"today"
# weren't stop words and survived to make `meaningful` a non-subset of
# _GENERAL_QUERIES. A direct test against 9 realistic phrasings found 9
# of 9 failing before this fix. Also added "whats" (no apostrophe) —
# _GENERAL_QUERIES already handled both apostrophe forms for the full
# phrase ("what's happening" / "whats happening"), but the bare
# contracted word itself was never a recognized stop word on its own,
# so "whats new" still failed even after the verb additions above.
_STOP_WORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "used", "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "about", "what", "whats", "which", "who", "whom",
    "this", "that", "these", "those", "i", "me", "my", "we", "our",
    "you", "your", "he", "she", "it", "they", "them", "their",
    "tell", "give", "show", "read", "check", "catch", "get", "find",
    "bring", "fetch", "pull", "up", "any", "new", "update", "updates",
    "today", "now", "currently", "please", "and",
}

# Recency bonus tiers — newer articles score higher, encouraging fresher
# news to surface over older articles with similar keyword relevance
_RECENCY_TIERS = [
    (3600, 15),       # published within the last hour
    (21600, 10),      # within 6 hours
    (86400, 5),       # within 24 hours
]


def _is_general_query(query: str) -> bool:
    """Return True if the query is a general news request with no specific topic."""
    query_lower = query.lower().strip()
    # Check if the full query matches a general phrase
    if query_lower in _GENERAL_QUERIES:
        return True

    # Multi-word _GENERAL_QUERIES phrases ("what's happening", "my
    # feeds") checked against the ORIGINAL query text directly,
    # deliberately independent of stop-word stripping below. A real
    # interaction bug was found and avoided here: adding "whats" to
    # _STOP_WORDS (to fix "whats new" as its own case) would otherwise
    # strip "whats" out of "catch me up on whats happening" before any
    # phrase check ran, breaking the match against the existing "whats
    # happening" entry. Checking the raw query keeps the two mechanisms
    # from being able to interfere with each other regardless of what
    # either set contains in the future. The remainder (whatever's left
    # after removing the matched phrase) still has to be entirely stop
    # words — otherwise "what's happening with bitcoin" would wrongly
    # match on the embedded substring "what's happening" despite
    # clearly being a specific-topic question, not a general one.
    multi_word_phrases = [p for p in _GENERAL_QUERIES if " " in p]
    for phrase in multi_word_phrases:
        if phrase in query_lower:
            remainder_words = set(query_lower.replace(phrase, "").split())
            if not (remainder_words - _STOP_WORDS):
                return True

    # Check if all meaningful words (after stop word removal) are general terms
    words = set(query_lower.split())
    meaningful = words - _STOP_WORDS
    return not meaningful or meaningful.issubset(_GENERAL_QUERIES)


def _recency_bonus(published_unix: int | None) -> int:
    """Return a recency bonus based on how long ago an article was published."""
    if not published_unix:
        return 0
    age_seconds = time.time() - published_unix
    if age_seconds < 0:
        return 0
    for max_age, bonus in _RECENCY_TIERS:
        if age_seconds <= max_age:
            return bonus
    return 0


# Cached FreshRSS Auth token — reused across calls rather than re-fetched
# on every single search. Found via a deliberate function-by-function
# audit: search() previously paid a full ClientLogin round trip (an extra
# HTTP request + credential exchange against FreshRSS) on EVERY query,
# despite Google Reader API Auth tokens being long-lived by design —
# FreshRSS's own GReader implementation derives the token from stable
# credentials rather than rotating it per session, so the same token
# stays valid indefinitely until the API password changes. The identical
# class of per-call-connection-setup cost already found and fixed twice
# in this project (uptime_kuma.py's Socket.IO login, llm.py's TCP
# connections), just one layer up the stack.
#
# Self-healing rather than TTL-guessing: search() invalidates and
# re-fetches exactly once on an HTTP 401 from the articles request (the
# real signal the token genuinely stopped working — an API password
# change, a FreshRSS reinstall), so a stale token costs one extra
# request, never a wrong answer. A plain module-level variable with no
# lock, mirroring kiwix.py's _book_cache: two threads racing a cold
# token both perform a harmless duplicate login and the last write wins
# — a benign race, not a correctness one.
_cached_token: str | None = None


def _get_token(force_refresh: bool = False) -> str | None:
    global _cached_token
    if _cached_token and not force_refresh:
        return _cached_token
    try:
        resp = requests.post(
            f"{settings.freshrss_url}/api/greader.php/accounts/ClientLogin",
            data={"Email": settings.freshrss_user, "Passwd": settings.freshrss_api_password},
            timeout=5,
        )
        if resp.status_code != 200:
            _LOGGER.warning("FreshRSS auth failed: HTTP %d", resp.status_code)
            _cached_token = None
            return None
        for line in resp.text.splitlines():
            if line.startswith("Auth="):
                _cached_token = line[5:]
                return _cached_token
        _LOGGER.warning("FreshRSS auth response missing Auth= token")
        _cached_token = None
        return None
    except Exception as e:
        _LOGGER.warning("FreshRSS auth request failed: %s", e)
        _cached_token = None
        return None


def search(query: str) -> str:
    if not settings.freshrss_url or not settings.freshrss_user:
        return "FreshRSS is not configured. Set FRESHRSS_URL and FRESHRSS_USER."
    token = _get_token()
    if not token:
        return "Error: Could not authenticate with FreshRSS. Check credentials."
    try:
        resp = requests.get(
            f"{settings.freshrss_url}/api/greader.php/reader/api/0/stream/contents/reading-list",
            headers={"Authorization": f"GoogleLogin auth={token}"},
            params={"n": settings.freshrss_max_articles, "output": "json"},
            timeout=10,
        )
        if resp.status_code == 401:
            # The cached token stopped working (API password changed,
            # FreshRSS reinstalled) — invalidate, re-authenticate once,
            # and retry. Exactly one retry: a second 401 with a genuinely
            # fresh token means the credentials themselves are wrong,
            # which retrying can't fix.
            _LOGGER.info("FreshRSS returned 401 with cached token — re-authenticating")
            token = _get_token(force_refresh=True)
            if not token:
                return "Error: Could not authenticate with FreshRSS. Check credentials."
            resp = requests.get(
                f"{settings.freshrss_url}/api/greader.php/reader/api/0/stream/contents/reading-list",
                headers={"Authorization": f"GoogleLogin auth={token}"},
                params={"n": settings.freshrss_max_articles, "output": "json"},
                timeout=10,
            )
        if resp.status_code != 200:
            _LOGGER.warning("FreshRSS articles request failed: HTTP %d", resp.status_code)
            return f"Error: FreshRSS returned {resp.status_code}"

        items = resp.json().get("items", [])
        if not items:
            return "No recent articles found in FreshRSS."

        # Parse all articles, including published timestamp for recency scoring
        articles = []
        for item in items:
            title = item.get("title", "No title")
            source = item.get("origin", {}).get("title", "Unknown source")
            # Found via a deliberate function-by-function read: the
            # original `re.sub(r"<[^>]+>", "", ...)` regex approach has
            # two real, distinct gaps a genuine HTML parser doesn't.
            # First, HTML entities (&amp;, &lt;, &gt;) were never
            # decoded, so they survived as literal text in the output —
            # cosmetic, not a parsing failure, but still wrong. Second,
            # and more seriously: `[^>]+` stops at the FIRST `>` it
            # finds, with no awareness of quoted attribute values — a
            # real, plausible tag like `<img alt="a > b">` truncates the
            # match at the `>` inside the quoted alt text, leaving the
            # genuine tag boundary unmatched and `">` syntax bleeding
            # directly into the visible summary text. BeautifulSoup
            # (already a real, existing dependency — see kiwix.py's own
            # identical get_text() convention) uses a real parser that
            # understands attribute-value boundaries and decodes
            # entities automatically, fixing both gaps with the same
            # change rather than patching the regex twice.
            summary = BeautifulSoup(
                item.get("summary", {}).get("content", ""), "html.parser"
            ).get_text()[:300].strip()
            published = item.get("published")
            articles.append({
                "title": title,
                "source": source,
                "content": summary,
                "url": item.get("canonical", [{}])[0].get("href", "") if item.get("canonical") else "",
                "_recency_bonus": _recency_bonus(published),
            })

        # General query — return everything, no filtering
        if _is_general_query(query):
            _LOGGER.info("FreshRSS general query — returning all %d articles", len(articles))
            results = [f"**{a['title']}** ({a['source']})\n{a['content']}" for a in articles]
            return "\n\n---\n\n".join(results)

        # Specific query — score and filter by relevance using shared scoring,
        # which includes keyword overlap, generic-result penalty, and recency
        ranked = filter_and_rank(
            articles,
            query,
            score_threshold=settings.web_news_score_threshold,
            top_n=settings.web_news_top_n,
        )

        if ranked:
            _LOGGER.info(
                "FreshRSS filtered %d relevant articles from %d for query: %s",
                len(ranked), len(articles), query[:50]
            )
            results = [f"**{a['title']}** ({a['source']})\n{a['content']}" for a in ranked]
            return "\n\n---\n\n".join(results)

        # Nothing matched — fall back to full list with a note
        _LOGGER.info("FreshRSS no relevant articles found for query '%s', returning all", query[:50])
        results = [f"**{a['title']}** ({a['source']})\n{a['content']}" for a in articles]
        return "No articles specifically about that topic — here are the latest:\n\n" + "\n\n---\n\n".join(results)

    except Exception as e:
        _LOGGER.error("FreshRSS fetch error: %s", e)
        return f"Error fetching FreshRSS articles: {e}"
