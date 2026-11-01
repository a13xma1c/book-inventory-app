"""Colour schemes.

A warm "library" palette in two variants. Colours are applied through Toga's
``Pack`` styles (so they work on every platform); on iOS the app additionally
switches UIKit itself to light or dark appearance so that system-drawn parts
(list cells, text fields, the navigation bar) follow along.
"""

from __future__ import annotations

from dataclasses import dataclass

LIGHT_MODE, DARK_MODE = "light", "dark"


@dataclass(frozen=True)
class Palette:
    name: str
    background: str  # screen
    surface: str  # list / input backgrounds
    text: str  # primary text
    muted: str  # secondary text (status, hints)
    accent: str  # primary actions
    action: str  # secondary actions
    danger: str  # destructive actions

    @property
    def dark(self) -> bool:
        return self.name == DARK_MODE


LIGHT = Palette(
    name=LIGHT_MODE,
    background="#F6F1E7",  # parchment
    surface="#FFFBF4",
    text="#2B2118",  # ink
    muted="#7A6F63",
    accent="#B5512E",  # terracotta
    action="#2F6F73",  # teal
    danger="#9C2F2F",
)

DARK = Palette(
    name=DARK_MODE,
    background="#1C1A17",  # walnut
    surface="#2A2621",
    text="#F2EADF",
    muted="#A89F94",
    accent="#E8845A",
    action="#6FB3B8",
    danger="#E06C6C",
)


def palette(dark: bool) -> Palette:
    return DARK if dark else LIGHT
