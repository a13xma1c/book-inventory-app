"""End-to-end: OCR lines -> lookup -> scanner -> database -> aggregation -> export.

Only the camera/Vision output and the network are faked.
"""

import csv
import io
import json

from bookinventory.aggregation import (
    build_inventory,
    find_duplicates,
    group_by_location,
    merge_books,
    summarize,
    to_csv,
    to_json,
)
from bookinventory.database import Database
from bookinventory.lookup import find_book
from bookinventory.scanner import identify_books
from tests.conftest import BLANK_PHOTO, SHELF_A, SHELF_A_OVERLAP, SHELF_B, FakeOpenLibrary


def scan(ol, lines):
    return identify_books(lines, find=lambda q: find_book(q, fetch=ol))


def test_two_shelves_with_overlapping_photos():
    ol = FakeOpenLibrary()
    db = Database()

    for photo in (SHELF_A, SHELF_A_OVERLAP):
        db.record_scan(scan(ol, photo), "Study")
    report = db.record_scan(scan(ol, SHELF_B), "Bedroom")
    assert report.describe() == "Found 3 books (2 new, 1 extra copy)."

    entries = {e.title: e for e in build_inventory(db)}
    assert set(entries) == {
        "The Hobbit",
        "Dune",
        "Nineteen Eighty-Four",
        "Animal Farm",
        "Fahrenheit 451",
        "Pride and Prejudice",
    }
    assert entries["Dune"].times_seen == 3
    assert entries["Dune"].quantity == 2
    assert entries["Dune"].locations == ("Bedroom", "Study")
    assert entries["Nineteen Eighty-Four"].times_seen == 2
    assert entries["Nineteen Eighty-Four"].quantity == 1
    assert (entries["Dune"].book.year, entries["Dune"].book.isbn) == ("1965", "9780441013593")

    summary = summarize(entries.values())
    assert (summary.titles, summary.copies, summary.locations, summary.duplicates) == (6, 7, 2, 1)
    assert summary.copies_by_location == {"Bedroom": 3, "Study": 4}
    assert [e.title for e in find_duplicates(entries.values())] == ["Dune"]
    assert set(group_by_location(entries.values())) == {"Bedroom", "Study"}

    rows = list(csv.DictReader(io.StringIO(to_csv(entries.values()))))
    assert {r["title"]: r["quantity"] for r in rows}["Dune"] == "2"
    assert json.loads(to_json(entries.values()))["summary"] == {"titles": 6, "copies": 7}
    db.close()


def test_merge_photos_of_one_shelf_before_recording():
    """Merging several photos of one shelf first gives a single scan record."""
    ol = FakeOpenLibrary()
    db = Database()
    books = merge_books(scan(ol, SHELF_A), scan(ol, SHELF_A_OVERLAP))
    report = db.record_scan(books, "Study")
    assert report.found == 4 and report.new_titles == 4
    assert len(db.scans()) == 1
    assert all(e.times_seen == 1 for e in build_inventory(db))
    db.close()


def test_undo_and_blank_scan():
    ol = FakeOpenLibrary()
    db = Database()
    first = db.record_scan(scan(ol, SHELF_A), "Study")
    empty = db.record_scan(scan(ol, BLANK_PHOTO), "Study")
    assert empty.found == 0
    assert db.count() == 3
    assert db.delete_scan(first.scan_id)
    assert db.count() == 0
    assert [s.id for s in db.scans()] == [empty.scan_id]
    db.close()


def test_lookup_cache_is_not_needed_but_queries_are_bounded():
    """Every plausible OCR line costs at most one lookup."""
    ol = FakeOpenLibrary()
    scan(ol, SHELF_A)
    assert len(ol.queries) == len({q.lower() for q in ol.queries})
    assert len(ol.queries) <= len(SHELF_A)


def test_review_pipeline_end_to_end():
    """OCR lines with geometry -> spines -> concurrent cached lookups -> review -> inventory."""
    from bookinventory.lookup import CachedFetch, find_matches
    from bookinventory.ocr import OcrLine
    from bookinventory.scanner import merge_results, scan_lines

    ol = FakeOpenLibrary()
    fetch = CachedFetch(ol)

    def lookup(query):
        return find_matches(query, fetch=fetch)

    def stacked(*texts, y):
        return [OcrLine(t, 0.9, 6, 0.1, y - i * 0.06, 0.3, 0.05) for i, t in enumerate(texts)]

    photo_1 = stacked("DUNE", "Frank Herbert", y=0.9) + stacked("THE HOBBIT", "Tolkien", y=0.5)
    photo_2 = stacked("DUNE", "Frank Herbert", y=0.8) + [OcrLine("9780451524935", 0.99)]
    result = merge_results([scan_lines(photo_1, lookup), scan_lines(photo_2, lookup)])

    assert [c.book.title for c in result.candidates] == ["Dune", "The Hobbit"]
    assert result.candidates[0].query == "DUNE Frank Herbert"
    assert result.unmatched == []
    # The second photo's identical spine cost no extra requests.
    assert fetch.requests == len({q.lower() for q in ol.queries})

    result.candidates[1].included = False
    db = Database()
    report = db.record_scan(result.books, "Study")
    assert report.describe() == "Found 1 book (1 new)."
    [dune] = db.all()
    assert dune.work_key == "/works/OL893415W"
    assert dune.cover_url.endswith("12369637-M.jpg")
    db.close()
