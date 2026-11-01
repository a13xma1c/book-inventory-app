"""Tests for bookinventory.matching: OCR line filtering and result verification."""

import pytest

from bookinventory.matching import (
    MIN_MATCH_SCORE,
    candidate_queries,
    coverage,
    is_match,
    match_score,
    token_similarity,
    tokens,
)


class TestTokens:
    def test_lowercases_and_splits_on_punctuation(self):
        assert tokens("J.R.R. Tolkien") == ["j", "r", "r", "tolkien"]
        assert tokens("Nineteen Eighty-Four") == ["nineteen", "eighty", "four"]

    def test_drops_stopwords(self):
        assert tokens("The Lord of the Rings") == ["lord", "rings"]

    def test_keeps_digits(self):
        assert tokens("Fahrenheit 451") == ["fahrenheit", "451"]

    def test_empty(self):
        assert tokens("") == []
        assert tokens("!!! ---") == []


class TestCandidateQueries:
    @pytest.mark.parametrize(
        "line",
        ["Dune", "THE HOBBIT", "Frank Herbert", "Fahrenheit 451", "Catch-22", "J.R.R. Tolkien"],
    )
    def test_accepts(self, line):
        assert candidate_queries([line]) == [line]

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "ab",
            "abc",  # shorter than MIN_QUERY_LENGTH
            "12",
            "9780441013593",  # ISBN
            "978-0-441-01359-3",
            "$9.99",
            "$$ 4 5 ##",
            "A1 B2 C3",  # too few letters
            "abcd 1234 5678",  # enough letters, but mostly digits
            "- - - -",
        ],
    )
    def test_rejects(self, line):
        assert candidate_queries([line]) == []

    def test_collapses_whitespace(self):
        assert candidate_queries(["  Frank   Herbert\t"]) == ["Frank Herbert"]

    def test_dedupes_case_insensitively_keeping_first(self):
        assert candidate_queries(["Dune", "DUNE", "dune"]) == ["Dune"]

    def test_preserves_order(self):
        assert candidate_queries(["Zebra", "Apple", "Mango"]) == ["Zebra", "Apple", "Mango"]

    def test_empty_input(self):
        assert candidate_queries([]) == []


class TestTokenSimilarity:
    def test_identical(self):
        assert token_similarity("hobbit", "hobbit") == 1.0

    def test_short_tokens_must_match_exactly(self):
        assert token_similarity("dun", "dune") == 0.0
        assert token_similarity("of", "on") == 0.0

    def test_one_letter_ocr_slip(self):
        assert token_similarity("hobbjt", "hobbit") >= 0.8

    def test_unrelated(self):
        assert token_similarity("hobbit", "kitchen") < 0.5


class TestCoverage:
    def test_full_and_partial(self):
        assert coverage(["dune", "herbert"], ["dune", "frank", "herbert"]) == 1.0
        assert coverage(["dune", "buggy"], ["dune"]) == 0.5

    def test_each_haystack_token_explains_one_needle(self):
        assert coverage(["dune", "dune"], ["dune"]) == 0.5

    def test_fuzzy(self):
        assert coverage(["hobbjt"], ["hobbit"]) == 1.0

    def test_empty_needles(self):
        assert coverage([], ["dune"]) == 0.0


class TestMatchScore:
    def test_title_and_author_present(self):
        assert match_score("Dune Herbert", "Dune", "Frank Herbert") == 1.0

    def test_title_only_line(self):
        assert match_score("DUNE", "Dune", "Frank Herbert") == 1.0

    def test_author_only_line_is_not_a_match(self):
        # Half the score is title coverage, so "Frank Herbert" can't claim Dune.
        assert match_score("Frank Herbert", "Dune", "Frank Herbert") == 0.5
        assert not is_match("Frank Herbert", "Dune", "Frank Herbert")

    def test_partial_title_scores_below_exact(self):
        assert match_score("Dune", "Dune", "") > match_score("Dune", "Dune Messiah", "")
        assert match_score("Dune", "Dune Messiah", "") == 0.75

    def test_extra_ocr_noise_is_tolerated(self):
        assert is_match("Dune Frank Herbert xyzzy", "Dune", "Frank Herbert")

    def test_long_unrelated_line_with_the_title_in_it(self):
        score = match_score("dune buggy racing manual fourth edition", "Dune", "")
        assert score < MIN_MATCH_SCORE

    def test_stopwords_ignored_on_both_sides(self):
        assert match_score("the hobbit", "The Hobbit", "J.R.R. Tolkien") == 1.0
        assert match_score("Lord Rings", "The Lord of the Rings", "") == 1.0

    def test_case_and_punctuation_insensitive(self):
        assert match_score("NINETEEN EIGHTY-FOUR", "Nineteen Eighty-Four", "Orwell") == 1.0

    def test_ocr_typo_in_title(self):
        assert match_score("THE HOBBJT", "The Hobbit", "J.R.R. Tolkien") == 1.0

    def test_no_overlap(self):
        assert match_score("Cooking Basics", "Quantum Field Theory", "A. Zee") == 0.0

    def test_empty_inputs(self):
        assert match_score("", "Dune", "Frank Herbert") == 0.0
        assert match_score("the of", "Dune", "Frank Herbert") == 0.0
        assert match_score("Dune", "", "") == 0.0

    def test_rounded(self):
        score = match_score("one two three", "one", "")
        assert score == round(score, 4)


class TestIsMatch:
    def test_threshold(self):
        assert is_match("the hobbit", "The Hobbit", "J.R.R. Tolkien")
        assert not is_match("Cooking Basics", "Quantum Field Theory", "A. Zee")

    def test_threshold_is_inclusive(self):
        assert MIN_MATCH_SCORE == 0.6
        # line coverage 1/5 + title coverage 1 -> 0.6 exactly
        assert is_match("one two three four five", "one", "")
        assert not is_match("one two three four five six", "one", "")
