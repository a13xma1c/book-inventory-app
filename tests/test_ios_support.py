"""Guard against using Toga features that the iOS backend doesn't implement.

The iOS backend (toga-iOS) can't be *run* off-device - Rubicon needs the
Objective-C runtime - but its factory module lists every widget and
feature it provides. Using anything else (``toga.Table``, for instance, which
the first version of this app used) raises ``NotImplementedError`` during
``startup()`` and the app never opens. This test reads that list statically
and checks it against what ``app.py`` uses.
"""

import ast
import importlib.util
from pathlib import Path

import pytest

from bookinventory import app as app_module

spec = importlib.util.find_spec("toga_iOS.factory")
if spec is None or spec.origin is None:
    pytest.skip("toga-iOS is not installed", allow_module_level=True)


def ios_features() -> set[str]:
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "__all__":
            return {element.value for element in node.value.elts}
    raise AssertionError("toga_iOS.factory has no __all__")


def toga_attributes_used(source_path: Path) -> set[str]:
    """Names used as ``toga.<Name>`` in a module (widgets, App, Image, ...)."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "toga"
            and node.attr[:1].isupper()
        ):
            used.add(node.attr)
    return used


class TestIOSBackendSupport:
    def test_every_toga_class_the_ui_uses_exists_on_ios(self):
        features = ios_features()
        used = toga_attributes_used(Path(app_module.__file__))
        assert used, "expected app.py to reference toga classes"
        missing = sorted(used - features)
        assert not missing, f"app.py uses Toga features missing on iOS: {missing}"

    def test_table_is_not_used(self):
        # Documented regression: toga.Table is not implemented on iOS.
        assert "Table" not in toga_attributes_used(Path(app_module.__file__))
        assert "Table" not in ios_features()

    def test_camera_and_detailed_list_are_available(self):
        assert {"Camera", "DetailedList", "Image", "Paths"} <= ios_features()
