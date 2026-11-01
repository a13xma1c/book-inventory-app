"""Native iOS touches that Toga can't express: screen shape, appearance, sharing.

* **The shape of the screen.** toga-iOS insets its layout for the navigation
  bar at the top but not for the home indicator at the bottom, so a bottom row
  of controls ends up in the home-indicator zone. :func:`apply` keeps the
  layout's bottom inset in step with the safe area.
* **Appearance.** The app's own dark mode is applied to UIKit as well
  (:func:`set_appearance`), so system-drawn parts - list cells, text fields,
  the navigation bar - switch with the rest of the screen.
* **Sharing.** Exports are handed to the system share sheet
  (:func:`share_files`).

Everything reaches through Toga's implementation objects to UIKit via Rubicon,
is independent and failure-tolerant (a warning is logged, the Toga UI stays
usable), and is a no-op off iOS.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

# UIUserInterfaceStyle
UNSPECIFIED, LIGHT_STYLE, DARK_STYLE = 0, 1, 2


def is_ios() -> bool:
    return sys.platform == "ios"


def respect_safe_area(app) -> None:
    """Inset Toga's layout by the bottom safe area (home indicator).

    toga-iOS recomputes the top inset on every native layout pass; this wraps
    that callback so the bottom inset is recomputed alongside it.
    """
    container = app.main_window._impl.container
    original = container.on_native_layout

    def on_native_layout(c):
        c.bottom_inset = c.native.safeAreaInsets.bottom
        original(c)

    container.on_native_layout = on_native_layout
    container.native.setNeedsLayout()


def set_appearance(app, dark: bool) -> bool:
    """Force UIKit into dark or light appearance for this app's window."""
    if not is_ios():
        return False
    try:
        window = app.main_window._impl.native  # the UIWindow
        window.overrideUserInterfaceStyle = DARK_STYLE if dark else LIGHT_STYLE
    except Exception:  # noqa: BLE001 - never let polish take the app down
        log.warning("Could not set the interface style", exc_info=True)
        return False
    return True


def share_files(app, paths) -> bool:
    """Present the system share sheet for ``paths``. Returns whether it was shown."""
    if not is_ios():
        return False
    try:
        from rubicon.objc import ObjCClass

        NSURL = ObjCClass("NSURL")
        items = [NSURL.fileURLWithPath(str(path)) for path in paths]
        controller = (
            ObjCClass("UIActivityViewController")
            .alloc()
            .initWithActivityItems(items, applicationActivities=None)
        )
        presenter = app.main_window._impl.container.controller
        popover = controller.popoverPresentationController  # iPad: anchor the popover
        if popover is not None:
            popover.sourceView = presenter.view
            popover.sourceRect = presenter.view.bounds
        presenter.presentViewController(controller, animated=True, completion=None)
    except Exception:  # noqa: BLE001
        log.warning("Could not present the share sheet", exc_info=True)
        return False
    return True


def apply(app, dark: bool) -> bool:
    """Apply the native touches to ``app``. Returns whether anything was applied."""
    if not is_ios():
        return False
    try:
        respect_safe_area(app)
    except Exception:  # noqa: BLE001
        log.warning("Could not apply safe-area insets", exc_info=True)
    set_appearance(app, dark)
    try:
        app.main_window.content.refresh()
    except Exception:  # noqa: BLE001
        log.warning("Could not refresh layout", exc_info=True)
    return True
