"""Tests for the Toga UI, driven through Toga's ``dummy`` backend.

The real widget tree and handlers run; only the camera, OCR, the network and
the cover downloads are simulated.
"""

from __future__ import annotations

import asyncio
import io
import os

import pytest

pytest.importorskip("toga_dummy", reason="UI tests need the toga-dummy backend")
PIL = pytest.importorskip("PIL.Image")

os.environ["TOGA_BACKEND"] = "toga_dummy"

import toga  # noqa: E402
from travertino.colors import Color  # noqa: E402

from bookinventory import app as app_module  # noqa: E402
from bookinventory import glass, photos, themes  # noqa: E402
from bookinventory.aggregation import InventoryEntry  # noqa: E402
from bookinventory.app import (  # noqa: E402
    HISTORY,
    INVENTORY,
    RECOMMEND,
    REVIEW,
    BookInventory,
    describe_candidate,
    describe_entry,
    describe_scan,
)
from bookinventory.database import Book, Database, Scan  # noqa: E402
from bookinventory.ocr import OcrLine  # noqa: E402
from bookinventory.scanner import Candidate  # noqa: E402
from tests.conftest import BLANK_PHOTO, SHELF_A, SHELF_A_OVERLAP, SHELF_B, FakeOpenLibrary  # noqa: E402


def png_bytes() -> bytes:
    buffer = io.BytesIO()
    PIL.new("RGB", (4, 4), "white").save(buffer, format="PNG")
    return buffer.getvalue()


class FakeCameraRoll:
    """Queues canned OCR output for successive photos and records calls."""

    def __init__(self):
        self.queue: list[list[str]] = []
        self.images: list = []
        self.fail_with: Exception | None = None

    def recognize(self, image) -> list[OcrLine]:
        self.images.append(image)
        if self.fail_with:
            raise self.fail_with
        return [OcrLine(t) for t in (self.queue.pop(0) if self.queue else [])]


@pytest.fixture
def ol() -> FakeOpenLibrary:
    return FakeOpenLibrary()


@pytest.fixture
def roll() -> FakeCameraRoll:
    return FakeCameraRoll()


@pytest.fixture
def downloads() -> list[str]:
    return []


@pytest.fixture
def make_app(tmp_path, monkeypatch, ol, roll, downloads):
    monkeypatch.setenv("TOGA_DUMMY_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(BookInventory, "recognize", staticmethod(roll.recognize))
    monkeypatch.setattr(BookInventory, "fetch", staticmethod(ol))
    monkeypatch.setattr(BookInventory, "search", staticmethod(ol.search))
    monkeypatch.setattr(BookInventory, "fetch_work", staticmethod(ol.work))

    def download(url):
        downloads.append(url)
        return png_bytes()

    monkeypatch.setattr(BookInventory, "download_cover", staticmethod(download))
    created = []

    def factory(**kwargs):
        kwargs.setdefault("db_path", tmp_path / "books.sqlite3")
        application = BookInventory("Book Inventory", "com.example.bookinventory", **kwargs)
        created.append(application)
        return application

    yield factory
    for application in created:
        application.db.close()


@pytest.fixture
async def app(make_app) -> BookInventory:
    return make_app()


async def take(app: BookInventory, roll: FakeCameraRoll, lines: list[str] | None) -> None:
    """Simulate pressing *Take Photo* and taking a photo that OCRs to ``lines``."""
    if lines is None:
        app.camera._impl.simulate_photo(None)  # user cancelled
    else:
        roll.queue.append(lines)
        app.camera._impl.simulate_photo(toga.Image(png_bytes()))
    await app.take_photo_and_scan(app.camera_button)


async def scan_and_add(app: BookInventory, roll: FakeCameraRoll, *photos: list[str]) -> None:
    """Photograph ``photos`` and accept the review as-is."""
    for lines in photos:
        roll.queue.append(lines)
    await app.scan_images([toga.Image(png_bytes()) for _ in photos])
    assert app.mode == REVIEW
    app.confirm_review(app.add_button)


def status(app: BookInventory) -> str:
    """The status line with iOS-style wrapping undone."""
    return " ".join(app.status.text.split())


def titles(app: BookInventory) -> list[str]:
    return [row.title for row in app.inventory_list.data]


def row_for(app: BookInventory, title: str):
    return next(row for row in app.inventory_list.data if row.title == title)


def quantity_of(app: BookInventory, title: str) -> int:
    return next(e.quantity for e in app.entries if e.title == title)


def select(app: BookInventory, title: str) -> None:
    index = next(i for i, row in enumerate(app.inventory_list.data) if row.title == title)
    app.inventory_list._impl.simulate_selection(index)


def shown_rows(app: BookInventory) -> list[list[str]]:
    """The button labels currently shown below the list, row by row."""
    return [[button.text for button in row.children] for row in app.actions.children]


def shown_content(app: BookInventory) -> list:
    return list(app.content.children)


async def settle(app: BookInventory) -> None:
    """Let background cover loading finish."""
    if app._cover_task is not None:
        await app._cover_task


class TestStartup:
    async def test_empty_state(self, app):
        assert app.main_window.title == "Book Inventory"
        assert app.mode == INVENTORY
        assert len(app.inventory_list.data) == 0
        assert status(app) == "Ready."
        assert app.summary.text == "0 titles, 0 copies"
        assert shown_content(app) == [app.hint, app.inventory_list]
        assert shown_rows(app) == [
            ["Take Photo", "Choose Photos"],
            ["+1 copy", "−1 copy", "Delete"],
            ["History", "For you", "Export"],
        ]
        assert app.camera_button.enabled and app.library_button.enabled
        assert not app.location_select.enabled  # nothing recorded yet
        assert not app.dark_switch.value

    async def test_loads_existing_inventory_and_last_location(self, make_app, tmp_path):
        path = tmp_path / "existing.sqlite3"
        db = Database(str(path))
        db.record_scan([Book("Dune", "Frank Herbert")], "Study")
        db.set_setting("last_location", "Study")
        db.close()
        app = make_app(db_path=path)
        assert titles(app) == ["Dune"]
        assert row_for(app, "Dune").subtitle == "Frank Herbert · 1 copy · Study"
        assert app.location_input.value == "Study"
        assert [item.value for item in app.location_select.items] == ["Recent locations…", "Study"]
        assert shown_content(app) == [app.inventory_list]  # no empty-state hint

    async def test_default_db_in_app_data_dir(self, make_app):
        app = make_app(db_path=None)
        assert (app.paths.data / "books.sqlite3").exists()

    def test_main_factory(self, make_app, tmp_path):
        app = app_module.main(
            formal_name="Book Inventory",
            app_id="com.example.bookinventory",
            db_path=tmp_path / "m.sqlite3",
        )
        try:
            assert isinstance(app, BookInventory)
        finally:
            app.db.close()

    async def test_uses_detailed_lists_not_tables(self, app):
        for widget in app.lists.values():
            assert isinstance(widget, toga.DetailedList)
        assert app.inventory_list._primary_action == "Delete"
        assert app.review_list._primary_action == "Skip"
        assert app.history_list._primary_action == "Undo"

    async def test_only_text_buttons(self, app):
        for button in app.buttons:
            assert isinstance(button, toga.Button)
            assert button.text.strip() and button.icon is None
        # Rows keep their buttons apart.
        for row in (row for rows in app.rows.values() for row in rows):
            assert row.style.gap == app_module.BUTTON_GAP


class TestDescriptions:
    def test_describe_entry(self, dune):
        assert describe_entry(InventoryEntry(dune, ("Hall", "Study"))) == (
            "Frank Herbert · 1 copy · Hall, Study"
        )
        assert describe_entry(InventoryEntry(Book("Zine", quantity=2))) == (
            "Unknown author · 2 copies"
        )

    def test_describe_candidate(self, dune):
        candidate = Candidate(dune, "DUNE", 1.0, alternatives=[Book("Dune Messiah")])
        assert describe_candidate(candidate) == (
            "Frank Herbert · 1965 · from “DUNE” · 1 other match"
        )
        assert describe_candidate(Candidate(Book("Zine"), "zine", 0.7)) == (
            "Unknown author · from “zine”"
        )

    def test_describe_scan(self):
        scan = Scan(1, "2024-01-02T10:00:00+00:00", "Study", 3)
        assert describe_scan(scan) == ("Study · 3 books", "2024-01-02 10:00:00 UTC")
        assert describe_scan(Scan(2, "t", "", 1))[0] == "(unspecified) · 1 book"

    def test_wrap_status(self):
        text = app_module.wrap_status(
            "Exported inventory.csv and inventory.json. "
            "Open the Files app: On My iPhone › Book Inventory."
        )
        assert all(len(line) <= app_module.STATUS_WIDTH for line in text.splitlines())
        assert len(text.splitlines()) >= 2
        path = "/Users/someone/Library/Developer/CoreSimulator/Devices/ABC/Documents"
        assert f"{path}." in app_module.wrap_status(f"Exported x to {path}.").splitlines()
        assert app_module.wrap_status("") == ""

    def test_default_recognize_uses_bytes_off_ios(self, monkeypatch):
        seen = {}

        def recognize_lines(image_bytes=None, native_image=None):
            seen.update(bytes=image_bytes, native=native_image)
            return []

        monkeypatch.setattr(app_module, "recognize_lines", recognize_lines)
        monkeypatch.setattr(app_module.sys, "platform", "linux")
        app_module.default_recognize(toga.Image(png_bytes()))
        assert seen["native"] is None and seen["bytes"]

    def test_default_recognize_uses_native_image_on_ios(self, monkeypatch):
        seen = {}

        def recognize_lines(image_bytes=None, native_image=None):
            seen.update(bytes=image_bytes, native=native_image)
            return []

        monkeypatch.setattr(app_module, "recognize_lines", recognize_lines)
        monkeypatch.setattr(app_module.sys, "platform", "ios")
        image = toga.Image(png_bytes())
        app_module.default_recognize(image)
        assert seen["native"] is image._impl.native and seen["bytes"] is None


class TestScanAndReview:
    async def test_scan_enters_review(self, app, roll):
        app.location_input.value = "Study"
        await take(app, roll, SHELF_A)
        assert app.mode == REVIEW
        assert shown_content(app) == [app.review_list]
        assert shown_rows(app) == [["Cancel", "Add 3 books"]]
        assert [row.title for row in app.review_list.data] == [
            "✓ The Hobbit",
            "✓ Dune",
            "✓ Nineteen Eighty-Four",
        ]
        assert app.summary.text == "3 of 3 selected"
        assert app.add_button.text == "Add 3 books"
        assert status(app).startswith("Found 3 books; 3 lines not matched.")
        assert app.db.scans() == []  # nothing recorded yet
        assert app.camera_button.enabled and not app.spinner.is_running

    async def test_confirm_records_and_remembers_location(self, app, roll):
        app.location_input.value = "Study"
        await take(app, roll, SHELF_A)
        app.confirm_review(app.add_button)
        assert app.mode == INVENTORY
        assert titles(app) == ["Dune", "Nineteen Eighty-Four", "The Hobbit"]
        assert status(app) == "Found 3 books (3 new) at Study."
        assert app.summary.text == "3 titles, 3 copies, 1 location"
        assert shown_content(app) == [app.inventory_list]
        assert shown_rows(app)[0] == ["Take Photo", "Choose Photos"]
        assert app.db.get_setting("last_location") == "Study"
        assert [i.value for i in app.location_select.items] == ["Recent locations…", "Study"]
        [scan] = app.db.scans()
        assert (scan.location, scan.books_found) == ("Study", 3)

    async def test_toggle_skip_and_next_match(self, app, roll):
        await take(app, roll, ["DUNE"])
        app.review_list._impl.simulate_selection(0)  # tap: exclude
        assert app.review_list.data[0].title == "○ Dune"
        assert app.summary.text == "0 of 1 selected"
        assert not app.add_button.enabled
        app.review_list._impl.simulate_selection(0)  # tap again: include
        assert app.review_list.data[0].title == "✓ Dune"

        app.review_list.on_secondary_action(row=app.review_list.data[0])  # next match
        alternative = app.review_list.data[0].title
        assert alternative != "✓ Dune" and alternative.startswith("✓ ")
        assert status(app) == f"Using “{alternative[2:]}” for “DUNE”."
        for _ in range(len(app.result.candidates[0].alternatives)):
            app.review_list.on_secondary_action(row=app.review_list.data[0])
        assert app.review_list.data[0].title == "✓ Dune"  # cycled all the way round

        app.review_list.on_primary_action(row=app.review_list.data[0])  # skip
        assert app.review_list.data[0].title == "○ Dune"
        app.confirm_review(app.add_button)
        assert titles(app) == []
        assert status(app) == "No books found."

    async def test_review_handlers_tolerate_stale_state(self, app, roll):
        await take(app, roll, ["DUNE"])
        stale = type("Row", (), {"index": 99})()
        app.next_match_row(None, row=stale)  # index out of range
        app.skip_row(None, row=type("Row", (), {})())  # row without index
        app.cancel_review(None)
        app.toggle_row(None)  # no result any more
        app.next_match_row(None, row=stale)
        app.confirm_review(None)
        assert app.mode == INVENTORY and app.db.scans() == []

    async def test_next_match_without_alternatives(self, app, roll):
        await take(app, roll, ["Pride and Prejudice"])
        app.review_list.on_secondary_action(row=app.review_list.data[0])
        assert status(app) == "No other matches for “Pride and Prejudice”."

    async def test_cancel_review(self, app, roll):
        await take(app, roll, SHELF_B)
        app.cancel_review(app.cancel_button)
        assert app.mode == INVENTORY
        assert app.db.scans() == [] and titles(app) == []
        assert status(app) == "Scan discarded."
        assert app.summary.text == "0 titles, 0 copies"

    async def test_multiple_photos_merge_into_one_review(self, app, roll):
        roll.queue.extend([SHELF_A, SHELF_A_OVERLAP])
        await app.scan_images([toga.Image(png_bytes()), toga.Image(png_bytes())])
        assert len(roll.images) == 2
        assert [row.title for row in app.review_list.data] == [
            "✓ The Hobbit",
            "✓ Dune",
            "✓ Nineteen Eighty-Four",
            "✓ Animal Farm",
        ]
        assert status(app).startswith("Found 4 books")

    async def test_overlapping_photo_same_shelf_does_not_double_count(self, app, roll):
        app.location_input.value = "Study"
        await scan_and_add(app, roll, SHELF_A)
        await scan_and_add(app, roll, SHELF_A_OVERLAP)
        assert quantity_of(app, "Dune") == 1
        assert status(app) == "Found 3 books (1 new, 2 already listed) at Study."

    async def test_same_title_on_another_shelf_is_another_copy(self, app, roll):
        app.location_input.value = "Study"
        await scan_and_add(app, roll, SHELF_A)
        app.location_input.value = "Bedroom"
        await scan_and_add(app, roll, SHELF_B)
        assert quantity_of(app, "Dune") == 2
        assert row_for(app, "Dune").subtitle == "Frank Herbert · 2 copies · Bedroom, Study"
        assert app.summary.text == "5 titles, 6 copies, 2 locations, 1 duplicated"

    async def test_recent_location_selection_fills_the_field(self, app, roll):
        app.location_input.value = "Study"
        await scan_and_add(app, roll, SHELF_B)
        app.location_input.value = ""
        app.location_select.value = "Study"
        assert app.location_input.value == "Study"
        app.location_select.value = "Recent locations…"
        assert app.location_input.value == "Study"  # placeholder doesn't clear it

    async def test_cancelled_photo(self, app, roll):
        await take(app, roll, None)
        assert app.mode == INVENTORY and roll.images == []

    async def test_no_text_in_photo(self, app, roll):
        await take(app, roll, BLANK_PHOTO)
        assert status(app).startswith("No text found")
        assert app.mode == INVENTORY

    async def test_text_but_no_known_books(self, app, roll):
        await take(app, roll, ["Garbled Spine", "Unknown Author"])
        assert status(app).startswith("No books recognised")

    async def test_lookup_outage(self, app, roll, ol):
        ol.fail_with = OSError("offline")
        await take(app, roll, SHELF_A)
        assert status(app).startswith("No books recognised")

    async def test_ocr_failure_reported(self, app, roll):
        roll.fail_with = RuntimeError("Vision exploded")
        await take(app, roll, SHELF_A)
        assert status(app) == "Scan failed: Vision exploded"
        assert app.camera_button.enabled and app.mode == INVENTORY

    async def test_camera_permission_denied(self, app, roll):
        app.camera._impl._has_permission = 0
        await app.take_photo_and_scan(app.camera_button)
        assert status(app).startswith("Camera access denied")
        assert roll.images == []

    async def test_busy_state_while_scanning(self, app, roll, monkeypatch):
        observed = []

        def recognize(image):
            observed.append((app.camera_button.enabled, app.spinner.is_running, status(app)))
            return [OcrLine(t) for t in SHELF_B]

        monkeypatch.setattr(BookInventory, "recognize", staticmethod(recognize))
        await app.scan_images([toga.Image(png_bytes())])
        assert observed == [(False, True, "Reading spines in 1 photo and looking up titles…")]
        assert app.camera_button.enabled and not app.spinner.is_running

    async def test_lookups_are_cached_across_photos(self, app, roll, ol):
        await scan_and_add(app, roll, SHELF_B)
        before = len(ol.queries)
        await scan_and_add(app, roll, SHELF_B)
        assert len(ol.queries) == before


class TestPhotoSource:
    async def test_camera_button_falls_back_to_library(self, app, roll, monkeypatch):
        monkeypatch.setattr(photos, "camera_available", lambda: False)
        seen = []

        async def choose(window):
            seen.append((window, status(app)))
            return [toga.Image(png_bytes())]

        monkeypatch.setattr(photos, "choose_photos", choose)
        roll.queue.append(list(SHELF_B))
        await app.take_photo_and_scan(app.camera_button)
        assert seen == [
            (app.main_window, "No camera on this device; choose photos from your library instead.")
        ]
        assert app.mode == REVIEW

    async def test_library_button(self, app, roll, monkeypatch):
        async def choose(window):
            return [toga.Image(png_bytes()), toga.Image(png_bytes())]

        monkeypatch.setattr(photos, "choose_photos", choose)
        roll.queue.extend([SHELF_A, SHELF_B])
        await app.choose_photos_and_scan(app.library_button)
        assert len(roll.images) == 2 and app.mode == REVIEW

    async def test_library_cancel(self, app, roll, monkeypatch):
        async def choose(window):
            return []

        monkeypatch.setattr(photos, "choose_photos", choose)
        await app.choose_photos_and_scan(app.library_button)
        assert roll.images == [] and status(app) == "Ready."

    async def test_unreadable_file_is_reported(self, app, roll, monkeypatch):
        async def choose(window):
            raise ValueError("Unable to load image from shelf.heic")

        monkeypatch.setattr(photos, "choose_photos", choose)
        await app.choose_photos_and_scan(app.library_button)
        assert status(app) == "Couldn't open that image: Unable to load image from shelf.heic"

    async def test_desktop_file_dialog_end_to_end(self, app, roll, tmp_path, monkeypatch):
        monkeypatch.setattr(photos, "is_ios", lambda: False)
        paths = []
        for name in ("a.png", "b.png"):
            (tmp_path / name).write_bytes(png_bytes())
            paths.append(tmp_path / name)
        app.main_window._impl.dialog_responses["OpenFileDialog"] = [paths]
        roll.queue.extend([SHELF_A, SHELF_B])
        await app.choose_photos_and_scan(app.library_button)
        assert len(roll.images) == 2 and app.mode == REVIEW

    async def test_desktop_file_dialog_cancelled(self, app, roll, monkeypatch):
        monkeypatch.setattr(photos, "is_ios", lambda: False)
        app.main_window._impl.dialog_responses["OpenFileDialog"] = [None]
        await app.choose_photos_and_scan(app.library_button)
        assert roll.images == []


class TestHistory:
    async def test_history_mode_and_undo(self, app, roll):
        app.location_input.value = "Study"
        await scan_and_add(app, roll, SHELF_A)
        app.location_input.value = "Hall"
        await scan_and_add(app, roll, SHELF_B)
        app.show_history(app.history_button)
        assert app.mode == HISTORY
        assert shown_content(app) == [app.history_list]
        assert shown_rows(app) == [["Back to inventory"]]
        assert not app.location_input.enabled
        assert [row.title for row in app.history_list.data] == [
            "Hall · 3 books",
            "Study · 3 books",
        ]
        assert status(app) == "2 scans recorded. Swipe one to undo it."

        app.history_list.on_primary_action(row=app.history_list.data[0])  # undo Hall
        assert status(app) == "Scan undone."
        assert [row.title for row in app.history_list.data] == ["Study · 3 books"]
        app.show_inventory(app.back_button)
        assert app.mode == INVENTORY and app.location_input.enabled
        assert "Fahrenheit 451" not in titles(app)
        assert quantity_of(app, "Dune") == 1

    async def test_undo_unknown_scan(self, app):
        app.show_history(None)
        app.undo_row(None, row=type("Row", (), {"id": 999})())
        assert status(app) != "Scan undone."


class TestRecommendations:
    async def test_for_you_lists_suggestions_with_reasons(self, app, roll, ol):
        await scan_and_add(app, roll, ["DUNE", "Nineteen Eighty-Four George Orwell"])
        await app.show_recommendations(app.recommend_button)
        assert app.mode == RECOMMEND
        assert shown_content(app) == [app.recommend_list]
        assert shown_rows(app) == [["Back to inventory", "Refresh"]]
        titles_shown = [row.title for row in app.recommend_list.data]
        assert titles_shown[0] == "Children of Dune"
        assert "Dune" not in titles_shown  # owned
        assert "More by Frank Herbert" in app.recommend_list.data[0].subtitle
        assert app.summary.text.endswith("suggestions")
        assert status(app).startswith("Based on 2 books: Frank Herbert, George Orwell")
        assert not app.spinner.is_running and app.refresh_button.enabled
        assert any("author" in s for s in ol.searches)

    async def test_tapping_a_suggestion_shows_its_summary(self, app, roll):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        assert status(app).endswith("Tap a suggestion for a summary.")
        app.recommend_list._impl.simulate_selection(0)  # Children of Dune
        assert status(app).startswith(
            "Children of Dune: Nine years after the events of Dune Messiah"
        )
        index = next(i for i, r in enumerate(app.recommendations) if r.book.title == "Dune Messiah")
        app.recommend_list._impl.simulate_selection(index)
        assert status(app) == "No summary available for “Dune Messiah”."

    async def test_summary_tap_ignored_while_refreshing_or_stale(self, app, roll):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        app.recommendations = []
        app.recommend_list._impl.simulate_selection(0)  # row no longer backed by a rec
        assert not status(app).startswith("Children of Dune")

    async def test_empty_inventory(self, app):
        await app.show_recommendations(app.recommend_button)
        assert app.mode == RECOMMEND
        assert len(app.recommend_list.data) == 0
        assert status(app).startswith("Add some books")
        assert app.summary.text == "0 suggestions"

    async def test_dismiss_is_remembered(self, app, roll):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        first = app.recommend_list.data[0]
        app.recommend_list.on_primary_action(row=first)
        assert status(app) == f"Dismissed “{first.title}”."
        assert first.title not in [row.title for row in app.recommend_list.data]
        assert first.key in app.dismissed
        await app.refresh_recommendations(app.refresh_button)
        assert first.title not in [row.title for row in app.recommend_list.data]

    async def test_own_it_adds_to_inventory(self, app, roll):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        row = app.recommend_list.data[0]
        app.recommend_list.on_secondary_action(row=row)
        assert status(app) == f"Added “{row.title}” to your inventory."
        assert row.title in titles(app)
        assert row.title not in [r.title for r in app.recommend_list.data]
        app.show_inventory(app.recommend_back_button)
        assert app.mode == INVENTORY and app.summary.text.startswith("2 titles")

    async def test_own_it_when_already_owned(self, app, roll):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        row = app.recommend_list.data[0]
        app.db.add(Book(row.title, "Frank Herbert", work_key=row.key.removeprefix("work:")))
        app.recommend_list.on_secondary_action(row=row)
        assert status(app) == f"“{row.title}” is already in your inventory."

    async def test_stale_row_is_ignored(self, app, roll):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        stale = type("Row", (), {"key": "work:/nope"})()
        app.recommend_list.on_primary_action(row=stale)
        app.recommend_list.on_secondary_action(row=stale)
        assert app.dismissed == set()

    async def test_suggestions_are_reused_until_the_inventory_changes(self, app, roll, ol):
        await scan_and_add(app, roll, ["DUNE"])
        await app.show_recommendations(app.recommend_button)
        count = len(ol.searches)
        app.show_inventory(None)
        await app.show_recommendations(app.recommend_button)
        assert len(ol.searches) == count  # reused
        app.show_inventory(None)
        await scan_and_add(app, roll, ["The Hobbit"])
        await app.show_recommendations(app.recommend_button)
        assert len(ol.searches) > count  # recomputed for the new inventory

    async def test_failure_is_reported(self, app, roll, monkeypatch):
        await scan_and_add(app, roll, ["DUNE"])

        def boom(*args, **kwargs):
            raise RuntimeError("no network")

        monkeypatch.setattr(app.recommender, "recommend", boom)
        await app.show_recommendations(app.recommend_button)
        assert status(app) == "Couldn't fetch recommendations: no network"
        assert app.refresh_button.enabled and not app.spinner.is_running

    async def test_nothing_to_suggest(self, app, roll, monkeypatch):
        await scan_and_add(app, roll, ["DUNE"])
        monkeypatch.setattr(app.recommender, "recommend", lambda *a, **k: [])
        await app.show_recommendations(app.recommend_button)
        assert status(app).startswith("No new suggestions")

    async def test_corrupt_dismissed_setting(self, app):
        app.db.set_setting("dismissed_recommendations", "not json")
        assert app.dismissed == set()

    async def test_recommendation_covers_are_loaded(self, app, roll, downloads):
        await scan_and_add(app, roll, ["DUNE"])
        await settle(app)
        await app.show_recommendations(app.recommend_button)
        await settle(app)
        assert any("8244209" in url for url in downloads)  # Children of Dune's cover
        assert isinstance(app.recommend_list.data[0].icon, toga.Icon)


class TestCovers:
    async def test_covers_are_downloaded_and_shown(self, app, roll, downloads):
        await scan_and_add(app, roll, ["DUNE"])
        await settle(app)
        assert downloads == ["https://covers.openlibrary.org/b/id/12369637-M.jpg"]
        assert (app.cover_cache / "12369637.png").exists()
        assert isinstance(row_for(app, "Dune").icon, toga.Icon)
        # A refresh reuses the cache: no new download, icon present immediately.
        app.refresh()
        await settle(app)
        assert downloads == ["https://covers.openlibrary.org/b/id/12369637-M.jpg"]
        assert isinstance(row_for(app, "Dune").icon, toga.Icon)

    async def test_download_failure_leaves_row_without_icon(self, app, roll, monkeypatch):
        def failing(url):
            raise OSError("offline")

        monkeypatch.setattr(BookInventory, "download_cover", staticmethod(failing))
        await scan_and_add(app, roll, ["DUNE"])
        await settle(app)
        assert row_for(app, "Dune").icon is None

    async def test_no_task_when_nothing_missing(self, app):
        app.refresh()
        assert app._cover_task is None


class TestQuantityButtons:
    async def test_plus_minus(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        select(app, "Dune")
        app.add_copy(None)
        app.add_copy(None)
        assert quantity_of(app, "Dune") == 3
        assert status(app) == "Dune: 3 copies."
        app.remove_copy(None)
        assert quantity_of(app, "Dune") == 2

    async def test_minus_floors_at_zero_and_singular(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        select(app, "Dune")
        app.remove_copy(None)
        app.remove_copy(None)
        assert status(app) == "Dune: 0 copies."
        app.add_copy(None)
        assert status(app) == "Dune: 1 copy."

    async def test_without_selection(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        app.add_copy(None)
        assert status(app) == "Select a book to change its number of copies."


class TestSwipeActionsAndDelete:
    async def test_swipe_to_delete_and_add_copy(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        app.inventory_list.on_secondary_action(row=row_for(app, "Dune"))
        assert quantity_of(app, "Dune") == 2
        app.inventory_list.on_primary_action(row=row_for(app, "Dune"))
        assert "Dune" not in titles(app)
        assert status(app) == "Deleted Dune."

    async def test_action_on_row_deleted_elsewhere(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        row = row_for(app, "Dune")
        app.db.delete(row.id)
        app.inventory_list.on_secondary_action(row=row)  # must not raise
        assert "Dune" not in titles(app)

    async def test_delete_selected(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        select(app, "Dune")
        app.delete_selected(None)
        assert titles(app) == ["Fahrenheit 451", "Pride and Prejudice"]

    async def test_delete_without_selection(self, app, roll):
        await scan_and_add(app, roll, SHELF_B)
        app.delete_selected(None)
        assert status(app) == "Select a book to delete."


class TestExport:
    async def test_export_to_documents(self, app, roll, tmp_path):
        app.location_input.value = "Study"
        await scan_and_add(app, roll, SHELF_B)
        documents = tmp_path / "home" / "documents"
        documents.mkdir(parents=True)  # the dummy backend doesn't create it
        app.export(None)
        assert status(app) == f"Exported inventory.csv and inventory.json to {documents}."
        assert "Pride and Prejudice" in (documents / "inventory.csv").read_text(encoding="utf-8")

    async def test_export_falls_back_to_data_dir(self, app):
        app.export(None)
        assert app.export_directory() == app.paths.data
        assert (app.paths.data / "inventory.csv").exists()

    async def test_export_failure_reported(self, app, monkeypatch):
        def fail(directory):
            raise OSError("read-only file system")

        monkeypatch.setattr(app, "export_inventory", fail)
        app.export(None)
        assert status(app) == "Export failed: read-only file system"

    async def test_ios_share_sheet(self, app, monkeypatch):
        monkeypatch.setattr(glass, "share_files", lambda application, paths: True)
        app.export(None)
        assert status(app) == "Sharing inventory.csv and inventory.json."

    async def test_ios_without_share_sheet(self, app, monkeypatch):
        monkeypatch.setattr(glass, "share_files", lambda application, paths: False)
        monkeypatch.setattr(app_module.sys, "platform", "ios")
        app.export(None)
        assert status(app).startswith("Exported inventory.csv and inventory.json. Open the Files")


class TestTheme:
    async def test_light_palette_applied_at_startup(self, app):
        light = themes.LIGHT
        assert str(app.root.style.background_color) == str(Color.parse(light.background))
        assert str(app.camera_button.style.color) == str(Color.parse(light.accent))
        assert str(app.delete_button.style.color) == str(Color.parse(light.danger))
        assert str(app.history_button.style.color) == str(Color.parse(light.action))
        assert str(app.status.style.color) == str(Color.parse(light.muted))
        assert str(app.inventory_list.style.background_color) == str(Color.parse(light.surface))

    async def test_dark_mode_switch_applies_and_persists(self, app, make_app, monkeypatch):
        appearances = []
        monkeypatch.setattr(glass, "set_appearance", lambda a, dark: appearances.append(dark))
        app.dark_switch.value = True
        dark = themes.DARK
        assert app.dark
        assert str(app.root.style.background_color) == str(Color.parse(dark.background))
        assert str(app.summary.style.color) == str(Color.parse(dark.text))
        assert str(app.camera_button.style.color) == str(Color.parse(dark.accent))
        assert status(app) == "Dark mode on."
        assert appearances == [True]
        assert app.db.get_setting("dark_mode") == "1"

        app.dark_switch.value = False
        assert status(app) == "Dark mode off."
        assert str(app.root.style.background_color) == str(Color.parse(themes.LIGHT.background))
        assert appearances == [True, False]

        # The preference is remembered across launches.
        app.dark_switch.value = True
        app.db.close()
        again = make_app(db_path=app._db_path)
        assert again.dark and again.dark_switch.value
        assert str(again.root.style.background_color) == str(Color.parse(dark.background))

    async def test_ios_appearance_applied_at_startup(self, make_app, monkeypatch):
        applied = []
        monkeypatch.setattr(glass, "apply", lambda a, dark: applied.append(dark))
        make_app()
        assert applied == [False]


class TestEntryPoint:
    def test_package_is_runnable_as_main(self, monkeypatch):
        """Briefcase starts the app with ``python -m bookinventory``; that needs
        ``bookinventory/__main__.py`` to exist and to start the main loop."""
        import runpy

        started = []

        class FakeApp:
            def main_loop(self):
                started.append("main_loop")

        monkeypatch.setattr(app_module, "main", lambda **kwargs: FakeApp())
        runpy.run_module("bookinventory", run_name="__main__", alter_sys=True)
        assert started == ["main_loop"]


async def test_asyncio_loop_is_available(app):
    assert app.loop is asyncio.get_running_loop()
