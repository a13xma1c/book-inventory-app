"""Tests for bookinventory.lookup: search, ranking, caching, concurrency, retries."""

import io
import json
import threading
import urllib.error
import urllib.parse

import pytest

from bookinventory.database import Book
from bookinventory.lookup import (
    FIELDS,
    RETRY_DELAYS,
    SEARCH_URL,
    USER_AGENT,
    CachedFetch,
    Match,
    _fetch,
    book_from_doc,
    find_book,
    find_matches,
    lookup_all,
    rank_docs,
    run_lookups,
)


class TestBookFromDoc:
    def test_full_document(self):
        doc = {
            "key": "/works/OL1W",
            "title": "Dune",
            "author_name": ["Frank Herbert", "Someone", "Else"],
            "first_publish_year": 1965,
            "isbn": ["0441013597", "9780441013593"],
            "cover_i": 12345,
        }
        book = book_from_doc(doc)
        assert book == Book(
            "Dune",
            "Frank Herbert, Someone",
            "1965",
            "0441013597",
            work_key="/works/OL1W",
            cover_url="https://covers.openlibrary.org/b/id/12345-M.jpg",
        )
        assert book.key == "work:/works/OL1W"

    def test_sparse_document(self):
        assert book_from_doc({"title": "Zine"}) == Book("Zine")

    def test_missing_title(self):
        assert book_from_doc({"author_name": ["x"]}) is None
        assert book_from_doc({"title": ""}) is None

    def test_empty_isbn_list(self):
        assert book_from_doc({"title": "X", "isbn": []}).isbn == ""


class TestRankDocs:
    def test_best_first_and_non_matches_dropped(self):
        docs = [
            {"title": "Dune Messiah", "author_name": ["Frank Herbert"]},
            {"title": "Dune", "author_name": ["Frank Herbert"]},
            {"title": "Kitchen Confidential", "author_name": ["Anthony Bourdain"]},
        ]
        matches = rank_docs("DUNE", docs)
        assert [m.book.title for m in matches] == ["Dune", "Dune Messiah"]
        assert matches[0].score == 1.0
        assert matches[1].score == 0.75

    def test_ties_keep_service_order(self):
        docs = [{"title": "Dune", "author_name": ["A"]}, {"title": "Dune", "author_name": ["B"]}]
        assert [m.book.authors for m in rank_docs("Dune", docs)] == ["A", "B"]

    def test_skips_bad_docs(self):
        assert rank_docs("Dune", [{"x": 1}, {"title": ""}]) == []


class TestFindMatches:
    def test_matches(self, fake_openlibrary):
        matches = find_matches("Dune Herbert", fetch=fake_openlibrary)
        assert matches[0].book.title == "Dune"
        assert matches[0].score > matches[1].score
        assert all("Dune" in m.book.title for m in matches)
        assert all(isinstance(m, Match) for m in matches)

    def test_network_error_gives_empty(self, fake_openlibrary):
        fake_openlibrary.fail_with = OSError("offline")
        assert find_matches("Dune", fetch=fake_openlibrary) == []

    def test_non_network_errors_propagate(self):
        def fetch(query):
            raise ValueError("bad json")

        with pytest.raises(ValueError):
            find_matches("Dune", fetch=fetch)


class TestFindBook:
    def test_picks_best_not_first(self):
        def fetch(query):
            return {
                "docs": [
                    {"title": "Unrelated Thing", "author_name": ["Someone"]},
                    {"title": "Dune Messiah", "author_name": ["Frank Herbert"]},
                    {"title": "Dune", "author_name": ["Frank Herbert"]},
                ]
            }

        assert find_book("Dune", fetch=fetch).title == "Dune"

    def test_author_only_query_finds_nothing(self, fake_openlibrary):
        assert find_book("Frank Herbert", fetch=fake_openlibrary) is None

    def test_no_match_and_empty(self, fake_openlibrary):
        assert find_book("Completely Unknown", fetch=fake_openlibrary) is None
        assert find_book("Dune", fetch=lambda q: {"docs": []}) is None
        assert find_book("Dune", fetch=lambda q: {}) is None

    def test_network_error_returns_none(self, fake_openlibrary):
        fake_openlibrary.fail_with = OSError("offline")
        assert find_book("Dune", fetch=fake_openlibrary) is None

    def test_http_error_is_an_oserror(self):
        def fetch(query):
            raise urllib.error.HTTPError("u", 503, "busy", {}, None)

        assert find_book("Dune", fetch=fetch) is None

    def test_returned_book_has_no_id_and_one_copy(self, fake_openlibrary):
        book = find_book("Dune", fetch=fake_openlibrary)
        assert book.id is None and book.quantity == 1
        assert book.work_key.startswith("/works/")


class TestCachedFetch:
    def test_caches_by_normalised_query(self, fake_openlibrary):
        fetch = CachedFetch(fake_openlibrary)
        fetch("The Hobbit")
        fetch("the  hobbit!")
        fetch("THE HOBBIT")
        assert fake_openlibrary.queries == ["The Hobbit"]
        assert fetch.requests == 1

    def test_failures_are_not_cached(self, fake_openlibrary):
        fetch = CachedFetch(fake_openlibrary)
        fake_openlibrary.fail_with = OSError("offline")
        with pytest.raises(OSError):
            fetch("Dune")
        fake_openlibrary.fail_with = None
        assert fetch("Dune")["docs"][0]["title"] == "Dune"
        assert fetch.requests == 1

    def test_clear(self, fake_openlibrary):
        fetch = CachedFetch(fake_openlibrary)
        fetch("Dune")
        fetch.clear()
        fetch("Dune")
        assert len(fake_openlibrary.queries) == 2

    def test_thread_safe(self, fake_openlibrary):
        fetch = CachedFetch(fake_openlibrary)
        threads = [threading.Thread(target=fetch, args=("Dune",)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert fetch("Dune")["docs"][0]["title"] == "Dune"
        assert fetch.requests <= 8  # no crash, no corruption; duplicates possible pre-cache


class TestRunLookups:
    def test_concurrent_and_deduplicated(self):
        seen = []
        lock = threading.Lock()

        def lookup(query):
            with lock:
                seen.append((query, threading.current_thread().name))
            return [Match(Book(query), 1.0)]

        results = run_lookups(["a", "b", "a", "c"], lookup, workers=3)
        assert list(results) == ["a", "b", "c"]
        assert results["b"][0].book.title == "b"
        assert len(seen) == 3
        assert all(not name.startswith("MainThread") for _, name in seen)

    def test_empty(self):
        assert run_lookups([], lambda q: []) == {}

    def test_lookup_all_uses_fetch(self, fake_openlibrary):
        results = lookup_all(["Dune", "Pride and Prejudice"], fetch=fake_openlibrary, workers=2)
        assert results["Dune"][0].book.title == "Dune"
        assert results["Pride and Prejudice"][0].book.title == "Pride and Prejudice"
        assert sorted(fake_openlibrary.queries) == ["Dune", "Pride and Prejudice"]


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestFetch:
    def test_builds_request_and_parses_json(self, monkeypatch):
        seen = {}

        def fake_urlopen(request, timeout):
            seen["url"] = request.full_url
            seen["agent"] = request.get_header("User-agent")
            seen["timeout"] = timeout
            return Response(json.dumps({"docs": [{"title": "Dune"}]}).encode())

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert _fetch("Dune Herbert", timeout=4.5) == {"docs": [{"title": "Dune"}]}
        split = urllib.parse.urlsplit(seen["url"])
        assert f"{split.scheme}://{split.netloc}{split.path}" == SEARCH_URL
        params = urllib.parse.parse_qs(split.query)
        assert params["q"] == ["Dune Herbert"]
        assert params["limit"] == ["5"]
        assert params["fields"] == [FIELDS]
        assert "cover_i" in FIELDS and "key" in FIELDS
        assert seen["agent"] == USER_AGENT
        assert "(" in USER_AGENT and "@" in USER_AGENT  # app name + contact, as OL asks
        assert seen["timeout"] == 4.5

    def test_retries_rate_limit_then_succeeds(self, monkeypatch):
        attempts = []
        sleeps = []

        def fake_urlopen(request, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.HTTPError("u", 429, "slow down", {}, None)
            return Response(b'{"docs": []}')

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert _fetch("Dune", sleep=sleeps.append) == {"docs": []}
        assert len(attempts) == 3
        assert sleeps == list(RETRY_DELAYS)

    def test_gives_up_after_retries(self, monkeypatch):
        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("no network")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        sleeps = []
        with pytest.raises(urllib.error.URLError):
            _fetch("Dune", sleep=sleeps.append)
        assert len(sleeps) == len(RETRY_DELAYS)

    def test_client_errors_are_not_retried(self, monkeypatch):
        attempts = []

        def fake_urlopen(request, timeout):
            attempts.append(1)
            raise urllib.error.HTTPError("u", 404, "nope", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        with pytest.raises(urllib.error.HTTPError):
            _fetch("Dune", sleep=lambda s: None)
        assert len(attempts) == 1

    def test_timeout_is_retried(self, monkeypatch):
        attempts = []

        def fake_urlopen(request, timeout):
            attempts.append(1)
            if len(attempts) == 1:
                raise TimeoutError("timed out")
            return Response(b"{}")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert _fetch("Dune", sleep=lambda s: None) == {}
        assert len(attempts) == 2
