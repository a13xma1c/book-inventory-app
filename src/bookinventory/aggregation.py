"""Inventory aggregation: turn many scans into one inventory and summarise it.

One photo yields a list of books. A real inventory comes from many photos -
several per shelf, several shelves per room - and the same title shows up
again and again. :class:`~bookinventory.database.Database.record_scan` folds
each photo into the stored inventory; this module provides the read side:

* :func:`merge_books` - combine the books from several photos of one shelf,
* :func:`build_inventory` - join books with their sightings into
  :class:`InventoryEntry` objects,
* :func:`summarize`, :func:`group_by_author`, :func:`group_by_location`,
  :func:`group_by_decade`, :func:`find_duplicates`, :func:`search`,
* :func:`to_csv` / :func:`to_json` for export.

Everything here is pure Python over plain data so it's easy to test and is
independent of both SQLite and the UI.
"""

import csv
import io
import json
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from bookinventory.database import Book, Database

UNKNOWN_LOCATION = "(unspecified)"
UNKNOWN_AUTHOR = "(unknown author)"


def merge_books(*photos: Iterable[Book]) -> list[Book]:
    """Combine the books found in several photos of the *same* shelf.

    Duplicates collapse into one book - by stable identity (work key / ISBN)
    or, failing that, by case-insensitive title and author; blank year, ISBN,
    work key and cover are filled from whichever copy has them. Order of first
    appearance is preserved.
    """
    merged: dict[str, Book] = {}
    by_text: dict[str, Book] = {}
    for photo in photos:
        for book in photo:
            existing = merged.get(book.key) or by_text.get(book.text_key)
            if existing is None:
                copy = Book(
                    book.title,
                    book.authors,
                    book.year,
                    book.isbn,
                    work_key=book.work_key,
                    cover_url=book.cover_url,
                )
                merged[copy.key] = copy
                by_text[copy.text_key] = copy
            else:
                existing.year = existing.year or book.year
                existing.isbn = existing.isbn or book.isbn
                existing.work_key = existing.work_key or book.work_key
                existing.cover_url = existing.cover_url or book.cover_url
    return list(merged.values())


@dataclass(frozen=True)
class InventoryEntry:
    """One distinct title in the inventory, with where and how often it was seen."""

    book: Book
    locations: tuple[str, ...] = ()
    times_seen: int = 0
    first_seen: str = ""
    last_seen: str = ""

    @property
    def title(self) -> str:
        return self.book.title

    @property
    def authors(self) -> str:
        return self.book.authors

    @property
    def quantity(self) -> int:
        return self.book.quantity

    @property
    def decade(self) -> str:
        digits = "".join(ch for ch in self.book.year if ch.isdigit())[:4]
        if len(digits) != 4:
            return "unknown"
        return f"{int(digits) // 10 * 10}s"


def build_inventory(db: Database) -> list[InventoryEntry]:
    """Join the stored books with their sightings, sorted by title."""
    locations: dict[int, set[str]] = defaultdict(set)
    seen: Counter[int] = Counter()
    first: dict[int, str] = {}
    last: dict[int, str] = {}
    for book_id, _scan_id, location, scanned_at in db.sightings():
        locations[book_id].add(location.strip() or UNKNOWN_LOCATION)
        seen[book_id] += 1
        first[book_id] = min(first.get(book_id, scanned_at), scanned_at)
        last[book_id] = max(last.get(book_id, scanned_at), scanned_at)
    entries = []
    for book in db.all():
        book_id = -1 if book.id is None else book.id
        entries.append(
            InventoryEntry(
                book=book,
                locations=tuple(sorted(locations.get(book_id, ()), key=str.lower)),
                times_seen=seen.get(book_id, 0),
                first_seen=first.get(book_id, ""),
                last_seen=last.get(book_id, ""),
            )
        )
    return entries


@dataclass(frozen=True)
class InventorySummary:
    titles: int
    copies: int
    locations: int
    duplicates: int  # titles with more than one copy
    copies_by_location: dict[str, int] = field(default_factory=dict)
    titles_by_author: dict[str, int] = field(default_factory=dict)
    titles_by_decade: dict[str, int] = field(default_factory=dict)

    def describe(self) -> str:
        """One line for the status bar, e.g. ``"12 titles, 14 copies, 2 locations"``."""
        parts = [
            f"{self.titles} title{'s' if self.titles != 1 else ''}",
            f"{self.copies} cop{'ies' if self.copies != 1 else 'y'}",
        ]
        if self.locations:
            parts.append(f"{self.locations} location{'s' if self.locations != 1 else ''}")
        if self.duplicates:
            parts.append(f"{self.duplicates} duplicated")
        return ", ".join(parts)


def summarize(entries: Iterable[InventoryEntry]) -> InventorySummary:
    entries = list(entries)
    by_location: Counter[str] = Counter()
    by_author: Counter[str] = Counter()
    by_decade: Counter[str] = Counter()
    for entry in entries:
        by_author[entry.authors or UNKNOWN_AUTHOR] += 1
        by_decade[entry.decade] += 1
        if entry.locations:
            # Spread the copies across the places the title was seen, one per
            # location first, any surplus to the first location alphabetically.
            base, extra = divmod(entry.quantity, len(entry.locations))
            for i, location in enumerate(entry.locations):
                by_location[location] += base + (1 if i < extra else 0)
    return InventorySummary(
        titles=len(entries),
        copies=sum(e.quantity for e in entries),
        locations=len(by_location),
        duplicates=sum(1 for e in entries if e.quantity > 1),
        copies_by_location=dict(sorted(by_location.items(), key=lambda kv: kv[0].lower())),
        titles_by_author=dict(sorted(by_author.items(), key=lambda kv: (-kv[1], kv[0].lower()))),
        titles_by_decade=dict(
            sorted(by_decade.items(), key=lambda kv: (kv[0] == "unknown", kv[0]))
        ),
    )


def group_by_author(entries: Iterable[InventoryEntry]) -> dict[str, list[InventoryEntry]]:
    groups: dict[str, list[InventoryEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.authors or UNKNOWN_AUTHOR].append(entry)
    return dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))


def group_by_location(entries: Iterable[InventoryEntry]) -> dict[str, list[InventoryEntry]]:
    """Entries per location; a title seen in several places is listed under each."""
    groups: dict[str, list[InventoryEntry]] = defaultdict(list)
    for entry in entries:
        for location in entry.locations:
            groups[location].append(entry)
    return dict(sorted(groups.items(), key=lambda kv: kv[0].lower()))


def group_by_decade(entries: Iterable[InventoryEntry]) -> dict[str, list[InventoryEntry]]:
    groups: dict[str, list[InventoryEntry]] = defaultdict(list)
    for entry in entries:
        groups[entry.decade].append(entry)
    return dict(sorted(groups.items(), key=lambda kv: (kv[0] == "unknown", kv[0])))


def find_duplicates(entries: Iterable[InventoryEntry]) -> list[InventoryEntry]:
    """Titles with more than one copy, most copies first."""
    return sorted(
        (e for e in entries if e.quantity > 1), key=lambda e: (-e.quantity, e.title.lower())
    )


def search(entries: Iterable[InventoryEntry], query: str) -> list[InventoryEntry]:
    """Case-insensitive substring search over title, authors and ISBN."""
    needle = " ".join(query.lower().split())
    if not needle:
        return list(entries)
    return [e for e in entries if needle in f"{e.title} {e.authors} {e.book.isbn}".lower()]


CSV_COLUMNS = (
    "title",
    "authors",
    "year",
    "isbn",
    "quantity",
    "locations",
    "times_seen",
    "first_seen",
    "last_seen",
)


def entry_to_dict(entry: InventoryEntry) -> dict:
    return {
        "title": entry.title,
        "authors": entry.authors,
        "year": entry.book.year,
        "isbn": entry.book.isbn,
        "quantity": entry.quantity,
        "locations": list(entry.locations),
        "times_seen": entry.times_seen,
        "first_seen": entry.first_seen,
        "last_seen": entry.last_seen,
    }


def to_csv(entries: Iterable[InventoryEntry]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for entry in entries:
        row = entry_to_dict(entry)
        row["locations"] = "; ".join(entry.locations)
        writer.writerow(row)
    return buffer.getvalue()


def to_json(entries: Iterable[InventoryEntry], indent: int | None = 2) -> str:
    entries = list(entries)
    summary = summarize(entries)
    document = {
        "summary": {"titles": summary.titles, "copies": summary.copies},
        "books": [entry_to_dict(e) for e in entries],
    }
    return json.dumps(document, indent=indent, ensure_ascii=False)
