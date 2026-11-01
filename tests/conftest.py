"""Shared fixtures: a fake Open Library, canned OCR output and sample books."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from bookinventory.database import Book, Database
from bookinventory.matching import tokens

# --------------------------------------------------------------------------- #
# Canned Open Library catalogue
# --------------------------------------------------------------------------- #

CATALOGUE: list[dict] = [
    {
        "key": "/works/OL262758W",
        "cover_i": 14627509,
        "title": "The Hobbit",
        "subject": ["Fantasy", "Fiction", "Dragons", "Adventure"],
        "ratings_average": 4.3,
        "ratings_count": 1200,
        "readinglog_count": 9000,
        "author_name": ["J.R.R. Tolkien"],
        "first_publish_year": 1937,
        "isbn": ["9780261102217", "0261102214"],
    },
    {
        "key": "/works/OL27448W",
        "cover_i": 9255566,
        "title": "The Lord of the Rings",
        "subject": ["Fantasy", "Fiction", "Epic", "Adventure"],
        "ratings_average": 4.5,
        "ratings_count": 2100,
        "readinglog_count": 15000,
        "author_name": ["J.R.R. Tolkien"],
        "first_publish_year": 1954,
        "isbn": ["9780618640157"],
    },
    {
        "key": "/works/OL893415W",
        "cover_i": 12369637,
        "title": "Dune",
        "subject": ["Science fiction", "Fiction", "Desert", "Politics"],
        "ratings_average": 4.2,
        "ratings_count": 1800,
        "readinglog_count": 12000,
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1965,
        "isbn": ["9780441013593"],
    },
    {
        "key": "/works/OL893409W",
        "cover_i": 8241218,
        "title": "Dune Messiah",
        "subject": ["Science fiction", "Fiction", "Politics"],
        "ratings_average": 3.8,
        "ratings_count": 400,
        "readinglog_count": 3000,
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1969,
        "isbn": ["9780441172696"],
    },
    {
        "key": "/works/OL1168083W",
        "cover_i": 8575741,
        "title": "Nineteen Eighty-Four",
        "subject": ["Dystopia", "Science fiction", "Fiction", "Politics"],
        "ratings_average": 4.1,
        "ratings_count": 2500,
        "readinglog_count": 20000,
        "author_name": ["George Orwell"],
        "first_publish_year": 1949,
        "isbn": ["9780451524935"],
    },
    {
        "key": "/works/OL1168007W",
        "cover_i": 12716527,
        "title": "Animal Farm",
        "subject": ["Satire", "Fiction", "Politics", "Allegory"],
        "ratings_average": 3.9,
        "ratings_count": 1900,
        "readinglog_count": 16000,
        "author_name": ["George Orwell"],
        "first_publish_year": 1945,
        "isbn": ["9780451526342"],
    },
    {
        "key": "/works/OL45804W",
        "cover_i": 10520832,
        "title": "Fahrenheit 451",
        "subject": ["Science fiction", "Dystopia", "Fiction", "Censorship"],
        "ratings_average": 4.0,
        "ratings_count": 1500,
        "readinglog_count": 11000,
        "author_name": ["Ray Bradbury"],
        "first_publish_year": 1953,
        "isbn": ["9781451673319"],
    },
    {
        "key": "/works/OL66554W",
        "cover_i": 14348537,
        "title": "Pride and Prejudice",
        "subject": ["Romance", "Fiction", "Classics", "England"],
        "ratings_average": 4.4,
        "ratings_count": 2300,
        "readinglog_count": 18000,
        "author_name": ["Jane Austen"],
        "first_publish_year": 1813,
        "isbn": ["9780141439518"],
    },
    {
        "key": "/works/OL3140822W",
        "cover_i": 8231856,
        "title": "Kitchen Confidential",
        "subject": ["Cooking", "Memoir", "Restaurants"],
        "ratings_average": 4.0,
        "ratings_count": 600,
        "readinglog_count": 4000,
        "author_name": ["Anthony Bourdain", "Ghost Writer", "Third Person"],
        "first_publish_year": 2000,
        "isbn": ["9780060899226"],
    },
    {"title": "Untitled Zine"},  # deliberately sparse metadata
    # Reachable via author/subject searches (recommendations), not via spine text.
    {
        "key": "/works/OL893421W",
        "cover_i": 8244209,
        "title": "Children of Dune",
        "first_sentence": ["The sand was hot under Leto's feet."],
        "subject": ["Science fiction", "Fiction", "Politics"],
        "ratings_average": 3.9,
        "ratings_count": 300,
        "readinglog_count": 2500,
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1976,
        "isbn": ["9780441104024"],
    },
    {
        "key": "/works/OL893417W",
        "cover_i": 8244210,
        "title": "God Emperor of Dune",
        "subject": ["Science fiction", "Fiction"],
        "ratings_average": 3.7,
        "ratings_count": 200,
        "readinglog_count": 1800,
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1981,
    },
    {
        "key": "/works/OL893419W",
        "cover_i": 8244211,
        "title": "Heretics of Dune",
        "subject": ["Science fiction", "Fiction"],
        "ratings_average": 3.6,
        "ratings_count": 150,
        "readinglog_count": 1200,
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1984,
    },
    {
        "key": "/works/OL893420W",
        "cover_i": 8244212,
        "title": "Chapterhouse: Dune",
        "subject": ["Science fiction", "Fiction"],
        "ratings_average": 3.5,
        "ratings_count": 120,
        "readinglog_count": 1000,
        "author_name": ["Frank Herbert"],
        "first_publish_year": 1985,
    },
    {
        "key": "/works/OL2163649W",
        "cover_i": 8231990,
        "title": "Foundation",
        "subject": ["Science fiction", "Fiction", "Galactic empire", "Accessible book"],
        "ratings_average": 4.2,
        "ratings_count": 1300,
        "readinglog_count": 9000,
        "author_name": ["Isaac Asimov"],
        "first_publish_year": 1951,
        "isbn": ["9780553293357"],
    },
    {
        "key": "/works/OL8193416W",
        "cover_i": 12000001,
        "title": "The Left Hand of Darkness",
        "subject": ["Science fiction", "Fiction", "Gender"],
        "ratings_average": 4.1,
        "ratings_count": 700,
        "readinglog_count": 5000,
        "author_name": ["Ursula K. Le Guin"],
        "first_publish_year": 1969,
    },
    {
        "key": "/works/OL19325W",
        "cover_i": 12000002,
        "title": "Brave New World",
        "subject": ["Dystopia", "Science fiction", "Fiction"],
        "ratings_average": 4.0,
        "ratings_count": 1600,
        "readinglog_count": 13000,
        "author_name": ["Aldous Huxley"],
        "first_publish_year": 1932,
    },
    {
        "key": "/works/OL1168008W",
        "cover_i": 12000003,
        "title": "Homage to Catalonia",
        "subject": ["Spanish Civil War", "Memoir", "Politics"],
        "ratings_average": 4.2,
        "ratings_count": 500,
        "readinglog_count": 3500,
        "author_name": ["George Orwell"],
        "first_publish_year": 1938,
    },
]


@dataclass
class FakeOpenLibrary:
    """Stands in for ``lookup._fetch``: ``fetch(query) -> dict``.

    Returns every catalogue document sharing at least one token with the
    query, most overlapping first, mimicking a relevance-ranked search.
    """

    catalogue: list[dict] = field(default_factory=lambda: [dict(d) for d in CATALOGUE])
    queries: list[str] = field(default_factory=list)
    searches: list[dict] = field(default_factory=list)
    fail_with: Exception | None = None

    descriptions: dict = field(
        default_factory=lambda: {
            "/works/OL893421W": {
                "type": "/type/text",
                "value": (
                    "Nine years after the events of *Dune Messiah*, the twins Leto and "
                    "Ghanima Atreides must face their destiny on Arrakis. It is a story of "
                    "prescience, politics and the price of transformation. "
                    "A long third sentence follows that pushes the paragraph past the "
                    "summary limit so that it has to be trimmed at a sentence boundary.\n\n"
                    "----------\nContains: afterword"
                ),
            },
            "/works/OL27448W": "An epic high-fantasy quest to destroy the One Ring.",
        }
    )
    work_requests: list[str] = field(default_factory=list)

    def work(self, key: str) -> dict:
        """Stands in for ``lookup.fetch_work``."""
        self.work_requests.append(key)
        if self.fail_with is not None:
            raise self.fail_with
        data = {"key": key, "title": "?"}
        if key in self.descriptions:
            data["description"] = self.descriptions[key]
        return data

    def search(self, params: dict) -> dict:
        """Stands in for ``lookup.search_request``: ``author=`` / ``subject=`` / ``q=`` queries."""
        self.searches.append(dict(params))
        if self.fail_with is not None:
            raise self.fail_with
        if "q" in params:
            return self(params["q"])
        docs = []
        for doc in self.catalogue:
            if "author" in params:
                wanted = params["author"].lower()
                if not any(wanted == a.lower() for a in doc.get("author_name", [])):
                    continue
            if "subject" in params:
                wanted = params["subject"].lower()
                if not any(wanted == s.lower() for s in doc.get("subject", [])):
                    continue
            docs.append(doc)
        if params.get("sort") == "rating":
            docs.sort(key=lambda d: -(d.get("ratings_average") or 0))
        limit = int(params.get("limit", len(docs)))
        return {"numFound": len(docs), "docs": docs[:limit]}

    def __call__(self, query: str) -> dict:
        self.queries.append(query)
        if self.fail_with is not None:
            raise self.fail_with
        wanted = set(tokens(query))
        scored = []
        for doc in self.catalogue:
            have = set(tokens(doc.get("title", "")))
            for author in doc.get("author_name", []):
                have.update(tokens(author))
            overlap = len(wanted & have)
            if overlap:
                scored.append((overlap, doc))
        scored.sort(key=lambda pair: -pair[0])
        return {"numFound": len(scored), "docs": [doc for _, doc in scored]}


@pytest.fixture
def fake_openlibrary() -> FakeOpenLibrary:
    return FakeOpenLibrary()


# --------------------------------------------------------------------------- #
# Canned OCR output ("photos")
# --------------------------------------------------------------------------- #

SHELF_A = [
    "THE HOBBIT",
    "J.R.R. TOLKIEN",
    "DUNE",
    "Frank Herbert",
    "PENGUIN",
    "9780441013593",
    "Nineteen Eighty-Four  George Orwell",
    "DUNE",  # same spine read in a second orientation
]
SHELF_A_OVERLAP = ["DUNE", "Nineteen Eighty Four", "Animal Farm"]
SHELF_B = ["Dune", "Fahrenheit 451 Ray Bradbury", "Pride and Prejudice"]
BLANK_PHOTO: list[str] = []


# --------------------------------------------------------------------------- #
# Sample books / database
# --------------------------------------------------------------------------- #


@pytest.fixture
def hobbit() -> Book:
    return Book("The Hobbit", "J.R.R. Tolkien", "1937", "9780261102217")


@pytest.fixture
def dune() -> Book:
    return Book("Dune", "Frank Herbert", "1965", "9780441013593")


@pytest.fixture
def orwell() -> Book:
    return Book("Nineteen Eighty-Four", "George Orwell", "1949")


@pytest.fixture
def sample_books(hobbit: Book, dune: Book, orwell: Book) -> list[Book]:
    return [hobbit, dune, orwell]


@pytest.fixture
def db() -> Database:
    database = Database()
    yield database
    database.close()


@pytest.fixture
def file_db(tmp_path) -> Database:
    database = Database(str(tmp_path / "books.sqlite3"))
    yield database
    database.close()


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests must never reach the real Open Library; fail fast instead of retrying."""

    def refuse(*args, **kwargs):
        raise AssertionError("test tried to use the network")

    monkeypatch.setattr("urllib.request.urlopen", refuse)
