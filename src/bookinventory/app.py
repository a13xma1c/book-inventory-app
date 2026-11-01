"""Book Inventory: photograph a bookshelf and list the books on it.

The window has three modes, sharing the location field and status line:

* **inventory** - the aggregated inventory; swipe to delete / add a copy.
* **review** - the books a scan found, before anything is recorded: tap to
  include/exclude, swipe to skip or switch to the next alternative match.
* **history** - the recorded scans; swipe to undo one.
* **for you** - recommendations based on the inventory (see
  :mod:`bookinventory.recommend`); swipe to dismiss or to add one you own.

Actions are plain text buttons below the list, one set per mode; the
colour scheme (light or dark, see :mod:`bookinventory.themes`) is chosen with
a switch and remembered.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any

import toga
from toga.style import Pack
from toga.style.pack import BOLD, CENTER, COLUMN, ROW

from bookinventory import covers, glass, photos, themes
from bookinventory.aggregation import InventoryEntry, build_inventory, summarize, to_csv, to_json
from bookinventory.database import Database, Scan
from bookinventory.lookup import CachedFetch, _fetch, fetch_work, find_matches, search_request
from bookinventory.ocr import OcrLine, recognize_lines
from bookinventory.recommend import Recommendation, Recommender, build_profile
from bookinventory.scanner import Candidate, ScanResult, merge_results, scan_lines

#: iOS labels don't word-wrap; they only break on explicit newlines. Status
#: messages are wrapped to this many characters so nothing is clipped.
STATUS_WIDTH = 44
RECENT_PLACEHOLDER = "Recent locations…"
LAST_LOCATION = "last_location"
DARK_MODE = "dark_mode"
DISMISSED = "dismissed_recommendations"
BUTTON_GAP = 10

EMPTY_HINT = (
    "No books yet.\n"
    "Type where the shelf is, then take a photo of it\n"
    "or choose photos from your library.\n"
    "Swipe a book left to delete it, right to add a copy."
)

INVENTORY, REVIEW, HISTORY, RECOMMEND = "inventory", "review", "history", "recommend"


def describe_entry(entry: InventoryEntry) -> str:
    """Second line of an inventory row: author, copies and where the title lives."""
    parts = [entry.authors or "Unknown author"]
    parts.append(f"{entry.quantity} cop{'ies' if entry.quantity != 1 else 'y'}")
    if entry.locations:
        parts.append(", ".join(entry.locations))
    return " · ".join(parts)


def describe_candidate(candidate: Candidate) -> str:
    """Second line of a review row: author, year, what it was read from, alternatives."""
    book = candidate.book
    parts = [book.authors or "Unknown author"]
    if book.year:
        parts.append(book.year)
    parts.append(f"from “{candidate.query}”")
    if candidate.alternatives:
        n = len(candidate.alternatives)
        parts.append(f"{n} other match{'es' if n != 1 else ''}")
    return " · ".join(parts)


def describe_scan(scan: Scan) -> tuple[str, str]:
    """Title and subtitle of a history row."""
    where = scan.location or "(unspecified)"
    title = f"{where} · {scan.books_found} book{'s' if scan.books_found != 1 else ''}"
    return title, scan.scanned_at.replace("T", " ").replace("+00:00", " UTC")


def wrap_status(text: str) -> str:
    """Insert line breaks so a status message fits an iOS label (breaks at spaces only)."""
    return "\n".join(
        textwrap.fill(line, STATUS_WIDTH, break_long_words=False, break_on_hyphens=False)
        for line in text.splitlines()
    )


def default_recognize(image: toga.Image) -> list[OcrLine]:
    """OCR a Toga image: hand Vision the native UIImage on iOS (no PNG round-trip)."""
    native = getattr(image._impl, "native", None) if sys.platform == "ios" else None
    if native is not None:
        return recognize_lines(native_image=native)
    return recognize_lines(image.data)


def as_ocr_lines(lines) -> list[OcrLine]:
    return [line if isinstance(line, OcrLine) else OcrLine(str(line)) for line in lines]


def rows_of(widget: toga.DetailedList) -> list:
    """The rows of a list widget (Toga's sources are sequences, not iterators)."""
    data: Any = widget.data
    return [data[i] for i in range(len(data))]


def row_id(row: object) -> int:
    """The database id stored on a list row (rows are dynamic Toga objects)."""
    return int(row.id)  # type: ignore[attr-defined]


def text_button(text: str, on_press: Callable, role: str = "action") -> toga.Button:
    """A plain text button. ``role`` ("accent", "action", "danger") picks its colour."""
    button = toga.Button(
        text,
        on_press=on_press,
        style=Pack(
            flex=1, margin=(4, BUTTON_GAP // 2), font_weight=BOLD if role == "accent" else "normal"
        ),
    )
    button.role = role
    return button


def button_row(*buttons: toga.Button) -> toga.Box:
    """Buttons side by side, evenly sized and clearly separated."""
    return toga.Box(
        children=list(buttons), style=Pack(direction=ROW, gap=BUTTON_GAP, margin=(2, 0))
    )


class BookInventory(toga.App):
    # Overridable so tests (and desktop builds without Vision/network) can swap them out.
    recognize = staticmethod(default_recognize)
    fetch = staticmethod(_fetch)
    search = staticmethod(search_request)
    fetch_work = staticmethod(fetch_work)
    download_cover = staticmethod(covers.download)

    def __init__(self, *args, db_path: str | Path | None = None, **kwargs):
        self._db_path = db_path
        super().__init__(*args, **kwargs)

    # ------------------------------------------------------------------ setup

    def startup(self):
        if self._db_path is None:
            self.paths.data.mkdir(parents=True, exist_ok=True)
            self._db_path = self.paths.data / "books.sqlite3"
        self.db = Database(str(self._db_path))
        self.cached_fetch = CachedFetch(self.fetch)
        self.recommender = Recommender(search=self.search, fetch_work=self.fetch_work)
        self.recommendations: list[Recommendation] = []
        self.mode = INVENTORY
        self.result: ScanResult | None = None
        self._refreshing = False
        self.entries: list[InventoryEntry] = []
        self.scans: list[Scan] = []
        self._cover_task: asyncio.Task | None = None

        self.dark = self.db.get_setting(DARK_MODE) == "1"

        self.location_input = toga.TextInput(
            placeholder="Where is this shelf? (e.g. Study, shelf 2)",
            value=self.db.get_setting(LAST_LOCATION),
            style=Pack(flex=1, margin=4),
        )
        self.location_select = toga.Selection(
            items=[RECENT_PLACEHOLDER], on_change=self.pick_recent_location, style=Pack(margin=4)
        )
        self.summary = toga.Label("", style=Pack(margin=(0, 4), font_weight=BOLD))
        self.status = toga.Label("", style=Pack(flex=1, margin=(2, 4)))
        self.spinner = toga.ActivityIndicator(style=Pack(margin=(2, 4)))
        self.hint = toga.Label(EMPTY_HINT, style=Pack(margin=(24, 8), text_align=CENTER))
        self.dark_switch = toga.Switch(
            "Dark mode", value=self.dark, on_change=self.toggle_dark_mode, style=Pack(margin=4)
        )

        # toga.Table isn't available on iOS; DetailedList is, and gives us native
        # swipe actions. One list per mode, since swipe labels are fixed at creation.
        self.inventory_list = toga.DetailedList(
            primary_action="Delete",
            on_primary_action=self.delete_row,
            secondary_action="+1 copy",
            on_secondary_action=self.add_copy_to_row,
            style=Pack(flex=1),
        )
        self.review_list = toga.DetailedList(
            primary_action="Skip",
            on_primary_action=self.skip_row,
            secondary_action="Next match",
            on_secondary_action=self.next_match_row,
            on_select=self.toggle_row,
            style=Pack(flex=1),
        )
        self.history_list = toga.DetailedList(
            primary_action="Undo",
            on_primary_action=self.undo_row,
            secondary_action=None,
            style=Pack(flex=1),
        )
        self.recommend_list = toga.DetailedList(
            primary_action="Dismiss",
            on_primary_action=self.dismiss_row,
            secondary_action="I own it",
            on_secondary_action=self.own_row,
            on_select=self.show_summary,
            style=Pack(flex=1),
        )
        self.lists = {
            INVENTORY: self.inventory_list,
            REVIEW: self.review_list,
            HISTORY: self.history_list,
            RECOMMEND: self.recommend_list,
        }

        self.camera_button = text_button("Take Photo", self.take_photo_and_scan, "accent")
        self.library_button = text_button("Choose Photos", self.choose_photos_and_scan, "accent")
        self.plus_button = text_button("+1 copy", self.add_copy)
        self.minus_button = text_button("−1 copy", self.remove_copy)
        self.delete_button = text_button("Delete", self.delete_selected, "danger")
        self.history_button = text_button("History", self.show_history)
        self.recommend_button = text_button("For you", self.show_recommendations)
        self.export_button = text_button("Export", self.export)
        self.add_button = text_button("Add to inventory", self.confirm_review, "accent")
        self.cancel_button = text_button("Cancel", self.cancel_review)
        self.back_button = text_button("Back to inventory", self.show_inventory, "accent")
        self.recommend_back_button = text_button("Back to inventory", self.show_inventory, "accent")
        self.refresh_button = text_button("Refresh", self.refresh_recommendations)
        self.buttons = [
            self.camera_button,
            self.library_button,
            self.plus_button,
            self.minus_button,
            self.delete_button,
            self.history_button,
            self.recommend_button,
            self.export_button,
            self.add_button,
            self.cancel_button,
            self.back_button,
            self.recommend_back_button,
            self.refresh_button,
        ]

        # The rows of text buttons shown below the list, per mode. Toga's
        # ``display: none`` isn't honoured by native layouts, so rows are
        # added to / removed from the ``actions`` box instead.
        self.rows = {
            INVENTORY: [
                button_row(self.camera_button, self.library_button),
                button_row(self.plus_button, self.minus_button, self.delete_button),
                button_row(self.history_button, self.recommend_button, self.export_button),
            ],
            REVIEW: [button_row(self.cancel_button, self.add_button)],
            HISTORY: [button_row(self.back_button)],
            RECOMMEND: [button_row(self.recommend_back_button, self.refresh_button)],
        }
        self.content = toga.Box(style=Pack(direction=COLUMN, flex=1))
        self.actions = toga.Box(style=Pack(direction=COLUMN, margin=(6, 0, 12, 0)))
        self.root = toga.Box(
            children=[
                toga.Box(
                    children=[self.location_input, self.location_select],
                    style=Pack(direction=ROW),
                ),
                self.summary,
                toga.Box(children=[self.status, self.spinner], style=Pack(direction=ROW)),
                self.content,
                toga.Divider(style=Pack(margin=(4, 4))),
                self.actions,
                toga.Box(children=[self.dark_switch], style=Pack(direction=ROW)),
            ],
            style=Pack(direction=COLUMN, margin=8),
        )

        window = toga.MainWindow(title=self.formal_name)
        self.main_window = window
        window.content = self.root
        self.apply_theme()
        self.refresh()
        self.show_mode(INVENTORY)
        self.say("Ready.")
        window.show()

        # On iOS: keep the layout out of the home-indicator zone and switch
        # UIKit's appearance with our theme. A no-op elsewhere.
        glass.apply(self, self.dark)

    # ------------------------------------------------------------------ theme

    @property
    def palette(self) -> themes.Palette:
        return themes.palette(self.dark)

    def apply_theme(self) -> None:
        """Push the current palette into every widget's style."""
        p = self.palette
        self.root.style.background_color = p.background
        self.content.style.background_color = p.background
        self.actions.style.background_color = p.background
        self.summary.style.color = p.text
        self.status.style.color = p.muted
        self.hint.style.color = p.muted
        self.dark_switch.style.color = p.text
        for widget in self.lists.values():
            widget.style.background_color = p.surface
        for button in self.buttons:
            button.style.color = {"accent": p.accent, "danger": p.danger}.get(
                button.role,
                p.action,
            )

    def toggle_dark_mode(self, widget, **kwargs):
        self.dark = bool(self.dark_switch.value)
        self.db.set_setting(DARK_MODE, "1" if self.dark else "0")
        self.apply_theme()
        glass.set_appearance(self, self.dark)
        self.say("Dark mode on." if self.dark else "Dark mode off.")

    # ------------------------------------------------------------------ state

    @property
    def current_location(self) -> str:
        return " ".join((self.location_input.value or "").split())

    @property
    def window(self) -> toga.Window:
        assert isinstance(self.main_window, toga.Window)
        return self.main_window

    def say(self, message: str) -> None:
        """Show a short message in the status line (wrapped for iOS labels)."""
        self.status.text = wrap_status(message)

    def show_mode(self, mode: str) -> None:
        """Swap the list and the button rows shown for ``mode``."""
        self.mode = mode
        self.content.clear()
        if mode == INVENTORY and not self.entries:
            self.content.add(self.hint)
        self.content.add(self.lists[mode])
        self.actions.clear()
        self.actions.add(*self.rows[mode])
        self.location_input.enabled = mode != HISTORY

    def refresh(self):
        """Reload everything from the database."""
        self.entries = build_inventory(self.db)
        self.recommendations = []  # the inventory changed: suggestions are stale
        self.inventory_list.data = [
            {
                "title": e.title,
                "subtitle": describe_entry(e),
                "icon": self.cover_icon(e.book),
                "id": e.book.id,
                "cover": covers.cover_id(e.book.cover_url),
            }
            for e in self.entries
        ]
        self.summary.text = summarize(self.entries).describe()
        if self.mode == INVENTORY:
            self.show_mode(INVENTORY)  # the empty-state hint may need to come or go
        known = self.db.locations()
        self.location_select.items = [RECENT_PLACEHOLDER, *known]
        self.location_select.enabled = bool(known)
        self.scans = self.db.scans()
        self.history_list.data = [
            dict(zip(("title", "subtitle"), describe_scan(s), strict=True), icon=None, id=s.id)
            for s in reversed(self.scans)
        ]
        self.schedule_cover_loading([e.book for e in self.entries], self.inventory_list)

    @staticmethod
    def review_row(index: int, candidate: Candidate) -> dict:
        return {
            "title": ("✓ " if candidate.included else "○ ") + candidate.book.title,
            "subtitle": describe_candidate(candidate),
            "icon": None,
            "index": index,
        }

    def refresh_review(self):
        """Rebuild the review list. Changing a list's source makes some backends
        fire ``on_select``; ``_refreshing`` stops that re-entering ``toggle_row``."""
        assert self.result is not None
        self._refreshing = True
        try:
            self.review_list.data = [
                self.review_row(i, c) for i, c in enumerate(self.result.candidates)
            ]
        finally:
            self._refreshing = False
        self.update_review_summary()

    def update_review_row(self, index: int) -> None:
        """Update one row in place (keeps the selection and doesn't fire on_select)."""
        assert self.result is not None
        row = rows_of(self.review_list)[index]
        for key, value in self.review_row(index, self.result.candidates[index]).items():
            setattr(row, key, value)
        self.update_review_summary()

    def update_review_summary(self) -> None:
        assert self.result is not None
        n = self.result.included
        self.summary.text = f"{n} of {len(self.result.candidates)} selected"
        self.add_button.text = f"Add {n} book{'s' if n != 1 else ''}"
        self.add_button.enabled = n > 0

    def selected_id(self) -> int | None:
        row = self.inventory_list.selection
        return None if row is None else row_id(row)

    def set_busy(self, busy: bool) -> None:
        self.camera_button.enabled = not busy
        self.library_button.enabled = not busy
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    # ----------------------------------------------------------------- covers

    @property
    def cover_cache(self) -> Path:
        return self.paths.cache / "covers"

    def cover_icon(self, book) -> toga.Icon | None:
        base = covers.cached_cover(self.cover_cache, book)
        return None if base is None else toga.Icon(base)

    def schedule_cover_loading(self, books: list, widget: toga.DetailedList) -> None:
        """Download missing covers in the background and put them on ``widget``'s rows."""
        missing = [
            b for b in books if b.cover_url and covers.cached_cover(self.cover_cache, b) is None
        ]
        if not missing or (self._cover_task is not None and not self._cover_task.done()):
            return
        self._cover_task = asyncio.ensure_future(self.load_covers(missing, widget))

    async def load_covers(self, books: list, widget: toga.DetailedList) -> None:
        fetched = await asyncio.to_thread(
            covers.fetch_covers, self.cover_cache, books, self.download_cover
        )
        if not fetched:
            return
        for row in rows_of(widget):
            base = fetched.get(getattr(row, "cover", None) or "")
            if base is not None:
                row.icon = toga.Icon(base)

    # ------------------------------------------------------------- locations

    def pick_recent_location(self, widget, **kwargs):
        value = self.location_select.value
        if value and value != RECENT_PLACEHOLDER:
            self.location_input.value = value

    # ---------------------------------------------------------- photo sources

    async def take_photo_and_scan(self, widget, **kwargs):
        """Photograph a shelf with the camera (or the library where there's no camera)."""
        if not photos.camera_available():
            self.say("No camera on this device; choose photos from your library instead.")
            images = await self.choose_photos()
        else:
            try:
                photo = await self.camera.take_photo()
            except PermissionError:
                self.say("Camera access denied. Enable it in Settings > Book Inventory.")
                return
            images = [] if photo is None else [photo]
        await self.scan_images(images)

    async def choose_photos_and_scan(self, widget, **kwargs):
        """Pick one or more existing photos of a shelf from the photo library."""
        await self.scan_images(await self.choose_photos())

    async def choose_photos(self) -> list:
        """The photo library on iOS; a file dialog on desktop platforms."""
        try:
            return await photos.choose_photos(self.window)
        except (OSError, ValueError) as exc:  # unreadable or unsupported file
            self.say(f"Couldn't open that image: {exc}")
            return []

    # --------------------------------------------------------------- scanning

    def lookup(self, query):
        return find_matches(query, fetch=self.cached_fetch)

    def scan(self, lines) -> ScanResult:
        return scan_lines(as_ocr_lines(lines), lookup=self.lookup)

    async def scan_images(self, images: list) -> None:
        if not images:
            return  # user cancelled
        self.set_busy(True)
        n = len(images)
        self.say(f"Reading spines in {n} photo{'s' if n != 1 else ''} and looking up titles…")
        try:
            # Vision and the network lookups block, so keep them off the UI thread.
            results = []
            for image in images:
                lines = await asyncio.to_thread(self.recognize, image)
                results.append(await asyncio.to_thread(self.scan, lines))
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            self.say(f"Scan failed: {exc}")
            return
        finally:
            self.set_busy(False)

        result = merge_results(results)
        if not result.candidates:
            self.say(
                "No books recognised. Try a closer, well-lit photo of one or two rows."
                if result.line_count
                else "No text found in the photo. Try a closer, well-lit shot."
            )
            return
        self.enter_review(result)

    # ----------------------------------------------------------------- review

    def enter_review(self, result: ScanResult) -> None:
        self.result = result
        self.show_mode(REVIEW)
        self.refresh_review()
        self.say(result.describe() + " Tap to include or exclude, then add.")

    def candidate_for(self, row) -> tuple[int, Candidate] | None:
        """The review candidate behind a list row, with its index."""
        if self.result is None or row is None:
            return None
        try:
            index = int(row.index)
            return index, self.result.candidates[index]
        except (AttributeError, IndexError):
            return None

    def toggle_row(self, widget, **kwargs):
        if self._refreshing:
            return
        found = self.candidate_for(self.review_list.selection)
        if found is not None:
            index, candidate = found
            candidate.included = not candidate.included
            self.update_review_row(index)

    def skip_row(self, widget, row, **kwargs):
        found = self.candidate_for(row)
        if found is not None:
            index, candidate = found
            candidate.included = False
            self.update_review_row(index)

    def next_match_row(self, widget, row, **kwargs):
        found = self.candidate_for(row)
        if found is None:
            return
        index, candidate = found
        if candidate.next_alternative():
            self.update_review_row(index)
            self.say(f"Using “{candidate.book.title}” for “{candidate.query}”.")
        else:
            self.say(f"No other matches for “{candidate.query}”.")

    def confirm_review(self, widget, **kwargs):
        if self.result is None:
            return
        books = self.result.books
        location = self.current_location
        report = self.db.record_scan(books, location)
        self.db.set_setting(LAST_LOCATION, location)
        self.result = None
        self.refresh()
        self.show_mode(INVENTORY)
        where = f" at {report.location}" if report.location else ""
        self.say(f"{report.describe()[:-1]}{where}.")

    def cancel_review(self, widget, **kwargs):
        self.result = None
        self.summary.text = summarize(self.entries).describe()
        self.show_mode(INVENTORY)
        self.say("Scan discarded.")

    # ---------------------------------------------------------------- history

    def show_history(self, widget, **kwargs):
        self.refresh()
        self.show_mode(HISTORY)
        n = len(self.scans)
        self.say(f"{n} scan{'s' if n != 1 else ''} recorded. Swipe one to undo it.")

    def show_inventory(self, widget, **kwargs):
        self.show_mode(INVENTORY)
        self.summary.text = summarize(self.entries).describe()
        self.say("Ready.")

    def undo_row(self, widget, row, **kwargs):
        if self.db.delete_scan(row_id(row)):
            self.refresh()
            self.say("Scan undone.")

    # -------------------------------------------------------- recommendations

    @property
    def dismissed(self) -> set[str]:
        try:
            return set(json.loads(self.db.get_setting(DISMISSED, "[]")))
        except ValueError:
            return set()

    def dismiss(self, key: str) -> None:
        self.db.set_setting(DISMISSED, json.dumps(sorted(self.dismissed | {key})))

    async def show_recommendations(self, widget, **kwargs):
        self.show_mode(RECOMMEND)
        if not self.recommendations:
            await self.load_recommendations()
        else:
            self.refresh_recommendation_list()

    async def refresh_recommendations(self, widget, **kwargs):
        self.recommender = Recommender(search=self.search, fetch_work=self.fetch_work)
        await self.load_recommendations()

    async def load_recommendations(self) -> None:
        profile = build_profile(self.entries)
        if profile.empty:
            self.recommendations = []
            self.refresh_recommendation_list()
            self.say(profile.describe())
            return
        self.set_busy(True)
        self.refresh_button.enabled = False
        self.say("Finding books you might like…")
        try:
            self.recommendations = await asyncio.to_thread(
                self.recommender.recommend, self.entries, dismissed=self.dismissed
            )
        except Exception as exc:  # noqa: BLE001 - report, don't crash
            self.recommendations = []
            self.say(f"Couldn't fetch recommendations: {exc}")
            return
        finally:
            self.set_busy(False)
            self.refresh_button.enabled = True
        self.refresh_recommendation_list()
        if self.recommendations:
            self.say(profile.describe() + " Tap a suggestion for a summary.")
        else:
            self.say("No new suggestions right now. Try again after adding more books.")

    def refresh_recommendation_list(self) -> None:
        self._refreshing = True
        try:
            self._set_recommendation_rows()
        finally:
            self._refreshing = False
        n = len(self.recommendations)
        self.summary.text = f"{n} suggestion{'s' if n != 1 else ''}"
        self.schedule_cover_loading([r.book for r in self.recommendations], self.recommend_list)

    def _set_recommendation_rows(self) -> None:
        self.recommend_list.data = [
            {
                "title": r.book.title,
                "subtitle": r.describe(),
                "icon": self.cover_icon(r.book),
                "key": r.key,
                "cover": covers.cover_id(r.book.cover_url),
            }
            for r in self.recommendations
        ]

    def recommendation_for(self, row) -> Recommendation | None:
        key = getattr(row, "key", None)
        return next((r for r in self.recommendations if r.key == key), None)

    def show_summary(self, widget, **kwargs):
        """Tapping a suggestion shows its summary in the (wrapping) status line."""
        if self._refreshing:
            return
        rec = self.recommendation_for(self.recommend_list.selection)
        if rec is None:
            return
        if rec.summary:
            self.say(f"{rec.book.title}: {rec.summary}")
        else:
            self.say(f"No summary available for “{rec.book.title}”.")

    def dismiss_row(self, widget, row, **kwargs):
        rec = self.recommendation_for(row)
        if rec is None:
            return
        self.dismiss(rec.key)
        self.recommendations = [r for r in self.recommendations if r.key != rec.key]
        self.refresh_recommendation_list()
        self.say(f"Dismissed “{rec.book.title}”.")

    def own_row(self, widget, row, **kwargs):
        rec = self.recommendation_for(row)
        if rec is None:
            return
        added = self.db.add(rec.book)
        self.recommendations = [r for r in self.recommendations if r.key != rec.key]
        self.refresh()
        self.refresh_recommendation_list()
        self.say(
            f"Added “{rec.book.title}” to your inventory."
            if added
            else f"“{rec.book.title}” is already in your inventory."
        )

    # ---------------------------------------------------------------- editing

    def delete_selected(self, widget, **kwargs):
        book_id = self.selected_id()
        if book_id is None:
            self.say("Select a book to delete.")
            return
        self._delete(book_id)

    def delete_row(self, widget, row, **kwargs):
        self._delete(row_id(row))

    def _delete(self, book_id: int):
        book = self.db.get(book_id)
        self.db.delete(book_id)
        self.refresh()
        self.say(f"Deleted {book.title}." if book else "Deleted.")

    def add_copy(self, widget, **kwargs):
        self._adjust(self.selected_id(), +1)

    def remove_copy(self, widget, **kwargs):
        self._adjust(self.selected_id(), -1)

    def add_copy_to_row(self, widget, row, **kwargs):
        self._adjust(row_id(row), +1)

    def _adjust(self, book_id: int | None, delta: int):
        if book_id is None:
            self.say("Select a book to change its number of copies.")
            return
        book = self.db.get(book_id)
        if book is None:
            self.refresh()
            return
        quantity = self.db.adjust_quantity(book_id, delta)
        self.refresh()
        self.say(f"{book.title}: {quantity} cop{'ies' if quantity != 1 else 'y'}.")

    # ----------------------------------------------------------------- export

    def export_directory(self) -> Path:
        """The user's Documents folder where the platform has one, else app data."""
        try:
            return self.paths.documents
        except RuntimeError:
            return self.paths.data

    def export_inventory(self, directory: Path) -> list[Path]:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        csv_path = directory / "inventory.csv"
        json_path = directory / "inventory.json"
        csv_path.write_text(to_csv(self.entries), encoding="utf-8")
        json_path.write_text(to_json(self.entries), encoding="utf-8")
        return [csv_path, json_path]

    def export(self, widget, **kwargs):
        try:
            paths = self.export_inventory(self.export_directory())
        except OSError as exc:
            self.say(f"Export failed: {exc}")
            return
        names = " and ".join(p.name for p in paths)
        if glass.share_files(self, paths):
            self.say(f"Sharing {names}.")
        elif sys.platform == "ios":
            self.say(f"Exported {names}. Open the Files app: On My iPhone › Book Inventory.")
        else:
            self.say(f"Exported {names} to {paths[0].parent}.")


def main(**kwargs):
    return BookInventory(**kwargs)
