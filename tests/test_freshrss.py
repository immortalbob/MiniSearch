"""
Tests for app/sources/freshrss.py — general query detection and article scoring.
No network calls required.
"""


# ---------------------------------------------------------------------------
# General query detection
# ---------------------------------------------------------------------------

class TestIsGeneralQuery:
    """Tests for _is_general_query — determines if filtering should be skipped."""

    def setup_method(self):
        from app.sources.freshrss import _is_general_query
        self.check = _is_general_query

    def test_news_is_general(self):
        assert self.check("news") is True

    def test_headlines_is_general(self):
        assert self.check("headlines") is True

    def test_my_feeds_is_general(self):
        assert self.check("my feeds") is True

    def test_rss_is_general(self):
        assert self.check("rss") is True

    def test_whats_happening_is_general(self):
        assert self.check("what's happening") is True

    def test_whats_happening_no_apostrophe_is_general(self):
        assert self.check("whats happening") is True

    def test_specific_topic_is_not_general(self):
        assert self.check("news about politics") is False

    def test_specific_technology_is_not_general(self):
        assert self.check("articles about Docker") is False

    def test_specific_event_is_not_general(self):
        assert self.check("news about the election") is False

    # Regression tests — things that used to wrongly be general
    def test_latest_iphone_is_not_general(self):
        assert self.check("latest iPhone release") is False

    def test_recent_earthquakes_is_not_general(self):
        assert self.check("recent earthquakes") is False

    def test_recent_python_is_not_general(self):
        assert self.check("recent Python releases") is False

    # Regression tests for a real, significant gap found via a deliberate
    # complexity-investigation pass: nearly every natural phrasing of a
    # general news request (with a real request verb like "tell"/"give")
    # was being misclassified as a SPECIFIC topic query, since common
    # request verbs weren't in _STOP_WORDS — meaning these queries went
    # through scoring against literal words like "tell"/"give" instead
    # of cleanly returning the general feed.
    def test_tell_me_the_news_is_general(self):
        assert self.check("tell me the news") is True

    def test_give_me_the_headlines_is_general(self):
        assert self.check("give me the headlines") is True

    def test_show_me_my_feeds_is_general(self):
        assert self.check("show me my feeds") is True

    def test_give_me_a_news_update_is_general(self):
        assert self.check("give me a news update") is True

    def test_any_news_today_is_general(self):
        assert self.check("any news today") is True

    def test_check_the_news_is_general(self):
        assert self.check("check the news") is True

    def test_whats_new_is_general(self):
        """Regression test for a second, distinct gap found in the same
        investigation: "whats" (no apostrophe) was never itself a
        recognized stop word, even though _GENERAL_QUERIES already
        handled the apostrophe-free form of "whats happening" as a full
        phrase. "whats new" failed even after the request-verb fix
        above, since "whats" alone still survived stop-word removal."""
        assert self.check("whats new") is True

    def test_catch_me_up_on_the_news_is_general(self):
        assert self.check("catch me up on the news") is True

    def test_catch_me_up_on_whats_happening_is_general(self):
        """Regression test for a real interaction bug found while fixing
        the "whats new" case above: adding "whats" to _STOP_WORDS would,
        if checked naively, strip "whats" out of "whats happening"
        BEFORE any multi-word phrase check ran against it, breaking the
        match against the existing "whats happening" _GENERAL_QUERIES
        entry. The fix checks multi-word phrases against the original
        query text directly, independent of stop-word stripping, so the
        two mechanisms can't interfere with each other."""
        assert self.check("catch me up on whats happening") is True

    def test_whats_happening_with_specific_topic_is_not_general(self):
        """Confirms the multi-word phrase fix doesn't over-match — a
        genuinely specific-topic question that happens to CONTAIN
        "what's happening" as a literal substring must still be
        correctly classified as specific, since the remainder
        ("bitcoin") isn't accounted for by the matched phrase at all."""
        assert self.check("what's happening with bitcoin") is False

    def test_news_about_bitcoin_is_not_general(self):
        """Confirms the broader fix doesn't introduce a blind substring
        match against the whole query — "news" appearing as one word
        among other genuinely specific topic words must still correctly
        classify as a specific query, not general."""
        assert self.check("what's the latest news about bitcoin") is False


# ---------------------------------------------------------------------------
# Article scoring — now delegated to app.scoring.filter_and_rank, see
# tests/test_scoring.py for the underlying scoring mechanics. These tests
# confirm freshrss.py wires it in correctly, not the scoring math itself.
# ---------------------------------------------------------------------------

class TestRecencyBonus:
    """Tests for _recency_bonus — freshness scoring for news articles."""

    def test_no_bonus_for_missing_timestamp(self):
        from app.sources.freshrss import _recency_bonus
        assert _recency_bonus(None) == 0

    def test_no_bonus_for_zero_timestamp(self):
        from app.sources.freshrss import _recency_bonus
        assert _recency_bonus(0) == 0

    def test_high_bonus_for_very_recent_article(self):
        from app.sources.freshrss import _recency_bonus
        import time
        one_minute_ago = int(time.time()) - 60
        assert _recency_bonus(one_minute_ago) == 15

    def test_medium_bonus_for_few_hours_old(self):
        from app.sources.freshrss import _recency_bonus
        import time
        four_hours_ago = int(time.time()) - (4 * 3600)
        assert _recency_bonus(four_hours_ago) == 10

    def test_low_bonus_for_within_a_day(self):
        from app.sources.freshrss import _recency_bonus
        import time
        twenty_hours_ago = int(time.time()) - (20 * 3600)
        assert _recency_bonus(twenty_hours_ago) == 5

    def test_no_bonus_for_old_article(self):
        from app.sources.freshrss import _recency_bonus
        import time
        three_days_ago = int(time.time()) - (3 * 86400)
        assert _recency_bonus(three_days_ago) == 0

    def test_no_bonus_for_future_timestamp(self):
        from app.sources.freshrss import _recency_bonus
        import time
        future = int(time.time()) + 3600
        assert _recency_bonus(future) == 0


class TestGetToken:
    """Tests for _get_token FreshRSS authentication."""

    def setup_method(self):
        # _get_token() now caches its result module-level (see
        # freshrss._cached_token) — reset between tests so one test's
        # cached token can't leak into another's assertions.
        from app.sources import freshrss
        freshrss._cached_token = None

    def test_returns_token_on_success(self):
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "SID=abc\nLSID=def\nAuth=mytoken123\n"
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp):
            token = freshrss._get_token()
        assert token == "mytoken123"
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_returns_none_when_response_is_200_but_missing_auth_token(self):
        """Regression test for a real, documented FreshRSS-side
        misconfiguration (confirmed via real production reports, not a
        contrived scenario): ClientLogin can return a genuine HTTP 200
        with a body of just "OK" and no Auth= token at all, rather than
        the expected three-line SID/LSID/Auth response. This isn't
        something Mnemolis can fix — it's a real upstream FreshRSS
        config issue — but the existing code already handles it
        correctly (falls through the line-scan loop, logs a warning,
        returns None) without needing any changes. This test locks in
        that already-correct behavior against the exact real-world
        response shape rather than leaving it unverified."""
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "OK"
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp):
            token = freshrss._get_token()
        assert token is None
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_returns_none_on_auth_failure(self):
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "wrong"
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp):
            token = freshrss._get_token()
        assert token is None
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_returns_none_on_connection_error(self):
        from app.sources import freshrss
        from app.config import settings
        import requests as req
        from unittest.mock import patch
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"
        with patch("app.sources.freshrss.requests.post", side_effect=req.exceptions.ConnectionError()):
            token = freshrss._get_token()
        assert token is None
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_returns_none_when_auth_missing_from_response(self):
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "SID=abc\nLSID=def\n"  # no Auth= line
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp):
            token = freshrss._get_token()
        assert token is None
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_second_call_uses_cached_token_without_a_network_call(self):
        """The whole point of the cache: search() previously paid a full
        ClientLogin round trip on every single query. A second
        _get_token() call must return the cached token without touching
        the network at all."""
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "SID=abc\nLSID=def\nAuth=mytoken123\n"
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp) as mock_post:
            first = freshrss._get_token()
            second = freshrss._get_token()
        assert first == second == "mytoken123"
        assert mock_post.call_count == 1
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_force_refresh_bypasses_cached_token(self):
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Auth=freshtoken456\n"
        freshrss._cached_token = "staletoken"
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp) as mock_post:
            token = freshrss._get_token(force_refresh=True)
        assert token == "freshtoken456"
        assert mock_post.call_count == 1
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_failed_refresh_clears_stale_cached_token(self):
        """A failed re-auth must not leave the old, known-bad token
        cached — otherwise the very next call would happily reuse the
        token that just failed, defeating the 401-retry path entirely."""
        from app.sources import freshrss
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "wrong"
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        freshrss._cached_token = "staletoken"
        with patch("app.sources.freshrss.requests.post", return_value=mock_resp):
            token = freshrss._get_token(force_refresh=True)
        assert token is None
        assert freshrss._cached_token is None
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""


class TestSearchTokenRetry:
    """Tests for search()'s single 401 retry with a freshly-fetched token
    — the self-healing half of the token cache: a stale cached token
    (API password changed, FreshRSS reinstalled) costs exactly one extra
    request, never a wrong answer and never an unbounded retry loop."""

    def setup_method(self):
        from app.sources import freshrss
        freshrss._cached_token = None

    def _configure(self):
        from app.config import settings
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"

    def _deconfigure(self):
        from app.config import settings
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        settings.freshrss_api_password = ""

    def test_401_with_cached_token_reauths_once_and_succeeds(self):
        from app.sources import freshrss
        from unittest.mock import patch, MagicMock
        self._configure()
        freshrss._cached_token = "staletoken"

        auth_resp = MagicMock()
        auth_resp.status_code = 200
        auth_resp.text = "Auth=freshtoken\n"

        stale_resp = MagicMock()
        stale_resp.status_code = 401
        fresh_resp = MagicMock()
        fresh_resp.status_code = 200
        fresh_resp.json.return_value = {"items": [{
            "title": "Real headline",
            "origin": {"title": "Real source"},
            "summary": {"content": "Real content"},
            "published": None,
        }]}

        try:
            with patch("app.sources.freshrss.requests.post", return_value=auth_resp) as mock_post, \
                 patch("app.sources.freshrss.requests.get", side_effect=[stale_resp, fresh_resp]) as mock_get:
                result = freshrss.search("news")
            assert "Real headline" in result
            assert mock_get.call_count == 2   # stale attempt + one retry
            assert mock_post.call_count == 1  # exactly one re-auth
            # The retry must have used the FRESH token, not the stale one
            retry_headers = mock_get.call_args_list[1].kwargs["headers"]
            assert "freshtoken" in retry_headers["Authorization"]
        finally:
            self._deconfigure()

    def test_second_401_after_fresh_token_does_not_retry_again(self):
        from app.sources import freshrss
        from unittest.mock import patch, MagicMock
        self._configure()
        freshrss._cached_token = "staletoken"

        auth_resp = MagicMock()
        auth_resp.status_code = 200
        auth_resp.text = "Auth=freshtoken\n"
        denied = MagicMock()
        denied.status_code = 401

        try:
            with patch("app.sources.freshrss.requests.post", return_value=auth_resp), \
                 patch("app.sources.freshrss.requests.get", return_value=denied) as mock_get:
                result = freshrss.search("news")
            # Exactly two article requests (original + single retry), then
            # an honest error — never a loop.
            assert mock_get.call_count == 2
            assert "Error" in result
        finally:
            self._deconfigure()


class TestSearch:
    """Tests for search() — previously had zero direct test coverage at
    all, found via a deliberate function-by-function read. These focus
    first on the real bug found and fixed in this same pass (HTML
    handling in article summaries), then on the function's other core
    behaviors that had never been directly exercised either."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.freshrss_url
        self._orig_user = settings.freshrss_user
        self._orig_password = settings.freshrss_api_password
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "admin"
        settings.freshrss_api_password = "password"

    def teardown_method(self):
        from app.config import settings
        settings.freshrss_url = self._orig_url
        settings.freshrss_user = self._orig_user
        settings.freshrss_api_password = self._orig_password

    def _mock_articles_response(self, items):
        from unittest.mock import MagicMock
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"items": items}
        return mock_resp

    def test_returns_not_configured_message_when_url_missing(self):
        from app.sources import freshrss
        from app.config import settings
        settings.freshrss_url = ""
        result = freshrss.search("news")
        assert "not configured" in result.lower()

    def test_returns_auth_error_when_token_unavailable(self):
        from app.sources import freshrss
        from unittest.mock import patch
        with patch("app.sources.freshrss._get_token", return_value=None):
            result = freshrss.search("news")
        assert "could not authenticate" in result.lower()

    def test_html_entities_are_decoded_not_left_as_literal_text(self):
        """The first half of the real bug this pass found and fixed:
        the original regex-based HTML stripper never decoded entities,
        so &amp; survived as literal text in the actual response. A
        real parser (BeautifulSoup, already a project dependency) does
        this correctly as a side effect of parsing."""
        from app.sources import freshrss
        from unittest.mock import patch
        items = [{
            "title": "Test Article", "origin": {"title": "Source"},
            "summary": {"content": "Cats &amp; dogs are friends"},
            "published": None, "canonical": [],
        }]
        with patch("app.sources.freshrss._get_token", return_value="tok"), \
             patch("app.sources.freshrss.requests.get", return_value=self._mock_articles_response(items)):
            result = freshrss.search("cats and dogs")
        assert "Cats & dogs are friends" in result
        assert "&amp;" not in result

    def test_angle_bracket_inside_quoted_attribute_does_not_leak_into_summary(self):
        """The second, more serious half of the real bug: the original
        regex `<[^>]+>` stops at the FIRST `>` it finds, with no
        awareness of quoted attribute values. A real `<img>` tag with a
        literal `>` inside its `alt` attribute used to truncate the
        match early, leaking raw `">` syntax directly into the visible
        summary text instead of being recognized as part of the same
        tag. Confirmed this exact case fails against the original regex
        before switching to BeautifulSoup, which correctly understands
        attribute-value boundaries."""
        from app.sources import freshrss
        from unittest.mock import patch
        items = [{
            "title": "Test Article", "origin": {"title": "Source"},
            "summary": {"content": '<img src="x.jpg" alt="A description with a > in it">After image text'},
            "published": None, "canonical": [],
        }]
        with patch("app.sources.freshrss._get_token", return_value="tok"), \
             patch("app.sources.freshrss.requests.get", return_value=self._mock_articles_response(items)):
            result = freshrss.search("test article")
        assert '">' not in result
        assert "After image text" in result

    def test_general_query_returns_all_articles_unfiltered(self):
        from app.sources import freshrss
        from unittest.mock import patch
        items = [
            {"title": "Article One", "origin": {"title": "Source"}, "summary": {"content": "content one"}, "published": None, "canonical": []},
            {"title": "Article Two", "origin": {"title": "Source"}, "summary": {"content": "content two"}, "published": None, "canonical": []},
        ]
        with patch("app.sources.freshrss._get_token", return_value="tok"), \
             patch("app.sources.freshrss.requests.get", return_value=self._mock_articles_response(items)):
            result = freshrss.search("what's happening")
        assert "Article One" in result
        assert "Article Two" in result

    def test_no_items_returns_clean_message_not_a_crash(self):
        from app.sources import freshrss
        from unittest.mock import patch
        with patch("app.sources.freshrss._get_token", return_value="tok"), \
             patch("app.sources.freshrss.requests.get", return_value=self._mock_articles_response([])):
            result = freshrss.search("news")
        assert "no recent articles" in result.lower()

