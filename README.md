# Book Inventory (iOS, Python)

Take photos of your bookshelves; the app reads the spines and builds an aggregated inventory of every title you own, how many copies, and where they are.

Built with [BeeWare](https://beeware.org) (Toga + Briefcase), so the app is written in Python and packaged as a native iOS app.

## How it works

Scanning is a pipeline: photo → OCR lines with geometry → lines grouped per
spine → concurrent, cached Open Library lookups → a **review list** you confirm
→ the inventory. Nothing is recorded until you tap *Add*.

1. `app.py` – Toga UI with three modes. **Inventory**: a `DetailedList` (cover, title; author · copies · where) with swipe actions (delete / +1 copy), summary and status lines, a location field with recent locations. **Review**: the books a scan found – tap to include/exclude, swipe to skip or switch to the next alternative match – then *Add*. **History**: recorded scans; swipe to undo one. All actions are plain text buttons in clearly separated rows below the list, swapped per mode; a *Dark mode* switch picks the colour scheme.
2. `themes.py` – the colour scheme: a warm light palette (parchment, ink, terracotta accents, teal actions) and a dark counterpart; the choice is remembered.
   (`toga.Table` is **not** implemented by the iOS backend – using it crashes the app at startup – so the list widget is `DetailedList`; `tests/test_ios_support.py` guards this.)
3. `glass.py` – native iOS touches: keeps the layout out of the home-indicator zone, switches UIKit's appearance with the app's dark mode, and presents the share sheet.
4. `photos.py` – *Choose Photos*: `PHPickerViewController` on iOS (multi-select, no permission prompt; falls back to `UIImagePickerController`), a multi-file dialog on desktop; plus the "is there a camera?" check: Toga's `take_photo()` silently returns `None` where there is none (the Simulator), so *Take Photo* offers the library instead.
5. `ocr.py` – Apple's Vision framework (via Rubicon-ObjC) reads text on-device. The photo is scanned upright and rotated both ways, since spine text is vertical.
5. `matching.py` – drops noisy OCR lines and scores search results against the OCR text.
6. `lookup.py` – looks up each line on the free [Open Library](https://openlibrary.org/dev/docs/api/search) API and keeps results that match.
7. `scanner.py` – turns one photo's OCR lines into a de-duplicated list of books.
8. `database.py` – stores books, scans and sightings in SQLite on the device and aggregates each scan into the inventory.
9. `aggregation.py` – inventory aggregation: entries with locations, summaries, grouping, search, CSV/JSON export.

## Inventory aggregation

One photo gives a list of books; a real inventory comes from many photos of many shelves, and the same title keeps turning up. Every scan is therefore recorded with **where** it was taken (type a location such as "Study, shelf 2" before scanning) and folded into the inventory by `Database.record_scan`:

- a title not yet in the inventory is **added** (one copy);
- a known title seen in a **new location** is **another copy** – its count goes up;
- a known title re-seen where it has already been seen (an overlapping photo of the same shelf) is **left alone**;
- blank year/ISBN fields are filled in from later sightings, never overwritten.

After each scan the status line reports what changed ("Found 3 books (1 new, 2 already listed) at Study."). Locations are matched case-insensitively, and the `+1` / `-1` buttons let you correct a count by hand (e.g. two copies side by side on one shelf). `Database.delete_scan` undoes a scan.

`aggregation.py` provides the read side: `build_inventory` joins titles with their sightings (locations, times seen, first/last seen); `summarize` gives totals plus breakdowns by location, author and decade; `group_by_*`, `find_duplicates` and `search` slice the inventory; `merge_books` combines several photos of one shelf; `to_csv` / `to_json` export (the **Export** button writes `inventory.csv` and `inventory.json` to the app's Documents folder, visible in the Files app).

Existing `books.sqlite3` databases are migrated in place (v1 → v2 adds quantities, scans and sightings; v2 → v3 adds work keys, covers, a stable identity and settings; v3 → v4 adds subjects). Books recorded before v4 have no subjects, so recommendations for them rest on authors until they're re-scanned.

## Look and feel, and the shape of the screen

- **Actions are text buttons** – no icons – in rows below the list with a
  fixed gap between them, and only the current mode's rows are shown. Toga's
  `display: none` style isn't honoured by native layouts (every row would be
  visible at once), so rows are added to and removed from their container
  instead.
- **Colour scheme** – `themes.py` defines a warm light palette and a dark one,
  applied through `Pack` styles so they render on every platform. The
  *Dark mode* switch flips between them and the choice is stored in the
  database. On iOS, `glass.py` also sets the window's
  `overrideUserInterfaceStyle`, so the parts UIKit draws itself (list cells,
  text fields, the navigation bar) switch with the rest of the screen.
- **Shape of the screen** – toga-iOS insets its layout for the navigation bar
  at the top but never for the bottom safe area, so `glass.py` wraps its
  layout callback to apply `safeAreaInsets.bottom`; the button rows stay
  clear of the home indicator.
- **Liquid Glass** – the navigation bar is drawn by the system and gets the
  iOS 26 glass treatment on its own. Glass *controls* inside the content
  would need either custom UIKit or SwiftUI – see below.

### Can this integrate with SwiftUI?

Not from Python directly: SwiftUI is a Swift-only API (no Objective-C
surface), so Rubicon can't reach it, and Toga has no SwiftUI backend. Three
realistic routes:

1. **Stay native-UIKit via Rubicon**: reach the UIKit classes that get
   Liquid Glass for free – bars, `UIButton` configurations
   (`glass`/`prominentGlass` on iOS 26), `UIVisualEffectView` with
   `UIGlassEffect` – from Python, the way `glass.py` reaches the window and
   share sheet. No Swift, no template changes.
2. **Embed SwiftUI views in this app**: write the SwiftUI view in Swift with
   an `@objc` factory (e.g. `@objc class GlassPanel: NSObject { @objc static
   func controller(json: String) -> UIViewController }` returning a
   `UIHostingController`), add the Swift file to the Xcode project via a
   forked Briefcase iOS template (`template = ...` in `pyproject.toml`; the
   generated project is rebuilt on `briefcase create`), and present it from
   Python with `ObjCClass("GlassPanel").controller(json=to_json(entries))` on
   the window's `UINavigationController`. Data crosses as ObjC-visible types
   (strings/JSON, `NSArray`/`NSDictionary`); callbacks via ObjC blocks or
   `NotificationCenter`. Practical for a few screens; the rest stays Toga.
3. **Invert it – a SwiftUI app with a Python core**: build the UI in Xcode
   with SwiftUI and embed Python (Python-Apple-support + PythonKit) to call
   `scanner`, `database` and `aggregation`, which are UI-independent and
   fully tested. This gives the complete Liquid Glass experience (glass
   buttons, `.glassEffect()`, morphing transitions, large titles) at the cost
   of leaving Toga/Briefcase's UI layer.

## Requirements

- macOS with **full Xcode** installed (the Command Line Tools alone are not enough)
- [uv](https://docs.astral.sh/uv/) – manages the Python environment and dependencies
- Python 3.11+ (uv will download one if needed)

## Run

Dependencies are declared in `pyproject.toml` and pinned in `uv.lock`. `uv sync`
creates `.venv` with the app's dependencies, the test extras and the dev tools
(Briefcase, pytest, coverage, ruff, toga-dummy); `uv run` executes commands in it.

```bash
uv sync                                  # one-off (and after changing dependencies)

uv run pytest                            # full test-suite, no device needed
uv run pytest --cov=bookinventory        # with coverage
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # type check

uv run briefcase create iOS
uv run briefcase build iOS
uv run briefcase run iOS                 # launches in the iOS Simulator
uv run briefcase run iOS --test -r       # runs the tests on the Simulator (UI tests skip there)
```

The `-r` (`--update-requirements`) matters the first time you run in test
mode and whenever `test_requires` change: Briefcase only reinstalls
requirements into an existing build when asked, and pytest is a *test*
requirement – without `-r` the bundle built for normal runs has no pytest and
the run ends with `ModuleNotFoundError: No module named 'pytest'`. Once
installed, plain `uv run briefcase run iOS --test` is enough.

Add a dependency with `uv add <package>` (runtime) or `uv add --dev <package>`
(tooling); both update `uv.lock`. Runtime dependencies in `[project]` are picked
up by Briefcase automatically, alongside the per-platform backends declared in
`[tool.briefcase.app.bookinventory.<platform>]`.

Briefcase uses uv internally as well (`env_manager = "uv"` in
`[tool.briefcase.app.bookinventory]`): the isolated environment it builds the
app's requirements into – for iOS, macOS or `briefcase dev --isolated` – is
created with `uv venv` and populated with `uv pip install` rather than
`venv` + `pip`. It only needs `uv` on your `PATH`, which is always the case
when Briefcase is run via `uv run`.

The Simulator has no real camera. `take_photo()` falls back to a photo-library picker there. To use the camera, run on a device (`briefcase run iOS -d`, requires an Apple developer team).

## Tests

`tests/` holds ~200 tests that run anywhere, with no device, camera or network:

| File | Covers |
| --- | --- |
| `test_core.py` | the original smoke tests (unchanged) |
| `test_matching.py` | OCR line filtering, fuzzy token coverage, title-aware scoring, match threshold |
| `test_lookup.py` | request building, ranking/alternatives, cache, concurrency, retries/back-off |
| `test_ocr.py` | how Vision is driven (orientations, options, confidence, geometry, native images) against a simulated Objective-C API |
| `test_spines.py` | grouping lines per spine by geometry |
| `test_scanner.py` | spines → concurrent lookups → candidates with alternatives; over-grouping fallback; merging photos |
| `test_covers.py` | cover download/caching, best-effort failures |
| `test_recommend.py` | taste profile, subject cleaning, scoring and reasons, ranking, diversity cap, exclusions, caching, concurrency, summaries (trimming, fallback, failures) |
| `test_database.py` | storage, `record_scan` aggregation rules, quantities, undo, v1 → v2 migration |
| `test_aggregation.py` | entries, summaries, grouping, duplicates, search, CSV/JSON export |
| `test_photos.py` | the pickers against a simulated UIKit/PhotosUI: multi-select `PHPicker`, legacy fallback, delegate callbacks, cancel, desktop dialogs |
| `test_app.py` | the real Toga UI via the `toga-dummy` backend: scan → review (toggle, skip, next match, cancel, add) → inventory, multi-photo, history/undo, *For you* (dismiss, own it, refresh, failures), covers, recent locations, busy state, swipe actions, export/share, text-only button rows per mode, theme and dark-mode persistence |
| `test_glass.py` | safe-area inset tracking, appearance override, share sheet – against simulated UIKit; every failure is logged, never raised |
| `test_themes.py` | palettes are valid, light/dark really are, text contrasts with background |
| `test_on_device.py` | (iOS only) the Objective-C classes/selectors the bridges use exist; Vision runs on a generated image |
| `test_ios_support.py` | every `toga.*` class used by the UI exists in the iOS backend (catches `toga.Table`-style startup crashes) |
| `test_integration.py` | end-to-end: photos → lookup → database → aggregation → export |

`tests/conftest.py` provides a fake Open Library serving a small canned catalogue and canned OCR output for a few "photos". `tests/bookinventory.py` is the entry point Briefcase uses for `briefcase run --test`.

## Notes and limitations

- Not run on iOS in this environment (no Xcode). The Vision calls in `ocr.py` and the picker in `photos.py` use Rubicon-ObjC keyword-argument syntax, mirror toga-iOS's own code, and are verified against simulated Objective-C APIs; if a call fails on first run, those files are the first place to check. The on-device test run was rehearsed by executing `tests/bookinventory.py` from a read-only copy of the bundle layout with only the bundle's dependencies installed.
- `uv.lock` was resolved against a package index snapshot that did not yet have Briefcase 0.4.7's dependencies, so it pins Briefcase 0.4.6; run `uv lock --upgrade-package briefcase` to move forward.
- iOS labels don't word-wrap, so status messages are kept short and broken into lines by `wrap_status`; anything long (like a path) would be clipped.
- Recognition quality depends on photo quality: take close, well-lit shots of one or two shelf rows at a time. Spines with stylized fonts may not be recognized.
- Lookups need an internet connection; OCR does not.
- Edit `bundle` (under `[tool.briefcase]`) and `authors` (under `[project]`) in `pyproject.toml` before building.
