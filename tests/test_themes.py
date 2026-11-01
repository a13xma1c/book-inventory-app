"""Tests for bookinventory.themes."""

import re

import pytest

from bookinventory import themes

HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


def luminance(colour: str) -> float:
    r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


class TestPalettes:
    @pytest.mark.parametrize("palette", [themes.LIGHT, themes.DARK])
    def test_all_colours_are_hex(self, palette):
        for field in ("background", "surface", "text", "muted", "accent", "action", "danger"):
            assert HEX.match(getattr(palette, field)), field

    def test_light_is_light_and_dark_is_dark(self):
        assert luminance(themes.LIGHT.background) > 0.8
        assert luminance(themes.DARK.background) < 0.2
        assert luminance(themes.LIGHT.text) < 0.2
        assert luminance(themes.DARK.text) > 0.8

    def test_text_contrasts_with_background(self):
        for palette in (themes.LIGHT, themes.DARK):
            assert abs(luminance(palette.text) - luminance(palette.background)) > 0.6
            assert abs(luminance(palette.muted) - luminance(palette.background)) > 0.25

    def test_palette_selection(self):
        assert themes.palette(True) is themes.DARK and themes.DARK.dark
        assert themes.palette(False) is themes.LIGHT and not themes.LIGHT.dark

    def test_palettes_are_immutable(self):
        with pytest.raises(AttributeError):
            themes.LIGHT.accent = "#000000"  # type: ignore[misc]
