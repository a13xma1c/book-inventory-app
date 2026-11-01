"""Tests for bookinventory.glass: safe-area inset, appearance, share sheet.

UIKit is simulated through fake objects; what's verified is the calls made,
the inset arithmetic, and that every failure leaves the Toga UI usable.
"""

import logging
import sys
import types
from types import SimpleNamespace

import pytest

from bookinventory import glass


class FakeContainer:
    def __init__(self, bottom_safe_area=34.0):
        self.layouts = []
        self.on_native_layout = lambda c: self.layouts.append(("original", c.bottom_inset))
        self.bottom_inset = 0
        self.native = SimpleNamespace(
            safeAreaInsets=SimpleNamespace(bottom=bottom_safe_area),
            setNeedsLayout=lambda: self.layouts.append("setNeedsLayout"),
        )
        self.controller = SimpleNamespace()


def make_app(container, window_native=None):
    refreshed = []
    return SimpleNamespace(
        main_window=SimpleNamespace(
            _impl=SimpleNamespace(container=container, native=window_native or SimpleNamespace()),
            content=SimpleNamespace(refresh=lambda: refreshed.append(True)),
        ),
        refreshed=refreshed,
    )


@pytest.fixture
def ios(monkeypatch):
    monkeypatch.setattr(glass, "is_ios", lambda: True)


class TestOffIOS:
    def test_everything_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "linux")
        assert glass.apply(None, dark=True) is False
        assert glass.set_appearance(None, dark=True) is False
        assert glass.share_files(None, ["/x"]) is False


class TestSafeArea:
    def test_bottom_inset_tracks_native_layout(self, ios):
        container = FakeContainer(bottom_safe_area=34.0)
        app = make_app(container)
        assert glass.apply(app, dark=False) is True
        assert "setNeedsLayout" in container.layouts
        assert app.refreshed == [True]

        container.on_native_layout(container)  # UIKit lays out
        assert container.bottom_inset == 34.0
        assert container.layouts[-1] == ("original", 34.0)  # toga's own callback still runs

        container.native.safeAreaInsets.bottom = 21.0  # rotated to landscape
        container.on_native_layout(container)
        assert container.bottom_inset == 21.0

    def test_failures_are_logged_not_raised(self, ios, caplog):
        broken = SimpleNamespace(main_window=SimpleNamespace(_impl=SimpleNamespace()))
        with caplog.at_level(logging.WARNING, logger="bookinventory.glass"):
            assert glass.apply(broken, dark=True) is True
        messages = [r.getMessage() for r in caplog.records]
        assert "Could not apply safe-area insets" in messages
        assert "Could not set the interface style" in messages
        assert "Could not refresh layout" in messages


class TestAppearance:
    @pytest.mark.parametrize("dark, style", [(True, glass.DARK_STYLE), (False, glass.LIGHT_STYLE)])
    def test_sets_window_interface_style(self, ios, dark, style):
        window = SimpleNamespace(overrideUserInterfaceStyle=glass.UNSPECIFIED)
        app = make_app(FakeContainer(), window_native=window)
        assert glass.set_appearance(app, dark) is True
        assert window.overrideUserInterfaceStyle == style

    def test_uikit_values(self):
        assert (glass.UNSPECIFIED, glass.LIGHT_STYLE, glass.DARK_STYLE) == (0, 1, 2)

    def test_apply_sets_appearance(self, ios):
        window = SimpleNamespace(overrideUserInterfaceStyle=glass.UNSPECIFIED)
        glass.apply(make_app(FakeContainer(), window_native=window), dark=True)
        assert window.overrideUserInterfaceStyle == glass.DARK_STYLE


class FakeUIKit:
    def __init__(self, fail_class=None):
        self.fail_class = fail_class
        self.presented = []
        kit = self

        class NSURL:
            @classmethod
            def fileURLWithPath(cls, path):
                return f"url:{path}"

        class Popover:
            sourceView = None
            sourceRect = None

        class Activity:
            popoverPresentationController = Popover()

            @classmethod
            def alloc(cls):
                return cls

            @classmethod
            def initWithActivityItems(cls, items, applicationActivities):
                controller = cls()
                controller.items = items
                return controller

        self.Activity = Activity
        classes = {"NSURL": NSURL, "UIActivityViewController": Activity}

        def objc_class(name):
            if name == kit.fail_class:
                raise NameError(name)
            return classes[name]

        self.module = types.ModuleType("rubicon.objc")
        self.module.ObjCClass = objc_class

    def install(self, monkeypatch):
        rubicon = types.ModuleType("rubicon")
        rubicon.objc = self.module
        monkeypatch.setitem(sys.modules, "rubicon", rubicon)
        monkeypatch.setitem(sys.modules, "rubicon.objc", self.module)


class TestShareFiles:
    def test_presents_activity_controller(self, ios, monkeypatch):
        kit = FakeUIKit()
        kit.install(monkeypatch)
        presented = []
        presenter = SimpleNamespace(
            view=SimpleNamespace(bounds="bounds"),
            presentViewController=lambda c, animated, completion: presented.append(c),
        )
        container = FakeContainer()
        container.controller = presenter
        assert glass.share_files(make_app(container), ["/a/inventory.csv", "/a/inventory.json"])
        [controller] = presented
        assert controller.items == ["url:/a/inventory.csv", "url:/a/inventory.json"]
        assert kit.Activity.popoverPresentationController.sourceRect == "bounds"

    def test_failure_is_logged(self, ios, monkeypatch, caplog):
        kit = FakeUIKit(fail_class="UIActivityViewController")
        kit.install(monkeypatch)
        with caplog.at_level(logging.WARNING, logger="bookinventory.glass"):
            assert glass.share_files(make_app(FakeContainer()), ["/x"]) is False
        assert "share sheet" in caplog.text

    @pytest.mark.parametrize("platform, expected", [("ios", True), ("darwin", False)])
    def test_is_ios(self, monkeypatch, platform, expected):
        monkeypatch.setattr(sys, "platform", platform)
        assert glass.is_ios() is expected
