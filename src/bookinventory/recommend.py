"""Recommendations based on the books already in the inventory.

The approach is deliberately simple and explainable:

1. :func:`build_profile` turns the inventory into a **taste profile**: which
   authors you own (weighted by how many of their titles and copies you
   have), which subjects recur across your books, and which decades.
2. :func:`Recommender.recommend` asks Open Library for more works by your
   strongest authors and for the top-rated works in your strongest subjects
   (one concurrent batch), drops what you already own or have dismissed, and
   scores every candidate by how well it fits the profile plus its rating and
   popularity. A diversity cap stops one author from filling the list.
3. Each :class:`Recommendation` carries the *reasons* it was chosen, so the
   UI can say "More by Frank Herbert · Science fiction, like 4 of your books".
4. The chosen books get a **short summary** from their Open Library work
   record (falling back to the search result's first sentence), trimmed to a
   couple of sentences.

Everything here is pure Python over plain data; the network call is a single
injectable ``search(params) -> dict`` function.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from bookinventory.aggregation import InventoryEntry
from bookinventory.database import Book, normalize
from bookinventory.lookup import FIELDS, book_from_doc, fetch_work, search_request

Search = Callable[[dict], dict]
FetchWork = Callable[[str], dict]

RECOMMENDATION_FIELDS = FIELDS + ",ratings_average,ratings_count,readinglog_count,first_sentence"
SUMMARY_LENGTH = 280  # characters
MAX_AUTHORS = 4  # profile authors queried
MAX_SUBJECTS = 4  # profile subjects queried
RESULTS_PER_QUERY = 20
PER_AUTHOR_CAP = 3  # diversity: at most this many recommendations by one author
DEFAULT_LIMIT = 20
MIN_SCORE = 0.15

#: Open Library subject tags that say nothing about taste.
SUBJECT_STOPLIST = frozenset(
    {
        "fiction",
        "literature",
        "accessible book",
        "protected daisy",
        "in library",
        "large type books",
        "open library staff picks",
        "reading level-grade 9",
        "reading level-grade 10",
        "reading level-grade 11",
        "reading level-grade 12",
        "long now manual for civilization",
        "overdrive",
        "new york times bestseller",
        "nyt:",
        "translations into",
        "juvenile fiction",
        "general",
        "english language",
        "readers",
        "textbooks",
        "study guides",
    }
)
_SUBJECT_NOISE = re.compile(
    r"^(nyt:|translations into |reading level|award:|places:|people:|times:)"
)

# Score weights (sum to 1).
W_AUTHOR, W_SUBJECT, W_RATING, W_POPULARITY = 0.45, 0.35, 0.12, 0.08
POPULARITY_SCALE = 2000  # readinglog count at which popularity saturates


def clean_subject(subject: str) -> str | None:
    """Normalise a subject tag; ``None`` if it carries no taste information."""
    text = " ".join(subject.lower().replace("_", " ").split())
    if not text or text in SUBJECT_STOPLIST or _SUBJECT_NOISE.match(text):
        return None
    if text.endswith(" (fiction)"):
        text = text[: -len(" (fiction)")]
    return text


@dataclass(frozen=True)
class Profile:
    """What the inventory says about the reader's taste (weights in [0, 1])."""

    authors: dict[str, float] = field(default_factory=dict)  # display name -> weight
    subjects: dict[str, float] = field(default_factory=dict)  # cleaned subject -> weight
    decades: dict[str, float] = field(default_factory=dict)
    author_titles: dict[str, int] = field(default_factory=dict)  # display name -> titles owned
    subject_titles: dict[str, int] = field(default_factory=dict)
    owned_keys: frozenset[str] = frozenset()
    size: int = 0

    @property
    def empty(self) -> bool:
        return self.size == 0

    def top_authors(self, n: int = MAX_AUTHORS) -> list[str]:
        return [a for a, _ in sorted(self.authors.items(), key=lambda kv: -kv[1])[:n]]

    def top_subjects(self, n: int = MAX_SUBJECTS) -> list[str]:
        return [s for s, _ in sorted(self.subjects.items(), key=lambda kv: -kv[1])[:n]]

    def describe(self) -> str:
        if self.empty:
            return "Add some books and recommendations will appear here."
        parts = self.top_authors(2) + self.top_subjects(2)
        basis = ", ".join(parts) if parts else "your books"
        return f"Based on {self.size} book{'s' if self.size != 1 else ''}: {basis}."


def _normalise(weights: dict[str, float]) -> dict[str, float]:
    if not weights:
        return {}
    top = max(weights.values())
    return {key: value / top for key, value in weights.items()}


def build_profile(entries: Iterable[InventoryEntry | Book]) -> Profile:
    """Derive a :class:`Profile` from inventory entries (or plain books)."""
    books = [e.book if isinstance(e, InventoryEntry) else e for e in entries]
    authors: dict[str, float] = {}
    author_names: dict[str, str] = {}
    subjects: dict[str, float] = {}
    subject_titles: Counter[str] = Counter()
    decades: dict[str, float] = {}
    owned: set[str] = set()
    for book in books:
        weight = 1.0 + 0.5 * max(0, book.quantity - 1)  # extra copies count a little
        for author in book.author_list:
            key = normalize(author)
            author_names.setdefault(key, author)
            authors[key] = authors.get(key, 0.0) + weight
        seen: set[str] = set()
        for subject in book.subjects:
            cleaned = clean_subject(subject)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                subjects[cleaned] = subjects.get(cleaned, 0.0) + weight
                subject_titles[cleaned] += 1
        digits = "".join(ch for ch in book.year if ch.isdigit())[:4]
        if len(digits) == 4:
            decade = f"{int(digits) // 10 * 10}s"
            decades[decade] = decades.get(decade, 0.0) + 1
        owned.update({book.key, book.text_key})
    return Profile(
        authors={author_names[k]: w for k, w in _normalise(authors).items()},
        subjects=_normalise(subjects),
        decades=_normalise(decades),
        author_titles={author_names[k]: int(round(v)) for k, v in authors.items()},
        subject_titles=dict(subject_titles),
        owned_keys=frozenset(owned),
        size=len(books),
    )


_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")  # markdown links -> their text
_SENTENCE_END = re.compile(r"(?<=[.!?])\s")
#: Where the useful part of a description ends: source notes and back-matter.
_TRAILER = re.compile(r"\s*(?:\(\s*)?(?:-{2,}|source:|\[source\]|Contains:)", re.IGNORECASE)


def summarize_description(raw: object, limit: int = SUMMARY_LENGTH) -> str:
    """A short, clean summary from an Open Library description.

    Descriptions come as a string or ``{"type": "/type/text", "value": ...}``,
    often with markdown, source notes and many paragraphs. Keep whole
    sentences from the first paragraph up to ``limit`` characters.
    """
    if isinstance(raw, dict):
        raw = raw.get("value", "")
    if isinstance(raw, list):
        raw = " ".join(str(part) for part in raw)
    if not isinstance(raw, str):
        return ""
    text = _LINK.sub(r"\1", raw).replace("*", "").replace("`", "")
    paragraph = next((p for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()), "")
    paragraph = " ".join(paragraph.split())
    paragraph = _TRAILER.split(paragraph, maxsplit=1)[0]
    paragraph = paragraph.strip(" -–—")
    if len(paragraph) <= limit:
        return paragraph
    kept = ""
    for sentence in _SENTENCE_END.split(paragraph):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    if not kept:  # one very long sentence: cut at a word boundary
        kept = paragraph[:limit].rsplit(" ", 1)[0]
    return kept.rstrip(" ,;:") + ("" if kept.endswith((".", "!", "?")) else "…")


@dataclass
class Recommendation:
    book: Book
    score: float
    reasons: list[str]
    rating: float | None = None
    summary: str = ""

    @property
    def key(self) -> str:
        return self.book.key

    def describe(self) -> str:
        """Subtitle for a list row: author · year · reasons."""
        parts = [self.book.authors or "Unknown author"]
        if self.book.year:
            parts.append(self.book.year)
        if self.rating:
            parts.append(f"rated {self.rating:.1f}")
        parts.extend(self.reasons[:2])
        return " · ".join(parts)


def _author_weight(profile: Profile, book: Book) -> tuple[float, str | None]:
    best, name = 0.0, None
    wanted = {normalize(a): a for a in profile.authors}
    for author in book.author_list:
        key = normalize(author)
        if key in wanted and profile.authors[wanted[key]] > best:
            best, name = profile.authors[wanted[key]], wanted[key]
    return best, name


def _subject_weight(profile: Profile, book: Book) -> tuple[float, str | None]:
    total, best, best_name = 0.0, 0.0, None
    seen: set[str] = set()
    for subject in book.subjects:
        cleaned = clean_subject(subject)
        if cleaned is None or cleaned in seen:
            continue
        seen.add(cleaned)
        weight = profile.subjects.get(cleaned, 0.0)
        total += weight
        if weight > best:
            best, best_name = weight, cleaned
    return min(1.0, total / 2.0), best_name  # two strong shared subjects saturate


def score_candidate(profile: Profile, book: Book, doc: dict) -> Recommendation:
    """Score one candidate against the profile, with human-readable reasons."""
    author_w, author = _author_weight(profile, book)
    subject_w, subject = _subject_weight(profile, book)
    rating_raw = doc.get("ratings_average")
    rating = float(rating_raw) if isinstance(rating_raw, int | float) else None
    rating_w = (rating / 5.0) if rating else 0.0
    readers = doc.get("readinglog_count") or 0
    popularity_w = min(1.0, math.log1p(readers) / math.log1p(POPULARITY_SCALE))
    score = W_AUTHOR * author_w + W_SUBJECT * subject_w + W_RATING * rating_w
    score += W_POPULARITY * popularity_w

    reasons: list[str] = []
    if author:
        n = profile.author_titles.get(author, 0)
        reasons.append(f"More by {author}" + (f" (you have {n})" if n else ""))
    if subject:
        n = profile.subject_titles.get(subject, 0)
        reasons.append(f"{subject.capitalize()}, like {n} of your books" if n else subject)
    if readers:
        reasons.append(f"{readers:,} readers")
    first = doc.get("first_sentence")
    fallback = summarize_description(first) if first else ""
    return Recommendation(book, round(score, 4), reasons, rating, summary=fallback)


class Recommender:
    """Fetches and ranks recommendations for a profile."""

    def __init__(
        self,
        search: Search = search_request,
        fetch_work: FetchWork = fetch_work,
        workers: int = 4,
    ):
        self._search = search
        self._fetch_work = fetch_work
        self.workers = workers
        self._cache: dict[str, dict] = {}
        self._summaries: dict[str, str] = {}

    def summary_for(self, book: Book) -> str:
        """The work's description as a short summary ("" if unavailable)."""
        key = book.work_key
        if not key:
            return ""
        if key not in self._summaries:
            try:
                self._summaries[key] = summarize_description(
                    self._fetch_work(key).get("description")
                )
            except (OSError, ValueError):  # best effort
                self._summaries[key] = ""
        return self._summaries[key]

    def add_summaries(self, recommendations: list[Recommendation]) -> None:
        """Fill in summaries concurrently; keep the first-sentence fallback otherwise."""
        if not recommendations:
            return
        with ThreadPoolExecutor(
            max_workers=max(1, min(self.workers, len(recommendations)))
        ) as pool:
            summaries = list(pool.map(lambda r: self.summary_for(r.book), recommendations))
        for rec, summary in zip(recommendations, summaries, strict=True):
            if summary:
                rec.summary = summary

    def _cached_search(self, params: dict) -> dict:
        key = json.dumps(params, sort_keys=True)
        if key not in self._cache:
            try:
                self._cache[key] = self._search(params)
            except OSError:  # one failed query shouldn't sink the rest
                return {}
        return self._cache[key]

    def queries(self, profile: Profile) -> list[dict]:
        common = {"limit": RESULTS_PER_QUERY, "fields": RECOMMENDATION_FIELDS, "sort": "rating"}
        return [{"author": a, **common} for a in profile.top_authors()] + [
            {"subject": s, **common} for s in profile.top_subjects()
        ]

    def recommend(
        self,
        entries: Iterable[InventoryEntry | Book],
        limit: int = DEFAULT_LIMIT,
        dismissed: Iterable[str] = (),
    ) -> list[Recommendation]:
        profile = build_profile(entries)
        if profile.empty:
            return []
        queries = self.queries(profile)
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(queries)))) as pool:
            responses = list(pool.map(self._cached_search, queries))

        excluded = set(profile.owned_keys) | set(dismissed)
        best: dict[str, Recommendation] = {}
        for response in responses:
            for doc in response.get("docs") or []:
                book = book_from_doc(doc)
                if book is None or book.key in excluded or book.text_key in excluded:
                    continue
                candidate = score_candidate(profile, book, doc)
                if candidate.score < MIN_SCORE:
                    continue
                if book.key not in best or candidate.score > best[book.key].score:
                    best[book.key] = candidate

        ranked = sorted(best.values(), key=lambda r: (-r.score, r.book.title.lower()))
        per_author: Counter[str] = Counter()
        chosen: list[Recommendation] = []
        for rec in ranked:
            primary = normalize(rec.book.author_list[0]) if rec.book.author_list else ""
            if primary and per_author[primary] >= PER_AUTHOR_CAP:
                continue
            per_author[primary] += 1
            chosen.append(rec)
            if len(chosen) >= limit:
                break
        self.add_summaries(chosen)
        return chosen
