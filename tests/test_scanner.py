"""Tests for bookinventory.scanner: OCR lines -> reviewed candidates."""

from bookinventory.database import Book
from bookinventory.lookup import find_book, find_matches
from bookinventory.ocr import OcrLine
from bookinventory.scanner import (
    Candidate,
    ScanResult,
    identify_books,
    merge_results,
    scan_lines,
)
from tests.conftest import BLANK_PHOTO, SHELF_A, SHELF_B


def lookup_with(fake_openlibrary):
    return lambda query: find_matches(query, fetch=fake_openlibrary)


def finder(fake_openlibrary):
    return lambda query: find_book(query, fetch=fake_openlibrary)


def spine(*texts, x=0.1, y=0.5, orientation=6):
    """Stacked lines forming one spine."""
    return [OcrLine(t, 0.9, orientation, x, y - i * 0.06, 0.3, 0.05) for i, t in enumerate(texts)]


class TestCandidate:
    def test_next_alternative_cycles(self):
        c = Candidate(Book("A"), "q", 1.0, alternatives=[Book("B"), Book("C")])
        assert c.next_alternative()
        assert c.book.title == "B" and [b.title for b in c.alternatives] == ["C", "A"]
        c.next_alternative()
        c.next_alternative()
        assert c.book.title == "A"

    def test_next_alternative_without_alternatives(self):
        c = Candidate(Book("A"), "q", 1.0)
        assert not c.next_alternative()
        assert c.book.title == "A"

    def test_use_alternative(self):
        c = Candidate(Book("A"), "q", 1.0, alternatives=[Book("B"), Book("C")])
        c.use_alternative(1)
        assert c.book.title == "C" and [b.title for b in c.alternatives] == ["A", "B"]


class TestScanResult:
    def test_books_respects_inclusion_and_dedupes(self):
        result = ScanResult(
            candidates=[
                Candidate(Book("A"), "a", 1.0),
                Candidate(Book("B"), "b", 1.0, included=False),
                Candidate(Book("a"), "a2", 0.9),  # same text identity as A
            ]
        )
        assert [b.title for b in result.books] == ["A"]
        assert result.included == 2

    def test_describe(self):
        assert ScanResult().describe() == "No books recognised."
        one = ScanResult(candidates=[Candidate(Book("A"), "a", 1.0)])
        assert one.describe() == "Found 1 book."
        two = ScanResult(candidates=[Candidate(Book("A"), "a", 1.0)] * 2, unmatched=["x"])
        assert two.describe() == "Found 2 books; 1 line not matched."


class TestScanLines:
    def test_spine_text_is_looked_up_as_one_query(self, fake_openlibrary):
        result = scan_lines(spine("DUNE", "Frank Herbert"), lookup=lookup_with(fake_openlibrary))
        assert [c.book.title for c in result.candidates] == ["Dune"]
        assert result.candidates[0].query == "DUNE Frank Herbert"
        # The combined text and the individual lines go out in one batch.
        assert set(fake_openlibrary.queries) == {"DUNE Frank Herbert", "DUNE", "Frank Herbert"}
        assert result.unmatched == []

    def test_alternatives_are_offered(self, fake_openlibrary):
        result = scan_lines([OcrLine("Dune")], lookup=lookup_with(fake_openlibrary))
        [candidate] = result.candidates
        assert candidate.book.title == "Dune"
        assert candidate.alternatives and all("Dune" in b.title for b in candidate.alternatives)
        assert len(candidate.alternatives) <= 4

    def test_over_grouped_spines_fall_back_to_lines(self, fake_openlibrary):
        # Two neighbouring spines read as one group: the combined query fails,
        # the individual lines succeed.
        lines = spine("Animal Farm", "Fahrenheit 451")
        result = scan_lines(lines, lookup=lookup_with(fake_openlibrary))
        assert sorted(c.book.title for c in result.candidates) == ["Animal Farm", "Fahrenheit 451"]
        assert set(fake_openlibrary.queries) == {
            "Animal Farm Fahrenheit 451",
            "Animal Farm",
            "Fahrenheit 451",
        }
        assert result.unmatched == []

    def test_confident_spine_match_is_trusted(self, fake_openlibrary):
        result = scan_lines(spine("DUNE", "Frank Herbert"), lookup=lookup_with(fake_openlibrary))
        assert [c.book.title for c in result.candidates] == ["Dune"]
        assert result.unmatched == []  # the author line is not reported as unmatched

    def test_unmatched_multi_line_spine_reports_its_lines(self, fake_openlibrary):
        result = scan_lines(spine("Garbled", "Nonsense"), lookup=lookup_with(fake_openlibrary))
        assert result.candidates == []
        assert result.unmatched == ["Garbled", "Nonsense"]

    def test_author_only_spine_is_unmatched(self, fake_openlibrary):
        result = scan_lines([OcrLine("J.R.R. TOLKIEN")], lookup=lookup_with(fake_openlibrary))
        assert result.candidates == []
        assert result.unmatched == ["J.R.R. TOLKIEN"]

    def test_plain_strings_and_noise(self, fake_openlibrary):
        result = scan_lines([OcrLine(t) for t in SHELF_A], lookup=lookup_with(fake_openlibrary))
        assert [c.book.title for c in result.candidates] == [
            "The Hobbit",
            "Dune",
            "Nineteen Eighty-Four",
        ]
        assert "9780441013593" not in fake_openlibrary.queries  # ISBN never looked up
        assert fake_openlibrary.queries.count("DUNE") == 1  # duplicate line, one request
        assert result.line_count == len(SHELF_A)
        assert "PENGUIN" in result.unmatched and "J.R.R. TOLKIEN" in result.unmatched

    def test_same_book_from_two_spines_kept_once_with_best_query(self, fake_openlibrary):
        lines = spine("DUNE xyzzy", y=0.9) + spine("Dune Frank Herbert", y=0.3)
        result = scan_lines(lines, lookup=lookup_with(fake_openlibrary))
        [candidate] = result.candidates
        assert candidate.query == "Dune Frank Herbert"  # the higher-scoring read
        assert candidate.score == 1.0

    def test_blank_photo(self, fake_openlibrary):
        result = scan_lines(BLANK_PHOTO, lookup=lookup_with(fake_openlibrary))
        assert result.candidates == [] and result.line_count == 0
        assert fake_openlibrary.queries == []

    def test_lookup_outage(self, fake_openlibrary):
        fake_openlibrary.fail_with = OSError("offline")
        result = scan_lines([OcrLine(t) for t in SHELF_B], lookup=lookup_with(fake_openlibrary))
        assert result.candidates == []
        assert len(result.unmatched) == 3

    def test_lookups_run_concurrently(self, fake_openlibrary):
        import threading

        names = set()
        lock = threading.Lock()

        def lookup(query):
            with lock:
                names.add(threading.current_thread().name)
            return find_matches(query, fetch=fake_openlibrary)

        scan_lines([OcrLine(t) for t in SHELF_B], lookup=lookup, workers=3)
        assert "MainThread" not in names


class TestMergeResults:
    def test_merges_photos_of_one_shelf(self):
        a = ScanResult(
            candidates=[Candidate(Book("Dune", work_key="/w/1"), "DUNE", 0.8)],
            unmatched=["x"],
            line_count=3,
        )
        b = ScanResult(
            candidates=[
                Candidate(Book("Dune", work_key="/w/1"), "Dune Herbert", 1.0),
                Candidate(Book("Emma"), "Emma", 1.0),
            ],
            unmatched=["x", "y"],
            line_count=4,
        )
        merged = merge_results([a, b])
        assert [c.book.title for c in merged.candidates] == ["Dune", "Emma"]
        assert merged.candidates[0].query == "Dune Herbert"
        assert merged.unmatched == ["x", "y"]
        assert merged.line_count == 7

    def test_empty(self):
        assert merge_results([]).candidates == []


class TestIdentifyBooks:
    def test_line_by_line(self, fake_openlibrary):
        books = identify_books(SHELF_A, find=finder(fake_openlibrary))
        assert [b.title for b in books] == ["The Hobbit", "Dune", "Nineteen Eighty-Four"]

    def test_dedupes_by_identity(self):
        dune = Book("Dune", "Frank Herbert")
        shouting = Book("DUNE", "FRANK HERBERT", "1965")
        books = identify_books(
            ["Dune", "Frank Herbert", "junk line"],
            find={"Dune": dune, "Frank Herbert": shouting}.get,
        )
        assert books == [dune]

    def test_default_finder_is_find_book(self):
        assert identify_books.__defaults__ == (find_book,)
