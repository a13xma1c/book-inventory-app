"""Tests for bookinventory.covers: cover thumbnails, cached on disk, best effort."""

import io
import threading

from bookinventory import covers
from bookinventory.database import Book

URL = "https://covers.openlibrary.org/b/id/12345-M.jpg"


class TestCoverId:
    def test_parses_id(self):
        assert covers.cover_id(URL) == "12345"

    def test_no_id(self):
        assert covers.cover_id("") is None
        assert covers.cover_id("https://example.com/x.jpg") is None


class TestPaths:
    def test_base_and_cached(self, tmp_path):
        book = Book("Dune", cover_url=URL)
        assert covers.cover_base(tmp_path, book) == tmp_path / "12345"
        assert covers.cached_cover(tmp_path, book) is None
        (tmp_path / "12345.png").write_bytes(b"jpeg-bytes")
        assert covers.cached_cover(tmp_path, book) == tmp_path / "12345"

    def test_book_without_cover(self, tmp_path):
        assert covers.cover_base(tmp_path, Book("Zine")) is None
        assert covers.cached_cover(tmp_path, Book("Zine")) is None


class TestFetchCover:
    def test_downloads_once(self, tmp_path):
        calls = []

        def download(url):
            calls.append(url)
            return b"jpeg-bytes"

        book = Book("Dune", cover_url=URL)
        assert covers.fetch_cover(tmp_path / "covers", book, download) == tmp_path / "covers/12345"
        assert (tmp_path / "covers/12345.png").read_bytes() == b"jpeg-bytes"
        assert covers.fetch_cover(tmp_path / "covers", book, download) == tmp_path / "covers/12345"
        assert calls == [URL]
        assert not list((tmp_path / "covers").glob("*.part"))

    def test_failure_is_swallowed(self, tmp_path):
        def download(url):
            raise OSError("offline")

        assert covers.fetch_cover(tmp_path, Book("Dune", cover_url=URL), download) is None
        assert not (tmp_path / "12345.png").exists()

    def test_empty_response(self, tmp_path):
        assert covers.fetch_cover(tmp_path, Book("Dune", cover_url=URL), lambda u: b"") is None

    def test_no_cover_url(self, tmp_path):
        assert covers.fetch_cover(tmp_path, Book("Zine"), lambda u: b"x") is None


class TestFetchCovers:
    def test_concurrent_and_keyed_by_cover_id(self, tmp_path):
        names = set()
        lock = threading.Lock()

        def download(url):
            with lock:
                names.add(threading.current_thread().name)
            return b"x" if "111" in url else b""

        books = [
            Book("A", id=1, cover_url="https://covers.openlibrary.org/b/id/111-M.jpg"),
            Book("B", id=2, cover_url="https://covers.openlibrary.org/b/id/222-M.jpg"),
            Book("C", id=3),
            Book("D", cover_url="https://covers.openlibrary.org/b/id/111-M.jpg"),  # same cover
        ]
        fetched = covers.fetch_covers(tmp_path, books, download, workers=2)
        assert fetched == {"111": tmp_path / "111"}
        assert "MainThread" not in names

    def test_each_cover_downloaded_once(self, tmp_path):
        calls = []
        books = [
            Book("A", cover_url=f"https://covers.openlibrary.org/b/id/{i}-M.jpg") for i in (1, 1, 2)
        ]
        covers.fetch_covers(tmp_path, books, lambda url: calls.append(url) or b"x")
        assert sorted(calls) == [
            "https://covers.openlibrary.org/b/id/1-M.jpg",
            "https://covers.openlibrary.org/b/id/2-M.jpg",
        ]

    def test_nothing_to_do(self, tmp_path):
        assert covers.fetch_covers(tmp_path, [Book("C", id=3)], lambda u: b"x") == {}


class TestDownload:
    def test_uses_user_agent(self, monkeypatch):
        seen = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            seen["agent"] = request.get_header("User-agent")
            seen["url"] = request.full_url
            return Response(b"jpeg")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        assert covers.download(URL) == b"jpeg"
        assert seen == {"agent": covers.USER_AGENT, "url": URL}
