"""Group OCR lines that belong to the same book spine.

Vision returns one observation per line of text. A spine usually carries two
or three - title, author, sometimes publisher - and looking them up separately
loses the strongest signal there is: *"Dune" next to "Frank Herbert"*.

After Vision has rotated the photo (see :mod:`bookinventory.ocr`), spine text
is horizontal, and the lines of one spine are either stacked (same horizontal
span, a small vertical gap) or run on from each other (same vertical band, a
small horizontal gap). This module clusters lines by that geometry, within one
orientation, and produces a combined query per spine with the lines in
reading order.
"""

from __future__ import annotations

from dataclasses import dataclass

from bookinventory.ocr import OcrLine

#: Vertical gap between stacked lines, as a multiple of the taller line's height.
STACK_GAP = 0.6
#: Minimum horizontal overlap between stacked lines, as a fraction of the narrower line.
STACK_OVERLAP = 0.3
#: Horizontal gap between lines running on from each other, as a multiple of line height.
RUN_ON_GAP = 2.0
#: Minimum vertical overlap for run-on lines, as a fraction of the shorter line.
RUN_ON_OVERLAP = 0.5


@dataclass(frozen=True)
class Spine:
    """The lines found on one spine."""

    lines: tuple[OcrLine, ...]

    @property
    def text(self) -> str:
        """All lines joined in reading order."""
        return " ".join(line.text.strip() for line in self.lines if line.text.strip())

    @property
    def texts(self) -> list[str]:
        return [line.text for line in self.lines]

    @property
    def orientation(self) -> int:
        return self.lines[0].orientation


def _overlap(a_start: float, a_size: float, b_start: float, b_size: float) -> float:
    return max(0.0, min(a_start + a_size, b_start + b_size) - max(a_start, b_start))


def _gap(a_start: float, a_size: float, b_start: float, b_size: float) -> float:
    return max(a_start, b_start) - min(a_start + a_size, b_start + b_size)


def same_spine(a: OcrLine, b: OcrLine) -> bool:
    """Whether two lines (from the same orientation) sit on one spine."""
    if a.orientation != b.orientation:
        return False
    height = max(a.height, b.height)
    if height <= 0:
        return False
    # Stacked: overlapping horizontally, close vertically.
    x_overlap = _overlap(a.x, a.width, b.x, b.width)
    if (
        x_overlap >= STACK_OVERLAP * max(min(a.width, b.width), 1e-9)
        and _gap(a.y, a.height, b.y, b.height) <= STACK_GAP * height
    ):
        return True
    # Run-on: overlapping vertically, close horizontally.
    y_overlap = _overlap(a.y, a.height, b.y, b.height)
    return (
        y_overlap >= RUN_ON_OVERLAP * max(min(a.height, b.height), 1e-9)
        and _gap(a.x, a.width, b.x, b.width) <= RUN_ON_GAP * height
    )


def group_spines(lines: list[OcrLine]) -> list[Spine]:
    """Cluster ``lines`` into spines (transitively: A~B and B~C puts all three together).

    Lines without geometry (zero-size boxes) each form their own spine. The
    order of spines follows the first line of each; within a spine, lines are
    in reading order (top to bottom, then left to right).
    """
    parent = list(range(len(lines)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            if same_spine(lines[i], lines[j]):
                parent[find(i)] = find(j)

    groups: dict[int, list[OcrLine]] = {}
    order: list[int] = []
    for i, line in enumerate(lines):
        root = find(i)
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(line)

    spines = []
    for root in order:
        members = sorted(groups[root], key=lambda ln: (round(-(ln.y + ln.height), 3), ln.x))
        spines.append(Spine(tuple(members)))
    return spines
