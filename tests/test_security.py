"""
Security, fuzz, and concurrency tests for Mnemolis.

Covers:
- SQL injection attempts against query logging and snapshot storage
- Malformed/adversarial input through the router and decomposer
- Path traversal attempts against the backup endpoint
- Token/secret leakage in error messages and logs
- Concurrent access to shared state (cache, routing cache)
- Fuzz testing with extreme input (very long, unicode, null bytes, pure punctuation)
"""
import pytest
import tempfile
import os
import threading
from unittest.mock import patch
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_db = f.name
    with patch("app.main._LOG_DB", temp_db):
        from app.main import app
        with TestClient(app) as c:
            yield c
    os.unlink(temp_db)


class TestSQLInjection:
    """Tests confirming SQL injection attempts are neutralized by parameterized queries."""

    def test_log_query_with_sql_injection_string(self, client):
        from app.main import _log_query
        malicious = "'; DROP TABLE query_log; --"
        # Should not raise, and should not actually drop the table
        _log_query(malicious, "auto", "kiwix", False, True, 100)
        resp = client.get("/logs?limit=1")
        assert resp.status_code == 200

    def test_query_log_survives_injection_attempt(self, client):
        from app.main import _log_query
        _log_query("normal query", "auto", "kiwix", False, True, 50)
        injection = "x'; DELETE FROM query_log WHERE '1'='1"
        _log_query(injection, "auto", "kiwix", False, True, 50)
        # Table should still exist and be queryable
        resp = client.get("/logs/stats")
        assert resp.status_code == 200
        assert resp.json()["total_queries"] >= 0

    def test_search_endpoint_with_sql_injection_query(self, client):
        resp = client.post("/search", json={
            "query": "'; DROP TABLE query_log; --",
            "source": "kiwix"
        })
        # Should return a normal response, not crash
        assert resp.status_code == 200

    def test_union_select_attempt_in_query(self, client):
        from app.main import _log_query
        injection = "' UNION SELECT * FROM sqlite_master --"
        _log_query(injection, "auto", "kiwix", False, True, 50)
        resp = client.get("/logs/stats")
        assert resp.status_code == 200

    def test_snapshot_diff_with_injection_in_content(self):
        from app.snapshots import _diff_uptime
        malicious = "'; DROP TABLE snapshots; -- All services up"
        result = _diff_uptime("All services up.", malicious)
        # Should not raise — diff functions only do string comparison
        assert isinstance(result, list)


class TestPathTraversal:
    """Tests confirming the backup endpoint cannot be used for path traversal."""

    def test_backup_endpoint_uses_fixed_file_list(self, client):
        # The backup endpoint takes no user input, so traversal isn't possible
        # via query params. Confirm it ignores any extra params silently.
        resp = client.get("/backup?file=../../etc/passwd")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/gzip"

    def test_backup_info_ignores_extra_params(self, client):
        resp = client.get("/backup/info?path=/etc/passwd")
        assert resp.status_code == 200
        # Should return the same fixed file list regardless of params
        files = resp.json()["files"]
        assert "passwd" not in files
        assert "cache.json" in files


class TestTokenLeakage:
    """Tests confirming secrets are not leaked in responses or error messages."""

    def test_health_does_not_leak_ha_token(self, client):
        from app.config import settings
        settings.ha_token = "super-secret-token-12345"
        resp = client.get("/health")
        assert "super-secret-token-12345" not in resp.text
        settings.ha_token = ""

    def test_health_does_not_leak_freshrss_password(self, client):
        from app.config import settings
        settings.freshrss_api_password = "secret-password-67890"
        resp = client.get("/health")
        assert "secret-password-67890" not in resp.text
        settings.freshrss_api_password = ""

    def test_ha_connection_error_does_not_leak_token(self, client):
        from app.sources import home_assistant
        from app.config import settings
        import requests as req
        settings.ha_url = "http://homeassistant:8123"
        settings.ha_token = "leak-test-token-abc123"
        with patch("app.sources.home_assistant._session.get",
                   side_effect=req.exceptions.ConnectionError("refused")):
            result = home_assistant.search("house status")
        assert "leak-test-token-abc123" not in result
        settings.ha_url = ""
        settings.ha_token = ""


class TestFuzzInput:
    """Fuzz tests with adversarial or extreme input."""

    def test_very_long_query_does_not_crash_decompose(self):
        from app.router import _decompose
        long_query = "what is the weather and " * 1000
        result = _decompose(long_query)
        assert isinstance(result, list)

    def test_unicode_query_does_not_crash_decompose(self):
        from app.router import _decompose
        result = _decompose("天気予報 and サービス状況 🎉🔥💀")
        assert isinstance(result, list)

    def test_null_byte_in_query_does_not_crash(self, client):
        resp = client.post("/search", json={
            "query": "what is\x00nitrogen",
            "source": "kiwix"
        })
        assert resp.status_code in (200, 422)

    def test_pure_punctuation_query_does_not_crash_decompose(self):
        from app.router import _decompose
        result = _decompose("!@#$%^&*()_+-=[]{}|;:,.<>?")
        assert isinstance(result, list)

    def test_empty_query_does_not_crash_decompose(self):
        from app.router import _decompose
        result = _decompose("")
        assert isinstance(result, list)

    def test_whitespace_only_query_does_not_crash(self):
        from app.router import _decompose
        result = _decompose("     ")
        assert isinstance(result, list)

    def test_extremely_long_single_word_does_not_crash_kiwix_scoring(self):
        from app.sources.kiwix import _score_result
        huge_word = "a" * 100000
        result = {"title": huge_word, "excerpt": "", "book": "wikipedia_en_all_maxi_2026-02"}
        score = _score_result(result, huge_word, "wikipedia_en_all_maxi_2026-02")
        assert isinstance(score, int)

    def test_repeated_conjunctions_do_not_cause_infinite_loop(self):
        from app.router import _decompose

        result = _decompose("and and and and and and weather and services")
        # Should terminate — test passes simply by not hanging
        assert isinstance(result, list)

    def test_malformed_json_in_ha_diff_does_not_crash(self):
        from app.snapshots import _diff_ha
        result = _diff_ha("{not valid json", "[also not valid")
        assert result == []

    def test_search_endpoint_with_unicode_emoji_query(self, client):
        resp = client.post("/search", json={
            "query": "🔥💀🎉 what is nitrogen 天気",
            "source": "kiwix"
        })
        assert resp.status_code == 200

    def test_search_endpoint_with_extremely_long_query(self, client):
        resp = client.post("/search", json={
            "query": "x" * 50000,
            "source": "kiwix"
        })
        assert resp.status_code == 200

    def test_negative_logs_limit_does_not_crash(self, client):
        resp = client.get("/logs?limit=-5")
        assert resp.status_code in (200, 422)

    def test_huge_logs_limit_does_not_crash(self, client):
        resp = client.get("/logs?limit=999999999")
        assert resp.status_code == 200

    def test_changes_endpoint_negative_hours(self, client):
        resp = client.get("/changes?hours=-100")
        assert resp.status_code == 200

    def test_changes_endpoint_huge_hours(self, client):
        resp = client.get("/changes?hours=999999")
        assert resp.status_code == 200


class TestConcurrency:
    """Tests for concurrent access to shared cache and routing cache state."""

    def test_concurrent_cache_clear_and_search_no_crash(self, client):
        errors = []

        def clear_repeatedly():
            for _ in range(20):
                try:
                    client.post("/cache/clear")
                except Exception as e:
                    errors.append(e)

        def search_repeatedly():
            for _ in range(20):
                try:
                    client.post("/search", json={"query": "what is nitrogen", "source": "kiwix"})
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=clear_repeatedly),
            threading.Thread(target=search_repeatedly),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []

    def test_concurrent_logs_clear_and_log_query_no_crash(self, client):
        from app.main import _log_query
        errors = []

        def clear_repeatedly():
            for _ in range(20):
                try:
                    client.post("/logs/clear")
                except Exception as e:
                    errors.append(e)

        def log_repeatedly():
            for _ in range(20):
                try:
                    _log_query("test", "auto", "kiwix", False, True, 10)
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=clear_repeatedly),
            threading.Thread(target=log_repeatedly),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []

    def test_concurrent_snapshot_writes_no_crash(self):
        import tempfile
        import os as os_module
        from unittest.mock import patch
        from app.snapshots import _store_snapshot, init_snapshot_db

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_db = f.name

        errors = []

        def store_repeatedly(source):
            for i in range(20):
                try:
                    with patch("app.snapshots.SNAPSHOT_DB", temp_db):
                        _store_snapshot(source, f"snapshot content {i}")
                except Exception as e:
                    errors.append(e)

        with patch("app.snapshots.SNAPSHOT_DB", temp_db):
            init_snapshot_db()

        threads = [
            threading.Thread(target=store_repeatedly, args=("uptime",)),
            threading.Thread(target=store_repeatedly, args=("forecast",)),
            threading.Thread(target=store_repeatedly, args=("news",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        os_module.unlink(temp_db)
        assert errors == []

    def test_concurrent_backup_requests_no_crash(self, client):
        errors = []

        def backup_repeatedly():
            for _ in range(5):
                try:
                    resp = client.get("/backup")
                    assert resp.status_code == 200
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=backup_repeatedly) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert errors == []
