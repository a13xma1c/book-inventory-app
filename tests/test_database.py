"""Tests for bookinventory.database: storage, aggregation on write, migration."""

import sqlite3

import pytest

from bookinventory.database import SCHEMA_VERSION, Book, Database, Scan, ScanReport


class TestBook:
    def test_defaults(self):
        book = Book("Dune")
        assert (book.authors, book.year, book.isbn, book.id, book.quantity) == ("", "", "", None, 1)

    def test_key_is_case_and_whitespace_insensitive(self):
        assert Book("DUNE ", " frank herbert").key == Book("Dune", "Frank Herbert").key
        assert Book("Dune", "Frank Herbert").key != Book("Dune", "Brian Herbert").key


class TestScanReport:
    @pytest.mark.parametrize(
        ("report", "text"),
        [
            (ScanReport(1, "", 0, 0, 0, 0), "No books found."),
            (ScanReport(1, "", 1, 1, 0, 0), "Found 1 book (1 new)."),
            (
                ScanReport(1, "", 3, 1, 1, 1),
                "Found 3 books (1 new, 1 extra copy, 1 already listed).",
            ),
            (ScanReport(1, "", 2, 0, 2, 0), "Found 2 books (2 extra copies)."),
            (ScanReport(1, "", 2, 0, 0, 2), "Found 2 books (2 already listed)."),
        ],
    )
    def test_describe(self, report, text):
        assert report.describe() == text


class TestSchema:
    def test_fresh_database_is_current_version(self, db):
        assert db.schema_version == SCHEMA_VERSION

    def test_tables_exist(self, db):
        names = {
            r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"books", "scans", "sightings"} <= names

    def test_reopen_is_idempotent(self, tmp_path):
        path = str(tmp_path / "books.sqlite3")
        Database(path).close()
        db = Database(path)
        assert db.schema_version == SCHEMA_VERSION
        db.close()

    def test_migrates_version_1_database(self, tmp_path):
        """A database created by the previous release keeps its books."""
        path = str(tmp_path / "books.sqlite3")
        conn = sqlite3.connect(path)
        conn.execute(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                authors TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                isbn TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (title COLLATE NOCASE, authors COLLATE NOCASE)
            )
            """
        )
        conn.execute(
            "INSERT INTO books (title, authors, year) VALUES ('Dune', 'Frank Herbert', '1965')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        assert db.schema_version == SCHEMA_VERSION
        [dune] = db.all()
        assert (dune.title, dune.year, dune.quantity) == ("Dune", "1965", 1)
        assert db.total_copies() == 1
        assert db.scans() == []
        # New functionality works on the migrated data.
        report = db.record_scan([Book("Dune", "Frank Herbert")], "Study")
        assert report.new_copies == 1
        assert db.get(dune.id).quantity == 2
        db.close()

    def test_quantity_cannot_go_negative_at_the_sql_level(self, db, dune):
        db.add(dune)
        with pytest.raises(sqlite3.IntegrityError):
            db._conn.execute("UPDATE books SET quantity = -1")

    def test_foreign_keys_cascade(self, db, dune):
        report = db.record_scan([dune], "Study")
        db.delete(db.all()[0].id)
        assert db.sightings() == []
        assert db.scans()[0].id == report.scan_id  # the scan itself remains


class TestAdd:
    def test_add_and_duplicate(self, db):
        assert db.add(Book("Dune", "Frank Herbert"))
        assert not db.add(Book("DUNE", "frank herbert"))
        assert db.count() == 1

    def test_add_respects_quantity(self, db):
        db.add(Book("Dune", quantity=3))
        assert db.all()[0].quantity == 3

    def test_add_floors_quantity_at_one(self, db):
        db.add(Book("Dune", quantity=0))
        assert db.all()[0].quantity == 1

    def test_add_creates_no_scan(self, db, dune):
        db.add(dune)
        assert db.scans() == [] and db.sightings() == []


class TestRecordScan:
    def test_new_titles(self, db, sample_books):
        report = db.record_scan(sample_books, "Study")
        assert report == ScanReport(1, "Study", 3, 3, 0, 0)
        assert db.count() == 3
        assert db.total_copies() == 3
        assert db.scans() == [Scan(1, db.scans()[0].scanned_at, "Study", 3)]
        assert len(db.sightings()) == 3
        assert db.scans()[0].scanned_at.endswith("+00:00")

    def test_dedupes_within_a_scan(self, db):
        report = db.record_scan([Book("Dune", "Frank Herbert"), Book("DUNE", "frank herbert")])
        assert report.found == 1
        assert db.total_copies() == 1

    def test_overlapping_photo_same_location_does_not_add_copies(self, db, dune):
        db.record_scan([dune], "Study")
        report = db.record_scan([dune], "Study")
        assert report == ScanReport(2, "Study", 1, 0, 0, 1)
        assert db.get(db.all()[0].id).quantity == 1

    def test_new_location_adds_a_copy(self, db, dune):
        db.record_scan([dune], "Study")
        report = db.record_scan([dune], "Bedroom")
        assert report == ScanReport(2, "Bedroom", 1, 0, 1, 0)
        assert db.all()[0].quantity == 2

    def test_location_matching_is_case_and_space_insensitive(self, db, dune):
        db.record_scan([dune], "Study")
        report = db.record_scan([dune], "  STUDY ")
        assert report.already_seen == 1
        assert report.location == "Study"  # stored spelling reused
        assert db.locations() == ["Study"]

    def test_unspecified_location_counts_as_one_place(self, db, dune):
        db.record_scan([dune])
        db.record_scan([dune], "   ")
        assert db.all()[0].quantity == 1
        assert db.locations() == []

    def test_mixed_report(self, db, hobbit, dune, orwell):
        db.record_scan([hobbit, dune], "Study")
        report = db.record_scan([hobbit, dune, orwell], "Hall")
        # hobbit+dune: new location -> extra copies; orwell: new.
        assert (report.new_titles, report.new_copies, report.already_seen) == (1, 2, 0)
        report = db.record_scan([hobbit, orwell], "Hall")
        assert (report.new_titles, report.new_copies, report.already_seen) == (0, 0, 2)

    def test_fills_in_missing_metadata_without_overwriting(self, db):
        db.record_scan([Book("Dune", "Frank Herbert")], "A")
        db.record_scan([Book("Dune", "Frank Herbert", "1965", "9780441013593")], "B")
        db.record_scan([Book("Dune", "Frank Herbert", "2021", "0000000000")], "C")
        [dune] = db.all()
        assert (dune.year, dune.isbn) == ("1965", "9780441013593")

    def test_existing_book_added_via_add_then_scanned(self, db, dune):
        db.add(dune)
        report = db.record_scan([dune], "Study")
        # Never seen anywhere before, so the first sighting location is new.
        assert report.new_copies == 1
        assert db.all()[0].quantity == 2

    def test_empty_scan_is_recorded(self, db):
        report = db.record_scan([], "Empty shelf")
        assert report.found == 0
        assert db.scans()[0].books_found == 0

    def test_rollback_on_failure(self, db, dune, monkeypatch):
        def explode(*args, **kwargs):
            raise sqlite3.OperationalError("disk full")

        monkeypatch.setattr(db, "_fill_metadata", explode)
        db.add(dune)
        with pytest.raises(sqlite3.OperationalError):
            db.record_scan([dune], "Study")
        assert db.scans() == []
        assert db.all()[0].quantity == 1


class TestQuantities:
    def test_set_quantity(self, db, dune):
        db.add(dune)
        book_id = db.all()[0].id
        db.set_quantity(book_id, 4)
        assert db.get(book_id).quantity == 4
        db.set_quantity(book_id, 0)
        assert db.get(book_id).quantity == 0

    def test_set_quantity_negative(self, db, dune):
        db.add(dune)
        with pytest.raises(ValueError):
            db.set_quantity(db.all()[0].id, -1)

    def test_set_quantity_unknown(self, db):
        with pytest.raises(KeyError):
            db.set_quantity(999, 1)

    def test_adjust_quantity(self, db, dune):
        db.add(dune)
        book_id = db.all()[0].id
        assert db.adjust_quantity(book_id, +2) == 3
        assert db.adjust_quantity(book_id, -1) == 2
        assert db.adjust_quantity(book_id, -10) == 0  # floored
        assert db.total_copies() == 0

    def test_adjust_quantity_unknown(self, db):
        with pytest.raises(KeyError):
            db.adjust_quantity(999, 1)

    def test_manual_quantity_survives_rescan_of_known_location(self, db, dune):
        db.record_scan([dune], "Study")
        book_id = db.all()[0].id
        db.set_quantity(book_id, 5)
        db.record_scan([dune], "Study")
        assert db.get(book_id).quantity == 5
        db.record_scan([dune], "Hall")
        assert db.get(book_id).quantity == 6


class TestDeletion:
    def test_delete_book(self, db, sample_books):
        db.record_scan(sample_books)
        db.delete(db.all()[0].id)
        assert db.count() == 2
        assert len(db.sightings()) == 2

    def test_delete_unknown_is_noop(self, db):
        db.delete(999)

    def test_delete_scan_removes_books_only_seen_there(self, db, hobbit, dune):
        first = db.record_scan([hobbit, dune], "Study")
        db.record_scan([dune], "Hall")
        assert db.delete_scan(first.scan_id)
        assert [b.title for b in db.all()] == ["Dune"]
        assert db.all()[0].quantity == 1  # lost the Study copy
        assert db.scans()[0].location == "Hall"

    def test_delete_scan_keeps_copy_when_location_still_sighted(self, db, dune):
        db.record_scan([dune], "Study")
        second = db.record_scan([dune], "Study")  # overlap photo, no copy added
        db.record_scan([dune], "Hall")
        assert db.all()[0].quantity == 2
        db.delete_scan(second.scan_id)
        assert db.all()[0].quantity == 2

    def test_delete_scan_never_drops_quantity_below_one(self, db, dune):
        db.record_scan([dune], "Study")
        db.record_scan([dune], "Hall")
        db.set_quantity(db.all()[0].id, 1)
        db.delete_scan(1)
        assert db.all()[0].quantity == 1

    def test_delete_unknown_scan(self, db):
        assert db.delete_scan(999) is False

    def test_clear(self, db, sample_books):
        db.record_scan(sample_books, "Study")
        db.clear()
        assert db.count() == 0 and db.scans() == [] and db.sightings() == []


class TestLocations:
    def test_locations_distinct_sorted(self, db, dune):
        db.record_scan([dune], "study")
        db.record_scan([dune], "Bedroom")
        db.record_scan([dune], "Study")
        db.record_scan([dune], "")
        assert db.locations() == ["Bedroom", "study"]

    def test_rename_location(self, db, dune, hobbit):
        db.record_scan([dune], "Study")
        db.record_scan([hobbit], "study")
        db.record_scan([dune], "Hall")
        assert db.rename_location("STUDY", "Office") == 2
        assert db.rename_location("Nowhere", "X") == 0
        assert db.locations() == ["Hall", "Office"]


class TestReads:
    def test_all_sorted_by_title_then_author(self, db):
        db.add(Book("dune", "Frank Herbert"))
        db.add(Book("Dune", "Brian Herbert"))
        db.add(Book("Animal Farm", "George Orwell"))
        assert [(b.title, b.authors) for b in db.all()] == [
            ("Animal Farm", "George Orwell"),
            ("Dune", "Brian Herbert"),
            ("dune", "Frank Herbert"),
        ]

    def test_get(self, db, dune):
        db.add(dune)
        book = db.get(db.all()[0].id)
        assert (book.title, book.authors, book.year, book.isbn, book.quantity) == (
            "Dune",
            "Frank Herbert",
            "1965",
            "9780441013593",
            1,
        )
        assert db.get(999) is None

    def test_count_vs_total_copies(self, db, dune, hobbit):
        db.record_scan([dune, hobbit], "A")
        db.record_scan([dune], "B")
        assert db.count() == 2
        assert db.total_copies() == 3

    def test_total_copies_empty(self, db):
        assert db.total_copies() == 0

    def test_sightings(self, db, dune, hobbit):
        db.record_scan([dune, hobbit], "A")
        db.record_scan([dune], "B")
        ids = {b.title: b.id for b in db.all()}
        rows = [(book_id, scan_id, loc) for book_id, scan_id, loc, _ in db.sightings()]
        assert rows == [(ids["Dune"], 1, "A"), (ids["The Hobbit"], 1, "A"), (ids["Dune"], 2, "B")]

    def test_file_database_persists(self, tmp_path, dune):
        path = str(tmp_path / "books.sqlite3")
        db = Database(path)
        db.record_scan([dune], "Study")
        db.close()
        db = Database(path)
        assert db.all()[0].title == "Dune"
        assert db.locations() == ["Study"]
        db.close()


class TestIdentity:
    def test_key_preference_order(self):
        assert Book("Dune", "Herbert", isbn="978-0-441-01359-3", work_key="/works/OL1W").key == (
            "work:/works/OL1W"
        )
        assert Book("Dune", "Herbert", isbn="978-0-441-01359-3").key == "isbn:9780441013593"
        assert Book("The Dune!", " Frank  Herbert ").key == "text:the dune|frank herbert"
        assert Book("Dune").text_key == "text:dune|"

    def test_editions_with_different_author_spelling_merge_by_work(self, db):
        db.record_scan([Book("Dune", "Frank Herbert", work_key="/works/OL1W")], "A")
        report = db.record_scan([Book("Dune", "Herbert, Frank", work_key="/works/OL1W")], "B")
        assert report.new_titles == 0 and report.new_copies == 1
        assert db.count() == 1

    def test_older_text_identity_is_upgraded(self, db):
        db.add(Book("Dune", "Frank Herbert"))  # e.g. migrated from v2, no work key
        db.record_scan([Book("DUNE", "frank herbert", work_key="/works/OL1W")], "A")
        [dune] = db.all()
        assert dune.work_key == "/works/OL1W"
        assert db.count() == 1
        # ...and the stronger identity is what's matched from now on.
        db.record_scan([Book("Dune (Deluxe)", "F. Herbert", work_key="/works/OL1W")], "B")
        assert db.count() == 1 and db.all()[0].quantity == 3  # manual add + 2 locations

    def test_cover_and_work_key_filled_in_later(self, db):
        db.add(Book("Dune", "Frank Herbert"))
        db.record_scan([Book("Dune", "Frank Herbert", cover_url="http://c/1.jpg")], "A")
        [dune] = db.all()
        assert dune.cover_url == "http://c/1.jpg"

    def test_add_returns_false_for_identity_duplicate(self, db):
        assert db.add(Book("Dune", "Frank Herbert", work_key="/w"))
        assert not db.add(Book("Other title", "Other", work_key="/w"))


class TestSettings:
    def test_get_set(self, db):
        assert db.get_setting("last_location") == ""
        assert db.get_setting("x", "default") == "default"
        db.set_setting("last_location", "Study")
        db.set_setting("last_location", "Hall")
        assert db.get_setting("last_location") == "Hall"

    def test_clear_resets_settings(self, db):
        db.set_setting("k", "v")
        db.clear()
        assert db.get_setting("k") == ""

    def test_last_scan(self, db, dune):
        assert db.last_scan() is None
        db.record_scan([dune], "A")
        last = db.record_scan([dune], "B")
        assert db.last_scan().id == last.scan_id


class TestMigrationV2ToV3:
    def test_v2_database_gains_identity_columns(self, tmp_path):
        path = str(tmp_path / "books.sqlite3")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE books (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                authors TEXT NOT NULL DEFAULT '',
                year TEXT NOT NULL DEFAULT '',
                isbn TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                quantity INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
                UNIQUE (title COLLATE NOCASE, authors COLLATE NOCASE)
            );
            CREATE TABLE scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT, scanned_at TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '', books_found INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE sightings (
                scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                PRIMARY KEY (scan_id, book_id)
            );
            INSERT INTO books (title, authors, quantity) VALUES ('Dune', 'Frank Herbert', 2);
            PRAGMA user_version = 2;
            """
        )
        conn.commit()
        conn.close()

        db = Database(path)
        assert db.schema_version == SCHEMA_VERSION
        [dune] = db.all()
        assert (dune.quantity, dune.work_key, dune.cover_url) == (2, "", "")
        identity = db._conn.execute("SELECT identity FROM books").fetchone()[0]
        assert identity == "text:dune|frank herbert"
        assert db.get_setting("anything") == ""
        db.close()
