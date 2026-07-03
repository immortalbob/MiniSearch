"""
Tests for app/sources/kiwix.py network-calling functions —
_fetch_catalog_page, get_books, refresh_catalog, _pick_books_with_llm,
_search_book, _fetch_article.

All HTTP calls are mocked. These complement test_kiwix.py, which covers
the pure scoring/stemming logic; this file covers the OPDS catalog parsing,
HTML scraping, and LLM book-selection dispatch.
"""
from unittest.mock import patch, MagicMock
import requests as req


SAMPLE_OPDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
  <entry>
    <title>Wikipedia</title>
    <summary>Free encyclopedia</summary>
    <link type="text/html" href="/content/wikipedia_en_all_maxi_2026-02"/>
  </entry>
  <entry>
    <title>Unix Stack Exchange</title>
    <summary>Q&amp;A for Unix users</summary>
    <link type="text/html" href="/content/unix.stackexchange.com_en_all_2026-02"/>
  </entry>
</feed>"""

EMPTY_OPDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:dc="http://purl.org/dc/terms/">
</feed>"""


class TestFetchCatalogPage:
    """Tests for _fetch_catalog_page OPDS XML parsing."""

    def _mock_xml_response(self, xml_content):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = xml_content.encode("utf-8")
        resp.raise_for_status.return_value = None
        return resp

    def test_parses_valid_opds_entries(self):
        from app.sources.kiwix import _fetch_catalog_page
        with patch("app.sources.kiwix._session.get", return_value=self._mock_xml_response(SAMPLE_OPDS_XML)):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        assert len(books) == 2

    def test_extracts_book_name_from_href(self):
        from app.sources.kiwix import _fetch_catalog_page
        with patch("app.sources.kiwix._session.get", return_value=self._mock_xml_response(SAMPLE_OPDS_XML)):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        names = [b["name"] for b in books]
        assert "wikipedia_en_all_maxi_2026-02" in names
        assert "unix.stackexchange.com_en_all_2026-02" in names

    def test_extracts_title_and_summary(self):
        from app.sources.kiwix import _fetch_catalog_page
        with patch("app.sources.kiwix._session.get", return_value=self._mock_xml_response(SAMPLE_OPDS_XML)):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        wiki = next(b for b in books if "wikipedia" in b["name"])
        assert wiki["title"] == "Wikipedia"
        assert wiki["summary"] == "Free encyclopedia"

    def test_empty_feed_returns_empty_list(self):
        from app.sources.kiwix import _fetch_catalog_page
        with patch("app.sources.kiwix._session.get", return_value=self._mock_xml_response(EMPTY_OPDS_XML)):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        assert books == []

    def test_returns_empty_on_connection_error(self):
        from app.sources.kiwix import _fetch_catalog_page
        with patch("app.sources.kiwix._session.get", side_effect=req.exceptions.ConnectionError()):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        assert books == []

    def test_returns_empty_on_malformed_xml(self):
        from app.sources.kiwix import _fetch_catalog_page
        with patch("app.sources.kiwix._session.get", return_value=self._mock_xml_response("not xml at all <<<")):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        assert books == []

    def test_entry_without_html_link_skipped(self):
        from app.sources.kiwix import _fetch_catalog_page
        xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>No Link Book</title></entry>
</feed>"""
        with patch("app.sources.kiwix._session.get", return_value=self._mock_xml_response(xml)):
            books = _fetch_catalog_page("http://kiwix:8080/catalog/v2/entries")
        assert books == []


class TestGetBooks:
    """Tests for get_books() pagination and caching."""

    def setup_method(self):
        import app.sources.kiwix as kiwix_module
        self._original_cache = list(kiwix_module._book_cache)
        kiwix_module._book_cache = []

    def teardown_method(self):
        import app.sources.kiwix as kiwix_module
        kiwix_module._book_cache = self._original_cache

    def test_returns_cached_books_without_fetching(self):
        import app.sources.kiwix as kiwix_module
        kiwix_module._book_cache = [{"name": "cached_book", "title": "T", "summary": ""}]
        with patch("app.sources.kiwix._fetch_catalog_page") as mock_fetch:
            books = kiwix_module.get_books()
        assert not mock_fetch.called
        assert books == [{"name": "cached_book", "title": "T", "summary": ""}]

    def test_fetches_and_caches_when_empty(self):
        from app.sources.kiwix import get_books
        page1 = [{"name": "b1", "title": "T1", "summary": ""}] * 10  # full page
        page2 = [{"name": "b2", "title": "T2", "summary": ""}]  # partial page, ends pagination
        with patch("app.sources.kiwix._fetch_catalog_page", side_effect=[page1, page2]):
            books = get_books()
        assert len(books) == 11

    def test_stops_pagination_on_partial_page(self):
        from app.sources.kiwix import get_books
        partial_page = [{"name": "only_book", "title": "T", "summary": ""}]
        with patch("app.sources.kiwix._fetch_catalog_page", return_value=partial_page) as mock_fetch:
            get_books()
        # Should only call once since first page was already partial (< page_size)
        assert mock_fetch.call_count == 1

    def test_no_books_found_returns_empty(self):
        from app.sources.kiwix import get_books
        with patch("app.sources.kiwix._fetch_catalog_page", return_value=[]):
            books = get_books()
        assert books == []


class TestRefreshCatalog:
    """Tests for refresh_catalog() forcing a fresh fetch."""

    def setup_method(self):
        import app.sources.kiwix as kiwix_module
        self._original_cache = list(kiwix_module._book_cache)

    def teardown_method(self):
        import app.sources.kiwix as kiwix_module
        kiwix_module._book_cache = self._original_cache

    def test_clears_cache_before_fetching(self):
        import app.sources.kiwix as kiwix_module
        kiwix_module._book_cache = [{"name": "old_book", "title": "Old", "summary": ""}]
        new_books = [{"name": "new_book", "title": "New", "summary": ""}]
        with patch("app.sources.kiwix._fetch_catalog_page", side_effect=[new_books, []]):
            result = kiwix_module.refresh_catalog()
        assert result == new_books
        assert "old_book" not in [b["name"] for b in result]


class TestPickBooksWithLLM:
    """Tests for _pick_books_with_llm() book selection dispatch."""

    def _books(self):
        return [
            {"name": "wikipedia_en_all_maxi_2026-02", "title": "Wikipedia", "summary": "Encyclopedia"},
            {"name": "unix.stackexchange.com_en_all_2026-02", "title": "Unix SE", "summary": "Q&A"},
        ]

    def test_empty_books_returns_empty(self):
        from app.sources.kiwix import _pick_books_with_llm
        result = _pick_books_with_llm("test query", [])
        assert result == []

    def test_uses_routing_cache_when_valid(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns:
            mock_get_routing = MagicMock(return_value="wikipedia_en_all_maxi_2026-02")
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("what is nitrogen", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_ignores_cached_book_not_in_current_list(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=False):
            mock_get_routing = MagicMock(return_value="some_deleted_book")
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books())
        # Cached book no longer exists, falls through to non-LLM fallback (wikipedia)
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_falls_back_to_wikipedia_when_llm_not_configured(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=False):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_falls_back_to_first_book_when_no_wikipedia_and_not_configured(self):
        from app.sources.kiwix import _pick_books_with_llm
        books = [{"name": "some_other_book", "title": "X", "summary": ""}]
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=False):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", books)
        assert result == ["some_other_book"]

    def test_llm_picks_exact_book_name(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="wikipedia_en_all_maxi_2026-02"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("what is nitrogen", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_llm_fuzzy_substring_match(self):
        """LLM sometimes returns a partial/truncated book name — should fuzzy match."""
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="wikipedia"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("what is nitrogen", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_llm_returns_multiple_books_respects_max(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="wikipedia_en_all_maxi_2026-02, unix.stackexchange.com_en_all_2026-02"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books(), max_books=1)
        assert len(result) == 1

    def test_llm_invalid_response_falls_back(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="completely_unrelated_garbage_xyz"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books())
        # Falls back to Wikipedia-first
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_llm_none_response_falls_back(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value=None):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_llm_empty_string_response_falls_back(self):
        """Regression test — empty string used to incorrectly substring-match
        whatever book name happened to come first in unordered set iteration,
        rather than falling through to the Wikipedia-first fallback."""
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value=""):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_llm_whitespace_only_response_falls_back(self):
        from app.sources.kiwix import _pick_books_with_llm
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="   "):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books())
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_ambiguous_fuzzy_match_is_deterministic_across_runs(self):
        """Regression test for a real bug found via a deliberate
        "bulletproofing" pass: the fuzzy-match fallback iterated over a
        set(), whose iteration order is not guaranteed stable across
        different process runs (depends on hash randomization, which
        this project never pins via PYTHONHASHSEED). An LLM response
        ambiguous enough to fuzzy-match more than one real book (e.g. a
        truncated "wikipedia_en_all" matching both "...maxi" and
        "...nopic" variants) could resolve to a DIFFERENT real book
        purely due to container restart timing. Confirms the fix is now
        deterministic across multiple independent calls."""
        from app.sources.kiwix import _pick_books_with_llm
        books = [
            {"name": "wikipedia_en_all_maxi_2026-02", "title": "W1", "summary": ""},
            {"name": "wikipedia_en_all_nopic_2026-02", "title": "W2", "summary": ""},
        ]
        results = []
        for _ in range(5):
            with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
                 patch("app.llm.is_configured", return_value=True), \
                 patch("app.llm.complete", return_value="wikipedia_en_all"):
                mock_fns.return_value = (MagicMock(return_value=None), MagicMock())
                results.append(_pick_books_with_llm("test", books))
        assert all(r == results[0] for r in results)
        assert results[0] == ["wikipedia_en_all_maxi_2026-02"]  # sorted() picks "maxi" before "nopic"


class TestFallbackBookChoice:
    """Tests for _fallback_book_choice() — extracted from
    _pick_books_with_llm() during a deliberate complexity-reduction
    investigation (the same discipline applied to app/router.py's
    route_with_source() and app/sources/home_assistant.py's search()
    earlier this release cycle). Unlike those two, which surfaced real
    behavioral bugs once compared carefully, this was a genuine, exact,
    byte-for-byte duplicate with no hidden divergence — the same "pick
    Wikipedia if available, else the first book" logic was used for both
    the 'LLM not configured' case and the 'LLM returned nothing usable'
    case in _pick_books_with_llm(), confirmed identical before
    extracting."""

    def test_picks_wikipedia_when_present(self):
        from app.sources.kiwix import _fallback_book_choice
        books = [
            {"name": "unix.stackexchange.com_en_all_2026-02", "title": "Unix SE", "summary": ""},
            {"name": "wikipedia_en_all_maxi_2026-02", "title": "Wikipedia", "summary": ""},
        ]
        mock_set_routing = MagicMock()
        result = _fallback_book_choice(books, "test_key", mock_set_routing)
        assert result == ["wikipedia_en_all_maxi_2026-02"]

    def test_picks_first_book_when_no_wikipedia(self):
        from app.sources.kiwix import _fallback_book_choice
        books = [
            {"name": "some_book", "title": "X", "summary": ""},
            {"name": "another_book", "title": "Y", "summary": ""},
        ]
        mock_set_routing = MagicMock()
        result = _fallback_book_choice(books, "test_key", mock_set_routing)
        assert result == ["some_book"]

    def test_empty_books_returns_empty_without_caching(self):
        from app.sources.kiwix import _fallback_book_choice
        mock_set_routing = MagicMock()
        result = _fallback_book_choice([], "test_key", mock_set_routing)
        assert result == []
        mock_set_routing.assert_not_called()

    def test_caches_the_decision_with_the_given_key(self):
        from app.sources.kiwix import _fallback_book_choice
        books = [{"name": "wikipedia_en_all_maxi_2026-02", "title": "Wikipedia", "summary": ""}]
        mock_set_routing = MagicMock()
        _fallback_book_choice(books, "my_cache_key", mock_set_routing)
        mock_set_routing.assert_called_once_with("my_cache_key", "wikipedia_en_all_maxi_2026-02")

    def test_both_callers_in_pick_books_with_llm_genuinely_share_this_function(self):
        """Confirms the unification is real, not coincidental — patches
        _fallback_book_choice itself and verifies it's actually invoked
        for both the 'not configured' and 'LLM returned nothing usable'
        scenarios, rather than just asserting on the final returned
        value (which could theoretically match by coincidence if the two
        call sites had silently diverged again in the future)."""
        from app.sources.kiwix import _pick_books_with_llm
        books = [{"name": "wikipedia_en_all_maxi_2026-02", "title": "Wikipedia", "summary": ""}]

        with patch("app.sources.kiwix._fallback_book_choice", return_value=["wikipedia_en_all_maxi_2026-02"]) as mock_fallback, \
             patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=False):
            mock_fns.return_value = (MagicMock(return_value=None), MagicMock())
            _pick_books_with_llm("test", books)
        assert mock_fallback.call_count == 1

        with patch("app.sources.kiwix._fallback_book_choice", return_value=["wikipedia_en_all_maxi_2026-02"]) as mock_fallback, \
             patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="not_a_real_book_name"):
            mock_fns.return_value = (MagicMock(return_value=None), MagicMock())
            _pick_books_with_llm("test", books)
        assert mock_fallback.call_count == 1


class TestShouldDisambiguate:
    """Tests for _should_disambiguate() eligibility checks."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.llm_url
        self._orig_model = settings.llm_model
        settings.llm_url = "http://ollama:11434"
        settings.llm_model = "qwen3:8b"

    def teardown_method(self):
        from app.config import settings
        settings.llm_url = self._orig_url
        settings.llm_model = self._orig_model

    def test_false_when_llm_not_configured(self):
        from app.sources.kiwix import _should_disambiguate
        from app.config import settings
        settings.llm_url = ""
        result = _should_disambiguate("what are galaxies", "galaxy", ["wikipedia_en_all_maxi_2026-02"])
        assert result is False

    def test_false_for_non_definitional_query(self):
        from app.sources.kiwix import _should_disambiguate
        result = _should_disambiguate("galaxy power factor correction", "galaxy", ["wikipedia_en_all_maxi_2026-02"])
        assert result is False

    def test_false_when_not_wikipedia(self):
        from app.sources.kiwix import _should_disambiguate
        result = _should_disambiguate("what are galaxies", "galaxy", ["unix.stackexchange.com_en_all_2026-02"])
        assert result is False

    def test_false_for_multi_word_term(self):
        from app.sources.kiwix import _should_disambiguate
        result = _should_disambiguate("what is the capital of france", "capit franc", ["wikipedia_en_all_maxi_2026-02"])
        assert result is False

    def test_true_for_eligible_query(self):
        from app.sources.kiwix import _should_disambiguate
        result = _should_disambiguate("what are galaxies", "galaxy", ["wikipedia_en_all_maxi_2026-02"])
        assert result is True


class TestGetDisambiguationCandidates:
    """Tests for _get_disambiguation_candidates() — multi-candidate generation."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.llm_url
        self._orig_model = settings.llm_model
        settings.llm_url = "http://ollama:11434"
        settings.llm_model = "qwen3:8b"

    def teardown_method(self):
        from app.config import settings
        settings.llm_url = self._orig_url
        settings.llm_model = self._orig_model

    def test_uses_routing_cache_when_available(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns:
            mock_get_routing = MagicMock(return_value="galaxy astronomy|galaxy spiral|galaxy")
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert result == ["galaxy astronomy", "galaxy spiral", "galaxy"]

    def test_parses_pipe_separated_response(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="galaxy astronomy|galaxy spiral|galaxy"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert len(result) == 3
        assert "galaxy" in result

    def test_always_includes_bare_original_term_as_fallback(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="galaxy astronomy|galaxy spiral"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert "galaxy" in result

    def test_filters_candidates_missing_original_word(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="samsung electronics|galaxy astronomy|galaxy"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert "samsung electronics" not in result

    def test_single_character_original_word_uses_word_boundary_not_bare_substring(self):
        """Regression test for a real bug found via a deliberate
        "bulletproofing" pass: a bare substring check is far too loose
        for a one-character original word, since almost any English
        phrase coincidentally contains a single letter somewhere — the
        filter would provide meaningfully less protection for short
        search terms (now genuinely reachable after fixing
        _build_search_terms() to stop dropping single alphanumeric
        characters) than it does for longer ones. Confirms candidates
        that merely happen to contain the letter "c" as a substring
        (not as a standalone word) are correctly rejected, while a
        candidate where "c" genuinely appears as its own word is
        correctly kept."""
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="computer science|c programming|coding"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what is c", "c")
        assert "computer science" not in result
        assert "coding" not in result
        assert "c programming" in result

    def test_falls_back_to_original_term_when_all_candidates_invalid(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="completely unrelated garbage"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert result == ["galaxy"]

    def test_falls_back_when_llm_returns_none(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value=None):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert result == ["galaxy"]

    def test_genuine_llm_failure_is_not_cached(self):
        """Regression test for a real, significant bug found via a
        deliberate complexity-investigation pass — the same pattern
        already found and fixed in _llm_pick_fusion_sources() and
        _llm_detect(): caching the bare-fallback result under the same
        key a genuine success would use means a single transient LLM
        hiccup permanently locks the query into the unhelpful, bare
        fallback (just the original ambiguous word, no real
        disambiguation at all) for the full routing cache TTL. When the
        LLM call itself genuinely fails (complete() returns None/empty,
        not a real response), the result must NOT be cached, so the
        next identical query gets a fresh, real chance at success."""
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value=None):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            _get_disambiguation_candidates("what are galaxies", "galaxy")
        mock_set_routing.assert_not_called()

    def test_genuine_response_that_fails_sanity_filter_is_still_cached(self):
        """A deliberate, real distinction from the fix above: when the
        LLM genuinely RESPONDS (raw is truthy) but none of its phrases
        survive the sanity filter (e.g. none contain the original word
        at all), that's a substantive answer that simply wasn't usable
        — not a transient hiccup. The same prompt would likely produce
        a similarly unusable answer again, so this outcome IS still
        cached, avoiding a wasteful repeat LLM call for a query the
        model has already genuinely struggled with."""
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="completely unrelated garbage"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            _get_disambiguation_candidates("what are galaxies", "galaxy")
        mock_set_routing.assert_called_once()

    def test_caps_at_three_candidates(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="galaxy a|galaxy b|galaxy c|galaxy d|galaxy e"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert len(result) <= 3

    def test_rejects_overly_long_individual_candidates(self):
        from app.sources.kiwix import _get_disambiguation_candidates
        from unittest.mock import patch, MagicMock
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.complete", return_value="galaxy this is way too many words|galaxy"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _get_disambiguation_candidates("what are galaxies", "galaxy")
        assert "galaxy this is way too many words" not in result


class TestSearchMultiCandidateScoring:
    """Integration tests confirming search() searches multiple candidates
    and scoring picks the best result across all of them — not just
    trusting whichever candidate the LLM happened to suggest first."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.llm_url
        self._orig_model = settings.llm_model
        settings.llm_url = "http://ollama:11434"
        settings.llm_model = "qwen3:8b"

    def teardown_method(self):
        from app.config import settings
        settings.llm_url = self._orig_url
        settings.llm_model = self._orig_model

    def test_does_not_disambiguate_long_multi_word_technical_query(self):
        """Regression test — the real bug found via real usage. search()
        was checking disambiguation eligibility against primary_term (the
        already-reduced single longest word) instead of the full
        search_terms phrase. Since primary_term is ALWAYS exactly one word
        by construction, the eligibility check was trivially always true —
        meaning even genuinely long, specific, unambiguous queries like
        "raspberry pi gpio permission errors in python" (5+ real content
        words) still triggered single-word disambiguation on "permission"
        alone, discarding all the surrounding context that made the query
        unambiguous, and landing on an unrelated article (macOS disk
        permissions instead of Raspberry Pi GPIO)."""
        from app.sources import kiwix
        from unittest.mock import patch

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_get_disambiguation_candidates") as mock_disambig, \
             patch.object(kiwix, "_search_book", return_value=[{"title": "GPIO Permission Fix", "excerpt": "raspberry pi gpio permission", "url": "http://x/gpio", "book": "wikipedia_en_all_maxi_2026-02"}]), \
             patch.object(kiwix, "_fetch_article", return_value="content"):
            kiwix.search("remind me whats the deal with raspberry pi gpio permission errors in python")

        # Disambiguation candidate generation should never be called for
        # a query with this many real content words, regardless of how
        # the LLM-selected book or definitional-phrase detection resolves
        assert not mock_disambig.called

    def test_still_disambiguates_genuinely_short_query(self):
        """Confirm the fix didn't break the original working case — a
        genuinely short, single-word-after-stemming query should still
        trigger disambiguation exactly as before."""
        from app.sources import kiwix
        from unittest.mock import patch

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_get_disambiguation_candidates", return_value=["galaxy astronomy", "galaxy"]) as mock_disambig, \
             patch.object(kiwix, "_search_book", return_value=[{"title": "Galaxy", "excerpt": "a galaxy is a system of stars", "url": "http://x/galaxy", "book": "wikipedia_en_all_maxi_2026-02"}]), \
             patch.object(kiwix, "_fetch_article", return_value="content"):
            kiwix.search("what are galaxies")

        assert mock_disambig.called

    def test_searches_every_candidate_term(self):
        from app.sources import kiwix
        from unittest.mock import patch

        searched_terms = []

        def fake_search_book(term, book, limit=None):
            searched_terms.append(term)
            return []

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_get_disambiguation_candidates", return_value=["galaxy astronomy", "galaxy spiral", "galaxy"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book):
            kiwix.search("what are galaxies")

        assert "galaxy astronomy" in searched_terms
        assert "galaxy spiral" in searched_terms
        assert "galaxy" in searched_terms

    def test_picks_best_scoring_result_across_all_candidates(self):
        from app.sources import kiwix
        from unittest.mock import patch

        def fake_search_book(term, book, limit=None):
            if term == "galaxy astronomy":
                return [{"title": "Radio Galaxy Zoo", "excerpt": "citizen science project", "url": "http://kiwix/rgz", "book": book}]
            elif term == "galaxy":
                return [{"title": "Galaxy", "excerpt": "a galaxy is a system of stars", "url": "http://kiwix/galaxy", "book": book}]
            return []

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_get_disambiguation_candidates", return_value=["galaxy astronomy", "galaxy"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", return_value="A galaxy is a gravitationally bound system."):
            result = kiwix.search("what are galaxies")

        # The plain "Galaxy" article should score higher than "Radio Galaxy Zoo"
        # for the query "what are galaxies" due to the exact stemmed title match
        assert "Galaxy" in result
        assert "Radio Galaxy Zoo" not in result

    def test_deduplicates_results_across_candidates(self):
        from app.sources import kiwix
        from unittest.mock import patch

        call_count = {"n": 0}

        def fake_search_book(term, book, limit=None):
            call_count["n"] += 1
            # Same URL returned for every candidate term
            return [{"title": "Galaxy", "excerpt": "test", "url": "http://kiwix/galaxy", "book": book}]

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_get_disambiguation_candidates", return_value=["galaxy astronomy", "galaxy spiral", "galaxy"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", return_value="content"):
            result = kiwix.search("what are galaxies")

        # All 3 candidates searched, but the duplicate URL only counted once
        assert call_count["n"] == 3
        assert result.count("# Galaxy") <= 1

    def test_non_eligible_query_only_searches_once(self):
        from app.sources import kiwix
        from unittest.mock import patch

        call_count = {"n": 0}

        def fake_search_book(term, book, limit=None):
            call_count["n"] += 1
            return [{"title": "Result", "excerpt": "test", "url": "http://kiwix/r", "book": book}]

        with patch.object(kiwix, "get_books", return_value=[{"name": "unix.stackexchange.com_en_all_2026-02", "title": "U", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["unix.stackexchange.com_en_all_2026-02"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", return_value="content"):
            kiwix.search("how do I configure systemd")

        # Non-Wikipedia, non-definitional or multi-word query — no disambiguation, single search
        assert call_count["n"] == 1


class TestMultiBookFusionNegativeScoreGuard:
    """Regression tests for a real logic flaw found via a deliberate
    complexity-investigation pass: a result can legitimately score
    negative (a list/index article nets -2 or -7 after its own partial
    offset, with zero other matches). If the OVERALL best result across
    every selected book happens to be negative, "score >= top_score *
    0.5" silently breaks down for a negative top_score (e.g. -10 >= -5
    is False), meaning even the top result itself wouldn't pass its own
    bar. This never produced a wrong final answer — when a genuinely
    good result exists anywhere, it becomes `top` by construction, so
    the bug could only manifest when every candidate was already poor,
    in which case falling through to the single best (still poor) result
    was always the correct outcome anyway. The explicit `top_score > 0`
    guard makes that intent clear and correct by construction, rather
    than relying on the threshold math accidentally breaking down to
    reach the right answer."""

    def test_negative_top_score_skips_multi_book_comparison_without_crashing(self):
        from app.sources import kiwix

        def fake_search_book(term, book, limit=None):
            # Both books return only genuinely poor, list-article-style
            # results with zero real overlap with the query
            return [{
                "title": f"List of things in {book}",
                "excerpt": "completely unrelated filler content",
                "url": f"http://example.com/{book}",
                "book": book,
            }]

        with patch.object(kiwix, "get_books", return_value=[
                {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""},
                {"name": "unix.stackexchange.com_en_all_2026-02", "title": "U", "summary": ""},
             ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=[
                "wikipedia_en_all_maxi_2026-02", "unix.stackexchange.com_en_all_2026-02"
             ]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", return_value="some content"):
            result = kiwix.search("xyzzyplugh nonexistent gibberish query")  # must not raise

        assert "List of things in" in result  # falls through to single-result path

    def test_genuinely_competitive_positive_scores_still_trigger_fusion(self):
        """Confirms the new top_score > 0 guard didn't accidentally
        break the real, intended multi-book fusion case — two genuinely
        relevant, positively-scored results from different books should
        still trigger fusion exactly as before."""
        from app.sources import kiwix

        def fake_search_book(term, book, limit=None):
            return [{
                "title": "python",
                "excerpt": "python programming language gpio raspberry pi",
                "url": f"http://example.com/{book}",
                "book": book,
            }]

        with patch.object(kiwix, "get_books", return_value=[
                {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""},
                {"name": "raspberrypi.stackexchange.com_en_all", "title": "RPI", "summary": ""},
             ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=[
                "wikipedia_en_all_maxi_2026-02", "raspberrypi.stackexchange.com_en_all"
             ]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", return_value="real article content"):
            result = kiwix.search("python gpio raspberry pi")

        assert "WIKIPEDIA" in result.upper() or "RASPBERRYPI" in result.upper()


class TestDisambiguationOnlyAppliesToWikipediaBook:
    """Regression tests for a real, genuine inefficiency found via the
    same investigation: disambiguation candidates are specifically
    Wikipedia-oriented phrasings, but the search loop previously applied
    them to EVERY selected book when multiple books were chosen —
    including a non-Wikipedia secondary book the mechanism was never
    designed for. Never produced a wrong answer (scoring still picks
    the genuine best result regardless of which term found it), but
    meant real, unnecessary extra Kiwix requests against a book that had
    no business being searched with Wikipedia-disambiguation phrasings."""

    def test_non_wikipedia_book_searched_with_plain_terms_not_disambiguation_candidates(self):
        from app.sources import kiwix

        calls_per_book = {}

        def fake_search_book(term, book, limit=None):
            calls_per_book.setdefault(book, []).append(term)
            return []

        with patch.object(kiwix, "get_books", return_value=[
                {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""},
                {"name": "raspberrypi.stackexchange.com_en_all", "title": "RPI", "summary": ""},
             ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=[
                "wikipedia_en_all_maxi_2026-02", "raspberrypi.stackexchange.com_en_all"
             ]), \
             patch.object(kiwix, "_should_disambiguate", return_value=True), \
             patch.object(kiwix, "_get_disambiguation_candidates",
                          return_value=["galaxy astronomy", "galaxy spiral", "galaxy"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix.settings, "llm_url", "http://fake"), \
             patch.object(kiwix.settings, "llm_model", "fake-model"):
            kiwix.search("what is galaxy")

        # The Wikipedia book searches the PLAIN term first, then the
        # candidates (minus any candidate identical to the plain term —
        # "galaxy" appears in both lists here and must not be searched
        # twice). The plain term always participating is the v3.54.2
        # pooling fix: candidates exist to ADD senses, never to replace
        # the bare term, whose article scoring can only rank if it was
        # actually fetched. The non-Wikipedia book still gets only the
        # plain terms — disambiguation candidates are Wikipedia-oriented
        # phrasings it has no business searching with.
        assert calls_per_book["wikipedia_en_all_maxi_2026-02"] == ["galaxy", "galaxy astronomy", "galaxy spiral"]
        assert calls_per_book["raspberrypi.stackexchange.com_en_all"] == ["galaxy"]


class TestConfigurableMaxBooks:
    """Tests for settings-backed kiwix_max_books default."""

    def setup_method(self):
        from app.config import settings
        self._orig_max_books = settings.kiwix_max_books

    def teardown_method(self):
        from app.config import settings
        settings.kiwix_max_books = self._orig_max_books

    def _books(self, n):
        return [
            {"name": f"book_{i}", "title": f"Book {i}", "summary": ""}
            for i in range(n)
        ]

    def test_pick_books_uses_settings_default(self):
        from app.sources.kiwix import _pick_books_with_llm
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.kiwix_max_books = 1
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=False):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books(5))
        assert len(result) <= 1

    def test_pick_books_respects_higher_configured_max(self):
        from app.sources.kiwix import _pick_books_with_llm
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.kiwix_max_books = 3
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="book_0, book_1, book_2, book_3"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books(5))
        assert len(result) == 3

    def test_explicit_max_books_overrides_settings(self):
        from app.sources.kiwix import _pick_books_with_llm
        from app.config import settings
        from unittest.mock import patch, MagicMock
        settings.kiwix_max_books = 5
        with patch("app.sources.kiwix._get_routing_fns") as mock_fns, \
             patch("app.llm.is_configured", return_value=True), \
             patch("app.llm.complete", return_value="book_0, book_1, book_2, book_3, book_4"):
            mock_get_routing = MagicMock(return_value=None)
            mock_set_routing = MagicMock()
            mock_fns.return_value = (mock_get_routing, mock_set_routing)
            result = _pick_books_with_llm("test", self._books(5), max_books=2)
        assert len(result) == 2

    def test_search_passes_no_explicit_max_books(self):
        """search() should let _pick_books_with_llm fall through to the
        settings default rather than hardcoding max_books=2 at the call site."""
        from app.sources import kiwix
        from unittest.mock import patch

        captured_kwargs = {}

        def fake_pick_books(query, books, **kwargs):
            captured_kwargs.update(kwargs)
            return ["wikipedia_en_all_maxi_2026-02"]

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", side_effect=fake_pick_books), \
             patch.object(kiwix, "_search_book", return_value=[]):
            kiwix.search("what is nitrogen")

        assert "max_books" not in captured_kwargs


class TestFuseMultiBookResults:
    """Tests for _fuse_multi_book_results() — merging best result per book."""

    def test_merges_multiple_books(self):
        from app.sources.kiwix import _fuse_multi_book_results
        from unittest.mock import patch
        relevant = [
            ("python.org_en_all", {"title": "GPIO Module", "url": "http://kiwix/python/gpio", "book": "python.org_en_all"}, 30),
            ("raspberrypi.stackexchange.com_en_all", {"title": "GPIO pinout", "url": "http://kiwix/rpi/gpio", "book": "raspberrypi.stackexchange.com_en_all"}, 28),
        ]
        with patch("app.sources.kiwix._fetch_article", side_effect=["Python GPIO docs content.", "Raspberry Pi GPIO pinout content."]):
            result = _fuse_multi_book_results(relevant)
        assert "GPIO Module" in result
        assert "GPIO pinout" in result
        assert "PYTHON.ORG_EN_ALL" in result
        assert "RASPBERRYPI.STACKEXCHANGE.COM_EN_ALL" in result

    def test_sorts_by_score_descending(self):
        from app.sources.kiwix import _fuse_multi_book_results
        from unittest.mock import patch
        relevant = [
            ("book_low", {"title": "Lower Result", "url": "http://kiwix/low", "book": "book_low"}, 20),
            ("book_high", {"title": "Higher Result", "url": "http://kiwix/high", "book": "book_high"}, 35),
        ]
        with patch("app.sources.kiwix._fetch_article", side_effect=["Lower content.", "Higher content."]):
            result = _fuse_multi_book_results(relevant)
        # Higher-scored book should appear first
        assert result.index("Higher Result") < result.index("Lower Result")

    def test_skips_books_with_failed_fetch(self):
        from app.sources.kiwix import _fuse_multi_book_results
        from unittest.mock import patch
        relevant = [
            ("book_a", {"title": "Result A", "url": "http://kiwix/a", "book": "book_a"}, 30),
            ("book_b", {"title": "Result B", "url": "http://kiwix/b", "book": "book_b"}, 28),
        ]
        with patch("app.sources.kiwix._fetch_article", side_effect=["Content A.", ""]):
            result = _fuse_multi_book_results(relevant)
        assert "Result A" in result
        assert "Result B" not in result

    def test_returns_plain_format_when_only_one_fetch_succeeds(self):
        from app.sources.kiwix import _fuse_multi_book_results
        from unittest.mock import patch
        relevant = [
            ("book_a", {"title": "Result A", "url": "http://kiwix/a", "book": "book_a"}, 30),
            ("book_b", {"title": "Result B", "url": "http://kiwix/b", "book": "book_b"}, 28),
        ]
        with patch("app.sources.kiwix._fetch_article", side_effect=["Content A.", ""]):
            result = _fuse_multi_book_results(relevant)
        # Single surviving section should not have a [BOOK] header since fusion didn't really happen
        assert "[BOOK_A]" not in result
        assert "Result A" in result

    def test_returns_error_when_all_fetches_fail(self):
        from app.sources.kiwix import _fuse_multi_book_results
        from unittest.mock import patch
        relevant = [
            ("book_a", {"title": "Result A", "url": "http://kiwix/a", "book": "book_a"}, 30),
        ]
        with patch("app.sources.kiwix._fetch_article", return_value=""):
            result = _fuse_multi_book_results(relevant)
        assert "could not fetch" in result.lower()

    def test_truncates_long_articles(self):
        from app.sources.kiwix import _fuse_multi_book_results
        from app.config import settings
        from unittest.mock import patch
        original_max = settings.fusion_max_chars_per_source
        settings.fusion_max_chars_per_source = 100
        relevant = [
            ("book_a", {"title": "Result A", "url": "http://kiwix/a", "book": "book_a"}, 30),
            ("book_b", {"title": "Result B", "url": "http://kiwix/b", "book": "book_b"}, 28),
        ]
        with patch("app.sources.kiwix._fetch_article", side_effect=["x" * 1000, "y" * 1000]):
            result = _fuse_multi_book_results(relevant)
        settings.fusion_max_chars_per_source = original_max
        # Each section should be truncated, not the full 1000 chars
        assert result.count("x") < 1000


class TestSearchMultiBookFusionIntegration:
    """Integration tests confirming search() triggers multi-book fusion
    only when multiple books have genuinely competitive relevance scores."""

    def setup_method(self):
        from app.config import settings
        self._orig_url = settings.llm_url
        self._orig_model = settings.llm_model
        settings.llm_url = "http://ollama:11434"
        settings.llm_model = "qwen3:8b"

    def teardown_method(self):
        from app.config import settings
        settings.llm_url = self._orig_url
        settings.llm_model = self._orig_model

    def test_fuses_when_multiple_books_competitive(self):
        from app.sources import kiwix
        from unittest.mock import patch

        def fake_search_book(term, book, limit=None):
            if book == "python.org_en_all":
                return [{"title": "Python GPIO", "excerpt": "gpio python raspberry pi", "url": "http://kiwix/py", "book": book}]
            elif book == "raspberrypi.stackexchange.com_en_all":
                return [{"title": "GPIO pinout", "excerpt": "gpio raspberry pi python", "url": "http://kiwix/rpi", "book": book}]
            return []

        with patch.object(kiwix, "get_books", return_value=[
                {"name": "python.org_en_all", "title": "P", "summary": ""},
                {"name": "raspberrypi.stackexchange.com_en_all", "title": "R", "summary": ""},
            ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["python.org_en_all", "raspberrypi.stackexchange.com_en_all"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", side_effect=["Python content.", "RPi content."]):
            result = kiwix.search("python raspberry pi gpio not working")

        assert "Python GPIO" in result
        assert "GPIO pinout" in result

    def test_does_not_fuse_when_second_book_irrelevant(self):
        from app.sources import kiwix
        from unittest.mock import patch

        def fake_search_book(term, book, limit=None):
            if book == "wikipedia_en_all_maxi_2026-02":
                return [{"title": "Nitrogen", "excerpt": "nitrogen is a chemical element nitrogen nitrogen", "url": "http://kiwix/n2", "book": book}]
            elif book == "unrelated_book":
                return [{"title": "Unrelated Page", "excerpt": "", "url": "http://kiwix/u", "book": book}]
            return []

        with patch.object(kiwix, "get_books", return_value=[
                {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""},
                {"name": "unrelated_book", "title": "U", "summary": ""},
            ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02", "unrelated_book"]), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", return_value="Nitrogen content."):
            result = kiwix.search("what is nitrogen")

        # Should return single best result, not fuse in the irrelevant book
        assert "Nitrogen" in result
        assert "[UNRELATED_BOOK]" not in result

    def test_single_book_never_triggers_fusion_path(self):
        from app.sources import kiwix
        from unittest.mock import patch

        with patch.object(kiwix, "get_books", return_value=[{"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_search_book", return_value=[{"title": "Nitrogen", "excerpt": "test", "url": "http://kiwix/n2", "book": "wikipedia_en_all_maxi_2026-02"}]), \
             patch.object(kiwix, "_fuse_multi_book_results") as mock_fuse, \
             patch.object(kiwix, "_fetch_article", return_value="content"):
            kiwix.search("what is nitrogen")

        assert not mock_fuse.called


class TestSearchBook:
    """Tests for _search_book() HTML scraping of Kiwix search results."""

    def setup_method(self):
        from app.config import settings
        self._original_url = settings.kiwix_url
        settings.kiwix_url = "http://kiwix:8080"

    def teardown_method(self):
        from app.config import settings
        settings.kiwix_url = self._original_url

    def _mock_html_response(self, html):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = html.encode("utf-8")
        resp.raise_for_status.return_value = None
        return resp

    def test_parses_results_with_title_and_excerpt(self):
        from app.sources.kiwix import _search_book
        html = """
        <div class="results">
          <li><a href="/viewer#wikipedia/A/Nitrogen">Nitrogen</a><cite>chemical element</cite></li>
        </div>
        """
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            results = _search_book("nitrogen", "wikipedia_en_all_maxi_2026-02")
        assert len(results) == 1
        assert results[0]["title"] == "Nitrogen"
        assert results[0]["excerpt"] == "chemical element"

    def test_no_results_div_returns_empty(self):
        from app.sources.kiwix import _search_book
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response("<html><body>nothing</body></html>")):
            results = _search_book("test", "wikipedia_en_all_maxi_2026-02")
        assert results == []

    def test_excludes_stack_exchange_tag_pages(self):
        from app.sources.kiwix import _search_book
        html = """
        <div class="results">
          <li><a href="/viewer#se/questions/tagged/python">Python tag</a></li>
          <li><a href="/viewer#se/A/Real-Question">Real Question</a><cite>excerpt</cite></li>
        </div>
        """
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            results = _search_book("python", "stackoverflow")
        assert len(results) == 1
        assert results[0]["title"] == "Real Question"

    def test_handles_missing_cite_tag(self):
        from app.sources.kiwix import _search_book
        html = """
        <div class="results">
          <li><a href="/viewer#wikipedia/A/Test">Test</a></li>
        </div>
        """
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            results = _search_book("test", "wikipedia_en_all_maxi_2026-02")
        assert results[0]["excerpt"] == ""

    def test_returns_empty_on_connection_error(self):
        from app.sources.kiwix import _search_book
        with patch("app.sources.kiwix._session.get", side_effect=req.exceptions.ConnectionError()):
            results = _search_book("test", "wikipedia_en_all_maxi_2026-02")
        assert results == []

    def test_uses_configured_search_limit_by_default(self):
        from app.sources.kiwix import _search_book
        from app.config import settings
        from unittest.mock import patch
        html = '<div class="results"><li><a href="/viewer#wikipedia/A/Test">Test</a></li></div>'
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)) as mock_get:
            _search_book("test", "wikipedia_en_all_maxi_2026-02")
        params = mock_get.call_args.kwargs["params"]
        assert params["limit"] == settings.kiwix_search_limit

    def test_explicit_limit_overrides_settings_default(self):
        from app.sources.kiwix import _search_book
        from unittest.mock import patch
        html = '<div class="results"><li><a href="/viewer#wikipedia/A/Test">Test</a></li></div>'
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)) as mock_get:
            _search_book("test", "wikipedia_en_all_maxi_2026-02", limit=3)
        params = mock_get.call_args.kwargs["params"]
        assert params["limit"] == 3

    def test_book_field_set_correctly(self):
        from app.sources.kiwix import _search_book
        html = """
        <div class="results">
          <li><a href="/viewer#wikipedia/A/Test">Test</a><cite>excerpt</cite></li>
        </div>
        """
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            results = _search_book("test", "wikipedia_en_all_maxi_2026-02")
        assert results[0]["book"] == "wikipedia_en_all_maxi_2026-02"


class TestFetchArticle:
    """Tests for _fetch_article() HTML content extraction."""

    def _mock_html_response(self, html):
        resp = MagicMock()
        resp.status_code = 200
        resp.content = html.encode("utf-8")
        resp.raise_for_status.return_value = None
        return resp

    def test_extracts_wikipedia_style_content(self):
        from app.sources.kiwix import _fetch_article
        html = '<html><body><div class="mw-parser-output">Nitrogen is a chemical element.</div></body></html>'
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#wikipedia/A/Nitrogen")
        assert "Nitrogen is a chemical element" in result

    def test_falls_back_to_body_when_no_known_selector(self):
        from app.sources.kiwix import _fetch_article
        html = "<html><body>Just plain body content here.</body></html>"
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert "Just plain body content" in result

    def test_strips_script_and_style_tags(self):
        from app.sources.kiwix import _fetch_article
        html = '''<html><body><div class="mw-parser-output">
            <script>alert('bad')</script>
            <style>.x { color: red; }</style>
            Real content here.
        </div></body></html>'''
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert "alert" not in result
        assert "color: red" not in result
        assert "Real content here" in result

    def test_strips_table_of_contents_box(self):
        """Regression test for a real bug found via a deliberate
        "bulletproofing" pass: ".toc" and "#toc" were CSS-selector
        syntax passed to soup([...]), which only matches literal HTML
        tag names (e.g. looking for a tag literally named "<.toc>",
        which doesn't exist) — confirmed directly that table-of-
        contents boxes were never actually being stripped from any
        fetched article, despite the code's clear intent."""
        from app.sources.kiwix import _fetch_article
        html = '''<html><body><div class="mw-parser-output">
            <div class="toc">Contents 1 History 2 Background 3 See also</div>
            <div id="toc">Another TOC variant</div>
            Real article content that matters.
        </div></body></html>'''
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert "Contents" not in result
        assert "Another TOC variant" not in result
        assert "Real article content that matters" in result

    def test_strips_table_tags(self):
        """Confirms "table" (a real, valid bare HTML tag name, unlike
        the broken CSS-selector entries above) was already working
        correctly and continues to after the fix."""
        from app.sources.kiwix import _fetch_article
        html = '''<html><body><div class="mw-parser-output">
            <table class="infobox"><tr><td>Infobox content</td></tr></table>
            Real article content.
        </div></body></html>'''
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert "Infobox content" not in result
        assert "Real article content" in result

    def test_truncates_to_max_chars(self):
        from app.sources.kiwix import _fetch_article
        long_text = "x" * 5000
        html = f'<html><body><div class="mw-parser-output">{long_text}</div></body></html>'
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#test", max_chars=100)
        assert len(result) <= 100

    def test_collapses_excess_newlines(self):
        from app.sources.kiwix import _fetch_article
        html = '<html><body><div class="mw-parser-output">Line1\n\n\n\n\nLine2</div></body></html>'
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert "\n\n\n" not in result

    def test_no_content_found_returns_empty(self):
        from app.sources.kiwix import _fetch_article
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response("")):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert result == ""

    def test_returns_empty_on_connection_error(self):
        from app.sources.kiwix import _fetch_article
        with patch("app.sources.kiwix._session.get", side_effect=req.exceptions.ConnectionError()):
            result = _fetch_article("http://kiwix:8080/viewer#test")
        assert result == ""

    def test_stack_exchange_style_question_div(self):
        from app.sources.kiwix import _fetch_article
        html = '<html><body><div id="question">How do I do X?</div></body></html>'
        with patch("app.sources.kiwix._session.get", return_value=self._mock_html_response(html)):
            result = _fetch_article("http://kiwix:8080/viewer#se/test")
        assert "How do I do X?" in result


class TestArticleFetchFallbackCap:
    """Regression tests for a real, significant bug found via a
    deliberate "bulletproofing" pass: search()'s article-fetch fallback
    loop ("try the next best result if the top one's article fetch
    fails") previously had no upper bound, trying EVERY remaining
    scored result. A realistic worst case (multiple books selected,
    disambiguation active across 3 candidate phrases, 15 results per
    search call) could produce up to ~59 total results — if Kiwix's
    search endpoint stayed healthy but the specific article-content
    path kept failing for every single one (a malformed page, broken
    links, transient timeouts), this loop could make up to 59
    sequential real HTTP requests at a 10s timeout each, nearly 10
    minutes for one search request. Capped at 5 fallback attempts —
    generous enough to recover from a realistic cluster of a few broken
    links near the top of the results, narrow enough to bound the
    worst case to under a minute."""

    def test_fallback_attempts_are_capped_not_unbounded(self):
        """The actual real-world regression test: confirms the loop
        stops well before exhausting every available result when every
        attempt fails."""
        from app.sources import kiwix
        fetch_attempts = []

        def fake_fetch(url, max_chars=3000):
            fetch_attempts.append(url)
            return ""  # every fetch fails

        fake_results = [
            {"title": f"Article {i}", "excerpt": "x", "url": f"http://x/{i}", "book": "wikipedia_en_all_maxi_2026-02"}
            for i in range(20)
        ]
        with patch.object(kiwix, "get_books", return_value=[
                {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}
             ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_search_book", return_value=fake_results), \
             patch.object(kiwix, "_fetch_article", side_effect=fake_fetch):
            kiwix.search("test query")

        assert len(fetch_attempts) <= 6  # 1 original attempt + 5 capped fallbacks
        assert len(fetch_attempts) < 20  # must NOT try every available result

    def test_genuine_recovery_within_cap_still_works(self):
        """Confirms the cap doesn't break the real, intended recovery
        behavior — a result within the cap that genuinely succeeds
        should still be correctly returned."""
        from app.sources import kiwix

        def fake_fetch(url, max_chars=3000):
            if "3" in url:
                return "Real article content here."
            return ""

        fake_results = [
            {"title": f"Article {i}", "excerpt": "x", "url": f"http://x/{i}", "book": "wikipedia_en_all_maxi_2026-02"}
            for i in range(10)
        ]
        with patch.object(kiwix, "get_books", return_value=[
                {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}
             ]), \
             patch.object(kiwix, "_pick_books_with_llm", return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_search_book", return_value=fake_results), \
             patch.object(kiwix, "_fetch_article", side_effect=fake_fetch):
            result = kiwix.search("test query")

        assert "Real article content here." in result
        assert "Article 3" in result


class TestPlainTermPooledDuringDisambiguation:
    """Regression tests for the 'what is a galaxy' -> 'Galaxy
    morphological classification' incident (v3.54.2), diagnosed live
    from an explanation-chain trace: during disambiguation, the LLM's
    candidate phrasings REPLACED the plain search term entirely, so the
    bare term was never searched and the plain 'Galaxy' article never
    even entered the candidate pool — the v3.54.1 content-exact +20
    can only rank what was fetched. The plain term is the one candidate
    that's always legitimate, and for a definitional ask it's usually
    the answer."""

    def _run_search(self, candidates, per_term_results, query="what is a galaxy"):
        from app.sources import kiwix
        from unittest.mock import patch

        searched = []

        def fake_search_book(term, book, limit=None):
            searched.append(term)
            return per_term_results.get(term, [])

        with patch.object(kiwix, "get_books", return_value=[
                 {"name": "wikipedia_en_all_maxi_2026-02", "title": "W", "summary": ""}]), \
             patch.object(kiwix, "_pick_books_with_llm",
                          return_value=["wikipedia_en_all_maxi_2026-02"]), \
             patch.object(kiwix, "_should_disambiguate", return_value=True), \
             patch.object(kiwix, "_get_disambiguation_candidates", return_value=candidates), \
             patch.object(kiwix, "_search_book", side_effect=fake_search_book), \
             patch.object(kiwix, "_fetch_article", side_effect=lambda url: f"content for {url}"):
            result = kiwix.search(query)
        return result, searched

    def test_plain_galaxy_article_wins_the_live_incident(self):
        """The exact live shape: candidates pool around sub-senses, the
        plain article only surfaces for the bare term. Before the fix,
        the bare term was never searched and the classification article
        won by default; now the plain article must both be fetched AND
        win scoring."""
        book = "wikipedia_en_all_maxi_2026-02"
        per_term = {
            "galaxy": [{"title": "Galaxy", "excerpt": "gravitationally bound system",
                        "url": "http://kiwix/Galaxy", "book": book}],
            "galaxy morphological classification": [
                {"title": "Galaxy morphological classification", "excerpt": "",
                 "url": "http://kiwix/Class", "book": book}],
            "galaxy formation": [
                {"title": "Galaxy formation and evolution", "excerpt": "",
                 "url": "http://kiwix/Form", "book": book}],
        }
        result, searched = self._run_search(
            ["galaxy morphological classification", "galaxy formation"], per_term
        )
        assert "galaxy" in searched, "the plain term was never searched"
        assert searched[0] == "galaxy", "the plain term should be pooled first"
        assert result.startswith("# Galaxy\n"), f"plain article did not win: {result[:60]!r}"

    def test_plain_term_not_searched_twice_when_llm_repeats_it(self):
        """The LLM sometimes returns the bare term AS a candidate —
        pooling must not search it twice."""
        book = "wikipedia_en_all_maxi_2026-02"
        per_term = {"galaxy": [{"title": "Galaxy", "excerpt": "",
                                "url": "http://kiwix/Galaxy", "book": book}]}
        _result, searched = self._run_search(
            ["galaxy", "galaxy astronomy"], per_term
        )
        assert searched.count("galaxy") == 1
