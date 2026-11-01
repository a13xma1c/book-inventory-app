"""Tests for bookinventory.aggregation: the inventory aggregation feature."""

import csv
import io
import json

import pytest

from bookinventory.aggregation import (
    CSV_COLUMNS,
    UNKNOWN_AUTHOR,
    UNKNOWN_LOCATION,
    InventoryEntry,
    build_inventory,
    entry_to_dict,
    find_duplicates,
    group_by_author,
    group_by_decade,
    group_by_location,
    merge_books,
    search,
    summarize,
    to_csv,
    to_json,
)
from bookinventory.database import Book


class TestMergeBooks:
    def test_collapses_duplicates_across_photos(self):
        photo1 = [Book("Dune", "Frank Herbert"), Book("Emma", "Jane Austen", "1815")]
        photo2 = [
            Book("DUNE", "frank herbert", "1965", "9780441013593"),
            Book("Persuasion", "Jane Austen"),
        ]
        merged = merge_books(photo1, photo2)
        assert [b.title for b in merged] == ["Dune", "Emma", "Persuasion"]
        dune = merged[0]
        assert (dune.year, dune.isbn) == ("1965", "9780441013593")  # filled from photo 2

    def test_does_not_overwrite_existing_metadata(self):
        merged = merge_books([Book("Dune", "", "1965")], [Book("Dune", "", "2021")])
        assert merged[0].year == "1965"

    def test_returns_copies_not_the_inputs(self):
        original = Book("Dune", "Frank Herbert")
        [merged] = merge_books([original], [Book("Dune", "Frank Herbert", "1965")])
        assert merged.year == "1965"
        assert original.year == ""

    def test_no_photos(self):
        assert merge_books() == []
        assert merge_books([], []) == []

    def test_single_photo_dedupes(self):
        assert len(merge_books([Book("Dune"), Book("dune")])) == 1


class TestInventoryEntry:
    def test_properties_delegate_to_book(self, dune):
        entry = InventoryEntry(dune, ("Study",), 2, "2024-01-01", "2024-01-02")
        assert (entry.title, entry.authors, entry.quantity) == ("Dune", "Frank Herbert", 1)

    @pytest.mark.parametrize(
        ("year", "decade"),
        [
            ("1965", "1960s"),
            ("1813", "1810s"),
            ("2000", "2000s"),
            ("c. 1999", "1990s"),
            ("", "unknown"),
            ("n.d.", "unknown"),
            ("99", "unknown"),
        ],
    )
    def test_decade(self, year, decade):
        assert InventoryEntry(Book("X", year=year)).decade == decade

    def test_frozen(self, dune):
        entry = InventoryEntry(dune)
        with pytest.raises(AttributeError):
            entry.locations = ("x",)  # type: ignore[misc]


class TestBuildInventory:
    def test_joins_books_with_sightings(self, db, hobbit, dune):
        db.record_scan([hobbit, dune], "Study")
        db.record_scan([dune], "Study")
        db.record_scan([dune], "bedroom")
        entries = build_inventory(db)
        assert [e.title for e in entries] == ["Dune", "The Hobbit"]
        dune_entry, hobbit_entry = entries
        assert dune_entry.quantity == 2
        assert dune_entry.times_seen == 3
        assert dune_entry.locations == ("bedroom", "Study")  # case-insensitive sort
        assert dune_entry.first_seen <= dune_entry.last_seen
        assert dune_entry.first_seen == db.scans()[0].scanned_at
        assert dune_entry.last_seen == db.scans()[2].scanned_at
        assert hobbit_entry.times_seen == 1
        assert hobbit_entry.locations == ("Study",)

    def test_books_added_manually_have_no_sightings(self, db, dune):
        db.add(dune)
        [entry] = build_inventory(db)
        assert entry.times_seen == 0
        assert entry.locations == ()
        assert entry.first_seen == "" and entry.last_seen == ""

    def test_unspecified_location_label(self, db, dune):
        db.record_scan([dune])
        assert build_inventory(db)[0].locations == (UNKNOWN_LOCATION,)

    def test_empty(self, db):
        assert build_inventory(db) == []


@pytest.fixture
def library(db, hobbit, dune, orwell):
    """A small inventory exercising every branch of the summaries."""
    farm = Book("Animal Farm", "George Orwell", "1945")
    zine = Book("Untitled Zine")
    db.record_scan([hobbit, dune], "Study")
    db.record_scan([dune], "Study")  # overlap
    db.record_scan([dune, orwell, farm], "Bedroom")
    db.record_scan([zine])
    hobbit_id = next(b.id for b in db.all() if b.title == "The Hobbit")
    db.set_quantity(hobbit_id, 3)  # the user says they own three copies
    return build_inventory(db)


class TestSummarize:
    def test_headline_numbers(self, library):
        summary = summarize(library)
        assert summary.titles == 5
        assert summary.copies == 3 + 2 + 1 + 1 + 1
        assert summary.locations == 3  # Study, Bedroom, (unspecified)
        assert summary.duplicates == 2  # Hobbit (3) and Dune (2)

    def test_copies_by_location(self, library):
        assert summarize(library).copies_by_location == {
            UNKNOWN_LOCATION: 1,
            "Bedroom": 3,  # Dune, 1984, Animal Farm
            "Study": 4,  # Hobbit x3, Dune
        }

    def test_titles_by_author_sorted_by_count_then_name(self, library):
        assert list(summarize(library).titles_by_author.items()) == [
            ("George Orwell", 2),
            (UNKNOWN_AUTHOR, 1),
            ("Frank Herbert", 1),
            ("J.R.R. Tolkien", 1),
        ]

    def test_titles_by_decade_unknown_last(self, library):
        assert summarize(library).titles_by_decade == {
            "1930s": 1,
            "1940s": 2,
            "1960s": 1,
            "unknown": 1,
        }

    def test_describe(self, library):
        assert summarize(library).describe() == "5 titles, 8 copies, 3 locations, 2 duplicated"

    def test_describe_empty_and_singular(self, dune):
        assert summarize([]).describe() == "0 titles, 0 copies"
        assert summarize([InventoryEntry(dune, ("A",))]).describe() == "1 title, 1 copy, 1 location"

    def test_surplus_copies_go_to_first_location(self):
        entry = InventoryEntry(Book("X", quantity=5), ("A", "B"))
        assert summarize([entry]).copies_by_location == {"A": 3, "B": 2}

    def test_zero_quantity(self):
        entry = InventoryEntry(Book("X", quantity=0), ("A",))
        summary = summarize([entry])
        assert summary.copies == 0
        assert summary.copies_by_location == {"A": 0}
        assert summary.duplicates == 0

    def test_entries_without_locations_dont_count_as_a_location(self, dune):
        assert summarize([InventoryEntry(dune)]).locations == 0


class TestGrouping:
    def test_group_by_author(self, library):
        groups = group_by_author(library)
        assert list(groups) == [UNKNOWN_AUTHOR, "Frank Herbert", "George Orwell", "J.R.R. Tolkien"]
        assert [e.title for e in groups["George Orwell"]] == ["Animal Farm", "Nineteen Eighty-Four"]

    def test_group_by_location_lists_multi_location_titles_under_each(self, library):
        groups = group_by_location(library)
        assert list(groups) == [UNKNOWN_LOCATION, "Bedroom", "Study"]
        assert "Dune" in [e.title for e in groups["Bedroom"]]
        assert "Dune" in [e.title for e in groups["Study"]]
        assert [e.title for e in groups[UNKNOWN_LOCATION]] == ["Untitled Zine"]

    def test_group_by_decade(self, library):
        groups = group_by_decade(library)
        assert list(groups) == ["1930s", "1940s", "1960s", "unknown"]
        assert [e.title for e in groups["1940s"]] == ["Animal Farm", "Nineteen Eighty-Four"]

    def test_grouping_empty(self):
        assert (
            group_by_author([]) == {} and group_by_location([]) == {} and group_by_decade([]) == {}
        )

    def test_find_duplicates_most_copies_first(self, library):
        assert [(e.title, e.quantity) for e in find_duplicates(library)] == [
            ("The Hobbit", 3),
            ("Dune", 2),
        ]

    def test_find_duplicates_none(self, dune):
        assert find_duplicates([InventoryEntry(dune)]) == []


class TestSearch:
    def test_title_author_isbn(self, library):
        assert [e.title for e in search(library, "orwell")] == [
            "Animal Farm",
            "Nineteen Eighty-Four",
        ]
        assert [e.title for e in search(library, "HOBBIT")] == ["The Hobbit"]
        assert [e.title for e in search(library, "9780441013593")] == ["Dune"]

    def test_blank_returns_everything(self, library):
        assert search(library, "   ") == library

    def test_whitespace_normalised(self, library):
        assert [e.title for e in search(library, "  eighty   four ")] == []  # hyphenated in title
        assert [e.title for e in search(library, "eighty-four")] == ["Nineteen Eighty-Four"]

    def test_no_hits(self, library):
        assert search(library, "zzz") == []


class TestExport:
    def test_entry_to_dict(self, dune):
        entry = InventoryEntry(dune, ("A", "B"), 2, "t1", "t2")
        assert entry_to_dict(entry) == {
            "title": "Dune",
            "authors": "Frank Herbert",
            "year": "1965",
            "isbn": "9780441013593",
            "quantity": 1,
            "locations": ["A", "B"],
            "times_seen": 2,
            "first_seen": "t1",
            "last_seen": "t2",
        }

    def test_csv(self, library):
        rows = list(csv.DictReader(io.StringIO(to_csv(library))))
        assert [r["title"] for r in rows] == [
            "Animal Farm",
            "Dune",
            "Nineteen Eighty-Four",
            "The Hobbit",
            "Untitled Zine",
        ]
        dune = rows[1]
        assert dune["authors"] == "Frank Herbert"
        assert dune["year"] == "1965"
        assert dune["isbn"] == "9780441013593"
        assert dune["quantity"] == "2"
        assert dune["locations"] == "Bedroom; Study"
        assert dune["times_seen"] == "3"
        assert dune["first_seen"] and dune["last_seen"]
        zine = rows[-1]
        assert (zine["year"], zine["isbn"], zine["locations"]) == ("", "", UNKNOWN_LOCATION)

    def test_csv_header_only_when_empty(self):
        assert to_csv([]).splitlines() == [",".join(CSV_COLUMNS)]

    def test_csv_escaping(self):
        entry = InventoryEntry(Book('Say "Hi", Please', "A, B"), ("Hall, left",))
        [row] = list(csv.DictReader(io.StringIO(to_csv([entry]))))
        assert row["title"] == 'Say "Hi", Please'
        assert row["authors"] == "A, B"
        assert row["locations"] == "Hall, left"

    def test_json(self, library):
        document = json.loads(to_json(library))
        assert document["summary"] == {"titles": 5, "copies": 8}
        dune = next(b for b in document["books"] if b["title"] == "Dune")
        assert dune["locations"] == ["Bedroom", "Study"]
        assert dune["quantity"] == 2

    def test_json_keeps_unicode_and_honours_indent(self):
        text = to_json([InventoryEntry(Book("Crème Brûlée"))], indent=None)
        assert "Crème Brûlée" in text
        assert "\n" not in text
