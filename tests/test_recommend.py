"""Tests for bookinventory.recommend: taste profile, scoring, ranking, exclusions."""

import threading

import pytest

from bookinventory import recommend
from bookinventory.aggregation import InventoryEntry
from bookinventory.database import Book
from bookinventory.lookup import find_book
from bookinventory.recommend import (
    PER_AUTHOR_CAP,
    Profile,
    Recommendation,
    Recommender,
    build_profile,
    clean_subject,
    score_candidate,
    summarize_description,
)


def owned(fake_openlibrary, *queries):
    return [find_book(q, fetch=fake_openlibrary) for q in queries]


class TestCleanSubject:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Science fiction", "science fiction"),
            ("  Science_Fiction ", "science fiction"),
            ("Dragons (Fiction)", "dragons"),
            ("Fiction", None),
            ("Accessible book", None),
            ("nyt:hardcover_fiction=2021", None),
            ("Translations into French", None),
            ("", None),
        ],
    )
    def test_cleaning(self, raw, expected):
        assert clean_subject(raw) == expected


class TestBuildProfile:
    def test_empty(self):
        profile = build_profile([])
        assert profile.empty
        assert profile.describe().startswith("Add some books")
        assert profile.top_authors() == [] and profile.top_subjects() == []

    def test_authors_weighted_by_titles_and_copies(self):
        books = [
            Book("Dune", "Frank Herbert", subjects=("Science fiction",)),
            Book("Dune Messiah", "Frank Herbert", subjects=("Science fiction",)),
            Book("Emma", "Jane Austen, Someone Else", quantity=3, subjects=("Romance", "Fiction")),
        ]
        profile = build_profile(books)
        assert profile.authors["Frank Herbert"] == 1.0
        assert profile.authors["Jane Austen"] == pytest.approx(
            2.0 / 2.0
        )  # 1 + 0.5 * 2 extra copies
        assert profile.authors["Someone Else"] == profile.authors["Jane Austen"]
        assert profile.author_titles == {"Frank Herbert": 2, "Jane Austen": 2, "Someone Else": 2}
        assert profile.subjects["science fiction"] == 1.0
        assert "fiction" not in profile.subjects  # stoplisted
        assert profile.subject_titles == {"science fiction": 2, "romance": 1}
        assert profile.size == 3

    def test_decades_and_owned_keys(self, dune, hobbit):
        profile = build_profile([dune, hobbit])
        assert profile.decades == {"1960s": 1.0, "1930s": 1.0}
        assert dune.key in profile.owned_keys and hobbit.text_key in profile.owned_keys

    def test_accepts_inventory_entries(self, dune):
        profile = build_profile([InventoryEntry(dune, ("Study",))])
        assert profile.size == 1 and "Frank Herbert" in profile.authors

    def test_author_name_spelling_is_merged_case_insensitively(self):
        profile = build_profile([Book("A", "frank herbert"), Book("B", "Frank Herbert")])
        assert list(profile.authors) == ["frank herbert"]  # first spelling kept
        assert profile.author_titles == {"frank herbert": 2}

    def test_describe(self, fake_openlibrary):
        profile = build_profile(owned(fake_openlibrary, "Dune", "Nineteen Eighty-Four Orwell"))
        assert profile.describe() == (
            "Based on 2 books: Frank Herbert, George Orwell, science fiction, politics."
        )
        assert build_profile([Book("Zine")]).describe() == "Based on 1 book: your books."


class TestScoreCandidate:
    @pytest.fixture
    def profile(self, fake_openlibrary):
        return build_profile(owned(fake_openlibrary, "Dune", "Nineteen Eighty-Four Orwell"))

    def test_author_and_subject_reasons(self, profile):
        doc = {"ratings_average": 3.9, "readinglog_count": 2500}
        book = Book("Children of Dune", "Frank Herbert", "1976", subjects=("Science fiction",))
        rec = score_candidate(profile, book, doc)
        assert rec.reasons == [
            "More by Frank Herbert (you have 1)",
            "Science fiction, like 2 of your books",
            "2,500 readers",
        ]
        assert rec.rating == 3.9
        assert 0.7 < rec.score <= 1.0
        assert rec.describe().startswith("Frank Herbert · 1976 · rated 3.9 · More by Frank Herbert")

    def test_unrelated_book_scores_low(self, profile):
        book = Book("Kitchen Confidential", "Anthony Bourdain", subjects=("Cooking",))
        rec = score_candidate(profile, book, {"ratings_average": 4.0, "readinglog_count": 4000})
        assert rec.score < recommend.MIN_SCORE + 0.1
        assert rec.reasons == ["4,000 readers"]

    def test_missing_rating_and_readers(self, profile):
        rec = score_candidate(profile, Book("X", "Frank Herbert"), {})
        assert rec.rating is None and rec.reasons == ["More by Frank Herbert (you have 1)"]
        assert rec.describe() == "Frank Herbert · More by Frank Herbert (you have 1)"

    def test_bad_rating_type_ignored(self, profile):
        rec = score_candidate(profile, Book("X", "Frank Herbert"), {"ratings_average": "4"})
        assert rec.rating is None

    def test_weights_sum_to_one(self):
        total = recommend.W_AUTHOR + recommend.W_SUBJECT + recommend.W_RATING
        assert total + recommend.W_POPULARITY == pytest.approx(1.0)


class TestRecommender:
    def test_recommends_related_unowned_books(self, fake_openlibrary):
        books = owned(fake_openlibrary, "Dune", "Nineteen Eighty-Four Orwell", "The Hobbit")
        recs = Recommender(
            search=fake_openlibrary.search, fetch_work=fake_openlibrary.work
        ).recommend(books, limit=8)
        titles = [r.book.title for r in recs]
        assert titles[0] == "Children of Dune"
        assert "Dune" not in titles and "The Hobbit" not in titles  # owned
        assert "Homage to Catalonia" in titles and "The Lord of the Rings" in titles
        assert "Kitchen Confidential" not in titles  # nothing in common
        assert all(isinstance(r, Recommendation) for r in recs)
        assert recs == sorted(recs, key=lambda r: -r.score)

    def test_diversity_cap_per_author(self, fake_openlibrary):
        recs = Recommender(
            search=fake_openlibrary.search, fetch_work=fake_openlibrary.work
        ).recommend(owned(fake_openlibrary, "Dune"))
        herbert = [r for r in recs if "Frank Herbert" in r.book.authors]
        assert len(herbert) == PER_AUTHOR_CAP

    def test_queries_top_authors_and_subjects_with_rating_sort(self, fake_openlibrary):
        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work)
        books = owned(fake_openlibrary, "Dune", "Nineteen Eighty-Four Orwell")
        recommender.recommend(books)
        authors = [s["author"] for s in fake_openlibrary.searches if "author" in s]
        subjects = [s["subject"] for s in fake_openlibrary.searches if "subject" in s]
        assert authors == ["Frank Herbert", "George Orwell"]
        assert subjects[:2] == ["science fiction", "politics"]
        assert all(
            s["sort"] == "rating" and "ratings_average" in s["fields"]
            for s in fake_openlibrary.searches
        )

    def test_limit_and_dismissed(self, fake_openlibrary):
        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work)
        books = owned(fake_openlibrary, "Dune")
        first = recommender.recommend(books, limit=2)
        assert len(first) == 2
        again = recommender.recommend(books, limit=2, dismissed=[first[0].key])
        assert first[0].key not in [r.key for r in again]

    def test_empty_inventory(self, fake_openlibrary):
        assert (
            Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work).recommend(
                []
            )
            == []
        )
        assert fake_openlibrary.searches == []

    def test_responses_are_cached_and_failures_tolerated(self, fake_openlibrary):
        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work)
        books = owned(fake_openlibrary, "Dune")
        recommender.recommend(books)
        count = len(fake_openlibrary.searches)
        recommender.recommend(books)
        assert len(fake_openlibrary.searches) == count  # cached

        fake_openlibrary.fail_with = OSError("offline")
        assert (
            Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work).recommend(
                books
            )
            == []
        )

    def test_searches_run_concurrently(self, fake_openlibrary):
        names = set()
        lock = threading.Lock()

        def search(params):
            with lock:
                names.add(threading.current_thread().name)
            return fake_openlibrary.search(params)

        Recommender(search=search, fetch_work=fake_openlibrary.work, workers=3).recommend(
            owned(fake_openlibrary, "Dune", "The Hobbit")
        )
        assert "MainThread" not in names

    def test_profile_dataclass_defaults(self):
        assert Profile().empty and Profile().top_authors() == []


def test_unrelated_candidates_below_min_score_are_dropped(fake_openlibrary):
    # Subject search for "politics" returns Kitchen Confidential? No - but a
    # doc with nothing in common can arrive via an author's other genres.
    fake_openlibrary.catalogue.append(
        {
            "key": "/works/OLZZZW",
            "title": "Herbert's Gardening Tips",
            "author_name": ["Another Herbert"],
            "subject": ["Gardening"],
        }
    )

    def search(params):
        data = fake_openlibrary.search(params)
        data["docs"].append(fake_openlibrary.catalogue[-1])  # sneak it into every response
        return data

    recs = Recommender(search=search, fetch_work=fake_openlibrary.work).recommend(
        owned(fake_openlibrary, "Dune")
    )
    assert "Herbert's Gardening Tips" not in [r.book.title for r in recs]


class TestSummarizeDescription:
    def test_text_object_and_markdown(self):
        raw = {"type": "/type/text", "value": "A tale of *Arrakis* with a [link](http://x)."}
        assert summarize_description(raw) == "A tale of Arrakis with a link."

    def test_first_paragraph_only_and_trailers_dropped(self):
        raw = "First paragraph here.\n\nSecond paragraph.\n\n----------\nContains: notes"
        assert summarize_description(raw) == "First paragraph here."
        assert summarize_description("Body text. Source: Wikipedia") == "Body text."

    def test_trimmed_at_sentence_boundary(self):
        raw = "One sentence. " * 30
        summary = summarize_description(raw, limit=50)
        assert summary == "One sentence. One sentence. One sentence."
        assert len(summary) <= 50

    def test_single_long_sentence_cut_at_word(self):
        raw = "word " * 100
        summary = summarize_description(raw, limit=40)
        assert summary.endswith("…") and len(summary) <= 41
        assert " " not in summary[-2:]

    def test_list_and_junk(self):
        assert summarize_description(["One.", "Two."]) == "One. Two."
        assert summarize_description(None) == ""
        assert summarize_description(42) == ""
        assert summarize_description("   ") == ""


class TestSummaries:
    def test_recommendations_carry_summaries(self, fake_openlibrary):
        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work)
        recs = recommender.recommend(owned(fake_openlibrary, "Dune", "The Hobbit"))
        by_title = {r.book.title: r for r in recs}
        children = by_title["Children of Dune"]
        assert children.summary.startswith("Nine years after the events of Dune Messiah")
        assert children.summary.endswith("transformation.")  # trimmed at a sentence
        assert "Contains" not in children.summary and len(children.summary) <= 280
        assert by_title["The Lord of the Rings"].summary == (
            "An epic high-fantasy quest to destroy the One Ring."
        )
        assert by_title["Dune Messiah"].summary == ""  # no description anywhere
        assert sorted(fake_openlibrary.work_requests) == sorted(
            r.book.work_key for r in recs if r.book.work_key
        )

    def test_first_sentence_fallback(self, fake_openlibrary):
        fake_openlibrary.descriptions.clear()
        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work)
        recs = recommender.recommend(owned(fake_openlibrary, "Dune"))
        children = next(r for r in recs if r.book.title == "Children of Dune")
        assert children.summary == "The sand was hot under Leto's feet."

    def test_work_fetch_failures_are_tolerated_and_cached(self, fake_openlibrary):
        calls = []

        def fetch_work(key):
            calls.append(key)
            raise OSError("offline")

        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fetch_work)
        recs = recommender.recommend(owned(fake_openlibrary, "Dune"))
        assert recs and all(r.summary in ("", "The sand was hot under Leto's feet.") for r in recs)
        count = len(calls)
        recommender.add_summaries(recs)
        assert len(calls) == count  # failures cached too: no retry storm

    def test_summary_for_book_without_work_key(self, fake_openlibrary):
        recommender = Recommender(search=fake_openlibrary.search, fetch_work=fake_openlibrary.work)
        assert recommender.summary_for(Book("Zine")) == ""
        recommender.add_summaries([])
