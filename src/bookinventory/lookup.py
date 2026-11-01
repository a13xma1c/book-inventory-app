"""Book metadata lookup using the free Open Library search API (no API key).

* :func:`find_book` / :func:`find_matches` - one query, best result(s).
* :class:`CachedFetch` - remembers responses so the same spine text photographed
  twice costs one request.
* :func:`lookup_all` - many queries concurrently (the network round-trip, not
  the CPU, is what makes a scan slow).
* :func:`_fetch` - the HTTP call, with retries and back-off for transient
  errors, and the descriptive User-Agent that Open Library asks clients for.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from bookinventory.database import Book, normalize
from bookinventory.matching import is_match, match_score

SEARCH_URL = "https://openlibrary.org/search.json"
WORK_URL = "https://openlibrary.org{key}.json"  # key like "/works/OL893421W"
FIELDS = "key,title,author_name,first_publish_year,isbn,cover_i,subject"
MAX_SUBJECTS = 20  # per book; Open Library lists hundreds for popular works
COVER_URL = "https://covers.openlibrary.org/b/id/{cover_id}-M.jpg"
# Open Library asks for a User-Agent that identifies the app and a contact.
USER_AGENT = "BookInventoryApp/0.1 (https://example.com/bookinventory; you@example.com)"
RESULT_LIMIT = 5
RETRY_DELAYS = (0.5, 1.5)  # seconds between attempts on transient failures
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_WORKERS = 4

Fetch = Callable[[str], dict]


def get_json(url: str, timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> dict:
    """GET a JSON document; retries rate-limits/server errors/timeouts."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRYABLE_STATUS or attempt >= len(RETRY_DELAYS):
                raise
        except (urllib.error.URLError, TimeoutError):
            if attempt >= len(RETRY_DELAYS):
                raise
        sleep(RETRY_DELAYS[attempt])
        attempt += 1


def search_request(
    params: dict, timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep
) -> dict:
    """GET the search endpoint with ``params``."""
    return get_json(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}", timeout, sleep)


def fetch_work(
    key: str, timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep
) -> dict:
    """GET a work record (description, subjects, ...) by its Open Library key."""
    return get_json(WORK_URL.format(key=key), timeout, sleep)


def _fetch(query: str, timeout: float = 10.0, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Search for OCR'd spine text."""
    return search_request({"q": query, "limit": RESULT_LIMIT, "fields": FIELDS}, timeout, sleep)


@dataclass(frozen=True)
class Match:
    """A search result that plausibly corresponds to an OCR'd line."""

    book: Book
    score: float


def book_from_doc(doc: dict) -> Book | None:
    title = doc.get("title", "")
    if not title:
        return None
    isbns = doc.get("isbn") or [""]
    cover_id = doc.get("cover_i")
    subjects = [str(s).strip() for s in doc.get("subject") or [] if str(s).strip()]
    return Book(
        title=title,
        authors=", ".join(doc.get("author_name", [])[:2]),
        year=str(doc.get("first_publish_year", "")),
        isbn=isbns[0],
        work_key=doc.get("key") or "",
        cover_url=COVER_URL.format(cover_id=cover_id) if cover_id else "",
        subjects=tuple(subjects[:MAX_SUBJECTS]),
    )


def rank_docs(query: str, docs: Iterable[dict]) -> list[Match]:
    """Matching documents, best first (ties keep the service's relevance order)."""
    matches = []
    for doc in docs:
        book = book_from_doc(doc)
        if book is not None and is_match(query, book.title, book.authors):
            matches.append(Match(book, match_score(query, book.title, book.authors)))
    matches.sort(key=lambda m: -m.score)
    return matches


def find_matches(query: str, fetch: Fetch = _fetch) -> list[Match]:
    """All plausible matches for ``query``, best first; empty on network failure."""
    try:
        data = fetch(query)
    except OSError:  # network down, timeout, HTTP error
        return []
    return rank_docs(query, data.get("docs", []))


def find_book(query: str, fetch: Fetch = _fetch) -> Book | None:
    """Return the search result that best matches the OCR text."""
    matches = find_matches(query, fetch)
    return matches[0].book if matches else None


class CachedFetch:
    """Wrap a fetch function with a thread-safe cache keyed by normalised query."""

    def __init__(self, fetch: Fetch = _fetch):
        self._fetch = fetch
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.requests = 0

    def __call__(self, query: str) -> dict:
        key = normalize(query)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        data = self._fetch(query)  # outside the lock: failures are not cached
        with self._lock:
            self._cache[key] = data
            self.requests += 1
        return data

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


Lookup = Callable[[str], list[Match]]


def run_lookups(
    queries: Iterable[str], lookup: Lookup, workers: int = DEFAULT_WORKERS
) -> dict[str, list[Match]]:
    """Run ``lookup`` for several queries concurrently. Returns ``{query: matches}``."""
    queries = list(dict.fromkeys(queries))  # de-duplicate, keep order
    if not queries:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(queries)))) as pool:
        results = list(pool.map(lookup, queries))
    return dict(zip(queries, results, strict=True))


def lookup_all(
    queries: Iterable[str], fetch: Fetch = _fetch, workers: int = DEFAULT_WORKERS
) -> dict[str, list[Match]]:
    """Look up several queries concurrently against ``fetch``."""
    return run_lookups(queries, lambda q: find_matches(q, fetch), workers)
