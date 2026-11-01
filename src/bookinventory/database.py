"""SQLite storage for the book inventory.

Schema (version 3)
------------------

``books``      one row per distinct book, with the number of copies
``scans``      one row per photo that was recorded, with where it was taken
``sightings``  which books were seen in which scan
``settings``   small key/value store (last location used, ...)

Older databases are migrated in place on open: version 1 had only ``books``;
version 2 added scans, sightings and quantities; version 3 added the Open
Library work key, cover URL and a stable ``identity`` column; version 4 added
each book's subjects (used by the recommendation system).
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCHEMA_VERSION = 4

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    return _NON_ALNUM.sub(" ", text.lower()).strip()


def text_identity(title: str, authors: str) -> str:
    return f"text:{normalize(title)}|{normalize(authors)}"


@dataclass
class Book:
    title: str
    authors: str = ""
    year: str = ""
    isbn: str = ""
    id: int | None = None
    quantity: int = 1
    work_key: str = ""  # Open Library work key, e.g. "/works/OL1168083W"
    cover_url: str = ""
    subjects: tuple[str, ...] = field(default_factory=tuple)  # Open Library subject tags

    @property
    def author_list(self) -> list[str]:
        """The individual authors ("A, B" -> ["A", "B"])."""
        return [a.strip() for a in self.authors.split(",") if a.strip()]

    @property
    def key(self) -> str:
        """Stable identity: the Open Library work (one per *work*, shared by all its
        editions and author spellings), else the ISBN, else normalised title+author."""
        if self.work_key:
            return f"work:{self.work_key.strip()}"
        isbn = re.sub(r"[\s-]", "", self.isbn)
        if isbn:
            return f"isbn:{isbn.upper()}"
        return self.text_key

    @property
    def text_key(self) -> str:
        return text_identity(self.title, self.authors)


@dataclass(frozen=True)
class Scan:
    id: int
    scanned_at: str
    location: str
    books_found: int


@dataclass(frozen=True)
class ScanReport:
    """What recording a scan did to the inventory."""

    scan_id: int
    location: str
    found: int  # distinct books in the scan
    new_titles: int  # books not previously in the inventory
    new_copies: int  # existing books seen in a new location (quantity bumped)
    already_seen: int  # existing books re-seen where they'd been seen before

    def describe(self) -> str:
        if self.found == 0:
            return "No books found."
        parts = [f"Found {self.found} book{'s' if self.found != 1 else ''}"]
        details = []
        if self.new_titles:
            details.append(f"{self.new_titles} new")
        if self.new_copies:
            details.append(f"{self.new_copies} extra cop{'ies' if self.new_copies != 1 else 'y'}")
        if self.already_seen:
            details.append(f"{self.already_seen} already listed")
        if details:
            parts.append(f" ({', '.join(details)})")
        return "".join(parts) + "."


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        conn = self._conn
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS books (
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
        columns = {row[1] for row in conn.execute("PRAGMA table_info(books)")}
        if "quantity" not in columns:  # version 1 -> 2
            conn.execute(
                "ALTER TABLE books ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1 "
                "CHECK (quantity >= 0)"
            )
        if "identity" not in columns:  # version 2 -> 3
            conn.execute("ALTER TABLE books ADD COLUMN work_key TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE books ADD COLUMN cover_url TEXT NOT NULL DEFAULT ''")
            conn.execute("ALTER TABLE books ADD COLUMN identity TEXT")
            for book_id, title, authors in conn.execute("SELECT id, title, authors FROM books"):
                conn.execute(
                    "UPDATE books SET identity = ? WHERE id = ?",
                    (text_identity(title, authors), book_id),
                )
        if "subjects" not in columns:  # version 3 -> 4
            conn.execute("ALTER TABLE books ADD COLUMN subjects TEXT NOT NULL DEFAULT '[]'")
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS books_identity ON books(identity);
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT NOT NULL,
                location TEXT NOT NULL DEFAULT '',
                books_found INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS sightings (
                scan_id INTEGER NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
                book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                PRIMARY KEY (scan_id, book_id)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    # ----------------------------------------------------------------- writing

    def add(self, book: Book) -> bool:
        """Insert a book. Returns False if it is already in the inventory."""
        if self._find_id(book) is not None:
            return False
        self._insert(book, max(1, book.quantity))
        self._conn.commit()
        return True

    def _insert(self, book: Book, quantity: int) -> int:
        cur = self._conn.execute(
            "INSERT INTO books "
            "(title, authors, year, isbn, quantity, work_key, cover_url, identity, subjects) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                book.title,
                book.authors,
                book.year,
                book.isbn,
                quantity,
                book.work_key,
                book.cover_url,
                book.key,
                json.dumps(list(book.subjects)),
            ),
        )
        assert cur.lastrowid is not None
        return int(cur.lastrowid)

    def record_scan(self, books: list[Book], location: str = "") -> ScanReport:
        """Aggregate one photo's books into the inventory.

        * A book not yet in the inventory is added (quantity 1).
        * A book already in the inventory that is seen in a *new* location is
          another copy: its quantity is incremented.
        * A book re-seen somewhere it has already been seen (e.g. an
          overlapping photo of the same shelf) is left alone.

        Every book gets a sighting tied to the scan so the inventory can say
        where each title lives.
        """
        location = self._canonical_location(location)
        distinct = list({b.key: b for b in books}.values())
        conn = self._conn
        try:
            cur = conn.execute(
                "INSERT INTO scans (scanned_at, location, books_found) VALUES (?, ?, ?)",
                (_now(), location, len(distinct)),
            )
            assert cur.lastrowid is not None
            scan_id = int(cur.lastrowid)
            new_titles = new_copies = already_seen = 0
            for book in distinct:
                book_id = self._find_id(book)
                if book_id is None:
                    book_id = self._insert(book, 1)
                    new_titles += 1
                else:
                    self._fill_metadata(book_id, book)
                    if location.lower() in (loc.lower() for loc in self._locations(book_id)):
                        already_seen += 1
                    else:
                        conn.execute(
                            "UPDATE books SET quantity = quantity + 1 WHERE id = ?", (book_id,)
                        )
                        new_copies += 1
                conn.execute(
                    "INSERT OR IGNORE INTO sightings (scan_id, book_id) VALUES (?, ?)",
                    (scan_id, book_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return ScanReport(scan_id, location, len(distinct), new_titles, new_copies, already_seen)

    def _canonical_location(self, location: str) -> str:
        """Collapse whitespace and reuse the stored spelling of a known location,
        so "study" and "Study" are the same place."""
        location = " ".join(location.split())
        for known in self.locations():
            if known.lower() == location.lower():
                return known
        return location

    def _find_id(self, book: Book) -> int | None:
        """The stored row for ``book``: by identity, else by title/author text.

        A book first recorded without an Open Library key (older data, or a
        manual entry) is matched by its text key and upgraded to the stronger
        identity so later sightings of other editions merge into it.
        """
        row = self._conn.execute("SELECT id FROM books WHERE identity = ?", (book.key,)).fetchone()
        if row is not None:
            return int(row[0])
        row = self._conn.execute(
            "SELECT id FROM books WHERE identity = ? OR "
            "(title = ? COLLATE NOCASE AND authors = ? COLLATE NOCASE)",
            (book.text_key, book.title, book.authors),
        ).fetchone()
        if row is None:
            return None
        book_id = int(row[0])
        if book.key != book.text_key:
            self._conn.execute(
                "UPDATE books SET identity = ?, work_key = CASE WHEN work_key = '' THEN ? "
                "ELSE work_key END WHERE id = ?",
                (book.key, book.work_key, book_id),
            )
        return book_id

    def _fill_metadata(self, book_id: int, book: Book) -> None:
        """Fill in blank metadata from a later sighting; never overwrite."""
        self._conn.execute(
            """
            UPDATE books SET
                year = CASE WHEN year = '' THEN ? ELSE year END,
                isbn = CASE WHEN isbn = '' THEN ? ELSE isbn END,
                work_key = CASE WHEN work_key = '' THEN ? ELSE work_key END,
                cover_url = CASE WHEN cover_url = '' THEN ? ELSE cover_url END,
                subjects = CASE WHEN subjects = '[]' THEN ? ELSE subjects END
            WHERE id = ?
            """,
            (
                book.year,
                book.isbn,
                book.work_key,
                book.cover_url,
                json.dumps(list(book.subjects)),
                book_id,
            ),
        )

    def set_quantity(self, book_id: int, quantity: int) -> None:
        if quantity < 0:
            raise ValueError("quantity must not be negative")
        cur = self._conn.execute("UPDATE books SET quantity = ? WHERE id = ?", (quantity, book_id))
        self._conn.commit()
        if cur.rowcount == 0:
            raise KeyError(book_id)

    def adjust_quantity(self, book_id: int, delta: int) -> int:
        """Add ``delta`` copies (floored at zero). Returns the new quantity."""
        row = self._conn.execute("SELECT quantity FROM books WHERE id = ?", (book_id,)).fetchone()
        if row is None:
            raise KeyError(book_id)
        quantity = max(0, row[0] + delta)
        self.set_quantity(book_id, quantity)
        return quantity

    def delete(self, book_id: int) -> None:
        self._conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        self._conn.commit()

    def delete_scan(self, scan_id: int) -> bool:
        """Undo a scan. Books that were only ever seen in that scan are removed;
        books also seen elsewhere lose the copy that scan contributed."""
        conn = self._conn
        row = conn.execute("SELECT location FROM scans WHERE id = ?", (scan_id,)).fetchone()
        if row is None:
            return False
        location = row[0].lower()
        book_ids = [
            r[0]
            for r in conn.execute("SELECT book_id FROM sightings WHERE scan_id = ?", (scan_id,))
        ]
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
        for book_id in book_ids:
            remaining = [loc.lower() for loc in self._locations(book_id)]
            if not remaining:
                conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
            elif location not in remaining:
                # That scan was the only sighting at its location, so it had
                # contributed a copy; take it back (but never drop below one).
                conn.execute(
                    "UPDATE books SET quantity = MAX(1, quantity - 1) WHERE id = ?", (book_id,)
                )
        conn.commit()
        return True

    def rename_location(self, old: str, new: str) -> int:
        cur = self._conn.execute(
            "UPDATE scans SET location = ? WHERE location = ? COLLATE NOCASE",
            (" ".join(new.split()), " ".join(old.split())),
        )
        self._conn.commit()
        return cur.rowcount

    def clear(self) -> None:
        self._conn.executescript(
            "DELETE FROM sightings; DELETE FROM scans; DELETE FROM books; DELETE FROM settings;"
        )
        self._conn.commit()

    # ----------------------------------------------------------------- reading

    _BOOK_COLUMNS = "id, title, authors, year, isbn, quantity, work_key, cover_url, subjects"

    @staticmethod
    def _book(r) -> Book:
        return Book(
            id=r[0],
            title=r[1],
            authors=r[2],
            year=r[3],
            isbn=r[4],
            quantity=r[5],
            work_key=r[6],
            cover_url=r[7],
            subjects=tuple(json.loads(r[8] or "[]")),
        )

    def all(self) -> list[Book]:
        rows = self._conn.execute(
            f"SELECT {self._BOOK_COLUMNS} FROM books "
            "ORDER BY title COLLATE NOCASE, authors COLLATE NOCASE"
        )
        return [self._book(r) for r in rows]

    def get(self, book_id: int) -> Book | None:
        r = self._conn.execute(
            f"SELECT {self._BOOK_COLUMNS} FROM books WHERE id = ?", (book_id,)
        ).fetchone()
        return None if r is None else self._book(r)

    def last_scan(self) -> Scan | None:
        scans = self.scans()
        return scans[-1] if scans else None

    # --------------------------------------------------------------- settings

    def get_setting(self, key: str, default: str = "") -> str:
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return default if row is None else row[0]

    def set_setting(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def count(self) -> int:
        """Number of distinct titles."""
        return self._conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]

    def total_copies(self) -> int:
        return self._conn.execute("SELECT COALESCE(SUM(quantity), 0) FROM books").fetchone()[0]

    def scans(self) -> list[Scan]:
        rows = self._conn.execute(
            "SELECT id, scanned_at, location, books_found FROM scans ORDER BY id"
        )
        return [Scan(*r) for r in rows]

    def locations(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT location FROM scans WHERE location != '' "
            "ORDER BY location COLLATE NOCASE"
        )
        return [r[0] for r in rows]

    def _locations(self, book_id: int) -> list[str]:
        rows = self._conn.execute(
            """
            SELECT DISTINCT s.location FROM scans s
            JOIN sightings g ON g.scan_id = s.id
            WHERE g.book_id = ? ORDER BY s.location COLLATE NOCASE
            """,
            (book_id,),
        )
        return [r[0] for r in rows]

    def sightings(self) -> list[tuple[int, int, str, str]]:
        """``(book_id, scan_id, location, scanned_at)`` for every sighting."""
        rows = self._conn.execute(
            """
            SELECT g.book_id, g.scan_id, s.location, s.scanned_at
            FROM sightings g JOIN scans s ON s.id = g.scan_id
            ORDER BY g.scan_id, g.book_id
            """
        )
        return [tuple(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
