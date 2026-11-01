"""Turn raw OCR text into book-search queries and verify search results."""

import re
from collections.abc import Sequence
from difflib import SequenceMatcher

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "a", "an", "of", "and", "in", "to", "by"}

MIN_QUERY_LENGTH = 4
MIN_MATCH_SCORE = 0.6

#: Two tokens are "the same word" if identical, or long enough and this similar
#: (tolerates the one-letter OCR slips that are common on spines: Hobbjt/Hobbit).
FUZZY_MIN_LENGTH = 4
FUZZY_THRESHOLD = 0.8


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def candidate_queries(lines: list[str]) -> list[str]:
    """Filter and de-duplicate OCR lines that could plausibly be book text."""
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        line = " ".join(line.split())
        letters = sum(c.isalpha() for c in line)
        if len(line) < MIN_QUERY_LENGTH or letters < MIN_QUERY_LENGTH:
            continue
        if letters / len(line) < 0.6:  # mostly digits/punctuation: noise
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def token_similarity(a: str, b: str) -> float:
    """1.0 for identical tokens, else a ratio in [0, 1] for tokens of 4+ letters."""
    if a == b:
        return 1.0
    if len(a) < FUZZY_MIN_LENGTH or len(b) < FUZZY_MIN_LENGTH:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def coverage(needles: Sequence[str], haystack: Sequence[str]) -> float:
    """Fraction of ``needles`` with a (fuzzy) match in ``haystack``.

    Each haystack token can explain at most one needle, so "dune" alone does
    not cover the title "Dune Dune".
    """
    if not needles:
        return 0.0
    available = list(haystack)
    hits = 0
    for needle in needles:
        best, best_index = 0.0, -1
        for i, candidate in enumerate(available):
            similarity = token_similarity(needle, candidate)
            if similarity > best:
                best, best_index = similarity, i
        if best >= FUZZY_THRESHOLD:
            hits += 1
            del available[best_index]
    return hits / len(needles)


def match_score(ocr_text: str, title: str, authors: str) -> float:
    """How well a search result explains ``ocr_text``, in [0, 1].

    Half the score is how much of the OCR text the result accounts for (title
    *or* author words); the other half is how much of the result's *title* is
    present in the OCR text. The second half is what stops an author-only line
    ("Frank Herbert") from matching every book by that author.
    """
    wanted = tokens(ocr_text)
    if not wanted:
        return 0.0
    title_tokens = tokens(title)
    line_coverage = coverage(wanted, title_tokens + tokens(authors))
    title_coverage = coverage(title_tokens, wanted)
    return round(0.5 * line_coverage + 0.5 * title_coverage, 4)


def is_match(ocr_text: str, title: str, authors: str) -> bool:
    return match_score(ocr_text, title, authors) >= MIN_MATCH_SCORE
