"""
Tests for app/sources/freshrss.py — network-dependent functions.
Uses unittest.mock to avoid real network calls.
"""
from unittest.mock import patch, MagicMock


def _mock_auth_response(success: bool = True) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200 if success else 401
    mock.text = "Auth=abc123token\nSID=something" if success else "Error=BadAuthentication"
    return mock


def _mock_articles_response(items: list) -> MagicMock:
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {"items": items}
    return mock


def _make_item(title: str, source: str, summary: str) -> dict:
    return {
        "title": title,
        "origin": {"title": source},
        "summary": {"content": summary},
    }


class TestFreshRSSGuard:
    """Tests for URL/user guard."""

    def test_returns_not_configured_when_url_blank(self):
        from app.sources import freshrss
        from app.config import settings
        original_url = settings.freshrss_url
        original_user = settings.freshrss_user
        settings.freshrss_url = ""
        settings.freshrss_user = ""
        try:
            result = freshrss.search("news")
            assert "not configured" in result.lower()
        finally:
            settings.freshrss_url = original_url
            settings.freshrss_user = original_user

    def test_returns_not_configured_when_user_blank(self):
        from app.sources import freshrss
        from app.config import settings
        original = settings.freshrss_user
        settings.freshrss_user = ""
        try:
            result = freshrss.search("news")
            assert "not configured" in result.lower()
        finally:
            settings.freshrss_user = original


class TestFreshRSSAuth:
    """Tests for authentication handling."""

    def test_auth_failure_returns_error(self):
        from app.sources import freshrss
        from app.config import settings
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "testuser"
        try:
            with patch("app.sources.freshrss._session.post", return_value=_mock_auth_response(success=False)):
                result = freshrss.search("news")
            assert "error" in result.lower() or "could not authenticate" in result.lower()
        finally:
            settings.freshrss_url = ""
            settings.freshrss_user = ""

    def test_connection_error_returns_error(self):
        from app.sources import freshrss
        from app.config import settings
        import requests
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "testuser"
        try:
            with patch("app.sources.freshrss._session.post", side_effect=requests.exceptions.ConnectionError("refused")):
                result = freshrss.search("news")
            assert "error" in result.lower()
        finally:
            settings.freshrss_url = ""
            settings.freshrss_user = ""


class TestFreshRSSArticles:
    """Tests for article fetching with mocked responses."""

    def setup_method(self):
        from app.config import settings
        settings.freshrss_url = "http://freshrss"
        settings.freshrss_user = "testuser"

    def teardown_method(self):
        from app.config import settings
        settings.freshrss_url = ""
        settings.freshrss_user = ""

    def test_empty_feed_returns_no_articles_message(self):
        from app.sources import freshrss
        with patch("app.sources.freshrss._session.post", return_value=_mock_auth_response()), \
             patch("app.sources.freshrss._session.get", return_value=_mock_articles_response([])):
            result = freshrss.search("news")
        assert "no recent articles" in result.lower()

    def test_general_query_returns_all_articles(self):
        from app.sources import freshrss
        items = [
            _make_item("Politics Today", "News", "Political news"),
            _make_item("Tech Update", "Tech", "Technology news"),
            _make_item("Sports Roundup", "Sports", "Sports news"),
        ]
        with patch("app.sources.freshrss._session.post", return_value=_mock_auth_response()), \
             patch("app.sources.freshrss._session.get", return_value=_mock_articles_response(items)):
            result = freshrss.search("news")
        assert "Politics Today" in result
        assert "Tech Update" in result
        assert "Sports Roundup" in result

    def test_specific_query_filters_articles(self):
        from app.sources import freshrss
        items = [
            _make_item("Python Release", "Tech", "Python 3.13 released today"),
            _make_item("Sports Roundup", "Sports", "Game scores and highlights"),
            _make_item("Python Tutorial", "Dev", "How to use Python decorators"),
        ]
        with patch("app.sources.freshrss._session.post", return_value=_mock_auth_response()), \
             patch("app.sources.freshrss._session.get", return_value=_mock_articles_response(items)):
            result = freshrss.search("python programming")
        assert "Python Release" in result
        assert "Python Tutorial" in result
        assert "Sports Roundup" not in result

    def test_no_match_returns_fallback_with_note(self):
        from app.sources import freshrss
        items = [
            _make_item("Sports Roundup", "Sports", "Game scores"),
            _make_item("Politics Today", "News", "Political update"),
        ]
        with patch("app.sources.freshrss._session.post", return_value=_mock_auth_response()), \
             patch("app.sources.freshrss._session.get", return_value=_mock_articles_response(items)):
            result = freshrss.search("medieval japanese pottery")
        assert "no articles specifically" in result.lower()
        assert "Sports Roundup" in result

    def test_articles_formatted_with_source(self):
        from app.sources import freshrss
        items = [_make_item("Test Article", "My Feed", "Article content here")]
        with patch("app.sources.freshrss._session.post", return_value=_mock_auth_response()), \
             patch("app.sources.freshrss._session.get", return_value=_mock_articles_response(items)):
            result = freshrss.search("news")
        assert "My Feed" in result
        assert "Test Article" in result
