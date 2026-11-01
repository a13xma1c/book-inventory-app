"""Photo -> books.

:func:`scan_lines` is the full pipeline used by the app: group OCR lines by
spine, look up each spine's combined text (concurrently), fall back to the
individual lines of spines that found nothing, and return a
:class:`ScanResult` of :class:`Candidate` books - each with its alternatives
- for the user to review before anything is recorded.

:func:`identify_books` is the simple line-by-line version kept for scripting.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from bookinventory.database import Book
from bookinventory.lookup import Lookup, Match, find_book, find_matches, run_lookups
from bookinventory.matching import candidate_queries
from bookinventory.ocr import OcrLine
from bookinventory.spines import group_spines

MAX_ALTERNATIVES = 4


@dataclass
class Candidate:
    """A book the scan believes is on the shelf, awaiting the user's confirmation."""

    book: Book
    query: str  # the OCR text it was matched from
    score: float
    alternatives: list[Book] = field(default_factory=list)
    included: bool = True

    def use_alternative(self, index: int) -> None:
        """Swap the chosen book with ``alternatives[index]``."""
        chosen = self.alternatives.pop(index)
        self.alternatives.insert(0, self.book)
        self.book = chosen

    def next_alternative(self) -> bool:
        """Cycle to the next alternative. Returns False if there are none."""
        if not self.alternatives:
            return False
        chosen = self.alternatives.pop(0)
        self.alternatives.append(self.book)
        self.book = chosen
        return True


@dataclass
class ScanResult:
    candidates: list[Candidate] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)
    line_count: int = 0

    @property
    def books(self) -> list[Book]:
        """The books the user has kept, de-duplicated."""
        seen: set[str] = set()
        books = []
        for candidate in self.candidates:
            if candidate.included and candidate.book.key not in seen:
                seen.add(candidate.book.key)
                books.append(candidate.book)
        return books

    @property
    def included(self) -> int:
        return sum(1 for c in self.candidates if c.included)

    def describe(self) -> str:
        found = len(self.candidates)
        if not found:
            return "No books recognised."
        text = f"Found {found} book{'s' if found != 1 else ''}"
        if self.unmatched:
            n = len(self.unmatched)
            text += f"; {n} line{'s' if n != 1 else ''} not matched"
        return text + "."


def _add_candidates(
    result: ScanResult, by_key: dict[str, Candidate], query: str, matches: list[Match]
) -> None:
    best, *rest = matches
    existing = by_key.get(best.book.key)
    if existing is None:
        candidate = Candidate(
            book=best.book,
            query=query,
            score=best.score,
            alternatives=[m.book for m in rest[:MAX_ALTERNATIVES]],
        )
        by_key[best.book.key] = candidate
        result.candidates.append(candidate)
    elif best.score > existing.score:
        existing.score, existing.query = best.score, query


#: A spine match at or above this score is trusted on its own; below it, the
#: spine's individual lines are consulted too (they may be two books read as one).
CONFIDENT = 0.9


def scan_lines(
    ocr_lines: Iterable[OcrLine], lookup: Lookup = find_matches, workers: int = 4
) -> ScanResult:
    """The full pipeline for one photo's OCR output."""
    ocr_lines = list(ocr_lines)
    result = ScanResult(line_count=len(ocr_lines))
    by_key: dict[str, Candidate] = {}
    spines = group_spines(ocr_lines)

    # One concurrent batch: each spine's combined text ("DUNE Frank Herbert")
    # plus, for multi-line spines, the individual lines.
    combined = {spine: candidate_queries([spine.text]) for spine in spines}
    separate = {
        spine: candidate_queries(spine.texts) if len(spine.lines) > 1 else [] for spine in spines
    }
    queries = [q for qs in combined.values() for q in qs] + [
        q for qs in separate.values() for q in qs
    ]
    found = run_lookups(queries, lookup, workers)

    for spine in spines:
        best_score = 0.0
        for query in combined[spine]:
            if found.get(query):
                _add_candidates(result, by_key, query, found[query])
                best_score = max(best_score, found[query][0].score)
        if best_score >= CONFIDENT:
            continue
        # Not (confidently) one book: the lines may be several books, or the
        # spine may have been over-grouped with a neighbour.
        line_matched = False
        for query in separate[spine]:
            if found.get(query):
                _add_candidates(result, by_key, query, found[query])
                line_matched = True
        if best_score == 0.0 and not line_matched:
            result.unmatched.extend(combined[spine] if len(spine.lines) == 1 else separate[spine])
    return result


def merge_results(results: Iterable[ScanResult]) -> ScanResult:
    """Combine the results of several photos of one shelf into one review list."""
    merged = ScanResult()
    by_key: dict[str, Candidate] = {}
    for result in results:
        merged.line_count += result.line_count
        for candidate in result.candidates:
            existing = by_key.get(candidate.book.key)
            if existing is None:
                by_key[candidate.book.key] = candidate
                merged.candidates.append(candidate)
            elif candidate.score > existing.score:
                existing.score, existing.query = candidate.score, candidate.query
        for line in result.unmatched:
            if line not in merged.unmatched:
                merged.unmatched.append(line)
    return merged


def identify_books(lines: list[str], find=find_book) -> list[Book]:
    """Look up each plausible OCR line and return the distinct books found."""
    books: dict[str, Book] = {}
    for query in candidate_queries(lines):
        book = find(query)
        if book:
            books.setdefault(book.key, book)
    return list(books.values())
