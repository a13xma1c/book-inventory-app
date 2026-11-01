"""Tests for bookinventory.spines: grouping OCR lines by book spine."""

from bookinventory.ocr import OcrLine
from bookinventory.spines import Spine, group_spines, same_spine


def line(text, x, y, w=0.3, h=0.05, orientation=6):
    return OcrLine(text, 0.9, orientation, x, y, w, h)


class TestSameSpine:
    def test_stacked_lines(self):
        title = line("DUNE", 0.1, 0.50)
        author = line("Frank Herbert", 0.12, 0.44)  # just below, overlapping horizontally
        assert same_spine(title, author)

    def test_run_on_lines(self):
        title = line("DUNE", 0.1, 0.5, w=0.2)
        author = line("Frank Herbert", 0.33, 0.5, w=0.3)  # to the right, same band
        assert same_spine(title, author)

    def test_far_apart_vertically(self):
        assert not same_spine(line("A", 0.1, 0.5), line("B", 0.1, 0.3))

    def test_far_apart_horizontally(self):
        assert not same_spine(line("A", 0.1, 0.5, w=0.1), line("B", 0.6, 0.5, w=0.1))

    def test_different_orientation(self):
        assert not same_spine(
            line("A", 0.1, 0.5, orientation=6), line("B", 0.1, 0.5, orientation=8)
        )

    def test_no_geometry(self):
        assert not same_spine(OcrLine("A"), OcrLine("B"))

    def test_stacked_but_not_overlapping_horizontally(self):
        # Neighbouring spine at the same height but offset sideways.
        assert not same_spine(line("A", 0.0, 0.5, w=0.2), line("B", 0.3, 0.45, w=0.2))


class TestGroupSpines:
    def test_groups_and_reading_order(self):
        lines = [
            line("Frank Herbert", 0.12, 0.44),  # author listed first by Vision
            line("DUNE", 0.1, 0.50),
            line("THE HOBBIT", 0.1, 0.80),  # another spine higher up
            line("PENGUIN", 0.6, 0.1, orientation=1),  # upright text elsewhere
        ]
        spines = group_spines(lines)
        assert [s.text for s in spines] == ["DUNE Frank Herbert", "THE HOBBIT", "PENGUIN"]
        assert spines[0].texts == ["DUNE", "Frank Herbert"]
        assert spines[0].orientation == 6

    def test_transitive_grouping(self):
        a = line("A", 0.1, 0.50)
        b = line("B", 0.1, 0.44)
        c = line("C", 0.1, 0.38)  # near B, not near A
        assert not same_spine(a, c)
        assert [s.text for s in group_spines([a, b, c])] == ["A B C"]

    def test_lines_without_geometry_stay_separate(self):
        spines = group_spines([OcrLine("DUNE"), OcrLine("Frank Herbert")])
        assert [s.text for s in spines] == ["DUNE", "Frank Herbert"]

    def test_order_follows_first_appearance(self):
        spines = group_spines([line("Z", 0.1, 0.1), line("A", 0.5, 0.9)])
        assert [s.text for s in spines] == ["Z", "A"]

    def test_empty(self):
        assert group_spines([]) == []

    def test_blank_lines_dropped_from_text(self):
        spine = Spine((line("  ", 0.1, 0.5), line("DUNE", 0.1, 0.45)))
        assert spine.text == "DUNE"
